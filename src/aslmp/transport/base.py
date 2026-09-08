"""Layer 3 -- the transport: bytes, deadlines, and deliberately nothing else.

This package is the only place in the library where a socket is legal, and it is the
only place that is forbidden to know what a frame is. ``tests/unit/test_layering.py``
bans the edge ``aslmp.transport -> aslmp.wire`` outright rather than by layer
arithmetic (``wire`` is *below* ``transport``, so the layer rule alone would permit
it), because a transport that can name a frame will eventually parse one -- and a
parser that lives next to the socket is how a resynchronising client returns the
previous transaction's bytes as this transaction's answer.

What crosses the boundary instead is structure:

* :class:`Reassembler` -- ``bytes_needed`` / ``feed``. ``aslmp.wire.reader``'s
  ``ResponseAccumulator`` satisfies it without being named here. The transport drives a
  length-driven read without learning what a length field is.
* :class:`Correlation` -- ``matches(datagram) -> bool`` plus an opaque ``label``. The
  UDP transport can route a datagram to the transaction that owns it without knowing
  that the thing it is matching on is a 4E serial No.
* :class:`TransportObserver` -- the two events only a transport can witness (a dropped
  datagram, a socket rebind), reported upward so that :mod:`aslmp.connection` can attach
  the connection id and bump the generation.

The dependency runs upward; the knowledge runs downward.

.. rubric:: The measurements this module is shaped by

All on **MELSEC iQ-F FX5U-32MT/DS, firmware 1.065**, 2026-09-06/07:

* **TCP request coalescing corrupts silently.** Two requests written before the first
  response is read produce ONE response, for the *last* request, end code ``0x0000``.
  On 3E there is no serial, so it is undetectable wrong data reported as success. Hence
  one transaction in flight per TCP connection, enforced by a capability token rather
  than by documentation (:mod:`aslmp.transport.inflight`).
* **UDP does not have that failure** -- datagrams are framed and the same test returns
  both responses correctly -- so the one-in-flight rule is a TCP rule, not a universal
  one. UDP pipelining was clean at depth 8 and 32 and lost 20 of 64 at depth 64, with
  no end code, no ICMP and no error of any kind.
* **Wrong coding, wrong frame type, wrong transport and an overstated request length
  all fail by SILENCE.** A bare ``TimeoutError`` is therefore useless and
  :func:`timeout_error` computes an ordered cause list from what was actually observed
  (graft G3, DESIGN section 3.4).
* **TCP segmentation is real**: one 1931-byte response of three identical trials
  arrived as 1460 + 471 bytes 3.0 ms apart. Reads are length-driven and the receive
  stamp is taken after the LAST chunk.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol, final

from aslmp.errors import (
    SlmpConfigurationError,
    SlmpTimeoutError,
)
from aslmp.errors.routing import timeout_causes
from aslmp.timing import Chunk, Clock, Nanos

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable

    from aslmp.timing import TimingBuilder

__all__ = [
    "DEFAULT_BUFFER_CAPACITY",
    "DEFAULT_UDP_PIPELINE_DEPTH",
    "MAX_UDP_PIPELINE_DEPTH",
    "NULL_OBSERVER",
    "Binding",
    "Correlation",
    "Deadline",
    "Reassembler",
    "RecvBuffer",
    "Transport",
    "TransportKind",
    "TransportObserver",
    "WireResult",
    "accept_any",
    "timeout_error",
]

_NS_PER_S: Final = 1_000_000_000

DEFAULT_BUFFER_CAPACITY: Final = 2048
"""Big enough for the largest response this protocol can produce, in one allocation.

A 960-point batch read returns 1935 bytes (measured, both transports). The buffer is
reused for the life of the transport, so a control loop that reads two words forever
allocates no receive buffer at all after the first transaction.
"""

DEFAULT_UDP_PIPELINE_DEPTH: Final = 8
"""In-flight UDP requests permitted by default. Deliberately below the measured 32.

Depth 8 returned 8/8 and depth 32 returned 32/32 with zero loss; depth 64 returned
44/64 -- twenty requests dropped by the PLC's receive path with no error anywhere
(FX5U-32MT/DS fw 1.065, UDP entry No. 2 port 5001, 2026-09-06, **from the laptop over
Wi-Fi**). Re-measured 2026-09-07 from ``argus-bench`` over the **wired** link (UDP entry
port 5005), depth 48 answered exactly 32 and depth 64 answered exactly 32: the queue is
a hard 32, and the 44 was the CPU draining part of it while a slow burst was still
arriving. Both readings put the ceiling at 32. Defaulting *at* it would put the loss
cliff one firmware revision away.
"""

MAX_UDP_PIPELINE_DEPTH: Final = 32
"""The deepest burst measured lossless, on both links. Above it, requests vanish silently.

Refused rather than warned about: the failure has no end code, no ICMP and no error of
any kind, so a caller who exceeds it finds out from a transaction that never returns.
Raising this constant is a change that needs a new measurement, on the CPU it is
claimed for.
"""


class TransportKind(enum.Enum):
    """Which socket carries the frames. A GX Works3 connection-entry fact.

    Never auto-detected and never retried in the other value: sending 3E/binary at a
    UDP entry over TCP fails by silence, and the only thing a retry in the other
    transport would prove is that the caller's configuration was already wrong.

    ``TCP`` is the library default for **configurability, not speed**. On the wired
    retest (FX5U-32MT/DS fw 1.065, 2026-09-07, ``argus-bench`` 192.168.10.36, wired,
    n=300 each interleaved, control drift 0.01 ms at p50) UDP was faster at every
    percentile: p50 2.42 against 3.63 ms, p90 3.40 against 4.05, p99 3.56 against 4.69,
    sd 0.40 against 0.36. An earlier Wi-Fi run had TCP winning the tail and that was
    the reason recorded here; it was a property of the radio and did not reproduce on
    wire. TCP stays the default because a UDP SLMP entry on iQ-F is point-to-point --
    GX Works3 will not save one without a destination IP, and there are at most eight
    entries -- while a TCP entry serves any peer, and because loss is silent on UDP.
    See :data:`aslmp.client.TRANSPORT_CHOICE` and ``docs/hardware.md`` section 5.
    """

    TCP = "tcp"
    UDP = "udp"


# ---------------------------------------------------------------------------
# Deadlines
# ---------------------------------------------------------------------------


@final
@dataclass(frozen=True, slots=True)
class Deadline:
    """A client-side deadline, measured on the injected monotonic clock.

    Enforced independently of the SLMP monitoring timer. The two are different
    mechanisms: the monitoring timer makes the *PLC* give up and answer with a
    decodable end code, and this one makes the *client* give up and raise. A client
    deadline shorter than the monitoring timer converts a diagnosable ``0xC0..`` into a
    bare silence, which is why the client checks the ordering at construction.
    """

    expires_at: Nanos
    total_s: float
    clock: Clock

    @classmethod
    def after(cls, seconds: float, *, clock: Clock) -> Deadline:
        """A deadline ``seconds`` from now. Refuses a non-positive budget."""
        if not seconds > 0.0:
            raise SlmpConfigurationError(
                f"a deadline of {seconds!r} s gives the PLC no time to answer at all. "
                f"The fastest complete transaction measured on FX5U-32MT/DS fw 1.065 "
                f"was 3.99 ms (UDP) / 4.35 ms (TCP)."
            )
        return cls(
            expires_at=Nanos(clock() + int(seconds * _NS_PER_S)),
            total_s=float(seconds),
            clock=clock,
        )

    def remaining_s(self) -> float:
        """Seconds left, negative once the deadline has passed. Never clamped."""
        return (self.expires_at - self.clock()) / _NS_PER_S

    def expired(self) -> bool:
        return self.clock() >= self.expires_at

    def elapsed_s(self) -> float:
        """How long has been spent, for the message on a deadline that expired."""
        return self.total_s - self.remaining_s()


# ---------------------------------------------------------------------------
# The structural protocols the transport takes instead of importing wire
# ---------------------------------------------------------------------------


class Reassembler(Protocol):
    """Whatever turns received units into a message. ``ResponseAccumulator`` does.

    Two members, and neither of them mentions a frame. ``bytes_needed`` is how many
    more wire units the message wants -- the transport asks the socket for **at most**
    that many, which is what stops a TCP read from swallowing the head of the next
    message -- and ``feed`` hands over what arrived.
    """

    @property
    def bytes_needed(self) -> int: ...

    def feed(self, data: bytes, /) -> None: ...


def accept_any(datagram: bytes, /) -> bool:
    """The matcher for a connection that cannot correlate: take the next datagram.

    Correct only while exactly one request is in flight, which is why 3E/UDP is
    refused any in-flight depth above 1 (:class:`aslmp.transport.udp.UdpTransport`).
    """
    return True


@final
@dataclass(frozen=True, slots=True)
class Correlation:
    """How the layer above recognises its own response, without the transport knowing.

    ``matches`` is asked of each received datagram. ``label`` is carried only so that
    :class:`~aslmp.errors.SlmpDatagramLostError` can name *which* request vanished; the
    transport never interprets it. In practice it is the 4E serial No., and it is
    ``None`` on 3E -- which is exactly why 3E/UDP pipelining is never offered.
    """

    matches: Callable[[bytes], bool] = accept_any
    label: int | None = None


class TransportObserver(Protocol):
    """The two things only a transport can witness, reported to whoever owns identity.

    The transport does not know the connection id and does not own ``generation``, so
    it cannot build a :class:`~aslmp.observability.ConnectionEvent`. It reports the
    fact; :class:`aslmp.connection.Connection` counts it, bumps the generation for a
    rebind, and emits the typed event.
    """

    def datagram_dropped(
        self, *, reason: str, nbytes: int, source: tuple[str, int] | None
    ) -> None: ...

    def socket_rebound(
        self,
        *,
        previous_local: tuple[str, int],
        local: tuple[str, int],
        reason: str,
    ) -> None: ...


@final
class _NullObserver:
    """The default observer: counts nothing, because nobody asked to be told."""

    __slots__ = ()

    def datagram_dropped(
        self, *, reason: str, nbytes: int, source: tuple[str, int] | None
    ) -> None:
        return None

    def socket_rebound(
        self,
        *,
        previous_local: tuple[str, int],
        local: tuple[str, int],
        reason: str,
    ) -> None:
        return None


NULL_OBSERVER: Final[TransportObserver] = _NullObserver()


# ---------------------------------------------------------------------------
# Buffers and results
# ---------------------------------------------------------------------------


@final
class RecvBuffer:
    """One reusable ``bytearray`` and its ``memoryview``, for ``recv_into``.

    The steady state of a control loop must not allocate a receive buffer per cycle.
    :meth:`window` hands out a writable slice of the existing buffer and only allocates
    when a message wants more than has ever been wanted before -- which for a fixed
    read is never after the first one.
    """

    __slots__ = ("_buf", "_view")

    def __init__(self, capacity: int = DEFAULT_BUFFER_CAPACITY) -> None:
        if capacity <= 0:
            raise SlmpConfigurationError(
                f"a receive buffer of {capacity} byte(s) cannot hold a response; the "
                f"smallest 3E/binary response frame is 11 bytes."
            )
        self._buf = bytearray(capacity)
        self._view = memoryview(self._buf)

    @property
    def capacity(self) -> int:
        return len(self._buf)

    def window(self, nbytes: int) -> memoryview:
        """A writable view of exactly ``nbytes``, growing the buffer only if it must."""
        if nbytes <= 0:
            raise SlmpConfigurationError(
                f"a read window of {nbytes} byte(s) is not a read. The transport asks "
                f"for reassembler.bytes_needed, which is positive while a message is "
                f"incomplete and zero when the loop should already have stopped."
            )
        if nbytes > len(self._buf):
            self._view.release()
            self._buf = bytearray(max(nbytes, len(self._buf) * 2))
            self._view = memoryview(self._buf)
        return self._view[:nbytes]


@final
@dataclass(frozen=True, slots=True)
class WireResult:
    """What one exchange did on the wire. No frame, no end code, no interpretation.

    ``chunks`` is the per-``recv`` record; the caller's
    :class:`~aslmp.timing.TimingBuilder` holds the same stamps, taken at the moment
    each chunk landed rather than when the coroutine that owns it woke up.
    """

    sent: bool
    responded: bool
    bytes_sent: int
    bytes_received: int
    chunks: tuple[Chunk, ...]


@final
@dataclass(frozen=True, slots=True)
class Binding:
    """The two ends of an open socket.

    ``local`` is carried because a silent socket rebuild -- a reconnect nobody
    announced, or a UDP rebind -- is visible in the local port and nowhere else.
    """

    peer: tuple[str, int]
    local: tuple[str, int]


class Transport(Protocol):
    """Bytes, deadlines and a :class:`Reassembler`. There is no ``send``.

    ``exchange`` writes and reads as one operation, which is what makes "two writes in
    a row" inexpressible rather than merely discouraged -- the measured TCP coalescing
    corruption is the only failure on our bench that produces plausible wrong data with
    end code ``0x0000`` and, on 3E, no correlation field that could catch it.
    """

    @property
    def kind(self) -> TransportKind: ...

    @property
    def peer(self) -> tuple[str, int]: ...

    @property
    def is_open(self) -> bool: ...

    @property
    def binding(self) -> Binding | None: ...

    @property
    def max_in_flight(self) -> int: ...

    @property
    def transactions_completed(self) -> int: ...

    def attach_observer(self, observer: TransportObserver, /) -> None:
        """Tell the transport where to report a dropped datagram or a rebind.

        Called by :class:`aslmp.connection.Connection` on itself at construction, which
        is the only way the two can be wired: the transport is built first (it is what
        the connection is *for*), so the observer cannot be a constructor argument of
        the thing that has to exist before the observer does.
        """
        ...

    async def open(self, deadline: Deadline) -> Binding: ...

    async def exchange(
        self,
        request: bytes | memoryview,
        reassembler: Reassembler,
        deadline: Deadline,
        timing: TimingBuilder,
        *,
        expect_response: bool = True,
        correlation: Correlation | None = None,
    ) -> WireResult: ...

    async def close(self) -> None: ...


# ---------------------------------------------------------------------------
# Graft G3 -- the computed timeout
# ---------------------------------------------------------------------------


def timeout_error(
    *,
    deadline: Deadline,
    bytes_received: int,
    completed_transactions: int,
    peer: tuple[str, int],
    kind: TransportKind,
    where: str,
) -> SlmpTimeoutError:
    """Silence, explained from what was observed (graft G3, DESIGN section 3.4).

    The ordering is not a fixed list. Three observations give three orderings, and the
    two that matter are both measured on FX5U-32MT/DS fw 1.065 and both look identical
    from a socket:

    * **zero bytes on a connection that has never completed a transaction** -- the
      per-connection facts are all still unproven, and a coding mismatch answers
      ``0xC06F`` by saying nothing at all, so ``CODING_MISMATCH`` leads;
    * **partial bytes** -- an *overstated* request length field leaves the CPU blocked
      waiting for bytes that will never come, which is indistinguishable from a dead
      PLC, so ``REQUEST_LENGTH_OVERSTATED`` leads.

    :func:`aslmp.errors.routing.timeout_causes` owns the ordering; this function owns
    the message and the numbers, so that there is exactly one implementation of each.
    """
    causes = timeout_causes(
        bytes_received=bytes_received,
        completed_transactions=completed_transactions,
    )
    host, port = peer
    arrived = (
        "nothing arrived"
        if bytes_received == 0
        else f"{bytes_received} byte(s) arrived and then stopped"
    )
    return SlmpTimeoutError(
        f"no response from {host}:{port} over {kind.value} within "
        f"{deadline.total_s:g} s while {where}: {arrived}.",
        likely_causes=causes,
        bytes_received=bytes_received,
        deadline_s=deadline.total_s,
    )
