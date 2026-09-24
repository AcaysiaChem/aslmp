"""The TCP transport, against a real loopback server we control. No PLC involved.

Every behaviour asserted here was measured on **MELSEC iQ-F FX5U-32MT/DS fw 1.065** and
none of them can be reproduced against a PLC on demand -- the CPU FINs a second
connection to a busy entry, splits a 1931-byte response at the MSS boundary on one read
in three, and answers a coding mismatch with silence. A fake server reproduces all three
deterministically, which is the only way they get a regression test at all.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket  # noqa: TID251 - transport tests need a real socket; that is the point
import struct
import time
from dataclasses import dataclass, field
from types import TracebackType
from typing import Self
from unittest.mock import patch

import pytest

from aslmp.errors import (
    SlmpConcurrentTransactionError,
    SlmpConnectionEntryBusyError,
    SlmpConnectionLostError,
    SlmpNotConnectedError,
    SlmpProtocolError,
    SlmpTimeoutError,
    TimeoutCause,
)
from aslmp.timing import TimingBuilder
from aslmp.transport.base import Deadline
from aslmp.transport.tcp import TcpTransport

REQUEST = (
    b"\x50\x00\x00\xff\xff\x03\x00\x0c\x00\x00\x00"
    b"\x01\x04\x00\x00\xa8\x00\x00\x00\xd0\x02\x00"
)
"""One real 3E/binary Read Words request. The transport never looks inside it."""


# --------------------------------------------------------------------------------------
# A fake SLMP-shaped server. It never parses anything; it replays scripted bytes.
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class Reply:
    """What the server does when a request arrives."""

    chunks: tuple[bytes, ...] = ()
    delay: float = 0.0
    """Held before the first byte goes out -- the PLC's ~7 ms service processing."""
    gap: float = 0.0
    """Held between chunks -- the measured 3.0 ms MSS split."""
    close_after: bool = False


@dataclass(slots=True)
class FakeServer:
    """A loopback listener that replays scripted replies and can behave like an FX5U.

    ``max_connections`` reproduces the measured one-connection-per-entry rule: the CPU
    *accepts* the second connection and then immediately FINs it, so ``socket.connect()``
    succeeds and the client learns the truth from a zero-byte read.
    """

    replies: list[Reply] = field(default_factory=list)
    default: Reply | None = None
    max_connections: int = 1
    abortive_close: bool = False
    """Refuse the over-limit connection with RST instead of FIN.

    Which of the two a busy entry produces belongs to the HOST's TCP stack, not to
    the PLC. CI measured windows-latest delivering FIN and ubuntu-latest delivering
    ECONNRESET for the identical server and client, 2026-09-24.
    """
    received: list[bytes] = field(default_factory=list)
    connections: int = 0
    port: int = 0
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
            if self.abortive_close:
                raw = writer.get_extra_info("socket")
                raw.setsockopt(
                    socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
                )
            writer.close()  # accept, then FIN (or RST): exactly what the FX5U does
            return
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    return
                self.received.append(data)
                reply = self.replies.pop(0) if self.replies else self.default
                if reply is None:
                    continue  # silence: the measured coding-mismatch failure
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


class FixedLength:
    """A ``Reassembler`` that wants exactly ``total`` bytes and knows nothing else.

    The transport is not allowed to import ``aslmp.wire``, so its tests do not either:
    if these pass with this three-line stand-in, the transport really is driving the
    read from the structural protocol and not from a frame it secretly understands.
    """

    def __init__(self, total: int) -> None:
        self.total = total
        self.data = bytearray()

    @property
    def bytes_needed(self) -> int:
        return self.total - len(self.data)

    def feed(self, data: bytes, /) -> None:
        self.data += data


def a_deadline(seconds: float = 2.0) -> Deadline:
    return Deadline.after(seconds, clock=time.monotonic_ns)


def a_timing() -> TimingBuilder:
    timing = TimingBuilder(time.monotonic_ns)
    timing.gate_acquired()
    timing.encoded()
    return timing


async def connected(server: FakeServer) -> TcpTransport:
    transport = TcpTransport("127.0.0.1", server.port)
    await transport.open(a_deadline())
    return transport


# --------------------------------------------------------------------------------------
# Open
# --------------------------------------------------------------------------------------


async def test_open_reports_both_ends_and_sets_nodelay() -> None:
    async with FakeServer() as server:
        transport = TcpTransport("127.0.0.1", server.port)
        binding = await transport.open(a_deadline())
        try:
            assert binding.peer == ("127.0.0.1", server.port)
            assert binding.local[1] != 0  # a silent socket rebuild shows up here
            assert transport.is_open
            assert transport.binding == binding
            assert transport.max_in_flight == 1
        finally:
            await transport.close()


async def test_open_refuses_a_second_socket_on_one_transport() -> None:
    async with FakeServer() as server:
        transport = await connected(server)
        try:
            with pytest.raises(SlmpNotConnectedError):
                await transport.open(a_deadline())
        finally:
            await transport.close()


async def test_close_is_idempotent() -> None:
    async with FakeServer() as server:
        transport = await connected(server)
        await transport.close()
        await transport.close()
        assert not transport.is_open


async def test_a_second_connection_to_a_busy_entry_is_named_not_guessed() -> None:
    """The measured accept-then-FIN. Graft G2 classifies it; the first read proves it.

    Whether the FIN has arrived by the time the non-blocking probe runs is a scheduling
    race on loopback, and the design says so: the probe classifies, it never proves. So
    the assertion is on the promise that actually holds -- a second connection to a
    one-entry configuration raises :class:`SlmpConnectionEntryBusyError`, at the open or
    at the first read, and never returns wrong data or a bare timeout.
    """
    async with FakeServer(max_connections=1) as server:
        first = await connected(server)
        try:
            second = TcpTransport("127.0.0.1", server.port)
            with pytest.raises(SlmpConnectionEntryBusyError) as caught:
                await second.open(a_deadline())
                await second.exchange(
                    REQUEST, FixedLength(20), a_deadline(), a_timing()
                )
            await second.close()
            assert "entry" in str(caught.value).lower()
        finally:
            await first.close()


async def a_pair(*, then: str) -> tuple[socket.socket, socket.socket]:
    """A connected loopback pair where the server end has already done ``then``.

    The test waits until the client end can actually *see* the consequence with a
    non-blocking peek, so graft G2's classifier is then exercised with no race at all.
    ``open()`` itself cannot be pinned down this way -- whether the FIN has arrived by
    the time the probe runs is exactly the scheduling race the design calls out, which
    is why the probe classifies and the handshake proves.
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client.setblocking(False)
    loop = asyncio.get_running_loop()
    await loop.sock_connect(client, listener.getsockname())
    server, _ = listener.accept()
    listener.close()
    if then == "fin":
        server.close()
    else:
        server.sendall(b"\xd0\x00hello")
    for _ in range(500):
        try:
            if client.recv(1, socket.MSG_PEEK) is not None:
                break
        except BlockingIOError:
            await asyncio.sleep(0.002)
    return client, server


def assert_names_both_entry_busy_causes(error: SlmpConnectionEntryBusyError) -> None:
    """Both causes, the measured window and the settle, in the message itself.

    The wording is load-bearing. An outside reviewer met this error repeatedly with
    nothing else connected to the CPU: the same client was reconnecting to the entry it
    had just released, inside the CPU's FIN processing, and the old message -- "the SLMP
    connection entry is in use by another client" -- sent them hunting a second client
    that did not exist.

    The race itself is deliberately **not** reproduced anywhere in this file. It is a
    property of the CPU's FIN processing rather than of this code, the window is about
    2 ms on our wired link and *wider* on a faster one, and a test that tried to hit it
    would flake on every machine that is not that bench. The measurement is in
    ``docs/hardware.md`` section 2.1 and in ``aslmp/data/ambiguities.tsv`` as
    ``A-ENTRY-RELEASE-RACE``; this asserts only that the message carries it.
    """
    message = str(error)
    assert "another client" in message, "the second-client cause must still be named"
    assert "FIN" in message, "the self-inflicted cause is a race against FIN processing"
    assert "reconnect" in message, "say that reconnecting is what did it"
    assert "2 ms" in message and "3.64 ms" in message, "give the measured window and link"
    assert "link-dependent" in message, "a faster link widens the window; say so"
    assert "settle" in message, "name the fix"
    assert "not a retry loop" in message, "and rule out the wrong one"


async def test_a_fin_that_has_already_arrived_is_named_entry_busy() -> None:
    """Graft G2, with the race removed: the FIN is provably delivered before the check."""
    client, server = await a_pair(then="fin")
    transport = TcpTransport("127.0.0.1", 5002)
    try:
        with pytest.raises(SlmpConnectionEntryBusyError) as caught:
            transport._check_no_early_eof(client)
    finally:
        client.close()
        server.close()
    assert_names_both_entry_busy_causes(caught.value)
    assert "pooling" in str(caught.value)


async def test_unsolicited_bytes_before_anything_was_sent_are_refused() -> None:
    """Bytes on a fresh connection are somebody's message; guessing whose desynchronises."""
    client, server = await a_pair(then="greet")
    transport = TcpTransport("127.0.0.1", 5002)
    try:
        with pytest.raises(SlmpProtocolError) as caught:
            transport._check_no_early_eof(client)
    finally:
        client.close()
        server.close()
    assert "unsolicited" in str(caught.value)


async def test_a_healthy_fresh_connection_passes_the_probe_with_no_wait() -> None:
    """The common case: nothing has arrived, the check costs nothing and says nothing."""
    async with FakeServer() as server:
        transport = await connected(server)
        try:
            assert transport.is_open
        finally:
            await transport.close()


async def test_connect_to_a_dead_port_never_reports_success() -> None:
    """A refused connect and a black-holed one are both failures, and neither is silent.

    Which one a host produces is not ours to choose -- a firewall that drops instead of
    resetting turns the refusal into silence -- so both outcomes are asserted, and both
    carry their own diagnosis.
    """
    closed = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    closed.bind(("127.0.0.1", 0))
    port = int(closed.getsockname()[1])
    closed.close()
    transport = TcpTransport("127.0.0.1", port)
    with pytest.raises((SlmpNotConnectedError, SlmpTimeoutError)) as caught:
        await transport.open(a_deadline(1.0))
    error: BaseException = caught.value
    if isinstance(error, SlmpNotConnectedError):
        assert error.reason == "connect-failed"
        assert "Nothing was sent" in str(error)
    elif isinstance(error, SlmpTimeoutError):
        assert TimeoutCause.WRONG_PORT in error.likely_causes
    assert not transport.is_open


# --------------------------------------------------------------------------------------
# Exchange
# --------------------------------------------------------------------------------------


async def test_one_exchange_writes_the_request_and_reads_exactly_the_response() -> None:
    response = bytes(range(20))
    async with FakeServer(default=Reply(chunks=(response,))) as server:
        transport = await connected(server)
        try:
            reassembler = FixedLength(20)
            timing = a_timing()
            result = await transport.exchange(REQUEST, reassembler, a_deadline(), timing)
        finally:
            await transport.close()
    assert server.received == [REQUEST]
    assert bytes(reassembler.data) == response
    assert result.sent and result.responded
    assert result.bytes_sent == len(REQUEST)
    assert result.bytes_received == 20
    assert timing.sent_at is not None


async def test_an_exchange_that_reads_no_response_closes_the_socket() -> None:
    """This transport has no ``send``, and ``expect_response=False`` must not become one.

    The request goes out and nothing reads the answer, so nothing here can prove there is
    not one on its way. A socket kept open after that hands those bytes to the next
    exchange as fresh data with end code 0x0000, and 3E has no serial No. to reveal it --
    which is exactly what happened, because this branch used to return early without
    reading *or* closing.
    """
    # The delay is load-bearing, and not for timing. Without it the server's 20 bytes
    # are already sitting unread in the client's receive buffer when this branch closes
    # the socket, and closing a socket with unread data queued makes the OS send RST
    # rather than FIN -- which discards the peer's buffers too, so the request this test
    # then asserts on can vanish from `server.received`. Measured on CI 2026-09-24:
    # failed on macos/3.11, macos/3.13, ubuntu/3.11 and windows/3.11, passed on
    # ubuntu/3.13 and windows/3.13, which is the signature of a race and not of a rule.
    # Holding the reply means nothing is unread at close, so the close is a clean FIN and
    # the server keeps what it already read. It is also the more faithful scenario: the
    # answer is genuinely still in flight, which is the case the docstring describes.
    async with FakeServer(default=Reply(chunks=(bytes(20),), delay=2.0)) as server:
        transport = await connected(server)
        try:
            result = await transport.exchange(
                REQUEST,
                FixedLength(20),
                a_deadline(),
                a_timing(),
                expect_response=False,
            )
            assert result.sent and not result.responded
            assert result.bytes_received == 0
            assert not transport.is_open
            with pytest.raises(SlmpNotConnectedError):
                await transport.exchange(
                    REQUEST, FixedLength(20), a_deadline(), a_timing()
                )
            # Inside the block, and bounded, on purpose. The client abandons this
            # connection the instant the request is written, so asserting a SERVER-side
            # fact after the server has been torn down races the handler's first read:
            # it failed on macOS while passing on ubuntu and windows (CI 2026-09-24).
            # Waiting here gives the handler the scheduling it is entitled to, while the
            # listener is still up, and turns a platform race into a stated deadline.
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 5.0
            while not server.received:
                if loop.time() > deadline:
                    raise AssertionError(
                        "the server never read the request the client says it sent"
                    )
                await asyncio.sleep(0.005)
            assert server.received == [REQUEST]
        finally:
            await transport.close()


async def test_the_read_stops_at_the_message_and_leaves_the_next_one_alone() -> None:
    """Never ``recv(4096)``. A surplus read is how the next transaction gets stale bytes."""
    async with FakeServer(default=Reply(chunks=(bytes(40),))) as server:
        transport = await connected(server)
        try:
            reassembler = FixedLength(20)
            result = await transport.exchange(
                REQUEST, reassembler, a_deadline(), a_timing()
            )
            assert result.bytes_received == 20
            assert len(reassembler.data) == 20
            # The other 20 bytes are still on the socket, unread and unattributed.
            second = FixedLength(20)
            await transport.exchange(REQUEST, second, a_deadline(), a_timing())
            assert len(second.data) == 20
        finally:
            await transport.close()


async def test_a_segmented_response_is_reassembled_and_stamped_on_the_last_chunk() -> None:
    """The measured 1460 + 471 split. Stamping the first chunk reports 11 ms for 14 ms."""
    head, tail = bytes(1460), bytes(471)
    async with FakeServer(default=Reply(chunks=(head, tail), gap=0.02)) as server:
        transport = await connected(server)
        try:
            reassembler = FixedLength(1931)
            timing = a_timing()
            result = await transport.exchange(REQUEST, reassembler, a_deadline(), timing)
        finally:
            await transport.close()
    assert result.bytes_received == 1931
    assert len(reassembler.data) == 1931
    assert len(timing.chunks) >= 2
    record = timing.build()
    assert record.segmented
    assert record.received_at == record.chunks[-1].at
    assert record.first_byte_at == record.chunks[0].at
    assert record.transfer_ns >= 15_000_000  # the 20 ms gap the server held


async def test_silence_raises_a_timeout_that_blames_the_coding_first() -> None:
    """Wrong coding, wrong frame, wrong transport: all three fail by saying nothing."""
    async with FakeServer(default=None) as server:
        transport = await connected(server)
        try:
            with pytest.raises(SlmpTimeoutError) as caught:
                await transport.exchange(
                    REQUEST, FixedLength(20), a_deadline(0.2), a_timing()
                )
        finally:
            await transport.close()
    assert caught.value.likely_causes[0] is TimeoutCause.CODING_MISMATCH
    assert caught.value.bytes_received == 0


async def test_a_partial_response_blames_the_overstated_length_first() -> None:
    async with FakeServer(default=Reply(chunks=(bytes(9),))) as server:
        transport = await connected(server)
        try:
            with pytest.raises(SlmpTimeoutError) as caught:
                await transport.exchange(
                    REQUEST, FixedLength(20), a_deadline(0.25), a_timing()
                )
        finally:
            await transport.close()
    assert caught.value.likely_causes[0] is TimeoutCause.REQUEST_LENGTH_OVERSTATED
    assert caught.value.bytes_received == 9


async def test_silence_after_a_working_transaction_blames_the_plc_not_the_coding() -> None:
    async with FakeServer(replies=[Reply(chunks=(bytes(20),))], default=None) as server:
        transport = await connected(server)
        try:
            await transport.exchange(REQUEST, FixedLength(20), a_deadline(), a_timing())
            assert transport.transactions_completed == 1
            with pytest.raises(SlmpTimeoutError) as caught:
                await transport.exchange(
                    REQUEST, FixedLength(20), a_deadline(0.2), a_timing()
                )
        finally:
            await transport.close()
    assert caught.value.likely_causes[0] is TimeoutCause.PLC_STOPPED_OR_RESET
    assert TimeoutCause.CODING_MISMATCH not in caught.value.likely_causes


async def test_a_zero_byte_read_on_the_first_transaction_is_entry_busy() -> None:
    async with FakeServer(default=Reply(close_after=True)) as server:
        transport = await connected(server)
        try:
            with pytest.raises(SlmpConnectionEntryBusyError) as caught:
                await transport.exchange(
                    REQUEST, FixedLength(20), a_deadline(), a_timing()
                )
        finally:
            await transport.close()
    assert_names_both_entry_busy_causes(caught.value)
    assert "coding" in str(caught.value), "nothing was read, so nothing was proven"


async def test_the_entry_busy_message_does_not_send_the_reader_hunting_one_cause() -> None:
    """The reviewer's afternoon: this error with nothing else connected to the CPU.

    Both entry-busy paths -- the FIN already waiting at connect, and the zero-byte read
    on the first transaction -- must offer the reconnect race as well as the second
    client, because with no default backoff on reconnect the race is the common way to
    get here. See ``docs/hardware.md`` section 2.1 for the numbers behind the wording.
    """
    at_connect: SlmpConnectionEntryBusyError
    client, server = await a_pair(then="fin")
    transport = TcpTransport("127.0.0.1", 5002)
    try:
        with pytest.raises(SlmpConnectionEntryBusyError) as caught:
            transport._check_no_early_eof(client)
        at_connect = caught.value
    finally:
        client.close()
        server.close()

    async with FakeServer(default=Reply(close_after=True)) as fake:
        second = await connected(fake)
        try:
            with pytest.raises(SlmpConnectionEntryBusyError) as caught:
                await second.exchange(REQUEST, FixedLength(20), a_deadline(), a_timing())
        finally:
            await second.close()

    for error in (at_connect, caught.value):
        assert_names_both_entry_busy_causes(error)
        text = str(error)
        assert "in use by another client" not in text, (
            "the old wording named one cause as if it were the only one, and it is the "
            "less likely of the two"
        )
        assert "300 ms" not in text, (
            "the settle is 5 ms against a measured 2 ms window; 300 ms was the "
            "reviewer's guess and this library does not publish guesses as numbers"
        )


async def test_a_zero_byte_read_after_a_working_transaction_is_a_lost_connection() -> None:
    """The entry was demonstrably ours, so the same EOF means something else entirely."""
    async with FakeServer(
        replies=[Reply(chunks=(bytes(20),)), Reply(close_after=True)]
    ) as server:
        transport = await connected(server)
        try:
            await transport.exchange(REQUEST, FixedLength(20), a_deadline(), a_timing())
            with pytest.raises(SlmpConnectionLostError):
                await transport.exchange(
                    REQUEST, FixedLength(20), a_deadline(), a_timing()
                )
        finally:
            await transport.close()


async def test_a_truncated_response_then_a_close_is_a_lost_connection() -> None:
    async with FakeServer(default=Reply(chunks=(bytes(9),), close_after=True)) as server:
        transport = await connected(server)
        try:
            with pytest.raises(SlmpConnectionLostError) as caught:
                await transport.exchange(
                    REQUEST, FixedLength(20), a_deadline(), a_timing()
                )
        finally:
            await transport.close()
    assert "9 byte(s)" in str(caught.value)


async def test_exchange_on_a_closed_transport_says_nothing_was_sent() -> None:
    transport = TcpTransport("127.0.0.1", 1)
    with pytest.raises(SlmpNotConnectedError) as caught:
        await transport.exchange(REQUEST, FixedLength(20), a_deadline(), a_timing())
    assert caught.value.reason == "closed"


async def test_two_concurrent_exchanges_are_refused_by_the_transport_itself() -> None:
    """Belt and braces beneath the gate: this is the corruption, so both layers refuse."""
    async with FakeServer(default=Reply(chunks=(bytes(20),), delay=0.05)) as server:
        transport = await connected(server)
        try:
            first = asyncio.create_task(
                transport.exchange(REQUEST, FixedLength(20), a_deadline(), a_timing())
            )
            await asyncio.sleep(0.01)
            with pytest.raises(SlmpConcurrentTransactionError) as caught:
                await transport.exchange(
                    REQUEST, FixedLength(20), a_deadline(), a_timing()
                )
            await first
        finally:
            await transport.close()
    assert "0x0000" in str(caught.value)


async def test_an_exchange_that_expects_no_response_sends_and_returns() -> None:
    """Remote Reset is the only such request; the CPU resets before it can answer."""
    async with FakeServer(default=None) as server:
        transport = await connected(server)
        try:
            result = await transport.exchange(
                REQUEST, FixedLength(20), a_deadline(), a_timing(), expect_response=False
            )
        finally:
            await transport.close()
    assert result.sent
    assert not result.responded
    assert result.bytes_received == 0


async def test_transactions_completed_resets_when_the_socket_is_rebuilt() -> None:
    """A reconnect makes the per-connection facts unproven again, so the count restarts."""
    async with FakeServer(default=Reply(chunks=(bytes(20),)), max_connections=5) as server:
        transport = await connected(server)
        await transport.exchange(REQUEST, FixedLength(20), a_deadline(), a_timing())
        assert transport.transactions_completed == 1
        await transport.close()
        await transport.open(a_deadline())
        try:
            assert transport.transactions_completed == 0
        finally:
            await transport.close()


def test_repr_says_where_it_points_and_whether_it_is_open() -> None:
    transport = TcpTransport("192.168.10.250", 5002)
    assert "192.168.10.250" in repr(transport)
    assert "closed" in repr(transport)


PARTIAL_PREFIX = bytes([80, 0, 0, 255])
"""Four bytes of a 3E response prefix: enough for "some of it arrived"."""


async def test_a_busy_entry_is_entry_busy_whether_it_closes_cleanly_or_abortively() -> None:
    """Same error on every platform, because the difference is the host's, not the PLC's.

    A second connection to a one-entry SLMP configuration is accepted and then ended. The
    CPU does one thing; the local TCP stack decides how the client sees it. Windows
    delivers FIN, so ``recv`` returns 0. Linux delivers RST, so ``recv`` raises
    ``ECONNRESET`` and the classifier never ran.

    CI found this on the first nine-cell run, 2026-09-24: identical simulator, identical
    client, ``SlmpConnectionEntryBusyError`` on windows-latest and
    ``SlmpConnectionLostError`` on ubuntu-latest. This library's most precisely-named
    error existed only on the platform it was developed on, and Linux is where most
    industrial Python runs.

    ``SO_LINGER 0`` asks for the abortive case and on Linux that is what arrives. On
    Windows the local stack still reports a clean EOF, so **this test does not exercise
    the RST path there** -- confirmed by mutation: deleting the ``ConnectionResetError``
    branch leaves it green on Windows. Its companion below injects the error directly and
    is the one that holds everywhere. Both are kept: this is the only end-to-end evidence,
    and only Linux CI can supply it.
    """
    for abortive in (False, True):
        async with FakeServer(max_connections=1, abortive_close=abortive) as server:
            first = await connected(server)
            try:
                second = TcpTransport("127.0.0.1", server.port)
                with pytest.raises(SlmpConnectionEntryBusyError) as caught:
                    await second.open(a_deadline())
                    await second.exchange(REQUEST, FixedLength(20), a_deadline(), a_timing())
                await second.close()
                assert "entry" in str(caught.value).lower(), (
                    f"abortive_close={abortive} gave: {caught.value}"
                )
            finally:
                await first.close()


async def test_econnreset_with_nothing_read_is_classified_as_entry_busy() -> None:
    """The platform-independent half, and the one that actually bites.

    The end-to-end test above cannot produce ECONNRESET on Windows, so it stays green
    there with the fix deleted. This injects the error the Linux stack raises, so the
    classification is pinned wherever the suite runs.
    """
    async with FakeServer(max_connections=1, default=Reply(chunks=(b"x" * 20,))) as server:
        transport = TcpTransport("127.0.0.1", server.port)
        await transport.open(a_deadline())
        try:
            loop = asyncio.get_running_loop()

            async def reset(*args: object, **kwargs: object) -> int:
                raise ConnectionResetError(104, "Connection reset by peer")

            with (
                patch.object(loop, "sock_recv_into", reset),
                pytest.raises(SlmpConnectionEntryBusyError) as caught,
            ):
                await transport.exchange(REQUEST, FixedLength(20), a_deadline(), a_timing())
            assert "entry" in str(caught.value).lower()
            assert isinstance(caught.value.__cause__, ConnectionResetError)
        finally:
            await transport.close()


async def test_econnreset_after_a_partial_response_is_still_a_lost_connection() -> None:
    """The other half of the rule, which widening the classifier must not have eaten.

    Zero bytes read means the peer never accepted us: entry busy. Bytes read and then a
    reset means a connection that was working has gone and the response is incomplete, so
    it stays :class:`SlmpConnectionLostError` -- nothing here invents the rest of a
    half-arrived frame. Asserted rather than claimed in a docstring, because swallowing
    the narrow case is exactly how a widened classifier goes wrong.
    """
    async with FakeServer(max_connections=1, default=Reply(chunks=(b"x" * 20,))) as server:
        transport = TcpTransport("127.0.0.1", server.port)
        await transport.open(a_deadline())
        try:
            loop = asyncio.get_running_loop()
            calls = 0

            async def partial_then_reset(sock: object, buf: memoryview) -> int:
                nonlocal calls
                calls += 1
                if calls == 1:
                    buf[:4] = PARTIAL_PREFIX
                    return len(PARTIAL_PREFIX)
                raise ConnectionResetError(104, "Connection reset by peer")

            with (
                patch.object(loop, "sock_recv_into", partial_then_reset),
                pytest.raises(SlmpConnectionLostError) as caught,
            ):
                await transport.exchange(REQUEST, FixedLength(20), a_deadline(), a_timing())
            assert "entry" not in str(caught.value).lower()
            assert "4 byte(s)" in str(caught.value)
        finally:
            await transport.close()
