"""Blocks against a PLC that behaves like the bench, defects included.

``aslmp.testing`` imports layers 0 to 2 only and is forbidden to import
``aslmp.transport``, ``aslmp.connection`` or ``aslmp.client`` (DESIGN.md section 4.11), so
the transport, the in-flight gate and the client's control flow are all *under test* here
rather than cancelled out by both sides sharing them. What that buys this file in
particular: a bound plan sends bytes nothing else in the library sends -- a frame built at
bind, kept for the life of the process, with only the 4E serial patched into it -- and the
simulator is an independent reader of those bytes.

Registers follow the bench's own map: ``D0`` IO_SP, ``D2`` IO_PV, ``D4`` IO_MV, ``D6``
IO_Err, ``D8`` IO_Scan, all f32 low word first, with ``D100``-``D119`` and ``M100``-``M119``
as scratch.
"""

from __future__ import annotations

import contextlib
import struct
from collections.abc import AsyncIterator
from typing import Any

import pytest

from aslmp.blocks.fields import F32, F64, I16, U16, U32, Bit, PlcBlock, Str, at
from aslmp.blocks.layout import plc_block
from aslmp.blocks.plan import BlockPlan, Split, SplitBlockPlan, bind
from aslmp.client import Plc, PlcClockSource
from aslmp.errors import SlmpDeviceRangeError, SlmpTargetChangedError
from aslmp.identity import CpuIdentity
from aslmp.profile import Family
from aslmp.testing.server import PlcSimulator
from aslmp.testing.targets import FX5U_32MT_DS
from aslmp.timing import Transaction
from aslmp.transport.base import TransportKind

pytestmark = pytest.mark.simulator

FX5U_KEY = "melsec:iq-f/fx5u"


@plc_block(base="D0")
class LoopState(PlcBlock):
    """The bench's own map, read in one 0403: five values, one bit, one scattered word."""

    setpoint: F32
    process_value: F32
    output: F32
    error: F32
    scan: U32
    fault: Bit = at("M100")
    alarm: Bit = at("M107")
    mode: U16 = at("D110")


@plc_block(base="D100")
class Scratch:
    """Registers only, so it has one prebuilt 1402 template and can be written whole."""

    value: F32
    count: U32
    trim: I16
    name: str = Str(length=6, address="D112")


@contextlib.asynccontextmanager
async def bench() -> AsyncIterator[PlcSimulator]:
    """A simulator shaped like our bench: five entries, the measured pathology board.

    Five, not the six the real bench has grown -- UDP 5005 was added on 2026-09-07
    for the wired retest and is peer-bound to a host the simulator has no notion of.
    """
    simulator = PlcSimulator(target=FX5U_32MT_DS)
    await simulator.start()
    try:
        yield simulator
    finally:
        await simulator.aclose()


def client_for(simulator: PlcSimulator, entry: str = "tcp", **kwargs: Any) -> Plc:
    host, port = simulator.address(entry)
    configured = simulator.entry(entry)
    kwargs.setdefault("encoding", configured.encoding)
    kwargs.setdefault("frame", configured.frame)
    kwargs.setdefault(
        "transport",
        TransportKind.TCP if configured.protocol == "tcp" else TransportKind.UDP,
    )
    kwargs.setdefault("timeout", 2.0)
    return Plc(host, port, profile=FX5U_KEY, **kwargs)


def load_bench_values(simulator: PlcSimulator) -> None:
    """The plant, as the loop would find it."""
    memory = simulator.memory
    memory.set_f32("D", 0, 55.0)
    memory.set_f32("D", 2, 54.25)
    memory.set_f32("D", 4, 31.5)
    memory.set_f32("D", 6, -0.75)
    memory.set_u32("D", 8, 1_234_567)
    memory.write_bits("M", 100, [True])
    memory.write_bits("M", 107, [True])
    memory.set_u16("D", 110, 3)


# ========================================================================================
# One block, one transaction
# ========================================================================================


async def test_a_block_reads_the_whole_map_in_one_transaction() -> None:
    """The feature, end to end: declare once, read in one 0403, get a typed object."""
    async with bench() as simulator, client_for(simulator) as plc:
        load_bench_values(simulator)
        plan = bind(plc, LoopState)
        simulator.clear_transcript()
        state = await plan.read()

    assert state.setpoint == 55.0
    assert state.process_value == 54.25
    assert state.output == 31.5
    assert state.error == -0.75
    assert state.scan == 1_234_567
    assert state.fault is True
    assert state.alarm is True
    assert state.mode == 3
    # One request and one response: eight fields, seven registers and two bits, once.
    assert len(simulator.transcript) == 2


async def test_the_transaction_record_says_the_frame_was_prebuilt() -> None:
    """``prebuilt`` exists so a bound plan's ``encode_ns`` is never compared to a built one."""
    seen: list[Transaction] = []
    async with bench() as simulator:
        client = client_for(simulator, on_transaction=seen.append)
        async with client as plc:
            load_bench_values(simulator)
            plan = bind(plc, LoopState)
            state = await plan.read()

    assert state.tx is not None
    tx = state.tx
    assert tx.prebuilt is True
    assert tx.command == 0x0403
    assert tx.subcommand == 0x0000
    assert tx.end_code == 0
    assert tx.request_bytes == len(plan.request_frame)
    assert tx.timing.wire_ms > 0
    assert seen[-1] is tx  # a block read lands in on_transaction like everything else


async def test_a_block_read_is_counted_like_every_other_transaction() -> None:
    async with bench() as simulator, client_for(simulator) as plc:
        load_bench_values(simulator)
        plan = bind(plc, LoopState)
        before = plc.counters.transactions_completed
        for _ in range(3):
            await plan.read()
        assert plc.counters.transactions_completed == before + 3
        latency = plc.metrics().latency
        assert latency is not None
        assert latency.count >= 3


async def test_the_bytes_on_the_wire_are_the_frame_bind_prebuilt() -> None:
    """Byte for byte, every cycle, with only the 4E serial ever patched."""
    async with bench() as simulator, client_for(simulator) as plc:
        load_bench_values(simulator)
        plan = bind(plc, LoopState)
        simulator.clear_transcript()
        await plan.read()
        await plan.read()

    sent = [record for record in simulator.transcript if record.direction == "rx"]
    assert [record.data for record in sent] == [plan.request_frame] * 2


# ========================================================================================
# Every entry on the bench
# ========================================================================================


@pytest.mark.parametrize("entry", ["tcp", "tcp-4e", "udp", "udp-4e", "tcp-ascii"])
async def test_a_block_reads_the_same_values_on_every_entry(entry: str) -> None:
    """Four transports and two codings decode through one compiled decoder."""
    async with bench() as simulator, client_for(simulator, entry) as plc:
        load_bench_values(simulator)
        state = await bind(plc, LoopState).read()

    assert (state.setpoint, state.scan, state.fault, state.mode) == (
        55.0,
        1_234_567,
        True,
        3,
    )


async def test_a_4e_plan_patches_a_fresh_serial_into_the_same_frame() -> None:
    """The serial is the only in-band correlation SLMP has, so it must still move."""
    async with bench() as simulator, client_for(simulator, "tcp-4e") as plc:
        load_bench_values(simulator)
        plan = bind(plc, LoopState)
        first = await plan.read()
        second = await plan.read()

    assert first.tx is not None and second.tx is not None
    assert first.tx.serial != second.tx.serial
    assert first.setpoint == second.setpoint == 55.0


# ========================================================================================
# Writing
# ========================================================================================


async def test_write_puts_the_named_fields_where_the_layout_says() -> None:
    async with bench() as simulator, client_for(simulator) as plc:
        plan = bind(plc, Scratch)
        await plan.write(value=12.5, count=70_000, trim=-3, name="ACID")
        state = await plan.read()

    memory = simulator.memory
    assert memory.get_f32("D", 100) == 12.5
    assert memory.get_u32("D", 102) == 70_000
    assert memory.get_u16("D", 104) == 0xFFFD
    assert state.value == 12.5
    assert state.count == 70_000
    assert state.trim == -3
    assert state.name == "ACID"


async def test_write_block_round_trips_a_whole_typed_instance() -> None:
    async with bench() as simulator, client_for(simulator) as plc:
        plan = bind(plc, Scratch)
        await plan.write_block(
            Scratch(value=-1.5, count=9, trim=7, name="BASE12")
        )
        state = await plan.read()

    assert (state.value, state.count, state.trim, state.name) == (-1.5, 9, 7, "BASE12")


async def test_writing_bits_touches_only_the_bits_named() -> None:
    """A single bit is 1402 in bit units, so the other fifteen of the window are safe."""
    async with bench() as simulator, client_for(simulator) as plc:
        simulator.memory.write_bits("M", 100, [False] * 16)
        simulator.memory.write_bits("M", 103, [True])
        plan = bind(plc, LoopState)
        await plan.write(fault=True)
        state = await plan.read()

    assert state.fault is True
    assert state.alarm is False
    assert simulator.memory.read_bits("M", 103, 1) == (True,)


async def test_one_write_is_one_transaction() -> None:
    async with bench() as simulator, client_for(simulator) as plc:
        plan = bind(plc, Scratch)
        simulator.clear_transcript()
        await plan.write(value=1.0, count=2, trim=3, name="AB")

    assert len(simulator.transcript) == 2


# ========================================================================================
# Splitting, and what it costs
# ========================================================================================


async def test_a_split_plan_reads_every_field_and_reports_the_span() -> None:
    """Several requests are several snapshots, and the result type says so."""
    annotations = {f"f{index}": U16 for index in range(200)}
    made: type[Any] = type("Wide200", (), {"__annotations__": annotations})
    block = plc_block(base="D1000")(made)

    async with bench() as simulator:
        for index in range(200):
            simulator.memory.set_u16("D", 1000 + index, index)
        async with client_for(simulator) as plc:
            plan = bind(plc, block, allow_split=True)
            assert isinstance(plan, SplitBlockPlan)
            simulator.clear_transcript()
            result = await plan.read()

    assert isinstance(result, Split)
    assert len(result.transactions) == 2
    assert result.snapshot_span_ns > 0
    assert len(simulator.transcript) == 4
    assert result.block.f0 == 0
    assert result.block.f199 == 199


# ========================================================================================
# The failures that are the reason this library exists
# ========================================================================================


async def test_an_end_code_from_a_block_read_raises_with_the_block_named() -> None:
    """``validate_ranges=False`` turns off our table, not the CPU's own opinion.

    ``D7999`` read as two registers reaches ``D8000``, which returned ``0xC056`` on
    FX5U-32MT/DS fw 1.065. With the range table off, bind cannot catch it and the PLC
    does -- which is the path that has to carry the block's name into the exception.
    """

    @plc_block(base="D7999")
    class OverTheEdge:
        value: F32

    async with bench() as simulator, client_for(simulator, validate_ranges=False) as plc:
        plan = bind(plc, OverTheEdge)
        with pytest.raises(SlmpDeviceRangeError) as caught:
            await plan.read()

    assert caught.value.end_code == 0xC056
    assert "OverTheEdge.read()" in str(caught.value)


async def test_a_plan_refuses_to_resume_into_a_different_cpu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Graft G6, proved by the transcript: nothing at all goes out on the wire."""
    async with bench() as simulator, client_for(simulator) as plc:
        load_bench_values(simulator)
        plan = bind(plc, LoopState)
        await plan.read()
        monkeypatch.setattr(
            plc,
            "_identity",
            CpuIdentity(
                model="R04CPU",
                model_code=0x4806,
                family=Family.IQ_R,
                raw=b"R04CPU          ",
            ),
        )
        simulator.clear_transcript()
        with pytest.raises(SlmpTargetChangedError):
            await plan.read()
        assert simulator.transcript == ()


# ========================================================================================
# The PLC's own clock
# ========================================================================================


async def test_a_configured_plc_clock_rides_inside_the_same_snapshot() -> None:
    """The only way to tell "the network was slow" from "the CPU did not scan"."""
    async with bench() as simulator:
        client = client_for(simulator, plc_clock=PlcClockSource("D8", kind="u32"))
        async with client as plc:
            load_bench_values(simulator)
            plan = bind(plc, LoopState)
            state = await plan.read()

    assert state.tx is not None
    assert state.tx.plc_clock == 1_234_567
    assert state.scan == 1_234_567


# ========================================================================================
# describe(), against a real bound connection
# ========================================================================================


async def test_describe_names_the_cpu_that_answered_the_handshake() -> None:
    async with bench() as simulator, client_for(simulator) as plc:
        report = bind(plc, LoopState).describe()

    assert "FX5U-32MT/DS" in report
    assert "melsec:iq-f/fx5u" in report
    assert "M100 +16 points" in report
    assert "points: 2 word + 5 double-word = 7" in report


async def test_a_block_of_every_field_type_round_trips() -> None:
    """One of each, so a width that decoded into the wrong register would show up."""

    @plc_block(base="D100")
    class Everything:
        wide: F64
        single: F32
        unsigned: U32
        small: I16
        plain: U16
        text: str = Str(length=4)
        flag: Bit = at("M110")

    async with bench() as simulator, client_for(simulator) as plc:
        memory = simulator.memory
        memory.write_words("D", 100, list(struct.unpack("<4H", struct.pack("<d", 2.5))))
        memory.set_f32("D", 104, -1.25)
        memory.set_u32("D", 106, 4_000_000_000)
        memory.set_u16("D", 108, 0x8000)
        memory.set_u16("D", 109, 4242)
        memory.write_words("D", 110, list(struct.unpack("<3H", b"WXYZ\x00\x00")))
        memory.write_bits("M", 110, [True])
        plan: BlockPlan[Everything] = bind(plc, Everything)
        value = await plan.read()

    assert value.wide == 2.5
    assert value.single == -1.25
    assert value.unsigned == 4_000_000_000
    assert value.small == -32768
    assert value.plain == 4242
    assert value.text == "WXYZ"
    assert value.flag is True
