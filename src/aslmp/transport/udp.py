"""The UDP transport: one datagram per message, an epoch, a rebind, and a hard loss error.

Everything here is measured on **MELSEC iQ-F FX5U-32MT/DS, firmware 1.065**, over GX
Works3 connection entry No. 2 (SLMP / UDP / PLC port 5001 / destination 192.168.10.41),
2026-09-06, **from that laptop over Wi-Fi** -- except where a wired retest is named. The
findings reverse the naive assumption that UDP is the riskier transport, and they are the
reason this file is shaped the way it is.

**1. UDP does not suffer the TCP coalescing corruption.** Two requests written without
reading between them returned *both* responses, correct and in order, where TCP returned
one response for the last request with end code ``0x0000``. Datagrams are framed, so the
failure does not exist. The one-transaction-in-flight rule is a TCP rule.

**2. The receive queue is a hard 32, and overflow is silent.** Bursts of 4E reads fired
without waiting gave 8/8, 32/32 and **44/64** here -- twenty requests dropped by the PLC's
receive path with no end code, no ICMP, and no error of any kind. Repeated 2026-09-07 from
``argus-bench`` over the **wired** link (UDP entry port 5005), depth 48 answered exactly 32
and depth 64 answered exactly 32, so the ceiling is a hard 32 rather than the soft
degradation the 44 suggested: on the slower link the CPU drains part of the queue while the
rest of the burst is still arriving. The client finds out only from a
serial No. that never comes back, which is why that case raises its own
:class:`~aslmp.errors.SlmpDatagramLostError` naming the serial and the depth, and never a
generic timeout and never a retry.

**3. Pipelining requires 4E.** Serials are echoed correctly on UDP, so responses can be
correlated. On 3E there is no serial at all, so responses could only be matched
positionally -- and (2) proves loss is real, so positional matching is silently mismatched
replies: the same bug class as the TCP coalescing corruption. Depth above 1 is therefore
refused outright on a frame format that carries no serial. Not warned about. Refused.

**4. A UDP SLMP entry is point-to-point.** GX Works3 will not save one without a
destination IP address, so a datagram from any other address is somebody else's or
nobody's. Every datagram's source is validated, and a foreign one is dropped and counted,
never consumed.

**5. There is no one-connection limit.** A second UDP socket from a different source port
was served concurrently and the first kept working, so the accept-then-FIN behaviour that
makes TCP entries exclusive does not apply here.

**6. A 960-point read returns 1935 bytes**, over a 1500-byte MTU, so IP fragmentation is
in play and every fragment must arrive or the whole datagram is gone. A datagram shorter
than the message it declares is a truncated *message*, not a short read, and raises
rather than being parsed.

.. rubric:: The epoch and the rebind

A late reply from the correct peer becoming the next transaction's answer is the UDP
equivalent of ``Esmool``'s unread tail. Two mechanisms prevent it. Before a request goes
out with nothing else in flight, the socket is **drained** and anything found is counted
as a stale-epoch drop -- it can only be the answer to a transaction that has already
ended. And on a timeout, when nothing else is in flight, the socket is **rebound to a
fresh source port**, so the vanished request's answer arrives at a port nobody is
listening on. The rebind bumps ``generation`` through the observer, which is what makes it
impossible to mistake for a slow PLC.
"""

from __future__ import annotations

import asyncio
import socket
from typing import TYPE_CHECKING, Any, Final, final

from aslmp.errors import (
    SlmpConcurrentTransactionError,
    SlmpConfigurationError,
    SlmpConnectionLostError,
    SlmpDatagramLostError,
    SlmpDatagramSourceError,
    SlmpNotConnectedError,
    SlmpNotSentError,
    SlmpShortDatagramError,
    SlmpTransportError,
)
from aslmp.transport.base import (
    DEFAULT_BUFFER_CAPACITY,
    MAX_UDP_PIPELINE_DEPTH,
    NULL_OBSERVER,
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

__all__ = ["FOREIGN_SOURCE", "STALE_EPOCH", "UNMATCHED", "UdpTransport"]

_PIPELINE_MEASUREMENT: Final = (
    "measured on FX5U-32MT/DS fw 1.065 over UDP entry No. 2 (PLC port 5001), "
    "2026-09-06 over Wi-Fi: bursts of 4E reads returned 8/8 and 32/32 with zero loss "
    "and 44/64 at depth 64 -- twenty requests dropped with no end code, no ICMP and no "
    "error. Wired on 2026-09-07 the ceiling was harder still: exactly 32 answered at "
    "depth 48 and exactly 32 at depth 64"
)

STALE_EPOCH: Final = "stale-epoch"
"""A datagram found queued before a request went out. It answers a dead transaction."""

FOREIGN_SOURCE: Final = "foreign-source"
"""A datagram from an address this point-to-point entry never sent to."""

UNMATCHED: Final = "unmatched"
"""A datagram from the right peer that no in-flight request claims. Never guessed at."""


@final
class _Waiter:
    """One in-flight request, waiting for the datagram that matches it.

    The waiter carries its own :class:`~aslmp.timing.TimingBuilder` so that whichever
    coroutine happens to be reading the socket stamps the chunk **when the datagram
    landed**, not when the owning coroutine was next scheduled. Under pipelining those
    are different moments, and the second one is not a measurement of the PLC.
    """

    __slots__ = ("correlation", "future", "in_flight", "timing")

    def __init__(
        self,
        *,
        correlation: Correlation,
        timing: TimingBuilder,
        future: asyncio.Future[bytes],
        in_flight: int,
    ) -> None:
        self.correlation = correlation
        self.timing = timing
        self.in_flight = in_flight
        self.future = future

    def deliver(self, datagram: bytes) -> None:
        if self.future.done():
            return
        self.timing.chunk(len(datagram))
        self.future.set_result(datagram)


@final
class UdpTransport:
    """One UDP socket to one configured, point-to-point SLMP connection entry.

    ``pipeline_depth`` is the in-flight cap and it becomes the connection's gate
    capacity. It defaults to 1 -- one request at a time, exactly like TCP -- so
    pipelining is something a caller asks for rather than something that happens;
    :data:`~aslmp.transport.base.DEFAULT_UDP_PIPELINE_DEPTH` is the conservative value
    to ask for. It is refused above :data:`~aslmp.transport.base.MAX_UDP_PIPELINE_DEPTH`
    and refused above 1 entirely unless ``carries_serial`` is true.

    ``carries_serial`` is a structural fact about the frame format, handed down from the
    connection. This module cannot import ``aslmp.wire`` and therefore cannot ask a
    ``FrameFormat`` the question itself; it is told the answer, exactly as it is told how
    to recognise its own response by :class:`~aslmp.transport.base.Correlation`.
    """

    __slots__ = (
        "_binding",
        "_buffer",
        "_carries_serial",
        "_host",
        "_local",
        "_observer",
        "_peer",
        "_pipeline_depth",
        "_port",
        "_read_baton",
        "_sock",
        "_source_host",
        "_transactions_completed",
        "_waiters",
    )

    def __init__(
        self,
        host: str,
        port: int,
        *,
        carries_serial: bool,
        pipeline_depth: int = 1,
        source_host: str = "",
        buffer_capacity: int = DEFAULT_BUFFER_CAPACITY,
        observer: TransportObserver = NULL_OBSERVER,
    ) -> None:
        if pipeline_depth < 1:
            raise SlmpConfigurationError(
                f"pipeline_depth={pipeline_depth} admits no transactions at all."
            )
        if pipeline_depth > 1 and not carries_serial:
            raise SlmpConfigurationError(
                f"pipeline_depth={pipeline_depth} was requested on a frame format that "
                f"carries no serial No. 3E has no correlation field, so several "
                f"requests in flight could only be matched to their responses by "
                f"arrival order -- and datagrams really are lost on this hardware "
                f"({_PIPELINE_MEASUREMENT}). Positional matching plus real loss is "
                f"silently mismatched replies, which is the same failure as the TCP "
                f"coalescing corruption wearing a different hat. Use 4E to pipeline."
            )
        if pipeline_depth > MAX_UDP_PIPELINE_DEPTH:
            raise SlmpConfigurationError(
                f"pipeline_depth={pipeline_depth} is above the deepest burst measured "
                f"lossless ({MAX_UDP_PIPELINE_DEPTH}). {_PIPELINE_MEASUREMENT}. Past "
                f"that the PLC's receive path overflows and drops requests with no "
                f"error anywhere, and this library does not offer a setting whose "
                f"failure mode is invisible."
            )
        self._host = host
        self._port = port
        self._carries_serial = carries_serial
        self._pipeline_depth = pipeline_depth
        self._source_host = source_host
        self._buffer = RecvBuffer(buffer_capacity)
        self._observer = observer
        self._sock: socket.socket | None = None
        self._binding: Binding | None = None
        self._peer: tuple[str, int] = (host, port)
        self._local: tuple[str, int] = ("", 0)
        self._waiters: list[_Waiter] = []
        self._read_baton = asyncio.Lock()
        self._transactions_completed = 0

    # -- identity ------------------------------------------------------------

    @property
    def kind(self) -> TransportKind:
        return TransportKind.UDP

    @property
    def peer(self) -> tuple[str, int]:
        return self._peer

    @property
    def is_open(self) -> bool:
        return self._sock is not None

    @property
    def binding(self) -> Binding | None:
        return self._binding

    @property
    def max_in_flight(self) -> int:
        return self._pipeline_depth

    @property
    def transactions_completed(self) -> int:
        return self._transactions_completed

    @property
    def in_flight(self) -> int:
        return len(self._waiters)

    def attach_observer(self, observer: TransportObserver, /) -> None:
        """Point the drop counter and the rebind report at whoever owns the identity.

        The transport does not know the connection id and does not own ``generation``,
        so it cannot build an event; it reports the fact and the connection bumps the
        generation, which is what makes a rebind impossible to mistake for a slow PLC.
        """
        self._observer = observer

    # -- lifecycle -----------------------------------------------------------

    async def open(self, deadline: Deadline) -> Binding:
        """Bind a fresh source port and resolve the peer. No handshake, no proof.

        A UDP socket cannot tell you whether anything is listening -- there is no
        connect, no FIN and no reset to observe. The ``0x0619`` Self Test the layer
        above runs next is the only proof there is, and on this transport it is the
        *whole* proof.
        """
        if self._sock is not None:
            raise SlmpNotConnectedError(
                "this transport already holds an open socket; close it first.",
                reason="already-open",
            )
        peer = await self._resolve(deadline)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setblocking(False)
            sock.bind((self._source_host, 0))
            local = _addr(sock.getsockname())
        except BaseException:
            sock.close()
            raise
        self._sock = sock
        self._peer = peer
        self._local = local
        self._binding = Binding(peer=peer, local=local)
        self._transactions_completed = 0
        self._waiters.clear()
        return self._binding

    async def _resolve(self, deadline: Deadline) -> tuple[str, int]:
        """Resolve the peer on the event loop's resolver, inside the connect deadline.

        A blocking ``gethostbyname`` here would stall the whole loop -- including every
        other connection in an ``EntryGroup`` -- on a DNS server that is not answering.
        """
        remaining = deadline.remaining_s()
        if remaining <= 0.0:
            raise timeout_error(
                deadline=deadline,
                bytes_received=0,
                completed_transactions=0,
                peer=(self._host, self._port),
                kind=TransportKind.UDP,
                where="resolving the peer address",
            )
        loop = asyncio.get_running_loop()
        try:
            infos = await asyncio.wait_for(
                loop.getaddrinfo(
                    self._host,
                    self._port,
                    family=socket.AF_INET,
                    type=socket.SOCK_DGRAM,
                ),
                remaining,
            )
        except TimeoutError as exc:
            raise timeout_error(
                deadline=deadline,
                bytes_received=0,
                completed_transactions=0,
                peer=(self._host, self._port),
                kind=TransportKind.UDP,
                where="resolving the peer address",
            ) from exc
        except OSError as exc:
            raise SlmpNotConnectedError(
                f"could not resolve {self._host!r}: {exc.__class__.__name__}: {exc}. "
                f"Nothing was sent.",
                reason="resolve-failed",
            ) from exc
        if not infos:
            raise SlmpNotConnectedError(
                f"{self._host!r} resolved to no IPv4 address. Nothing was sent.",
                reason="resolve-failed",
            )
        return _addr(infos[0][4])

    async def close(self) -> None:
        """Close the socket and fail every waiter. Idempotent, never raises."""
        sock = self._sock
        self._sock = None
        for waiter in tuple(self._waiters):
            if not waiter.future.done():
                waiter.future.set_exception(
                    SlmpConnectionLostError(
                        "the UDP socket was closed while this request was in flight; "
                        "its outcome on the PLC is unknown."
                    )
                )
                # Mark it retrieved: the coroutine that owned this waiter may already
                # have left through its own error path, and an unretrieved exception
                # would surface later as an asyncio warning about the wrong thing.
                waiter.future.exception()
        self._waiters.clear()
        if sock is not None:
            sock.close()

    def _rebind(self, *, reason: str) -> None:
        """A fresh source port, so the vanished request's answer lands nowhere.

        Only done when nothing else is in flight: rebinding under a pipelined burst
        would throw away the answers to requests that are still perfectly alive, which
        is a silent loss of its own.
        """
        sock = self._sock
        if sock is None or self._waiters:
            return
        previous = self._local
        replacement = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            replacement.setblocking(False)
            replacement.bind((self._source_host, 0))
        except BaseException:
            replacement.close()
            raise
        sock.close()
        self._sock = replacement
        self._local = _addr(replacement.getsockname())
        self._binding = Binding(peer=self._peer, local=self._local)
        self._observer.socket_rebound(previous_local=previous, local=self._local, reason=reason)

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
        """Send exactly one datagram and take exactly one datagram back.

        The response is required to be a whole message in a single datagram: after it is
        fed to the reassembler, anything still wanted means the datagram was truncated
        and :class:`~aslmp.errors.SlmpShortDatagramError` is raised rather than a
        partial frame being parsed.
        """
        sock = self._require_open()
        if len(self._waiters) >= self._pipeline_depth:
            raise self._too_many_in_flight()
        payload = bytes(request)
        if not payload:
            raise SlmpNotSentError(
                "refusing to send an empty datagram: there is no SLMP message of zero "
                "length, and an empty datagram is a message the PLC will answer 0xC061 "
                "to at best."
            )
        if not self._waiters:
            self._drain_stale(sock)
        sent = self._send(sock, payload)
        timing.sent()
        if not expect_response:
            return WireResult(
                sent=True, responded=False, bytes_sent=sent, bytes_received=0, chunks=()
            )
        datagram = await self._await_response(correlation or Correlation(), deadline, timing)
        reassembler.feed(datagram)
        outstanding = reassembler.bytes_needed
        if outstanding:
            raise SlmpShortDatagramError(
                f"the datagram from {self._peer[0]}:{self._peer[1]} is "
                f"{len(datagram)} byte(s) and the message it declares wants "
                f"{outstanding} more. One SLMP message is one datagram; a truncated one "
                f"is a different message, not a short read, and it is never parsed. A "
                f"960-point read returns 1935 bytes and is IP-fragmented on a 1500-byte "
                f"MTU, so every fragment must arrive or the whole datagram is gone."
            )
        self._transactions_completed += 1
        return WireResult(
            sent=True,
            responded=True,
            bytes_sent=sent,
            bytes_received=len(datagram),
            chunks=timing.chunks,
        )

    def _require_open(self) -> socket.socket:
        sock = self._sock
        if sock is None:
            raise SlmpNotConnectedError(
                "this UDP transport has no open socket. Nothing was sent.",
                reason="closed",
            )
        return sock

    def _too_many_in_flight(self) -> SlmpConcurrentTransactionError:
        if self._pipeline_depth == 1:
            why = (
                "is 4E; raise pipeline_depth to use it."
                if self._carries_serial
                else "is 3E and has no serial No. at all, so pipelining it would match "
                "responses to requests by arrival order -- and datagrams really are "
                "lost on this hardware."
            )
            return SlmpConcurrentTransactionError(
                f"a second request was submitted on a UDP connection whose in-flight "
                f"depth is 1. Pipelining is offered on UDP -- datagrams are framed and "
                f"this CPU answers both of two unread requests correctly -- but only on "
                f"4E, where the serial No. correlates a response to its request. This "
                f"connection {why}"
            )
        return SlmpConcurrentTransactionError(
            f"{len(self._waiters)} request(s) are already in flight, which is this "
            f"connection's pipeline_depth. {_PIPELINE_MEASUREMENT}."
        )

    def _send(self, sock: socket.socket, payload: bytes) -> int:
        try:
            return sock.sendto(payload, self._peer)
        except OSError as exc:
            raise SlmpNotSentError(
                f"the datagram to {self._peer[0]}:{self._peer[1]} was refused by the "
                f"OS before it left this process: {exc.__class__.__name__}: {exc}. "
                f"Nothing was sent, so nothing happened on the PLC."
            ) from exc

    def _drain_stale(self, sock: socket.socket) -> None:
        """Discard anything already queued. It cannot be an answer to anything current.

        Counted, never delivered: a late reply from the correct peer becoming the next
        transaction's answer is exactly the failure the per-transaction epoch exists to
        prevent, and on 3E there is no serial No. that would ever reveal it.
        """
        while True:
            queued: tuple[bytes, object] | None
            try:
                queued = sock.recvfrom(self._buffer.capacity)
            except OSError:
                # BlockingIOError -- the ordinary "nothing was queued" -- or a socket
                # that has just gone bad, which the send that follows reports properly.
                queued = None
            if queued is None:
                return
            data, source = queued
            self._observer.datagram_dropped(
                reason=STALE_EPOCH, nbytes=len(data), source=_addr(source)
            )

    async def _await_response(
        self, correlation: Correlation, deadline: Deadline, timing: TimingBuilder
    ) -> bytes:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[bytes] = loop.create_future()
        waiter = _Waiter(
            correlation=correlation,
            timing=timing,
            future=future,
            in_flight=len(self._waiters) + 1,
        )
        self._waiters.append(waiter)
        try:
            if self._pipeline_depth == 1:
                return await self._pump_solo(waiter, deadline)
            return await self._pump_shared(waiter, deadline)
        finally:
            if waiter in self._waiters:
                self._waiters.remove(waiter)

    async def _pump_solo(self, waiter: _Waiter, deadline: Deadline) -> bytes:
        """The depth-1 read: this coroutine is the only possible reader of the socket."""
        while not waiter.future.done():
            data, source = await self._recv_one(waiter, deadline)
            self._dispatch(data, source)
        return await waiter.future

    async def _pump_shared(self, waiter: _Waiter, deadline: Deadline) -> bytes:
        """The pipelined read: one reader at a time, whoever gets the baton.

        Two coroutines must never call ``sock_recvfrom`` on the same socket at once. On
        a selector event loop the second registration replaces the first's callback and
        the first request hangs until its deadline -- a self-inflicted version of the
        very datagram loss this transport exists to report. So the reader holds a lock,
        reads one datagram, hands it to whoever claims it, and lets go. There is no
        background task, and therefore no task to leak: the reader is always a coroutine
        that is itself waiting for an answer.
        """
        loop = asyncio.get_running_loop()
        while not waiter.future.done():
            remaining = deadline.remaining_s()
            if remaining <= 0.0:
                raise self._lost(waiter, deadline)
            acquired = loop.create_task(self._read_baton.acquire())
            racing: list[asyncio.Future[Any]] = [acquired, waiter.future]
            try:
                await asyncio.wait(
                    racing, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
                )
            except BaseException:
                acquired.cancel()
                raise
            if not acquired.done():
                acquired.cancel()
                if waiter.future.done():
                    break
                raise self._lost(waiter, deadline)
            if waiter.future.done():
                self._read_baton.release()
                break
            try:
                data, source = await self._recv_one(waiter, deadline)
                self._dispatch(data, source)
            finally:
                self._read_baton.release()
        return await waiter.future

    async def _recv_one(
        self, waiter: _Waiter, deadline: Deadline
    ) -> tuple[bytes, tuple[str, int]]:
        """One datagram, or the named failure for why there was not one."""
        remaining = deadline.remaining_s()
        if remaining <= 0.0:
            raise self._lost(waiter, deadline)
        sock = self._sock
        if sock is None:
            raise SlmpConnectionLostError(
                "the UDP socket was closed while this request was in flight; its "
                "outcome on the PLC is unknown."
            )
        loop = asyncio.get_running_loop()
        try:
            data, source = await asyncio.wait_for(
                loop.sock_recvfrom(sock, self._buffer.capacity), remaining
            )
        except TimeoutError as exc:
            raise self._lost(waiter, deadline) from exc
        except OSError as exc:
            raise SlmpConnectionLostError(
                f"the UDP socket failed while waiting for a response from "
                f"{self._peer[0]}:{self._peer[1]}: {exc.__class__.__name__}: {exc}."
            ) from exc
        return data, _addr(source)

    def _dispatch(self, data: bytes, source: tuple[str, int]) -> None:
        """Give the datagram to whoever claims it, or drop it and say so.

        A foreign datagram is dropped and counted rather than raised: this is an
        unauthenticated cleartext protocol on a plant network, and letting any host stop
        a control loop by sending one stray packet would be a denial of service dressed
        up as a diagnosis. If the deadline then expires with nothing but foreign traffic
        seen, the timeout says so.
        """
        if source != self._peer:
            self._observer.datagram_dropped(
                reason=FOREIGN_SOURCE, nbytes=len(data), source=source
            )
            return
        for waiter in self._waiters:
            if waiter.future.done():
                continue
            if waiter.correlation.matches(data):
                waiter.deliver(data)
                return
        self._observer.datagram_dropped(reason=UNMATCHED, nbytes=len(data), source=source)

    def _lost(self, waiter: _Waiter, deadline: Deadline) -> SlmpTransportError:
        """Nothing came back. Which failure that is depends on what has worked before.

        On a socket that has already completed a transaction -- or on any pipelined
        burst -- the per-connection facts are proven and the diagnosis is the measured
        one: the PLC's receive path dropped the request and nothing anywhere will ever
        say so. That is :class:`~aslmp.errors.SlmpDatagramLostError`, naming the serial
        and the depth, because *those two numbers are the diagnosis*: at depth 32 this
        does not happen and at depth 64 it happened to a third of the burst. Reporting
        it as a timeout would send the reader looking at the network.

        On the very first transaction of a generation nothing has been proven yet, so
        the honest answer is the ordered cause list of graft G3 -- the coding, the frame
        format, the protocol and the port are all still candidates, and a coding
        mismatch is silent by design.
        """
        first_ever = self._transactions_completed == 0 and waiter.in_flight <= 1
        serial = waiter.correlation.label
        in_flight = waiter.in_flight
        self._forget(waiter)
        self._rebind(reason="timeout" if first_ever else "datagram lost")
        if first_ever:
            return timeout_error(
                deadline=deadline,
                bytes_received=0,
                completed_transactions=0,
                peer=self._peer,
                kind=TransportKind.UDP,
                where="waiting for the first response on this socket",
            )
        return SlmpDatagramLostError(
            f"no response to the request sent to {self._peer[0]}:{self._peer[1]} "
            f"within {deadline.total_s:g} s. The datagram, or its answer, was dropped: "
            f"there is no end code, no ICMP and no error of any kind for this, and this "
            f"library will not retry and hope.",
            serial=serial,
            in_flight=in_flight,
            deadline_s=deadline.total_s,
        )

    def _forget(self, waiter: _Waiter) -> None:
        if waiter in self._waiters:
            self._waiters.remove(waiter)

    def source_error(self, actual: tuple[str, int]) -> SlmpDatagramSourceError:
        """The typed refusal for a datagram from the wrong peer.

        Not raised by :meth:`exchange`, which drops and counts instead. It is offered
        here so that a caller who *has* decided a foreign datagram is fatal -- a
        conformance test, or the pass-through proxy -- raises the named class rather
        than inventing one.
        """
        return SlmpDatagramSourceError(
            f"a datagram arrived from {actual[0]}:{actual[1]}, but this SLMP entry is "
            f"point-to-point with {self._peer[0]}:{self._peer[1]}. GX Works3 refuses to "
            f"save a UDP SLMP entry without a destination IP address, so a datagram "
            f"from anywhere else is somebody else's, or nobody's.",
            expected_peer=self._peer,
            actual_peer=actual,
        )

    def __repr__(self) -> str:
        state = "open" if self._sock is not None else "closed"
        return (
            f"UdpTransport({self._host!r}, {self._port!r}, "
            f"pipeline_depth={self._pipeline_depth}) [{state}]"
        )


def _addr(value: object) -> tuple[str, int]:
    """Narrow the broad static type of ``getsockname`` / ``recvfrom`` addresses once."""
    if isinstance(value, tuple) and len(value) >= 2:
        host, port = value[0], value[1]
        if isinstance(host, str) and isinstance(port, int):
            return (host, port)
    return ("", 0)
