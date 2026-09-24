"""Layer 6 -- the control-loop cadence. A period, a tick, and an overrun you can see.

.. rubric:: Why there is no ``SKIP``

:class:`OverrunPolicy` has exactly three members and the missing fourth is the point
(DESIGN.md graft G18). A scheduler that quietly drops a cycle to catch up is doing the
same thing a client does when it returns the previous reading after a timeout: it is
substituting something plausible for the thing that did not happen, and it is doing it
where nobody looks. A control loop that misses a cycle has a problem; a control loop
that misses a cycle *and is not told* has two.

So a :class:`Cadence` never drops an index. Ticks are numbered from a fixed grid --
tick ``n`` is due at ``start + n * period`` -- and every index is delivered exactly
once, whatever the loop body does with its time. There is no burst either: one tick is
one pass through the caller's body, so a slow body does not queue up a backlog of empty
iterations. What a slow body produces instead is **lateness**, and lateness is reported
three ways: on every :class:`Tick`, in :attr:`Cadence.overruns`, and as the distribution
in :attr:`Cadence.latency`. A loop that is falling behind shows it as a lateness that
grows without bound, which is precisely what it is.

.. rubric:: Where the overrun is measured

Not after the sleep -- **at the moment the caller's body returns**. That is the only
place the number means what it says: it is the body's own doing, with no sleep
granularity mixed in. It matters on Windows, where the default timer resolution is
~15.6 ms and a naive "did the sleep return late?" test would call every cycle of a 10 ms
loop an overrun.

.. rubric:: What this pairs with

:attr:`Tick.previous_cycle_ns` is the host-side counterpart of
:attr:`aslmp.timing.TransactionTiming.host_gap_ns`: the cycle time this library can see
from outside a transaction, next to the gap the transaction records from inside one.
Together they answer "was the loop late because the PLC was slow, or because we were".
"""

from __future__ import annotations

import asyncio
import enum
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Final, Self, final

from aslmp._clock import DEFAULT_CLOCK
from aslmp.errors import Diagnostics, SlmpConfigurationError, SlmpError
from aslmp.observability import Percentiles
from aslmp.timing import Clock, Nanos

__all__ = [
    "Cadence",
    "OverrunPolicy",
    "Sleeper",
    "SlmpCadenceOverrunError",
    "Tick",
]

_NS_PER_S: Final = 1_000_000_000
_NS_PER_MS: Final = 1_000_000.0

Sleeper = Callable[[float], Awaitable[None]]
"""How a cadence waits. ``asyncio.sleep`` by default.

Injectable for the same reason ``clock`` is: a scheduling primitive whose only
observable behaviour is *when* it hands control back cannot be tested against a real
clock without making the test suite a timing experiment.
"""


class OverrunPolicy(enum.Enum):
    """What a :class:`Cadence` does when the loop body outran the period.

    ``RECORD`` (the default)
        Count it, stamp it on the :class:`Tick`, keep going. The loop keeps controlling
        the plant and the evidence is in :attr:`Cadence.overruns` and
        :attr:`Cadence.latency`.
    ``RAISE``
        Raise :class:`SlmpCadenceOverrunError` out of the ``async for``. For a loop
        whose whole contract is the period -- a fixed-rate sampler, a log that must not
        gap -- a missed deadline is a failure, not a statistic. It is **not** a watchdog
        and must not be used as one: a host that has stopped looks exactly like a host
        that is on time, from the CPU's side, and the deadline this policy enforces is
        checked here rather than there.
    ``STOP``
        End the iteration cleanly, the way any exhausted iterator does. The ``async
        for`` falls through to the code after it, which is where an orderly shutdown
        belongs.

    There is **no** ``SKIP``. Silently dropping a cycle to catch up is the scheduling
    equivalent of returning a stale value (DESIGN.md graft G18).
    """

    RECORD = "record"
    RAISE = "raise"
    STOP = "stop"


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class Tick:
    """One cycle of a :class:`Cadence`, with everything that is true about its timing.

    ``scheduled_at`` comes from the fixed grid and ``started_at`` from the clock, so
    ``lateness_ns`` is the loop's accumulated phase error rather than this cycle's
    excess. A loop that is 4 ms behind and staying there reports 4 ms every tick; a loop
    that is falling behind reports a number that climbs. ``previous_cycle_ns`` is the
    other half: how long the *previous* cycle's body ran, from that tick's start to the
    moment it handed control back, and ``None`` on the first tick because there was no
    previous body. It is not the start-to-start interval -- that is the grid, and the
    grid is already known.
    """

    index: int
    scheduled_at: Nanos
    started_at: Nanos
    lateness_ns: int
    overrun: bool
    previous_cycle_ns: int | None

    @property
    def lateness_ms(self) -> float:
        """``lateness_ns`` in milliseconds, for the one place humans read it."""
        return self.lateness_ns / _NS_PER_MS

    def __str__(self) -> str:
        cycle = (
            "cycle -"
            if self.previous_cycle_ns is None
            else f"cycle {self.previous_cycle_ns / _NS_PER_MS:.3f} ms"
        )
        flag = " OVERRUN" if self.overrun else ""
        return f"tick {self.index} late {self.lateness_ms:.3f} ms, {cycle}{flag}"


class SlmpCadenceOverrunError(SlmpError):
    """A cycle body outran the period under :attr:`OverrunPolicy.RAISE`.

    Carries the :class:`Tick` that overran and the period it overran, because "the loop
    is too slow" is not actionable and "tick 412 started 37.2 ms into a 10.0 ms period"
    is.

    .. note::

       This class belongs in the DESIGN.md section 3.1 tree beside the other
       :class:`~aslmp.errors.SlmpError` subclasses and should move to
       ``aslmp/errors/__init__.py`` when that module is next opened. It is defined here
       because the cadence is the only thing that can raise it and ``errors/`` was
       closed when this module was written.
    """

    def __init__(
        self,
        message: str,
        *,
        tick: Tick,
        period_ns: int,
        diagnostics: Diagnostics | None = None,
    ) -> None:
        super().__init__(message, diagnostics=diagnostics)
        self.tick = tick
        self.period_ns = period_ns


@final
class Cadence:
    """A fixed-period async iterator of :class:`Tick`, with the overruns on the record.

    ::

        cadence = Cadence(timedelta(milliseconds=100))
        async for tick in cadence:
            pv = await plc.read_f32("D2")
            ...
        print(cadence.overruns, cadence.latency)

    The grid is fixed from the first tick: tick ``n`` is due at ``start + n * period``.
    Nothing is dropped, nothing is bursted, and the arithmetic is in integer nanoseconds
    from one monotonic clock -- ``float`` seconds accumulate a drift of their own over a
    loop that runs for a week.
    """

    __slots__ = (
        "_clock",
        "_index",
        "_lateness",
        "_overruns",
        "_period_ns",
        "_policy",
        "_previous_started",
        "_sleep",
        "_start",
        "_stopped",
        "_ticks",
    )

    def __init__(
        self,
        period: timedelta,
        *,
        on_overrun: OverrunPolicy = OverrunPolicy.RECORD,
        clock: Clock = DEFAULT_CLOCK,
        window: int = 4096,
        sleep: Sleeper = asyncio.sleep,
    ) -> None:
        period_ns = round(period.total_seconds() * _NS_PER_S)
        if period_ns <= 0:
            raise SlmpConfigurationError(
                f"a cadence period must be positive; {period!r} is "
                f"{period_ns} ns. A zero or negative period is a busy loop with a "
                f"schedule drawn on it."
            )
        if window < 1:
            raise SlmpConfigurationError(
                f"the lateness window must hold at least one sample; got {window}."
            )
        self._period_ns = period_ns
        self._policy = on_overrun
        self._clock = clock
        self._sleep = sleep
        self._lateness: deque[int] = deque(maxlen=window)
        self._start: int | None = None
        self._previous_started: int | None = None
        self._index = 0
        self._ticks = 0
        self._overruns = 0
        self._stopped = False

    # -- inspection ----------------------------------------------------------

    @property
    def period(self) -> timedelta:
        """The configured period, as it was given."""
        return timedelta(microseconds=self._period_ns / 1000)

    @property
    def period_ns(self) -> int:
        """The period in integer nanoseconds -- the number the grid is built from."""
        return self._period_ns

    @property
    def policy(self) -> OverrunPolicy:
        """What this cadence does about an overrun. Never ``SKIP``; there is no ``SKIP``."""
        return self._policy

    @property
    def ticks(self) -> int:
        """Ticks delivered. A tick that raised or stopped the iteration is not one."""
        return self._ticks

    @property
    def overruns(self) -> int:
        """Cycles whose body returned after the next tick was already due."""
        return self._overruns

    @property
    def stopped(self) -> bool:
        """True once ``STOP`` ended the iteration or ``RAISE`` broke it."""
        return self._stopped

    @property
    def latency(self) -> Percentiles:
        """The distribution of tick lateness, in nanoseconds, over the window.

        Raises :class:`~aslmp.observability.NoSamplesError` before the first tick, for
        the same reason every other percentile in this library does: the p99 of nothing
        is not zero, it is undefined.
        """
        return Percentiles.of(self._lateness)

    def __repr__(self) -> str:
        return (
            f"Cadence(period_ns={self._period_ns}, on_overrun={self._policy.value}, "
            f"ticks={self._ticks}, overruns={self._overruns})"
        )

    # -- iteration -----------------------------------------------------------

    def __aiter__(self) -> Self:
        """The cadence is its own iterator: its counters outlive the ``async for``."""
        return self

    async def __anext__(self) -> Tick:
        if self._stopped:
            raise StopAsyncIteration
        clock = self._clock
        now = clock()
        if self._start is None:
            self._start = now
        scheduled = self._start + self._index * self._period_ns
        previous_cycle = (
            None if self._previous_started is None else now - self._previous_started
        )
        # Measured HERE -- at the instant the caller's body handed control back -- and
        # not after the sleep below, so the number is the body's doing and carries no
        # timer granularity. See the module docstring.
        if self._index > 0 and now > scheduled:
            self._note_overrun(now, scheduled, previous_cycle)
        delay = (scheduled - now) / _NS_PER_S
        if delay > 0:
            await self._sleep(delay)
        started = Nanos(clock())
        tick = Tick(
            index=self._index,
            scheduled_at=Nanos(scheduled),
            started_at=started,
            lateness_ns=started - scheduled,
            overrun=now > scheduled and self._index > 0,
            previous_cycle_ns=previous_cycle,
        )
        self._index += 1
        self._ticks += 1
        self._previous_started = started
        self._lateness.append(tick.lateness_ns)
        return tick

    def _note_overrun(self, now: int, scheduled: int, previous_cycle: int | None) -> None:
        """Count the overrun and apply the policy. ``RECORD`` returns; the others do not."""
        self._overruns += 1
        if self._policy is OverrunPolicy.RECORD:
            return
        self._stopped = True
        if self._policy is OverrunPolicy.STOP:
            raise StopAsyncIteration
        tick = Tick(
            index=self._index,
            scheduled_at=Nanos(scheduled),
            started_at=Nanos(now),
            lateness_ns=now - scheduled,
            overrun=True,
            previous_cycle_ns=previous_cycle,
        )
        raise SlmpCadenceOverrunError(
            f"cycle {self._index - 1} of this cadence returned {tick.lateness_ms:.3f} ms "
            f"after tick {self._index} was due, in a period of "
            f"{self._period_ns / _NS_PER_MS:.3f} ms. OverrunPolicy.RAISE says a missed "
            f"deadline is a failure rather than a statistic; OverrunPolicy.RECORD keeps "
            f"going and leaves the evidence in Cadence.overruns and Cadence.latency. "
            f"There is deliberately no policy that drops the cycle instead.",
            tick=tick,
            period_ns=self._period_ns,
        )
