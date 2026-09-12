"""Layer 4 -- one connection: a transport bound to a frame format, a codec and a route.

This is where the transport's bytes become a message and where the connection's *state*
lives. It knows nothing about devices, profiles or commands; the client above it does.
It owns four things, and each of them is a decision the hardware forced:

**The gate, and therefore the absence of a public ``send``.** Bytes reach a socket only
through :class:`Txn`, a capability token obtainable only from
:meth:`Connection.transaction`, exclusive per connection, and burnt after one exchange.
Measured on **MELSEC iQ-F FX5U-32MT/DS fw 1.065**: two TCP requests written before the
first response is read return ONE response, for the LAST request, with end code
``0x0000``. On 3E there is no serial No., so it is undetectable wrong data reported as
success. A rule in a docstring does not survive an ``asyncio.gather``; a missing method
does.

**``generation``.** It bumps on every reopen and every UDP source-port rebind. It is the
anti-lie field: a latency spike caused by a socket that was quietly rebuilt cannot be
mistaken for a slow PLC, because the transaction record says which socket it ran on.

**The sticky ``FAILED`` state.** Any failure mid-message closes the socket and the
connection stays failed. There is no automatic reconnection anywhere in this package --
an unread tail on a socket is exactly how ``Esmool`` hands the previous transaction's
bytes to the next one, and a client that reconnects on its own hides both the failure
and the gap in the data. :class:`aslmp.resilience.Supervisor` is the only thing that
reconnects, its backoff policy has no default, and every attempt is an event.

**The serial allocator.** 4E serials are allocated here rather than by the caller,
because the same number has to appear in three places at once -- the request frame, the
response accumulator's ``expect_serial``, and (on UDP) the
:class:`~aslmp.transport.base.Correlation` that routes a datagram to its transaction.
Handing the caller a number and trusting it to use it three times is how they drift.

.. rubric:: What this module deliberately does not do

It does not perform the ``0x0619`` handshake. That needs :mod:`aslmp.commands`, which is
the client's business (DESIGN section 4.5); the connection reaches ``READY`` only when
the client calls :meth:`Connection.confirm` with the proof. Between :meth:`Connection.open`
and that call the state is ``OPEN``: the socket exists and nothing has been proven.
"""

from __future__ import annotations

import contextlib
import enum
from contextlib import asynccontextmanager
from dataclasses import dataclass
from itertools import count
from typing import TYPE_CHECKING, Final, NoReturn, Protocol, final

from aslmp._clock import DEFAULT_CLOCK
from aslmp.errors import (
    NO_DIAGNOSTICS,
    Diagnostics,
    OutcomeUnknownReason,
    SlmpConnectionEntryBusyError,
    SlmpConnectionLostError,
    SlmpDatagramLostError,
    SlmpError,
    SlmpNotConnectedError,
    SlmpNotSentError,
    SlmpOutcomeUnknownError,
    SlmpProtocolError,
    SlmpSinkError,
    SlmpTimeoutError,
    SlmpUsageError,
)
from aslmp.errors.routing import protocol_error_for
from aslmp.observability import (
    Connected,
    ConnectFailed,
    Connecting,
    ConnectionEvent,
    ConnectionFailed,
    Counters,
    DatagramDropped,
    Disconnected,
    EventSink,
    SocketRebound,
)
from aslmp.timing import Clock, Nanos, TimingBuilder, TransactionTiming
from aslmp.transport.base import Correlation, Deadline, TransportKind
from aslmp.transport.inflight import Concurrency, GateSlot, TransactionGate
from aslmp.wire.frames import FrameFormat
from aslmp.wire.raw import RawResponse, SlmpFrameError
from aslmp.wire.reader import ResponseAccumulator
from aslmp.wire.route import Route

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import AsyncIterator

    from aslmp.timing import Transaction
    from aslmp.transport.base import Binding, Transport
    from aslmp.wire.codec import Codec

__all__ = [
    "Connection",
    "ConnectionInfo",
    "ConnectionState",
    "IdentitySummary",
    "SerialAllocator",
    "Txn",
]

_SERIAL_MAX: Final = 0xFFFF

_connection_ids = count(1)


class ConnectionState(enum.Enum):
    """Where a connection is. ``FAILED`` is sticky and is never left implicitly.

    ``NEW``
        Constructed; no socket has ever been opened.
    ``CONNECTING``
        A socket open is in progress.
    ``OPEN``
        The socket exists and **nothing has been proven**. ``socket.connect()``
        demonstrably lies on this hardware: a second connection to a one-entry SLMP
        configuration completes and is then FINed. Transactions are permitted here
        because the ``0x0619`` handshake is itself a transaction.
    ``READY``
        The handshake echoed byte for byte, so the entry was free, the coding is right,
        the frame format is accepted, the route bytes are right and the PLC answered.
    ``FAILED``
        Something failed mid-message and the socket is closed. Sticky. Only an explicit
        :meth:`Connection.reopen` -- or a ``Supervisor``, which is a separate object
        with a mandatory backoff policy -- leaves this state.
    ``CLOSED``
        Closed on purpose.
    """

    NEW = "new"
    CONNECTING = "connecting"
    OPEN = "open"
    READY = "ready"
    FAILED = "failed"
    CLOSED = "closed"

    @property
    def usable(self) -> bool:
        """True where a transaction may be submitted."""
        return self in (ConnectionState.OPEN, ConnectionState.READY)


class IdentitySummary(Protocol):
    """What a connection may record about the CPU that answered ``0x0101``.

    Structural, for the same reason :class:`aslmp.timing.WireOption` is: ``CpuIdentity``
    lives at Layer 2 alongside :mod:`aslmp.commands`, and this module has no business
    naming a command. ``aslmp.identity.CpuIdentity`` satisfies it.
    """

    @property
    def model(self) -> str: ...

    @property
    def model_code(self) -> int: ...


@final
@dataclass(frozen=True, slots=True)
class ConnectionInfo:
    """The record of one established connection (DESIGN section 2.3).

    ``local`` is carried because a socket that was quietly rebuilt is visible in the
    local port and nowhere else. ``handshake`` is the ``0x0619`` transaction with its
    full timing -- the connect-time latency baseline, ~7 ms on FX5U-32MT/DS fw 1.065 --
    and is ``None`` only when the caller chose ``Handshake.NONE`` and therefore chose to
    trust ``connect()``.
    """

    connection_id: str
    generation: int
    peer: tuple[str, int]
    local: tuple[str, int]
    established_at: Nanos
    handshake: Transaction | None = None
    identity: IdentitySummary | None = None


@final
class SerialAllocator:
    """4E serial numbers, 1 through 0xFFFF, wrapping. Never 0.

    Zero is skipped so that a zeroed buffer, a defaulted field and a serial this library
    actually issued are three distinguishable things. The serial is the only in-band
    defence against the measured coalescing corruption, and it ships labelled
    non-contractual: our FX5U accepts 4E and echoes the serial correctly on a connection
    entry configured for 3E, which contradicts JY997D56001-K section 2.1.
    """

    __slots__ = ("_next",)

    def __init__(self, start: int = 1) -> None:
        if not 1 <= start <= _SERIAL_MAX:
            raise SlmpUsageError(
                f"a 4E serial No. is a 16-bit field and this allocator never issues 0; "
                f"start={start} is outside 1..0x{_SERIAL_MAX:04X}."
            )
        self._next = start

    def next(self) -> int:
        value = self._next
        self._next = 1 if value >= _SERIAL_MAX else value + 1
        return value

    @property
    def peek(self) -> int:
        return self._next


@final
class Txn:
    """A capability token for exactly one request/response exchange.

    Not constructible by callers: the only source is
    :meth:`Connection.transaction`, an async context manager. The token holds the
    connection's single in-flight slot for the body of that ``with``, and
    :meth:`exchange` burns it -- so "two writes in a row" is not a discouraged pattern,
    it is a thing this API cannot express. That is the correct weight for the only
    failure on the bench that produces plausible wrong data with end code ``0x0000`` and
    no correlation field to catch it.
    """

    __slots__ = (
        "_command",
        "_conn",
        "_deadline",
        "_finished",
        "_prebuilt",
        "_sent",
        "_serial",
        "_slot",
        "_subcommand",
        "_timing",
    )

    def __init__(
        self,
        connection: Connection,
        slot: GateSlot,
        *,
        timing: TimingBuilder,
        deadline: Deadline,
        serial: int | None,
        command: int | None,
        subcommand: int | None,
    ) -> None:
        self._conn = connection
        self._slot = slot
        self._timing = timing
        self._deadline = deadline
        self._serial = serial
        self._command = command
        self._subcommand = subcommand
        self._sent = False
        self._finished = False
        self._prebuilt = False

    # -- what the caller needs to build a frame ------------------------------

    @property
    def sequence(self) -> int:
        return self._slot.sequence

    @property
    def serial(self) -> int | None:
        """The 4E serial for this exchange, or ``None`` on 3E.

        Allocated by the connection, not by the caller, because the same number has to
        go into the request frame, the response accumulator and the UDP correlation.
        """
        return self._serial

    @property
    def generation(self) -> int:
        return self._conn.generation

    @property
    def deadline(self) -> Deadline:
        return self._deadline

    @property
    def timing(self) -> TimingBuilder:
        return self._timing

    @property
    def sent(self) -> bool:
        """Whether the request may have reached the OS.

        Conservative on purpose. It is ``False`` only where the transport could
        *prove* nothing was written -- ``SlmpNotSentError`` and the pre-send usage
        refusals. Everywhere else the answer is "may have happened", because for a
        state-changing command the difference between those two is the difference
        between retrying and reading the device back (graft G8).
        """
        return self._sent

    @property
    def used(self) -> bool:
        return self._slot.used

    @property
    def command(self) -> int | None:
        """The command this transaction was opened for, if the caller named it.

        Carried so that the transaction record and the gate's refusal message can both
        say *which* request is in flight, without either of them re-deriving it from
        bytes.
        """
        return self._command

    @property
    def subcommand(self) -> int | None:
        return self._subcommand

    @property
    def prebuilt(self) -> bool:
        """Whether the request frame came from a bound plan rather than being built now.

        A ``BlockPlan`` prebuilds its ``0403`` frame at bind time and patches only the
        4E serial in place, so ``encode_ns`` for a prebuilt frame is a different number
        from one that was assembled this cycle, and the record says which it was.
        """
        return self._prebuilt

    @property
    def finished(self) -> bool:
        """True once the exchange is over, successfully or not.

        A token that sent bytes and is *not* finished when its ``with`` block ends left
        a response on the socket, which is why the context manager fails the connection
        there rather than letting the next transaction read it.
        """
        return self._finished

    # -- the exchange --------------------------------------------------------

    async def exchange(
        self,
        request: bytes | memoryview,
        reassembler: ResponseAccumulator,
        *,
        mutates: bool,
        prebuilt: bool = False,
    ) -> tuple[RawResponse, TransactionTiming]:
        """Write the request, read exactly one response, and burn this token.

        Returns the parsed response and the timing *as of reception*: ``decoded_at`` is
        not stamped yet, because the caller has not decoded anything yet. Call
        :meth:`decoded` after turning the payload into values to get the final record.
        An end-code failure never reaches :meth:`decoded`, which is why a
        :class:`~aslmp.timing.TransactionTiming` for one has no ``decode_ns`` -- it did
        not have one.

        A failure after the bytes went out is re-raised as
        :class:`~aslmp.errors.SlmpOutcomeUnknownError` when ``mutates`` is true. The
        caller does not repeat that wrapping: ``sent`` is a transport fact and this is
        the only place that knows it exactly.
        """
        self._slot.use()
        self._prebuilt = prebuilt
        connection = self._conn
        connection.require_usable("exchange")
        self._timing.encoded()
        try:
            await connection.transport.exchange(
                request,
                reassembler,
                self._deadline,
                self._timing,
                expect_response=True,
                correlation=self._correlation(),
            )
            self._sent = True
            response = reassembler.take()
        except SlmpFrameError as exc:
            self._sent = True
            self._finished = True
            public = protocol_error_for(exc, diagnostics=self._diagnostics())
            public.__cause__ = exc
            await self._fail(public, mutates)
        except (SlmpNotSentError, SlmpUsageError, SlmpNotConnectedError) as exc:
            self._sent = False
            self._finished = True
            await connection.note_failure(exc, close=not isinstance(exc, SlmpUsageError))
            raise
        except SlmpError as exc:
            self._sent = True
            self._finished = True
            await self._fail(exc, mutates)
        except BaseException:
            # Cancellation, most likely. The request went out and the response did not
            # come in: the socket is unusable and the connection goes sticky FAILED.
            self._sent = True
            self._finished = True
            await connection.note_failure(
                SlmpConnectionLostError(
                    "the transaction was cancelled after its request went out; the "
                    "response was never read and the socket cannot be reused."
                ),
                close=True,
            )
            raise
        self._finished = True
        timing = self._timing.build()
        connection.note_success(timing)
        return response, timing

    async def exchange_without_response(
        self,
        request: bytes | memoryview,
        *,
        mutates: bool = True,
    ) -> TransactionTiming:
        """Send a request whose *absence* of a response is the expected outcome.

        This exists for exactly one command -- ``0x1006`` Remote Reset, where the CPU
        resets before it can answer -- and it burns the token like any other exchange,
        so it is not a general ``send``. Everything else must read its response: a
        request whose answer is left on the socket is how the *next* transaction reads
        the previous one's bytes as fresh data.

        **The socket does not survive this call.** Whatever was sent, nothing read the
        answer, so nothing can prove there is not one waiting; a connection kept in
        service would hand those bytes to the next transaction as fresh data with end
        code ``0x0000``, and on 3E there is no serial No. that would ever reveal it.
        :meth:`Connection.retire_after_silent_exchange` therefore closes it and the
        connection goes sticky ``FAILED``, recoverable only by an explicit
        ``reopen(reason=...)``. For ``0x1006`` that costs nothing: the CPU is resetting
        and the connection was going with it either way
        (:meth:`aslmp.client.RemoteControl.reset` closes the client straight after).
        """
        self._slot.use()
        connection = self._conn
        connection.require_usable("exchange")
        self._timing.encoded()
        try:
            await connection.transport.exchange(
                request,
                _NOTHING_EXPECTED,
                self._deadline,
                self._timing,
                expect_response=False,
                correlation=self._correlation(),
            )
        except (SlmpNotSentError, SlmpUsageError, SlmpNotConnectedError) as exc:
            self._sent = False
            self._finished = True
            await connection.note_failure(exc, close=not isinstance(exc, SlmpUsageError))
            raise
        except SlmpError as exc:
            self._sent = True
            self._finished = True
            await self._fail(exc, mutates)
        self._sent = True
        self._finished = True
        timing = self._timing.build()
        await connection.retire_after_silent_exchange()
        return timing

    def decoded(self) -> TransactionTiming:
        """Stamp ``decoded_at`` and return the final timing record."""
        self._timing.decoded()
        return self._timing.build()

    # -- internals -----------------------------------------------------------

    def _correlation(self) -> Correlation:
        return self._conn.correlation_for(self._serial)

    def _diagnostics(self) -> Diagnostics:
        return Diagnostics(requested_route=self._conn.route)

    async def _fail(self, exc: SlmpError, mutates: bool) -> NoReturn:
        """Record the failure, close the socket, and raise what the caller should see.

        For a read that is the failure itself. For a **state-changing** command whose
        bytes went out it is :class:`~aslmp.errors.SlmpOutcomeUnknownError` with the
        failure as its ``__cause__``, because "provably did not happen" and "may have
        happened" have different recoveries and only one of them is safe to retry
        blindly (graft G8). The classification is driven by ``Command.mutates``, so a
        new command cannot forget the distinction.
        """
        await self._conn.note_failure(exc, close=True)
        if not mutates:
            raise exc
        raise SlmpOutcomeUnknownError(
            f"a state-changing request failed after its bytes went out: {exc.headline()}",
            reason=_unknown_reason(exc),
            sent=True,
            diagnostics=exc.diagnostics,
        ) from exc

    def __repr__(self) -> str:
        serial = "-" if self._serial is None else f"0x{self._serial:04X}"
        return (
            f"Txn(sequence={self.sequence}, serial={serial}, used={self._slot.used}, "
            f"sent={self._sent})"
        )


def _unknown_reason(exc: SlmpError) -> OutcomeUnknownReason:
    """Why the outcome cannot be known, from the failure that produced it."""
    if isinstance(exc, SlmpTimeoutError | SlmpDatagramLostError):
        return OutcomeUnknownReason.TIMEOUT
    if isinstance(exc, SlmpConnectionLostError | SlmpConnectionEntryBusyError):
        return OutcomeUnknownReason.CONNECTION_LOST
    if isinstance(exc, SlmpProtocolError):
        return OutcomeUnknownReason.RESPONSE_CORRUPT
    return OutcomeUnknownReason.CONNECTION_LOST


@final
class _NothingExpected:
    """The reassembler for an exchange that expects no response at all."""

    __slots__ = ()

    @property
    def bytes_needed(self) -> int:
        return 0

    def feed(self, data: bytes, /) -> None:
        raise SlmpProtocolError(
            f"{len(data)} byte(s) arrived for a request that expects no response. "
            f"Remote Reset is the only such request, and a CPU that answers it has not "
            f"reset."
        )


_NOTHING_EXPECTED: Final = _NothingExpected()


@final
class Connection:
    """One transport, bound to a frame format, a codec and a route.

    Construct it with an already-built transport -- :class:`~aslmp.transport.tcp.TcpTransport`
    or :class:`~aslmp.transport.udp.UdpTransport`. The in-flight capacity comes from the
    transport itself (``max_in_flight``), because it is a property of the medium: TCP
    coalesces and is therefore always 1, and UDP pipelines only on 4E.
    """

    __slots__ = (
        "_capture_frames",
        "_clock",
        "_codec",
        "_connection_id",
        "_counters",
        "_established_at",
        "_events",
        "_frame",
        "_gate",
        "_generation",
        "_info",
        "_prev_received_at",
        "_route",
        "_sequences",
        "_serials",
        "_sink_failed_this_generation",
        "_state",
        "_timeout",
        "_transport",
    )

    def __init__(
        self,
        transport: Transport,
        *,
        frame: FrameFormat,
        codec: Codec,
        route: Route = Route.OWN_STATION,
        timeout: float = 3.0,
        concurrency: Concurrency = Concurrency.STRICT,
        clock: Clock = DEFAULT_CLOCK,
        connection_id: str | None = None,
        counters: Counters | None = None,
        events: EventSink | None = None,
        serials: SerialAllocator | None = None,
        capture_frames: bool = False,
    ) -> None:
        self._transport = transport
        self._frame = frame
        self._codec = codec
        self._route = route
        self._timeout = timeout
        self._clock = clock
        self._counters = counters if counters is not None else Counters()
        self._events = events
        self._capture_frames = capture_frames
        self._connection_id = (
            connection_id if connection_id is not None else f"conn-{next(_connection_ids)}"
        )
        self._serials = serials if serials is not None else SerialAllocator()
        self._gate = TransactionGate(
            clock=clock,
            mode=concurrency,
            capacity=transport.max_in_flight,
            counters=self._counters,
        )
        # The transport was built before this object existed, so the observer link is
        # made here rather than in its constructor. Without it a UDP rebind would happen
        # with nothing to bump the generation, and a fresh source port would be
        # invisible in every transaction record taken afterwards.
        transport.attach_observer(self)
        self._sequences = count(1)
        self._generation = 0
        self._state = ConnectionState.NEW
        self._prev_received_at: Nanos | None = None
        self._established_at: Nanos = Nanos(0)
        self._info: ConnectionInfo | None = None
        self._sink_failed_this_generation = False

    # -- inspection ----------------------------------------------------------

    @property
    def connection_id(self) -> str:
        return self._connection_id

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def generation(self) -> int:
        """Bumps on every reopen and every UDP source-port rebind. The anti-lie field."""
        return self._generation

    @property
    def counters(self) -> Counters:
        return self._counters

    @property
    def frame(self) -> FrameFormat:
        return self._frame

    @property
    def codec(self) -> Codec:
        return self._codec

    @property
    def route(self) -> Route:
        return self._route

    @property
    def transport(self) -> Transport:
        return self._transport

    @property
    def kind(self) -> TransportKind:
        return self._transport.kind

    @property
    def gate(self) -> TransactionGate:
        return self._gate

    @property
    def info(self) -> ConnectionInfo | None:
        return self._info

    @property
    def binding(self) -> Binding | None:
        return self._transport.binding

    @property
    def prev_received_at(self) -> Nanos | None:
        """The previous response's last chunk: the start of ``host_gap_ns``."""
        return self._prev_received_at

    # -- lifecycle -----------------------------------------------------------

    async def open(self, *, timeout: float | None = None) -> Binding:
        """Open the socket. Proves nothing; the client's handshake does that.

        Legal only from ``NEW``. ``FAILED`` is sticky, and reopening it is
        :meth:`reopen`, which bumps the generation -- so a reconnect can never be
        mistaken for the connection that came before it.
        """
        if self._state is not ConnectionState.NEW:
            raise SlmpNotConnectedError(
                f"open() is legal only on a new connection; this one is "
                f"{self._state.value}. Reconnection is never implicit in this library: "
                f"call reopen(reason=...) and it will bump the generation, or use a "
                f"Supervisor, whose backoff policy has no default.",
                reason=self._state.value,
            )
        return await self._open(timeout=timeout, attempt=1)

    async def reopen(self, *, reason: str, timeout: float | None = None) -> Binding:
        """Explicitly rebuild the socket after a failure, bumping ``generation``.

        The generation bump is the point. Everything sampled through the old socket is
        attributable to it, and a supervisor that reconnects behind a control loop's
        back is why "the p99 moved" is otherwise unanswerable.
        """
        if self._state is ConnectionState.CLOSED:
            raise SlmpNotConnectedError(
                "this connection was closed on purpose; build a new one rather than "
                "resurrecting it.",
                reason="closed",
            )
        await self._transport.close()
        self._generation += 1
        self._sink_failed_this_generation = False
        self._counters.reconnects += 1
        self._prev_received_at = None
        self._info = None
        return await self._open(timeout=timeout, attempt=self._generation + 1, why=reason)

    async def _open(
        self, *, timeout: float | None, attempt: int, why: str = ""
    ) -> Binding:
        budget = timeout if timeout is not None else self._timeout
        deadline = Deadline.after(budget, clock=self._clock)
        self._state = ConnectionState.CONNECTING
        peer = _peer_of(self._transport)
        try:
            self._emit(
                Connecting(
                    connection_id=self._connection_id,
                    generation=self._generation,
                    at=Nanos(self._clock()),
                    peer=peer,
                    transport=self._transport.kind.value,
                    attempt=attempt,
                )
            )
            binding = await self._transport.open(deadline)
        except BaseException as exc:
            # Everything that can go wrong here leaves the connection FAILED and the
            # socket closed -- including a listener that raised, which is a connect
            # failure like any other and must not leave the state machine stranded in
            # CONNECTING with nothing able to move it.
            self._state = ConnectionState.FAILED
            self._counters.connect_failures += 1
            if isinstance(exc, SlmpConnectionEntryBusyError):
                self._counters.entry_busy += 1
            with contextlib.suppress(SlmpSinkError):
                self._emit(
                    ConnectFailed(
                        connection_id=self._connection_id,
                        generation=self._generation,
                        at=Nanos(self._clock()),
                        peer=peer,
                        reason=why or _first_line(exc),
                        error_type=type(exc).__name__,
                    )
                )
            await self._transport.close()
            raise
        self._state = ConnectionState.OPEN
        self._established_at = Nanos(self._clock())
        return binding

    def confirm(
        self,
        *,
        handshake: Transaction | None = None,
        identity: IdentitySummary | None = None,
    ) -> ConnectionInfo:
        """``OPEN`` -> ``READY``: the handshake proved it. Emits :class:`Connected`.

        The proof itself is the client's ``0x0619`` Self Test with a byte-for-byte echo
        compare (DESIGN section 4.5 step 3). This method exists so that the state
        machine records who proved what, rather than a connection reporting itself
        healthy because a socket call returned.
        """
        if self._state is not ConnectionState.OPEN:
            raise SlmpNotConnectedError(
                f"confirm() is legal only from OPEN; this connection is "
                f"{self._state.value}.",
                reason=self._state.value,
            )
        binding = self._transport.binding
        peer = binding.peer if binding is not None else _peer_of(self._transport)
        local = binding.local if binding is not None else ("", 0)
        self._state = ConnectionState.READY
        self._counters.connects += 1
        info = ConnectionInfo(
            connection_id=self._connection_id,
            generation=self._generation,
            peer=peer,
            local=local,
            established_at=self._established_at,
            handshake=handshake,
            identity=identity,
        )
        self._info = info
        self._emit(
            Connected(
                connection_id=self._connection_id,
                generation=self._generation,
                at=Nanos(self._clock()),
                peer=peer,
                local=local,
                handshake_ns=None if handshake is None else handshake.timing.wire_ns,
                model=None if identity is None else identity.model,
                model_code=None if identity is None else identity.model_code,
            )
        )
        return info

    async def aclose(self) -> None:
        """Close on purpose. Idempotent; emits :class:`Disconnected` once."""
        if self._state is ConnectionState.CLOSED:
            return
        await self._transport.close()
        self._state = ConnectionState.CLOSED
        self._info = None
        self._counters.disconnects += 1
        self._emit(
            Disconnected(
                connection_id=self._connection_id,
                generation=self._generation,
                at=Nanos(self._clock()),
                reason="closed by the caller",
                expected=True,
            )
        )

    # -- failure -------------------------------------------------------------

    def require_usable(self, what: str) -> None:
        """Raise unless a transaction may be submitted right now."""
        if self._state.usable:
            return
        raise SlmpNotConnectedError(
            f"cannot {what}: this connection is {self._state.value}. The FAILED state "
            f"is sticky and the socket is closed -- there is no automatic reconnection "
            f"anywhere in this package, and a transaction submitted during a supervised "
            f"reconnect raises immediately rather than waiting for one.",
            reason=self._state.value,
        )

    async def retire_after_silent_exchange(self) -> None:
        """Close the socket after a request whose response was deliberately never read.

        The one connection-level consequence of ``expect_response=False``. Nothing
        failed, so no failure is counted and no ``ConnectionFailed`` is emitted -- but the
        socket is finished, because this process sent a request and did not read the
        answer, and it has no way to know whether there is one queued behind it. Keeping
        it ``READY`` is how the next transaction decodes the previous request's response
        as its own, with end code ``0x0000`` and, on 3E, no serial No. to catch it.

        The state is ``FAILED`` rather than ``CLOSED`` for one reason: ``FAILED`` is the
        state an explicit ``reopen(reason=...)`` can leave, and ``CLOSED`` deliberately
        cannot ("build a new one rather than resurrecting it"). A caller who used the
        escape hatch on purpose gets to reconnect; nobody gets to keep transacting.

        Idempotent, and never raises: :meth:`Transport.close` does not, and a connection
        that is already ``CLOSED`` or ``FAILED`` is left where it is.
        """
        await self._transport.close()
        if self._state in (ConnectionState.CLOSED, ConnectionState.FAILED):
            return
        self._state = ConnectionState.FAILED
        self._info = None
        self._counters.disconnects += 1
        with contextlib.suppress(SlmpSinkError):
            self._emit(
                Disconnected(
                    connection_id=self._connection_id,
                    generation=self._generation,
                    at=Nanos(self._clock()),
                    reason=(
                        "a request was sent with expect_response=False; the socket is "
                        "retired rather than reused, because nothing read the answer "
                        "and nothing can prove there is not one waiting"
                    ),
                    expected=True,
                )
            )

    async def note_failure(self, exc: BaseException, *, close: bool) -> None:
        """Count a failure and, if it touched the wire, go sticky ``FAILED``.

        The socket is closed rather than left half-read. An unread tail is exactly how
        the *next* transaction reads the previous one's bytes as fresh data, and on 3E
        there is no serial No. that would ever reveal it.
        """
        self._count_failure(exc)
        if not close:
            return
        await self._transport.close()
        if self._state in (ConnectionState.CLOSED, ConnectionState.FAILED):
            return
        self._state = ConnectionState.FAILED
        # A broken event sink must not replace the PLC failure that is on its way to the
        # caller with a report about the sink: that would hide the actual fault behind a
        # callback bug. It is already counted in counters.sink_errors, and the next
        # successful emission on this generation reports it.
        with contextlib.suppress(SlmpSinkError):
            self._emit(
                ConnectionFailed(
                    connection_id=self._connection_id,
                    generation=self._generation,
                    at=Nanos(self._clock()),
                    reason=_first_line(exc),
                    error_type=type(exc).__name__,
                )
            )

    def _count_failure(self, exc: BaseException) -> None:
        counters = self._counters
        if isinstance(exc, SlmpTimeoutError):
            counters.timeouts += 1
        elif isinstance(exc, SlmpConnectionEntryBusyError):
            counters.entry_busy += 1
        elif isinstance(exc, SlmpConnectionLostError):
            counters.connection_lost += 1
        elif isinstance(exc, SlmpProtocolError | SlmpFrameError):
            counters.protocol_errors += 1

    def note_success(self, timing: TransactionTiming) -> None:
        """Record the reception stamp that starts the next transaction's host gap."""
        self._prev_received_at = timing.received_at

    # -- the only path to a socket -------------------------------------------

    @asynccontextmanager
    async def transaction(
        self,
        *,
        command: int | None = None,
        subcommand: int | None = None,
        timeout: float | None = None,
    ) -> AsyncIterator[Txn]:
        """Hold the in-flight slot and hand out one single-use capability token.

        Under :attr:`~aslmp.transport.inflight.Concurrency.STRICT` a second concurrent
        call raises, naming the holder. Under ``SERIALIZE`` it waits, and the wait lands
        in ``queue_ns`` because the timing record is created *before* the gate is
        acquired -- a queue that does not report its own delay makes every latency
        number in this library a lie.
        """
        self.require_usable("start a transaction")
        timing = TimingBuilder(self._clock, prev_received_at=self._prev_received_at)
        sequence = next(self._sequences)
        async with self._gate.lease(
            sequence=sequence, command=command, subcommand=subcommand
        ) as slot:
            timing.gate_acquired()
            self.require_usable("start a transaction")
            token = Txn(
                self,
                slot,
                timing=timing,
                deadline=Deadline.after(
                    timeout if timeout is not None else self._timeout, clock=self._clock
                ),
                serial=self._serials.next() if self._frame.carries_serial else None,
                command=command,
                subcommand=subcommand,
            )
            try:
                yield token
            finally:
                if token.sent and not token.finished:
                    await self.note_failure(
                        SlmpConnectionLostError(
                            "a transaction sent its request and did not finish reading "
                            "the response. The socket is closed rather than left "
                            "half-read: an unread tail is how the next transaction "
                            "reads this one's bytes as fresh data, which is exactly "
                            "the defect this library is written against."
                        ),
                        close=True,
                    )

    def accumulator(self, serial: int | None) -> ResponseAccumulator:
        """A response accumulator bound to this connection's frame, codec and serial."""
        return ResponseAccumulator(self._frame, self.codec, expect_serial=serial)

    def correlation_for(self, serial: int | None) -> Correlation:
        """How a UDP datagram is recognised as the answer to *this* request.

        On 4E the serial No. is read straight out of the response's fixed prefix. On 3E
        there is nothing to read, so the matcher accepts the next datagram -- which is
        correct only because 3E is never given an in-flight depth above 1
        (:class:`~aslmp.transport.udp.UdpTransport` refuses it at construction).
        """
        if serial is None:
            return Correlation()
        frame = self._frame
        codec = self.codec

        def matches(datagram: bytes, /) -> bool:
            try:
                prefix = frame.read_prefix(datagram, codec, response=True)
            except SlmpFrameError:
                # Not a response frame at all, so it is nobody's answer. Refusing to
                # claim it is not a recovery: the datagram is still dropped and counted
                # by the transport, never delivered to a transaction that did not ask
                # for it.
                prefix = None
            return prefix is not None and prefix.serial == serial

        return Correlation(matches=matches, label=serial)

    # -- the transport observer ----------------------------------------------

    def datagram_dropped(
        self, *, reason: str, nbytes: int, source: tuple[str, int] | None
    ) -> None:
        """A datagram was discarded before it could become somebody's answer."""
        if reason == "foreign-source":
            self._counters.foreign_datagrams += 1
        else:
            self._counters.stale_datagrams += 1
        self._emit(
            DatagramDropped(
                connection_id=self._connection_id,
                generation=self._generation,
                at=Nanos(self._clock()),
                reason=reason,
                nbytes=nbytes,
                source=source,
            )
        )

    def socket_rebound(
        self, *, previous_local: tuple[str, int], local: tuple[str, int], reason: str
    ) -> None:
        """A UDP socket was rebound after a timeout. **The generation bumps here.**

        The old source port is dead, so the vanished request's answer can never be
        delivered to the next transaction. The generation bump is what makes the change
        of socket visible in every transaction record taken afterwards.
        """
        self._counters.socket_rebinds += 1
        self._generation += 1
        self._sink_failed_this_generation = False
        self._emit(
            SocketRebound(
                connection_id=self._connection_id,
                generation=self._generation,
                at=Nanos(self._clock()),
                previous_local=previous_local,
                local=local,
                reason=reason,
            )
        )

    # -- events --------------------------------------------------------------

    def _emit(self, event: ConnectionEvent) -> None:
        """Hand one event to the sink. A sink that raises is counted, then reported once.

        Once per generation, because a control loop whose event sink is broken should
        learn that exactly once and then keep controlling the plant, and because the
        alternative -- swallowing it -- is the silent recovery this library is written
        against.
        """
        sink = self._events
        if sink is None:
            return
        failure: Exception | None = None
        try:
            sink(event)
        except Exception as exc:
            failure = exc
        if failure is None:
            return
        self._counters.sink_errors += 1
        reported_before = self._sink_failed_this_generation
        self._sink_failed_this_generation = True
        if reported_before:
            # Counted, not swallowed: counters.sink_errors keeps rising and the tally
            # is in every MetricsSnapshot. Raising on every event instead would let one
            # broken callback take a plant off the network.
            return
        raise SlmpSinkError(
            f"the event sink raised on {event.kind}: "
            f"{type(failure).__name__}: {failure}. Counted in counters.sink_errors; "
            f"reported once per generation.",
            sink="on_event",
            diagnostics=NO_DIAGNOSTICS,
        ) from failure

    def __repr__(self) -> str:
        return (
            f"Connection({self._connection_id}, {self._transport!r}, "
            f"state={self._state.value}, generation={self._generation})"
        )


def _first_line(exc: BaseException) -> str:
    """The headline of a failure, for an event field that is one line wide."""
    text = str(exc.args[0]) if exc.args else type(exc).__name__
    return text.splitlines()[0]


def _peer_of(transport: Transport) -> tuple[str, int]:
    """The peer as the transport last resolved it, or as it was configured."""
    binding = transport.binding
    return binding.peer if binding is not None else transport.peer
