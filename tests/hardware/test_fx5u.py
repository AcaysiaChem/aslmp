"""Tier 9 -- the real FX5U-32MT/DS. The only tests here that can settle anything.

Everything else in this suite proves the library agrees with a simulator we wrote from
the same manuals. This file is the one place where a Mitsubishi CPU is the oracle.

Gated on ``ASLMP_TEST_HOST``; marked ``hardware``; never run in CI::

    ASLMP_TEST_HOST=192.168.10.250 .venv/Scripts/python.exe -m pytest tests/hardware

**The bench.** FX5U-32MT/DS firmware 1.065 at 192.168.10.250, in RUN at ~1024 scans/s,
no physical I/O wired. Five configured SLMP connection entries: TCP 5000 (in use by other
tooling -- not touched here), TCP 5002/5003/5004, and UDP 5001, which is point-to-point
and bound to 192.168.10.41.

**The register map**, all f32 low word first: ``D0`` IO_SP, ``D2`` IO_PV, ``D4`` IO_MV,
``D6`` IO_Err, ``D8`` IO_Scan (a free-running scan counter). Scratch: ``D100``-``D119``
and ``M100``-``M119``, stability-checked before use and restored after.

**What this file will not do.** It never sends ``0x1001``/``1002``/``1003``/``1005``/
``1006``. No client here is constructed with ``allow_remote_control=True``, and
``test_no_client_in_this_file_can_reach_a_remote_control_command`` asserts it by walking
this module's own AST, because a comment enforces nothing. It never writes outside the
scratch range, and the last test re-reads the setpoint and the scan counter to prove the
CPU was left running and unchanged.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import os
import socket  # noqa: TID251 - the raw-socket control must share no code with the library
import statistics
import struct
import time
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from aslmp import (
    F32,
    Concurrency,
    FrameType,
    Plc,
    PlcBlock,
    SlmpAddressRangeError,
    SlmpCapabilityError,
    SlmpConcurrentTransactionError,
    SlmpConfigurationError,
    SlmpConnectionEntryBusyError,
    SlmpDeviceRangeError,
    SlmpPointLimitError,
    SlmpWordPointCountError,
    TransportKind,
    dword,
    plc_block,
    word,
)
from aslmp.profile import Family

HOST = os.environ.get("ASLMP_TEST_HOST")
TCP_PORT = int(os.environ.get("ASLMP_TEST_TCP_PORT", "5002"))
TCP_PORT_ALT = int(os.environ.get("ASLMP_TEST_TCP_PORT_ALT", "5003"))
TCP_PORT_RAW = int(os.environ.get("ASLMP_TEST_TCP_PORT_RAW", "5004"))
UDP_PORT = int(os.environ.get("ASLMP_TEST_UDP_PORT", "5001"))
PROFILE = os.environ.get("ASLMP_TEST_PROFILE", "melsec:iq-f/fx5u")

pytestmark = [
    pytest.mark.hardware,
    pytest.mark.skipif(HOST is None, reason="set ASLMP_TEST_HOST to run against a real PLC"),
]

# The bench map. Named once so a misreading is one edit, not twenty.
SP, PV, MV, ERR, SCAN = "D0", "D2", "D4", "D6", "D8"
SCRATCH_WORD = "D100"
SCRATCH_BIT = "M100"

# Remote RUN / STOP / PAUSE / LATCH CLEAR / RESET. Forbidden on this CPU: a memory-card
# error once made it refuse a remote RUN and need a physical power cycle.
REMOTE_COMMANDS = frozenset({0x1001, 0x1002, 0x1003, 0x1005, 0x1006})
REMOTE_METHODS = frozenset({"run", "stop", "pause", "latch_clear", "reset"})

# One 0401 read of one register, hand-built. Used only by the raw-socket control and the
# coalescing probe, which must share no code with the library they are checking.
# 3E binary request: 50 00 | 00 FF FF 03 00 | L | 10 00 (monitoring timer) | 0401 0000.
RAW_HEAD = b"\x50\x00\x00\xff\xff\x03\x00\x0c\x00\x10\x00\x01\x04\x00\x00"


def d_spec(number: int) -> bytes:
    """Three little-endian device-number bytes then the D device code (0xA8), binary."""
    return struct.pack("<I", number)[:3] + bytes([0xA8])


def measured(label: str, **numbers: object) -> None:
    """Print a measurement so ``-s`` output is the artifact, not just a pass/fail."""
    body = "  ".join(f"{key}={value}" for key, value in numbers.items())
    print(f"\n[MEASURED] {label}: {body}")


@contextlib.asynccontextmanager
async def bench(port: int = TCP_PORT, **kwargs: Any) -> AsyncIterator[Plc]:
    """A connected client on one entry. One TCP connection per entry, always."""
    assert HOST is not None
    plc = Plc(HOST, port, profile=PROFILE, **kwargs)
    async with plc:
        yield plc


# ========================================================================================
# The handshake
# ========================================================================================


async def test_the_handshake_identifies_the_cpu_and_proves_the_socket() -> None:
    """``socket.connect()`` lies; ``0x0619`` plus ``0x0101`` does not.

    The 0x0619 Self Test is zero-side-effect and its latency equals a real read, so it is
    both the liveness proof and the first honest latency sample of the connection.
    """
    async with bench() as plc:
        info = plc.info
        assert info is not None
        assert plc.state.usable
        assert plc.model == "FX5U-32MT/DS"
        assert plc.model_code == 0x4A49
        assert plc.identity is not None
        assert plc.identity.family is Family.IQ_F
        assert info.handshake is not None
        assert info.handshake.end_code == 0
        assert info.handshake.command == 0x0619
        measured(
            "handshake",
            model=plc.model,
            model_code=hex(plc.model_code or 0),
            wire_ms=round(info.handshake.timing.wire_ms, 3),
            local=f"{info.local[0]}:{info.local[1]}",
        )


async def test_a_second_connection_to_one_entry_is_named_and_not_a_timeout() -> None:
    """The CPU accepts the TCP handshake on a busy entry and then immediately FINs.

    Pooling against one configured entry cannot work, so this is its own error class
    rather than a three-second timeout. Measured on FX5U-32MT/DS fw 1.065.
    """
    async with bench(TCP_PORT_ALT) as first:
        assert first.state.usable
        assert HOST is not None
        second = Plc(HOST, TCP_PORT_ALT, profile=PROFILE, timeout=2.0, connect_timeout=2.0)
        with pytest.raises(SlmpConnectionEntryBusyError):
            await second.connect()
        await second.aclose()
        assert first.counters.entry_busy == 0, "the busy one was the second client"
        await first.read_u16(SP)  # the first connection kept working


# ========================================================================================
# The running controller, decoded
# ========================================================================================


async def test_the_floats_decode_low_word_first_against_the_running_controller() -> None:
    """One batch read of D0-D9, then five typed reads, and they must agree.

    The batch read is the independent oracle: ``read_words`` returns raw registers and
    this test reassembles the floats itself, low word first. If the high-word-first
    reading were the right one every value here would differ, and on this bench the
    60.0 setpoint would read as 8.4e-44.
    """
    async with bench() as plc:
        raw = await plc.read_words("D0", 10)
        assert len(raw) == 10
        wrong: list[float] = []
        values: list[float] = []
        for index, address in enumerate((SP, PV, MV, ERR)):
            low, high = raw[index * 2], raw[index * 2 + 1]
            expected = struct.unpack("<f", struct.pack("<HH", low, high))[0]
            other = struct.unpack("<f", struct.pack("<HH", high, low))[0]
            got = await plc.read_f32(address)
            assert got == expected, (
                f"{address} raw {low:#06x} {high:#06x} decoded as {got!r}, but low word "
                f"first is {expected!r}"
            )
            values.append(got)
            wrong.append(other)
        # D8 is a free-running counter, so it cannot be compared to an earlier sample
        # for equality. Bracket it instead: the low-word-first assembly of the same two
        # registers must fall between two u32 reads taken either side of it, and the
        # high-word-first assembly must not. That is a real oracle rather than a
        # coincidence, because the two orderings are ~65000x apart on this value.
        before = await plc.read_u32(SCAN)
        pair = await plc.read_words(SCAN, 2)
        after = await plc.read_u32(SCAN)
        low_first = pair[0] | (pair[1] << 16)
        high_first = pair[1] | (pair[0] << 16)
        assert before <= low_first <= after, (
            f"{pair} assembled low word first is {low_first}, outside the bracket "
            f"[{before}, {after}] the CPU's own u32 read gives"
        )
        assert not before <= high_first <= after, "high word first is not distinguishable"
        scan = after
        measured(
            "D0-D9",
            words=" ".join(f"{value:#06x}" for value in raw),
            sp=values[0],
            pv=values[1],
            mv=values[2],
            err=values[3],
            scan=scan,
            high_first_would_be=" ".join(f"{value:.4g}" for value in wrong),
        )


async def test_the_scan_counter_advances_between_two_reads() -> None:
    """D8 is free-running at ~1024 scans/s, so it is the bench's own liveness proof."""
    async with bench() as plc:
        first = await plc.read_u32(SCAN)
        await asyncio.sleep(0.25)
        second = await plc.read_u32(SCAN)
        assert second > first, "the CPU is not scanning; every other result here is stale"
        measured("scan counter", first=first, second=second, delta=second - first)


# ========================================================================================
# The block plan: one transaction against five
# ========================================================================================


@plc_block(base="D0")
class LoopState(PlcBlock):
    """The bench map, declared once. Five values, one 0403, one snapshot."""

    setpoint: F32
    process_value: F32
    output: F32
    error: F32
    scan: F32  # a REAL, not a counter. See test_declaring_scan_as_u32_reads_a_bit_pattern.


async def test_one_bound_block_read_beats_five_batch_reads_and_is_one_snapshot() -> None:
    """The whole point of ``bind``: five values sampled together, frame prebuilt once.

    Five separate ``0401``s sample the plant five times, milliseconds apart, and no type
    says so. One ``0403`` samples them once. Both latencies are recorded.
    """
    async with bench() as plc:
        plan = plc.bind(LoopState)
        assert plan.points == 5
        assert plan.request_frame, "the 0403 frame is built at bind, not per cycle"

        await plan.read()  # warm the path; the first read of a connection is slower
        block_ms: list[float] = []
        for _ in range(9):
            state = await plan.read()
            assert state.tx is not None
            assert state.tx.command == 0x0403
            assert state.tx.prebuilt is True
            block_ms.append(state.tx.timing.wire_ms)

        separate_wire: list[float] = []
        separate_wall: list[float] = []
        for _ in range(9):
            started = time.perf_counter_ns()
            wires = 0.0
            for address in (SP, PV, MV, ERR, SCAN):
                wires += (await plc.timed.read_f32(address)).tx.timing.wire_ms
            separate_wall.append((time.perf_counter_ns() - started) / 1e6)
            separate_wire.append(wires)

        block_p50 = statistics.median(block_ms)
        wall_p50 = statistics.median(separate_wall)
        measured(
            "one block read vs five batch reads",
            block_p50_ms=round(block_p50, 3),
            five_reads_wire_sum_p50_ms=round(statistics.median(separate_wire), 3),
            five_reads_wall_p50_ms=round(wall_p50, 3),
            speedup=round(wall_p50 / block_p50, 2),
        )
        assert block_p50 < wall_p50, "one round trip must beat five"

        # The block's own values must agree with the same registers read separately.
        # Only the free-running scan counter may move between the two samples.
        state = await plan.read()
        assert (state.setpoint, state.process_value, state.output, state.error) == (
            await plc.read_f32(SP),
            await plc.read_f32(PV),
            await plc.read_f32(MV),
            await plc.read_f32(ERR),
        )
        assert state.scan > 0


async def test_declaring_scan_as_u32_reads_a_bit_pattern_and_nothing_says_so() -> None:
    """The one silent-wrong-data path the wire cannot close, pinned as a regression.

    ``IO_Scan`` is a ``REAL`` -- the PLC's own ST does ``IO_Scan := IO_Scan + 1.0`` and
    ``IF IO_Scan > 1.0E7``. Read it as ``u32`` and you get the float's bit pattern: a
    plausible-looking integer, end code ``0x0000``, no error anywhere. A D register carries
    no type on the wire, so the library cannot detect this and must not pretend to.

    What makes it genuinely dangerous, and what this test exists to pin: IEEE-754 bit
    patterns rise monotonically for positive floats, so the wrong reading still *increases*
    every cycle. A naive "is the counter advancing?" assertion passes. Ours did, until the
    rate was checked against the documented ~1024 scans/s.
    """
    async with bench() as plc:
        first_f32 = await plc.read_f32(SCAN)
        first_u32 = await plc.read_u32(SCAN)
        await asyncio.sleep(2.0)
        second_f32 = await plc.read_f32(SCAN)
        second_u32 = await plc.read_u32(SCAN)

        as_real = second_f32 - first_f32
        as_uint = second_u32 - first_u32
        expected = 1024 * 2.0  # the CPU's measured scan rate over the sleep

        # Both readings rise. Only one of them is the scan count.
        assert as_real > 0 and as_uint > 0, "both readings advance -- that is the trap"
        assert 0.5 < as_real / expected < 2.0, "the f32 reading tracks the real scan rate"
        assert not 0.5 < as_uint / expected < 2.0, (
            f"the u32 reading advanced by {as_uint:,} over 2 s against an expected "
            f"~{expected:,.0f}. If this ever falls in range, IO_Scan's type in the PLC "
            f"program has changed and this test's premise is stale -- check the global "
            f"label in GX Works3 before editing anything here."
        )
        measured(
            "IO_Scan read as the right type and the wrong one",
            as_f32_delta=round(as_real, 1),
            as_u32_delta=as_uint,
            expected_delta=expected,
            u32_wrong_by=f"{as_uint / expected:.1f}x",
        )


# ========================================================================================
# Scratch writes -- stability-checked, restored
# ========================================================================================


async def stable_words(plc: Plc, address: str, count: int) -> tuple[int, ...]:
    """Read twice with a real gap. A register the ladder is driving is not scratch."""
    first = await plc.read_words(address, count)
    await asyncio.sleep(0.3)
    second = await plc.read_words(address, count)
    assert first == second, (
        f"{address}+{count} changed between two reads ({first} then {second}); the ladder "
        f"is driving it and this test must not write there"
    )
    return first


async def test_a_float_whose_low_word_exceeds_0x7fff_round_trips_through_scratch() -> None:
    """The defect that breaks ``pymcprotocol``: a register above ``0x7FFF`` packed signed.

    Every value is written to ``D100``, read back through three independent paths (typed
    f32, raw registers, a random read) and then restored. ``3.4028235e38`` puts ``0xFFFF``
    in the low word and ``0x7F7F`` in the high one; ``-1.0`` puts ``0xBF80`` in the high
    one. A library that packs a register with ``'<h'`` raises on both.
    """
    async with bench() as plc:
        original = await stable_words(plc, SCRATCH_WORD, 2)
        try:
            for value in (1234.5, -1.0, 3.4028235e38, -3.4028235e38, 65535.5):
                # The CPU stores 32 bits. A Python float is 64, so the value that comes
                # back is the f32 rounding of what went out and comparing to the literal
                # would be comparing to a number that was never on the wire.
                exact = struct.unpack("<f", struct.pack("<f", value))[0]
                ack = await plc.timed.write_f32(SCRATCH_WORD, value)
                back = await plc.read_f32(SCRATCH_WORD)
                registers = await plc.read_words(SCRATCH_WORD, 2)
                point = await plc.read_random([dword(SCRATCH_WORD, kind="f32")])
                assert back == exact
                assert point[0] == exact
                assert registers == struct.unpack("<HH", struct.pack("<f", value))
                measured(
                    "f32 round trip",
                    wrote=value,
                    read=back,
                    registers=f"{registers[0]:#06x} {registers[1]:#06x}",
                    low_word_over_7fff=registers[0] > 0x7FFF,
                    write_ms=round(ack.tx.timing.wire_ms, 3),
                )
            # 1234.5 is the value the design cites: D100=0x5000, D101=0x449A.
            await plc.write_f32(SCRATCH_WORD, 1234.5)
            assert await plc.read_words(SCRATCH_WORD, 2) == (0x5000, 0x449A)
        finally:
            await plc.write_words(SCRATCH_WORD, list(original))
        assert await plc.read_words(SCRATCH_WORD, 2) == original


async def test_verified_writes_cost_exactly_one_extra_round_trip() -> None:
    """``verify=True`` reads back. It is a second transaction and the counters say so."""
    async with bench() as plc:
        original = await stable_words(plc, "D102", 2)
        try:
            base = plc.counters.transactions_started
            await plc.write_f32("D102", 12.5)
            one = plc.counters.transactions_started - base
            await plc.write_f32("D102", 25.5, verify=True)
            two = plc.counters.transactions_started - base - one
            assert (one, two) == (1, 2)
            assert await plc.read_f32("D102") == 25.5
        finally:
            await plc.write_words("D102", list(original))


async def test_bit_access_on_the_scratch_flags_and_a_clean_restore() -> None:
    """M100-M119 in bit units, then folded into one word point, then restored."""
    async with bench() as plc:
        first = await plc.read_bits(SCRATCH_BIT, 20)
        await asyncio.sleep(0.3)
        assert first == await plc.read_bits(SCRATCH_BIT, 20), "M100+20 is being driven"
        try:
            await plc.write_bit(SCRATCH_BIT, True)
            assert await plc.read_bit(SCRATCH_BIT) is True
            pattern = [index % 3 == 0 for index in range(20)]
            ack = await plc.timed.write_bits(SCRATCH_BIT, pattern)
            assert list(await plc.read_bits(SCRATCH_BIT, 20)) == pattern
            folded = await plc.read_random([word(SCRATCH_BIT, kind="bits")])
            window = folded[0]
            assert isinstance(window, tuple), "a bits point decodes to a tuple of bools"
            assert list(window)[:16] == pattern[:16], "16 bits from one word point"
            measured(
                "bits",
                pattern="".join("1" if bit else "0" for bit in pattern),
                write_ms=round(ack.tx.timing.wire_ms, 3),
                points=ack.points,
            )
        finally:
            await plc.write_bits(SCRATCH_BIT, list(first))
        assert await plc.read_bits(SCRATCH_BIT, 20) == first


# ========================================================================================
# Real errors, named correctly
# ========================================================================================


async def test_out_of_range_is_refused_before_the_wire_and_is_0xc056_on_it() -> None:
    """D ends at D7999. The client refuses D8000 for free; the CPU answers 0xC056.

    Both halves matter. The client-side refusal is what makes a typo cost nothing; the
    wire answer is what proves the shipped range table is not inventing the boundary.
    """
    async with bench() as plc:
        before = plc.counters.transactions_started
        with pytest.raises(SlmpAddressRangeError, match="D7999"):
            await plc.read_words("D8000", 1)
        assert plc.counters.transactions_started == before, "the refusal reached the socket"
        assert len(await plc.read_words("D7999", 1)) == 1, "D7999 is inside the range"

    async with bench(TCP_PORT, validate_ranges=False) as plc:
        with pytest.raises(SlmpDeviceRangeError) as caught:
            await plc.read_words("D8000", 1)
        assert caught.value.end_code == 0xC056
        measured("D8000 with validate_ranges=False", end_code=hex(caught.value.end_code))


async def test_over_the_word_ceiling_is_refused_and_is_0xc052_on_the_wire() -> None:
    """960 words is the batch ceiling. 961 is refused; ``raw_command`` proves the code.

    Point limits are deliberately not defeatable through the typed API -- they are a
    property of the silicon, not of the parameter file -- so the only way to see the
    CPU's own answer is the documented escape hatch, which bypasses command validation
    and nothing else: the frame, the L guard, the gate and the end-code raise all apply.
    """
    async with bench() as plc:
        before = plc.counters.transactions_started
        with pytest.raises(SlmpPointLimitError, match="960"):
            await plc.read_words("D0", 961)
        assert plc.counters.transactions_started == before

        assert len(await plc.read_words("D0", 960)) == 960, "960 is accepted"

        with pytest.raises(SlmpWordPointCountError) as caught:
            await plc.raw_command(
                0x0401, 0x0000, d_spec(0) + struct.pack("<H", 961), mutates=False
            )
        assert caught.value.end_code == 0xC052
        measured("961 words via raw_command", end_code=hex(caught.value.end_code))


async def test_monitor_is_refused_pre_transport_and_sends_nothing() -> None:
    """``0801``/``0802`` are iQ-R commands. The FX5U answers ``0xC059``; we never ask.

    Refusing before the socket is the point: the alternative a reader of the generic
    reference expects is ``0xC05D`` "monitor not registered", and the difference between
    diagnosing those two is a day.
    """
    async with bench() as plc:
        before = plc.counters.transactions_started
        sent = plc.counters.bytes_sent
        with pytest.raises(SlmpCapabilityError, match="0x0801"):
            await plc.monitor_register([word(SCRATCH_WORD)])
        assert plc.counters.transactions_started == before, "0801 reached the CPU"
        assert plc.counters.bytes_sent == sent, "0801 put bytes on the wire"


async def test_two_transactions_at_once_on_one_tcp_socket_is_inexpressible() -> None:
    """The measured coalescing corruption, refused by name, on real iron.

    On this CPU two requests written before the first response is read return ONE
    response, for the LAST request, end code 0x0000 -- undetectable on 3E. There is no
    public ``send()``, so the second caller is refused instead of corrupted.
    """
    async with bench() as plc:
        outcomes = await asyncio.gather(
            plc.read_f32(SP), plc.read_f32(MV), return_exceptions=True
        )
        refused = [x for x in outcomes if isinstance(x, SlmpConcurrentTransactionError)]
        values = [x for x in outcomes if isinstance(x, float)]
        assert len(refused) == 1
        assert len(values) == 1
        assert plc.counters.concurrent_rejections == 1


def test_the_coalescing_corruption_is_still_real_on_this_cpu() -> None:
    """The measurement the whole architecture rests on, taken again on a raw socket.

    Read-only: two ``0401`` reads of one register each, written in one ``send``. If the
    CPU answered twice this test fails loudly, because the in-flight gate would then be
    defending against a defect that no longer exists.
    """
    assert HOST is not None
    both = RAW_HEAD + d_spec(0) + struct.pack("<H", 1)
    both += RAW_HEAD + d_spec(8) + struct.pack("<H", 1)
    received = b""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(3.0)
    try:
        sock.connect((HOST, TCP_PORT_RAW))
        sock.sendall(both)
        sock.settimeout(1.0)
        with contextlib.suppress(TimeoutError, OSError):
            while len(received) < 64:
                chunk = sock.recv(64)
                if not chunk:
                    break
                received += chunk
    finally:
        sock.close()
    end_code = struct.unpack_from("<H", received, 9)[0] if len(received) >= 11 else None
    measured(
        "two 0401s written in one send",
        bytes_back=len(received),
        frame=received.hex(),
        end_code=None if end_code is None else hex(end_code),
    )
    assert len(received) < 26, (
        f"the CPU answered {len(received)} bytes to two coalesced requests. The measured "
        f"corruption is ONE response of 13 bytes; if that has changed, the one-in-flight "
        f"rule needs re-deriving, not relaxing."
    )
    assert end_code == 0x0000, "the corruption reports success; that is what makes it fatal"
    assert len(received) == 13, "one 3E response to a one-register read is 11 + 2 bytes"


# ========================================================================================
# UDP, and the pipelining only 4E may have
# ========================================================================================


@contextlib.asynccontextmanager
async def udp(depth: int) -> AsyncIterator[Plc]:
    assert HOST is not None
    plc = Plc(
        HOST,
        UDP_PORT,
        profile=PROFILE,
        transport=TransportKind.UDP,
        frame=FrameType.FOUR_E,
        udp_pipeline_depth=depth,
        timeout=3.0,
    )
    async with plc:
        yield plc


async def test_udp_reaches_the_same_cpu_and_the_record_says_which_transport() -> None:
    """A transaction record that cannot tell UDP from TCP cannot explain a tail."""
    async with udp(1) as plc:
        assert plc.model == "FX5U-32MT/DS"
        reading = await plc.timed.read_f32(SP)
        assert reading.tx.transport is TransportKind.UDP
        assert reading.tx.frame is FrameType.FOUR_E
        assert reading.tx.serial is not None, "4E carries a serial No.; 3E does not"
        measured(
            "udp single read",
            value=reading.value,
            serial=reading.tx.serial,
            wire_ms=round(reading.tx.timing.wire_ms, 3),
            transport=reading.tx.transport.value,
        )


def test_3e_over_udp_cannot_be_asked_to_pipeline() -> None:
    """Positional matching plus real loss is the coalescing bug wearing a hat.

    Refused at construction, before a socket exists: there is no serial No. to correlate
    a 3E response by, and datagrams measurably do get lost.
    """
    assert HOST is not None
    with pytest.raises(SlmpConfigurationError, match="serial"):
        Plc(
            HOST,
            UDP_PORT,
            profile=PROFILE,
            transport=TransportKind.UDP,
            frame=FrameType.THREE_E,
            udp_pipeline_depth=8,
        )


@pytest.mark.parametrize("depth", [8, 16])
async def test_udp_pipelining_at_depth(depth: int) -> None:
    """Measured clean to 32 and ~31% lost at 64. 8 and 16 must lose nothing.

    A lost datagram has no end code, no ICMP and no error of any kind, so the only
    evidence is a serial that never comes back. Anything missing here is a real failure
    and is reported as one; nothing in this library retries.
    """
    async with udp(depth) as plc:
        await plc.read_f32(SP)
        started = time.perf_counter_ns()
        results = await asyncio.gather(
            *[plc.timed.read_f32(SP) for _ in range(depth)], return_exceptions=True
        )
        elapsed_ms = (time.perf_counter_ns() - started) / 1e6
        failures = [item for item in results if isinstance(item, BaseException)]
        good = [item for item in results if not isinstance(item, BaseException)]
        serials = sorted(item.tx.serial for item in good if item.tx.serial is not None)
        measured(
            f"udp burst at depth {depth}",
            answered=len(good),
            lost=len(failures),
            elapsed_ms=round(elapsed_ms, 2),
            rate_txn_s=round(len(good) / (elapsed_ms / 1000)),
            serials=f"{serials[0]}..{serials[-1]}" if serials else "none",
            max_wire_ms=round(max(item.tx.timing.wire_ms for item in good), 2) if good else 0,
        )
        assert not failures, f"lost {len(failures)} of {depth}: {failures[0]!r}"
        assert len(serials) == len(set(serials)) == depth, "every reply matched its own serial"
        assert all(item.tx.transport is TransportKind.UDP for item in good)


async def test_pipelined_udp_beats_serial_tcp_on_throughput() -> None:
    """The only reason to pipeline at all. Both numbers, same minute, same host."""
    reads = 16
    async with bench() as plc:
        await plc.read_f32(SP)
        started = time.perf_counter_ns()
        for _ in range(reads):
            await plc.read_f32(SP)
        tcp_ms = (time.perf_counter_ns() - started) / 1e6

    async with udp(reads) as pipelined:
        await pipelined.read_f32(SP)
        started = time.perf_counter_ns()
        await asyncio.gather(*[pipelined.read_f32(SP) for _ in range(reads)])
        udp_ms = (time.perf_counter_ns() - started) / 1e6

    measured(
        f"{reads} reads: serial TCP vs pipelined UDP",
        tcp_ms=round(tcp_ms, 2),
        tcp_rate_txn_s=round(reads / (tcp_ms / 1000)),
        udp_ms=round(udp_ms, 2),
        udp_rate_txn_s=round(reads / (udp_ms / 1000)),
        gain=round(tcp_ms / udp_ms, 2),
    )


# ========================================================================================
# Is the number we print the number that happened?
# ========================================================================================


def raw_socket_control(host: str, port: int, samples: int) -> list[float]:
    """A blocking socket, a hand-built ``0401`` for D4, and ``time.perf_counter_ns``.

    Shares no code with the library on purpose. A published latency without a
    same-session control is not a measurement: this bench gave p50 7.1 / p99 18.8 ms on
    one day and p50 10.3 / p99 95.2 ms on another.
    """
    frame = RAW_HEAD + d_spec(4) + struct.pack("<H", 2)
    out: list[float] = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.settimeout(3.0)
    try:
        sock.connect((host, port))
        for _ in range(samples):
            started = time.perf_counter_ns()
            sock.sendall(frame)
            head = sock.recv(9)
            length = struct.unpack_from("<H", head, 7)[0]
            body = b""
            while len(body) < length:
                body += sock.recv(length - len(body))
            out.append((time.perf_counter_ns() - started) / 1e6)
    finally:
        sock.close()
    return out


def percentile(values: Sequence[float], fraction: float) -> float:
    """Nearest-rank, so a p99 of 120 samples is a sample and not an interpolation."""
    ordered = sorted(values)
    rank = max(1, min(len(ordered), -(-int(fraction * len(ordered) * 1000) // 1000)))
    return ordered[rank - 1]


async def test_the_transaction_record_agrees_with_a_raw_socket_in_the_same_session() -> None:
    """``timing.wire_ms`` must be the round trip, not a number we like the look of.

    The library's figure legitimately includes the event-loop hop that the blocking
    control does not, so the assertion is that the two p50s are close and that the
    library is never *faster* than a raw socket -- which would mean the stamps are in
    the wrong place.
    """
    assert HOST is not None
    samples = 120
    control = raw_socket_control(HOST, TCP_PORT_RAW, samples)

    library: list[float] = []
    wall: list[float] = []
    async with bench() as plc:
        await plc.read_f32(MV)
        for _ in range(samples):
            started = time.perf_counter_ns()
            reading = await plc.timed.read_f32(MV)
            wall.append((time.perf_counter_ns() - started) / 1e6)
            library.append(reading.tx.timing.wire_ms)

    control_p50 = percentile(control, 0.50)
    record_p50 = percentile(library, 0.50)
    measured(
        "raw-socket control vs the transaction record",
        n=samples,
        control_p50=round(control_p50, 2),
        control_p99=round(percentile(control, 0.99), 2),
        record_p50=round(record_p50, 2),
        record_p99=round(percentile(library, 0.99), 2),
        wall_p50=round(percentile(wall, 0.50), 2),
        stdev_control=round(statistics.stdev(control), 2),
        stdev_record=round(statistics.stdev(library), 2),
    )
    assert record_p50 >= control_p50 - 1.0, (
        f"the record's p50 ({record_p50:.2f} ms) is below a raw socket's "
        f"({control_p50:.2f} ms); the wire stamps cannot be where they claim"
    )
    assert record_p50 <= control_p50 + 3.0, (
        f"the record's p50 ({record_p50:.2f} ms) is {record_p50 - control_p50:.2f} ms "
        f"above a raw socket's; that is overhead being reported as wire time"
    )
    for index, value in enumerate(library):
        assert value <= wall[index] + 0.001, "wire time cannot exceed the wall clock"


async def test_a_960_word_read_is_length_driven_and_stamped_at_the_last_chunk() -> None:
    """1931 bytes over a 1460-byte MSS. Reads are length-driven, never ``recv``-driven.

    The response is read as the fixed prefix and then exactly ``L`` more units, so it
    always takes at least two ``recv`` calls; that pair is structural and is not
    segmentation. ``segmented`` is true only when a read came back SHORT of what it
    asked for. Whether that happens is a scheduling race -- the bench saw the split on
    1 of 3 identical reads on 2026-09-06 and on none on 2026-09-07 -- so this asserts
    the invariants and records which way it went rather than demanding a split.
    """
    async with bench() as plc:
        splits = 0
        for _ in range(6):
            reading = await plc.timed.read_words("D0", 960)
            assert len(reading.value) == 960
            timing = reading.tx.timing
            chunks = timing.chunks
            assert reading.tx.response_bytes == 1931
            assert reading.tx.response_bytes == sum(chunk.nbytes for chunk in chunks)
            assert timing.received_at == chunks[-1].at, "stamped at the LAST chunk"
            assert len(chunks) >= 2, "prefix then body: always at least two reads"
            assert timing.segmented == any(chunk.partial for chunk in chunks)
            splits += 1 if timing.segmented else 0
            measured(
                "960-word read",
                response_bytes=reading.tx.response_bytes,
                chunks=len(chunks),
                sizes=" ".join(str(chunk.nbytes) for chunk in chunks),
                segmented=timing.segmented,
                wire_ms=round(timing.wire_ms, 3),
            )
        measured("960-word reads that actually split", of_six=splits)


async def test_serialised_concurrency_reports_its_own_queue_time() -> None:
    """A queue that hides its own delay makes every latency number in the process a lie."""
    async with bench(TCP_PORT_ALT, concurrency=Concurrency.SERIALIZE) as plc:
        readings = await asyncio.gather(
            plc.timed.read_f32(SP), plc.timed.read_f32(MV), plc.timed.read_f32(ERR)
        )
        queued = [reading.tx.timing.queue_ns for reading in readings]
        assert plc.counters.queue_waits >= 2
        assert max(queued) > 0
        assert plc.counters.concurrent_rejections == 0
        measured(
            "SERIALIZE queue",
            queue_ms=" ".join(f"{value / 1e6:.2f}" for value in queued),
            waits=plc.counters.queue_waits,
        )


# ========================================================================================
# Leave it as we found it
# ========================================================================================


def test_no_client_in_this_file_can_reach_a_remote_control_command() -> None:
    """Walk this module's own AST. ``allow_remote_control`` is never passed here.

    The user forbade remote control on this CPU: a memory-card error once made it refuse
    a remote RUN and need a physical power cycle. A comment saying so enforces nothing.
    """
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "allow_remote_control":
            offenders.append(f"allow_remote_control at line {node.lineno}")
        if isinstance(node, ast.Attribute) and node.attr in REMOTE_METHODS:
            owner = node.value
            if isinstance(owner, ast.Attribute) and owner.attr == "remote":
                offenders.append(f"plc.remote.{node.attr} at line {node.lineno}")
        if isinstance(node, ast.Call) and node.args:
            callee = node.func
            named = callee.attr if isinstance(callee, ast.Attribute) else None
            first = node.args[0]
            remote = isinstance(first, ast.Constant) and first.value in REMOTE_COMMANDS
            if named == "raw_command" and remote:
                offenders.append(f"raw_command at line {node.lineno}")
    assert not offenders, "remote control reached in a hardware test: " + ", ".join(offenders)


async def test_the_bench_is_left_running_and_the_scratch_is_clean() -> None:
    """The last word: the CPU still scans, the setpoint is untouched, scratch is zeroed.

    ``D0`` is never written anywhere in this file. This asserts the map still reads as a
    controller rather than as whatever a failed test left behind.
    """
    async with bench() as plc:
        status = await plc.read_cpu_status()
        assert status.name == "RUN", f"the CPU is in {status.name}, not RUN"
        first = await plc.read_u32(SCAN)
        await asyncio.sleep(0.3)
        second = await plc.read_u32(SCAN)
        assert second > first, "the scan counter stopped"

        scratch = await plc.read_words("D100", 20)
        bits = await plc.read_bits("M100", 20)
        setpoint = await plc.read_f32(SP)
        measured(
            "final state",
            cpu=status.name,
            scan_delta=second - first,
            setpoint=setpoint,
            scratch_words=" ".join(f"{value:#06x}" for value in scratch),
            scratch_bits="".join("1" if bit else "0" for bit in bits),
        )
        assert set(scratch) == {0}, f"scratch registers were left dirty: {scratch}"
        assert not any(bits), f"scratch flags were left set: {bits}"
