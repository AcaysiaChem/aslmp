"""3E and 4E frames, against Mitsubishi's own hex, Apache PLC4X's, and the bench's.

Three kinds of check, in descending order of how much they are worth.

**Golden byte vectors** (``tests/vectors/frame_vectors.jsonl``) are the independent
oracle. Every expected byte string in that file was typed in from a named manual page, a
named bench log or Apache PLC4X's ``ParserSerializerTestsuite.xml`` -- never produced by
this library. Each row is asserted three ways: ``build(decoded) == bytes``,
``parse(bytes) == decoded`` and ``parse(build(x)) == x``. If the corpus and the code
disagree, the corpus is right.

**The three ``L`` guards of DESIGN section 4.2**, because the failure is asymmetric and
measured: an understated data length returns end code ``0xC061`` and the connection
recovers, while an **overstated** one gets no response at all -- the PLC blocks waiting
for bytes that never come and it looks exactly like a dead PLC (FX5U-32MT/DS fw 1.065,
2026-09-06). So: an AST walk asserting ``L`` is assigned exactly once in
``wire/frames.py``; the ``expect_body_len`` runtime guard in both directions; and a
property test over generated bodies, routes, codecs and frame formats.

**Refusals.** A parser that resynchronises, that reads an error block out of a frame too
short to hold one, or that accepts a request-shaped subheader as a response, produces a
plausible wrong answer with no error anywhere. Every one of those raises here.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from aslmp.wire.codec import ASCII, BINARY, Codec, SlmpCodecValueError
from aslmp.wire.frames import (
    FOUR_E,
    FRAMES,
    THREE_E,
    FrameFormat,
    FrameType,
    request_body,
    response_body,
)
from aslmp.wire.raw import (
    ErrorInfo,
    RawRequest,
    RawResponse,
    SlmpErrorInfoError,
    SlmpFrameFormatError,
    SlmpSerialMismatchError,
    SlmpShortFrameError,
    SlmpTrailingDataError,
    SlmpUnsolicitedFrameError,
)
from aslmp.wire.route import Route

VECTORS = Path(__file__).resolve().parents[1] / "vectors" / "frame_vectors.jsonl"
FRAMES_SOURCE = (
    Path(__file__).resolve().parents[2] / "src" / "aslmp" / "wire" / "frames.py"
)

BY_NAME: dict[str, Codec] = {"binary": BINARY, "ascii": ASCII}
BY_FRAME: dict[str, FrameFormat] = {"3E": THREE_E, "4E": FOUR_E}


def load_vectors() -> tuple[dict[str, Any], ...]:
    rows: list[dict[str, Any]] = []
    for lineno, line in enumerate(VECTORS.read_text(encoding="ascii").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict):
            raise TypeError(f"{VECTORS}:{lineno}: a vector row must be a JSON object")
        rows.append(row)
    return tuple(rows)


VECTOR_ROWS = load_vectors()


def vector_id(row: dict[str, Any]) -> str:
    return str(row["id"])


def parametrize(direction: str) -> pytest.MarkDecorator:
    rows = [r for r in VECTOR_ROWS if r["direction"] == direction]
    if not rows:  # pragma: no cover - a corpus that lost a whole direction is a bug
        raise AssertionError(f"no {direction!r} vectors in {VECTORS}")
    return pytest.mark.parametrize("row", rows, ids=[vector_id(r) for r in rows])


def frame_of(row: dict[str, Any]) -> FrameFormat:
    return BY_FRAME[row["frame"]]


def codec_of(row: dict[str, Any]) -> Codec:
    return BY_NAME[row["codec"]]


def wire(row: dict[str, Any], key: str = "hex") -> bytes:
    return bytes.fromhex(row[key])


def route_of(value: list[int]) -> Route:
    return Route(*value)


def error_info_of(row: dict[str, Any]) -> ErrorInfo | None:
    info = row.get("error_info")
    if info is None:
        return None
    return ErrorInfo(
        responding=route_of(info["responding"]),
        command=info["command"],
        subcommand=info["subcommand"],
    )


def body_of(row: dict[str, Any]) -> bytes:
    """Rebuild the row's body from its decoded fields, in the row's own coding."""
    codec = codec_of(row)
    if row["direction"] == "request":
        return request_body(
            codec,
            monitoring_timer=row["monitoring_timer"],
            command=row["command"],
            subcommand=row["subcommand"],
            payload=wire(row, "payload"),
        )
    return response_body(
        codec,
        end_code=row["end_code"],
        payload=wire(row, "payload"),
        error_info=error_info_of(row),
        extra_error_data=wire(row, "extra_error_data"),
    )


def rebuild(row: dict[str, Any]) -> bytes:
    frame, codec = frame_of(row), codec_of(row)
    builder = frame.build if row["direction"] == "request" else frame.build_response
    return builder(
        route=route_of(row["route"]),
        body=body_of(row),
        codec=codec,
        serial=row["serial"],
    )


# ======================================================================================
# The corpus itself
# ======================================================================================


def test_the_corpus_is_present_and_has_not_quietly_shrunk() -> None:
    """Vectors are added and never deleted: a lost row is a lost regression test."""
    ids = [vector_id(r) for r in VECTOR_ROWS]
    assert len(ids) == len(set(ids)), "duplicate vector ids"
    assert len(ids) >= 35, "the frame corpus has shrunk; vectors are never removed"


@pytest.mark.parametrize("row", VECTOR_ROWS, ids=[vector_id(r) for r in VECTOR_ROWS])
def test_every_vector_names_its_source(row: dict[str, Any]) -> None:
    """A vector with no provenance is an assertion nobody can check."""
    if row["provenance"] == "live":
        assert row["cpu"] and row["firmware"] and row["measured"]
    else:
        assert row["manual"] and row["revision"]
    assert row["meaning"].strip()
    assert row["frame"] in BY_FRAME
    assert row["codec"] in BY_NAME


def test_the_measured_vectors_name_the_one_cpu_we_have() -> None:
    """One unit, one firmware. A measurement that names no silicon is a claim."""
    live = [r for r in VECTOR_ROWS if r["provenance"] == "live"]
    assert live, "the hardware baseline is the whole point of this corpus"
    assert {r["cpu"] for r in live} == {"FX5U-32MT/DS"}
    assert {r["firmware"] for r in live} == {"1.065"}


def test_the_plc4x_rows_carry_their_attribution() -> None:
    """Apache-2.0 is a straight licence match, and the row says where it came from."""
    borrowed = [r for r in VECTOR_ROWS if r.get("source") == "plc4x"]
    assert borrowed, "the PLC4X testsuite rows are a second party's reading; keep them"
    for row in borrowed:
        assert "Apache" in row["source_note"]
        assert "PLC4X" in row["source_note"]


def test_an_unbuildable_row_says_why_in_the_data() -> None:
    """Exclusions are data, not silence (DESIGN section 5.2)."""
    excluded = [r for r in VECTOR_ROWS if r.get("buildable") is False]
    assert excluded, "the A-3 subheader-tail probe is a frame we deliberately cannot emit"
    for row in excluded:
        assert row["not_buildable_because"].strip()


# ======================================================================================
# The golden vectors: build(decoded) == bytes, parse(bytes) == decoded, and the round trip
# ======================================================================================


@parametrize("request")
def test_building_a_request_reproduces_the_captured_bytes(row: dict[str, Any]) -> None:
    if row.get("buildable") is False:
        pytest.skip(row["not_buildable_because"])
    assert rebuild(row) == wire(row)


@parametrize("response")
def test_building_a_response_reproduces_the_captured_bytes(row: dict[str, Any]) -> None:
    assert rebuild(row) == wire(row)


@parametrize("request")
def test_parsing_a_request_recovers_every_declared_field(row: dict[str, Any]) -> None:
    parsed = frame_of(row).parse_request(wire(row), codec_of(row))
    assert isinstance(parsed, RawRequest)
    assert parsed.route == route_of(row["route"])
    assert parsed.serial == row["serial"]
    assert parsed.subheader_tail == wire(row, "subheader_tail")
    assert parsed.length == row["declared_length"]
    assert parsed.monitoring_timer == row["monitoring_timer"]
    assert parsed.command == row["command"]
    assert parsed.subcommand == row["subcommand"]
    assert parsed.payload == wire(row, "payload")
    assert parsed.raw == wire(row)


@parametrize("response")
def test_parsing_a_response_recovers_every_declared_field(row: dict[str, Any]) -> None:
    parsed = frame_of(row).parse(wire(row), codec_of(row))
    assert isinstance(parsed, RawResponse)
    assert parsed.route == route_of(row["route"])
    assert parsed.serial == row["serial"]
    assert parsed.subheader_tail == wire(row, "subheader_tail")
    assert parsed.length == row["declared_length"]
    assert parsed.end_code == row["end_code"]
    assert parsed.payload == wire(row, "payload")
    assert parsed.error_info == error_info_of(row)
    assert parsed.extra_error_data == wire(row, "extra_error_data")
    assert parsed.raw == wire(row)


@pytest.mark.parametrize("row", VECTOR_ROWS, ids=[vector_id(r) for r in VECTOR_ROWS])
def test_the_declared_length_is_the_frame_minus_its_prefix(row: dict[str, Any]) -> None:
    """``L`` is the whole frame less the fixed prefix, in this codec's wire units."""
    frame, codec = frame_of(row), codec_of(row)
    assert len(wire(row)) == frame.prefix_units(codec) + row["declared_length"]


@parametrize("request")
def test_a_request_round_trips_through_parse_and_build(row: dict[str, Any]) -> None:
    frame, codec = frame_of(row), codec_of(row)
    parsed = frame.parse_request(wire(row), codec)
    rebuilt = frame.build(
        route=parsed.route,
        body=request_body(
            codec,
            monitoring_timer=parsed.monitoring_timer,
            command=parsed.command,
            subcommand=parsed.subcommand,
            payload=parsed.payload,
        ),
        codec=codec,
        serial=parsed.serial,
    )
    if row.get("buildable") is False:
        # The library cannot emit this frame's subheader tail, and says so rather than
        # asserting a byte equality it would fail: everything but the tail must match.
        assert rebuilt != wire(row)
        assert frame.parse_request(rebuilt, codec).subheader_tail == b"\x00\x00"
        return
    assert rebuilt == wire(row)
    assert frame.parse_request(rebuilt, codec) == parsed


@parametrize("response")
def test_a_response_round_trips_through_parse_and_build(row: dict[str, Any]) -> None:
    frame, codec = frame_of(row), codec_of(row)
    parsed = frame.parse(wire(row), codec)
    rebuilt = frame.build_response(
        route=parsed.route,
        body=response_body(
            codec,
            end_code=parsed.end_code,
            payload=parsed.payload,
            error_info=parsed.error_info,
            extra_error_data=parsed.extra_error_data,
        ),
        codec=codec,
        serial=parsed.serial,
    )
    assert rebuilt == wire(row)
    assert frame.parse(rebuilt, codec) == parsed


# ======================================================================================
# Guard 1 of 3: the AST walk. L is assigned exactly once in the whole module.
# ======================================================================================


def _assigned_names(tree: ast.AST) -> list[tuple[str, int]]:
    """Every name this module binds by assignment, with its line number."""
    names: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(
            node, ast.AnnAssign | ast.AugAssign | ast.NamedExpr | ast.For | ast.AsyncFor
        ):
            targets = [node.target]
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            targets = [node.optional_vars]
        for target in targets:
            for inner in ast.walk(target):
                if isinstance(inner, ast.Name):
                    names.append((inner.id, getattr(node, "lineno", 0)))
    return names


def test_the_length_expression_is_assigned_exactly_once_in_wire_frames() -> None:
    """The single-expression rule, enforced rather than asserted in a docstring.

    An understated ``L`` returns ``0xC061`` and the connection recovers. An OVERSTATED
    one produces no response at all: the CPU blocks for bytes that never arrive, and it
    is indistinguishable from a dead PLC (FX5U-32MT/DS fw 1.065, 2026-09-06). A second
    place that computes a length is how the two directions drift apart.
    """
    tree = ast.parse(FRAMES_SOURCE.read_text(encoding="utf-8"))
    assigned = _assigned_names(tree)
    ells = [line for name, line in assigned if name == "L"]
    assert len(ells) == 1, (
        f"`L` is assigned {len(ells)} time(s) in {FRAMES_SOURCE.name} at lines {ells}. "
        f"Exactly one expression in this package may produce L."
    )
    lengths = [(name, line) for name, line in assigned if name == "length"]
    assert not lengths, (
        f"{FRAMES_SOURCE.name} binds the name 'length' at {lengths}. Length arithmetic "
        f"in this module is one expression named L; anything else is a second one "
        f"wearing a different hat."
    )


def test_the_ast_guard_would_catch_a_second_length_expression(tmp_path: Path) -> None:
    """The detector is only worth having if it fires."""
    source = "def f(body):\n    L = len(body)\n    length = L + 2\n    return L, length\n"
    tree = ast.parse(source)
    names = [name for name, _ in _assigned_names(tree)]
    assert names.count("L") == 1
    assert "length" in names


# ======================================================================================
# Guard 2 of 3: expect_body_len, in both directions
# ======================================================================================


def test_an_overstated_body_length_is_refused_before_it_can_hang() -> None:
    """The direction that produces silence on real hardware.

    ``d2`` on the bench: a request declaring ``L = 0x000E`` for a 12-byte body got no
    response at all, and the 3000 ms timeout that followed is indistinguishable from a
    dead PLC. Refusing in the builder is the only place this is cheap.
    """
    body = request_body(BINARY, monitoring_timer=0, command=0x0401, subcommand=0)
    with pytest.raises(SlmpFrameFormatError, match="OVERSTATED"):
        THREE_E.build(
            route=Route.OWN_STATION,
            body=body,
            codec=BINARY,
            expect_body_len=len(body) + 2,
        )


def test_an_understated_body_length_is_refused_too() -> None:
    """``d1``/``d3`` on the bench returned ``0xC061`` -- loud, but still wrong."""
    body = request_body(BINARY, monitoring_timer=0, command=0x0401, subcommand=0)
    with pytest.raises(SlmpFrameFormatError, match="0xC061"):
        THREE_E.build(
            route=Route.OWN_STATION,
            body=body,
            codec=BINARY,
            expect_body_len=len(body) - 2,
        )


def test_a_correct_body_length_passes_the_guard() -> None:
    body = request_body(BINARY, monitoring_timer=0, command=0x0619, subcommand=0)
    built = THREE_E.build(
        route=Route.OWN_STATION, body=body, codec=BINARY, expect_body_len=len(body)
    )
    assert built == THREE_E.build(route=Route.OWN_STATION, body=body, codec=BINARY)


# ======================================================================================
# Guard 3 of 3: the property. len(build(...)) == prefix_units + len(body)
# ======================================================================================

CODECS: list[Codec] = [BINARY, ASCII]
FORMATS: list[FrameFormat] = [THREE_E, FOUR_E]


@given(
    payload=st.binary(min_size=0, max_size=600),
    timer=st.integers(min_value=0, max_value=0xFFFF),
    command=st.integers(min_value=0, max_value=0xFFFF),
    subcommand=st.integers(min_value=0, max_value=0xFFFF),
    serial=st.integers(min_value=0, max_value=0xFFFF),
    codec_index=st.integers(min_value=0, max_value=1),
    frame_index=st.integers(min_value=0, max_value=1),
)
def test_a_built_frame_is_exactly_its_prefix_plus_the_declared_length(
    payload: bytes,
    timer: int,
    command: int,
    subcommand: int,
    serial: int,
    codec_index: int,
    frame_index: int,
) -> None:
    """Over generated input, in both codings and both frame formats."""
    codec = CODECS[codec_index]
    frame = FORMATS[frame_index]
    # An ASCII payload is characters; the arithmetic is over wire units either way, so
    # a hex-safe rendering keeps the row legal without changing what is being asserted.
    data = payload if codec is BINARY else payload.hex().upper().encode("ascii")
    body = request_body(
        codec,
        monitoring_timer=timer,
        command=command,
        subcommand=subcommand,
        payload=data,
    )
    built = frame.build(
        route=Route.OWN_STATION,
        body=body,
        codec=codec,
        serial=serial if frame.carries_serial else None,
    )
    assert len(built) == frame.prefix_units(codec) + len(body)
    declared = frame.read_prefix(built, codec, response=False).declared
    assert declared == len(body)


@given(
    payload=st.binary(min_size=0, max_size=400),
    command=st.integers(min_value=0, max_value=0xFFFF),
    subcommand=st.integers(min_value=0, max_value=0xFFFF),
    frame_index=st.integers(min_value=0, max_value=1),
)
def test_a_request_survives_a_round_trip_through_the_wire(
    payload: bytes, command: int, subcommand: int, frame_index: int
) -> None:
    frame = FORMATS[frame_index]
    serial = 0x00AB if frame.carries_serial else None
    body = request_body(
        BINARY,
        monitoring_timer=0x0010,
        command=command,
        subcommand=subcommand,
        payload=payload,
    )
    built = frame.build(
        route=Route.OWN_STATION, body=body, codec=BINARY, serial=serial
    )
    parsed = frame.parse_request(built, BINARY)
    assert parsed.command == command
    assert parsed.subcommand == subcommand
    assert parsed.payload == payload
    assert parsed.serial == serial
    assert parsed.raw == built


def test_a_body_that_cannot_fit_the_length_field_raises_rather_than_wrapping() -> None:
    """65536 units would wrap to L = 0 and truncate the request into a shorter one."""
    body = request_body(BINARY, monitoring_timer=0, command=1, subcommand=0)
    body += b"\x00" * (0x10000 - len(body))
    with pytest.raises(SlmpCodecValueError):
        THREE_E.build(route=Route.OWN_STATION, body=body, codec=BINARY)


# ======================================================================================
# The fixed arithmetic, stated as a table
# ======================================================================================


@pytest.mark.parametrize(
    ("frame", "codec", "prefix", "data"),
    [
        (THREE_E, BINARY, 9, 11),
        (THREE_E, ASCII, 18, 22),
        (FOUR_E, BINARY, 13, 15),
        (FOUR_E, ASCII, 26, 30),
    ],
)
def test_prefix_and_data_offsets(
    frame: FrameFormat, codec: Codec, prefix: int, data: int
) -> None:
    """DESIGN section 4.2's table, which is also every parse offset in the manuals.

    3E binary: end code at 9, response data at 11. 4E binary: 13 and 15. ASCII: twice
    each. ``pymcprotocol``'s ``_get_answerstatus_index`` / ``_get_answerdata_index``
    agree independently (18/22 and 26/30).
    """
    assert frame.prefix_units(codec) == prefix
    assert frame.data_offset(codec) == data


def test_the_3e_request_subheader_is_50_00_not_a_little_endian_0x5000() -> None:
    """Day-one regression, named after ProtoForge ``mc/server.py:56``.

    It compares ``0x0054`` against a little-endian unpack of ``50 00`` and therefore
    rejects every correct client. Subheaders are literal byte sequences: this is the one
    field in binary coding that is not little-endian.
    """
    assert THREE_E.request.binary == b"\x50\x00"
    assert THREE_E.response.binary == b"\xd0\x00"
    assert FOUR_E.request.binary == b"\x54\x00"
    assert FOUR_E.response.binary == b"\xd4\x00"
    assert THREE_E.request.ascii == b"5000"
    assert FOUR_E.request.ascii == b"5400"


def test_the_4e_serial_is_not_a_byte_transformation_between_codings() -> None:
    """``0x1234`` is ``34 12`` in binary and ``"1234"`` in ASCII (S1 p.18 'Point').

    Hexlifying a binary frame to make an ASCII one reverses the serial, the module I/O
    number and every other multi-byte field.
    """
    binary = FOUR_E.subheader(BINARY, serial=0x1234, response=False)
    text = FOUR_E.subheader(ASCII, serial=0x1234, response=False)
    assert binary == b"\x54\x00\x34\x12\x00\x00"
    assert text == b"540012340000"
    assert binary.hex().upper().encode("ascii") != text


def test_frames_are_registered_by_their_public_enum_member() -> None:
    assert FRAMES == {FrameType.THREE_E: THREE_E, FrameType.FOUR_E: FOUR_E}
    assert not THREE_E.carries_serial
    assert FOUR_E.carries_serial
    assert repr(THREE_E) == "THREE_E"


def test_every_frame_format_cites_a_source() -> None:
    """DESIGN's single most important property: every builder cites its manual."""
    for frame in FRAMES.values():
        assert frame.cites
        for source in frame.cites:
            assert str(source).strip()


# ======================================================================================
# Refusals
# ======================================================================================


def test_a_foreign_subheader_is_refused_not_resynchronised() -> None:
    data = bytearray(wire(VECTOR_ROWS[1]))
    data[0] = 0xC0
    with pytest.raises(SlmpFrameFormatError, match="subheader"):
        THREE_E.parse(bytes(data), BINARY)


def test_a_3e_response_is_not_accepted_by_the_4e_parser() -> None:
    """The frame type is a connection-entry fact and is never inferred from the wire."""
    with pytest.raises(SlmpFrameFormatError):
        FOUR_E.parse(bytes.fromhex("D00000FFFF0300020000 00".replace(" ", "")), BINARY)


def test_an_unsolicited_ondemand_frame_never_parses_as_a_response() -> None:
    """SH(NA)-080956ENG-M section 5.11 p.204: command ``2101`` arrives unprompted.

    The PLC program pushes it; there is no request. A client that treats the next thing
    that arrives as its response is desynchronised for the life of the connection, and
    on 3E there is no serial number to notice with.
    """
    ondemand = THREE_E.build(
        route=Route.OWN_STATION,
        body=request_body(
            BINARY,
            monitoring_timer=0x0000,
            command=0x2101,
            subcommand=0x0000,
            payload=b"\x02\x00\x39\x30",
        ),
        codec=BINARY,
    )
    with pytest.raises(SlmpUnsolicitedFrameError, match="0x2101"):
        THREE_E.parse(ondemand, BINARY)


def test_a_short_frame_raises_and_is_never_completed_with_zeros() -> None:
    """``pymcprotocol`` ``type3e.py:148`` turns ``[111,222,333,444]`` into ``[111,222,0,0]``."""
    whole = bytes.fromhex("D00000FFFF03000800000095190212 3011".replace(" ", ""))
    with pytest.raises(SlmpShortFrameError, match="0x0008"):
        THREE_E.parse(whole[:-2], BINARY)


def test_an_empty_buffer_is_not_end_code_zero() -> None:
    with pytest.raises(SlmpShortFrameError):
        THREE_E.parse(b"", BINARY)


def test_trailing_bytes_are_refused_rather_than_attributed() -> None:
    """On UDP a datagram is one message; on TCP a surplus is the coalescing corruption."""
    whole = bytes.fromhex("D00000FFFF030002000000")
    with pytest.raises(SlmpTrailingDataError, match="surplus"):
        THREE_E.parse(whole + b"\xd0\x00", BINARY)


def test_an_abnormal_frame_too_short_for_its_error_block_raises() -> None:
    """``ErrorInfo`` is validated before it is believed.

    A frame that claims ``0xC056`` but declares ``L = 0x0004`` has no responding station
    and no command echo in it. Reading them out of whatever followed produces an error
    object naming a station nobody addressed, attached to a real end code.
    """
    truncated = THREE_E.build_response(
        route=Route.OWN_STATION,
        body=BINARY.number(0xC056, bits=16) + b"\x00\x00",
        codec=BINARY,
    )
    with pytest.raises(SlmpErrorInfoError, match="0xC056"):
        THREE_E.parse(truncated, BINARY)


def test_an_error_block_naming_an_undocumented_station_raises() -> None:
    """Validated before it is believed, field by field, not just by length.

    SH(NA)-080956ENG-M p.19 documents FFH, 01H-78H and the three special values for a
    station number. A response claiming to come from 7AH is a frame we have not
    understood, and believing it is how a reply from the wrong station becomes an answer.
    """
    frame = bytearray(
        bytes.fromhex("D00000FFFF03000B0056C000FFFF03000104 0000".replace(" ", ""))
    )
    frame[12] = 0x7A
    with pytest.raises(ValueError, match="station"):
        THREE_E.parse(bytes(frame), BINARY)


def test_command_defined_error_data_is_read_off_the_length_not_a_fixed_twenty() -> None:
    """All 16 measured abnormal frames were 20 bytes. 20 is an observation, not a rule.

    SH(NA)-080956ENG-M p.28 documents "response data when failed (when defined by
    command)", and the File group defines some. A parser that stopped at 20 would drop
    it silently.
    """
    trailer = b"\xde\xad\xbe\xef"
    data = THREE_E.build_response(
        route=Route.OWN_STATION,
        body=response_body(
            BINARY,
            end_code=0xC021,
            error_info=ErrorInfo(Route.OWN_STATION, 0x1810, 0x0000),
            extra_error_data=trailer,
        ),
        codec=BINARY,
    )
    parsed = THREE_E.parse(data, BINARY)
    assert parsed.length == 0x000B + len(trailer)
    assert parsed.extra_error_data == trailer
    assert len(data) == 20 + len(trailer)


def test_an_abnormal_end_code_with_no_error_block_cannot_be_constructed() -> None:
    with pytest.raises(SlmpErrorInfoError):
        RawResponse(
            frame=THREE_E,
            serial=None,
            route=Route.OWN_STATION,
            length=2,
            end_code=0xC056,
            payload=b"",
            error_info=None,
            extra_error_data=b"",
            raw=b"",
            subheader_tail=b"",
        )


def test_a_normal_end_code_with_an_error_block_cannot_be_constructed() -> None:
    with pytest.raises(SlmpFrameFormatError):
        RawResponse(
            frame=THREE_E,
            serial=None,
            route=Route.OWN_STATION,
            length=11,
            end_code=0x0000,
            payload=b"",
            error_info=ErrorInfo(Route.OWN_STATION, 0x0401, 0x0000),
            extra_error_data=b"",
            raw=b"",
            subheader_tail=b"",
        )


def test_a_response_body_refuses_to_mix_the_two_shapes() -> None:
    with pytest.raises(SlmpErrorInfoError):
        response_body(BINARY, end_code=0xC056)
    with pytest.raises(SlmpFrameFormatError):
        response_body(BINARY, end_code=0x0000, extra_error_data=b"\x00")


def test_an_impossible_declared_length_raises() -> None:
    """``L`` below the fields the format guarantees cannot be a frame."""
    frame = bytes.fromhex("D00000FFFF030001000000")
    with pytest.raises(SlmpFrameFormatError, match="impossible"):
        THREE_E.parse(frame, BINARY)


def test_a_request_declaring_zero_length_raises() -> None:
    """The bench's ``d3``: the FX5U answered ``0xC061`` echoing command ``0x0000``."""
    frame = bytes.fromhex("500000FFFF030000001000010400000000 00A80200".replace(" ", ""))
    with pytest.raises(SlmpFrameFormatError, match="impossible"):
        THREE_E.parse_request(frame, BINARY)


def test_a_serial_mismatch_raises_rather_than_returning_the_value() -> None:
    """The only in-band defence against the measured coalescing corruption."""
    reply = bytes.fromhex("D4003412000000FFFF03000600000000007042")
    assert FOUR_E.parse(reply, BINARY, expect_serial=0x1234).end_code == 0
    with pytest.raises(SlmpSerialMismatchError, match="0x1234"):
        FOUR_E.parse(reply, BINARY, expect_serial=0x1235)


def test_a_serial_cannot_be_asked_for_on_3e() -> None:
    """3E has no correlation field. That is a property of the format, not an option."""
    reply = bytes.fromhex("D00000FFFF030002000000")
    with pytest.raises(SlmpFrameFormatError, match="no serial"):
        THREE_E.parse(reply, BINARY, expect_serial=1)
    with pytest.raises(SlmpFrameFormatError, match="no serial"):
        THREE_E.build(
            route=Route.OWN_STATION,
            body=request_body(BINARY, monitoring_timer=0, command=1, subcommand=0),
            codec=BINARY,
            serial=1,
        )


def test_a_4e_frame_cannot_be_built_without_a_serial() -> None:
    with pytest.raises(SlmpFrameFormatError, match="requires a serial"):
        FOUR_E.build(
            route=Route.OWN_STATION,
            body=request_body(BINARY, monitoring_timer=0, command=1, subcommand=0),
            codec=BINARY,
        )


def test_an_ascii_frame_with_a_bad_nibble_raises_rather_than_reading_zero() -> None:
    """libslmp2 ``wordcodec.c:27`` returns 0 for a bad character. This does not."""
    text = bytearray(b"D00000FF03FF0000100000199512021130")
    text[15] = ord("Z")
    with pytest.raises(ValueError, match="Z"):
        THREE_E.parse(bytes(text), ASCII)
