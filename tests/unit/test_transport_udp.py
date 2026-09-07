"""The UDP transport, against a real loopback responder we control. No PLC involved.

The measurements this file is written from, all **FX5U-32MT/DS fw 1.065**, UDP entry
No. 2 (PLC port 5001), 2026-09-06:

* two unread requests are both answered correctly -- datagrams are framed, so the TCP
  coalescing corruption does not exist here and pipelining is legitimate;
* bursts of 4E reads returned 8/8 and 32/32 and then **44/64**: twenty requests dropped
  with no end code, no ICMP and no error of any kind;
* 4E serials are echoed, so a response can be correlated -- and 3E has none, which is
  why 3E is refused any in-flight depth above 1;
* the entry is point-to-point, so a datagram from any other address is not ours;
* a 960-point read is 1935 bytes and is IP-fragmented, so a truncated datagram is a
  different message rather than a short read.

The transport never imports ``aslmp.wire``, so neither does this file: the correlation
is a two-byte tag the test invents, and if the transport can route by it then it really
is routing structurally rather than parsing a frame it was not supposed to know about.
"""

from __future__ import annotations

import asyncio
import socket  # noqa: TID251 - transport tests need a real socket; that is the point
import time
from collections.abc import Callable
from types import TracebackType
from typing import Self

import pytest

from aslmp.errors import (
    SlmpConcurrentTransactionError,
    SlmpConfigurationError,
    SlmpDatagramLostError,
    SlmpNotConnectedError,
    SlmpNotSentError,
    SlmpShortDatagramError,
    SlmpTimeoutError,
    SlmpTransportError,
    TimeoutCause,
)
from aslmp.timing import TimingBuilder
from aslmp.transport.base import Correlation, Deadline
from aslmp.transport.udp import FOREIGN_SOURCE, STALE_EPOCH, UNMATCHED, UdpTransport

PAYLOAD = b"\xd0\x00\x00\xff\xff\x03\x00\x04\x00\x00\x00"


def request_for(serial: int) -> bytes:
    return serial.to_bytes(2, "little") + b"REQ" + PAYLOAD


def response_for(serial: int, *, size: int = 24) -> bytes:
    body = serial.to_bytes(2, "little") + b"RSP"
    return body + bytes(size - len(body))


def correlation_for(serial: int) -> Correlation:
    tag = serial.to_bytes(2, "little")
    return Correlation(matches=lambda datagram: datagram[:2] == tag, label=serial)


class Recorder:
    """A :class:`~aslmp.transport.base.TransportObserver` that just writes it down."""

    def __init__(self) -> None:
        self.drops: list[tuple[str, int, tuple[str, int] | None]] = []
        self.rebinds: list[tuple[tuple[str, int], tuple[str, int], str]] = []

    def datagram_dropped(
        self, *, reason: str, nbytes: int, source: tuple[str, int] | None
    ) -> None:
        self.drops.append((reason, nbytes, source))

    def socket_rebound(
        self, *, previous_local: tuple[str, int], local: tuple[str, int], reason: str
    ) -> None:
        self.rebinds.append((previous_local, local, reason))

    def reasons(self) -> list[str]:
        return [reason for reason, _, _ in self.drops]


class FakeUdpPeer:
    """A loopback UDP responder driven by a callable, plus an unsolicited send.

    ``behaviour`` maps one received datagram to the datagrams to send back -- an empty
    list is the measured silent drop, and several is the out-of-order pipelined case.
    """

    def __init__(self, behaviour: Callable[[bytes], list[bytes]]) -> None:
        self.behaviour = behaviour
        self.received: list[bytes] = []
        self.port = 0
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> Self:
        self._sock.bind(("127.0.0.1", 0))
        self._sock.setblocking(False)
        self.port = int(self._sock.getsockname()[1])
        self._task = asyncio.create_task(self._serve())
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        task = self._task
        if task is not None:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        self._sock.close()

    async def _serve(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            data, source = await loop.sock_recvfrom(self._sock, 4096)
            self.received.append(data)
            for reply in self.behaviour(data):
                self._sock.sendto(reply, source)

    def send_unsolicited(self, data: bytes, to: tuple[str, int]) -> None:
        self._sock.sendto(data, to)


def echo(size: int = 24) -> Callable[[bytes], list[bytes]]:
    def behaviour(request: bytes) -> list[bytes]:
        serial = int.from_bytes(request[:2], "little")
        return [response_for(serial, size=size)]

    return behaviour


def silence(request: bytes) -> list[bytes]:
    return []


class FixedLength:
    """A ``Reassembler`` that wants exactly ``total`` bytes and knows nothing else."""

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


async def connected(
    peer: FakeUdpPeer,
    *,
    depth: int = 1,
    carries_serial: bool = True,
    observer: Recorder | None = None,
) -> UdpTransport:
    transport = UdpTransport(
        "127.0.0.1",
        peer.port,
        carries_serial=carries_serial,
        pipeline_depth=depth,
        source_host="127.0.0.1",
        observer=observer if observer is not None else Recorder(),
    )
    await transport.open(a_deadline())
    return transport


# --------------------------------------------------------------------------------------
# Construction: what is refused before a socket exists
# --------------------------------------------------------------------------------------


def test_pipelining_is_refused_on_a_frame_with_no_serial() -> None:
    """3E has no correlation field; positional matching plus real loss is corruption."""
    with pytest.raises(SlmpConfigurationError) as caught:
        UdpTransport("127.0.0.1", 5001, carries_serial=False, pipeline_depth=4)
    message = str(caught.value)
    assert "serial No" in message
    assert "arrival order" in message
    assert "44/64" in message


def test_pipelining_above_the_measured_ceiling_is_refused() -> None:
    with pytest.raises(SlmpConfigurationError) as caught:
        UdpTransport("127.0.0.1", 5001, carries_serial=True, pipeline_depth=64)
    assert "32" in str(caught.value)
    assert "invisible" in str(caught.value)


def test_a_depth_of_zero_is_refused() -> None:
    with pytest.raises(SlmpConfigurationError):
        UdpTransport("127.0.0.1", 5001, carries_serial=True, pipeline_depth=0)


def test_the_deepest_measured_lossless_depth_is_allowed() -> None:
    transport = UdpTransport("127.0.0.1", 5001, carries_serial=True, pipeline_depth=32)
    assert transport.max_in_flight == 32


# --------------------------------------------------------------------------------------
# One request, one datagram
# --------------------------------------------------------------------------------------


async def test_one_datagram_in_one_datagram_out() -> None:
    async with FakeUdpPeer(echo()) as peer:
        transport = await connected(peer)
        try:
            reassembler = FixedLength(24)
            timing = a_timing()
            result = await transport.exchange(
                request_for(0x1234),
                reassembler,
                a_deadline(),
                timing,
                correlation=correlation_for(0x1234),
            )
        finally:
            await transport.close()
    assert result.sent and result.responded
    assert result.bytes_received == 24
    assert bytes(reassembler.data) == response_for(0x1234)
    assert len(timing.chunks) == 1
    assert transport.transactions_completed == 1


async def test_a_truncated_datagram_is_a_different_message_not_a_short_read() -> None:
    """A 960-point read is 1935 bytes and is fragmented; a missing fragment loses it all."""
    async with FakeUdpPeer(echo(size=12)) as peer:
        transport = await connected(peer)
        try:
            with pytest.raises(SlmpShortDatagramError) as caught:
                await transport.exchange(
                    request_for(1),
                    FixedLength(24),
                    a_deadline(),
                    a_timing(),
                    correlation=correlation_for(1),
                )
        finally:
            await transport.close()
    assert "12 byte(s)" in str(caught.value)
    assert "1935" in str(caught.value)


async def test_an_empty_request_is_refused_before_the_socket_sees_it() -> None:
    async with FakeUdpPeer(echo()) as peer:
        transport = await connected(peer)
        try:
            with pytest.raises(SlmpNotSentError):
                await transport.exchange(b"", FixedLength(24), a_deadline(), a_timing())
        finally:
            await transport.close()


async def test_exchange_on_a_closed_transport_says_nothing_was_sent() -> None:
    transport = UdpTransport("127.0.0.1", 5001, carries_serial=False)
    with pytest.raises(SlmpNotConnectedError) as caught:
        await transport.exchange(b"x", FixedLength(4), a_deadline(), a_timing())
    assert caught.value.reason == "closed"


async def test_a_request_that_expects_no_response_sends_and_returns() -> None:
    async with FakeUdpPeer(silence) as peer:
        transport = await connected(peer)
        try:
            result = await transport.exchange(
                request_for(1),
                FixedLength(24),
                a_deadline(),
                a_timing(),
                expect_response=False,
            )
        finally:
            await transport.close()
    assert result.sent and not result.responded


# --------------------------------------------------------------------------------------
# Silence: the two different diagnoses
# --------------------------------------------------------------------------------------


async def test_silence_on_the_first_transaction_is_a_timeout_that_blames_the_coding() -> None:
    """Nothing has been proven yet, so coding / frame / protocol / port are candidates."""
    observer = Recorder()
    async with FakeUdpPeer(silence) as peer:
        transport = await connected(peer, observer=observer)
        try:
            with pytest.raises(SlmpTimeoutError) as caught:
                await transport.exchange(
                    request_for(1),
                    FixedLength(24),
                    a_deadline(0.2),
                    a_timing(),
                    correlation=correlation_for(1),
                )
        finally:
            await transport.close()
    assert caught.value.likely_causes[0] is TimeoutCause.CODING_MISMATCH
    assert observer.rebinds, "a timeout must rebind the source port"


async def test_a_vanished_request_on_a_working_socket_is_its_own_named_error() -> None:
    """No end code, no ICMP, nothing. Reporting it as a timeout misdirects the reader."""
    replies = {"answer": True}

    def sometimes(request: bytes) -> list[bytes]:
        if replies["answer"]:
            replies["answer"] = False
            return [response_for(int.from_bytes(request[:2], "little"))]
        return []

    async with FakeUdpPeer(sometimes) as peer:
        transport = await connected(peer)
        try:
            await transport.exchange(
                request_for(1),
                FixedLength(24),
                a_deadline(),
                a_timing(),
                correlation=correlation_for(1),
            )
            with pytest.raises(SlmpDatagramLostError) as caught:
                await transport.exchange(
                    request_for(2),
                    FixedLength(24),
                    a_deadline(0.2),
                    a_timing(),
                    correlation=correlation_for(2),
                )
        finally:
            await transport.close()
    error = caught.value
    assert error.serial == 2
    assert error.in_flight == 1
    assert error.deadline_s == pytest.approx(0.2)
    assert "44/64" not in error.headline()
    assert "20 of 64 dropped" in str(error)


async def test_a_timeout_rebinds_to_a_fresh_source_port() -> None:
    """The vanished request's answer then arrives at a port nobody is listening on."""
    observer = Recorder()
    async with FakeUdpPeer(silence) as peer:
        transport = await connected(peer, observer=observer)
        before = transport.binding
        assert before is not None
        try:
            with pytest.raises(SlmpTimeoutError):
                await transport.exchange(
                    request_for(1),
                    FixedLength(24),
                    a_deadline(0.15),
                    a_timing(),
                    correlation=correlation_for(1),
                )
            after = transport.binding
            assert after is not None
            assert after.local[1] != before.local[1]
            assert observer.rebinds[0][0] == before.local
            assert observer.rebinds[0][1] == after.local
        finally:
            await transport.close()


# --------------------------------------------------------------------------------------
# The epoch and the source check
# --------------------------------------------------------------------------------------


async def test_a_datagram_from_a_foreign_address_is_dropped_and_counted() -> None:
    """The entry is point-to-point; a datagram from anywhere else is not an answer."""
    observer = Recorder()
    async with FakeUdpPeer(echo()) as peer, FakeUdpPeer(echo()) as stranger:
        transport = await connected(peer, observer=observer)
        binding = transport.binding
        assert binding is not None
        try:
            stranger.send_unsolicited(response_for(0x1234), binding.local)
            await asyncio.sleep(0.02)
            reassembler = FixedLength(24)
            await transport.exchange(
                request_for(0x1234),
                reassembler,
                a_deadline(),
                a_timing(),
                correlation=correlation_for(0x1234),
            )
        finally:
            await transport.close()
    assert bytes(reassembler.data) == response_for(0x1234)
    assert STALE_EPOCH in observer.reasons()


async def test_a_foreign_datagram_arriving_mid_wait_never_becomes_the_answer() -> None:
    observer = Recorder()
    held: list[tuple[bytes, tuple[str, int]]] = []

    def deferred(request: bytes) -> list[bytes]:
        return []

    async with FakeUdpPeer(deferred) as peer, FakeUdpPeer(deferred) as stranger:
        transport = await connected(peer, observer=observer)
        binding = transport.binding
        assert binding is not None
        local = binding.local
        try:

            async def interfere() -> None:
                await asyncio.sleep(0.03)
                stranger.send_unsolicited(b"\x01\x00" + bytes(22), local)
                await asyncio.sleep(0.03)
                peer.send_unsolicited(response_for(1), local)

            noise = asyncio.create_task(interfere())
            reassembler = FixedLength(24)
            await transport.exchange(
                request_for(1),
                reassembler,
                a_deadline(1.0),
                a_timing(),
                correlation=correlation_for(1),
            )
            await noise
        finally:
            await transport.close()
    assert bytes(reassembler.data) == response_for(1)
    assert FOREIGN_SOURCE in observer.reasons()
    assert held == []


async def test_a_late_answer_is_drained_before_the_next_request_goes_out() -> None:
    """The per-transaction epoch: a dead transaction's reply is never a live one's."""
    observer = Recorder()
    async with FakeUdpPeer(echo()) as peer:
        transport = await connected(peer, observer=observer)
        binding = transport.binding
        assert binding is not None
        try:
            peer.send_unsolicited(response_for(0x9999), binding.local)
            await asyncio.sleep(0.03)
            reassembler = FixedLength(24)
            await transport.exchange(
                request_for(7),
                reassembler,
                a_deadline(),
                a_timing(),
                correlation=correlation_for(7),
            )
        finally:
            await transport.close()
    assert bytes(reassembler.data) == response_for(7)
    assert observer.reasons().count(STALE_EPOCH) == 1


async def test_a_datagram_nobody_claims_is_dropped_rather_than_guessed_at() -> None:
    observer = Recorder()

    def wrong_serial(request: bytes) -> list[bytes]:
        return [response_for(0xDEAD), response_for(int.from_bytes(request[:2], "little"))]

    async with FakeUdpPeer(wrong_serial) as peer:
        transport = await connected(peer, observer=observer)
        try:
            reassembler = FixedLength(24)
            await transport.exchange(
                request_for(3),
                reassembler,
                a_deadline(),
                a_timing(),
                correlation=correlation_for(3),
            )
        finally:
            await transport.close()
    assert bytes(reassembler.data) == response_for(3)
    assert UNMATCHED in observer.reasons()


# --------------------------------------------------------------------------------------
# Pipelining
# --------------------------------------------------------------------------------------


async def test_depth_one_refuses_a_second_request_and_says_why() -> None:
    async with FakeUdpPeer(echo()) as peer:
        transport = await connected(peer, depth=1)
        try:
            blocked = asyncio.create_task(
                transport.exchange(
                    request_for(1),
                    FixedLength(24),
                    a_deadline(0.3),
                    a_timing(),
                    correlation=correlation_for(1),
                )
            )
            await asyncio.sleep(0)
            with pytest.raises(SlmpConcurrentTransactionError) as caught:
                await transport.exchange(
                    request_for(2),
                    FixedLength(24),
                    a_deadline(0.3),
                    a_timing(),
                    correlation=correlation_for(2),
                )
            await blocked
        finally:
            await transport.close()
    assert "4E" in str(caught.value)


async def test_a_pipelined_burst_is_correlated_not_positional() -> None:
    """Responses are returned reversed; every caller still gets its own answer."""
    pending: list[bytes] = []

    def hoard(request: bytes) -> list[bytes]:
        pending.append(request)
        if len(pending) < 4:
            return []
        replies = [
            response_for(int.from_bytes(held[:2], "little")) for held in reversed(pending)
        ]
        pending.clear()
        return replies

    async with FakeUdpPeer(hoard) as peer:
        transport = await connected(peer, depth=8)
        try:
            reassemblers = {serial: FixedLength(24) for serial in (11, 22, 33, 44)}
            results = await asyncio.gather(
                *(
                    transport.exchange(
                        request_for(serial),
                        reassemblers[serial],
                        a_deadline(2.0),
                        a_timing(),
                        correlation=correlation_for(serial),
                    )
                    for serial in (11, 22, 33, 44)
                )
            )
        finally:
            await transport.close()
    assert all(result.responded for result in results)
    for serial, reassembler in reassemblers.items():
        assert bytes(reassembler.data) == response_for(serial)


async def test_one_lost_request_in_a_burst_names_its_serial_and_the_depth() -> None:
    """At depth 32 this does not happen; at depth 64 it happened to a third of the burst."""
    pending: list[bytes] = []

    def drop_one(request: bytes) -> list[bytes]:
        pending.append(request)
        if len(pending) < 3:
            return []
        replies = [
            response_for(int.from_bytes(held[:2], "little"))
            for held in pending
            if int.from_bytes(held[:2], "little") != 22
        ]
        pending.clear()
        return replies

    async with FakeUdpPeer(drop_one) as peer:
        transport = await connected(peer, depth=8)
        try:
            outcomes = await asyncio.gather(
                *(
                    transport.exchange(
                        request_for(serial),
                        FixedLength(24),
                        a_deadline(0.4),
                        a_timing(),
                        correlation=correlation_for(serial),
                    )
                    for serial in (11, 22, 33)
                ),
                return_exceptions=True,
            )
        finally:
            await transport.close()
    lost = outcomes[1]
    assert isinstance(lost, SlmpDatagramLostError)
    assert lost.serial == 22
    assert lost.in_flight >= 1
    assert not isinstance(outcomes[0], BaseException)
    assert not isinstance(outcomes[2], BaseException)


async def test_the_pipeline_cap_is_a_ceiling() -> None:
    async with FakeUdpPeer(silence) as peer:
        transport = await connected(peer, depth=2)
        try:
            running = [
                asyncio.create_task(
                    transport.exchange(
                        request_for(serial),
                        FixedLength(24),
                        a_deadline(0.3),
                        a_timing(),
                        correlation=correlation_for(serial),
                    )
                )
                for serial in (1, 2)
            ]
            await asyncio.sleep(0.02)
            assert transport.in_flight == 2
            with pytest.raises(SlmpConcurrentTransactionError) as caught:
                await transport.exchange(
                    request_for(3),
                    FixedLength(24),
                    a_deadline(0.3),
                    a_timing(),
                    correlation=correlation_for(3),
                )
            outcomes = await asyncio.gather(*running, return_exceptions=True)
        finally:
            await transport.close()
    # Both in-flight requests fail, and each gets the diagnosis its own history earns:
    # the first was alone on a socket that had never worked, which is honestly the
    # graft-G3 timeout, and the second was sent at depth 2, which is the measured
    # silent drop. Neither is a retry and neither is a bare TimeoutError.
    assert all(isinstance(outcome, SlmpTransportError) for outcome in outcomes)
    assert any(isinstance(outcome, SlmpDatagramLostError) for outcome in outcomes)
    assert "pipeline_depth" in str(caught.value)


def test_repr_says_where_it_points_and_how_deep_it_pipelines() -> None:
    transport = UdpTransport("192.168.10.250", 5001, carries_serial=True, pipeline_depth=8)
    assert "192.168.10.250" in repr(transport)
    assert "pipeline_depth=8" in repr(transport)
    assert "closed" in repr(transport)
