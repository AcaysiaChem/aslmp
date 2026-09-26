"""Unit tests for ``aslmp.observability``.

Three properties carry weight here and each has a test that could fail:

* :class:`LatencyRecorder` allocates **nothing** after construction — proved with
  ``tracemalloc`` traces attributed to the module's own source lines, plus a live
  object count and buffer identity;
* percentiles are exact and follow one named method, checked at ``n = 1``, ``n = 2``,
  ``p0`` and ``p100``, and against a decimal (``99.9``) that binary floating point gets
  wrong;
* :mod:`logging` is named exactly once in the distribution, inside
  :func:`attach_logging`, and is not imported by ``import aslmp``.
"""

from __future__ import annotations

import gc
import logging
import subprocess
import sys
import tracemalloc
from array import array
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Final

import pytest

import aslmp.observability as obs
import aslmp.timing as timing_module
from aslmp.observability import (
    PERCENTILE_METHOD,
    Connected,
    ConnectionEvent,
    Counters,
    DatagramDropped,
    HistogramBucket,
    LatencyRecorder,
    MetricsSnapshot,
    NoSamplesError,
    Percentiles,
    ProbeSkipped,
    attach_logging,
    fanout,
    nearest_rank,
    percentile_ns,
)
from aslmp.timing import Nanos, Phase, TimingBuilder, Transaction, TransactionTiming

MS = 1_000_000
BASE = 1_000_000_000

# ---------------------------------------------------------------------------
# helpers (deliberately local: this file must not depend on another test module)
# ---------------------------------------------------------------------------


class FakeClock:
    """A monotonic nanosecond clock a test controls exactly."""

    def __init__(self, start: int = BASE) -> None:
        self.now = start

    def __call__(self) -> int:
        return self.now

    def advance(self, ns: int) -> int:
        self.now += ns
        return self.now


class WireLabel:
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


THREE_E = WireLabel("THREE_E", "3E")
BINARY = WireLabel("BINARY", "binary")
TCP = WireLabel("TCP", "tcp")


def a_timing(
    *,
    wire_ns: int,
    chunks: tuple[int, ...] = (11,),
    decode: bool = True,
    split: bool = False,
) -> TransactionTiming:
    """``split`` marks the first read short, which is what segmentation actually is.

    More than one chunk is NOT segmentation: the prefix-then-body pair is structural.
    """
    clock = FakeClock()
    builder = TimingBuilder(clock)
    builder.gate_acquired()
    builder.encoded()
    builder.sent()
    per_chunk = wire_ns // len(chunks)
    for index, nbytes in enumerate(chunks):
        step = wire_ns - per_chunk * (len(chunks) - 1) if index == 0 else per_chunk
        clock.advance(step)
        builder.chunk(nbytes, partial=split and index < len(chunks) - 1)
    if decode:
        builder.decoded()
    return builder.build()


def no_answer_timing() -> TransactionTiming:
    """Sent, and nothing came back. What a Remote Reset AND a timeout both look like.

    There is no field anywhere in :class:`~aslmp.timing.TransactionTiming` that tells
    the two apart, which is why :meth:`aslmp.observability.Counters.observe` has to ask
    the command registry instead of the record.
    """
    clock = FakeClock()
    builder = TimingBuilder(clock)
    builder.gate_acquired()
    builder.encoded()
    builder.sent()
    return builder.build()


def a_tx(
    timing: TransactionTiming, *, end_code: int = 0, command: int = 0x0401
) -> Transaction:
    return Transaction(
        timing=timing,
        sequence=1,
        connection_id="c1",
        generation=0,
        after_reconnect=False,
        command=command,
        subcommand=0x0000,
        frame=THREE_E,
        encoding=BINARY,
        transport=TCP,
        serial=None,
        request_bytes=21,
        response_bytes=13,
        end_code=end_code,
        prebuilt=False,
    )


# ---------------------------------------------------------------------------
# Percentiles — exact, and by a named method
# ---------------------------------------------------------------------------


def test_the_method_is_named_and_published() -> None:
    assert PERCENTILE_METHOD == "nearest-rank (ceiling), 1-indexed, no interpolation"
    assert Percentiles.METHOD == PERCENTILE_METHOD


def test_n_equals_one_gives_that_sample_for_every_percentile() -> None:
    p = Percentiles.of([7_338_000])
    for value in (0, 1, 50, 99, "99.9", 100):
        assert p.at(value) == 7_338_000
    assert p.minimum == p.maximum == p.p50 == 7_338_000
    assert p.mean_ns == 7_338_000.0


def test_n_equals_two_splits_at_fifty() -> None:
    p = Percentiles.of([200, 100])
    assert p.samples == (100, 200)
    assert p.at(0) == 100
    assert p.at(50) == 100
    assert p.at("50.0001") == 200
    assert p.at(100) == 200


def test_p0_is_the_minimum_and_p100_is_the_maximum_for_every_n() -> None:
    for n in range(1, 40):
        samples = [(i + 1) * 1000 for i in range(n)]
        p = Percentiles.of(reversed(samples))
        assert p.at(0) == p.minimum == 1000
        assert p.at(100) == p.maximum == n * 1000


def test_nearest_rank_matches_the_published_formula() -> None:
    # rank = max(1, ceil(p * n / 100)), 1-indexed.
    assert nearest_rank(0, 10) == 1
    assert nearest_rank(1, 10) == 1
    assert nearest_rank(10, 10) == 1
    assert nearest_rank(11, 10) == 2
    assert nearest_rank(50, 10) == 5
    assert nearest_rank(99, 10) == 10
    assert nearest_rank(100, 10) == 10
    assert nearest_rank(50, 1) == 1


def test_the_percentile_is_always_an_observed_sample_never_an_interpolation() -> None:
    samples = [1, 2, 3, 4]
    p = Percentiles.of(samples)
    for value in (0, 12.5, 25, 50, 62.5, 75, 99, 100):
        assert p.at(value) in samples


def test_a_decimal_percentile_uses_the_decimal_the_caller_wrote() -> None:
    # Binary 99.9 is 99.90000000000000568..., which at n = 1000 rounds up to rank 1000
    # and reports the maximum as the p99.9. Fraction(str(p)) gives the correct 999.
    assert nearest_rank(99.9, 1000) == 999
    assert nearest_rank("99.9", 1000) == 999
    samples = list(range(1, 1001))
    p = Percentiles.of(samples)
    assert p.p99_9 == 999
    assert p.maximum == 1000


def test_percentiles_over_an_empty_window_raise_rather_than_answer_zero() -> None:
    with pytest.raises(NoSamplesError):
        Percentiles.of([])
    with pytest.raises(NoSamplesError):
        nearest_rank(50, 0)


@pytest.mark.parametrize("bad", [-0.1, 100.1, 200])
def test_a_percentile_outside_zero_to_one_hundred_raises(bad: float) -> None:
    with pytest.raises(ValueError, match=r"\[0, 100\]"):
        nearest_rank(bad, 10)


def test_percentile_ns_is_the_free_function_form() -> None:
    ascending = (1, 2, 3, 4, 5)
    assert percentile_ns(ascending, 50) == 3
    assert percentile_ns(ascending, 100) == 5


def test_percentiles_render_milliseconds_for_humans() -> None:
    """The sample is the measured p50, so the rendering is checked against a real one.

    7.338 ms is the p50 of 500 sequential 2-word reads of D4 on FX5U-32MT/DS fw 1.065,
    2026-09-06, from the laptop at 192.168.10.41 over Wi-Fi at ~7 ms median RTT
    (``docs/hardware.md`` section 5). One sample, so every percentile is that value:
    this asserts formatting, not a distribution.
    """
    p = Percentiles.of([7_338_000])
    assert p.ms(50) == pytest.approx(7.338)
    assert "p50=7.338" in str(p)
    assert PERCENTILE_METHOD in str(p)


# ---------------------------------------------------------------------------
# The log-linear histogram
# ---------------------------------------------------------------------------


def test_every_bucket_contains_the_value_that_selected_it() -> None:
    values = list(range(0, 5000))
    values += [7_338_000, 12_986_000, 38_477_000, 1 << 40, (1 << 63) - 1]
    for value in values:
        index = obs._bucket_index(value)
        low, high = obs._bucket_bounds(index)
        assert low <= value <= high, (value, index, low, high)
        assert 0 <= index < obs._BUCKET_COUNT


def test_bucket_indices_are_monotonic_and_contiguous() -> None:
    previous = -1
    for value in range(0, 20000):
        index = obs._bucket_index(value)
        assert index >= previous
        previous = index


def test_relative_bucket_width_is_bounded() -> None:
    # 32 sub-buckets per octave: every bucket above the linear region is at most
    # 1/32 of its own lower bound wide.
    for value in (1000, 7_338_000, 38_477_000, 1 << 40):
        low, high = obs._bucket_bounds(obs._bucket_index(value))
        assert (high - low + 1) * 32 <= low * 2


def test_a_value_beyond_the_histogram_range_raises_and_is_never_clamped() -> None:
    with pytest.raises(ValueError, match="outside the histogram range"):
        obs._bucket_index(1 << 63)


def test_histogram_reports_only_non_empty_buckets_in_order() -> None:
    recorder = LatencyRecorder(capacity=16)
    for value in (1000, 1000, 7_338_000, 38_477_000):
        recorder.record_ns(value)
    buckets = recorder.histogram()
    assert all(isinstance(b, HistogramBucket) for b in buckets)
    assert [b.count for b in buckets] == [2, 1, 1]
    assert [b.low_ns for b in buckets] == sorted(b.low_ns for b in buckets)
    assert buckets[0].low_ns <= 1000 <= buckets[0].high_ns


# ---------------------------------------------------------------------------
# LatencyRecorder
# ---------------------------------------------------------------------------


def test_the_ring_keeps_the_most_recent_capacity_samples() -> None:
    recorder = LatencyRecorder(capacity=4)
    for value in (10, 20, 30, 40, 50):
        recorder.record_ns(value)
    assert recorder.samples() == (20, 30, 40, 50)
    assert recorder.window == 4
    assert recorder.capacity == 4
    assert recorder.recorded == 5
    # the all-time figures still remember the sample the ring dropped
    assert recorder.minimum_ns == 10
    assert recorder.maximum_ns == 50
    assert recorder.mean_ns == 30.0
    assert sum(b.count for b in recorder.histogram()) == 5


def test_a_negative_sample_raises_and_is_not_clamped() -> None:
    recorder = LatencyRecorder(capacity=4)
    with pytest.raises(ValueError, match="cannot be negative"):
        recorder.record_ns(-1)
    assert recorder.window == 0


def test_capacity_must_be_at_least_one() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        LatencyRecorder(capacity=0)


def test_an_incomplete_transaction_is_counted_never_invented() -> None:
    recorder = LatencyRecorder(capacity=8)
    recorder(a_tx(a_timing(wire_ns=7_338_000)))
    recorder(a_tx(a_timing(wire_ns=9_000_000, decode=False)))
    recorder(a_tx(a_timing(wire_ns=8_000_000), end_code=0xC052))
    assert recorder.observed == 3
    assert recorder.recorded == 2
    assert recorder.incomplete == 1
    assert recorder.failed == 1
    assert recorder.samples() == (7_338_000, 8_000_000)


def test_the_recorder_records_wire_ns_not_first_byte_ns() -> None:
    # Trial 3 on FX5U-32MT/DS fw 1.065: 1460 B then 471 B, true latency 14.010 ms.
    timing = a_timing(wire_ns=14_010_000, chunks=(1460, 471))
    assert timing.first_byte_ns != timing.wire_ns
    recorder = LatencyRecorder(capacity=4)
    recorder(a_tx(timing))
    assert recorder.samples() == (14_010_000,)
    assert recorder.percentiles().ms(50) == pytest.approx(14.010)


def test_phase_counts_follow_the_dominant_phase() -> None:
    recorder = LatencyRecorder(capacity=8)
    recorder(a_tx(a_timing(wire_ns=7_338_000)))
    counts = recorder.phase_counts()
    assert set(counts) == set(Phase)
    assert counts[Phase.ROUND_TRIP] == 1
    assert sum(counts.values()) == 1


def test_percentiles_and_extremes_raise_on_an_empty_recorder() -> None:
    recorder = LatencyRecorder(capacity=8)
    assert not recorder.has_samples
    for call in (recorder.percentiles, lambda: recorder.minimum_ns,
                 lambda: recorder.maximum_ns, lambda: recorder.mean_ns):
        with pytest.raises(NoSamplesError):
            call()


def test_reset_reuses_the_same_storage() -> None:
    recorder = LatencyRecorder(capacity=8)
    ring = recorder._ring
    buckets = recorder._buckets
    for value in (1, 2, 3):
        recorder.record_ns(value)
    recorder.reset()
    assert recorder._ring is ring
    assert recorder._buckets is buckets
    assert recorder.window == 0
    assert recorder.recorded == 0
    assert recorder.histogram() == ()
    with pytest.raises(NoSamplesError):
        _ = recorder.minimum_ns


def _traced_delta(
    work: Callable[[Any], None], rounds: int, items: Sequence[Any]
) -> tuple[int, int]:
    """Bytes and blocks still live after ``rounds`` passes, attributed to our modules."""
    gc.collect()
    tracemalloc.start()
    try:
        before = tracemalloc.take_snapshot()
        for _ in range(rounds):
            for item in items:
                work(item)
        after = tracemalloc.take_snapshot()
    finally:
        tracemalloc.stop()
    keep = [
        tracemalloc.Filter(True, obs.__file__),
        tracemalloc.Filter(True, timing_module.__file__),
    ]
    diff = after.filter_traces(keep).compare_to(before.filter_traces(keep), "lineno")
    return sum(d.size_diff for d in diff), sum(d.count_diff for d in diff)


_PARKED_BY_THE_INTERPRETER: Final = 8
"""Live blocks the interpreter may keep on our lines while we keep nothing.

Reading an ``array`` slot whose value is past the small-int cache makes a temporary
``int`` on every CPython, so a recording call allocates and frees a few objects each time
-- a transient peak of 200 bytes over 2 000 ``record_ns`` calls on 3.11.15, 3.12.13 and
3.13.14 alike (2026-09-25). "Allocates nothing" was never literally true; "retains
nothing" is, and that is what these tests assert.

3.11 to 3.13 return a freed temporary to the allocator and ``tracemalloc`` sees nothing
left. 3.14 keeps some for reuse, and a kept one still counts against the line that first
allocated it. Which lines, and when, is not deterministic -- one pass left 2 blocks and
twenty left 3 in the same process -- but the total is bounded by the lines, not by the
calls: the worst over 15 trials on 3.14.6 was 3 for ``record_ns`` and 4 for ``observe``,
and the same after 2 000 calls as after 200 000. This is twice that.

A recorder that kept one object per call would leave tens of thousands of blocks here,
and :func:`test_the_retention_bound_catches_a_kept_result` checks that it would.
"""


def test_the_recorder_owns_no_growable_container() -> None:
    """The structural half of the guarantee: there is nothing here that *can* grow."""
    recorder = LatencyRecorder(capacity=64)
    assert not hasattr(recorder, "__dict__")  # __slots__, so no attribute can appear
    for name in LatencyRecorder.__slots__:
        value = getattr(recorder, name)
        assert isinstance(value, array | int), (name, type(value))
    sizes = {
        name: len(getattr(recorder, name))
        for name in LatencyRecorder.__slots__
        if isinstance(getattr(recorder, name), array)
    }
    for i in range(1000):
        recorder.record_ns(i + 1)
    assert {
        name: len(getattr(recorder, name))
        for name in LatencyRecorder.__slots__
        if isinstance(getattr(recorder, name), array)
    } == sizes


def test_record_ns_retains_nothing_that_grows_with_use() -> None:
    recorder = LatencyRecorder(capacity=256)
    samples = [4_000_000 + (i % 997) * 1013 for i in range(2000)]
    for sample in samples:  # warm up: fill the ring, push every counter past the int cache
        recorder.record_ns(sample)
    ring, buckets, stats = recorder._ring, recorder._buckets, recorder._stats

    # 40 000 more recordings and nothing of ours kept. Every mutable scalar is a machine
    # integer in a preallocated array, so a ring entry, a histogram bucket or a running
    # sum has nowhere to be kept; what the interpreter parks is bounded by the lines and
    # not by the calls -- see _PARKED_BY_THE_INTERPRETER.
    _size, blocks = _traced_delta(recorder.record_ns, 20, samples)
    assert blocks <= _PARKED_BY_THE_INTERPRETER, f"{blocks} blocks live after 40 000 calls"

    # ... and the buffers are the same objects, still the same size.
    assert recorder._ring is ring
    assert recorder._buckets is buckets
    assert recorder._stats is stats
    assert len(recorder._ring) == 256
    assert recorder.window == 256
    assert recorder.recorded == 2000 * 21  # warm-up, then twenty passes


def test_observe_retains_nothing_that_grows_with_use() -> None:
    """The whole sink path, including ``wire_ns`` and ``dominant_phase`` in timing.py."""
    recorder = LatencyRecorder(capacity=64)
    transactions = [a_tx(a_timing(wire_ns=4_000_000 + i * 1013)) for i in range(200)]
    for tx in transactions:
        recorder.observe(tx)

    _size, blocks = _traced_delta(recorder.observe, 20, transactions)
    assert blocks <= _PARKED_BY_THE_INTERPRETER, f"{blocks} blocks live after 4 000 calls"
    assert recorder.window == 64
    assert recorder.recorded == 200 * 21  # warm-up, then twenty passes


def test_the_retention_bound_catches_a_kept_result() -> None:
    """The bound is only worth having if a real leak breaks it, so give it one.

    ``samples()`` builds a fresh tuple of 64 ints on a line in observability.py. Keep every
    one and the count must land two orders of magnitude past the bound, not near it.
    """
    recorder = LatencyRecorder(capacity=64)
    for i in range(200):
        recorder.record_ns(4_000_000 + i)
    kept: list[object] = []

    def keeps_what_it_is_given(sample: int) -> None:
        recorder.record_ns(sample)
        kept.append(recorder.samples())

    _size, blocks = _traced_delta(keeps_what_it_is_given, 20, range(4_000_000, 4_000_050))
    assert blocks > 100 * _PARKED_BY_THE_INTERPRETER, blocks


def test_the_ring_never_grows_however_many_samples_arrive() -> None:
    recorder = LatencyRecorder(capacity=32)
    for i in range(32 * 100):
        recorder.record_ns(i + 1)
    assert len(recorder._ring) == 32
    assert recorder.window == 32
    assert recorder.recorded == 3200
    assert recorder.samples() == tuple(range(3169, 3201))


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


def test_snapshot_is_a_frozen_view_of_everything() -> None:
    counters = Counters()
    recorder = LatencyRecorder(capacity=8)
    tx = a_tx(a_timing(wire_ns=7_338_000))
    counters.observe(tx)
    recorder(tx)
    snapshot = recorder.snapshot(
        at=Nanos(123), connection_id="c1", generation=2, counters=counters
    )
    assert isinstance(snapshot, MetricsSnapshot)
    assert snapshot.at == 123
    assert snapshot.generation == 2
    assert snapshot.window == 1
    assert snapshot.latency is not None
    assert snapshot.latency.p50 == 7_338_000
    counters.transactions_started += 100
    assert snapshot.counters.transactions_started == 1  # detached copy
    with pytest.raises(AttributeError):
        snapshot.generation = 3  # type: ignore[misc]  # frozen, deliberately illegal


def test_snapshot_latency_is_none_rather_than_a_zeroed_percentile() -> None:
    snapshot = LatencyRecorder(capacity=8).snapshot(at=Nanos(0))
    assert snapshot.latency is None
    assert snapshot.window == 0
    assert "no samples" in str(snapshot)


# ---------------------------------------------------------------------------
# Counters
# ---------------------------------------------------------------------------


def test_counters_start_at_zero_and_copy_detaches() -> None:
    counters = Counters()
    assert all(v == 0 for v in counters.as_mapping().values())
    counters.probes_skipped += 1
    frozen = counters.copy()
    counters.probes_skipped += 1
    assert frozen.probes_skipped == 1
    assert counters.probes_skipped == 2


def test_a_two_chunk_response_that_never_read_short_is_not_segmented() -> None:
    """Regression: the counter fired on 100% of TCP transactions.

    Every stream response is read as the fixed prefix and then exactly ``L`` more
    units, so two chunks is the floor, not evidence. A 960-word read against
    FX5U-32MT/DS fw 1.065 on 2026-09-07 arrived as 9 + 1922 five times out of six --
    one MSS split, five whole -- and the old rule counted all six.
    """
    counters = Counters()
    counters.observe(a_tx(a_timing(wire_ns=9_000_000, chunks=(9, 1922))))
    assert counters.chunks_received == 2
    assert counters.segmented_responses == 0

    counters.observe(a_tx(a_timing(wire_ns=11_000_000, chunks=(9, 1451, 471), split=True)))
    assert counters.segmented_responses == 1


def test_counters_observe_folds_a_segmented_transaction() -> None:
    counters = Counters()
    counters.observe(a_tx(a_timing(wire_ns=14_010_000, chunks=(1460, 471), split=True)))
    counters.observe(a_tx(a_timing(wire_ns=10_169_000, chunks=(1931,))))
    counters.observe(a_tx(a_timing(wire_ns=1 * MS, decode=False), end_code=0))
    assert counters.transactions_started == 3
    assert counters.transactions_completed == 2
    assert counters.transactions_failed == 1
    assert counters.chunks_received == 4
    assert counters.segmented_responses == 1
    assert counters.end_code_errors == 0
    counters.observe(a_tx(a_timing(wire_ns=1 * MS), end_code=0xC052))
    assert counters.end_code_errors == 1


def test_an_answerless_exchange_is_not_a_failure() -> None:
    """Regression, and it is the accounting that lied rather than the wire.

    ``0x1006`` Remote Reset is the one command whose ABSENCE of a reply is the expected
    outcome: SH(NA)-080956ENG-M p.136 says the response "is not be sent back to the
    external device". ``Counters.observe`` decided on ``tx.timing.is_complete``, which
    means ``decoded_at`` was stamped, and a silent exchange never stamps it -- so a call
    that returned normally moved ``transactions_started`` and ``transactions_failed``
    together and left ``transactions_completed`` behind. Measured across one such call:
    started 4 -> 5, completed 4 -> 4, failed 0 -> 1.
    """
    counters = Counters()
    counters.observe(a_tx(no_answer_timing(), command=0x1006))
    assert counters.transactions_started == 1
    assert counters.transactions_answerless == 1
    assert counters.transactions_failed == 0
    assert counters.transactions_completed == 0


def test_the_same_empty_record_on_any_other_command_is_still_a_failure() -> None:
    """The two are byte-identical as records. Only the command tells them apart.

    This is the half that matters: a timeout produces exactly the timing the test above
    produces, and a third bucket that swallowed it would be the silent recovery this
    library is written against.
    """
    counters = Counters()
    counters.observe(a_tx(no_answer_timing(), command=0x0401))
    assert counters.transactions_failed == 1
    assert counters.transactions_answerless == 0


def test_an_answer_to_an_answerless_command_is_a_failure() -> None:
    """A CPU that answers Remote Reset has not reset.

    ``exchange_without_response`` refuses the bytes at the transport
    (``_NothingExpected.feed``); the tally has to agree with it rather than pattern-match
    the command code and call it a success.
    """
    counters = Counters()
    counters.observe(a_tx(a_timing(wire_ns=1 * MS, decode=False), command=0x1006))
    assert counters.transactions_failed == 1
    assert counters.transactions_answerless == 0


def test_the_three_outcomes_partition_every_started_transaction() -> None:
    """``started == completed + failed + answerless``, always. No fourth state."""
    counters = Counters()
    counters.observe(a_tx(a_timing(wire_ns=7 * MS)))
    counters.observe(a_tx(a_timing(wire_ns=7 * MS, decode=False), end_code=0xC059))
    counters.observe(a_tx(no_answer_timing(), command=0x1006))
    counters.observe(a_tx(no_answer_timing(), command=0x0401))
    assert counters.transactions_started == 4
    assert (
        counters.transactions_completed
        + counters.transactions_failed
        + counters.transactions_answerless
        == counters.transactions_started
    )
    assert counters.transactions_answerless == 1
    assert counters.transactions_failed == 2


def test_the_answerless_set_is_read_off_the_command_registry() -> None:
    """Not a literal here. A command that declares ``response_optional`` joins it.

    ``0x1006`` is the only member today, and the point of deriving it is that the day a
    second command declares the property, the accounting follows without an edit.
    """
    from aslmp.commands.registry import COMMANDS

    codes = obs.answerless_commands()
    assert codes == {0x1006}
    for code in codes:
        spec = COMMANDS[code]
        assert spec.commands
        assert all(command.response_optional for command in spec.commands)


def test_asking_for_the_answerless_set_costs_one_import() -> None:
    """It is reached from a hot path, and ``import aslmp.observability`` must stay cheap.

    Two properties in one process: importing this module pulls in neither the registry
    nor a socket, and the set is cached rather than re-derived per transaction.
    """
    code = (
        "import sys, aslmp.observability as o; "
        "before = 'aslmp.commands.registry' in sys.modules; "
        "first = o.answerless_commands(); "
        "print(before, first is o.answerless_commands(), "
        "any(m in sys.modules for m in ('socket', 'asyncio')))"
    )
    result = subprocess.run(  # our own interpreter, fixed argv
        [sys.executable, "-I", "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.split() == ["False", "True", "False"], result.stdout


def test_the_counters_the_design_names_all_exist() -> None:
    names = set(Counters().as_mapping())
    for required in (
        "sink_errors",
        "probes_skipped",
        "stale_datagrams",
        "socket_rebinds",
        "entry_busy",
        "concurrent_rejections",
        "queue_waits",
        "segmented_responses",
        "outcome_unknown",
    ):
        assert required in names


# ---------------------------------------------------------------------------
# Events and fanout
# ---------------------------------------------------------------------------


def test_events_carry_the_connection_identity_and_render_readably() -> None:
    event = Connected(
        connection_id="c1",
        generation=3,
        at=Nanos(10),
        peer=("192.168.10.250", 5000),
        local=("192.168.10.10", 54321),
        handshake_ns=7_340_000,
        model="FX5U-32MT/DS",
        model_code=0x4A49,
    )
    assert event.kind == "Connected"
    assert isinstance(event, ConnectionEvent)
    rendered = str(event)
    assert rendered.startswith("Connected[c1 gen=3]")
    assert "FX5U-32MT/DS" in rendered
    assert event.as_mapping()["model_code"] == 0x4A49


def test_events_are_frozen() -> None:
    event = ProbeSkipped(connection_id="c1", generation=0, at=Nanos(1), reason="gate-held")
    with pytest.raises(AttributeError):
        event.reason = "other"  # type: ignore[misc]  # frozen, deliberately illegal


def test_fanout_calls_every_sink_even_when_one_raises() -> None:
    seen: list[str] = []

    def good_a(event: ConnectionEvent) -> None:
        seen.append("a")

    def bad(event: ConnectionEvent) -> None:
        raise RuntimeError("sink exploded")

    def good_b(event: ConnectionEvent) -> None:
        seen.append("b")

    sink = fanout(good_a, bad, good_b)
    event = ProbeSkipped(connection_id="c1", generation=0, at=Nanos(1), reason="gate-held")
    with pytest.raises(ExceptionGroup) as excinfo:
        sink(event)
    assert seen == ["a", "b"]
    assert len(excinfo.value.exceptions) == 1
    assert isinstance(excinfo.value.exceptions[0], RuntimeError)


def test_fanout_is_silent_when_every_sink_is_happy() -> None:
    seen: list[ConnectionEvent] = []
    sink = fanout(seen.append, seen.append)
    event = DatagramDropped(
        connection_id="c1", generation=1, at=Nanos(1), reason="stale-epoch", nbytes=20
    )
    sink(event)
    assert seen == [event, event]


def test_fanout_of_nothing_is_a_no_op() -> None:
    fanout()(ProbeSkipped(connection_id="c", generation=0, at=Nanos(0), reason="x"))


# ---------------------------------------------------------------------------
# attach_logging — the ONE place logging is named
# ---------------------------------------------------------------------------


def test_importing_aslmp_does_not_import_logging() -> None:
    code = (
        "import sys, aslmp, aslmp.timing, aslmp.observability;"
        "print([m for m in ('logging','socket','ssl','asyncio','selectors','threading')"
        " if m in sys.modules])"
    )
    result = subprocess.run(  # our own interpreter, fixed argv
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "[]"


def test_logging_is_named_only_inside_attach_logging() -> None:
    package = Path(obs.__file__).parent
    offenders = []
    for path in package.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), 1):
            if "import logging" in line and path != Path(obs.__file__):
                offenders.append(f"{path}:{number}")
    assert offenders == []
    source = Path(obs.__file__).read_text(encoding="utf-8")
    assert source.count("import logging") == 1
    body = source[source.index("def attach_logging(") :]
    assert "import logging" in body  # the single occurrence is inside the function


def test_attach_logging_registers_a_sink_and_logs_events(
    caplog: pytest.LogCaptureFixture,
) -> None:
    registered: list[object] = []
    sink = attach_logging(registered.append, logger_name="aslmp.test", level=logging.INFO)
    assert registered == [sink]
    with caplog.at_level(logging.INFO, logger="aslmp.test"):
        sink(
            ProbeSkipped(
                connection_id="c1", generation=0, at=Nanos(1), reason="gate-held"
            )
        )
    assert "ProbeSkipped[c1 gen=0] reason='gate-held'" in caplog.text


def test_attach_logging_default_level_is_the_numeric_value_of_info() -> None:
    import inspect

    default = inspect.signature(attach_logging).parameters["level"].default
    assert default == logging.INFO
