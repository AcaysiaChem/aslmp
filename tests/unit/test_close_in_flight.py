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
from typing import Any, Final

import pytest

from aslmp import ConnectionState, Plc
from aslmp.client import Handshake
from aslmp.errors import (
    SlmpConnectionClosedError,
    SlmpConnectionLostError,
    SlmpOutcomeUnknownError,
)
from aslmp.health import HealthMonitor, ProbeOutcome
from aslmp.transport.base import TransportKind

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
        assert (plc.counters.timeouts, plc.counters.connection_lost) == (0, 0)


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
