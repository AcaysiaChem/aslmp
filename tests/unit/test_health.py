"""The health monitor, and the one rule that shapes it: **skip, count, never queue**.

DESIGN.md graft G5. An SLMP connection entry on FX5U-32MT/DS fw 1.065 carries one
transaction at a time -- two TCP requests written before the first response is read
return ONE response, for the LAST request, with end code ``0x0000`` (2026-09-06) -- so a
probe that waited for the in-flight slot would delay the traffic it is measuring, and a
probe that raised would turn a busy moment into an alarm.

The central test here holds the gate open by hand and asserts three things at once: the
probe returned ``SKIPPED_BUSY``, ``counters.probes_skipped`` went up, and **nothing
joined the queue** -- ``gate.waiting`` stayed at zero and the simulator saw no ``0619``.
The rest of the file is the hysteresis and the schedule, both on an injected clock.
"""

from __future__ import annotations

import asyncio

import pytest

from aslmp import ConnectionState
from aslmp.client import Plc
from aslmp.commands.info import DEFAULT_LOOPBACK
from aslmp.errors import SlmpConfigurationError, SlmpSinkError
from aslmp.health import PROBE_COMMAND, HealthMonitor, HealthSnapshot, ProbeOutcome
from aslmp.observability import ConnectionEvent, ProbeSkipped
from aslmp.profile import Encoding
from aslmp.testing.server import PlcSimulator
from aslmp.timing import Nanos, TimingBuilder, Transaction, TransactionTiming
from aslmp.transport.base import TransportKind
from aslmp.wire.frames import FrameType

pytestmark = pytest.mark.simulator

FX5U = "melsec:iq-f/fx5u"
MS = 1_000_000


class FakeClock:
    def __init__(self, start: int = 1_000_000_000) -> None:
        self.now = start

    def __call__(self) -> int:
        return self.now

    def advance(self, ns: int) -> None:
        self.now += ns


class RecordingSleep:
    def __init__(self, clock: FakeClock) -> None:
        self._clock = clock
        self.calls: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.calls.append(delay)
        self._clock.advance(round(delay * 1_000_000_000))
        await asyncio.sleep(0)  # a fake sleep must still yield, or run() never lets go


def a_timing(clock: FakeClock, *, wire_ns: int = 7_000_000) -> TransactionTiming:
    builder = TimingBuilder(clock)
    builder.gate_acquired()
    builder.encoded()
    builder.sent()
    clock.advance(wire_ns)
    builder.chunk(11)
    builder.decoded()
    return builder.build()


def a_tx(
    clock: FakeClock, *, end_code: int = 0, command: int = 0x0401
) -> Transaction:
    return Transaction(
        timing=a_timing(clock),
        sequence=1,
        connection_id="c1",
        generation=0,
        after_reconnect=False,
        command=command,
        subcommand=0x0000,
        frame=FrameType.THREE_E,
        encoding=Encoding.BINARY,
        transport=TransportKind.TCP,
        serial=None,
        request_bytes=21,
        response_bytes=13,
        end_code=end_code,
        prebuilt=False,
    )


def a_client(port: int = 5002) -> Plc:
    """Constructing one opens no socket, so most of this file needs no simulator."""
    return Plc("192.168.10.250", port, profile=FX5U, name="bench")


def verdict(monitor: HealthMonitor) -> bool:
    """``monitor.healthy`` behind a call, so mypy does not narrow it across two asserts."""
    return monitor.healthy


def client_for(simulator: PlcSimulator, entry: str = "tcp") -> Plc:
    host, port = simulator.address(entry)
    return Plc(host, port, profile=FX5U, timeout=2.0, name=entry)


# ========================================================================================
# Graft G5 -- the skip
# ========================================================================================


async def test_a_probe_that_finds_the_gate_held_is_skipped_and_counted() -> None:
    """Skipped, counted, reported -- and above all **not queued** behind live traffic."""
    async with PlcSimulator() as simulator:
        plc = client_for(simulator)
        await plc.connect()
        events: list[ConnectionEvent] = []
        plc.add_event_listener(events.append)
        monitor = HealthMonitor(plc)
        gate = plc._conn.gate
        simulator.clear_transcript()
        try:
            async with gate.lease(sequence=9_001, command=0x0403):
                outcome = await monitor.probe_once()
                assert outcome is ProbeOutcome.SKIPPED_BUSY
                assert gate.waiting == 0  # it stood down; it did not join the queue
            assert plc.counters.probes_skipped == 1
            assert plc.counters.probes_sent == 0
            assert simulator.transcript == ()  # nothing reached the wire
        finally:
            await plc.aclose()
        skipped = [event for event in events if isinstance(event, ProbeSkipped)]
        assert len(skipped) == 1
        assert "0x0403" in skipped[0].reason
        assert "never queued" in skipped[0].reason
        assert skipped[0].connection_id == plc._conn.connection_id


async def test_a_skipped_probe_leaves_the_hysteresis_untouched() -> None:
    """A skip is not evidence about the connection; only a probe or real traffic is."""
    async with PlcSimulator() as simulator:
        plc = client_for(simulator)
        await plc.connect()
        monitor = HealthMonitor(plc)
        try:
            async with plc._conn.gate.lease(sequence=9_002):
                await monitor.probe_once()
            snapshot = monitor.snapshot()
            assert snapshot.observed == 0
            assert snapshot.consecutive_failures == 0
            assert snapshot.consecutive_successes == 0
        finally:
            await plc.aclose()


async def test_a_probe_against_an_unconnected_client_stands_down_without_counting() -> None:
    """``probes_skipped`` means "stood down for live traffic". Nothing else dilutes it."""
    plc = a_client()
    monitor = HealthMonitor(plc)
    assert await monitor.probe_once() is ProbeOutcome.SKIPPED_UNUSABLE
    assert plc.counters.probes_skipped == 0
    assert plc.counters.probes_sent == 0
    assert verdict(monitor) is False


# ========================================================================================
# The probe itself
# ========================================================================================


async def test_an_idle_connection_is_probed_with_0619() -> None:
    async with PlcSimulator() as simulator:
        plc = client_for(simulator)
        await plc.connect()
        monitor = HealthMonitor(plc)
        simulator.clear_transcript()
        try:
            assert await monitor.probe_once() is ProbeOutcome.SENT
            assert plc.counters.probes_sent == 1
            sent = [record for record in simulator.transcript if record.direction == "rx"]
            assert len(sent) == 1
            assert sent[0].data[11:13] == b""  # 0x0619, little endian
            assert sent[0].data.endswith(DEFAULT_LOOPBACK)
        finally:
            await plc.aclose()


async def test_a_failing_probe_is_recorded_and_never_raised() -> None:
    """A monitor whose job is to report a broken connection must not die of one."""
    async with PlcSimulator() as simulator:
        plc = client_for(simulator)
        await plc.connect()
        monitor = HealthMonitor(plc, unhealthy_after=1)
        await simulator.aclose()
        try:
            assert await monitor.probe_once() is ProbeOutcome.FAILED
            snapshot = monitor.snapshot()
            assert snapshot.healthy is False
            assert snapshot.failures == 1
            assert snapshot.last_failure is not None
            assert "0x0619 probe failed" in snapshot.last_failure
        finally:
            await plc.aclose()


# ========================================================================================
# The loop's own traffic
# ========================================================================================


def test_observe_ignores_the_probe_command() -> None:
    """The probe accounts for itself; counting it here too would be self-flattery."""
    clock = FakeClock()
    plc = a_client()
    monitor = HealthMonitor(plc, clock=clock)
    monitor.observe(a_tx(clock, command=PROBE_COMMAND))
    snapshot = monitor.snapshot()
    assert snapshot.observed == 0
    assert snapshot.last_activity_at is None


async def test_observe_counts_traffic_and_hysteresis_flips_after_enough_of_it() -> None:
    """A live connection, because ``healthy`` is the window AND the connection state."""
    async with PlcSimulator() as simulator:
        plc = client_for(simulator)
        await plc.connect()
        clock = FakeClock()
        changes: list[HealthSnapshot] = []
        monitor = HealthMonitor(
            plc, clock=clock, healthy_after=2, unhealthy_after=3, on_change=changes.append
        )
        try:
            assert verdict(monitor) is False  # nothing has proven it yet
            monitor.observe(a_tx(clock))
            assert verdict(monitor) is False  # one good read is not a verdict
            assert changes == []
            monitor.observe(a_tx(clock))
            assert verdict(monitor) is True
            assert len(changes) == 1
            assert changes[0].healthy is True
            assert changes[0].consecutive_successes == 2
        finally:
            await plc.aclose()


async def test_a_run_of_failures_flips_it_back_and_a_single_one_does_not() -> None:
    async with PlcSimulator() as simulator:
        plc = client_for(simulator)
        await plc.connect()
        clock = FakeClock()
        changes: list[HealthSnapshot] = []
        monitor = HealthMonitor(
            plc, clock=clock, healthy_after=1, unhealthy_after=3, on_change=changes.append
        )
        try:
            monitor.observe(a_tx(clock))
            assert verdict(monitor) is True
            monitor.observe(a_tx(clock, end_code=0xC059))
            monitor.observe(a_tx(clock, end_code=0xC059))
            assert verdict(monitor) is True  # two is not three
            monitor.observe(a_tx(clock, end_code=0xC059))
            assert verdict(monitor) is False
            assert [change.healthy for change in changes] == [True, False]
            snapshot = monitor.snapshot()
            assert snapshot.observed == 4
            assert snapshot.failures == 3
            assert snapshot.failure_ratio == pytest.approx(0.75)
            assert snapshot.last_failure is not None
            assert "0xC059" in snapshot.last_failure
        finally:
            await plc.aclose()


async def test_one_success_resets_the_failure_run() -> None:
    async with PlcSimulator() as simulator:
        plc = client_for(simulator)
        await plc.connect()
        clock = FakeClock()
        monitor = HealthMonitor(plc, clock=clock, healthy_after=1, unhealthy_after=2)
        try:
            monitor.observe(a_tx(clock))
            monitor.observe(a_tx(clock, end_code=0xC059))
            monitor.observe(a_tx(clock))
            monitor.observe(a_tx(clock, end_code=0xC059))
            assert verdict(monitor) is True
            assert monitor.snapshot().consecutive_failures == 1
        finally:
            await plc.aclose()


def test_the_window_is_bounded_and_the_ratio_is_over_that_window() -> None:
    clock = FakeClock()
    monitor = HealthMonitor(a_client(), clock=clock, window=4)
    for _ in range(6):
        monitor.observe(a_tx(clock))
    monitor.observe(a_tx(clock, end_code=0xC059))
    snapshot = monitor.snapshot()
    assert snapshot.observed == 4
    assert snapshot.capacity == 4
    assert snapshot.failures == 1
    assert snapshot.failure_ratio == pytest.approx(0.25)


def test_the_failure_ratio_of_an_empty_window_is_zero_over_zero_and_says_so() -> None:
    snapshot = HealthMonitor(a_client()).snapshot()
    assert snapshot.observed == 0
    assert snapshot.failure_ratio == 0.0


def test_a_good_window_cannot_outvote_an_unusable_connection() -> None:
    """The window describes a socket. ``NEW``, ``FAILED`` and ``CLOSED`` have none."""
    clock = FakeClock()
    plc = a_client()  # never connected
    monitor = HealthMonitor(plc, clock=clock, healthy_after=1)
    monitor.observe(a_tx(clock))
    assert monitor.snapshot().consecutive_successes == 1
    assert verdict(monitor) is False
    assert monitor.snapshot().healthy is False


def test_a_broken_on_change_is_reported_rather_than_swallowed() -> None:
    def explode(_snapshot: HealthSnapshot) -> None:
        raise RuntimeError("the alarm panel is down")

    clock = FakeClock()
    plc = a_client()
    monitor = HealthMonitor(plc, clock=clock, healthy_after=1, on_change=explode)
    with pytest.raises(SlmpSinkError, match="the alarm panel is down"):
        monitor.observe(a_tx(clock))
    assert plc.counters.sink_errors == 1


# ========================================================================================
# The schedule
# ========================================================================================


def test_a_fresh_transaction_pushes_the_next_probe_out_by_idle_probe_after() -> None:
    clock = FakeClock()
    monitor = HealthMonitor(a_client(), clock=clock, idle_probe_after=5.0)
    assert monitor.due_in() == 0.0  # nothing has ever happened: probe now
    monitor.observe(a_tx(clock))
    assert monitor.due_in() == pytest.approx(5.0, abs=0.01)
    clock.advance(3_000 * MS)
    assert monitor.due_in() == pytest.approx(2.0, abs=0.01)
    clock.advance(3_000 * MS)
    assert monitor.due_in() == 0.0


async def test_a_skipped_probe_still_counts_as_an_attempt_so_run_cannot_spin() -> None:
    """Without this the monitor would re-attempt in a tight loop on a busy connection."""
    async with PlcSimulator() as simulator:
        plc = client_for(simulator)
        await plc.connect()
        clock = FakeClock()
        monitor = HealthMonitor(plc, clock=clock, probe_interval=5.0)
        try:
            async with plc._conn.gate.lease(sequence=9_003):
                assert await monitor.probe_once() is ProbeOutcome.SKIPPED_BUSY
            assert monitor.due_in() == pytest.approx(5.0, abs=0.01)
        finally:
            await plc.aclose()


async def test_run_stops_when_the_client_is_closed() -> None:
    """``run()`` returns once the client reaches CLOSED -- bounded by the probe in flight.

    The two numbers here have to be ordered deliberately, and the original pair were
    equal, which made this a coin flip. Closing a socket does **not** reliably wake a task
    already awaiting a read on it: on some loops and platforms the pending
    ``sock_recv_into`` only unblocks when its own deadline expires. So if ``aclose()``
    lands while a probe is mid-flight, the monitor cannot return until that probe gives
    up, and the bound on this test is the CLIENT's timeout, not the close.

    With both set to 2.0 s the probe's deadline and the test's patience expired together
    and whichever won was down to scheduling -- green on windows-latest 3.13, red on
    ubuntu-latest and on 3.11, CI 2026-09-24. A short client timeout makes the ordering
    explicit and the test fast, instead of hiding the dependency behind a longer wait.
    """
    async with PlcSimulator() as simulator:
        host, port = simulator.address("tcp")
        plc = Plc(host, port, profile=FX5U, timeout=0.25, name="tcp")
        await plc.connect()
        monitor = HealthMonitor(plc, idle_probe_after=0.01, probe_interval=0.01)
        task = asyncio.create_task(monitor.run())
        while plc.counters.probes_sent < 2:
            await asyncio.sleep(0.005)
        await plc.aclose()
        assert plc.state is ConnectionState.CLOSED
        # Comfortably longer than the 0.25 s a probe can still be waiting out.
        await asyncio.wait_for(task, timeout=5.0)
        assert task.done()
        assert monitor.snapshot().probes_sent >= 2


async def test_run_sleeps_until_the_probe_is_due_rather_than_polling() -> None:
    clock = FakeClock()
    sleep = RecordingSleep(clock)
    plc = a_client()  # never connected: every probe stands down, cheaply
    monitor = HealthMonitor(
        plc, clock=clock, idle_probe_after=2.0, probe_interval=2.0, sleep=sleep
    )
    task = asyncio.create_task(monitor.run())
    for _ in range(1_000):
        if len(sleep.calls) >= 3:
            break
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sleep.calls == pytest.approx([2.0, 2.0, 2.0])


# ========================================================================================
# Construction
# ========================================================================================


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"idle_probe_after": 0.0}, "idle_probe_after must be positive"),
        ({"probe_interval": -1.0}, "probe_interval must be positive"),
        ({"window": 0}, "at least one observation"),
        ({"unhealthy_after": 0}, "at least 1"),
        ({"healthy_after": 0}, "at least 1"),
    ],
)
def test_an_incoherent_monitor_is_refused(kwargs: dict[str, object], match: str) -> None:
    with pytest.raises(SlmpConfigurationError, match=match):
        HealthMonitor(a_client(), **kwargs)  # type: ignore[arg-type]  # kwargs


def test_the_snapshot_carries_the_connection_it_describes() -> None:
    clock = FakeClock()
    plc = a_client()
    monitor = HealthMonitor(plc, clock=clock)
    snapshot = monitor.snapshot()
    assert snapshot.name == "bench"
    assert snapshot.generation == plc.generation
    assert snapshot.at == Nanos(clock.now)
    assert snapshot.idle_ns(now=Nanos(clock.now)) is None
    assert "UNHEALTHY" in str(snapshot)
    assert "probes 0 sent / 0 skipped" in str(snapshot)
