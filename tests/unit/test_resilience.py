"""Reconnection: opt-in, paced by a mandatory policy, and impossible to do by accident.

DESIGN.md graft G12. The client never rebuilds its own socket -- ``FAILED`` is sticky and
the socket is closed -- because a reconnect hidden inside a failed read is what makes
another library's latency numbers unreadable, and because on FX5U-32MT/DS fw 1.065 a
second connection to a one-entry configuration completes its handshake and is then FINed
(2026-09-06), so an unpaced retry is a spin loop with a socket in it.

Three things here are the unit's whole contract and each has its own test:

* ``Supervisor(client, policy=...)`` -- ``policy`` has **no default**, asserted by
  reflection rather than by reading the signature;
* a transaction submitted while a reconnect is under way **raises immediately** rather
  than waiting out the backoff;
* a reconnect that lands on a different CPU closes the connection and raises
  :class:`~aslmp.errors.SlmpTargetChangedError` (graft G6).

The reconnect tests run against a simulator bound to a *fixed* port, stopped and started
again, so the recovery is a real socket recovery rather than a mocked one.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect

import pytest

from aslmp.client import Plc
from aslmp.connection import ConnectionState
from aslmp.errors import (
    SlmpConfigurationError,
    SlmpError,
    SlmpNotConnectedError,
    SlmpTargetChangedError,
    SlmpTransportError,
)
from aslmp.observability import (
    ConnectionEvent,
    Reconnected,
    Reconnecting,
    TargetChanged,
)
from aslmp.resilience import ExponentialBackoff, ReconnectPolicy, Supervisor
from aslmp.testing.server import Entry, PlcSimulator
from aslmp.testing.targets import FX5U_32MT_DS, SimulatorTarget

pytestmark = pytest.mark.simulator

FX5U = "melsec:iq-f/fx5u"
SWAPPED = dataclasses.replace(
    FX5U_32MT_DS, model_code=0x4A21, model_name="FX5U-32MR/ES", key="fx5u-32mr-es"
)
"""A different CPU that the *same* profile claims, so the handshake passes and only the
model code says the machine changed. That is exactly the case graft G6 exists for: ``Y20``
is output 16 on both, and both answer end code 0x0000."""


async def a_free_port() -> int:
    """Bind an ephemeral port with a simulator, note it, give it back."""
    probe = PlcSimulator(entries=(Entry(name="tcp", protocol="tcp"),))
    await probe.start()
    port = probe.port_of("tcp")
    await probe.aclose()
    return port


async def simulator_on(port: int, *, target: SimulatorTarget = FX5U_32MT_DS) -> PlcSimulator:
    simulator = PlcSimulator(
        target=target, entries=(Entry(name="tcp", protocol="tcp", port=port),)
    )
    await simulator.start()
    return simulator


def client_on(port: int) -> Plc:
    return Plc(
        "127.0.0.1", port, profile=FX5U, timeout=1.0, connect_timeout=1.0, name="bench"
    )


def fast() -> ExponentialBackoff:
    """A policy with the shape of the real one and none of its patience."""
    return ExponentialBackoff(initial=0.02, maximum=0.05, jitter=0.0)


# ========================================================================================
# The policy has no default
# ========================================================================================


def test_the_backoff_policy_is_required_and_keyword_only() -> None:
    """You cannot get backoff by accident, and you cannot get none by accident either."""
    parameter = inspect.signature(Supervisor.__init__).parameters["policy"]
    assert parameter.default is inspect.Parameter.empty
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


def test_the_client_itself_offers_no_automatic_reconnection() -> None:
    """``Plc.reconnect`` exists and is explicit; nothing schedules it but a Supervisor."""
    assert {"auto_reconnect", "retry", "reconnect_forever"}.isdisjoint(dir(Plc))
    assert "reason" in inspect.signature(Plc.reconnect).parameters


def test_exponential_backoff_satisfies_the_protocol() -> None:
    assert isinstance(fast(), ReconnectPolicy)


# ========================================================================================
# ExponentialBackoff, arithmetic only
# ========================================================================================


def test_the_delay_doubles_and_then_stops_at_the_maximum() -> None:
    policy = ExponentialBackoff(initial=0.25, maximum=2.0, jitter=0.0)
    assert [policy.delay(n) for n in range(1, 6)] == [0.25, 0.5, 1.0, 2.0, 2.0]


def test_the_exponent_cannot_overflow_a_float() -> None:
    """A supervisor that has been retrying for a week must not die of ``2 ** 100000``."""
    policy = ExponentialBackoff(initial=0.25, maximum=30.0, jitter=0.0)
    assert policy.delay(100_000) == 30.0


def test_the_jitter_is_symmetric_around_the_base_delay() -> None:
    values = iter([0.0, 1.0, 0.5])
    policy = ExponentialBackoff(
        initial=1.0, maximum=1.0, jitter=0.2, random_source=lambda: next(values)
    )
    assert policy.delay(1) == pytest.approx(0.8)
    assert policy.delay(1) == pytest.approx(1.2)
    assert policy.delay(1) == pytest.approx(1.0)


def test_give_up_is_attempt_shaped_and_says_so() -> None:
    forever = ExponentialBackoff()
    assert forever.give_up(1_000_000, elapsed=1e9) is False
    bounded = ExponentialBackoff(max_attempts=3)
    assert [bounded.give_up(n, elapsed=0.0) for n in (1, 2, 3, 4)] == [
        False,
        False,
        True,
        True,
    ]


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"initial": 0.0}, "must be positive"),
        ({"initial": 1.0, "maximum": 0.5}, "below the initial"),
        ({"jitter": 1.0}, r"\[0, 1\)"),
        ({"jitter": -0.1}, r"\[0, 1\)"),
        ({"max_attempts": 0}, "at least 1"),
    ],
)
def test_an_incoherent_policy_is_refused(kwargs: dict[str, object], match: str) -> None:
    with pytest.raises(SlmpConfigurationError, match=match):
        ExponentialBackoff(**kwargs)  # type: ignore[arg-type]  # kwargs


def test_attempt_numbers_start_at_one() -> None:
    with pytest.raises(SlmpConfigurationError, match="start at 1"):
        ExponentialBackoff().delay(0)


# ========================================================================================
# The supervisor, against a socket that really goes away
# ========================================================================================


async def break_it(simulator: PlcSimulator, plc: Plc) -> None:
    """Stop the server, then make the client find out. Nothing tells it by itself."""
    await simulator.aclose()
    with pytest.raises(SlmpError):
        await plc.read_f32("D0")


async def test_a_broken_connection_is_rebuilt_and_every_step_is_an_event() -> None:
    port = await a_free_port()
    simulator = await simulator_on(port)
    plc = client_on(port)
    events: list[ConnectionEvent] = []
    plc.add_event_listener(events.append)
    revived: PlcSimulator | None = None
    try:
        async with Supervisor(plc, policy=fast()) as supervisor:
            assert plc.state is ConnectionState.READY
            before = plc.generation
            await break_it(simulator, plc)
            revived = await simulator_on(port)
            await supervisor.wait_ready(timeout=10.0)
            assert supervisor.reconnects == 1
            assert supervisor.failure is None
            assert plc.generation > before
            assert plc.counters.reconnects == 1
            assert await plc.read_f32("D0") == pytest.approx(0.0)
    finally:
        await plc.aclose()
        if revived is not None:
            await revived.aclose()
    attempts = [event for event in events if isinstance(event, Reconnecting)]
    landed = [event for event in events if isinstance(event, Reconnected)]
    assert attempts, "a reconnect that emits nothing is a reconnect nobody can attribute"
    assert attempts[0].attempt == 1
    assert attempts[0].delay_s == pytest.approx(0.02)
    assert len(landed) == 1
    assert landed[0].previous_generation < landed[0].generation


async def test_a_transaction_during_a_reconnect_raises_rather_than_waiting() -> None:
    """The point of graft G12: a control loop is told, immediately, on its own cycle."""
    port = await a_free_port()
    simulator = await simulator_on(port)
    plc = client_on(port)
    slow = ExponentialBackoff(initial=5.0, maximum=5.0, jitter=0.0)
    try:
        async with Supervisor(plc, policy=slow):
            await break_it(simulator, plc)
            loop = asyncio.get_running_loop()
            started = loop.time()
            with pytest.raises(SlmpNotConnectedError) as caught:
                await plc.read_f32("D0")
            assert loop.time() - started < 1.0  # it did not wait out a 5 s backoff
            assert caught.value.reason == "failed"
            assert "no automatic reconnection" in str(caught.value)
    finally:
        await plc.aclose()


async def test_a_policy_that_gives_up_keeps_the_reason_and_hands_it_to_every_waiter() -> None:
    port = await a_free_port()
    simulator = await simulator_on(port)
    plc = client_on(port)
    policy = ExponentialBackoff(initial=0.01, maximum=0.01, jitter=0.0, max_attempts=2)
    try:
        async with Supervisor(plc, policy=policy) as supervisor:
            await break_it(simulator, plc)
            with pytest.raises(SlmpTransportError):
                await supervisor.wait_ready(timeout=10.0)
            assert supervisor.surrendered is True
            assert supervisor.reconnects == 0
            assert isinstance(supervisor.failure, SlmpTransportError)
    finally:
        await plc.aclose()


async def test_a_reconnect_onto_a_different_cpu_closes_the_connection_and_raises() -> None:
    """Graft G6. Both CPUs answer 0x0000; only the ``0101`` model code says otherwise."""
    port = await a_free_port()
    simulator = await simulator_on(port)
    plc = client_on(port)
    events: list[ConnectionEvent] = []
    plc.add_event_listener(events.append)
    swapped: PlcSimulator | None = None
    try:
        async with Supervisor(plc, policy=fast()) as supervisor:
            assert plc.model_code == 0x4A49
            await break_it(simulator, plc)
            swapped = await simulator_on(port, target=SWAPPED)
            with pytest.raises(SlmpTargetChangedError) as caught:
                await supervisor.wait_ready(timeout=10.0)
            assert caught.value.expected_model_code == 0x4A49
            assert caught.value.actual_model_code == 0x4A21
            assert supervisor.surrendered is True
            assert supervisor.reconnects == 0
            assert plc.state is ConnectionState.CLOSED
    finally:
        await plc.aclose()
        if swapped is not None:
            await swapped.aclose()
    changed = [event for event in events if isinstance(event, TargetChanged)]
    assert len(changed) == 1
    assert changed[0].previous_model_code == 0x4A49
    assert changed[0].model_code == 0x4A21


async def test_closing_the_client_stands_the_supervisor_down() -> None:
    """Supervising a client somebody deliberately shut would be reconnecting it."""
    port = await a_free_port()
    simulator = await simulator_on(port)
    plc = client_on(port)
    try:
        supervisor = Supervisor(plc, policy=fast())
        await supervisor.start()
        await plc.aclose()
        await asyncio.sleep(0.05)
        assert supervisor.supervising is False
        await supervisor.aclose()
        assert supervisor.reconnects == 0
    finally:
        await simulator.aclose()


async def test_the_supervisor_does_not_close_the_client_it_was_given() -> None:
    port = await a_free_port()
    simulator = await simulator_on(port)
    plc = client_on(port)
    try:
        async with Supervisor(plc, policy=fast()):
            pass
        assert plc.state is ConnectionState.READY
        assert await plc.read_f32("D0") == pytest.approx(0.0)
    finally:
        await plc.aclose()
        await simulator.aclose()


# ========================================================================================
# Refusals
# ========================================================================================


async def test_starting_twice_is_refused() -> None:
    port = await a_free_port()
    simulator = await simulator_on(port)
    plc = client_on(port)
    try:
        supervisor = Supervisor(plc, policy=fast())
        await supervisor.start()
        with pytest.raises(SlmpConfigurationError, match="already running"):
            await supervisor.start()
        await supervisor.aclose()
    finally:
        await plc.aclose()
        await simulator.aclose()


async def test_waiting_on_a_supervisor_that_was_never_started_is_refused() -> None:
    supervisor = Supervisor(client_on(5002), policy=fast())
    with pytest.raises(SlmpConfigurationError, match="has not been started"):
        await supervisor.wait_ready()


async def test_wait_ready_reports_the_state_rather_than_a_stale_success() -> None:
    port = await a_free_port()
    simulator = await simulator_on(port)
    plc = client_on(port)
    slow = ExponentialBackoff(initial=5.0, maximum=5.0, jitter=0.0)
    try:
        async with Supervisor(plc, policy=slow) as supervisor:
            await break_it(simulator, plc)
            with pytest.raises(SlmpNotConnectedError, match=r"waited 0\.05 s"):
                await supervisor.wait_ready(timeout=0.05)
    finally:
        await plc.aclose()


def test_the_supervisor_repr_says_what_it_is_doing() -> None:
    supervisor = Supervisor(client_on(5002), policy=fast())
    assert "reconnects=0" in repr(supervisor)
    assert "supervising=False" in repr(supervisor)
