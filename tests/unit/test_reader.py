"""``ResponseAccumulator``: every segmentation a network can produce, with no network.

TCP is a stream and an SLMP response is delimited only by its own length field. A
1931-byte response was captured three times on FX5U-32MT/DS fw 1.065, 2026-09-06: twice
as one chunk, and once as ``1460`` then ``471`` bytes 3.0 ms apart -- the Ethernet MSS
boundary. Whether you see the split depends on host scheduling, so a reader that only
works when the response arrives whole passes two runs in three and fails in production.

DESIGN section 5.5 asks for exactly this file: every golden response frame fed **one unit
at a time**, **split exactly at 1460**, and in **200 seeded random chunkings**, all
yielding an identical :class:`~aslmp.wire.raw.RawResponse`. The seed is fixed, so a
failure is reproducible rather than a flake somebody reruns.

The rest of the file is refusals, because the alternatives are all real libraries:
``pymcprotocol`` zero-fills a truncated response, ``pymelsec`` returns ``[]`` from an
error path, and ``Esmool`` leaves the unread tail on the socket so the next transaction
reads the previous one's bytes as fresh data.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import pytest

from aslmp.wire.codec import ASCII, BINARY, Codec
from aslmp.wire.frames import FOUR_E, THREE_E, FrameFormat, request_body
from aslmp.wire.raw import (
    RawResponse,
    SlmpFrameFormatError,
    SlmpIncompleteFrameError,
    SlmpSerialMismatchError,
    SlmpTrailingDataError,
    SlmpUnsolicitedFrameError,
)
from aslmp.wire.reader import ResponseAccumulator
from aslmp.wire.route import Route

VECTORS = Path(__file__).resolve().parents[1] / "vectors" / "frame_vectors.jsonl"

BY_NAME: dict[str, Codec] = {"binary": BINARY, "ascii": ASCII}
BY_FRAME: dict[str, FrameFormat] = {"3E": THREE_E, "4E": FOUR_E}

MSS = 1460
"""The Ethernet maximum segment size the FX5U's 1931-byte response split at."""

RANDOM_CHUNKINGS = 200
CHUNKING_SEED = 20260906
"""Fixed: a segmentation bug that only reproduces one run in three is worse than none."""


def load_responses() -> tuple[dict[str, Any], ...]:
    rows: list[dict[str, Any]] = []
    for line in VECTORS.read_text(encoding="ascii").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row["direction"] == "response":
            rows.append(row)
    if not rows:  # pragma: no cover - a corpus with no responses is a bug
        raise AssertionError(f"no response vectors in {VECTORS}")
    return tuple(rows)


RESPONSES = load_responses()
IDS = [str(r["id"]) for r in RESPONSES]


def accumulator(row: dict[str, Any]) -> ResponseAccumulator:
    return ResponseAccumulator(BY_FRAME[row["frame"]], BY_NAME[row["codec"]])


def whole(row: dict[str, Any]) -> bytes:
    return bytes.fromhex(row["hex"])


def drive(row: dict[str, Any], chunks: list[bytes]) -> RawResponse:
    """Feed ``chunks`` in order and take the frame. Never reads past what it wants."""
    acc = accumulator(row)
    for chunk in chunks:
        acc.feed(chunk)
    return acc.take()


def random_chunks(data: bytes, rng: random.Random) -> list[bytes]:
    chunks: list[bytes] = []
    index = 0
    while index < len(data):
        take = rng.randint(1, max(1, len(data) - index))
        chunks.append(data[index : index + take])
        index += take
    return chunks


# ======================================================================================
# The three chunkings DESIGN section 5.5 names
# ======================================================================================


@pytest.mark.parametrize("row", RESPONSES, ids=IDS)
def test_a_whole_frame_in_one_feed(row: dict[str, Any]) -> None:
    """A UDP datagram arrives as one message and must not have to be sliced."""
    assert drive(row, [whole(row)]) == BY_FRAME[row["frame"]].parse(
        whole(row), BY_NAME[row["codec"]]
    )


@pytest.mark.parametrize("row", RESPONSES, ids=IDS)
def test_one_wire_unit_at_a_time(row: dict[str, Any]) -> None:
    """The pathological drip. Identical result, and it must never over-read."""
    data = whole(row)
    expected = drive(row, [data])
    acc = accumulator(row)
    for index in range(len(data)):
        assert acc.bytes_needed > 0, "the accumulator stopped wanting bytes too early"
        assert not acc.complete
        acc.feed(data[index : index + 1])
    assert acc.complete
    assert acc.bytes_needed == 0
    assert acc.take() == expected


@pytest.mark.parametrize("row", RESPONSES, ids=IDS)
def test_split_exactly_at_the_ethernet_mss(row: dict[str, Any]) -> None:
    """Trial 3 on the bench: 1460 then the rest, 3.0 ms apart.

    Every corpus frame is shorter than one segment, so this also covers the boundary
    case where the split point falls beyond the frame and the first chunk is the whole
    thing -- which is exactly how the same code path serves both.
    """
    data = whole(row)
    chunks = [data[:MSS], data[MSS:]]
    assert drive(row, [c for c in chunks if c]) == drive(row, [data])


@pytest.mark.parametrize("row", RESPONSES, ids=IDS)
def test_two_hundred_seeded_random_chunkings(row: dict[str, Any]) -> None:
    """Same bytes, arbitrary arrival, identical frame. 200 times, reproducibly."""
    data = whole(row)
    expected = drive(row, [data])
    rng = random.Random(CHUNKING_SEED)
    for _ in range(RANDOM_CHUNKINGS):
        assert drive(row, random_chunks(data, rng)) == expected


@pytest.mark.parametrize("row", RESPONSES, ids=IDS)
def test_bytes_needed_walks_down_to_zero_and_stops(row: dict[str, Any]) -> None:
    """The transport loops on this number, so it must never overstate what it wants."""
    data = whole(row)
    frame, codec = BY_FRAME[row["frame"]], BY_NAME[row["codec"]]
    acc = accumulator(row)
    assert acc.bytes_needed == frame.prefix_units(codec)
    assert acc.declared_length is None
    acc.feed(data[: frame.prefix_units(codec)])
    assert acc.declared_length == row["declared_length"]
    assert acc.bytes_needed == row["declared_length"]
    acc.feed(data[frame.prefix_units(codec) :])
    assert acc.bytes_needed == 0
    acc.take()
    assert acc.bytes_needed == 0


def test_a_long_segmented_response_reassembles_byte_for_byte() -> None:
    """The 1931-byte case that made this class necessary, rebuilt exactly.

    960 word points is 1920 bytes of data, ``L = 0x0782`` = 1922, and 1931 bytes on the
    wire -- the frame the FX5U split at 1460 on one trial in three.
    """
    payload = bytes(i % 256 for i in range(1920))
    body = BINARY.number(0x0000, bits=16) + payload
    data = THREE_E.build_response(route=Route.OWN_STATION, body=body, codec=BINARY)
    assert len(data) == 1931
    acc = ResponseAccumulator(THREE_E, BINARY)
    acc.feed(data[:MSS])
    assert acc.bytes_needed == len(data) - MSS
    acc.feed(data[MSS:])
    response = acc.take()
    assert response.length == 1922
    assert response.payload == payload
    assert response.raw == data


# ======================================================================================
# Refusals
# ======================================================================================


def test_a_truncated_frame_never_becomes_a_response() -> None:
    """It stays hungry. The opposite of zero-filling the tail."""
    data = bytes.fromhex("D00000FFFF030008000000951902123011")
    acc = ResponseAccumulator(THREE_E, BINARY)
    acc.feed(data[:-2])
    assert not acc.complete
    assert acc.bytes_needed == 2
    with pytest.raises(SlmpIncompleteFrameError, match="2 more"):
        acc.take()


def test_an_empty_buffer_is_not_end_code_zero() -> None:
    """``pymcprotocol`` ``type3e.py:414`` grades an empty buffer as success."""
    acc = ResponseAccumulator(THREE_E, BINARY)
    assert acc.bytes_needed == 9
    with pytest.raises(SlmpIncompleteFrameError, match="0x0000"):
        acc.take()


def test_feeding_past_the_end_of_the_frame_raises() -> None:
    """A second response in the same buffer is the measured coalescing corruption."""
    data = bytes.fromhex("D00000FFFF030002000000")
    acc = ResponseAccumulator(THREE_E, BINARY)
    with pytest.raises(SlmpTrailingDataError, match="surplus"):
        acc.feed(data + data)


def test_feeding_after_the_frame_has_been_taken_raises() -> None:
    data = bytes.fromhex("D00000FFFF030002000000")
    acc = ResponseAccumulator(THREE_E, BINARY)
    acc.feed(data)
    acc.take()
    with pytest.raises(SlmpTrailingDataError, match="already"):
        acc.feed(b"\xd0\x00")


def test_taking_twice_raises_rather_than_repeating_a_stale_frame() -> None:
    """One accumulator, one exchange, like the transaction token it travels with."""
    data = bytes.fromhex("D00000FFFF030002000000")
    acc = ResponseAccumulator(THREE_E, BINARY)
    acc.feed(data)
    acc.take()
    with pytest.raises(SlmpIncompleteFrameError, match="already been taken"):
        acc.take()


def test_a_bad_subheader_is_named_as_soon_as_the_prefix_is_in() -> None:
    """Not after waiting ``L`` more units -- that is what an overstated length looks like."""
    acc = ResponseAccumulator(THREE_E, BINARY)
    with pytest.raises(SlmpFrameFormatError, match="subheader"):
        acc.feed(bytes.fromhex("C00000FFFF03000200"))


def test_an_unsolicited_ondemand_frame_is_refused_at_the_prefix() -> None:
    """SH(NA)-080956ENG-M section 5.11 p.204. It is never anybody's response."""
    ondemand = THREE_E.build(
        route=Route.OWN_STATION,
        body=request_body(
            BINARY, monitoring_timer=0, command=0x2101, subcommand=0, payload=b"\x01\x00"
        ),
        codec=BINARY,
    )
    acc = ResponseAccumulator(THREE_E, BINARY)
    with pytest.raises(SlmpUnsolicitedFrameError, match="Ondemand"):
        acc.feed(ondemand)


def test_an_impossible_declared_length_is_refused_at_the_prefix() -> None:
    acc = ResponseAccumulator(THREE_E, BINARY)
    with pytest.raises(SlmpFrameFormatError, match="impossible"):
        acc.feed(bytes.fromhex("D00000FFFF03000100"))


# ======================================================================================
# 4E: the serial is the only in-band correlation SLMP has
# ======================================================================================


def test_the_4e_serial_is_checked_when_the_frame_is_taken() -> None:
    data = bytes.fromhex("D4003412000000FFFF03000600000000007042")
    good = ResponseAccumulator(FOUR_E, BINARY, expect_serial=0x1234)
    good.feed(data)
    assert good.take().serial == 0x1234
    bad = ResponseAccumulator(FOUR_E, BINARY, expect_serial=0x0002)
    bad.feed(data)
    with pytest.raises(SlmpSerialMismatchError, match="0x0002"):
        bad.take()


def test_a_serial_cannot_be_expected_on_3e() -> None:
    """Constructing the accumulator is where a 3E connection is told it has no serial."""
    with pytest.raises(ValueError, match="no serial"):
        ResponseAccumulator(THREE_E, BINARY, expect_serial=1)


def test_the_4e_subheader_tail_is_exposed_and_never_validated() -> None:
    """A-3, settled on the bench: sent ``AA 55``, returned ``00 00``, no complaint."""
    data = bytes.fromhex("D400EFBE000000FFFF03000600000000007042")
    acc = ResponseAccumulator(FOUR_E, BINARY, expect_serial=0xBEEF)
    acc.feed(data)
    response = acc.take()
    assert response.subheader_tail == b"\x00\x00"
    odd = bytearray(data)
    odd[4:6] = b"\xaa\x55"
    other = ResponseAccumulator(FOUR_E, BINARY, expect_serial=0xBEEF)
    other.feed(bytes(odd))
    assert other.take().subheader_tail == b"\xaa\x55"


# ======================================================================================
# The structural contract the transport depends on
# ======================================================================================


def test_it_satisfies_the_transport_reassembler_protocol() -> None:
    """``bytes_needed`` and ``feed`` and nothing else.

    ``aslmp.transport`` must never import ``aslmp.wire``: it takes this shape
    structurally, so the dependency runs upward while the knowledge runs downward. A
    transport that can name a frame will eventually parse one.
    """
    acc = ResponseAccumulator(THREE_E, BINARY)
    assert isinstance(acc.bytes_needed, int)
    acc.feed(b"")
    assert acc.bytes_needed == 9
    assert callable(acc.feed)


def test_the_buffer_is_visible_for_a_diagnostic() -> None:
    """An exception prints what actually came back, including a partial frame."""
    acc = ResponseAccumulator(THREE_E, BINARY)
    acc.feed(b"\xd0\x00\x00")
    assert acc.buffered == b"\xd0\x00\x00"
    assert "3 unit(s) in" in repr(acc) or acc.bytes_needed == 6
