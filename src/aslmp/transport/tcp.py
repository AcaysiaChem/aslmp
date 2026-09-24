"""The TCP transport: active open, ``TCP_NODELAY``, and a length-driven read.

Four behaviours here are measured on **MELSEC iQ-F FX5U-32MT/DS, firmware 1.065**, and
each of them is the reason a line of this file exists.

**1. One connection per configured SLMP entry, and ``connect()`` lies about it.**
A second TCP connection to a one-entry configuration completes ``connect()`` normally
and *then* the CPU sends FIN: ``recv()`` returns 0 bytes before anything has been sent.
So :meth:`TcpTransport.open` performs a **non-blocking** EOF check immediately after
connect and before any write (graft G2). Non-blocking is the whole point of the repair:
a blocking probe with a fixed wait would be racy and would cost latency on every healthy
connect, while a non-blocking check costs nothing and can only produce a true positive.
If the FIN has already been delivered we name it precisely; if it has not, the
handshake's own zero-byte read catches it. The probe classifies; it never proves.

**2. Request coalescing corrupts silently.** Two requests written before the first
response is read return ONE response, for the LAST request, end code ``0x0000``. This
transport therefore has no ``send``: :meth:`TcpTransport.exchange` writes and reads as
one operation, and :class:`~aslmp.transport.inflight.TransactionGate` makes a second
concurrent call an error rather than a race.

**3. Segmentation is real.** One 1931-byte response of three identical trials arrived as
1460 bytes at 10.964 ms and 471 bytes at 14.010 ms -- the Ethernet MSS boundary. The read
loop is driven by ``reassembler.bytes_needed`` and never by a fixed ``recv(4096)``, every
chunk is stamped as it lands, and the receive time is the **last** chunk: stamping the
first would have reported 11.0 ms for a 14.0 ms transaction. Whether the split appears
depends on host scheduling, so a single ``recv`` passes two runs in three and fails in
production. (``pymcprotocol`` 0.3.0 ``type3e.py:148`` is exactly that single ``recv``.)

**4. Silence is the normal failure.** Wrong coding, wrong frame type, wrong transport and
an overstated request length all produce no answer at all. Every timeout raised here goes
through :func:`~aslmp.transport.base.timeout_error`, which orders the causes from what was
actually observed.

Nothing here retries, reconnects, or reads past what the message asked for. An unread
tail on the socket is precisely how ``Esmool`` hands the previous transaction's bytes to
the next one, and 3E has no serial No. to catch it -- so any failure mid-message leaves
this transport unusable and :class:`aslmp.connection.Connection` closes the socket and
goes sticky ``FAILED``.
"""

from __future__ import annotations

import asyncio
import socket
from typing import TYPE_CHECKING, Final, final

from aslmp.errors import (
    SlmpConcurrentTransactionError,
    SlmpConnectionEntryBusyError,
    SlmpConnectionLostError,
    SlmpNotConnectedError,
    SlmpNotSentError,
    SlmpProtocolError,
    SlmpTimeoutError,
)
from aslmp.transport.base import (
    DEFAULT_BUFFER_CAPACITY,
    Binding,
    Correlation,
    Deadline,
    Reassembler,
    RecvBuffer,
    TransportKind,
    TransportObserver,
    WireResult,
    timeout_error,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from aslmp.timing import TimingBuilder

__all__ = ["TcpTransport"]

_FIN_MEASUREMENT: Final = (
    "measured on FX5U-32MT/DS fw 1.065, 2026-09-06: a second connection to a "
    "one-entry SLMP configuration completes connect() and then the CPU FINs, so "
    "recv() returns 0 bytes before anything has been sent"
)

_ENTRY_RELEASE_MEASUREMENT: Final = (
    "measured on FX5U-32MT/DS fw 1.065, 2026-09-07, wired, median RTT 3.64 ms, six "
    "trials per gap: after a clean close() a reconnect to the same entry succeeded 1/6 "
    "at a 0 ms gap, 2/6 at 1 ms, and 6/6 from 2 ms out to 200 ms. Over Wi-Fi at ~7 ms "
    "RTT the same test was 30/30 at every gap including 0 ms, so what has to elapse "
    "tracks the link and not the clock. That is what racing the CPU's own FIN "
    "processing looks like from outside, rather than a fixed hold period -- an "
    "inference from these timings, not something anything here can see inside the CPU. "
    "Either way the window is link-dependent, so a faster link is expected to widen it, "
    "and 2 ms is one CPU on one link on one day rather than a spec value"
)
"""Why the same client reconnecting into its own FIN gets the busy error (2026-09-07).

Named beside :data:`_FIN_MEASUREMENT` because the two together are the whole error
message: the entry serves one connection, and the moment it stops serving yours is not
the moment your next ``connect()`` can have it.
"""

_ENTRY_RELEASE_ADVICE: Final = (
    "The fix for that is a short explicit settle before reconnecting to an entry this "
    "client just released -- 5 ms was clean on our wired bench, against the 2 ms window "
    "measured there; a faster link may need more, and it will say so by raising this. "
    "It is not a hunt for a second client, and it is not a retry loop: retrying a "
    "connect that raced is how a client spins against a CPU that never had a problem. "
    "See docs/hardware.md section 2.1."
)
"""What to do about it. A settle is a decision the caller makes once, in one place; a
retry inside this transport would be exactly the silent recovery the package forbids."""


@final
class TcpTransport:
    """One TCP socket to one configured SLMP connection entry.

    Not reusable across peers and not poolable: the CPU accepts exactly one simultaneous
    connection per entry, so a pool against one entry is a queue of doomed sockets. It
    *is* reopenable -- :meth:`open` builds a fresh socket every time -- which is how a
    supervised reconnect gets a new local port and a new generation.
    """

    __slots__ = (
        "_binding",
        "_buffer",
        "_busy",
        "_host",
        "_nodelay",
        "_port",
        "_sock",
        "_source",
        "_transactions_completed",
    )

    def __init__(
        self,
        host: str,
        port: int,
        *,
        nodelay: bool = True,
        buffer_capacity: int = DEFAULT_BUFFER_CAPACITY,
        source: tuple[str, int] | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._nodelay = nodelay
        self._source = source
        self._buffer = RecvBuffer(buffer_capacity)
        self._sock: socket.socket | None = None
        self._binding: Binding | None = None
        self._busy = False
        self._transactions_completed = 0

    # -- identity ------------------------------------------------------------

    @property
    def kind(self) -> TransportKind:
        return TransportKind.TCP

    @property
    def peer(self) -> tuple[str, int]:
        return (self._host, self._port)

    @property
    def is_open(self) -> bool:
        return self._sock is not None

    @property
    def binding(self) -> Binding | None:
        return self._binding

    @property
    def max_in_flight(self) -> int:
        """Always 1, and not configurable.

        The coalescing corruption is a property of this CPU's TCP receive path, not a
        tuning parameter. UDP is where pipelining is offered, because datagrams are
        framed and the same test returns both responses correctly.
        """
        return 1

    @property
    def transactions_completed(self) -> int:
        """Exchanges that produced a complete message on **this** socket.

        Reset by :meth:`open`, because it answers exactly one question -- "have the
        per-connection facts (coding, frame, protocol, port, entry availability) ever
        been proven on this socket?" -- and a reconnect makes them unproven again.
        """
        return self._transactions_completed

    def attach_observer(self, observer: TransportObserver, /) -> None:
        """Accepted and ignored: TCP produces neither of the two reports.

        A dropped datagram and a source-port rebind are both UDP facts. TCP's own
        failures are exceptions, because on a stream every one of them is fatal to the
        message in flight -- there is nothing to drop and carry on from.
        """
        return None

    # -- lifecycle -----------------------------------------------------------

    async def open(self, deadline: Deadline) -> Binding:
        """Connect, prove nothing, and classify a FIN that has already arrived.

        The proof that the entry is free and the coding is right is the ``0x0619``
        handshake the layer above runs next; this only makes the already-lost case
        legible instead of arriving 3 seconds later as a bare timeout.
        """
        if self._sock is not None:
            raise SlmpNotConnectedError(
                "this transport already holds an open socket; close it before opening "
                "another. The CPU accepts one simultaneous connection per SLMP entry, "
                f"so a second socket to the same entry is refused by the PLC itself "
                f"({_FIN_MEASUREMENT}).",
                reason="already-open",
            )
        loop = asyncio.get_running_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setblocking(False)
            if self._nodelay:
                # Costs nothing and is NOT a latency fix: with NODELAY on, 500
                # sequential 2-word reads still gave p50 7.338 ms on this CPU from the
                # laptop at 192.168.10.41 over Wi-Fi (2026-09-06, ~7 ms median RTT).
                # It removes a delayed-ACK interaction, it does not remove the 7 ms.
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            if self._source is not None:
                sock.bind(self._source)
            await self._connect(loop, sock, deadline)
            self._check_no_early_eof(sock)
            binding = Binding(peer=_addr(sock.getpeername()), local=_addr(sock.getsockname()))
        except BaseException:
            sock.close()
            raise
        self._sock = sock
        self._binding = binding
        self._transactions_completed = 0
        self._busy = False
        return binding

    async def _connect(
        self, loop: asyncio.AbstractEventLoop, sock: socket.socket, deadline: Deadline
    ) -> None:
        remaining = deadline.remaining_s()
        if remaining <= 0.0:
            raise timeout_error(
                deadline=deadline,
                bytes_received=0,
                completed_transactions=0,
                peer=self.peer,
                kind=TransportKind.TCP,
                where="opening the socket",
            )
        try:
            await asyncio.wait_for(loop.sock_connect(sock, (self._host, self._port)), remaining)
        except SlmpTimeoutError:
            raise
        except TimeoutError as exc:
            raise timeout_error(
                deadline=deadline,
                bytes_received=0,
                completed_transactions=0,
                peer=self.peer,
                kind=TransportKind.TCP,
                where="opening the socket",
            ) from exc
        except OSError as exc:
            raise SlmpNotConnectedError(
                f"could not open a TCP connection to {self._host}:{self._port}: "
                f"{exc.__class__.__name__}: {exc}. Nothing was sent. Check that the "
                f"GX Works3 connection entry exists on this port and is configured for "
                f"TCP -- a UDP entry does not answer a TCP connect at all.",
                reason="connect-failed",
            ) from exc

    def _check_no_early_eof(self, sock: socket.socket) -> None:
        """Graft G2. Non-blocking, zero bytes on the wire, no wait, no false positive."""
        peeked: bytes | None
        try:
            peeked = sock.recv(1, socket.MSG_PEEK)
        except BlockingIOError:
            peeked = None  # Nothing has arrived. The healthy case, and the common one.
        except ConnectionResetError as exc:
            raise SlmpConnectionEntryBusyError(
                f"{self._host}:{self._port} accepted the TCP connection and then reset "
                f"it before anything was sent. The SLMP connection entry did not become "
                f"this socket's ({_FIN_MEASUREMENT}): either another client holds it, "
                f"or this client closed its own connection to the same entry moments "
                f"ago and reconnected before the CPU had processed the FIN. On the "
                f"second: {_ENTRY_RELEASE_MEASUREMENT}. {_ENTRY_RELEASE_ADVICE}"
            ) from exc
        except OSError:
            peeked = None  # Some other socket state; the handshake is the proof.
        if peeked is None:
            return
        if peeked == b"":
            raise SlmpConnectionEntryBusyError(
                f"{self._host}:{self._port} accepted the TCP connection and immediately "
                f"closed it: EOF was already waiting before this client sent a single "
                f"byte. The SLMP connection entry serves one TCP connection at a time "
                f"and this socket did not get it -- socket.connect() succeeds anyway on "
                f"this hardware ({_FIN_MEASUREMENT}). There are two causes and the "
                f"second is the one people meet: (a) another client already holds the "
                f"entry -- use a different configured entry, because connection pooling "
                f"against one entry cannot work; (b) this client closed its own "
                f"connection to the same entry moments ago and reconnected before the "
                f"CPU had processed the FIN. If nothing else is connected it is (b): "
                f"{_ENTRY_RELEASE_MEASUREMENT}. {_ENTRY_RELEASE_ADVICE}"
            )
        raise SlmpProtocolError(
            f"{len(peeked)} unsolicited byte(s) were already waiting on a freshly "
            f"opened connection to {self._host}:{self._port}, before this client sent "
            f"anything. They are somebody's message and this library will not guess "
            f"whose: reading them as a response is how a client stays desynchronised by "
            f"one message for the life of a connection."
        )

    async def close(self) -> None:
        """Close the socket. Idempotent, never raises, never half-closes."""
        sock = self._sock
        self._sock = None
        self._busy = False
        if sock is not None:
            sock.close()

    # -- the one operation that touches the wire -----------------------------

    async def exchange(
        self,
        request: bytes | memoryview,
        reassembler: Reassembler,
        deadline: Deadline,
        timing: TimingBuilder,
        *,
        expect_response: bool = True,
        correlation: Correlation | None = None,
    ) -> WireResult:
        """Write the request and read exactly one response. There is no other way in.

        ``correlation`` is accepted and ignored: TCP is a stream with one message in
        flight, so the response is the answer to the request by construction. It is UDP
        that needs it.

        ``expect_response=False`` writes and then **closes the socket**. It is not a
        ``send``: this transport has none, and one call that wrote without reading would
        be one, with the desynchronised socket left in service afterwards.
        :class:`aslmp.connection.Connection` retires the connection to ``FAILED`` on the
        same path, so the two agree; closing here is what makes the transport itself
        unusable rather than merely unused.
        """
        sock = self._require_open()
        if self._busy:
            raise SlmpConcurrentTransactionError(
                "a second exchange was started on this TCP socket while one was still "
                "in flight. Two requests written before the first response is read "
                "return ONE response, for the LAST request, with end code 0x0000 on "
                "FX5U-32MT/DS fw 1.065 -- and 3E has no serial No. to detect it."
            )
        self._busy = True
        try:
            payload = memoryview(request)
            sent = await self._send_all(sock, payload, deadline)
            timing.sent()
            if not expect_response:
                # This socket is finished. The request went out and nothing will read
                # the answer, so nothing here can prove there is not one on its way; a
                # socket kept open hands those bytes to the next exchange as fresh data
                # with end code 0x0000, and 3E has no serial No. that would reveal it.
                # Closing is the same decision the read loop makes for any failure
                # mid-message, and it is why this transport has no `send`.
                await self.close()
                return WireResult(
                    sent=True, responded=False, bytes_sent=sent, bytes_received=0, chunks=()
                )
            received = await self._read_message(sock, reassembler, deadline, timing)
            self._transactions_completed += 1
            return WireResult(
                sent=True,
                responded=True,
                bytes_sent=sent,
                bytes_received=received,
                chunks=timing.chunks,
            )
        finally:
            self._busy = False

    def _require_open(self) -> socket.socket:
        sock = self._sock
        if sock is None:
            raise SlmpNotConnectedError(
                "this TCP transport has no open socket. Nothing was sent. Reconnection "
                "is never implicit in this library: it is an explicit call and an "
                "observable event.",
                reason="closed",
            )
        return sock

    async def _send_all(
        self, sock: socket.socket, payload: memoryview, deadline: Deadline
    ) -> int:
        """Hand the request to the OS, distinguishing "did not happen" from "may have".

        The first write is attempted directly on the non-blocking socket. If *that*
        raises, provably not one byte reached the OS and the caller gets
        :class:`~aslmp.errors.SlmpNotSentError`, which for a state-changing command is
        the difference between "retry it" and "read the device back before deciding".
        Once any byte has gone out -- or once the write has been handed to the event
        loop, where partial progress is not observable -- the outcome is unknown and is
        reported as such.
        """
        total = len(payload)
        if total == 0:
            raise SlmpNotSentError(
                "refusing to send an empty request: there is no SLMP message of zero "
                "length, and a zero-byte write would leave the PLC waiting."
            )
        try:
            first = sock.send(payload)
        except BlockingIOError:
            first = 0
        except OSError as exc:
            raise SlmpNotSentError(
                f"the socket refused the request before a single byte reached the OS: "
                f"{exc.__class__.__name__}: {exc}. Nothing was sent, so nothing "
                f"happened on the PLC."
            ) from exc
        if first >= total:
            return first
        remaining = deadline.remaining_s()
        if remaining <= 0.0:
            raise timeout_error(
                deadline=deadline,
                bytes_received=0,
                completed_transactions=self._transactions_completed,
                peer=self.peer,
                kind=TransportKind.TCP,
                where=f"sending the request ({first} of {total} bytes written)",
            )
        loop = asyncio.get_running_loop()
        try:
            await asyncio.wait_for(loop.sock_sendall(sock, payload[first:]), remaining)
        except TimeoutError as exc:
            raise timeout_error(
                deadline=deadline,
                bytes_received=0,
                completed_transactions=self._transactions_completed,
                peer=self.peer,
                kind=TransportKind.TCP,
                where=f"sending the request ({first} of {total} bytes written)",
            ) from exc
        except OSError as exc:
            raise SlmpConnectionLostError(
                f"the connection to {self._host}:{self._port} failed while the request "
                f"was being written ({first} of {total} bytes had already gone out): "
                f"{exc.__class__.__name__}: {exc}. Part of the request may have reached "
                f"the PLC, so the outcome of a state-changing command is unknown."
            ) from exc
        return total

    async def _read_message(
        self,
        sock: socket.socket,
        reassembler: Reassembler,
        deadline: Deadline,
        timing: TimingBuilder,
    ) -> int:
        """Read exactly ``prefix + L`` units, however many ``recv`` calls that takes."""
        loop = asyncio.get_running_loop()
        received = 0
        while reassembler.bytes_needed:
            wanted = reassembler.bytes_needed
            window = self._buffer.window(wanted)
            remaining = deadline.remaining_s()
            if remaining <= 0.0:
                raise self._timeout(deadline, received)
            try:
                nbytes = await asyncio.wait_for(loop.sock_recv_into(sock, window), remaining)
            except TimeoutError as exc:
                raise self._timeout(deadline, received) from exc
            except ConnectionResetError as exc:
                # An abortive close is the SAME EVENT as a clean one; which of the two a
                # busy entry produces is a property of the host's TCP stack, not of the
                # PLC. Windows delivers FIN and recv() returns 0; Linux delivers RST and
                # recv() raises ECONNRESET. Routing both through _eof is what keeps
                # SlmpConnectionEntryBusyError meaning the same thing on every platform.
                # Measured 2026-09-24 in CI: identical simulator, identical client;
                # windows-latest raised entry-busy and ubuntu-latest raised connection-lost.
                raise self._eof(received) from exc
            except OSError as exc:
                raise SlmpConnectionLostError(
                    f"the connection to {self._host}:{self._port} failed after "
                    f"{received} byte(s) of the response had arrived: "
                    f"{exc.__class__.__name__}: {exc}. The response is incomplete and "
                    f"this library never invents the rest of it."
                ) from exc
            if nbytes == 0:
                raise self._eof(received)
            # A read that came back short of what it asked for is the only honest
            # evidence of a segment split; the prefix-then-body pair is structural and
            # is NOT segmentation. See Chunk.partial and TransactionTiming.segmented.
            timing.chunk(nbytes, partial=nbytes < wanted)
            received += nbytes
            reassembler.feed(bytes(window[:nbytes]))
        return received

    def _timeout(self, deadline: Deadline, received: int) -> SlmpTimeoutError:
        return timeout_error(
            deadline=deadline,
            bytes_received=received,
            completed_transactions=self._transactions_completed,
            peer=self.peer,
            kind=TransportKind.TCP,
            where="reading the response",
        )

    def _eof(self, received: int) -> SlmpConnectionEntryBusyError | SlmpConnectionLostError:
        """The peer ended the connection without answering. Which error that is depends
        on whether this socket ever worked.

        On the **first** read of a generation the entry-busy reading is the one that
        matches the hardware: the CPU accepts a second connection to a one-entry
        configuration and then FINs. Once a transaction has completed on this socket the
        entry was demonstrably ours, so the same ending means the peer closed an
        established connection -- a stopped CPU, a reset, or a cable.

        Two wire events arrive here and they are the same event. A clean close gives a
        zero-byte ``recv``; an abortive one raises ``ECONNRESET``. Which a busy entry
        produces is decided by the *host's* TCP stack rather than by the PLC, so both are
        classified identically -- otherwise this library's most precisely-named error
        would exist only on the platform it was developed on.
        """
        if self._transactions_completed == 0 and received == 0:
            return SlmpConnectionEntryBusyError(
                f"{self._host}:{self._port} closed the connection without answering the "
                f"first request on it. On this hardware that means the SLMP connection "
                f"entry never became this socket's ({_FIN_MEASUREMENT}). Two causes: "
                f"another client holds the entry, or this client closed its own "
                f"connection to the same entry moments ago and reconnected before the "
                f"CPU had processed the FIN. With nothing else connected it is the "
                f"second: {_ENTRY_RELEASE_MEASUREMENT}. {_ENTRY_RELEASE_ADVICE} Nothing "
                f"was read, so nothing about the coding or the frame format has been "
                f"proven either."
            )
        return SlmpConnectionLostError(
            f"{self._host}:{self._port} closed the connection after {received} byte(s) "
            f"of this response had arrived. The message is incomplete; a partial frame "
            f"is not end code 0x0000 and this library will not complete it. "
            f"Reconnection is explicit and is an observable event."
        )

    def __repr__(self) -> str:
        state = "open" if self._sock is not None else "closed"
        return f"TcpTransport({self._host!r}, {self._port!r}) [{state}]"


def _addr(value: object) -> tuple[str, int]:
    """``getsockname`` returns a tuple whose static type is broad. Narrow it once."""
    if isinstance(value, tuple) and len(value) >= 2:
        host, port = value[0], value[1]
        if isinstance(host, str) and isinstance(port, int):
            return (host, port)
    return ("", 0)

