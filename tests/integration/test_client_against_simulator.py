"""The client against a PLC that behaves like the bench, defects included.

The five programs of DESIGN.md section 2, run end to end -- construction and lifecycle
(2.2, 2.3), reads and writes (2.6), the control-loop primitive (2.6), the three ways
timing reaches a caller (2.5), and remote control (2.8) -- followed by the failures that
are the reason this library exists.

Why this file is worth more than its runtime. ``aslmp.testing`` imports layers 0 to 2
only and is forbidden to import ``aslmp.transport``, ``aslmp.connection`` or
``aslmp.client`` (DESIGN.md section 4.11), so a bug in the transport, the in-flight gate,
the reconnection logic or the client's control flow *is* visible here rather than
cancelled out by both sides sharing it. The simulator is running
``FX5U_MEASURED``: request coalescing, one connection per entry, silence on a coding
mismatch, and a hang on an overstated length are all switched on, each citing the
measurement it reproduces on FX5U-32MT/DS fw 1.065.

Registers follow the bench's own map: ``D0`` IO_SP, ``D2`` IO_PV, ``D4`` IO_MV, ``D6``
IO_Err, ``D8`` IO_Scan, all f32 low word first, with ``D100``-``D119`` and
``M100``-``M119`` as scratch.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator, AsyncIterator, Callable

import pytest

from aslmp.client import Handshake, MonitoringTimer, Plc
from aslmp.commands.block import BlockSpec
from aslmp.commands.random import RandomWrite, bit_point, dword, word
from aslmp.errors import (
    SlmpCapabilityError,
    SlmpConcurrentTransactionError,
    SlmpConfigurationError,
    SlmpConnectionEntryBusyError,
    SlmpConnectionLostError,
    SlmpDeviceRangeError,
    SlmpEndCodeError,
    SlmpNotConnectedError,
    SlmpRemoteStateNotReachedError,
    SlmpSinkError,
    SlmpTimeoutError,
    TimeoutCause,
)
from aslmp.identity import CpuStatus
from aslmp.observability import Connected, ConnectionEvent, HandshakeFailed
from aslmp.profile import Encoding
from aslmp.results import RandomReading, Reading, WriteAck
from aslmp.testing.dispatch import CpuRunState
from aslmp.testing.pathology import HEALTHY
from aslmp.testing.server import Entry, PlcSimulator
from aslmp.testing.targets import FX5U_32MT_DS, PEDANTIC, SimulatorTarget
from aslmp.timing import Transaction
from aslmp.transport.base import TransportKind
from aslmp.transport.inflight import Concurrency
from aslmp.wire.frames import FrameType

pytestmark = pytest.mark.simulator

FX5U_KEY = "melsec:iq-f/fx5u"


@contextlib.asynccontextmanager
async def bench(
    target: SimulatorTarget = FX5U_32MT_DS, **kwargs: object
) -> AsyncIterator[PlcSimulator]:
    """A simulator shaped like our bench: five entries, the measured pathology board.

    Five, not the six the real bench has grown -- UDP 5005 was added on 2026-09-07
    for the wired retest and is peer-bound to a host the simulator has no notion of.
    """
    simulator = PlcSimulator(target=target, **kwargs)  # type: ignore[arg-type]  # kwargs
    await simulator.start()
    try:
        yield simulator
    finally:
        await simulator.aclose()


async def eventually(condition: Callable[[], bool], *, timeout: float = 2.0) -> None:
    """Wait for a *server-side* fact the client cannot observe directly.

    ``remote.reset()`` returns as soon as its request is written, because silence is the
    expected outcome; the simulator records having answered nothing a moment later. This
    polls rather than sleeping a fixed time, so it is neither flaky nor slow.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        if loop.time() > deadline:
            raise AssertionError("the condition never became true")
        await asyncio.sleep(0.005)


def client_for(simulator: PlcSimulator, entry: str = "tcp", **kwargs: object) -> Plc:
    host, port = simulator.address(entry)
    configured = simulator.entry(entry)
    kwargs.setdefault("encoding", configured.encoding)
    kwargs.setdefault("frame", configured.frame)
    kwargs.setdefault(
        "transport",
        TransportKind.TCP if configured.protocol == "tcp" else TransportKind.UDP,
    )
    kwargs.setdefault("timeout", 2.0)
    return Plc(host, port, profile=FX5U_KEY, **kwargs)  # type: ignore[arg-type]  # kwargs


# ========================================================================================
# Program 1 -- DESIGN.md sections 2.2 and 2.3: construct, connect, prove, close
# ========================================================================================


async def test_program_1_connect_proves_the_connection_and_names_the_cpu() -> None:
    """``socket.connect()`` lies on this hardware, so ``connect()`` sends a request.

    ``0x0619`` Self Test with a per-generation nonce, echo compared byte for byte, then
    ``0x0101`` Read Type Name whose model code must be one the declared profile claims.
    One measured ~7 ms zero-side-effect round trip proves the entry is free, the coding,
    the frame format, the route, the protocol and that the CPU is answering now.
    """
    async with bench() as simulator:
        plc = client_for(simulator)
        info = await plc.connect()
        try:
            assert plc.state.name == "READY"
            assert info.identity is not None
            assert info.identity.model == "FX5U-32MT/DS"
            assert info.identity.model_code == 0x4A49
            assert plc.identity is not None
            assert plc.identity.family.value == "iq-f"
            assert info.handshake is not None
            assert info.handshake.command == 0x0619
            assert info.handshake.timing.wire_ns > 0
            assert info.local[1] != info.peer[1]
        finally:
            await plc.aclose()
        assert plc.state.name == "CLOSED"


async def test_program_1_the_handshake_payload_is_hex_and_echoes_exactly() -> None:
    """Both manuals restrict loopback data to ``0-9`` and ``A-F``, our own defaults included.

    The nonce is what makes a stale answer visible: a coalesced or replayed response
    carries a different payload, and on 3E a differing echo is the only way to see it.
    """
    async with bench() as simulator:
        plc = client_for(simulator)
        await plc.connect()
        await plc.aclose()
        sent = [record for record in simulator.transcript if record.direction == "rx"]
        assert b"0619" in sent[0].data
        payload = sent[0].data[-8:]
        assert payload[:4] == b"0619"
        assert all(byte in b"0123456789ABCDEF" for byte in payload[4:])


async def test_program_1_a_profile_the_cpu_does_not_match_is_refused_at_connect() -> None:
    """Requiring ``profile=`` carries no silent-wrong-radix risk: the handshake checks it."""
    from aslmp.errors import SlmpProfileMismatchError

    async with bench() as simulator:
        host, port = simulator.address("tcp")
        plc = Plc(host, port, profile="melsec:iq-r", timeout=2.0)
        seen: list[ConnectionEvent] = []
        plc.add_event_listener(seen.append)
        with pytest.raises(SlmpProfileMismatchError, match="aslmp identify"):
            await plc.connect()
        await plc.aclose()
        assert any(isinstance(event, HandshakeFailed) for event in seen)
        assert plc.counters.handshake_failures == 1


async def test_program_1_handshake_none_trades_a_truthful_connect_for_7_ms() -> None:
    """It exists, it is not the default, and it says what it gave up."""
    async with bench() as simulator:
        plc = client_for(simulator, handshake=Handshake.NONE)
        info = await plc.connect()
        try:
            assert info.handshake is None
            assert info.identity is None
            assert plc.identity is None
            assert simulator.transcript == ()
        finally:
            await plc.aclose()


async def test_program_1_the_client_is_an_async_context_manager() -> None:
    async with bench() as simulator, client_for(simulator) as plc:
        assert plc.state.name == "READY"
        assert await plc.ping() >= 0.0


# ========================================================================================
# Program 2 -- DESIGN.md section 2.6: typed reads and writes
# ========================================================================================


async def test_program_2_scalars_round_trip_through_the_scratch_registers() -> None:
    """One named method per width, concrete return types, no magic dtype strings."""
    async with bench() as simulator, client_for(simulator) as plc:
        await plc.write_f32("D100", 1234.5, verify=True)
        await plc.write_i32("D102", -70000, verify=True)
        await plc.write_u16("D104", 0xBEEF, verify=True)
        await plc.write_i16("D105", -1, verify=True)
        await plc.write_f64("D106", 3.14159265358979, verify=True)
        await plc.write_bit("M100", True, verify=True)
        await plc.write_str("D110", "PART-42", length=8, verify=True)

        assert await plc.read_f32("D100") == 1234.5
        assert await plc.read_i32("D102") == -70000
        assert await plc.read_u16("D104") == 0xBEEF
        assert await plc.read_i16("D105") == -1
        assert await plc.read_f64("D106") == pytest.approx(3.14159265358979)
        assert await plc.read_bit("M100") is True
        assert await plc.read_str("D110", length=8) == "PART-42"


async def test_program_2_a_float_is_low_word_first_on_the_wire() -> None:
    """Measured: 1234.5 leaves ``D104 = 0x5000`` and ``D105 = 0x449A`` behind.

    The client never byte-swaps: ``struct.pack("<f", v)`` is already low word first, and
    the ASCII-versus-binary dword flip is a codec property that is never user-selectable.
    """
    async with bench() as simulator, client_for(simulator) as plc:
        await plc.write_f32("D104", 1234.5)
        assert await plc.read_words("D104", 2) == (0x5000, 0x449A)
        assert simulator.memory.get_f32("D", 104) == 1234.5


async def test_program_2_batch_arrays_move_whole_runs() -> None:
    async with bench() as simulator, client_for(simulator) as plc:
        await plc.write_words("D100", [1, 2, 3, 4, 5])
        assert await plc.read_words("D100", 5) == (1, 2, 3, 4, 5)

        await plc.write_bits("M100", [True, False, True, True])
        assert await plc.read_bits("M100", 4) == (True, False, True, True)

        await plc.write_f32_array("D110", [1.5, -2.5, 0.0])
        assert await plc.read_f32_array("D110", 3) == (1.5, -2.5, 0.0)


async def test_program_2_verify_costs_a_second_round_trip_and_says_so() -> None:
    """``verify=True`` reads the device back. It is a second transaction, not a flag.

    A caller who cannot afford it leaves it off; a caller who turns it on gets a
    ``SlmpVerificationError`` on a disagreement rather than a write reported as
    successful because the end code said ``0x0000``.
    """
    async with bench() as simulator, client_for(simulator) as plc:
        before = plc.counters.transactions_completed
        await plc.write_u16("D100", 5, verify=True)
        assert plc.counters.transactions_completed == before + 2

        before = plc.counters.transactions_completed
        await plc.write_u16("D100", 6)
        assert plc.counters.transactions_completed == before + 1
        assert simulator.memory.get_u16("D", 100) == 6


async def test_program_2_blocks_move_several_runs_in_one_frame() -> None:
    async with bench() as simulator, client_for(simulator) as plc:
        await plc.write_words("D100", [11, 22])
        await plc.write_words("D110", [33, 44, 55])
        blocks = await plc.read_blocks([BlockSpec("D100", 2), BlockSpec("D110", 3)])
        assert blocks == ((11, 22), (33, 44, 55))


async def test_program_2_a_read_off_the_end_of_the_device_is_refused_client_side() -> None:
    """``D`` ends at ``D7999`` on an FX5U; ``D8000`` was measured to return ``0xC056``.

    Range validation always takes the **span**, never the start, so ``D7999`` alone is
    legal and ``D7999`` for two words is not -- and nothing reaches the socket.
    """
    from aslmp.errors import SlmpAddressRangeError

    async with bench() as simulator, client_for(simulator) as plc:
        simulator.clear_transcript()
        with pytest.raises(SlmpAddressRangeError):
            await plc.read_words("D7999", 2)
        assert simulator.transcript == ()


async def test_program_2_the_plc_end_code_reaches_the_caller_as_a_named_exception() -> None:
    """With client-side range checking off, the CPU's own refusal is what raises.

    ``validate_ranges=False`` exists because on an iQ-R every range is repartitionable in
    GX Works3; it turns off the range **table**, not the raise.
    """
    async with bench() as simulator, client_for(
        simulator, validate_ranges=False, capture_frames=True
    ) as plc:
        with pytest.raises(SlmpDeviceRangeError) as caught:
            await plc.read_words("D7999", 4)
        rendered = str(caught.value)
        assert "0xC056" in rendered
        assert "read_words" in rendered or "D7999" in rendered
        assert plc.counters.end_code_errors == 1


# ========================================================================================
# Program 3 -- DESIGN.md section 2.6: the control-loop primitive
# ========================================================================================


async def test_program_3_one_random_read_is_one_snapshot_in_the_callers_order() -> None:
    """The wire groups words before double words; the caller's order comes back.

    Reading ``D0`` as one float, ``M100`` as one bit and ``D104`` as one unsigned double
    word in the same ``0403`` is ordinary, and a dict keyed by device string could not
    express it -- which is why the result is positional.

    ``D8`` is declared ``f32`` because on the bench it is: the counter is a ``REAL``
    (``IO_Scan := IO_Scan + 1.0``, FX5U-32MT/DS fw 1.065, 2026-09-07). This test used to
    seed it with ``set_u32`` and read it back as ``u32``, which passed for the reason
    every version of that misread passes -- both halves were wrong the same way. The
    ``u32`` point moved to ``D104``, which is scratch and carries no declared type, so
    the mixed-kind property this test is actually about survives.
    """
    async with bench() as simulator, client_for(simulator) as plc:
        simulator.memory.set_f32("D", 0, 60.0)
        simulator.memory.set_f32("D", 2, 59.25)
        simulator.memory.set_f32("D", 8, 4096.0)
        simulator.memory.set_u32("D", 104, 4_000_000_000)
        simulator.memory.write_bits("M", 100, [True, False, True])

        points = [
            dword("D0", kind="f32"),
            dword("D2", kind="f32"),
            bit_point("M100"),
            dword("D8", kind="f32"),
            dword("D104", kind="u32"),
        ]
        simulator.clear_transcript()
        reading = await plc.read_random(points)

        assert isinstance(reading, RandomReading)
        assert reading.f32(0) == 60.0
        assert reading.f32(1) == 59.25
        assert reading.bits(2)[:3] == (True, False, True)
        assert reading.f32(3) == 4096.0
        assert reading.u32(4) == 4_000_000_000
        assert reading.tx.command == 0x0403
        assert reading.tx.timing.is_complete
        assert len(simulator.transcript) == 2  # one request, one response. One snapshot.


async def test_program_3_scattered_writes_go_in_one_frame() -> None:
    async with bench() as simulator, client_for(simulator) as plc:
        await plc.write_random(
            [
                RandomWrite(word("D100"), 0x1111),
                RandomWrite(word("D101"), 0x2222),
                RandomWrite(dword("D104", kind="f32"), 1234.5),
            ]
        )
        assert simulator.memory.get_u16("D", 100) == 0x1111
        assert simulator.memory.get_u16("D", 101) == 0x2222
        assert simulator.memory.get_u16("D", 104) == 0x5000
        assert simulator.memory.get_u16("D", 105) == 0x449A


async def test_program_3_over_the_ceiling_refuses_rather_than_splitting_silently() -> None:
    """192 points on an FX5U's built-in port, measured. Splitting is opt-in and typed."""
    from aslmp.errors import SlmpPointLimitError

    async with bench() as simulator, client_for(simulator) as plc:
        points = [word(f"D{index}") for index in range(200)]
        simulator.clear_transcript()
        with pytest.raises(SlmpPointLimitError):
            await plc.read_random(points)
        assert simulator.transcript == ()


async def test_program_3_an_opted_in_split_returns_a_different_type() -> None:
    """``SplitReading`` is not a ``RandomReading``: the lost atomicity is in the type."""
    from aslmp.results import SplitReading

    async with bench() as simulator, client_for(simulator) as plc:
        points = [word(f"D{index}") for index in range(200)]
        result = await plc.read_random(points, allow_split=True)
        assert isinstance(result, SplitReading)
        assert len(result) == 200
        assert len(result.transactions) == 2
        assert result.snapshot_span_ns > 0


async def test_program_3_monitor_is_refused_before_the_wire_on_an_iq_f() -> None:
    """``0801``/``0802`` answer ``0xC059`` on this CPU, and are never emulated with a 0403.

    Substituting a different command that returns similar-looking data is exactly the
    silent recovery this library forbids, so the refusal is pre-transport and typed.
    """
    async with bench() as simulator, client_for(simulator) as plc:
        simulator.clear_transcript()
        with pytest.raises(SlmpCapabilityError, match="0x0801"):
            await plc.monitor_register([word("D0")])
        assert simulator.transcript == ()


async def test_program_3_monitor_works_where_the_cpu_has_it() -> None:
    """The same call against ``PEDANTIC``, so the refusal above is about the CPU."""
    async with bench(target=PEDANTIC, pathology=HEALTHY) as simulator:
        host, port = simulator.address("tcp")
        async with Plc(
            host, port, profile="melsec:iq-r", timeout=2.0,
            handshake=Handshake.SELF_TEST,
        ) as plc:
            simulator.memory.set_u16("D", 0, 7)
            registration = await plc.monitor_register([word("D0")])
            reading = await plc.monitor_read(registration)
            assert reading.u16(0) == 7


# ========================================================================================
# Program 4 -- DESIGN.md section 2.5: three ways to the timing, none of them a tax
# ========================================================================================


async def test_program_4_timing_reaches_the_caller_three_ways() -> None:
    """A bare value, a ``Reading`` on ``plc.timed``, and a sink that sees every record."""
    records: list[Transaction] = []
    async with bench() as simulator, client_for(
        simulator, on_transaction=records.append
    ) as plc:
        simulator.memory.set_f32("D", 2, 59.25)

        # 1. the common path pays nothing
        value = await plc.read_f32("D2")
        assert value == 59.25

        # 2. the same call, with its record
        reading = await plc.timed.read_f32("D2")
        assert isinstance(reading, Reading)
        assert reading.value == 59.25
        assert reading.tx.timing.wire_ms >= 0.0
        assert reading.tx.command == 0x0401

        # 3. every record, including the handshake's
        assert [record.command for record in records[:2]] == [0x0619, 0x0101]
        info = plc.info
        assert info is not None
        assert all(record.connection_id == info.connection_id for record in records)


async def test_program_4_a_write_on_the_timed_surface_says_how_much_moved() -> None:
    async with bench() as simulator, client_for(simulator) as plc:
        ack = await plc.timed.write_f32("D100", 7.5)
        assert isinstance(ack, WriteAck)
        assert ack.points == 2
        assert ack.tx.command == 0x1401
        assert await plc.read_f32("D100") == 7.5


async def test_program_4_the_metrics_snapshot_counts_what_happened() -> None:
    async with bench() as simulator, client_for(simulator) as plc:
        for _ in range(5):
            await plc.read_u16("D0")
        snapshot = plc.metrics()
        assert snapshot.counters.transactions_completed == 7  # 5 reads + 0619 + 0101
        assert snapshot.latency is not None
        assert snapshot.latency.count == 7
        assert snapshot.generation == 0
        assert simulator.target.model_name == "FX5U-32MT/DS"


async def test_program_4_a_transaction_record_names_the_socket_it_ran_on() -> None:
    """``generation`` is the anti-lie field: a rebuilt socket cannot look like a slow PLC.

    ``after_reconnect`` marks the **first** transaction of a generation, which after a
    reconnect is the handshake that re-proved it -- the one transaction whose latency a
    reader would otherwise mistake for the loop's own.
    """
    records: list[Transaction] = []
    async with bench() as simulator, client_for(
        simulator, on_transaction=records.append
    ) as plc:
        before = await plc.timed.read_u16("D0")
        assert before.tx.generation == 0
        assert before.tx.after_reconnect is False

        info = await plc.reconnect(reason="a test asked for it")
        assert info.generation == 1
        after = await plc.timed.read_u16("D0")
        assert after.tx.generation == 1
        assert after.tx.after_reconnect is False

        marked = [record for record in records if record.after_reconnect]
        assert [record.command for record in marked] == [0x0619]
        assert marked[0].generation == 1
        assert plc.counters.reconnects == 1
        assert simulator.target is FX5U_32MT_DS


async def test_program_4_captured_frames_are_the_bytes_that_actually_went_out() -> None:
    async with bench() as simulator, client_for(simulator, capture_frames=True) as plc:
        simulator.clear_transcript()
        reading = await plc.timed.read_u16("D0")
        assert reading.tx.request_frame is not None
        assert reading.tx.response_frame is not None
        rx = [record for record in simulator.transcript if record.direction == "rx"]
        assert rx[0].data == reading.tx.request_frame
        assert reading.tx.request_frame[:2] == b"\x50\x00"
        assert reading.tx.response_frame[:2] == b"\xd0\x00"


async def test_program_4_events_reach_a_listener_and_an_async_iterator() -> None:
    async with bench() as simulator:
        plc = client_for(simulator)
        seen: list[ConnectionEvent] = []
        plc.add_event_listener(seen.append)
        stream = aiter(plc.events())
        await plc.connect()
        first = await asyncio.wait_for(anext(stream), timeout=2.0)
        if isinstance(stream, AsyncGenerator):
            await stream.aclose()
        await plc.aclose()
        assert any(isinstance(event, Connected) for event in seen)
        assert first.connection_id == seen[0].connection_id
        assert first.kind == seen[0].kind == "Connecting"


# ========================================================================================
# Program 5 -- DESIGN.md section 2.8: remote control, interlocked and verified
# ========================================================================================


async def test_program_5_remote_control_needs_the_interlock_and_sends_nothing_without_it(
) -> None:
    """This library can stop a running machine over an unauthenticated cleartext socket."""
    async with bench() as simulator, client_for(simulator) as plc:
        simulator.clear_transcript()
        with pytest.raises(SlmpConfigurationError, match="allow_remote_control=True"):
            await plc.remote.stop()
        assert simulator.transcript == ()


async def test_program_5_a_remote_run_that_did_not_run_raises_rather_than_returning() -> None:
    """Mitsubishi documents Remote RUN completing normally with the switch in STOP.

    SH(NA)-080956ENG-M p.131: "the access destination does not become the RUN state".
    ``verify=True`` is the default precisely because a ``0x0000`` end code there is a
    successful answer to a request that did not happen, and reporting it as success would
    be the silent lie this library is written against.

    **The setup is a key switch, not a hand-written SD203.** This test used to poke
    ``SD203`` directly and then assert that the client noticed -- which it would have done
    against a simulator whose ``1001`` handler was ``return Reply(0x0000)``, because
    nothing connected the two. Turning the simulated key is the documented cause; the
    CPU derives ``SD203`` from it, and the client reads what a client can read.
    """
    async with bench() as simulator, client_for(
        simulator, allow_remote_control=True
    ) as plc:
        simulator.state.switch_position = CpuRunState.STOP
        with pytest.raises(SlmpRemoteStateNotReachedError) as caught:
            await plc.remote.run()
        assert caught.value.requested == "RUN"
        assert caught.value.actual == "STOP"
        assert plc.remote.last is not None
        assert plc.remote.last.verified is True
        assert plc.remote.last.status is CpuStatus.STOP
        assert simulator.cpu_state() == CpuRunState.STOP, "and the CPU really did not run"


async def test_program_5_the_same_remote_run_succeeds_with_the_key_turned() -> None:
    """The control for the test above: same client, same bytes, a CPU that can run.

    Without this, ``run()`` raising proves only that ``run()`` raises. The difference
    between the two tests is one field of simulated hardware, and the end code is
    ``0x0000`` in both.
    """
    async with bench() as simulator, client_for(
        simulator, allow_remote_control=True
    ) as plc:
        assert await plc.remote.stop() is CpuStatus.STOP
        assert await plc.remote.run() is CpuStatus.RUN
        assert simulator.cpu_state() == CpuRunState.RUN


async def test_program_5_a_lying_cpu_is_caught_by_the_verify_that_exists_for_it() -> None:
    """``remote_run_lies``: ``0x0000`` on a state that was never reached, no key involved.

    The pathology's stated purpose is to give ``verify=True`` something real to catch, and
    until 2026-09-07 it could not: with ``SD203`` derived from nothing, a lying CPU and an
    honest one returned identical bytes for identical requests.
    """
    async with bench(
        pathology=FX5U_32MT_DS.pathology.replace(remote_run_lies=True)
    ) as simulator, client_for(simulator, allow_remote_control=True) as plc:
        with pytest.raises(SlmpRemoteStateNotReachedError) as caught:
            await plc.remote.stop()
        assert (caught.value.requested, caught.value.actual) == ("STOP", "RUN")
        assert simulator.cpu_state() == CpuRunState.RUN


async def test_program_5_verify_false_is_a_documented_choice_and_says_so() -> None:
    """``verify=False`` returns the state it *asked for*, and the CPU is in another one.

    The setup is the documented key switch again, and the last assertion is the price of
    the flag: ``run(verify=False)`` reported RUN while ``SD203`` said STOP the whole time.
    """
    async with bench() as simulator, client_for(
        simulator, allow_remote_control=True
    ) as plc:
        simulator.state.switch_position = CpuRunState.STOP
        assert await plc.remote.run(verify=False) is CpuStatus.RUN
        assert plc.remote.last is not None
        assert plc.remote.last.verified is False
        assert "UNVERIFIED" in str(plc.remote.last)
        assert await plc.remote.status() is CpuStatus.STOP, (
            "the state verify=False did not look at"
        )


async def test_program_5_a_verified_stop_that_took_returns_the_status_it_read() -> None:
    """A CPU that was running, a Remote STOP, and a status the handler actually caused.

    No line of setup puts ``SD203`` where this test wants it: the CPU starts in RUN, and
    the only thing that moves it is the ``1002`` under test. That is the whole difference
    from the version of this test that stood here until 2026-09-07, which pre-set
    ``SD203`` to ``STOP`` and would have passed against a no-op handler.
    """
    async with bench() as simulator, client_for(
        simulator, allow_remote_control=True
    ) as plc:
        assert await plc.remote.status() is CpuStatus.RUN, "the CPU was running"
        assert await plc.remote.stop() is CpuStatus.STOP
        assert plc.remote.last is not None
        assert plc.remote.last.verify_tx is not None
        assert await plc.remote.status() is CpuStatus.STOP
        assert simulator.cpu_state() == CpuRunState.STOP


async def test_program_5_a_stopped_cpu_stops_scanning() -> None:
    """The other oracle ``tests/hardware/test_remote_control.py`` trusts, in simulation.

    That file verifies a remote STOP against the program's own free-running counter
    rather than against ``SD203``, "because the counter is something only the CPU can
    advance". The simulator could not reproduce either half: ``advance_scan()`` counted
    happily through a Remote STOP, so a client-side bug that mistook a stale D8 for a
    stopped CPU -- or a running one for a stopped one -- had nothing to fail against.
    """
    async with bench() as simulator, client_for(
        simulator, allow_remote_control=True
    ) as plc:
        simulator.dispatcher.advance_scan()
        running_at = simulator.memory.get_f32("D", 8)
        assert running_at == 1.0

        assert await plc.remote.stop() is CpuStatus.STOP
        for _ in range(5):
            simulator.dispatcher.advance_scan()
        assert simulator.memory.get_f32("D", 8) == running_at, "a stopped CPU does not scan"

        assert await plc.remote.run() is CpuStatus.RUN
        assert simulator.dispatcher.advance_scan() == running_at + 1.0


async def test_program_5_remote_reset_expects_silence_and_takes_the_connection_with_it(
) -> None:
    """SH(NA)-080956ENG-M p.136: on success the response is not sent back at all.

    The one command in the package for which silence is not a failure, and the only user
    of ``exchange_without_response`` -- which still burns the capability token, so it is
    not a general ``send``.
    """
    async with bench() as simulator, client_for(
        simulator, allow_remote_control=True
    ) as plc:
        outcome = await plc.remote.reset()
        assert outcome.responded is False
        assert outcome.connection_closed is True
        assert plc.state.name == "CLOSED"
        await eventually(lambda: bool(simulator.events_of("silence")))


# ========================================================================================
# The failures this library exists for
# ========================================================================================


async def test_two_requests_in_flight_at_once_is_inexpressible() -> None:
    """Coalescing is on. It cannot be reached, because there is no public ``send``.

    On FX5U-32MT/DS fw 1.065 two requests written before the first response is read
    return ONE response, for the LAST request, with end code ``0x0000`` -- undetectable
    wrong data reported as success on a 3E frame. The gate refuses the second caller by
    name instead.
    """
    async with bench() as simulator, client_for(simulator) as plc:
        simulator.memory.set_u16("D", 100, 111)
        simulator.memory.set_u16("D", 110, 222)
        first, second = await asyncio.gather(
            plc.read_u16("D100"),
            plc.read_u16("D110"),
            return_exceptions=True,
        )
        outcomes = [first, second]
        refused = SlmpConcurrentTransactionError
        refusals = [item for item in outcomes if isinstance(item, refused)]
        values = [item for item in outcomes if isinstance(item, int)]
        assert len(refusals) == 1
        assert len(values) == 1
        assert values[0] in (111, 222)
        assert plc.counters.concurrent_rejections == 1


async def test_serialised_callers_wait_and_the_wait_is_reported_as_queue_time() -> None:
    """A queue that does not report its own delay makes every latency number a lie."""
    records: list[Transaction] = []
    async with bench() as simulator, client_for(
        simulator, concurrency=Concurrency.SERIALIZE, on_transaction=records.append
    ) as plc:
        simulator.memory.set_u16("D", 100, 111)
        simulator.memory.set_u16("D", 110, 222)
        values = await asyncio.gather(plc.read_u16("D100"), plc.read_u16("D110"))
        assert sorted(values) == [111, 222]
        assert plc.counters.queue_waits >= 1
        assert max(record.timing.queue_ns for record in records) > 0


async def test_a_second_connection_to_a_one_entry_configuration_is_named_not_guessed(
) -> None:
    """The CPU accepts the TCP handshake and then FINs. ``socket.connect()`` succeeds anyway.

    Pooling against one configured entry cannot work, so this is its own error class
    rather than a timeout three seconds later.
    """
    async with bench() as simulator, client_for(simulator) as first:
        second = client_for(simulator)
        with pytest.raises(SlmpConnectionEntryBusyError, match="entry"):
            await second.connect()
        await second.aclose()
        assert await first.read_u16("D0") == 0  # the incumbent is undisturbed
        assert second.counters.entry_busy == 1


async def test_the_wrong_coding_fails_by_silence_and_the_timeout_names_it_first() -> None:
    """A coding mismatch is answered with nothing at all on this hardware.

    Zero bytes on a generation that has never completed a transaction ranks
    ``CODING_MISMATCH`` first, because that is the one cause a constructor argument fixes
    and it is invisible on the wire.
    """
    async with bench() as simulator:
        host, port = simulator.address("tcp")  # a BINARY entry
        plc = Plc(
            host,
            port,
            profile=FX5U_KEY,
            encoding=Encoding.ASCII_XY_HEX,
            timeout=0.5,
            connect_timeout=0.5,
        )
        with pytest.raises(SlmpTimeoutError) as caught:
            await plc.connect()
        await plc.aclose()
        assert caught.value.likely_causes[0] is TimeoutCause.CODING_MISMATCH
        assert "read_type_name" not in str(caught.value)
        assert plc.counters.timeouts == 1


async def test_a_failed_transaction_is_sticky_and_never_reconnects_itself() -> None:
    """``FAILED`` is sticky, the socket is closed, and reconnection is always explicit."""
    async with bench() as simulator:
        host, port = simulator.address("tcp")
        plc = Plc(host, port, profile=FX5U_KEY, timeout=0.5, connect_timeout=2.0)
        await plc.connect()
        await simulator.aclose()  # the CPU goes away mid-loop
        with pytest.raises((SlmpTimeoutError, SlmpConnectionLostError)):
            await plc.read_u16("D0")
        assert plc.state.name == "FAILED"
        with pytest.raises(SlmpNotConnectedError, match="sticky"):
            await plc.read_u16("D0")
        await plc.aclose()


async def test_a_segmented_response_is_stamped_after_its_last_chunk() -> None:
    """One 1931-byte read split at the 1460-byte MSS on 1 of 3 identical trials, 3.0 ms
    apart. Stamping the first chunk reports 11.0 ms for a 14.0 ms transaction.

    **What this asserts and what it cannot.** Whether a client sees one chunk or two is
    decided by the local TCP stack, not by this library and not by the simulator that
    writes them: CI 2026-09-24 had Python 3.11 on windows-latest reassembling a
    deliberately split 1931-byte response before ``sock_recv_into`` returned, while 3.13
    on the same runner saw two chunks. Demanding the split made this test assert an
    operating-system behaviour, and it failed for a reason that had nothing to do with
    the code under test.

    So the contract is asserted unconditionally -- the receive stamp is the LAST chunk's,
    whichever way it arrived -- and the multi-chunk specifics only when the split actually
    happened. ``test_a_segmented_response_is_one_message`` in ``tests/unit/test_connection``
    is the deterministic pin: it feeds the two chunks itself, so the stamping rule is held
    on every platform regardless of what the stack does here.
    """
    entries = (Entry(name="tcp", protocol="tcp"),)
    board = FX5U_32MT_DS.pathology.replace(segment_at=1460, segment_gap_s=0.02)
    async with bench(entries=entries, pathology=board) as simulator, client_for(
        simulator
    ) as plc:
        reading = await plc.timed.read_words("D0", 960)
        assert len(reading.value) == 960
        timing = reading.tx.timing

        # The contract, true either way.
        assert timing.received_at == timing.chunks[-1].at
        assert timing.wire_ns > 0
        # Self-consistency: the flag and the chunk list must agree.
        assert timing.segmented == any(chunk.partial for chunk in timing.chunks)

        if timing.segmented:
            assert len(timing.chunks) > 1
            assert timing.wire_ns > timing.first_byte_ns
            assert plc.counters.segmented_responses >= 1


async def test_an_injected_latency_lands_in_wire_ns_and_nowhere_else() -> None:
    """DESIGN.md section 5.9: the injected delay is what ``wire_ns`` reports.

    ``wire_ns`` is sent to last chunk -- the number a control loop budgets against. With
    one transaction in flight and nothing to queue behind, ``queue_ns`` is the host
    contention that did not happen, and the injected latency must not appear there.
    """
    board = FX5U_32MT_DS.pathology.replace(late_reply_s=0.05)
    async with bench(pathology=board) as simulator, client_for(simulator) as plc:
        reading = await plc.timed.read_u16("D0")
        timing = reading.tx.timing
        assert timing.wire_ns >= 50_000_000
        assert timing.queue_ns < 50_000_000
        assert timing.total_ns >= timing.wire_ns
        assert plc.metrics().latency is not None


async def test_a_failed_transaction_reaches_the_sink_too() -> None:
    """A histogram that only sees successes is a histogram that flatters the connection.

    The record for a transaction that timed out has no ``wire_ns`` -- it never received
    anything -- and :class:`~aslmp.timing.TransactionTiming` raises rather than inventing
    one, which is why ``LatencyRecorder`` counts it as incomplete instead of sampling it.
    """
    records: list[Transaction] = []
    async with bench() as simulator:
        host, port = simulator.address("tcp")
        plc = Plc(
            host,
            port,
            profile=FX5U_KEY,
            encoding=Encoding.ASCII_XY_HEX,
            timeout=0.4,
            connect_timeout=0.4,
            on_transaction=records.append,
        )
        with pytest.raises(SlmpTimeoutError):
            await plc.connect()
        await plc.aclose()
    assert len(records) == 1
    assert records[0].command == 0x0619
    assert not records[0].timing.is_complete
    assert plc.metrics().incomplete == 1
    assert plc.metrics().latency is None


async def test_a_broken_sink_is_counted_and_reported_once_per_generation() -> None:
    """One broken callback must not take a plant off the network, or be swallowed."""
    calls = 0

    def explode(tx: Transaction) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("the sink is broken")

    async with bench() as simulator:
        plc = client_for(simulator, on_transaction=explode)
        with pytest.raises(SlmpSinkError, match=r"counters\.sink_errors"):
            await plc.connect()
        assert plc.counters.sink_errors == 1
        assert await plc.read_u16("D0") == 0  # the connection keeps working
        assert plc.counters.sink_errors > 1
        assert calls > 1
        await plc.aclose()


# ========================================================================================
# The other configured entries
# ========================================================================================


async def test_the_same_client_works_over_udp() -> None:
    """TCP and UDP are one configured entry each, and neither is a fallback for the other.

    UDP has no one-connection limit, but a 3E datagram carries no serial No., so the
    transport refuses an in-flight depth above 1 at construction. This asserts the
    ordinary path works, not that it pipelines.
    """
    async with bench() as simulator, client_for(simulator, entry="udp") as plc:
        simulator.memory.set_f32("D", 0, 60.0)
        assert plc.transport is TransportKind.UDP
        assert await plc.read_f32("D0") == 60.0
        info = plc.info
        assert info is not None
        assert info.identity is not None


async def test_a_4e_frame_carries_a_serial_the_responder_echoes() -> None:
    """The only in-band defence against the measured coalescing corruption."""
    async with bench() as simulator, client_for(simulator, entry="tcp-4e") as plc:
        reading = await plc.timed.read_u16("D0")
        assert plc.frame is FrameType.FOUR_E
        assert reading.tx.serial is not None
        assert reading.tx.serial > 0


async def test_the_ascii_entry_speaks_the_same_library() -> None:
    """One codec choice, one set of lengths. The odd-bit-count rule is per codec."""
    async with bench() as simulator, client_for(simulator, entry="tcp-ascii") as plc:
        await plc.write_f32("D100", 1234.5)
        assert await plc.read_f32("D100") == 1234.5
        assert await plc.read_bits("M100", 3) == (False, False, False)
        assert plc.encoding is Encoding.ASCII_XY_HEX


# ========================================================================================
# The escape hatch
# ========================================================================================


async def test_the_raw_hatch_bypasses_validation_and_nothing_else() -> None:
    """It exists because this CPU accepted requests its own manual forbids.

    The frame, the ``L`` guard, the gate, the end-code raise and the record all still
    apply -- those are the parts that make a wrong answer visible.
    """
    async with bench() as simulator, client_for(simulator) as plc:
        simulator.memory.set_u16("D", 0, 4242)
        payload = b"\x00\x00\x00\xa8\x01\x00"  # D0, one word
        response = await plc.raw_command(0x0401, 0x0000, payload, mutates=False)
        assert response.end_code == 0
        assert response.payload == b"\x92\x10"
        assert plc.counters.transactions_completed == 3


async def test_the_raw_hatch_still_raises_on_an_end_code() -> None:
    async with bench() as simulator, client_for(simulator) as plc:
        payload = b"\x00\x00\x00\xa8\x00\x00"  # D0, zero points
        with pytest.raises(SlmpEndCodeError) as caught:
            await plc.raw_command(0x0401, 0x0000, payload, mutates=False)
        assert caught.value.end_code == 0xC052


async def test_the_monitoring_timer_reaches_the_wire_where_the_caller_set_one() -> None:
    """250 ms units, and ``0x0000`` means wait indefinitely -- not zero milliseconds."""
    async with bench() as simulator, client_for(
        simulator, monitoring_timer=MonitoringTimer.seconds(1.0), capture_frames=True
    ) as plc:
        reading = await plc.timed.read_u16("D0")
        frame = reading.tx.request_frame
        assert frame is not None
        assert frame[9:11] == b"\x04\x00"  # 4 x 250 ms, little endian
