"""A connection closed under a transaction in flight: at once, by name, and not a failure.

The situation is ordinary. A control loop is waiting on a read and the process shuts
down, or a health probe is out when the client is closed. Until this was fixed the
caller was told something false, and on Linux it was also kept waiting. Measured
2026-09-26 against a peer that never answers, with a 5 s client timeout:

=============  ====================================================================
before         the in-flight read on Linux stayed parked for 4.8 s, then raised
               ``SlmpTimeoutError`` -- blaming a PLC that had not failed to answer
               anything -- and counted a timeout. On Windows it ended in ~2 ms, but
               as a socket failure, and counted a lost connection. TCP and UDP alike.
after          under 3 ms on both, as :class:`SlmpConnectionClosedError`, nothing
               counted, the connection ``CLOSED`` and ``Disconnected`` the only event.
=============  ====================================================================

The Linux half was the bug and the Windows half was the lie. On a selector event loop,
closing a socket does not wake a coroutine parked in ``sock_recv_into``; the transports
now cancel the parked operation first and close the socket after, which is also the only
order in which the reader is unregistered before its descriptor can be reused.

Every test here runs on both transports, and each would fail against the old code: on
Linux by hitting ``_PROMPT`` while the read waits out ``_CLIENT_TIMEOUT``, on Windows by
raising the wrong exception.
"""

from __future__ import annotations

import asyncio
import contextlib

# A UDP peer that never answers is a bound socket and nothing more.
import socket  # noqa: TID251
from collections.abc import AsyncIterator, Coroutine
from typing import Any, Final, cast
from unittest.mock import patch

import pytest

from aslmp import ConnectionState, FrameType, Plc
from aslmp.client import Handshake
from aslmp.errors import (
    SlmpConnectionClosedError,
    SlmpConnectionLostError,
    SlmpOutcomeUnknownError,
)
from aslmp.health import HealthMonitor, ProbeOutcome
from aslmp.transport.base import TransportKind
from aslmp.transport.udp import UdpTransport

_CLIENT_TIMEOUT: Final = 30.0
"""Long on purpose: a transaction still parked when a test gives up is then unmistakably
the old behaviour of waiting out its own deadline, not a slow runner."""

_PROMPT: Final = 3.0
"""What "at once" is allowed to mean: a tenth of the client's timeout, and a thousand
times what it measured, so a loaded runner never passes for a hang."""

KINDS = pytest.mark.parametrize(
    "kind", [TransportKind.TCP, TransportKind.UDP], ids=["tcp", "udp"]
)


@contextlib.asynccontextmanager
async def silent_peer(kind: TransportKind) -> AsyncIterator[int]:
    """A peer that takes every request and never answers. Yields its port."""
    if kind is TransportKind.TCP:

        async def swallow(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                while await reader.read(65536):
                    pass
            finally:
                writer.close()  # or wait_closed() waits on this connection forever

        server = await asyncio.start_server(swallow, "127.0.0.1", 0)
        try:
            yield int(server.sockets[0].getsockname()[1])
        finally:
            server.close()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(server.wait_closed(), 5.0)
    else:
        sink = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sink.bind(("127.0.0.1", 0))
        try:
            yield int(sink.getsockname()[1])
        finally:
            sink.close()


def a_client(kind: TransportKind, port: int) -> Plc:
    # No handshake: the peer never answers, and connect() would wait for one.
    return Plc(
        "127.0.0.1",
        port,
        profile="melsec:iq-f/fx5u",
        timeout=_CLIENT_TIMEOUT,
        handshake=Handshake.NONE,
        transport=kind,
    )


async def parked(transaction: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
    """Start a transaction and let it reach the socket and wait there."""
    task: asyncio.Task[Any] = asyncio.ensure_future(transaction)
    await asyncio.sleep(0.1)
    assert not task.done(), "the peer answered, and it is meant to be silent"
    return task


@KINDS
async def test_a_read_in_flight_ends_at_once_with_the_closed_error(kind: TransportKind) -> None:
    async with silent_peer(kind) as port:
        plc = a_client(kind, port)
        await plc.connect()
        read = await parked(plc.read_u16("D0"))
        await plc.aclose()
        with pytest.raises(SlmpConnectionClosedError, match="closed by this process"):
            await asyncio.wait_for(read, _PROMPT)


@KINDS
async def test_a_write_in_flight_is_outcome_unknown_never_unsent(kind: TransportKind) -> None:
    """The request went out. "It did not happen" is the one answer that must not come back.

    ``SlmpNotConnectedError`` -- the obvious type for "you closed it" -- is classified as
    not-sent by the transaction layer, because it is normally raised before a request is
    written. Reporting a close that way would tell the caller a write that may have
    reached the PLC provably did not, which is exactly the retry-it-blindly mistake
    :class:`SlmpOutcomeUnknownError` exists to prevent.
    """
    async with silent_peer(kind) as port:
        plc = a_client(kind, port)
        await plc.connect()
        write = await parked(plc.write_u16("D0", 1))
        await plc.aclose()
        with pytest.raises(SlmpOutcomeUnknownError) as caught:
            await asyncio.wait_for(write, _PROMPT)
        assert caught.value.sent is True
        assert isinstance(caught.value.__cause__, SlmpConnectionClosedError)


@KINDS
async def test_a_close_you_made_is_not_a_failure_of_the_link(kind: TransportKind) -> None:
    """No count, no ``ConnectionFailed``, and ``CLOSED`` rather than ``FAILED``.

    The event matters most. A supervisor reconnects on ``ConnectionFailed``, so emitting it
    for a deliberate close would start reconnecting a client that was shut down on
    purpose. It is only avoided because ``aclose()`` marks the connection ``CLOSED``
    before closing the transport: the woken transaction's failure path runs inside that
    close, and must already find it closed.
    """
    async with silent_peer(kind) as port:
        plc = a_client(kind, port)
        events: list[str] = []
        plc.add_event_listener(lambda event: events.append(type(event).__name__))
        await plc.connect()
        read = await parked(plc.read_u16("D0"))
        await plc.aclose()
        with pytest.raises(SlmpConnectionClosedError):
            await asyncio.wait_for(read, _PROMPT)
        assert plc.state is ConnectionState.CLOSED
        assert "ConnectionFailed" not in events
        counters = plc.counters
        assert (counters.timeouts, counters.connection_lost) == (0, 0)
        assert (counters.transactions_failed, counters.transactions_abandoned) == (0, 1)


@KINDS
async def test_cancelling_the_caller_is_still_a_cancellation(kind: TransportKind) -> None:
    async with silent_peer(kind) as port:
        plc = a_client(kind, port)
        await plc.connect()
        read = await parked(plc.read_u16("D0"))
        read.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(read, _PROMPT)
        await plc.aclose()


@KINDS
async def test_cancellation_wins_when_it_arrives_with_a_close(kind: TransportKind) -> None:
    """Both at once is a cancellation. Turning one into an ordinary exception is how a
    shutdown stops shutting down, so the close must never be allowed to explain it away.
    """
    async with silent_peer(kind) as port:
        plc = a_client(kind, port)
        await plc.connect()
        read = await parked(plc.read_u16("D0"))
        read.cancel()
        await plc.aclose()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(read, _PROMPT)


async def test_a_health_probe_closed_under_is_a_stand_down_not_a_failure() -> None:
    """A monitor that is stopped by closing its client must not end on a recorded failure."""
    async with silent_peer(TransportKind.TCP) as port:
        plc = a_client(TransportKind.TCP, port)
        await plc.connect()
        monitor = HealthMonitor(plc, idle_probe_after=60.0, probe_interval=60.0)
        probe = await parked(monitor.probe_once())
        await plc.aclose()
        assert await asyncio.wait_for(probe, _PROMPT) is ProbeOutcome.SKIPPED_UNUSABLE
        snapshot = monitor.snapshot()
        assert snapshot.failures == 0
        assert snapshot.last_failure is None


def test_the_closed_error_is_a_lost_connection_to_every_existing_handler() -> None:
    assert issubclass(SlmpConnectionClosedError, SlmpConnectionLostError)


# ======================================================================================
# The pre-release review of 2026-09-27. Each of these reproduced a defect before its fix;
# the first three were regressions in this very change, the last two predated it.
# ======================================================================================


@KINDS
async def test_a_monitor_fed_from_on_transaction_ignores_what_a_close_cut_off(
    kind: TransportKind,
) -> None:
    """Wired the way ``HealthMonitor``'s own docstring shows, a clean stop records nothing."""
    async with silent_peer(kind) as port:
        watched: list[HealthMonitor] = []
        plc = Plc(
            "127.0.0.1",
            port,
            profile="melsec:iq-f/fx5u",
            timeout=_CLIENT_TIMEOUT,
            handshake=Handshake.NONE,
            transport=kind,
            on_transaction=lambda tx: watched[0].observe(tx),
        )
        watched.append(HealthMonitor(plc, idle_probe_after=60.0, probe_interval=60.0))
        await plc.connect()
        read = await parked(plc.read_u16("D0"))
        await plc.aclose()
        with pytest.raises(SlmpConnectionClosedError):
            await asyncio.wait_for(read, _PROMPT)
        snapshot = watched[0].snapshot()
        assert (snapshot.failures, snapshot.last_failure) == (0, None)


@KINDS
async def test_a_cancelled_aclose_still_announces_the_close(kind: TransportKind) -> None:
    """``close()`` must not suspend: a caller cancelled inside it once skipped the rest.

    ``Disconnected`` was then never emitted -- not by that ``aclose()`` and not by any later
    one, because the state was already ``CLOSED`` -- and a supervisor, which stands down
    only on ``Disconnected``, went on reporting ready on a closed client.
    """
    async with silent_peer(kind) as port:
        plc = a_client(kind, port)
        events: list[str] = []
        plc.add_event_listener(lambda event: events.append(type(event).__name__))
        await plc.connect()
        read = await parked(plc.read_u16("D0"))
        closer = asyncio.ensure_future(plc.aclose())
        await asyncio.sleep(0)
        closer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(closer, _PROMPT)
        with contextlib.suppress(SlmpConnectionClosedError):
            await asyncio.wait_for(read, _PROMPT)
        assert events.count("Disconnected") == 1
        assert plc.counters.disconnects == 1
        assert plc.info is None


@KINDS
async def test_a_taskgroup_that_closes_under_its_own_reader_announces_the_close(
    kind: TransportKind,
) -> None:
    """The natural form of the case above: nobody calls ``cancel()`` by hand.

    The reader fails with the closed error, the ``TaskGroup`` cancels its siblings -- the
    closer among them -- and before the fix that cancellation landed inside ``close()``.
    """
    async with silent_peer(kind) as port:
        plc = a_client(kind, port)
        events: list[str] = []
        plc.add_event_listener(lambda event: events.append(type(event).__name__))
        await plc.connect()

        async def close_soon() -> None:
            await asyncio.sleep(0.1)
            await plc.aclose()

        with pytest.raises(ExceptionGroup) as caught:
            async with asyncio.TaskGroup() as group:
                group.create_task(plc.read_u16("D0"))
                group.create_task(close_soon())
        assert caught.group_contains(SlmpConnectionClosedError)
        assert events.count("Disconnected") == 1
        assert plc.counters.disconnects == 1


async def test_connectionfailed_names_the_real_failure_when_a_sibling_was_parked() -> None:
    """Pipelined UDP: B fails while A holds the read baton. The event must name B's failure.

    Before the fix B's close suspended, A -- woken by it -- reached the failure path first,
    and ``ConnectionFailed`` said ``SlmpConnectionClosedError``, "nothing failed on the
    link", while the real cause went unreported.
    """
    async with silent_peer(TransportKind.UDP) as port:
        seen: list[tuple[str, str | None]] = []
        plc = Plc(
            "127.0.0.1",
            port,
            profile="melsec:iq-f/fx5u",
            timeout=_CLIENT_TIMEOUT,
            handshake=Handshake.NONE,
            transport=TransportKind.UDP,
            frame=FrameType.FOUR_E,
            udp_pipeline_depth=4,
        )
        plc.add_event_listener(
            lambda event: seen.append(
                (type(event).__name__, getattr(event, "error_type", None))
            )
        )
        await plc.connect()
        a = await parked(plc.read_u16("D0"))  # holds the baton, parked on the socket
        b = asyncio.ensure_future(asyncio.wait_for(plc.read_u16("D1"), 0.2))  # at the baton
        outcomes = await asyncio.wait_for(asyncio.gather(a, b, return_exceptions=True), 10.0)
        assert isinstance(outcomes[0], SlmpConnectionClosedError)
        failed = [error for name, error in seen if name == "ConnectionFailed"]
        assert len(failed) == 1
        assert failed[0] != "SlmpConnectionClosedError", failed
        assert plc.counters.transactions_abandoned == 1
        await plc.aclose()


@KINDS
async def test_aclose_while_connecting_stays_closed(kind: TransportKind) -> None:
    """It used to be undone: the socket finished opening and the client reached READY.

    On TCP that socket is the PLC's single slot on the entry, held by a client whose owner
    had closed it -- after a supervisor had already stood down on the ``Disconnected``.
    """
    async with silent_peer(kind) as port:
        plc = a_client(kind, port)
        transport = plc._conn.transport
        original = type(transport).open
        entered, release = asyncio.Event(), asyncio.Event()

        async def held_open(self: Any, deadline: Any) -> Any:
            # The window, made deterministic. Stepping the loop a fixed number of times
            # found it on Windows and missed it on Linux, where a numeric address
            # resolves without a thread hop and the whole open finishes in one step.
            entered.set()
            await asyncio.wait_for(release.wait(), _PROMPT)
            return await original(self, deadline)

        with patch.object(type(transport), "open", held_open):
            connecting = asyncio.ensure_future(plc.connect())
            await asyncio.wait_for(entered.wait(), _PROMPT)
            assert plc.state is ConnectionState.CONNECTING
            await plc.aclose()
            release.set()
            with pytest.raises(SlmpConnectionClosedError, match="while it was being opened"):
                await asyncio.wait_for(connecting, _PROMPT)
        assert _state(plc) is ConnectionState.CLOSED  # read again: it changed under the await
        assert not transport.is_open


def _state(plc: Plc) -> ConnectionState:
    """The state, re-read. A property compared twice across an await is not narrowed."""
    return plc.state


def _four_e_answer(request: bytes) -> bytes:
    """A 4E response of end code 0 and one word, 42, echoing the request's serial."""
    body = b"\x00\x00\x2a\x00"
    return (
        b"\xd4\x00" + request[2:4] + b"\x00\x00\x00\xff\xff\x03\x00"
        + len(body).to_bytes(2, "little") + body
    )


class _HeldPeer(asyncio.DatagramProtocol):
    """A UDP peer that holds requests until told to answer them, or answers everything."""

    def __init__(self) -> None:
        self.held: list[tuple[bytes, tuple[str, int]]] = []
        self.answer_everything = False
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        # A cast, not an isinstance: CPython 3.11's selector datagram transport on Linux
        # is not a subclass of asyncio.DatagramTransport, and asserting it failed there.
        self.transport = cast(asyncio.DatagramTransport, transport)

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        assert self.transport is not None
        if self.answer_everything:
            self.transport.sendto(_four_e_answer(data), addr)
        else:
            self.held.append((data, addr))


async def test_a_cancel_just_after_the_baton_was_granted_does_not_leak_it() -> None:
    """Pipelined UDP. B's acquire succeeds, then B is cancelled before it resumes.

    Cancelling an acquire that has already succeeded is a no-op, so the lock was held by
    nobody, forever -- and since a reconnect did not replace it, every pipelined exchange
    after it, the reconnect's own handshake included, waited out its deadline against a
    peer answering at once.
    """
    loop = asyncio.get_running_loop()
    endpoint, peer = await loop.create_datagram_endpoint(_HeldPeer, local_addr=("127.0.0.1", 0))
    try:
        port = int(endpoint.get_extra_info("sockname")[1])
        plc = Plc(
            "127.0.0.1",
            port,
            profile="melsec:iq-f/fx5u",
            timeout=5.0,
            handshake=Handshake.NONE,
            transport=TransportKind.UDP,
            frame=FrameType.FOUR_E,
            udp_pipeline_depth=4,
        )
        await plc.connect()
        a = await parked(plc.read_u16("D0"))  # holds the baton
        b = asyncio.ensure_future(plc.read_u16("D1"))
        await asyncio.sleep(0.1)  # B is waiting for the baton
        acquiring = [
            task
            for task in asyncio.all_tasks()
            if getattr(task.get_coro(), "__qualname__", "") == "Lock.acquire"
        ]
        assert len(acquiring) == 1, acquiring
        data, addr = peer.held[0]
        assert peer.transport is not None
        peer.transport.sendto(_four_e_answer(data), addr)  # answer A only
        cancelled_in_the_window = False
        for _ in range(10_000):
            if acquiring[0].done() and not b.done():
                b.cancel()
                cancelled_in_the_window = True
                break
            if b.done():
                break
            await asyncio.sleep(0)
        assert cancelled_in_the_window, "B resumed before the window could be reached"
        assert await asyncio.wait_for(a, _PROMPT) == 42
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(b, _PROMPT)
        transport = plc._conn.transport
        assert isinstance(transport, UdpTransport)
        assert not transport._read_baton.locked(), "the baton is held with no reader"

        peer.answer_everything = True
        await plc.reconnect(reason="after a cancelled pipelined read")
        assert await asyncio.wait_for(plc.read_u16("D0"), _PROMPT) == 42
        await plc.aclose()
    finally:
        endpoint.close()

