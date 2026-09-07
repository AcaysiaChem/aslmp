"""The cadence: a fixed grid, an overrun you can see, and no way to drop a cycle.

Every scheduling test here runs on an **injected clock and an injected sleep**, so what
is asserted is the scheduler's arithmetic rather than the host's timer resolution. That
matters more than usual on this repo's own bench machine: Windows' default timer
granularity is ~15.6 ms, so a 10 ms cadence tested against ``time.monotonic_ns`` and a
real ``asyncio.sleep`` would report an overrun on every single cycle and the test would
be measuring the operating system.

Two things are asserted that a docstring cannot hold:

* :class:`~aslmp.loop.OverrunPolicy` has exactly three members and none of them is
  ``SKIP`` (DESIGN.md graft G18) -- an absence is the one decision a later contributor
  can undo without noticing;
* **no index is ever dropped**. A body that takes three periods still yields tick 1,
  then 2, then 3; what grows is the lateness, which is the honest report of a loop that
  is falling behind.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from aslmp.errors import SlmpConfigurationError
from aslmp.loop import Cadence, OverrunPolicy, SlmpCadenceOverrunError, Tick
from aslmp.observability import NoSamplesError
from aslmp.timing import Nanos

MS = 1_000_000


class FakeClock:
    """A monotonic clock the test moves by hand. Nanoseconds, like the real one."""

    def __init__(self, start: int = 1_000_000_000) -> None:
        self.now = start

    def __call__(self) -> int:
        return self.now

    def advance(self, ns: int) -> None:
        self.now += ns


class RecordingSleep:
    """An ``asyncio.sleep`` that advances the fake clock instead of the wall clock."""

    def __init__(self, clock: FakeClock) -> None:
        self._clock = clock
        self.calls: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.calls.append(delay)
        self._clock.advance(round(delay * 1_000_000_000))


def a_cadence(
    clock: FakeClock,
    sleep: RecordingSleep,
    *,
    period_ms: int = 10,
    on_overrun: OverrunPolicy = OverrunPolicy.RECORD,
    window: int = 4096,
) -> Cadence:
    return Cadence(
        timedelta(milliseconds=period_ms),
        on_overrun=on_overrun,
        clock=clock,
        window=window,
        sleep=sleep,
    )


# ========================================================================================
# The missing member
# ========================================================================================


def test_overrun_policy_has_exactly_three_members_and_none_is_skip() -> None:
    """Graft G18. Dropping a cycle to catch up is a stale value with a schedule on it."""
    assert [member.name for member in OverrunPolicy] == ["RECORD", "RAISE", "STOP"]
    assert not hasattr(OverrunPolicy, "SKIP")
    assert "SKIP" not in {member.value.upper() for member in OverrunPolicy}


def test_the_cadence_exposes_no_way_to_drop_a_cycle() -> None:
    """No ``skip``, no ``catch_up``, no ``reset`` that would quietly re-base the grid."""
    forbidden = {"skip", "catch_up", "catchup", "drop", "reset", "rebase"}
    assert forbidden.isdisjoint(dir(Cadence))


# ========================================================================================
# Construction
# ========================================================================================


@pytest.mark.parametrize("period", [timedelta(0), timedelta(milliseconds=-1)])
def test_a_non_positive_period_is_refused(period: timedelta) -> None:
    with pytest.raises(SlmpConfigurationError, match="must be positive"):
        Cadence(period)


def test_a_window_smaller_than_one_sample_is_refused() -> None:
    with pytest.raises(SlmpConfigurationError, match="at least one sample"):
        Cadence(timedelta(milliseconds=10), window=0)


def test_the_period_survives_the_round_trip_to_nanoseconds() -> None:
    cadence = Cadence(timedelta(milliseconds=2.5))
    assert cadence.period_ns == 2_500_000
    assert cadence.period == timedelta(milliseconds=2.5)
    assert cadence.policy is OverrunPolicy.RECORD


# ========================================================================================
# The grid
# ========================================================================================


async def test_a_punctual_loop_has_no_overruns_and_zero_lateness() -> None:
    clock = FakeClock()
    sleep = RecordingSleep(clock)
    cadence = a_cadence(clock, sleep)
    ticks: list[Tick] = []
    async for tick in cadence:
        ticks.append(tick)
        clock.advance(10 * MS)  # the body takes exactly one period
        if len(ticks) == 5:
            break
    assert [tick.index for tick in ticks] == [0, 1, 2, 3, 4]
    assert all(tick.lateness_ns == 0 for tick in ticks)
    assert cadence.overruns == 0
    assert cadence.ticks == 5
    assert sleep.calls == []  # exactly on time is not early


async def test_a_fast_loop_sleeps_the_remainder_of_the_period() -> None:
    clock = FakeClock()
    sleep = RecordingSleep(clock)
    cadence = a_cadence(clock, sleep)
    count = 0
    async for _tick in cadence:
        count += 1
        clock.advance(3 * MS)  # the body takes 3 ms of a 10 ms period
        if count == 3:
            break
    assert sleep.calls == pytest.approx([0.007, 0.007])
    assert cadence.overruns == 0
    assert cadence.latency.maximum == 0


async def test_the_first_tick_reports_no_previous_cycle_and_later_ones_do() -> None:
    clock = FakeClock()
    sleep = RecordingSleep(clock)
    cadence = a_cadence(clock, sleep)
    ticks: list[Tick] = []
    async for tick in cadence:
        ticks.append(tick)
        clock.advance(4 * MS)
        if len(ticks) == 3:
            break
    assert ticks[0].previous_cycle_ns is None
    # The body's own time, tick start to the moment it handed control back -- not the
    # start-to-start period, which is the grid and is known already.
    assert ticks[1].previous_cycle_ns == 4 * MS
    assert ticks[2].previous_cycle_ns == 4 * MS


# ========================================================================================
# Overruns
# ========================================================================================


async def test_a_slow_body_drops_no_index_and_reports_growing_lateness() -> None:
    """The whole of graft G18 in one assertion: indices 0..4, lateness climbing."""
    clock = FakeClock()
    sleep = RecordingSleep(clock)
    cadence = a_cadence(clock, sleep)
    ticks: list[Tick] = []
    async for tick in cadence:
        ticks.append(tick)
        clock.advance(25 * MS)  # 2.5x the period, every cycle
        if len(ticks) == 5:
            break
    assert [tick.index for tick in ticks] == [0, 1, 2, 3, 4]
    # 15 ms further behind every cycle: a 25 ms body in a 10 ms period, reported as the
    # unbounded phase error it is rather than hidden by re-basing the grid.
    assert [tick.lateness_ns for tick in ticks] == [0, 15 * MS, 30 * MS, 45 * MS, 60 * MS]
    assert [tick.overrun for tick in ticks] == [False, True, True, True, True]
    assert cadence.overruns == 4
    assert cadence.ticks == 5
    assert sleep.calls == []


async def test_record_keeps_going_and_leaves_the_evidence_in_the_percentiles() -> None:
    clock = FakeClock()
    sleep = RecordingSleep(clock)
    cadence = a_cadence(clock, sleep)
    count = 0
    async for _tick in cadence:
        count += 1
        clock.advance(12 * MS)
        if count == 4:
            break
    assert cadence.latency.count == 4
    assert cadence.latency.minimum == 0
    assert cadence.latency.maximum == 6 * MS
    assert cadence.stopped is False


async def test_raise_stops_the_iteration_with_the_tick_that_overran() -> None:
    clock = FakeClock()
    sleep = RecordingSleep(clock)
    cadence = a_cadence(clock, sleep, on_overrun=OverrunPolicy.RAISE)
    seen = 0
    with pytest.raises(SlmpCadenceOverrunError) as caught:
        async for _tick in cadence:
            seen += 1
            clock.advance(31 * MS)
    assert seen == 1
    assert caught.value.period_ns == 10 * MS
    assert caught.value.tick.index == 1
    assert caught.value.tick.lateness_ns == 21 * MS
    assert caught.value.tick.overrun is True
    assert "no policy that drops the cycle" in str(caught.value)
    assert cadence.overruns == 1
    assert cadence.ticks == 1  # the tick that raised was never delivered
    assert cadence.stopped is True


async def test_stop_ends_the_loop_cleanly_and_the_code_after_it_runs() -> None:
    clock = FakeClock()
    sleep = RecordingSleep(clock)
    cadence = a_cadence(clock, sleep, on_overrun=OverrunPolicy.STOP)
    delivered = 0
    async for _tick in cadence:
        delivered += 1
        clock.advance(11 * MS if delivered == 3 else 5 * MS)
    assert delivered == 3
    assert cadence.overruns == 1
    assert cadence.ticks == 3
    assert cadence.stopped is True


async def test_a_stopped_cadence_stays_stopped() -> None:
    clock = FakeClock()
    sleep = RecordingSleep(clock)
    cadence = a_cadence(clock, sleep, on_overrun=OverrunPolicy.STOP)
    async for _tick in cadence:
        clock.advance(40 * MS)
    with pytest.raises(StopAsyncIteration):
        await cadence.__anext__()


# ========================================================================================
# Reporting
# ========================================================================================


def test_the_latency_of_nothing_raises_rather_than_answering_zero() -> None:
    """The same rule the transaction histogram follows: there is no p99 of nothing."""
    cadence = Cadence(timedelta(milliseconds=10))
    with pytest.raises(NoSamplesError):
        _ = cadence.latency


async def test_the_lateness_window_is_bounded_and_keeps_the_most_recent() -> None:
    clock = FakeClock()
    sleep = RecordingSleep(clock)
    cadence = a_cadence(clock, sleep, window=3)
    count = 0
    async for _tick in cadence:
        count += 1
        clock.advance(11 * MS)
        if count == 6:
            break
    assert cadence.ticks == 6
    assert cadence.latency.count == 3
    assert cadence.latency.maximum == 5 * MS  # ticks 3, 4 and 5 -- not the early ones


async def test_the_cadence_is_its_own_iterator_so_counters_outlive_the_loop() -> None:
    clock = FakeClock()
    sleep = RecordingSleep(clock)
    cadence = a_cadence(clock, sleep)
    assert cadence.__aiter__() is cadence
    count = 0
    async for _tick in cadence:
        count += 1
        clock.advance(10 * MS)
        if count == 2:
            break
    async for _tick in cadence:  # resumed, not restarted
        count += 1
        clock.advance(10 * MS)
        if count == 4:
            break
    assert cadence.ticks == 4
    assert cadence.overruns == 0


def test_a_tick_renders_its_own_timing() -> None:
    tick = Tick(
        index=7,
        scheduled_at=Nanos(1_000),
        started_at=Nanos(2_500_000),
        lateness_ns=2_499_000,
        overrun=True,
        previous_cycle_ns=12_000_000,
    )
    rendered = str(tick)
    assert "tick 7" in rendered
    assert "2.499 ms" in rendered
    assert "cycle 12.000 ms" in rendered
    assert "OVERRUN" in rendered
    assert tick.lateness_ms == pytest.approx(2.499)


# ========================================================================================
# One run against the real clock, to prove the injection did not hide a bug
# ========================================================================================


async def test_a_real_cadence_ticks_against_the_real_clock() -> None:
    """Short, generous and free of a timing assertion: it proves the plumbing only."""
    cadence = Cadence(timedelta(milliseconds=1))
    started = asyncio.get_running_loop().time()
    ticks: list[Tick] = []
    async for tick in cadence:
        ticks.append(tick)
        if len(ticks) == 3:
            break
    assert [tick.index for tick in ticks] == [0, 1, 2]
    assert asyncio.get_running_loop().time() - started < 5.0
    assert cadence.latency.count == 3
