"""Unit tests for ``aslmp.timing`` — the seven-stamp transaction record (graft G4).

The oracle for the segmentation tests is a measurement, not the implementation:
**MELSEC iQ-F FX5U-32MT/DS firmware 1.065, 2026-09-06**, one 1931-byte response read
three times::

    trial 1: [(1931, 10.169 ms)]                    one chunk
    trial 2: [(1931,  9.299 ms)]                    one chunk
    trial 3: [(1460, 10.964 ms), (471, 14.010 ms)]  TWO chunks, 3.0 ms apart

Trial 3's true latency is 14.010 ms. Stamping the first chunk reports 11.0 ms. Every
test below that mentions a chunk is checking that we report 14.010.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

from aslmp.timing import (
    Chunk,
    Nanos,
    Phase,
    TimingBuilder,
    TimingIncompleteError,
    TimingOrderError,
    Transaction,
    TransactionTiming,
)

MS = 1_000_000
BASE = 1_000_000_000


class FakeClock:
    """A monotonic nanosecond clock a test controls exactly."""

    def __init__(self, start: int = BASE) -> None:
        self.now = start

    def __call__(self) -> int:
        return self.now

    def advance(self, ns: int) -> int:
        self.now += ns
        return self.now


class ScriptedClock:
    """Returns a fixed sequence of stamps, so a test can inject a non-monotonic one."""

    def __init__(self, stamps: list[int]) -> None:
        self.stamps = list(stamps)
        self.index = 0

    def __call__(self) -> int:
        value = self.stamps[self.index]
        self.index += 1
        return value


class FrameLike:
    """The structural shape ``Transaction`` asks of the wire option enums."""

    def __init__(self, name: str, value: str) -> None:
        self._name = name
        self._value = value

    @property
    def name(self) -> str:
        return self._name

    @property
    def value(self) -> str:
        return self._value


THREE_E = FrameLike("THREE_E", "3E")
BINARY = FrameLike("BINARY", "binary")
TCP = FrameLike("TCP", "tcp")


def trial_three() -> tuple[TransactionTiming, FakeClock]:
    """The measured segmented read, stamped through the builder."""
    clock = FakeClock()
    builder = TimingBuilder(clock, prev_received_at=Nanos(BASE - 1 * MS))
    clock.advance(200_000)
    builder.gate_acquired()
    clock.advance(300_000)
    builder.encoded()
    clock.advance(100_000)
    builder.sent()
    clock.advance(10_964_000)
    builder.chunk(1460)
    clock.advance(3_046_000)
    builder.chunk(471)
    clock.advance(50_000)
    builder.decoded()
    return builder.build(), clock


def a_transaction(timing: TransactionTiming, *, end_code: int = 0) -> Transaction:
    return Transaction(
        timing=timing,
        sequence=1,
        connection_id="c1",
        generation=0,
        after_reconnect=False,
        command=0x0401,
        subcommand=0x0000,
        frame=THREE_E,
        encoding=BINARY,
        transport=TCP,
        serial=None,
        request_bytes=21,
        response_bytes=1931,
        end_code=end_code,
        prebuilt=False,
    )


# ---------------------------------------------------------------------------
# The derived properties, exact against the injected clock
# ---------------------------------------------------------------------------


def test_every_derived_duration_is_exact_against_a_fake_clock() -> None:
    timing, _ = trial_three()
    assert timing.queue_ns == 200_000
    assert timing.encode_ns == 300_000
    assert timing.first_byte_ns == 10_964_000
    assert timing.transfer_ns == 3_046_000
    assert timing.wire_ns == 14_010_000
    assert timing.decode_ns == 50_000
    assert timing.total_ns == 14_660_000
    assert timing.host_gap_ns == 1 * MS
    assert timing.wire_ms == pytest.approx(14.010)


def test_wire_ns_is_sent_to_last_chunk_not_sent_to_first_chunk() -> None:
    timing, _ = trial_three()
    # The bug this record exists to prevent: 11.0 ms for a 14.0 ms transaction.
    assert timing.first_byte_ns == 10_964_000
    assert timing.wire_ns != timing.first_byte_ns
    assert timing.wire_ms == pytest.approx(14.010)
    assert timing.received_at == timing.chunks[-1].at
    assert timing.first_byte_at == timing.chunks[0].at


def test_unsegmented_trial_reports_the_single_chunk_for_both_stamps() -> None:
    clock = FakeClock()
    builder = TimingBuilder(clock)
    builder.gate_acquired()
    builder.encoded()
    builder.sent()
    clock.advance(10_169_000)
    builder.chunk(1931)
    builder.decoded()
    timing = builder.build()
    assert timing.wire_ms == pytest.approx(10.169)
    assert timing.transfer_ns == 0
    assert timing.first_byte_ns == timing.wire_ns
    assert not timing.segmented


def test_segmented_flag_and_byte_count() -> None:
    timing, _ = trial_three()
    assert timing.segmented
    assert timing.bytes_received == 1931
    assert [c.nbytes for c in timing.chunks] == [1460, 471]


def test_total_ns_covers_everything_the_caller_waited_for() -> None:
    timing, _ = trial_three()
    assert timing.send_ns == 100_000
    assert timing.total_ns == (
        timing.queue_ns
        + timing.encode_ns
        + timing.send_ns
        + timing.first_byte_ns
        + timing.transfer_ns
        + timing.decode_ns
    )


# ---------------------------------------------------------------------------
# Stamping the wrong chunk is unsayable
# ---------------------------------------------------------------------------


def test_builder_exposes_no_parameter_that_could_carry_a_stamp() -> None:
    expected = {
        "gate_acquired": [],
        "encoded": [],
        "sent": [],
        "chunk": ["nbytes"],
        "decoded": [],
        "build": [],
    }
    for name, params in expected.items():
        signature = inspect.signature(getattr(TimingBuilder, name))
        assert [p for p in signature.parameters if p != "self"] == params, name


def test_timing_refuses_a_receive_stamp_that_is_not_the_last_chunk() -> None:
    chunks = (Chunk(1460, Nanos(BASE + 11 * MS)), Chunk(471, Nanos(BASE + 14 * MS)))
    with pytest.raises(TimingOrderError, match="LAST chunk"):
        TransactionTiming(
            submitted_at=Nanos(BASE),
            gate_acquired_at=Nanos(BASE),
            encoded_at=Nanos(BASE),
            sent_at=Nanos(BASE),
            first_byte_at=chunks[0].at,
            received_at=chunks[0].at,  # the bug, spelled out
            chunks=chunks,
        )


def test_timing_refuses_a_first_byte_stamp_that_is_not_the_first_chunk() -> None:
    chunks = (Chunk(1460, Nanos(BASE + 11 * MS)), Chunk(471, Nanos(BASE + 14 * MS)))
    with pytest.raises(TimingOrderError, match="first_byte_at"):
        TransactionTiming(
            submitted_at=Nanos(BASE),
            gate_acquired_at=Nanos(BASE),
            encoded_at=Nanos(BASE),
            sent_at=Nanos(BASE),
            first_byte_at=chunks[1].at,
            received_at=chunks[1].at,
            chunks=chunks,
        )


def test_timing_refuses_receive_stamps_with_no_chunks_behind_them() -> None:
    with pytest.raises(TimingOrderError, match="no chunks"):
        TransactionTiming(
            submitted_at=Nanos(BASE),
            gate_acquired_at=Nanos(BASE),
            encoded_at=Nanos(BASE),
            sent_at=Nanos(BASE),
            first_byte_at=Nanos(BASE + MS),
            received_at=Nanos(BASE + MS),
        )


# ---------------------------------------------------------------------------
# Chunks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("nbytes", [0, -1])
def test_a_zero_or_negative_chunk_is_refused(nbytes: int) -> None:
    with pytest.raises(ValueError, match="must be positive"):
        Chunk(nbytes, Nanos(BASE))


def test_chunk_stamps_are_taken_from_the_clock_not_supplied() -> None:
    clock = FakeClock()
    builder = TimingBuilder(clock)
    builder.gate_acquired()
    builder.encoded()
    builder.sent()
    clock.advance(7 * MS)
    first = builder.chunk(1460)
    clock.advance(3 * MS)
    second = builder.chunk(471)
    assert first.at == BASE + 7 * MS
    assert second.at == BASE + 10 * MS
    assert builder.bytes_received == 1931


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------


def test_stamps_are_single_use() -> None:
    builder = TimingBuilder(FakeClock())
    builder.gate_acquired()
    with pytest.raises(TimingOrderError, match="already stamped"):
        builder.gate_acquired()


def test_stamps_must_be_taken_in_order() -> None:
    builder = TimingBuilder(FakeClock())
    with pytest.raises(TimingOrderError, match=r"encoded\(\) before gate_acquired"):
        builder.encoded()
    builder.gate_acquired()
    with pytest.raises(TimingOrderError, match=r"sent\(\) before encoded"):
        builder.sent()
    builder.encoded()
    with pytest.raises(TimingOrderError, match=r"chunk\(\) before sent"):
        builder.chunk(4)
    builder.sent()
    with pytest.raises(TimingOrderError, match=r"decoded\(\) before any chunk"):
        builder.decoded()


def test_a_chunk_after_decode_is_refused() -> None:
    clock = FakeClock()
    builder = TimingBuilder(clock)
    builder.gate_acquired()
    builder.encoded()
    builder.sent()
    builder.chunk(11)
    builder.decoded()
    with pytest.raises(TimingOrderError, match="after decoded"):
        builder.chunk(1)


def test_build_before_sent_is_refused() -> None:
    builder = TimingBuilder(FakeClock())
    builder.gate_acquired()
    builder.encoded()
    with pytest.raises(TimingOrderError, match="never reached the socket"):
        builder.build()


def test_a_non_monotonic_clock_raises_rather_than_producing_a_negative_duration() -> None:
    clock = ScriptedClock([BASE, BASE - 1])
    builder = TimingBuilder(clock)
    with pytest.raises(TimingOrderError, match="not monotonic"):
        builder.gate_acquired()


def test_a_non_monotonic_clock_raises_on_a_chunk_too() -> None:
    clock = ScriptedClock([BASE, BASE, BASE, BASE, BASE + 10, BASE + 5])
    builder = TimingBuilder(clock)
    builder.gate_acquired()
    builder.encoded()
    builder.sent()
    builder.chunk(4)
    with pytest.raises(TimingOrderError, match="not monotonic"):
        builder.chunk(4)


def test_out_of_order_stamps_are_refused_by_the_record_itself() -> None:
    with pytest.raises(TimingOrderError, match="precedes"):
        TransactionTiming(
            submitted_at=Nanos(BASE + 10),
            gate_acquired_at=Nanos(BASE),
            encoded_at=Nanos(BASE + 20),
            sent_at=Nanos(BASE + 30),
        )


# ---------------------------------------------------------------------------
# Incomplete transactions: raise, never substitute a zero
# ---------------------------------------------------------------------------


def test_a_timed_out_transaction_has_no_wire_ns_and_says_so() -> None:
    clock = FakeClock()
    builder = TimingBuilder(clock)
    builder.gate_acquired()
    builder.encoded()
    builder.sent()
    clock.advance(3_000 * MS)
    timing = builder.build()
    assert not timing.is_complete
    assert timing.chunks == ()
    for name in ("first_byte_ns", "transfer_ns", "wire_ns", "decode_ns", "total_ns"):
        with pytest.raises(TimingIncompleteError):
            getattr(timing, name)
    # The stamps it did take are still exact.
    assert timing.queue_ns == 0
    assert timing.encode_ns == 0


def test_a_partially_received_transaction_reports_first_byte_but_not_decode() -> None:
    clock = FakeClock()
    builder = TimingBuilder(clock)
    builder.gate_acquired()
    builder.encoded()
    builder.sent()
    clock.advance(11 * MS)
    builder.chunk(1460)
    timing = builder.build()
    assert timing.first_byte_ns == 11 * MS
    assert timing.wire_ns == 11 * MS
    assert not timing.is_complete
    with pytest.raises(TimingIncompleteError, match="decoded_at"):
        _ = timing.decode_ns


def test_the_first_transaction_of_a_connection_has_no_host_gap() -> None:
    clock = FakeClock()
    builder = TimingBuilder(clock)
    builder.gate_acquired()
    builder.encoded()
    builder.sent()
    builder.chunk(11)
    builder.decoded()
    timing = builder.build()
    assert not timing.has_host_gap
    with pytest.raises(TimingIncompleteError, match="prev_received_at"):
        _ = timing.host_gap_ns


def test_host_gap_is_the_previous_response_to_this_submission() -> None:
    timing, _ = trial_three()
    assert timing.has_host_gap
    assert timing.host_gap_ns == timing.submitted_at - (timing.prev_received_at or 0)


# ---------------------------------------------------------------------------
# Transaction
# ---------------------------------------------------------------------------


def test_dominant_phase_picks_the_largest() -> None:
    timing, _ = trial_three()
    tx = a_transaction(timing)
    assert tx.dominant_phase is Phase.ROUND_TRIP
    assert tx.phase_ns(Phase.ROUND_TRIP) == 10_964_000
    assert tx.phase_ns(Phase.TRANSFER) == 3_046_000
    assert tx.phase_ns(Phase.QUEUE) == 200_000
    assert tx.phase_ns(Phase.ENCODE) == 300_000
    assert tx.phase_ns(Phase.DECODE) == 50_000


def test_dominant_phase_breaks_ties_towards_the_earlier_phase() -> None:
    clock = FakeClock()
    builder = TimingBuilder(clock)
    clock.advance(MS)
    builder.gate_acquired()
    clock.advance(MS)
    builder.encoded()
    clock.advance(MS)
    builder.sent()
    clock.advance(MS)
    builder.chunk(11)
    clock.advance(MS)
    builder.decoded()
    tx = a_transaction(builder.build())
    assert tx.dominant_phase is Phase.QUEUE


def test_dominant_phase_of_an_incomplete_transaction_raises() -> None:
    builder = TimingBuilder(FakeClock())
    builder.gate_acquired()
    builder.encoded()
    builder.sent()
    tx = a_transaction(builder.build())
    with pytest.raises(TimingIncompleteError):
        _ = tx.dominant_phase


def test_transaction_ok_reflects_the_end_code_only() -> None:
    timing, _ = trial_three()
    assert a_transaction(timing).ok
    assert not a_transaction(timing, end_code=0xC052).ok


def test_transaction_records_are_frozen() -> None:
    timing, _ = trial_three()
    tx = a_transaction(timing)
    with pytest.raises(AttributeError):
        tx.generation = 7  # type: ignore[misc]  # frozen dataclass, deliberately illegal
    with pytest.raises(AttributeError):
        timing.sent_at = Nanos(0)  # type: ignore[misc]  # ditto


def test_wire_option_fields_keep_enum_identity() -> None:
    timing, _ = trial_three()
    tx = a_transaction(timing)
    assert tx.frame is THREE_E
    assert tx.frame.value == "3E"
    assert tx.encoding.name == "BINARY"
    assert tx.transport.value == "tcp"


# ---------------------------------------------------------------------------
# Purity
# ---------------------------------------------------------------------------


def test_timing_never_reaches_for_a_clock_of_its_own() -> None:
    source = Path(sys.modules["aslmp.timing"].__file__ or "").read_text(encoding="utf-8")
    body = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith(("#", '"'))
    )
    assert "import time" not in body
    assert "monotonic_ns()" not in body
