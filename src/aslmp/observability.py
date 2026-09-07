"""Layer 2.5 — observability as typed data. Pure; no I/O, no clock of its own.

Graft G10 of the locked architecture (DESIGN.md §1.8, §2.5): **no ``logging`` calls
anywhere in the library**; typed events plus a transaction sink. A log call inside the
transport lands in the latency number it describes, and a library that logs decides for
the application what its telemetry looks like. :func:`attach_logging` is the single
place in the distribution that names :mod:`logging`, it is opt-in, and it imports the
module inside the function body so that ``import aslmp`` never pulls it in.

What lives here:

* :class:`ConnectionEvent` and its subclasses — what the connection did, as records.
* :class:`EventSink` / :func:`fanout` — where those records go.
* :class:`Counters` — monotonic tallies owned by the connection.
* :class:`LatencyRecorder` — a ready-made ``on_transaction`` sink with a fixed ring and
  a log-linear histogram. **It allocates nothing after construction**: it sits in a
  control loop, and a sink that grows a list is a sink that eventually pauses the loop
  it is measuring.
* :class:`Percentiles` — exact, from the ring, by a **named** method
  (:data:`PERCENTILE_METHOD`). We publish these numbers; a Mitsubishi engineer must be
  able to recompute them from the same samples and get the same answer.
* :class:`MetricsSnapshot` — one frozen view of all of the above.

Latency context, all measured on **MELSEC iQ-F FX5U-32MT/DS firmware 1.065,
2026-09-06**: 500 sequential 2-word reads of D4 gave min 4.568 / p50 7.338 / p90 9.710 /
p99 12.986 / max 38.477 ms with ``TCP_NODELAY`` on. The same machine and PLC produced
p50 7.1 / p99 18.8 ms on one day and p50 10.3 / p99 95.2 ms on another — which is why
this module reports a window, a count and a method rather than a single number, and why
every published table must carry a same-session raw-socket control.
"""

from __future__ import annotations

import math
from array import array
from dataclasses import dataclass, fields
from fractions import Fraction
from typing import TYPE_CHECKING, ClassVar, Final, Protocol, final

from aslmp.timing import Nanos, Phase, Transaction

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

__all__ = [
    "PERCENTILE_METHOD",
    "ConnectFailed",
    "Connected",
    "Connecting",
    "ConnectionEvent",
    "ConnectionFailed",
    "Counters",
    "DatagramDropped",
    "Disconnected",
    "EventSink",
    "HandshakeFailed",
    "HistogramBucket",
    "LatencyRecorder",
    "MetricsSnapshot",
    "NoSamplesError",
    "Percentiles",
    "ProbeSkipped",
    "Reconnected",
    "Reconnecting",
    "SinkFailed",
    "SocketRebound",
    "TargetChanged",
    "attach_logging",
    "fanout",
    "nearest_rank",
    "percentile_ns",
]


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class ConnectionEvent:
    """Base of the connection event hierarchy. Never emitted on its own.

    ``generation`` bumps on every reconnect and every UDP source-port rebind, so an
    event stream can always be cut at the point where the peer stopped being the same
    socket. ``at`` comes from the connection's injected monotonic clock.
    """

    connection_id: str
    generation: int
    at: Nanos

    @property
    def kind(self) -> str:
        """The event class name — the stable string key for an exporter."""
        return type(self).__name__

    def as_mapping(self) -> Mapping[str, object]:
        """Every field, for a structured exporter that is not Python."""
        return {f.name: getattr(self, f.name) for f in fields(self)}

    def __str__(self) -> str:
        body = " ".join(
            f"{f.name}={getattr(self, f.name)!r}"
            for f in fields(self)
            if f.name not in ("connection_id", "generation", "at")
        )
        head = f"{self.kind}[{self.connection_id} gen={self.generation}]"
        return f"{head} {body}" if body else head


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class Connecting(ConnectionEvent):
    """A socket open was started. Says nothing about whether it will work."""

    peer: tuple[str, int]
    transport: str
    attempt: int = 1


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class Connected(ConnectionEvent):
    """The handshake proved the connection (DESIGN.md §4.5 step 5).

    ``local`` is carried because a silent socket rebuild would be visible in the local
    port and nowhere else. ``handshake_ns`` is the ``0619`` Self Test round trip — the
    connect-time latency baseline, ~7 ms on FX5U-32MT/DS fw 1.065.
    """

    peer: tuple[str, int]
    local: tuple[str, int]
    handshake_ns: int | None = None
    model: str | None = None
    model_code: int | None = None


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class ConnectFailed(ConnectionEvent):
    """The socket never opened, or opened onto a connection entry already in use.

    On FX5U-32MT/DS fw 1.065 the CPU accepts the TCP connection and *then* sends FIN
    when the single connection entry is busy, so ``error_type`` is the only thing that
    distinguishes this from a refused connect.
    """

    peer: tuple[str, int]
    reason: str
    error_type: str


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class HandshakeFailed(ConnectionEvent):
    """The ``0619`` echo did not verify, or the ``0101`` model code was not ours."""

    peer: tuple[str, int]
    reason: str
    error_type: str


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class Disconnected(ConnectionEvent):
    """The connection closed. ``expected`` is False for a peer-initiated close."""

    reason: str
    expected: bool


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class ConnectionFailed(ConnectionEvent):
    """The connection went sticky ``FAILED`` and the socket was closed.

    There is no automatic recovery in this package; a ``Supervisor`` is the only thing
    that reconnects, and only if the caller built one.
    """

    reason: str
    error_type: str


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class Reconnecting(ConnectionEvent):
    """A supervisor is about to reconnect, after ``delay_s`` of its backoff policy."""

    attempt: int
    delay_s: float
    reason: str


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class Reconnected(ConnectionEvent):
    """A supervisor re-established and re-proved the connection. ``generation`` bumped."""

    previous_generation: int


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class TargetChanged(ConnectionEvent):
    """The ``0101`` model code changed across a reconnect — a different CPU answered.

    A prebuilt ``0403`` frame carried into a different D-memory layout returns plausible
    floats, which is why this is an event *and* an exception.
    """

    previous_model_code: int | None
    model_code: int | None


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class DatagramDropped(ConnectionEvent):
    """A UDP datagram was discarded before it could become somebody's answer.

    ``reason`` is one of ``"stale-epoch"``, ``"foreign-source"``, ``"short"``,
    ``"trailing"``. Dropped and counted, never delivered: a late reply from the correct
    peer becoming the next transaction's answer is the exact bug this prevents.
    """

    reason: str
    nbytes: int
    source: tuple[str, int] | None = None


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class SocketRebound(ConnectionEvent):
    """A UDP socket was rebound to a fresh source port after a timeout."""

    previous_local: tuple[str, int]
    local: tuple[str, int]
    reason: str


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class ProbeSkipped(ConnectionEvent):
    """A health probe found the one-in-flight gate held and stood down.

    Skipped and counted, never queued and never raised (DESIGN.md G5): probing a
    connection with one in-flight slot would otherwise contend with the very thing it
    is watching.
    """

    reason: str


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class SinkFailed(ConnectionEvent):
    """A user callback raised. Counted here; re-raised at most once per generation."""

    sink: str
    error_type: str
    message: str


class EventSink(Protocol):
    """Anything that accepts connection events. ``print`` does not; a function does."""

    def __call__(self, event: ConnectionEvent, /) -> None: ...


@final
class _Fanout:
    """See :func:`fanout`."""

    __slots__ = ("_sinks",)

    def __init__(self, sinks: tuple[EventSink, ...]) -> None:
        self._sinks = sinks

    @property
    def sinks(self) -> tuple[EventSink, ...]:
        return self._sinks

    def __call__(self, event: ConnectionEvent, /) -> None:
        failures: list[Exception] = []
        for sink in self._sinks:
            try:
                sink(event)
            except Exception as exc:
                # Collected, never swallowed: every failure is re-raised below.
                failures.append(exc)
        if failures:
            raise ExceptionGroup(
                f"{len(failures)} of {len(self._sinks)} event sinks raised on {event.kind}",
                failures,
            )


def fanout(*sinks: EventSink) -> EventSink:
    """Combine event sinks into one.

    **Every** sink is called, even if an earlier one raises, and **every** failure is
    reported: the failures are collected and raised together as an ``ExceptionGroup``.
    Nothing is swallowed and no sink is skipped because its neighbour misbehaved —
    silently dropping the rest is precisely the silent recovery this library forbids.
    """
    return _Fanout(sinks)


def attach_logging(
    register: Callable[[EventSink], None],
    /,
    *,
    logger_name: str = "aslmp",
    level: int = 20,
) -> EventSink:
    """Opt-in bridge from connection events to :mod:`logging`. Returns the sink.

    This is the **only** place in the distribution that names :mod:`logging`, and the
    import happens inside the function body, so ``import aslmp`` never pulls the
    logging machinery in and no library code path can log. A log call inside the
    transport lands in the latency number it describes.

    ``register`` is the listener registrar — ``attach_logging(plc.add_event_listener)``.
    Taking a callable rather than a client keeps this module at Layer 2.5.

    ``level`` defaults to ``20``, the numeric value of ``logging.INFO``, spelled as a
    literal so the default costs no import.
    """
    import logging

    log = logging.getLogger(logger_name)

    def sink(event: ConnectionEvent, /) -> None:
        log.log(level, "%s", event)

    register(sink)
    return sink


# ---------------------------------------------------------------------------
# Counters
# ---------------------------------------------------------------------------


@final
@dataclass(slots=True)
class Counters:
    """Monotonic tallies owned by one connection. Mutable by design; copy to snapshot.

    Each field exists because something in the design or on the bench needs to be
    countable rather than logged. Nothing here is ever reset by the library.
    """

    # transactions
    transactions_started: int = 0
    transactions_completed: int = 0
    transactions_failed: int = 0
    end_code_errors: int = 0
    outcome_unknown: int = 0
    """State-changing requests that failed *after* the bytes went out."""

    # bytes and segmentation
    bytes_sent: int = 0
    bytes_received: int = 0
    chunks_received: int = 0
    segmented_responses: int = 0
    """Responses where a length-driven read came back SHORT of what it asked for.

    Not "more than one chunk": a stream response is read as the fixed prefix and then
    exactly ``L`` more units, so it always takes at least two ``recv`` calls and
    counting chunks would report 100% of TCP transactions. Measured on FX5U-32MT/DS fw
    1.065: 1 of 3 identical 1931-byte reads split at the 1460-byte MSS on 2026-09-06,
    and 1 of 6 (as 9 + 1451 + 471) on 2026-09-07."""

    # the one-in-flight gate
    concurrent_rejections: int = 0
    """``Concurrency.STRICT`` refusals. Two sends in a row on this CPU yield one
    response, for the LAST request, with end code 0x0000 and no error at all."""
    queue_waits: int = 0
    """``Concurrency.SERIALIZE`` waits. Every one of them lands in ``queue_ns``."""

    # connection lifecycle
    connects: int = 0
    connect_failures: int = 0
    entry_busy: int = 0
    """Accept-then-FIN. The CPU accepts exactly one simultaneous connection on an
    entry, and ``socket.connect()`` succeeds anyway."""
    handshake_failures: int = 0
    disconnects: int = 0
    reconnects: int = 0

    # transport faults
    timeouts: int = 0
    protocol_errors: int = 0
    connection_lost: int = 0

    # UDP
    stale_datagrams: int = 0
    """Dropped by the per-transaction epoch — a late reply is never an answer."""
    foreign_datagrams: int = 0
    socket_rebinds: int = 0

    # health probes and user callbacks
    probes_sent: int = 0
    probes_skipped: int = 0
    """A probe that found the gate held. Skipped and counted, never queued."""
    sink_errors: int = 0

    def copy(self) -> Counters:
        """A detached copy, for putting inside a frozen snapshot."""
        return Counters(**{f.name: getattr(self, f.name) for f in fields(self)})

    def as_mapping(self) -> Mapping[str, int]:
        return {f.name: int(getattr(self, f.name)) for f in fields(self)}

    def observe(self, tx: Transaction) -> None:
        """Fold one completed-or-failed transaction into the tallies."""
        self.transactions_started += 1
        self.bytes_sent += tx.request_bytes
        self.bytes_received += tx.response_bytes
        self.chunks_received += len(tx.timing.chunks)
        if tx.timing.segmented:
            self.segmented_responses += 1
        if tx.timing.is_complete:
            self.transactions_completed += 1
        else:
            self.transactions_failed += 1
        if tx.end_code != 0:
            self.end_code_errors += 1


# ---------------------------------------------------------------------------
# Percentiles
# ---------------------------------------------------------------------------

PERCENTILE_METHOD: Final = "nearest-rank (ceiling), 1-indexed, no interpolation"
"""The one method this library uses, spelled out so it can be checked.

For ``n`` samples sorted ascending as ``x[1..n]``, the ``p``-th percentile is
``x[rank]`` where ``rank = max(1, ceil(p * n / 100))``. Consequences, all tested:

* the result is always an **observed sample**, never an interpolated value that no
  transaction ever took;
* ``p0`` is the minimum and ``p100`` is the maximum, for every ``n``;
* ``n == 1`` gives that sample for every ``p``;
* ``n == 2`` gives ``x[1]`` for ``p <= 50`` and ``x[2]`` above it.

``p`` is converted with ``Fraction(str(p))`` so that the decimal a caller wrote is the
decimal that is used: binary ``99.9`` is ``99.90000000000000568...``, and at ``n = 1000``
that rounds up to rank 1000 instead of the correct 999.
"""


class NoSamplesError(ValueError):
    """Percentiles were asked for over an empty window.

    Raised rather than answered with zeros: a p99 of 0 ns for a connection that has
    never completed a transaction is a fabricated measurement.
    """


def nearest_rank(p: float | int | str, n: int) -> int:
    """The 1-indexed rank of the ``p``-th percentile of ``n`` samples.

    See :data:`PERCENTILE_METHOD`. ``p`` outside ``[0, 100]`` raises; ``n < 1`` raises.
    """
    if n < 1:
        raise NoSamplesError(f"nearest_rank needs at least one sample, got n={n}.")
    fraction = Fraction(str(p))
    if not 0 <= fraction <= 100:
        raise ValueError(f"percentile must be in [0, 100], got {p!r}.")
    rank = math.ceil(fraction * n / 100)
    return rank if rank >= 1 else 1


def percentile_ns(ascending: Sequence[int], p: float | int | str) -> int:
    """The ``p``-th percentile of an **already sorted ascending** sequence."""
    return ascending[nearest_rank(p, len(ascending)) - 1]


@final
@dataclass(frozen=True, slots=True)
class Percentiles:
    """An exact percentile view over a window of samples, in nanoseconds.

    ``samples`` is the window itself, sorted ascending, so the numbers we publish can
    be recomputed by hand from the same data.
    """

    samples: tuple[int, ...]

    METHOD: ClassVar[str] = PERCENTILE_METHOD

    def __post_init__(self) -> None:
        if not self.samples:
            raise NoSamplesError("Percentiles over an empty window.")

    @classmethod
    def of(cls, samples: Iterable[int]) -> Percentiles:
        """Sort ``samples`` ascending and build. Raises on an empty input."""
        return cls(tuple(sorted(samples)))

    @property
    def count(self) -> int:
        return len(self.samples)

    @property
    def minimum(self) -> int:
        return self.samples[0]

    @property
    def maximum(self) -> int:
        return self.samples[-1]

    @property
    def mean_ns(self) -> float:
        return sum(self.samples) / len(self.samples)

    @property
    def p50(self) -> int:
        return self.at(50)

    @property
    def p90(self) -> int:
        return self.at(90)

    @property
    def p99(self) -> int:
        return self.at(99)

    @property
    def p99_9(self) -> int:
        return self.at("99.9")

    def at(self, p: float | int | str) -> int:
        """The ``p``-th percentile in nanoseconds, by :data:`PERCENTILE_METHOD`."""
        return self.samples[nearest_rank(p, len(self.samples)) - 1]

    def ms(self, p: float | int | str) -> float:
        """The ``p``-th percentile in milliseconds, for the one place humans read it."""
        return self.at(p) / 1_000_000.0

    def __str__(self) -> str:
        return (
            f"n={self.count} min={self.minimum / 1e6:.3f} p50={self.p50 / 1e6:.3f} "
            f"p90={self.p90 / 1e6:.3f} p99={self.p99 / 1e6:.3f} "
            f"max={self.maximum / 1e6:.3f} ms [{self.METHOD}]"
        )


# ---------------------------------------------------------------------------
# Log-linear histogram
# ---------------------------------------------------------------------------

_SUB_BITS: Final = 5
"""Sub-buckets per octave = 2**5 = 32, so the worst-case relative width of a bucket is
1/32 and the worst-case relative error of a bucket midpoint is under 1.6 %."""

_SUB_COUNT: Final = 1 << _SUB_BITS
_LINEAR_LIMIT: Final = 1 << (_SUB_BITS + 1)
"""Below this, buckets are exact single nanosecond values."""

_MAX_OCTAVE: Final = 62
"""The histogram covers 0 .. 2**63-1 ns (~292 years). Nothing is ever clamped: a value
above the range raises, because a 292-year 'latency' is a defect, not a sample."""

_BUCKET_COUNT: Final = ((_MAX_OCTAVE - _SUB_BITS) << _SUB_BITS) + _SUB_COUNT + _SUB_COUNT


def _bucket_index(value: int) -> int:
    if value < _LINEAR_LIMIT:
        return value
    octave = value.bit_length() - 1
    if octave > _MAX_OCTAVE:
        raise ValueError(
            f"{value} ns is outside the histogram range (0 .. 2**63-1 ns). Nothing is "
            "clamped here; a value this large is a defect, not a measurement."
        )
    shift = octave - _SUB_BITS
    return (shift << _SUB_BITS) + (value >> shift)


def _bucket_bounds(index: int) -> tuple[int, int]:
    """Inclusive ``(low_ns, high_ns)`` of a bucket index."""
    if index < _LINEAR_LIMIT:
        return index, index
    shift = (index - _SUB_COUNT) >> _SUB_BITS
    sub = index - (shift << _SUB_BITS)
    low = sub << shift
    return low, low + (1 << shift) - 1


@final
@dataclass(frozen=True, slots=True)
class HistogramBucket:
    """One non-empty log-linear bucket. ``low_ns`` and ``high_ns`` are inclusive."""

    low_ns: int
    high_ns: int
    count: int


# ---------------------------------------------------------------------------
# LatencyRecorder
# ---------------------------------------------------------------------------

# Index of every mutable scalar inside LatencyRecorder's preallocated stats block.
# They live in an array rather than in attributes so that recording allocates nothing
# at all — see the class docstring.
_SUM: Final = 0
_RECORDED: Final = 1
_OBSERVED: Final = 2
_INCOMPLETE: Final = 3
_FAILED: Final = 4
_MIN: Final = 5
_MAX: Final = 6
_POS: Final = 7
_FILLED: Final = 8
_STAT_SLOTS: Final = 9

_PHASE_INDEX: Final[Mapping[Phase, int]] = {p: i for i, p in enumerate(Phase)}
_PHASES: Final[tuple[Phase, ...]] = tuple(Phase)


@final
class LatencyRecorder:
    """A ready-made ``on_transaction`` sink. **Allocates nothing after construction.**

    It keeps two things:

    * a fixed **ring** of the most recent ``capacity`` ``wire_ns`` samples, from which
      :meth:`percentiles` are computed exactly, and
    * an all-time **log-linear histogram** (:data:`_SUB_BITS` sub-buckets per octave)
      so the shape of the tail survives past the end of the ring.

    Those, the per-phase tallies and every running scalar live in four preallocated
    :class:`array.array` blocks of machine integers, written by index. There is no list
    to append to and no dict to grow, and not even the running sum retains a Python int
    that widens as it grows: this object is handed to a control loop, and a sink that
    allocates is a sink that eventually pauses the loop it is measuring. The only
    objects created per call are the transient boxed integers of the arithmetic itself,
    freed the moment the expression ends. ``tests/unit/test_observability.py`` proves
    it: a :mod:`tracemalloc` diff filtered to this module's own source lines is
    **exactly zero bytes and zero blocks** across ten thousand recordings, the buffers
    are the same objects at the same lengths afterwards, and nothing in
    ``__slots__`` is a growable container.

    The sink is called by the client **after** ``decoded_at`` is stamped, so time spent
    here cannot contaminate the measurement it is handed.
    """

    __slots__ = ("_buckets", "_capacity", "_phases", "_ring", "_stats")

    def __init__(self, *, capacity: int = 4096) -> None:
        if capacity < 1:
            raise ValueError(f"capacity must be at least 1, got {capacity}.")
        self._capacity: int = capacity
        self._ring: array[int] = array("q", bytes(8 * capacity))
        self._buckets: array[int] = array("q", bytes(8 * _BUCKET_COUNT))
        self._phases: array[int] = array("q", bytes(8 * len(_PHASES)))
        # Every mutable scalar lives in this preallocated block rather than in an
        # attribute, so not even the running sum retains a freshly widened Python int
        # as it grows past a 30-bit digit boundary. After construction this object owns
        # exactly four buffers and never allocates another one.
        self._stats: array[int] = array("q", bytes(8 * _STAT_SLOTS))
        self._stats[_MIN] = -1
        self._stats[_MAX] = -1

    # -- recording (the hot path) -------------------------------------------

    def record_ns(self, ns: int) -> None:
        """Record one ``wire_ns`` sample. Negative values raise; nothing is clamped."""
        if ns < 0:
            raise ValueError(f"a latency sample cannot be negative, got {ns} ns.")
        stats = self._stats
        pos = stats[_POS]
        self._ring[pos] = ns
        pos += 1
        stats[_POS] = 0 if pos == self._capacity else pos
        if stats[_FILLED] < self._capacity:
            stats[_FILLED] += 1
        self._buckets[_bucket_index(ns)] += 1
        stats[_RECORDED] += 1
        stats[_SUM] += ns
        if stats[_MIN] < 0 or ns < stats[_MIN]:
            stats[_MIN] = ns
        if ns > stats[_MAX]:
            stats[_MAX] = ns

    def observe(self, tx: Transaction) -> None:
        """Fold one transaction in. Incomplete ones are counted, never invented."""
        stats = self._stats
        stats[_OBSERVED] += 1
        if tx.end_code != 0:
            stats[_FAILED] += 1
        if tx.timing.is_complete:
            self.record_ns(tx.timing.wire_ns)
            self._phases[_PHASE_INDEX[tx.dominant_phase]] += 1
        else:
            stats[_INCOMPLETE] += 1

    def __call__(self, tx: Transaction, /) -> None:
        """:class:`aslmp.timing.TransactionSink` — pass this as ``on_transaction``."""
        self.observe(tx)

    # -- reading (not the hot path) -----------------------------------------

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def window(self) -> int:
        """Samples currently in the ring."""
        return self._stats[_FILLED]

    @property
    def observed(self) -> int:
        """Transactions handed to this sink, complete or not."""
        return self._stats[_OBSERVED]

    @property
    def recorded(self) -> int:
        """Samples ever recorded, including those the ring has since overwritten."""
        return self._stats[_RECORDED]

    @property
    def incomplete(self) -> int:
        """Transactions with no ``wire_ns`` — a timeout has no latency to report."""
        return self._stats[_INCOMPLETE]

    @property
    def failed(self) -> int:
        """Transactions whose end code was not ``0x0000``."""
        return self._stats[_FAILED]

    @property
    def has_samples(self) -> bool:
        return self._stats[_FILLED] > 0

    @property
    def minimum_ns(self) -> int:
        """All-time minimum. Raises when nothing was recorded."""
        if self._stats[_MIN] < 0:
            raise NoSamplesError("no latency samples have been recorded.")
        return self._stats[_MIN]

    @property
    def maximum_ns(self) -> int:
        """All-time maximum. Raises when nothing was recorded."""
        if self._stats[_MAX] < 0:
            raise NoSamplesError("no latency samples have been recorded.")
        return self._stats[_MAX]

    @property
    def mean_ns(self) -> float:
        """All-time mean. Raises when nothing was recorded."""
        recorded = self._stats[_RECORDED]
        if recorded == 0:
            raise NoSamplesError("no latency samples have been recorded.")
        return self._stats[_SUM] / recorded

    def samples(self) -> tuple[int, ...]:
        """The ring contents in ascending order. Allocates — not for the hot path."""
        filled = self._stats[_FILLED]
        if filled < self._capacity:
            return tuple(sorted(self._ring[:filled]))
        return tuple(sorted(self._ring))

    def percentiles(self) -> Percentiles:
        """Exact percentiles over the ring window. Raises when the window is empty."""
        if self._stats[_FILLED] == 0:
            raise NoSamplesError(
                "no latency samples in the window; a p99 of 0 ns would be a fabrication."
            )
        return Percentiles(self.samples())

    def histogram(self) -> tuple[HistogramBucket, ...]:
        """The all-time distribution: every non-empty bucket, ascending."""
        buckets = self._buckets
        out: list[HistogramBucket] = []
        for index in range(_BUCKET_COUNT):
            count = buckets[index]
            if count:
                low, high = _bucket_bounds(index)
                out.append(HistogramBucket(low, high, count))
        return tuple(out)

    def phase_counts(self) -> Mapping[Phase, int]:
        """How often each phase dominated a completed transaction."""
        return {phase: self._phases[i] for i, phase in enumerate(_PHASES)}

    def snapshot(
        self,
        *,
        at: Nanos,
        connection_id: str = "",
        generation: int = 0,
        counters: Counters | None = None,
    ) -> MetricsSnapshot:
        """One frozen view. ``latency`` is ``None`` when the window is empty."""
        return MetricsSnapshot(
            at=at,
            connection_id=connection_id,
            generation=generation,
            counters=(Counters() if counters is None else counters).copy(),
            latency=self.percentiles() if self._stats[_FILLED] else None,
            observed=self._stats[_OBSERVED],
            recorded=self._stats[_RECORDED],
            incomplete=self._stats[_INCOMPLETE],
            failed=self._stats[_FAILED],
            window=self._stats[_FILLED],
            capacity=self._capacity,
            histogram=self.histogram(),
            phases=self.phase_counts(),
        )

    def reset(self) -> None:
        """Clear the ring, the histogram and the tallies. Reuses the same storage."""
        ring = self._ring
        for i in range(self._capacity):
            ring[i] = 0
        buckets = self._buckets
        for i in range(_BUCKET_COUNT):
            buckets[i] = 0
        phases = self._phases
        for i in range(len(_PHASES)):
            phases[i] = 0
        stats = self._stats
        for i in range(_STAT_SLOTS):
            stats[i] = 0
        stats[_MIN] = -1
        stats[_MAX] = -1


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class MetricsSnapshot:
    """One frozen view of a connection's counters and latency window.

    ``latency`` is ``None`` — not a zeroed :class:`Percentiles` — when no transaction
    has completed yet. There is no such thing as the p99 of nothing.
    """

    at: Nanos
    connection_id: str
    generation: int
    counters: Counters
    latency: Percentiles | None
    observed: int
    recorded: int
    incomplete: int
    failed: int
    window: int
    capacity: int
    histogram: tuple[HistogramBucket, ...]
    phases: Mapping[Phase, int]

    def __str__(self) -> str:
        head = f"{self.connection_id or '<unbound>'} gen={self.generation}"
        latency = str(self.latency) if self.latency is not None else "no samples"
        return (
            f"{head} observed={self.observed} failed={self.failed} "
            f"incomplete={self.incomplete} window={self.window}/{self.capacity} :: {latency}"
        )
