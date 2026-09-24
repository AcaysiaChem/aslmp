"""The pathology board, one switch at a time, over real loopback sockets.

Each test turns on exactly one switch and asserts the failure it reproduces. That is the
whole discipline: a simulator whose misbehaviours cannot be isolated is a simulator whose
test failures cannot be attributed.

The bytes go over ``127.0.0.1`` through asyncio streams. No ``aslmp.transport`` is
imported anywhere in this file or in the package under test -- which is exactly why a
transport bug would be visible here rather than cancelled out.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Sequence

import pytest

from aslmp.profile import Encoding
from aslmp.testing.pathology import HEALTHY, PATHOLOGY_SOURCES, Pathology
from aslmp.testing.server import BENCH_ENTRIES, Entry, PlcSimulator
from aslmp.testing.targets import FX5U_32MT_DS, PEDANTIC, SimulatorTarget
from aslmp.wire.codec import ASCII, BINARY, Codec
from aslmp.wire.frames import FOUR_E, THREE_E, FrameFormat, FrameType, request_body
from aslmp.wire.route import Route

TCP = "tcp"
UDP = "udp"
TCP_4E = "tcp-4e"


def read_words_frame(
    *,
    device: bytes = b"\x00\x00\x00\xa8",
    count: int = 2,
    frame: FrameFormat = THREE_E,
    codec: Codec = BINARY,
    serial: int | None = None,
) -> bytes:
    """A 0401 Device Read request, ready to put on a socket."""
    payload = device + codec.number(count, bits=16)
    body = request_body(
        codec, monitoring_timer=0, command=0x0401, subcommand=0x0000, payload=payload
    )
    return frame.build(route=Route.OWN_STATION, body=body, codec=codec, serial=serial)


def end_code_of(raw: bytes, frame: FrameFormat = THREE_E, codec: Codec = BINARY) -> int:
    return int(codec.read_number(raw, frame.prefix_units(codec), bits=16))


def payload_of(raw: bytes, frame: FrameFormat = THREE_E, codec: Codec = BINARY) -> bytes:
    return raw[frame.data_offset(codec) :]


def serial_of(raw: bytes, codec: Codec = BINARY) -> int:
    return int(codec.read_number(raw, codec.number_len(16), bits=16))


async def read_frame(
    reader: asyncio.StreamReader,
    *,
    frame: FrameFormat = THREE_E,
    codec: Codec = BINARY,
    timeout: float = 1.0,
) -> bytes | None:
    """One length-driven response, or ``None`` if the deadline passes."""
    try:
        prefix = await asyncio.wait_for(
            reader.readexactly(frame.prefix_units(codec)), timeout
        )
        declared = codec.read_number(
            prefix, frame.subheader_units(codec) + Route.wire_len(codec), bits=16
        )
        return prefix + await asyncio.wait_for(reader.readexactly(declared), timeout)
    except (TimeoutError, asyncio.IncompleteReadError):
        return None


@contextlib.asynccontextmanager
async def simulator(
    *,
    target: SimulatorTarget = FX5U_32MT_DS,
    pathology: Pathology | None = None,
    entries: Sequence[Entry] = BENCH_ENTRIES,
) -> AsyncIterator[PlcSimulator]:
    plc = PlcSimulator(target=target, pathology=pathology, entries=entries)
    await plc.start()
    try:
        yield plc
    finally:
        await plc.aclose()


# --------------------------------------------------------------------------------------
# The board as data
# --------------------------------------------------------------------------------------


def test_a_clean_board_has_nothing_on() -> None:
    assert HEALTHY.active == ()
    assert str(HEALTHY) == "Pathology(clean)"


def test_replace_flips_exactly_one_switch() -> None:
    board = HEALTHY.replace(coalesce_requests=True)
    assert board.active == ("coalesce_requests",)
    assert HEALTHY.active == (), "the original board must not be mutated"


def test_every_active_switch_carries_its_measurement() -> None:
    board = HEALTHY.replace(coalesce_requests=True, single_connection=True)
    assert set(board.cites()) == {
        PATHOLOGY_SOURCES["coalesce_requests"],
        PATHOLOGY_SOURCES["single_connection"],
    }


def test_impossible_switch_values_are_refused() -> None:
    with pytest.raises(ValueError, match="segment_at"):
        Pathology(segment_at=0)
    with pytest.raises(ValueError, match="in flight"):
        Pathology(udp_drop_above_depth=0)
    with pytest.raises(ValueError, match="must not be negative"):
        Pathology(late_reply_s=-1.0)


# --------------------------------------------------------------------------------------
# H1: TCP request coalescing -- the reason the architecture exists
# --------------------------------------------------------------------------------------


async def test_coalescing_answers_only_the_last_request_with_end_code_zero() -> None:
    """Two requests in one write produce ONE response, for the LAST, end code 0x0000.

    Measured on FX5U-32MT/DS fw 1.065 (2026-09-06). On a 3E frame there is no serial, so
    a client that pairs the answer with the first request reads the scan counter and
    calls it the setpoint. There is no error, no end code and no timeout -- just wrong
    data reported as success.
    """
    async with simulator() as plc:
        plc.memory.set_u16("D", 0, 0x1111)
        plc.memory.set_u16("D", 8, 0x8888)
        host, port = plc.address(TCP)
        reader, writer = await asyncio.open_connection(host, port)
        first = read_words_frame(device=b"\x00\x00\x00\xa8", count=1)
        second = read_words_frame(device=b"\x08\x00\x00\xa8", count=1)
        writer.write(first + second)
        await writer.drain()

        answer = await read_frame(reader)
        assert answer is not None
        assert end_code_of(answer) == 0x0000, "the corruption reports success"
        assert payload_of(answer) == BINARY.words((0x8888,)), (
            "the single response answers the SECOND request; a client that paired it "
            "with the first would return the wrong register with end code 0x0000"
        )
        assert await read_frame(reader, timeout=0.2) is None, "the first request is gone"

        writer.close()
        assert plc.events_of("coalesced"), "the discard must be recorded, not inferred"


async def test_the_same_two_requests_one_at_a_time_are_both_answered() -> None:
    """The gate's whole job: separate writes, separate responses, both correct."""
    async with simulator() as plc:
        plc.memory.set_u16("D", 0, 0x1111)
        plc.memory.set_u16("D", 8, 0x8888)
        host, port = plc.address(TCP)
        reader, writer = await asyncio.open_connection(host, port)
        for device, expected in (
            (b"\x00\x00\x00\xa8", 0x1111),
            (b"\x08\x00\x00\xa8", 0x8888),
        ):
            writer.write(read_words_frame(device=device, count=1))
            await writer.drain()
            answer = await read_frame(reader)
            assert answer is not None
            assert payload_of(answer) == BINARY.words((expected,))
        writer.close()
        assert not plc.events_of("coalesced")


async def test_four_e_serials_make_the_coalescing_visible() -> None:
    """Three coalesced 4E requests answered only serial 0xC102 -- the third."""
    async with simulator() as plc:
        host, port = plc.address(TCP_4E)
        reader, writer = await asyncio.open_connection(host, port)
        writer.write(
            b"".join(
                read_words_frame(frame=FOUR_E, count=1, serial=serial)
                for serial in (0xC100, 0xC101, 0xC102)
            )
        )
        await writer.drain()
        answer = await read_frame(reader, frame=FOUR_E)
        assert answer is not None
        assert serial_of(answer) == 0xC102
        assert await read_frame(reader, frame=FOUR_E, timeout=0.2) is None
        writer.close()


async def test_a_clean_target_answers_every_coalesced_request() -> None:
    """PEDANTIC does not have the defect, so the same test must NOT see it."""
    async with simulator(target=PEDANTIC) as plc:
        host, port = plc.address(TCP)
        reader, writer = await asyncio.open_connection(host, port)
        writer.write(read_words_frame(count=1) + read_words_frame(count=1))
        await writer.drain()
        assert await read_frame(reader) is not None
        assert await read_frame(reader) is not None
        writer.close()


# --------------------------------------------------------------------------------------
# H2: one connection per entry
# --------------------------------------------------------------------------------------


async def test_a_second_connection_is_accepted_and_then_closed() -> None:
    """connect() succeeds and the CPU immediately FINs; the incumbent is undisturbed."""
    async with simulator() as plc:
        host, port = plc.address(TCP)
        first_reader, first_writer = await asyncio.open_connection(host, port)

        second_reader, second_writer = await asyncio.open_connection(host, port)
        assert await asyncio.wait_for(second_reader.read(1), 1.0) == b"", (
            "a zero-byte read on a fresh connection is the refusal, and it arrives "
            "before anything was sent"
        )
        second_writer.close()

        first_writer.write(read_words_frame(count=1))
        await first_writer.drain()
        assert await read_frame(first_reader) is not None, "the incumbent kept working"
        first_writer.close()
        await asyncio.sleep(0.05)

        assert plc.events_of("connection_refused")


async def test_the_slot_frees_when_the_incumbent_closes() -> None:
    async with simulator() as plc:
        host, port = plc.address(TCP)
        _reader, writer = await asyncio.open_connection(host, port)
        writer.close()
        await asyncio.sleep(0.05)
        reader2, writer2 = await asyncio.open_connection(host, port)
        writer2.write(read_words_frame(count=1))
        await writer2.drain()
        assert await read_frame(reader2) is not None
        writer2.close()


async def test_without_the_switch_two_connections_are_served() -> None:
    async with simulator(pathology=HEALTHY) as plc:
        host, port = plc.address(TCP)
        readers = []
        writers = []
        for _ in range(2):
            reader, writer = await asyncio.open_connection(host, port)
            readers.append(reader)
            writers.append(writer)
        for reader, writer in zip(readers, writers, strict=True):
            writer.write(read_words_frame(count=1))
            await writer.drain()
            assert await read_frame(reader) is not None
            writer.close()
        assert not plc.events_of("connection_refused")


# --------------------------------------------------------------------------------------
# H12: segmentation
# --------------------------------------------------------------------------------------


async def test_a_long_response_is_split_at_the_measured_boundary() -> None:
    """1931 bytes arrived as 1460 + 471, 3.0 ms apart, on one of three identical reads."""
    board = HEALTHY.replace(segment_at=1460, segment_gap_s=0.003)
    async with simulator(pathology=board) as plc:
        host, port = plc.address(TCP)
        reader, writer = await asyncio.open_connection(host, port)
        writer.write(read_words_frame(count=960))
        await writer.drain()
        answer = await read_frame(reader, timeout=2.0)
        assert answer is not None
        assert len(answer) == 1931, "9 prefix + L, with L = 1922"
        assert end_code_of(answer) == 0x0000
        writer.close()
        chunks = [
            record for record in plc.transcript if record.direction == "tx"
        ]
        assert [len(chunk.data) for chunk in chunks] == [1460, 471]
        assert plc.events_of("segmented")


# --------------------------------------------------------------------------------------
# Silence: the coding mismatch and the overstated length
# --------------------------------------------------------------------------------------


async def test_an_ascii_request_on_a_binary_entry_gets_no_answer_at_all() -> None:
    """No end code, no reset, nothing. Silence is the diagnosis."""
    async with simulator() as plc:
        host, port = plc.address(TCP)
        reader, writer = await asyncio.open_connection(host, port)
        writer.write(read_words_frame(device=b"D*000000", count=1, codec=ASCII))
        await writer.drain()
        assert await read_frame(reader, timeout=0.3) is None
        writer.close()
        assert plc.events_of("coding_mismatch_silence")


async def test_with_the_switch_off_the_coding_mismatch_is_0xc06f() -> None:
    """The documented code. Our silicon does not send it; a target may."""
    async with simulator(target=PEDANTIC) as plc:
        host, port = plc.address(TCP)
        reader, writer = await asyncio.open_connection(host, port)
        writer.write(read_words_frame(device=b"D*000000", count=1, codec=ASCII))
        await writer.drain()
        answer = await read_frame(reader, timeout=1.0)
        assert answer is not None
        assert end_code_of(answer) == 0xC06F
        writer.close()


async def test_an_overstated_length_hangs_forever() -> None:
    """The CPU blocks waiting for bytes that never come. Indistinguishable from dead."""
    async with simulator() as plc:
        host, port = plc.address(TCP)
        reader, writer = await asyncio.open_connection(host, port)
        raw = bytearray(read_words_frame(count=1))
        at = THREE_E.subheader_units(BINARY) + Route.wire_len(BINARY)
        raw[at : at + 2] = BINARY.number(len(raw) - THREE_E.prefix_units(BINARY) + 2, bits=16)
        writer.write(bytes(raw))
        await writer.drain()
        assert await read_frame(reader, timeout=0.3) is None
        writer.close()


async def test_with_the_switch_off_an_overstated_length_is_answered() -> None:
    board = HEALTHY.replace(overstated_length_grace_s=0.05)
    async with simulator(target=PEDANTIC, pathology=board) as plc:
        host, port = plc.address(TCP)
        reader, writer = await asyncio.open_connection(host, port)
        raw = bytearray(read_words_frame(count=1))
        at = THREE_E.subheader_units(BINARY) + Route.wire_len(BINARY)
        raw[at : at + 2] = BINARY.number(len(raw) - THREE_E.prefix_units(BINARY) + 2, bits=16)
        writer.write(bytes(raw))
        await writer.drain()
        answer = await read_frame(reader, timeout=1.0)
        assert answer is not None
        assert end_code_of(answer) == PEDANTIC.end_codes.request_length_mismatch
        writer.close()
        assert plc.events_of("overstated_length")


async def test_a_zero_data_length_is_answered_with_the_command_echoed_as_zero() -> None:
    """L = 0x0000 returned 0xC061 echoing cmd 0000 sub 0000: the CPU read the command
    out of the monitoring timer field of a frame that had none."""
    async with simulator() as plc:
        host, port = plc.address(TCP)
        reader, writer = await asyncio.open_connection(host, port)
        raw = bytearray(read_words_frame(count=1))
        at = THREE_E.subheader_units(BINARY) + Route.wire_len(BINARY)
        raw[at : at + 2] = BINARY.number(0, bits=16)
        writer.write(bytes(raw))
        await writer.drain()
        answer = await read_frame(reader, timeout=1.0)
        assert answer is not None
        assert end_code_of(answer) == 0xC061
        assert answer[-4:] == b"\x00\x00\x00\x00", "command and subcommand echo as zero"
        writer.close()


# --------------------------------------------------------------------------------------
# The frame type an entry is not configured for
# --------------------------------------------------------------------------------------


async def test_a_4e_frame_is_served_on_a_3e_entry() -> None:
    """Two Mitsubishi manuals say this is impossible. Our FX5U does it."""
    async with simulator() as plc:
        host, port = plc.address(TCP)
        reader, writer = await asyncio.open_connection(host, port)
        writer.write(read_words_frame(frame=FOUR_E, count=1, serial=0x1234))
        await writer.drain()
        answer = await read_frame(reader, frame=FOUR_E)
        assert answer is not None
        assert serial_of(answer) == 0x1234
        writer.close()


async def test_a_strict_entry_refuses_the_wrong_frame_type_by_silence() -> None:
    board = HEALTHY.replace(accept_4e_on_3e_entry=False, silence_on_wrong_encoding=True)
    async with simulator(pathology=board) as plc:
        host, port = plc.address(TCP)
        reader, writer = await asyncio.open_connection(host, port)
        writer.write(read_words_frame(frame=FOUR_E, count=1, serial=0x1234))
        await writer.drain()
        assert await read_frame(reader, frame=FOUR_E, timeout=0.3) is None
        writer.close()


async def test_the_4e_subheader_tail_comes_back_zeroed() -> None:
    """The two bytes after the serial are not a second correlation field."""
    async with simulator() as plc:
        host, port = plc.address(TCP_4E)
        reader, writer = await asyncio.open_connection(host, port)
        raw = bytearray(read_words_frame(frame=FOUR_E, count=1, serial=0xBEEF))
        raw[4:6] = b"\xaa\xbb"
        writer.write(bytes(raw))
        await writer.drain()
        answer = await read_frame(reader, frame=FOUR_E)
        assert answer is not None
        assert answer[4:6] == b"\x00\x00"
        writer.close()


async def test_a_wrong_serial_echo_is_reproducible() -> None:
    """The in-band shape of the coalescing corruption, on demand."""
    board = HEALTHY.replace(wrong_serial_echo=True)
    async with simulator(pathology=board) as plc:
        host, port = plc.address(TCP_4E)
        reader, writer = await asyncio.open_connection(host, port)
        writer.write(read_words_frame(frame=FOUR_E, count=1, serial=0x0001))
        await writer.drain()
        answer = await read_frame(reader, frame=FOUR_E)
        assert answer is not None
        assert serial_of(answer) != 0x0001
        writer.close()


# --------------------------------------------------------------------------------------
# Stale replies and late replies
# --------------------------------------------------------------------------------------


async def test_a_stale_reply_answers_with_the_previous_response() -> None:
    """One transaction behind, forever: what an unread tail on the socket produces."""
    board = HEALTHY.replace(stale_reply=True)
    async with simulator(pathology=board) as plc:
        plc.memory.set_u16("D", 0, 0xAAAA)
        plc.memory.set_u16("D", 8, 0xBBBB)
        host, port = plc.address(TCP)
        reader, writer = await asyncio.open_connection(host, port)

        writer.write(read_words_frame(device=b"\x00\x00\x00\xa8", count=1))
        await writer.drain()
        first = await read_frame(reader)
        assert first is not None
        assert payload_of(first) == BINARY.words((0xAAAA,))

        writer.write(read_words_frame(device=b"\x08\x00\x00\xa8", count=1))
        await writer.drain()
        second = await read_frame(reader)
        assert second is not None
        assert payload_of(second) == BINARY.words((0xAAAA,)), (
            "the answer to the D8 read is the D0 read's response"
        )
        writer.close()
        assert plc.events_of("stale_reply")


async def test_a_late_reply_arrives_after_a_short_deadline() -> None:
    """A reply that arrives after the client gave up must be dropped, never returned."""
    board = HEALTHY.replace(late_reply_s=0.2)
    async with simulator(pathology=board) as plc:
        host, port = plc.address(TCP)
        reader, writer = await asyncio.open_connection(host, port)
        writer.write(read_words_frame(count=1))
        await writer.drain()
        assert await read_frame(reader, timeout=0.05) is None
        assert await read_frame(reader, timeout=1.0) is not None
        writer.close()


# --------------------------------------------------------------------------------------
# UDP
# --------------------------------------------------------------------------------------


class _Client(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.received: list[bytes] = []

    def datagram_received(self, data: bytes, addr: object) -> None:
        del addr
        self.received.append(data)


async def test_udp_does_not_coalesce() -> None:
    """Datagrams are framed, so the TCP corruption simply does not exist on UDP.

    Two requests fired with no read between returned both responses, correct and in
    order, on FX5U-32MT/DS fw 1.065 (measured 2026-09-06). This is the strongest
    argument for UDP on this hardware and it reverses the naive assumption.
    """
    async with simulator() as plc:
        plc.memory.set_u16("D", 0, 0x1111)
        plc.memory.set_u16("D", 8, 0x8888)
        host, port = plc.address(UDP)
        loop = asyncio.get_running_loop()
        transport, protocol = await loop.create_datagram_endpoint(
            _Client, remote_addr=(host, port)
        )
        try:
            transport.sendto(read_words_frame(device=b"\x00\x00\x00\xa8", count=1))
            transport.sendto(read_words_frame(device=b"\x08\x00\x00\xa8", count=1))
            for _ in range(40):
                if len(protocol.received) >= 2:
                    break
                await asyncio.sleep(0.01)
            assert len(protocol.received) == 2
            assert payload_of(protocol.received[0]) == BINARY.words((0x1111,))
            assert payload_of(protocol.received[1]) == BINARY.words((0x8888,))
        finally:
            transport.close()
        assert not plc.events_of("coalesced")


async def test_udp_drops_silently_past_the_in_flight_depth() -> None:
    """64 pipelined requests returned 44: no end code, no ICMP, no error of any kind.

    The client learns only by timing out on a serial that never comes back, which is why
    a lost datagram must raise its own named error carrying the serial and the depth --
    never a retry and never a generic timeout.
    """
    # 0.05 s, not 0.01: below the event loop's datagram delivery granularity
    # nothing is ever concurrently in flight and the ceiling is never reached. On
    # Windows before CPython 3.13 that granularity is ~15.6 ms, so a 10 ms service
    # time answered all 64 and dropped none -- measured 2026-09-24 on 3.11.15.
    board = HEALTHY.replace(udp_drop_above_depth=8, udp_service_delay_s=0.05)
    async with simulator(pathology=board) as plc:
        host, port = plc.address(UDP)
        loop = asyncio.get_running_loop()
        transport, protocol = await loop.create_datagram_endpoint(
            _Client, remote_addr=(host, port)
        )
        try:
            for _ in range(64):
                transport.sendto(read_words_frame(count=1))
            await asyncio.sleep(1.0)
        finally:
            transport.close()
        assert 0 < len(protocol.received) < 64, (
            f"expected silent loss past a depth of 8; got {len(protocol.received)} of 64"
        )
        dropped = plc.events_of("datagram_dropped")
        assert dropped, "the loss must be recorded on the server side or it is invisible"
        assert "no error of any kind" in dropped[0].detail


async def test_udp_deterministic_loss_drops_every_nth_datagram() -> None:
    board = HEALTHY.replace(drop_every_nth_datagram=2, udp_service_delay_s=0.0)
    async with simulator(pathology=board) as plc:
        host, port = plc.address(UDP)
        loop = asyncio.get_running_loop()
        transport, protocol = await loop.create_datagram_endpoint(
            _Client, remote_addr=(host, port)
        )
        try:
            for _ in range(6):
                transport.sendto(read_words_frame(count=1))
                await asyncio.sleep(0.02)
        finally:
            transport.close()
        assert len(protocol.received) == 3
        assert len(plc.events_of("datagram_dropped")) == 3


async def test_udp_has_no_one_connection_limit() -> None:
    """The entry is bound to a peer address, not to a single socket."""
    async with simulator() as plc:
        host, port = plc.address(UDP)
        loop = asyncio.get_running_loop()
        endpoints = []
        try:
            for _ in range(2):
                transport, protocol = await loop.create_datagram_endpoint(
                    _Client, remote_addr=(host, port)
                )
                endpoints.append((transport, protocol))
                transport.sendto(read_words_frame(count=1))
            await asyncio.sleep(0.2)
            assert all(protocol.received for _t, protocol in endpoints)
        finally:
            for transport, _protocol in endpoints:
                transport.close()
        assert not plc.events_of("connection_refused")


# --------------------------------------------------------------------------------------
# The one message the PLC sends on its own
# --------------------------------------------------------------------------------------


async def test_an_ondemand_frame_arrives_with_a_request_subheader() -> None:
    """0x2101 is never anybody's response, and a client must not resynchronise on it."""
    async with simulator(entries=(Entry(name=TCP, protocol="tcp"),)) as plc:
        host, port = plc.address(TCP)
        reader, writer = await asyncio.open_connection(host, port)
        # Retry until a peer is actually registered, bounded. open_connection returns as
        # soon as the CLIENT's handshake completes, which does not require the server's
        # handler to have run, so the connection may not be in the entry's writer list
        # yet and push_ondemand has nobody to send to. It returns 0 and sends nothing in
        # that case, which is what makes retrying safe rather than duplicating frames.
        # Failed on both macOS cells while passing on ubuntu and windows (CI 2026-09-24):
        # a scheduling difference, not a defect in the push.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5.0
        sent = await plc.push_ondemand(b"\x01\x02")
        while sent == 0:
            if loop.time() > deadline:
                raise AssertionError("no peer was ever registered for the tcp entry")
            await asyncio.sleep(0.005)
            sent = await plc.push_ondemand(b"\x01\x02")
        assert sent == 1
        raw = await asyncio.wait_for(reader.readexactly(11 + 2), 1.0)
        assert raw[:2] == b"\x50\x00", "a request subheader: the PLC is the sender"
        assert BINARY.read_number(raw, THREE_E.prefix_units(BINARY) + 2, bits=16) == 0x2101
        writer.close()


# --------------------------------------------------------------------------------------
# The entry map itself
# --------------------------------------------------------------------------------------


async def test_every_entry_binds_its_own_coding_and_frame() -> None:
    """The bug where every listener serves the last entry in the list is silent."""
    async with simulator() as plc:
        assert {entry.name for entry in plc.entries} == {
            "tcp",
            "tcp-4e",
            "udp",
            "udp-4e",
            "tcp-ascii",
        }
        assert plc.entry("tcp-4e").frame is FrameType.FOUR_E
        assert plc.entry("tcp-ascii").encoding is Encoding.ASCII_XY_HEX
        ports = {plc.port_of(entry.name) for entry in plc.entries}
        assert len(ports) == len(plc.entries), "each entry binds its own port"


async def test_the_ascii_entry_serves_ascii() -> None:
    async with simulator() as plc:
        plc.memory.set_u16("D", 0, 0x1234)
        host, port = plc.address("tcp-ascii")
        reader, writer = await asyncio.open_connection(host, port)
        writer.write(read_words_frame(device=b"D*000000", count=1, codec=ASCII))
        await writer.drain()
        answer = await read_frame(reader, codec=ASCII)
        assert answer is not None
        assert end_code_of(answer, codec=ASCII) == 0x0000
        assert payload_of(answer, codec=ASCII) == b"1234"
        writer.close()


async def test_duplicate_entry_names_are_refused() -> None:
    with pytest.raises(ValueError, match="unique"):
        PlcSimulator(
            entries=(Entry(name="a", protocol="tcp"), Entry(name="a", protocol="udp"))
        )


async def test_the_transcript_records_both_directions() -> None:
    async with simulator(entries=(Entry(name=TCP, protocol="tcp"),)) as plc:
        host, port = plc.address(TCP)
        reader, writer = await asyncio.open_connection(host, port)
        writer.write(read_words_frame(count=1))
        await writer.drain()
        await read_frame(reader)
        writer.close()
        directions = [record.direction for record in plc.transcript]
        assert directions == ["rx", "tx"]
        assert plc.transcript[0].hex.startswith("50 00")
