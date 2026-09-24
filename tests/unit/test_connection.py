"""The connection: the gate, the generation, the sticky FAILED state, the serials.

This is the seam where the transport's bytes become a message, and it is where the four
decisions the FX5U forced are actually enforced:

* **there is no public ``send``** -- bytes reach a socket only through a single-use
  capability token, because two TCP requests written before the first response is read
  return ONE response, for the LAST request, with end code ``0x0000`` (FX5U-32MT/DS fw
  1.065, 2026-09-06), and 3E has no serial No. to catch it with;
* **``generation`` bumps** on every reopen and every UDP rebind, so a latency spike from
  a socket that was quietly rebuilt cannot be read as a slow PLC;
* **``FAILED`` is sticky** and the socket is closed -- an unread tail is how the next
  transaction reads this one's bytes as fresh data;
* **serials are allocated here**, because the same number has to appear in the request
  frame, the accumulator and the UDP correlation, and three copies drift.

The server is a real loopback socket replaying real 3E and 4E frames, so the frames the
connection parses are the frames ``aslmp.wire`` builds.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket  # noqa: TID251 - a real UDP peer, because the rebind is real
import time
from dataclasses import dataclass, field
from types import TracebackType
from typing import Self, TypeVar

import pytest

from aslmp.connection import (
    Connection,
    ConnectionState,
    SerialAllocator,
)
from aslmp.errors import (
    OutcomeUnknownReason,
    SlmpConcurrentTransactionError,
    SlmpConnectionEntryBusyError,
    SlmpNotConnectedError,
    SlmpNotSentError,
    SlmpOutcomeUnknownError,
    SlmpProtocolError,
    SlmpSinkError,
    SlmpTimeoutError,
    SlmpUsageError,
)
from aslmp.observability import (
    Connected,
    ConnectFailed,
    Connecting,
    ConnectionEvent,
    ConnectionFailed,
    Counters,
    DatagramDropped,
    Disconnected,
    SocketRebound,
)
from aslmp.transport.inflight import Concurrency
from aslmp.transport.tcp import TcpTransport
from aslmp.transport.udp import UdpTransport
from aslmp.wire.codec import BINARY
from aslmp.wire.frames import FOUR_E, THREE_E, FrameFormat, request_body, response_body
from aslmp.wire.route import Route


def a_request(frame: FrameFormat = THREE_E, *, serial: int | None = None) -> bytes:
    return frame.build(
        route=Route.OWN_STATION,
        body=request_body(
            BINARY, monitoring_timer=0, command=0x0401, subcommand=0x0000,
            payload=b"\x00\x00\x00\xa8\x02\x00",
        ),
        codec=BINARY,
        serial=serial,
    )


def a_response(
    frame: FrameFormat = THREE_E, *, serial: int | None = None, payload: bytes = b"\x2c\x01"
) -> bytes:
    return frame.build_response(
        route=Route.OWN_STATION,
        body=response_body(BINARY, end_code=0, payload=payload),
        codec=BINARY,
        serial=serial,
    )


# --------------------------------------------------------------------------------------
# The loopback server
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class Reply:
    chunks: tuple[bytes, ...] = ()
    delay: float = 0.0
    gap: float = 0.0
    close_after: bool = False


@dataclass(slots=True)
class FakeServer:
    replies: list[Reply] = field(default_factory=list)
    default: Reply | None = None
    max_connections: int = 8
    received: list[bytes] = field(default_factory=list)
    port: int = 0
    connections: int = 0
    _server: asyncio.AbstractServer | None = None

    async def __aenter__(self) -> Self:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = int(self._server.sockets[0].getsockname()[1])
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        server = self._server
        if server is not None:
            server.close()
            # Bounded, and force-closed where the interpreter allows. asyncio attaches an
            # accepted connection to the server before it announces it to the handler, so
            # there is a window holding a connection nothing can close -- and from CPython
            # 3.12.1 wait_closed() waits for exactly that. abort_clients() exists from
            # 3.13 for this; below it, giving up beats hanging the suite.
            try:
                await asyncio.wait_for(server.wait_closed(), 5.0)
            except TimeoutError:
                # Only now. Aborting up front kills a handler that has not yet read the
                # bytes already sitting in its buffer, which silently empties
                # `received` -- measured on 3.13, 2026-09-24.
                abort = getattr(server, "abort_clients", None)  # CPython 3.13+
                if abort is not None:
                    abort()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(server.wait_closed(), 5.0)

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.connections += 1
        if self.connections > self.max_connections:
            writer.close()
            return
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    return
                self.received.append(data)
                reply = self.replies.pop(0) if self.replies else self.default
                if reply is None:
                    continue
                if reply.delay:
                    await asyncio.sleep(reply.delay)
                for index, chunk in enumerate(reply.chunks):
                    if index:
                        await asyncio.sleep(reply.gap)
                    writer.write(chunk)
                    await writer.drain()
                if reply.close_after:
                    writer.close()
                    return
        except (ConnectionError, asyncio.CancelledError):  # pragma: no cover - teardown
            return
        finally:
            # Detach the transport from the server. asyncio does NOT close it when the
            # handler returns, and from CPython 3.12.1 Server.wait_closed() waits for
            # every accepted connection to detach -- so a handler that returns without
            # closing its writer hangs the teardown forever. Before 3.12.1 wait_closed()
            # returned immediately and this was invisible, which is why CI hung on every
            # 3.12 cell and on no 3.11 cell (2026-09-24). Verified by experiment: adding
            # this one call makes test_one_transaction_reads_one_response pass on 3.12.
            writer.close()


E = TypeVar("E", bound=ConnectionEvent)


class Events:
    """An event sink that records, so a test can ask what the connection announced."""

    def __init__(self) -> None:
        self.seen: list[ConnectionEvent] = []

    def __call__(self, event: ConnectionEvent) -> None:
        self.seen.append(event)

    def of(self, kind: type[E]) -> list[E]:
        return [event for event in self.seen if isinstance(event, kind)]


async def a_connection(
    server: FakeServer,
    *,
    frame: FrameFormat = THREE_E,
    concurrency: Concurrency = Concurrency.STRICT,
    counters: Counters | None = None,
    events: Events | None = None,
    timeout: float = 2.0,
) -> Connection:
    connection = Connection(
        TcpTransport("127.0.0.1", server.port),
        frame=frame,
        codec=BINARY,
        timeout=timeout,
        concurrency=concurrency,
        counters=counters,
        events=events,
        connection_id="conn-test",
    )
    await connection.open()
    return connection


async def read_once(connection: Connection, *, mutates: bool = False) -> object:
    async with connection.transaction(command=0x0401, subcommand=0x0000) as txn:
        request = a_request(connection.frame, serial=txn.serial)
        response, _timing = await txn.exchange(
            request, connection.accumulator(txn.serial), mutates=mutates
        )
        return response


# --------------------------------------------------------------------------------------
# There is no public send
# --------------------------------------------------------------------------------------


def test_the_connection_exposes_no_way_to_write_bytes() -> None:
    """The mechanism, asserted as a property of the surface rather than of a docstring."""
    surface = {name for name in dir(Connection) if not name.startswith("_")}
    assert not surface & {"send", "sendall", "write", "send_request", "socket"}
    assert "transaction" in surface


def test_the_transports_expose_no_public_send_either() -> None:
    for transport in (TcpTransport, UdpTransport):
        surface = {name for name in dir(transport) if not name.startswith("_")}
        assert not surface & {"send", "sendall", "write", "sendto"}
        assert "exchange" in surface


# --------------------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------------------


async def test_one_transaction_reads_one_response() -> None:
    async with FakeServer(default=Reply(chunks=(a_response(),))) as server:
        connection = await a_connection(server)
        try:
            async with connection.transaction(command=0x0401) as txn:
                assert txn.serial is None  # 3E carries none
                response, timing = await txn.exchange(
                    a_request(), connection.accumulator(None), mutates=False
                )
                final = txn.decoded()
        finally:
            await connection.aclose()
    assert response.end_code == 0
    assert response.payload == b"\x2c\x01"
    assert timing.decoded_at is None  # nothing has been decoded when exchange returns
    assert final.decoded_at is not None
    assert final.wire_ns > 0
    assert connection.state is ConnectionState.CLOSED


async def test_the_previous_response_starts_the_next_host_gap() -> None:
    async with FakeServer(default=Reply(chunks=(a_response(),))) as server:
        connection = await a_connection(server)
        try:
            await read_once(connection)
            assert connection.prev_received_at is not None
            async with connection.transaction() as txn:
                _response, timing = await txn.exchange(
                    a_request(), connection.accumulator(None), mutates=False
                )
            assert timing.has_host_gap
            assert timing.host_gap_ns >= 0
        finally:
            await connection.aclose()


async def test_a_segmented_response_is_one_message() -> None:
    """The measured 1460-byte MSS split, arriving as two chunks 3 ms apart."""
    whole = a_response(payload=bytes(400))
    async with FakeServer(
        default=Reply(chunks=(whole[:11], whole[11:]), gap=0.01)
    ) as server:
        connection = await a_connection(server)
        try:
            async with connection.transaction() as txn:
                response, timing = await txn.exchange(
                    a_request(), connection.accumulator(None), mutates=False
                )
        finally:
            await connection.aclose()
    assert response.payload == bytes(400)
    assert timing.segmented
    assert timing.received_at == timing.chunks[-1].at


# --------------------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------------------


async def test_strict_refuses_a_second_transaction_on_one_connection() -> None:
    counters = Counters()
    async with FakeServer(default=Reply(chunks=(a_response(),), delay=0.05)) as server:
        connection = await a_connection(server, counters=counters)
        try:
            first = asyncio.create_task(read_once(connection))
            await asyncio.sleep(0.01)
            with pytest.raises(SlmpConcurrentTransactionError):
                await read_once(connection)
            await first
        finally:
            await connection.aclose()
    assert counters.concurrent_rejections == 1


async def test_serialize_puts_the_wait_in_queue_ns_and_never_in_wire_ns() -> None:
    """A queue that does not report its own delay makes every latency number a lie."""
    async with FakeServer(
        default=Reply(chunks=(a_response(),), delay=0.05)
    ) as server:
        connection = await a_connection(server, concurrency=Concurrency.SERIALIZE)
        timings: list[object] = []

        async def one() -> None:
            async with connection.transaction() as txn:
                await txn.exchange(
                    a_request(), connection.accumulator(None), mutates=False
                )
                timings.append(txn.decoded())

        try:
            await asyncio.gather(one(), one())
        finally:
            await connection.aclose()
    queued = [t for t in timings if t.queue_ns > 20_000_000]  # type: ignore[attr-defined]
    assert len(queued) == 1, "exactly one of the two waited for the gate"
    waited = queued[0]
    # The wait for the gate is in queue_ns and NOWHERE else. wire_ns stays a measurement
    # of the PLC: it must not have absorbed the ~50 ms this transaction spent queued,
    # or every latency number the library publishes would be a lie about the plant.
    assert waited.total_ns - waited.wire_ns > 20_000_000  # type: ignore[attr-defined]
    assert waited.queue_ns + waited.wire_ns <= waited.total_ns  # type: ignore[attr-defined]


async def test_a_token_is_single_use() -> None:
    async with FakeServer(default=Reply(chunks=(a_response(),))) as server:
        connection = await a_connection(server)
        try:
            async with connection.transaction() as txn:
                await txn.exchange(a_request(), connection.accumulator(None), mutates=False)
                with pytest.raises(SlmpUsageError) as caught:
                    await txn.exchange(
                        a_request(), connection.accumulator(None), mutates=False
                    )
        finally:
            await connection.aclose()
    assert "already been used" in str(caught.value)


async def test_a_token_carries_what_the_record_needs_to_say_who_it_was() -> None:
    async with FakeServer(default=Reply(chunks=(a_response(),))) as server:
        connection = await a_connection(server)
        try:
            async with connection.transaction(command=0x0403, subcommand=0x0080) as txn:
                assert (txn.command, txn.subcommand) == (0x0403, 0x0080)
                assert txn.sequence == 1
                assert txn.generation == 0
                assert not txn.prebuilt
                await txn.exchange(
                    a_request(), connection.accumulator(None), mutates=False, prebuilt=True
                )
                assert txn.prebuilt
        finally:
            await connection.aclose()


async def test_a_token_that_is_never_used_leaves_the_connection_healthy() -> None:
    async with FakeServer(default=Reply(chunks=(a_response(),))) as server:
        connection = await a_connection(server)
        try:
            async with connection.transaction() as txn:
                assert not txn.sent
            assert connection.state is ConnectionState.OPEN
            await read_once(connection)
        finally:
            await connection.aclose()


# --------------------------------------------------------------------------------------
# Failure is sticky
# --------------------------------------------------------------------------------------


async def test_a_timeout_closes_the_socket_and_goes_sticky_failed() -> None:
    events = Events()
    counters = Counters()
    async with FakeServer(default=None) as server:
        connection = await a_connection(
            server, events=events, counters=counters, timeout=0.2
        )
        try:
            with pytest.raises(SlmpTimeoutError):
                await read_once(connection)
            assert connection.state is ConnectionState.FAILED
            assert not connection.transport.is_open
            with pytest.raises(SlmpNotConnectedError) as caught:
                await read_once(connection)
        finally:
            await connection.aclose()
    assert caught.value.reason == "failed"
    assert "sticky" in str(caught.value)
    assert counters.timeouts == 1
    failures = events.of(ConnectionFailed)
    assert len(failures) == 1


async def test_a_zero_byte_read_on_the_first_transaction_is_entry_busy() -> None:
    counters = Counters()
    async with FakeServer(default=Reply(close_after=True)) as server:
        connection = await a_connection(server, counters=counters)
        try:
            with pytest.raises(SlmpConnectionEntryBusyError):
                await read_once(connection)
            assert connection.state is ConnectionState.FAILED
        finally:
            await connection.aclose()
    assert counters.entry_busy == 1


async def test_a_malformed_frame_becomes_a_public_protocol_error() -> None:
    """``aslmp.wire`` raises its own ValueError family; exactly one table translates."""
    counters = Counters()
    rubbish = b"\x99\x99" + bytes(20)
    async with FakeServer(default=Reply(chunks=(rubbish,))) as server:
        connection = await a_connection(server, counters=counters, timeout=0.3)
        try:
            with pytest.raises(SlmpProtocolError):
                await read_once(connection)
            assert connection.state is ConnectionState.FAILED
        finally:
            await connection.aclose()
    assert counters.protocol_errors == 1


async def test_a_4e_response_with_the_wrong_serial_is_caught_in_band() -> None:
    """The only in-band defence against the measured coalescing corruption."""
    async with FakeServer(
        default=Reply(chunks=(a_response(FOUR_E, serial=0xBEEF),))
    ) as server:
        connection = await a_connection(server, frame=FOUR_E, timeout=0.5)
        try:
            async with connection.transaction() as txn:
                assert txn.serial == 1
                with pytest.raises(SlmpProtocolError) as caught:
                    await txn.exchange(
                        a_request(FOUR_E, serial=txn.serial),
                        connection.accumulator(txn.serial),
                        mutates=False,
                    )
            assert connection.state is ConnectionState.FAILED
        finally:
            await connection.aclose()
    assert "serial" in str(caught.value).lower()


# --------------------------------------------------------------------------------------
# Graft G8: did not happen vs may have happened
# --------------------------------------------------------------------------------------


async def test_a_failed_write_command_is_outcome_unknown() -> None:
    async with FakeServer(default=None) as server:
        connection = await a_connection(server, timeout=0.2)
        try:
            with pytest.raises(SlmpOutcomeUnknownError) as caught:
                await read_once(connection, mutates=True)
        finally:
            await connection.aclose()
    error = caught.value
    assert error.sent is True
    assert error.reason is OutcomeUnknownReason.TIMEOUT
    assert isinstance(error.__cause__, SlmpTimeoutError)
    assert not isinstance(error, SlmpTimeoutError), "retrying a write is a data-loss bug"
    assert "read the affected devices back" in str(error)


async def test_a_failed_read_command_is_the_failure_itself() -> None:
    async with FakeServer(default=None) as server:
        connection = await a_connection(server, timeout=0.2)
        try:
            with pytest.raises(SlmpTimeoutError):
                await read_once(connection, mutates=False)
        finally:
            await connection.aclose()


async def test_a_request_that_provably_never_left_is_not_outcome_unknown() -> None:
    """"Did not happen" and "may have happened" have different recoveries (graft G8)."""
    async with FakeServer(default=Reply(chunks=(a_response(),))) as server:
        connection = await a_connection(server)
        try:
            async with connection.transaction() as txn:
                with pytest.raises(SlmpNotSentError):
                    await txn.exchange(b"", connection.accumulator(None), mutates=True)
                assert txn.sent is False
        finally:
            await connection.aclose()


# --------------------------------------------------------------------------------------
# Lifecycle, generation, events
# --------------------------------------------------------------------------------------


async def test_open_confirm_and_close_announce_themselves() -> None:
    events = Events()
    counters = Counters()
    async with FakeServer(default=Reply(chunks=(a_response(),))) as server:
        connection = await a_connection(server, events=events, counters=counters)
        opened = connection.state
        info = connection.confirm()
        ready = connection.state
        assert opened is ConnectionState.OPEN
        assert ready is ConnectionState.READY
        assert info.connection_id == "conn-test"
        assert info.generation == 0
        assert info.peer == ("127.0.0.1", server.port)
        assert info.local[1] != 0
        assert connection.info == info
        await connection.aclose()
        await connection.aclose()
    assert len(events.of(Connecting)) == 1
    assert len(events.of(Connected)) == 1
    assert len(events.of(Disconnected)) == 1
    assert counters.connects == 1
    assert counters.disconnects == 1


async def test_confirm_is_legal_only_from_open() -> None:
    async with FakeServer() as server:
        connection = await a_connection(server)
        connection.confirm()
        with pytest.raises(SlmpNotConnectedError):
            connection.confirm()
        await connection.aclose()


async def test_open_is_legal_only_once_and_names_reopen() -> None:
    async with FakeServer() as server:
        connection = await a_connection(server)
        try:
            with pytest.raises(SlmpNotConnectedError) as caught:
                await connection.open()
        finally:
            await connection.aclose()
    assert "reopen" in str(caught.value)


async def test_reopen_bumps_the_generation() -> None:
    """The anti-lie field: a rebuilt socket can never look like the one before it."""
    events = Events()
    counters = Counters()
    async with FakeServer(
        replies=[Reply(close_after=True)], default=Reply(chunks=(a_response(),))
    ) as server:
        connection = await a_connection(server, events=events, counters=counters)
        try:
            with pytest.raises(SlmpConnectionEntryBusyError):
                await read_once(connection)
            assert connection.generation == 0
            await connection.reopen(reason="test")
            assert connection.generation == 1
            assert connection.state is ConnectionState.OPEN
            assert connection.prev_received_at is None
            await read_once(connection)
        finally:
            await connection.aclose()
    assert counters.reconnects == 1
    assert len(events.of(Connecting)) == 2
    assert events.of(Connecting)[1].generation == 1


async def test_a_closed_connection_is_not_resurrected() -> None:
    async with FakeServer() as server:
        connection = await a_connection(server)
        await connection.aclose()
        with pytest.raises(SlmpNotConnectedError) as caught:
            await connection.reopen(reason="test")
    assert "build a new one" in str(caught.value)


async def test_a_connect_failure_is_announced_and_leaves_it_failed() -> None:
    events = Events()
    counters = Counters()
    async with FakeServer(max_connections=0) as server:
        connection = Connection(
            TcpTransport("127.0.0.1", server.port),
            frame=THREE_E,
            codec=BINARY,
            timeout=0.3,
            events=events,
            counters=counters,
        )
        with pytest.raises(SlmpConnectionEntryBusyError):
            await connection.open()
            await read_once(connection)
        await connection.aclose()
    assert connection.state in (ConnectionState.FAILED, ConnectionState.CLOSED)
    assert counters.entry_busy >= 1
    if events.of(ConnectFailed):
        assert events.of(ConnectFailed)[0].peer[0] == "127.0.0.1"


# --------------------------------------------------------------------------------------
# The transport observer: drops and rebinds
# --------------------------------------------------------------------------------------


async def test_a_rebind_bumps_the_generation_and_is_announced() -> None:
    events = Events()
    counters = Counters()
    async with FakeServer() as server:
        connection = await a_connection(server, events=events, counters=counters)
        try:
            connection.socket_rebound(
                previous_local=("127.0.0.1", 51000),
                local=("127.0.0.1", 51001),
                reason="datagram lost",
            )
            assert connection.generation == 1
            assert counters.socket_rebinds == 1
            rebound = events.of(SocketRebound)
            assert len(rebound) == 1
            assert rebound[0].generation == 1
        finally:
            await connection.aclose()


async def test_dropped_datagrams_are_counted_by_the_reason_they_were_dropped() -> None:
    events = Events()
    counters = Counters()
    async with FakeServer() as server:
        connection = await a_connection(server, events=events, counters=counters)
        try:
            connection.datagram_dropped(
                reason="foreign-source", nbytes=24, source=("1.2.3.4", 9)
            )
            connection.datagram_dropped(reason="stale-epoch", nbytes=24, source=None)
            connection.datagram_dropped(reason="unmatched", nbytes=24, source=None)
        finally:
            await connection.aclose()
    assert counters.foreign_datagrams == 1
    assert counters.stale_datagrams == 2
    assert len(events.of(DatagramDropped)) == 3


async def test_a_udp_rebind_reaches_the_generation_through_the_connection() -> None:
    """End to end: the transport reports, the connection counts and bumps the generation.

    The wiring is the point. The transport is built before the connection exists, so if
    the connection did not attach itself as the observer, a UDP source-port rebind would
    happen with nothing to bump ``generation`` -- and a fresh local port would be
    invisible in every transaction record taken afterwards, which is the hidden-reconnect
    lie this field exists to prevent.
    """
    events = Events()
    counters = Counters()
    listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    listener.bind(("127.0.0.1", 0))
    port = int(listener.getsockname()[1])
    connection = Connection(
        UdpTransport("127.0.0.1", port, carries_serial=False, source_host="127.0.0.1"),
        frame=THREE_E,
        codec=BINARY,
        timeout=0.2,
        events=events,
        counters=counters,
        connection_id="conn-udp",
    )
    await connection.open()
    before = connection.binding
    assert before is not None
    try:
        with pytest.raises(SlmpTimeoutError):
            await read_once(connection)
        assert connection.state is ConnectionState.FAILED
    finally:
        await connection.aclose()
        listener.close()
    assert connection.generation == 1
    assert counters.socket_rebinds == 1
    rebound = events.of(SocketRebound)
    assert len(rebound) == 1
    assert rebound[0].previous_local == before.local
    assert rebound[0].local != before.local


# --------------------------------------------------------------------------------------
# Serials and correlation
# --------------------------------------------------------------------------------------


def test_the_serial_allocator_never_issues_zero_and_wraps() -> None:
    allocator = SerialAllocator(start=0xFFFE)
    assert [allocator.next() for _ in range(4)] == [0xFFFE, 0xFFFF, 1, 2]


def test_the_serial_allocator_refuses_an_impossible_start() -> None:
    for start in (0, 0x10000, -1):
        with pytest.raises(SlmpUsageError):
            SerialAllocator(start)


async def test_a_4e_connection_allocates_a_serial_per_transaction() -> None:
    async with FakeServer(max_connections=1) as server:
        connection = await a_connection(server, frame=FOUR_E)
        try:
            async with connection.transaction() as first:
                assert first.serial == 1
            async with connection.transaction() as second:
                assert second.serial == 2
        finally:
            await connection.aclose()


async def test_the_udp_correlation_matches_only_this_transactions_serial() -> None:
    """The transport routes a datagram without ever learning what a serial No. is."""
    async with FakeServer() as server:
        connection = await a_connection(server, frame=FOUR_E)
        try:
            correlation = connection.correlation_for(0x1234)
            assert correlation.label == 0x1234
            assert correlation.matches(a_response(FOUR_E, serial=0x1234))
            assert not correlation.matches(a_response(FOUR_E, serial=0x1235))
            assert not correlation.matches(b"not a frame at all")
            assert not correlation.matches(b"")
        finally:
            await connection.aclose()


async def test_a_3e_connection_has_no_correlation_to_offer() -> None:
    """Which is why 3E is never given a UDP in-flight depth above one."""
    async with FakeServer() as server:
        connection = await a_connection(server)
        try:
            correlation = connection.correlation_for(None)
            assert correlation.label is None
            assert correlation.matches(b"anything")
        finally:
            await connection.aclose()


# --------------------------------------------------------------------------------------
# Sinks
# --------------------------------------------------------------------------------------


async def test_a_broken_event_sink_is_reported_once_per_generation_and_counted() -> None:
    counters = Counters()

    def explode(event: ConnectionEvent) -> None:
        raise RuntimeError("the sink is broken")

    async with FakeServer(default=Reply(chunks=(a_response(),))) as server:
        connection = Connection(
            TcpTransport("127.0.0.1", server.port),
            frame=THREE_E,
            codec=BINARY,
            events=explode,
            counters=counters,
        )
        with pytest.raises(SlmpSinkError) as caught:
            await connection.open()
        # A listener that raises is a connect failure like any other: loud, counted, and
        # it leaves the state machine somewhere an explicit reopen can move it from --
        # never stranded mid-transition by a callback bug.
        assert connection.state is ConnectionState.FAILED
        assert counters.sink_errors >= 1
        reported = counters.sink_errors
        await connection.aclose()  # counted again, and this one does NOT raise
        assert counters.sink_errors > reported
    assert caught.value.sink == "on_event"
    assert "counters.sink_errors" in str(caught.value)


# --------------------------------------------------------------------------------------
# Remote Reset: the one request whose absent answer is the expected outcome
# --------------------------------------------------------------------------------------


async def test_a_request_that_expects_no_response_still_burns_the_token() -> None:
    async with FakeServer(default=None) as server:
        connection = await a_connection(server)
        try:
            async with connection.transaction(command=0x1006) as txn:
                timing = await txn.exchange_without_response(a_request(), mutates=True)
                assert txn.sent
                with pytest.raises(SlmpUsageError):
                    await txn.exchange_without_response(a_request(), mutates=True)
            assert timing.sent_at > 0
        finally:
            await connection.aclose()


async def test_a_request_that_expects_no_response_retires_the_socket() -> None:
    """The regression for the desynchronised-but-READY socket.

    ``exchange_without_response`` used to leave the connection OPEN and the socket in
    service. Nothing read the answer, so nothing could prove there was not one waiting,
    and the next transaction on the same connection would decode the previous request's
    response as its own -- with end code 0x0000 and, on 3E, no serial No. to catch it.
    """
    async with FakeServer(default=None) as server:
        connection = await a_connection(server)
        try:
            async with connection.transaction(command=0x1006) as txn:
                await txn.exchange_without_response(a_request(), mutates=True)
            assert connection.state is ConnectionState.FAILED
            assert not connection.transport.is_open
            with pytest.raises(SlmpNotConnectedError):
                connection.require_usable("read the next register")
        finally:
            await connection.aclose()


async def test_a_retired_socket_is_recoverable_by_an_explicit_reconnect() -> None:
    """FAILED rather than CLOSED, so 0x1006's caller can reopen after the CPU comes back."""
    async with FakeServer(default=None) as server:
        connection = await a_connection(server)
        try:
            async with connection.transaction(command=0x1006) as txn:
                await txn.exchange_without_response(a_request(), mutates=True)
            await connection.reopen(reason="the CPU finished resetting")
            assert connection.state is ConnectionState.OPEN
            assert connection.generation == 1
        finally:
            await connection.aclose()


def test_repr_says_which_connection_and_which_generation() -> None:
    connection = Connection(
        TcpTransport("192.168.10.250", 5002),
        frame=THREE_E,
        codec=BINARY,
        connection_id="conn-9",
    )
    assert "conn-9" in repr(connection)
    assert "generation=0" in repr(connection)
    assert connection.state is ConnectionState.NEW
    assert connection.route is Route.OWN_STATION
    assert connection.codec is BINARY
    assert connection.kind.value == "tcp"


def a_clock() -> int:
    return time.monotonic_ns()
