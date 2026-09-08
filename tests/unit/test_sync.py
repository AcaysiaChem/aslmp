"""The sync facade: **one** background loop, for the facade's lifetime, on one thread.

The failure this file exists to prevent is ``asyncio.run`` per call. A new loop per call
is a new socket per call, and an SLMP connection entry on FX5U-32MT/DS fw 1.065 serves
**one** connection: a second one completes its TCP handshake in 5.4 ms and is then FINed
by the CPU (2026-09-06). So a per-call loop would either reconnect on every read -- paying
a ~7 ms handshake and hiding it inside the read's own latency -- or lose to its own
previous call.

That is asserted three ways here, because it is the point of the module: the source
contains no ``asyncio.run`` at all; every transaction sink callback fires on the *same*
thread; and after a hundred reads ``counters.connects`` is still 1.

The simulator runs on its **own** event loop thread. It has to: these tests are
synchronous, and a synchronous call blocks the calling thread until the background loop
answers -- if the server needed the calling thread too, the test would deadlock, which is
precisely the deadlock the facade refuses to create when it is built inside a running
loop.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import threading
from pathlib import Path
from types import TracebackType
from typing import Self

import pytest

from aslmp import sync
from aslmp.blocks.fields import U16, PlcBlock
from aslmp.blocks.layout import plc_block
from aslmp.client import Plc as AsyncPlc
from aslmp.errors import SlmpConfigurationError, SlmpNotConnectedError, SlmpSinkError
from aslmp.sync import Plc
from aslmp.testing.server import Entry, PlcSimulator
from aslmp.timing import Transaction

pytestmark = pytest.mark.simulator

REPO_ROOT = Path(__file__).resolve().parents[2]
CLIENT = REPO_ROOT / "src" / "aslmp" / "client.py"
SYNC = REPO_ROOT / "src" / "aslmp" / "sync.py"
FX5U = "melsec:iq-f/fx5u"


class SimulatorThread:
    """A :class:`PlcSimulator` on its own loop and thread, so a sync test can talk to it."""

    def __init__(self) -> None:
        self.simulator = PlcSimulator(entries=(Entry(name="tcp", protocol="tcp"),))
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name="sim-loop", daemon=True
        )

    def __enter__(self) -> Self:
        self._thread.start()
        asyncio.run_coroutine_threadsafe(self.simulator.start(), self._loop).result(5.0)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        asyncio.run_coroutine_threadsafe(self.simulator.aclose(), self._loop).result(5.0)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(5.0)
        self._loop.close()

    @property
    def address(self) -> tuple[str, int]:
        return self.simulator.address("tcp")


def aslmp_threads() -> list[str]:
    return [t.name for t in threading.enumerate() if t.name.startswith("aslmp-")]


# ========================================================================================
# Never asyncio.run per call
# ========================================================================================


def test_the_module_never_calls_asyncio_run() -> None:
    """The single strongest assertion available without a socket: it is not in there."""
    tree = ast.parse(SYNC.read_text(encoding="utf-8"))
    calls = {
        f"{node.func.value.id}.{node.func.attr}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
    }
    assert "asyncio.run" not in calls
    assert "asyncio.run_coroutine_threadsafe" in calls


def test_every_call_runs_on_the_same_loop_and_the_same_socket() -> None:
    threads: list[int] = []

    def sink(tx: Transaction, /) -> None:
        del tx
        threads.append(threading.get_ident())

    with SimulatorThread() as bench:
        host, port = bench.address
        with Plc(host, port, profile=FX5U, on_transaction=sink, name="loop") as plc:
            connection = plc.info
            assert connection is not None
            for _ in range(50):
                plc.read_f32("D0")
            assert plc.counters.connects == 1  # one socket, not fifty
            assert plc.generation == 0
            assert plc.info is not None
            assert plc.info.connection_id == connection.connection_id
    assert len(set(threads)) == 1  # every callback on the one background thread
    assert threads[0] != threading.get_ident()


def test_the_facade_owns_exactly_one_thread_named_after_it() -> None:
    with SimulatorThread() as bench:
        host, port = bench.address
        assert aslmp_threads() == []
        plc = Plc(host, port, profile=FX5U, name="cell-4")
        try:
            assert plc.thread_name == "aslmp-cell-4"
            assert aslmp_threads() == ["aslmp-cell-4"]
            plc.connect()
            plc.read_f32("D0")
            assert aslmp_threads() == ["aslmp-cell-4"]  # still one, after real work
        finally:
            plc.close()
    assert aslmp_threads() == []


def test_the_default_thread_name_is_the_peer() -> None:
    plc = Plc("192.168.10.250", 5002, profile=FX5U)
    try:
        assert plc.thread_name == "aslmp-192.168.10.250:5002"
        assert plc.name == "192.168.10.250:5002"
    finally:
        plc.close()


# ========================================================================================
# Constructed inside a running loop
# ========================================================================================


async def test_constructing_one_inside_a_running_loop_is_refused() -> None:
    """You are already in async code; ``aslmp.client.Plc`` is the same API with await."""
    with pytest.raises(SlmpConfigurationError) as caught:
        Plc("192.168.10.250", 5002, profile=FX5U)
    message = str(caught.value)
    assert "inside a running event loop" in message
    assert "aslmp.client.Plc" in message
    assert aslmp_threads() == []  # it refused before it started anything


def test_calling_back_into_the_client_from_the_loop_thread_is_refused() -> None:
    """A callback runs on the background loop; blocking it on that loop deadlocks it."""
    holder: list[Plc] = []

    def sink(tx: Transaction, /) -> None:
        del tx
        holder[0].read_i16("D0")

    with SimulatorThread() as bench:
        host, port = bench.address
        plc = Plc(host, port, profile=FX5U, on_transaction=sink, name="reentrant")
        holder.append(plc)
        try:
            with pytest.raises(SlmpSinkError, match="deadlocks"):
                plc.connect()
        finally:
            plc.close()


# ========================================================================================
# Lifecycle
# ========================================================================================


def test_the_facade_reads_and_writes_like_the_async_client() -> None:
    with SimulatorThread() as bench:
        host, port = bench.address
        with Plc(host, port, profile=FX5U) as plc:
            assert plc.state.name == "READY"
            assert plc.model == "FX5U-32MT/DS"
            assert plc.model_code == 0x4A49
            plc.write_f32("D100", 1234.5)
            assert plc.read_f32("D100") == pytest.approx(1234.5)
            plc.write_bit("M100", True)
            assert plc.read_bit("M100") is True
            assert plc.read_words("D100", 2) == (0x5000, 0x449A)
            assert plc.self_test() == b"ABCD"
            assert plc.metrics().counters.transactions_completed >= 5


def test_the_loop_does_not_outlive_the_facade() -> None:
    with SimulatorThread() as bench:
        host, port = bench.address
        plc = Plc(host, port, profile=FX5U, name="gone")
        plc.connect()
        plc.close()
        assert aslmp_threads() == []
        with pytest.raises(SlmpNotConnectedError, match="has been shut down"):
            plc.read_f32("D0")


def test_close_is_idempotent() -> None:
    with SimulatorThread() as bench:
        host, port = bench.address
        plc = Plc(host, port, profile=FX5U)
        plc.connect()
        plc.close()
        plc.close()
        assert aslmp_threads() == []


def test_module_shutdown_stops_every_loop_this_module_still_owns() -> None:
    first = Plc("192.168.10.250", 5002, profile=FX5U, name="a")
    second = Plc("192.168.10.250", 5003, profile=FX5U, name="b")
    assert sorted(aslmp_threads()) == ["aslmp-a", "aslmp-b"]
    sync.shutdown()
    assert aslmp_threads() == []
    assert first.thread_name == "aslmp-a"  # the object is still inspectable
    assert second.thread_name == "aslmp-b"


def test_a_refusal_before_the_wire_still_reaches_the_caller_unchanged() -> None:
    """The facade is a transport for coroutines, not a filter on their exceptions."""
    from aslmp.errors import SlmpDeviceRadixError

    plc = Plc("192.168.10.250", 5002, profile=FX5U)
    try:
        with pytest.raises(SlmpDeviceRadixError):
            plc.read_bit("X8")  # octal on an iQ-F: 8 is not a digit
    finally:
        plc.close()


# ========================================================================================
# Parity with the async surface (DESIGN.md section 4.8)
# ========================================================================================


def mirrored_names() -> list[str]:
    """Every ``@mirrored`` method of ``class Plc`` in ``client.py``."""
    tree = ast.parse(CLIENT.read_text(encoding="utf-8"))
    plc = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "Plc"
    )
    names: list[str] = []
    for item in plc.body:
        if not isinstance(item, ast.AsyncFunctionDef):
            continue
        decorators = {
            decorator.id
            for decorator in item.decorator_list
            if isinstance(decorator, ast.Name)
        }
        if "mirrored" in decorators:
            names.append(item.name)
    return names


def test_there_is_something_to_compare() -> None:
    assert len(mirrored_names()) > 30


@pytest.mark.parametrize("name", mirrored_names())
def test_the_sync_surface_has_the_same_name_and_parameters(name: str) -> None:
    """Identical names and identical parameter lists, minus ``async`` (section 4.8)."""
    assert hasattr(Plc, name), f"aslmp.sync.Plc is missing {name}"
    synchronous = getattr(Plc, name)
    asynchronous = getattr(AsyncPlc, name)
    assert not inspect.iscoroutinefunction(synchronous)
    assert inspect.iscoroutinefunction(asynchronous)
    assert [
        str(parameter) for parameter in inspect.signature(synchronous).parameters.values()
    ] == [
        str(parameter)
        for parameter in inspect.signature(asynchronous).parameters.values()
    ]


def test_the_constructor_takes_the_async_clients_parameters_unchanged() -> None:
    assert [
        str(parameter)
        for parameter in inspect.signature(Plc.__init__).parameters.values()
    ] == [
        str(parameter)
        for parameter in inspect.signature(AsyncPlc.__init__).parameters.values()
    ]


def test_the_surfaces_this_facade_deliberately_omits_are_documented() -> None:
    """``remote``, ``timed`` and ``events`` hand back async objects; wrapping them would
    be three more facades, and remote control is where a caller should be reading the
    async API and its interlocks directly."""
    for missing in ("remote", "timed", "events"):
        assert not hasattr(Plc, missing)
    documentation = (sync.__doc__ or "").lower()
    assert "deliberately missing" in documentation
    for missing in ("remote", "timed", "events"):
        assert missing in documentation


# --------------------------------------------------------------------------------------
# Blocks: the surface the facade did not have
# --------------------------------------------------------------------------------------


def test_the_facade_can_bind_read_and_write_a_block() -> None:
    """``bind``/``read_block``/``write_block`` were missing entirely from this facade.

    ``read_blocks``/``write_blocks`` -- a different command, ``0406``/``1406`` -- were
    here, so the omission read as deliberate rather than as an oversight, and the only
    way to read a ``@plc_block`` from synchronous code was to reach past the facade for
    ``plc.asynchronous``.
    """

    @plc_block(base="D100")
    class Pair(PlcBlock):
        low: U16
        high: U16

    with SimulatorThread() as bench:
        host, port = bench.address
        with Plc(host, port, profile=FX5U) as plc:
            plan = plc.bind(Pair)
            assert plan.plc is plc.asynchronous
            plc.write_block(plan, Pair(low=11, high=22))
            state = plc.read_block(plan)
            assert (state.low, state.high) == (11, 22)
            assert state.tx is not None


def test_the_facade_refuses_a_block_plan_bound_to_another_client() -> None:
    """Same guard as the async client's, reached through the facade."""

    @plc_block(base="D100")
    class Pair(PlcBlock):
        low: U16
        high: U16

    with SimulatorThread() as bench:
        host, port = bench.address
        with Plc(host, port, profile=FX5U) as plc:
            stranger = AsyncPlc("10.255.255.1", 5099, profile=FX5U)
            foreign = stranger.bind(Pair)
            with pytest.raises(SlmpConfigurationError, match="bound to"):
                plc.read_block(foreign)
            with pytest.raises(SlmpConfigurationError, match="bound to"):
                plc.write_block(foreign, Pair(low=1, high=2))


def test_binding_through_the_facade_does_no_io_and_needs_no_connection() -> None:
    """``bind()`` is synchronous on both surfaces, so it is not submitted to the loop."""

    @plc_block(base="D100")
    class Pair(PlcBlock):
        low: U16
        high: U16

    plc = Plc("192.168.10.250", 5002, profile=FX5U)
    try:
        assert plc.bind(Pair).points == 2
    finally:
        plc.close()
