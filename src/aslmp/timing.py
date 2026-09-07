"""Layer 2.5 — the transaction record. Pure data; no I/O, no clock of its own.

Graft G4 of the locked architecture (DESIGN.md §1.8, §2.5): a seven-stamp
:class:`TransactionTiming` — queue / encode / first_byte / transfer / decode / wire /
host_gap — plus the chunk list and the connection generation, so that "the p99 moved"
becomes an attributable question without a packet capture.

Two properties of this module are load-bearing and both come from measurements on
**MELSEC iQ-F FX5U-32MT/DS, firmware 1.065, 2026-09-06**:

1. **The receive stamp is taken after the LAST chunk.** One 1931-byte response, three
   identical trials on that CPU::

       trial 1: [(1931, 10.169 ms)]                    one chunk
       trial 2: [(1931,  9.299 ms)]                    one chunk
       trial 3: [(1460, 10.964 ms), (471, 14.010 ms)]  TWO chunks, 3.0 ms apart

   Trial 3 is the 1460-byte MSS boundary; whether it appears depends on host
   scheduling, so it passes two tests out of three and fails in production. Stamping
   the *first* chunk reports 11.0 ms for a transaction that actually took 14.0 ms.
   :class:`TimingBuilder` therefore offers no way to supply a receive stamp at all —
   ``first_byte_at`` and ``received_at`` are derived from the chunk list, and
   :class:`TransactionTiming` re-checks the derivation in ``__post_init__`` so a
   hand-built record cannot lie either.

2. **Every stamp comes from an injected clock.** Nothing in this module calls
   :func:`time.monotonic_ns`; ``time`` is not imported. The clock is a constructor
   argument of :class:`TimingBuilder`, so every derived property is exact against a
   fake clock in a test.

All stamps come from one *monotonic* clock. Never wall-clock: a loop that runs for
months crosses an NTP step, a DST boundary and a leap smear.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from itertools import pairwise
from typing import NewType, Protocol, TypeAlias, final

__all__ = [
    "Chunk",
    "Clock",
    "EncodingLabel",
    "FrameLabel",
    "Nanos",
    "Phase",
    "TimingBuilder",
    "TimingIncompleteError",
    "TimingOrderError",
    "Transaction",
    "TransactionSink",
    "TransactionTiming",
    "TransportLabel",
    "WireOption",
]

Nanos = NewType("Nanos", int)
"""A stamp or duration in nanoseconds, from one monotonic clock."""


class Clock(Protocol):
    """A monotonic nanosecond clock. :func:`time.monotonic_ns` satisfies this.

    It is a parameter everywhere it is used. This module never reaches for a default:
    a stamp taken at a site a test cannot control is a number nobody can check.
    """

    def __call__(self) -> int: ...


class WireOption(Protocol):
    """Structural stand-in for the connection-entry option enums.

    :class:`Transaction` records which frame format, coding and transport produced it.
    Those enums (``FrameType``, ``Encoding``, ``TransportKind``) live at Layer 0/1 and
    this module is Layer 2.5 *pure data* that names no module above Layer 0 — so the
    fields are typed structurally, exactly as ``transport/`` takes a structural
    ``Reassembler`` rather than naming a frame (DESIGN.md §1.9). Any
    :class:`enum.Enum` whose members carry ``str`` values satisfies it, and identity
    comparison (``tx.frame is FrameType.THREE_E``) works unchanged.
    """

    @property
    def name(self) -> str: ...

    @property
    def value(self) -> str: ...


FrameLabel: TypeAlias = WireOption
EncodingLabel: TypeAlias = WireOption
TransportLabel: TypeAlias = WireOption


class TimingIncompleteError(ValueError):
    """A duration was asked for that the transaction never stamped.

    Raised, not defaulted to zero and not returned as ``None``: a failed transaction
    has no ``wire_ns``, and inventing one would put a fabricated sample into the
    histogram we publish. Callers that handle failed transactions test
    :attr:`TransactionTiming.is_complete` first.
    """


class TimingOrderError(ValueError):
    """A stamp was taken twice, out of order, or before its predecessor."""


class Phase(enum.Enum):
    """The attributable phases of one transaction (DESIGN.md §2.1).

    Declaration order is the tie-break order of
    :attr:`Transaction.dominant_phase`.
    """

    QUEUE = "queue"
    ENCODE = "encode"
    ROUND_TRIP = "round_trip"
    TRANSFER = "transfer"
    DECODE = "decode"


@final
@dataclass(frozen=True, slots=True)
class Chunk:
    """One ``recv`` that returned bytes, and the moment it returned them.

    A zero-byte read is EOF, not a chunk: the transport classifies it (entry-busy on
    the first read of a generation, connection-lost otherwise) and never records it
    here. ``nbytes`` must therefore be positive.

    ``partial`` is true when this ``recv`` returned FEWER bytes than the length-driven
    read asked for -- that is, the message was split across TCP segments. It is what
    :attr:`TransactionTiming.segmented` is computed from, and it is not the same
    question as "was there more than one chunk": a stream response always takes at
    least two reads by construction (the fixed prefix, then ``L`` units), so counting
    chunks reports every TCP response as segmented and tells an operator nothing.
    Always false on a datagram transport, where one datagram is one message.
    """

    nbytes: int
    at: Nanos
    partial: bool = False

    def __post_init__(self) -> None:
        if self.nbytes <= 0:
            raise ValueError(
                f"Chunk.nbytes must be positive, got {self.nbytes}. A zero-byte read is "
                "EOF and is classified by the transport, never recorded as a chunk."
            )


def _require(stamp: Nanos | None, name: str, what: str) -> Nanos:
    if stamp is None:
        raise TimingIncompleteError(
            f"{what} is undefined: this transaction never stamped {name}."
        )
    return stamp


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class TransactionTiming:
    """The seven-stamp record. All stamps from one monotonic clock.

    Stamps are non-decreasing in field order. ``first_byte_at`` and ``received_at``
    are *derived* from ``chunks`` — first chunk and **last** chunk respectively — and
    ``__post_init__`` refuses any record where they are not.

    The optional stamps are ``None`` for a transaction that did not get that far:
    a timeout has no ``received_at``, an end-code failure has no ``decoded_at``, and
    the first transaction of a connection has no ``prev_received_at``. The derived
    properties raise :class:`TimingIncompleteError` rather than substituting a zero.
    """

    submitted_at: Nanos
    gate_acquired_at: Nanos
    encoded_at: Nanos
    sent_at: Nanos
    first_byte_at: Nanos | None = None
    received_at: Nanos | None = None
    decoded_at: Nanos | None = None
    prev_received_at: Nanos | None = None
    chunks: tuple[Chunk, ...] = ()

    def __post_init__(self) -> None:
        ordered: list[tuple[str, Nanos]] = [
            ("submitted_at", self.submitted_at),
            ("gate_acquired_at", self.gate_acquired_at),
            ("encoded_at", self.encoded_at),
            ("sent_at", self.sent_at),
        ]
        for name in ("first_byte_at", "received_at", "decoded_at"):
            stamp: Nanos | None = getattr(self, name)
            if stamp is not None:
                ordered.append((name, stamp))
        for (prev_name, prev), (name, now) in pairwise(ordered):
            if now < prev:
                raise TimingOrderError(
                    f"{name}={now} precedes {prev_name}={prev}. Stamps must come from one "
                    "monotonic clock, in field order."
                )
        if self.received_at is not None and self.first_byte_at is None:
            raise TimingOrderError("received_at is stamped but first_byte_at is not.")
        if self.decoded_at is not None and self.received_at is None:
            raise TimingOrderError("decoded_at is stamped but received_at is not.")
        self._check_chunks()

    def _check_chunks(self) -> None:
        if not self.chunks:
            if self.first_byte_at is not None or self.received_at is not None:
                raise TimingOrderError(
                    "first_byte_at/received_at are stamped but no chunks were recorded; "
                    "both are derived from the chunk list."
                )
            return
        previous = self.sent_at
        for i, chunk in enumerate(self.chunks):
            if chunk.at < previous:
                raise TimingOrderError(
                    f"chunks[{i}].at={chunk.at} precedes the previous stamp {previous}."
                )
            previous = chunk.at
        if self.first_byte_at != self.chunks[0].at:
            raise TimingOrderError(
                f"first_byte_at={self.first_byte_at} is not chunks[0].at="
                f"{self.chunks[0].at}."
            )
        if self.received_at != self.chunks[-1].at:
            raise TimingOrderError(
                f"received_at={self.received_at} is not chunks[-1].at="
                f"{self.chunks[-1].at}. The receive stamp is taken after the LAST chunk: "
                "measured on FX5U-32MT/DS fw 1.065, a 1931-byte response arrived as "
                "(1460 B @ 10.964 ms) + (471 B @ 14.010 ms) on 1 of 3 identical reads, and "
                "stamping the first chunk reports 11.0 ms for a 14.0 ms transaction."
            )

    # -- shape ---------------------------------------------------------------

    @property
    def is_complete(self) -> bool:
        """True when the response was received and decoded."""
        return self.decoded_at is not None

    @property
    def segmented(self) -> bool:
        """True when one length-driven read came back in pieces: the 1460 B MSS case.

        Deliberately NOT ``len(self.chunks) > 1``. A stream response is read as the
        fixed prefix and then exactly ``L`` more units, so it takes at least two
        ``recv`` calls whatever the network does; counting chunks would flag 100% of
        TCP responses and make :attr:`~aslmp.observability.Counters.segmented_responses`
        unable to distinguish anything. Measured on FX5U-32MT/DS fw 1.065 (2026-09-06):
        1 of 3 identical 1931-byte reads split at 1460 bytes, and on 2026-09-07 the
        same read arrived as 9 + 1922 with neither read short -- one segment as far as
        this process could tell.
        """
        return any(chunk.partial for chunk in self.chunks)

    @property
    def bytes_received(self) -> int:
        return sum(chunk.nbytes for chunk in self.chunks)

    @property
    def has_host_gap(self) -> bool:
        """False for the first transaction of a connection, which has no predecessor."""
        return self.prev_received_at is not None

    # -- derived durations ---------------------------------------------------

    @property
    def queue_ns(self) -> int:
        """Host contention: waiting for the one-in-flight gate.

        Non-zero under ``Concurrency.SERIALIZE`` and for the ``aslmp.sync`` thread hop.
        A queue that does not report its wait makes every latency number a lie.
        """
        return self.gate_acquired_at - self.submitted_at

    @property
    def encode_ns(self) -> int:
        """Building the request frame: validation, encoding, serial allocation."""
        return self.encoded_at - self.gate_acquired_at

    @property
    def send_ns(self) -> int:
        """Handing the request to the OS: the ``sendall`` itself.

        Not a member of :class:`Phase` — DESIGN.md §2.1 fixes that enum at five members
        and this unit does not widen it. It is exposed anyway because it is the one
        interval of :attr:`total_ns` that no phase covers, so
        ``total_ns == queue + encode + send + first_byte + transfer + decode`` holds
        exactly. On a large ASCII write against a full socket buffer it is not zero.
        """
        return self.sent_at - self.encoded_at

    @property
    def first_byte_ns(self) -> int:
        """Network round trip plus the SLMP module's service processing.

        On FX5U-32MT/DS fw 1.065 this is essentially the whole transaction: a 4-byte
        ``0619`` loopback with no device access at all cost p50 7.340 ms against
        p50 6.916 ms for a 2-word ``0401``.
        """
        return _require(self.first_byte_at, "first_byte_at", "first_byte_ns") - self.sent_at

    @property
    def transfer_ns(self) -> int:
        """Bulk transfer and segmentation: first chunk to last chunk."""
        return _require(self.received_at, "received_at", "transfer_ns") - _require(
            self.first_byte_at, "first_byte_at", "transfer_ns"
        )

    @property
    def wire_ns(self) -> int:
        """Sent to last chunk. **The** number."""
        return _require(self.received_at, "received_at", "wire_ns") - self.sent_at

    @property
    def decode_ns(self) -> int:
        """Turning the response payload into typed values."""
        return _require(self.decoded_at, "decoded_at", "decode_ns") - _require(
            self.received_at, "received_at", "decode_ns"
        )

    @property
    def total_ns(self) -> int:
        """Submission to decoded value — everything the caller waited for.

        Exactly ``queue_ns + encode_ns + send_ns + first_byte_ns + transfer_ns +
        decode_ns``; see :attr:`send_ns` for why that list has six terms and
        :class:`Phase` has five members.
        """
        return _require(self.decoded_at, "decoded_at", "total_ns") - self.submitted_at

    @property
    def host_gap_ns(self) -> int:
        """Loop scheduling gap: the previous response to this submission.

        This is where ``Cadence`` overrun shows up (DESIGN.md G18). Raises for the
        first transaction of a connection; test :attr:`has_host_gap` first.
        """
        return self.submitted_at - _require(
            self.prev_received_at, "prev_received_at", "host_gap_ns"
        )

    @property
    def wire_ms(self) -> float:
        """:attr:`wire_ns` in milliseconds, for the one place humans read it."""
        return self.wire_ns / 1_000_000.0


@final
class TimingBuilder:
    """Accumulates the stamps of one transaction. The only sanctioned way to build one.

    Every method takes its stamp from the injected clock; there is no method that
    accepts a time, and no method that accepts a receive stamp. ``first_byte_at`` and
    ``received_at`` fall out of :meth:`chunk`, so recording the *first* chunk as the
    receive time is not a mistake one can make here — it is unsayable.

    Stamps are single-use and ordered. Calling :meth:`sent` twice, or :meth:`chunk`
    before :meth:`sent`, raises :class:`TimingOrderError`; nothing is overwritten and
    nothing is quietly ignored.
    """

    __slots__ = (
        "_chunks",
        "_clock",
        "_decoded_at",
        "_encoded_at",
        "_gate_acquired_at",
        "_prev_received_at",
        "_sent_at",
        "_submitted_at",
    )

    def __init__(self, clock: Clock, *, prev_received_at: Nanos | None = None) -> None:
        self._clock = clock
        self._submitted_at = Nanos(clock())
        self._prev_received_at = prev_received_at
        self._gate_acquired_at: Nanos | None = None
        self._encoded_at: Nanos | None = None
        self._sent_at: Nanos | None = None
        self._decoded_at: Nanos | None = None
        self._chunks: list[Chunk] = []

    def _stamp(self, name: str, current: Nanos | None, previous: Nanos) -> Nanos:
        if current is not None:
            raise TimingOrderError(f"{name} was already stamped at {current}.")
        now = Nanos(self._clock())
        if now < previous:
            raise TimingOrderError(
                f"{name}={now} precedes the previous stamp {previous}: the injected clock "
                "is not monotonic."
            )
        return now

    def gate_acquired(self) -> Nanos:
        """Stamp the moment the one-in-flight gate was won. Ends ``queue_ns``."""
        self._gate_acquired_at = self._stamp(
            "gate_acquired_at", self._gate_acquired_at, self._submitted_at
        )
        return self._gate_acquired_at

    def encoded(self) -> Nanos:
        """Stamp the moment the request frame is bytes. Ends ``encode_ns``."""
        if self._gate_acquired_at is None:
            raise TimingOrderError("encoded() before gate_acquired().")
        self._encoded_at = self._stamp("encoded_at", self._encoded_at, self._gate_acquired_at)
        return self._encoded_at

    def sent(self) -> Nanos:
        """Stamp immediately after the last byte is handed to the OS. Starts ``wire_ns``."""
        if self._encoded_at is None:
            raise TimingOrderError("sent() before encoded().")
        self._sent_at = self._stamp("sent_at", self._sent_at, self._encoded_at)
        return self._sent_at

    def chunk(self, nbytes: int, *, partial: bool = False) -> Chunk:
        """Record one ``recv`` that returned ``nbytes``, stamped now.

        The first call fixes ``first_byte_at``; the last call fixes ``received_at``.
        Neither can be supplied by a caller.

        ``partial`` says this read came back short of what it asked for, which is the
        only honest evidence of a segment split; see :attr:`Chunk.partial`. A datagram
        transport never passes it.
        """
        if self._sent_at is None:
            raise TimingOrderError("chunk() before sent().")
        if self._decoded_at is not None:
            raise TimingOrderError("chunk() after decoded().")
        previous = self._chunks[-1].at if self._chunks else self._sent_at
        now = Nanos(self._clock())
        if now < previous:
            raise TimingOrderError(
                f"chunk at {now} precedes the previous stamp {previous}: the injected clock "
                "is not monotonic."
            )
        recorded = Chunk(nbytes, now, partial)
        self._chunks.append(recorded)
        return recorded

    def decoded(self) -> Nanos:
        """Stamp the moment the payload became typed values. Ends ``decode_ns``."""
        if not self._chunks:
            raise TimingOrderError("decoded() before any chunk was received.")
        self._decoded_at = self._stamp("decoded_at", self._decoded_at, self._chunks[-1].at)
        return self._decoded_at

    # -- inspection ----------------------------------------------------------

    @property
    def submitted_at(self) -> Nanos:
        return self._submitted_at

    @property
    def sent_at(self) -> Nanos | None:
        return self._sent_at

    @property
    def chunks(self) -> tuple[Chunk, ...]:
        return tuple(self._chunks)

    @property
    def bytes_received(self) -> int:
        return sum(chunk.nbytes for chunk in self._chunks)

    def build(self) -> TransactionTiming:
        """Freeze the stamps taken so far into a :class:`TransactionTiming`.

        Legal for a failed transaction: the stamps that were never taken stay
        ``None`` and the durations that depend on them raise. It is *not* legal
        before :meth:`sent` — a transaction that never reached the socket has no
        timing to report and the transport raises ``SlmpNotSentError`` instead.
        """
        if self._gate_acquired_at is None or self._encoded_at is None or self._sent_at is None:
            raise TimingOrderError(
                "build() before sent(): a transaction that never reached the socket has no "
                "timing record."
            )
        chunks = tuple(self._chunks)
        return TransactionTiming(
            submitted_at=self._submitted_at,
            gate_acquired_at=self._gate_acquired_at,
            encoded_at=self._encoded_at,
            sent_at=self._sent_at,
            first_byte_at=chunks[0].at if chunks else None,
            received_at=chunks[-1].at if chunks else None,
            decoded_at=self._decoded_at,
            prev_received_at=self._prev_received_at,
            chunks=chunks,
        )


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class Transaction:
    """One request/response exchange, as data.

    ``generation`` is the anti-lie field: it bumps on **every** reconnect and every UDP
    source-port rebind, so a latency spike caused by a hidden reconnect cannot be
    mistaken for a slow PLC.
    """

    timing: TransactionTiming
    sequence: int
    connection_id: str
    generation: int
    after_reconnect: bool
    command: int
    subcommand: int
    frame: FrameLabel
    encoding: EncodingLabel
    transport: TransportLabel
    serial: int | None
    request_bytes: int
    response_bytes: int
    end_code: int
    prebuilt: bool
    plc_clock: int | None = None
    request_frame: bytes | None = None
    response_frame: bytes | None = None

    @property
    def ok(self) -> bool:
        """End code ``0x0000``. Says nothing about whether the request was *true*."""
        return self.end_code == 0

    @property
    def dominant_phase(self) -> Phase:
        """Which phase took the most time.

        Ties go to the earlier member of :class:`Phase`. Raises
        :class:`TimingIncompleteError` when the transaction did not complete: an
        incomplete transaction has no dominant phase, only a missing one.
        """
        t = self.timing
        # Written without a container on purpose: LatencyRecorder calls this per
        # transaction and must allocate nothing after construction.
        best_phase = Phase.QUEUE
        best = t.queue_ns
        value = t.encode_ns
        if value > best:
            best_phase, best = Phase.ENCODE, value
        value = t.first_byte_ns
        if value > best:
            best_phase, best = Phase.ROUND_TRIP, value
        value = t.transfer_ns
        if value > best:
            best_phase, best = Phase.TRANSFER, value
        value = t.decode_ns
        if value > best:
            best_phase, best = Phase.DECODE, value
        return best_phase

    def phase_ns(self, phase: Phase) -> int:
        """The duration of one phase. Raises if this transaction never stamped it."""
        t = self.timing
        if phase is Phase.QUEUE:
            return t.queue_ns
        if phase is Phase.ENCODE:
            return t.encode_ns
        if phase is Phase.ROUND_TRIP:
            return t.first_byte_ns
        if phase is Phase.TRANSFER:
            return t.transfer_ns
        return t.decode_ns


class TransactionSink(Protocol):
    """``on_transaction=`` — called after ``decoded_at`` is stamped, never before.

    A slow sink therefore cannot contaminate the measurement it is handed
    (DESIGN.md §2.5).
    """

    def __call__(self, tx: Transaction, /) -> None: ...

