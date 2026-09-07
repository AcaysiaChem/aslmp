"""The golden command corpus: Mitsubishi's own hex and the bench's, both directions.

``tests/vectors/command_vectors.jsonl`` is the independent oracle for ``aslmp.commands``.
Every ``request`` and ``response`` string in it was typed in from a named manual page or
a named bench log; none of it was produced by this library. If the corpus and the code
disagree, the corpus is right.

Four things are asserted, in descending order of how much they are worth.

**The exact bytes.** ``encode(ctx)`` must equal the request the manual prints, and
``decode(response)`` must equal the values it prints. That is the only check here that
an outsider could have written, and it is the one that catches a field-order swap, a
radix mistake or a word-order flip.

**``payload_len(ctx) == len(encode(ctx))``, for every row in every codec and every
frame.** The failure it guards is asymmetric and measured on FX5U-32MT/DS fw 1.065
(2026-09-06): an understated data length returns end code ``0xC061`` and the connection
recovers, while an **overstated** one gets no response at all -- the CPU blocks waiting
for bytes that never come and it looks exactly like a dead PLC.

**Round trips across every codec x frame combination.** A request goes through
``FrameFormat.build`` and back through ``parse_request``; a response is rebuilt from the
decoded values by :func:`render_response` -- a second, independent expression of each
command's response layout, written from the manual and not from the implementation --
and fed back through ``decode``. Both 3E and 4E, both codings, and every encoding the
row's profile allows.

**The citation is the test row.** Each row carries either a manual, a revision and a
section, or a CPU, a firmware and a date. ``test_every_row_carries_its_provenance``
makes "every frame builder cites its source" a property of data a Mitsubishi engineer
can check against the printed page, rather than a claim in a docstring.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import Any

import pytest

from aslmp.commands import (
    AccessWidth,
    BlockSpec,
    BlockWrite,
    ClearError,
    Command,
    EncodeContext,
    ExecuteMonitor,
    LockPassword,
    MonitorRegistration,
    RandomPoint,
    RandomWrite,
    ReadBits,
    ReadBlocks,
    ReadRandom,
    ReadTypeName,
    ReadWords,
    RegisterMonitor,
    RemoteLatchClear,
    RemotePause,
    RemoteReset,
    RemoteRun,
    RemoteStop,
    RunMode,
    SelfTest,
    UnlockPassword,
    WriteBits,
    WriteBlocks,
    WriteRandom,
    WriteRandomBits,
    WriteWords,
)
from aslmp.commands.random import BitWrite
from aslmp.profile import ClearMode, Encoding, Link
from aslmp.profiles import by_key
from aslmp.wire.codec import ASCII, BINARY, Codec, SpecFormat, Unit
from aslmp.wire.frames import FOUR_E, THREE_E, FrameFormat, request_body, response_body
from aslmp.wire.route import Route

VECTORS = Path(__file__).resolve().parents[1] / "vectors" / "command_vectors.jsonl"

BY_CODEC: dict[str, Codec] = {"binary": BINARY, "ascii": ASCII}
FRAMES: tuple[FrameFormat, ...] = (THREE_E, FOUR_E)


def load_vectors() -> tuple[dict[str, Any], ...]:
    rows: list[dict[str, Any]] = []
    for lineno, line in enumerate(VECTORS.read_text(encoding="ascii").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:  # pragma: no cover - corpus typo only
            raise AssertionError(f"{VECTORS}:{lineno} is not JSON: {exc}") from exc
    assert rows, f"{VECTORS} is empty"
    return tuple(rows)


ROWS = load_vectors()
IDS = [row["id"] for row in ROWS]


# ======================================================================================
# Building a command from a corpus row
# ======================================================================================


def _points(raw: list[list[Any]]) -> tuple[RandomPoint, ...]:
    return tuple(
        RandomPoint(address, AccessWidth(width), kind) for address, width, kind in raw
    )


def make_command(row: dict[str, Any]) -> Command[Any]:
    """The command a row describes. A plain switch, so the corpus stays declarative."""
    args = row["args"]
    name = row["cls"]
    if name == "ReadWords":
        return ReadWords(args["address"], args["count"])
    if name == "ReadBits":
        return ReadBits(args["address"], args["count"])
    if name == "WriteWords":
        return WriteWords(args["address"], tuple(args["values"]))
    if name == "WriteBits":
        return WriteBits(args["address"], tuple(bool(v) for v in args["values"]))
    if name == "ReadRandom":
        return ReadRandom(_points(args["points"]))
    if name == "WriteRandom":
        return WriteRandom(
            tuple(
                RandomWrite(RandomPoint(address, AccessWidth(width), kind), value)
                for address, width, kind, value in args["writes"]
            )
        )
    if name == "WriteRandomBits":
        return WriteRandomBits(
            tuple(BitWrite(address, bool(value)) for address, value in args["writes"])
        )
    if name == "ReadBlocks":
        return ReadBlocks(
            tuple(BlockSpec(address, points) for address, points in args["blocks"])
        )
    if name == "WriteBlocks":
        return WriteBlocks(
            tuple(BlockWrite(address, tuple(values)) for address, values in args["blocks"])
        )
    if name == "RegisterMonitor":
        return RegisterMonitor(_points(args["points"]))
    if name == "ExecuteMonitor":
        return ExecuteMonitor(
            MonitorRegistration(
                points=_points(args["points"]),
                profile_key=row["profile"],
                subcommand=row["subcommand"],
            )
        )
    if name == "ReadTypeName":
        return ReadTypeName()
    if name == "SelfTest":
        return SelfTest(bytes.fromhex(args["payload"]))
    if name == "ClearError":
        return ClearError()
    if name == "RemoteRun":
        return RemoteRun(RunMode[args["mode"]], ClearMode[args["clear"]])
    if name == "RemotePause":
        return RemotePause(RunMode[args["mode"]])
    if name == "RemoteStop":
        return RemoteStop(_fixed(args))
    if name == "RemoteLatchClear":
        return RemoteLatchClear(_fixed(args))
    if name == "RemoteReset":
        return RemoteReset(_fixed(args))
    if name == "UnlockPassword":
        return UnlockPassword(args["password"])
    if name == "LockPassword":
        return LockPassword(args["password"])
    raise AssertionError(f"the corpus names a class this test cannot build: {name!r}")


def _fixed(args: dict[str, Any]) -> bytes | None:
    raw = args.get("fixed_field")
    return None if raw is None else bytes.fromhex(raw)


def context(row: dict[str, Any], *, codec: Codec, encoding: Encoding) -> EncodeContext:
    return EncodeContext(
        codec=codec,
        spec=SpecFormat(row["spec"]),
        profile=by_key(row["profile"]),
        encoding=encoding,
        link=Link(row["link"]),
        allow_remote_control=True,
    )


def own_context(row: dict[str, Any]) -> EncodeContext:
    """The context the row's own bytes were recorded under."""
    return context(row, codec=BY_CODEC[row["codec"]], encoding=Encoding(row["encoding"]))


def encodings_for(row: dict[str, Any], codec: Codec) -> tuple[Encoding, ...]:
    allowed = by_key(row["profile"]).allowed_encodings
    if codec is BINARY:
        return (Encoding.BINARY,)
    return tuple(sorted((e for e in allowed if e.is_ascii), key=lambda e: e.value))


# ======================================================================================
# Point-budget gaps in the shipped profiles
#
# check_points() raises SlmpConfigurationError for a (command, encoding, unit, link) key
# the profile does not ship, and refuses to substitute a neighbouring one -- the right
# behaviour, and a real gap in the data of build unit U6. melsec:iq-r ships no ASCII
# limit for 0x0403 or 0x1402, so an ASCII sweep of the Read Random and Write Random
# rows cannot validate. The gap is SKIPPED and NAMED rather than swallowed, so that the
# day U6 fills it in these cases start running on their own.
# ======================================================================================

_LIMIT_UNITS: dict[str, tuple[int, Unit]] = {
    "ReadWords": (0x0401, Unit.WORD),
    "ReadBits": (0x0401, Unit.BIT),
    "WriteWords": (0x1401, Unit.WORD),
    "WriteBits": (0x1401, Unit.BIT),
    "ReadRandom": (0x0403, Unit.WORD),
    "WriteRandom": (0x1402, Unit.WORD),
    "WriteRandomBits": (0x1402, Unit.BIT),
    "ReadBlocks": (0x0406, Unit.WORD),
    "WriteBlocks": (0x1406, Unit.WORD),
    "RegisterMonitor": (0x0403, Unit.WORD),
}


def missing_limit(row: dict[str, Any], ctx: EncodeContext) -> str | None:
    """The limit key this row's command needs and its profile does not ship, if any."""
    needed = _LIMIT_UNITS.get(row["cls"])
    if needed is None:
        return None
    command, unit = needed
    key = (command, ctx.encoding, unit, ctx.link)
    if key in ctx.profile.limits:
        return None
    return (
        f"{ctx.profile.key} ships no points-per-request limit for 0x{command:04X}/"
        f"{ctx.encoding.value}/{unit.value}/{ctx.link.value}, so this combination "
        f"cannot be validated. That is a gap in the profile data of build unit U6, not "
        f"a property of the command."
    )


def validated(row: dict[str, Any], ctx: EncodeContext) -> Command[Any]:
    """The row's command, validated -- or a skip naming the profile data gap."""
    command = make_command(row)
    gap = missing_limit(row, ctx)
    if gap is not None:
        pytest.skip(gap)
    command.validate(ctx)
    return command


# ======================================================================================
# An independent expression of each command's RESPONSE layout
# ======================================================================================


def _word_value(kind: str, value: Any) -> int:
    """One 16-bit response word, from the decoded value the corpus records."""
    if kind == "bits":
        out = 0
        for index, bit in enumerate(value):
            if bit:
                out |= 1 << index
        return out
    if kind == "i16":
        return int(value) & 0xFFFF
    return int(value)


def _dword_value(kind: str, value: Any) -> int:
    if kind == "f32":
        return int(struct.unpack("<I", struct.pack("<f", float(value)))[0])
    if kind == "i32":
        return int(value) & 0xFFFFFFFF
    return int(value)


def render_response(row: dict[str, Any], command: Command[Any], ctx: EncodeContext) -> bytes:
    """Rebuild this row's response data from its decoded values, per the manual.

    Written from the printed response layouts, not from ``aslmp.commands``: a word read
    is one 16-bit field per point, a bit read is one nibble (binary) or one character
    (ASCII) per point, a Read Random response is the word section then the double-word
    section, a block read is each block's own point count of words, and a write has no
    response data at all.
    """
    kind = row["decoded_kind"]
    decoded = row["decoded"]
    codec = ctx.codec
    if kind == "none" or kind == "registration":
        return b""
    if kind == "words":
        return codec.words([int(value) for value in decoded])
    if kind == "bits":
        return codec.bits([bool(value) for value in decoded])
    if kind == "echo":
        payload = bytes.fromhex(decoded)
        return codec.number(len(payload), bits=16) + payload
    if kind == "typename":
        name: str = decoded["model"]
        padded = name.ljust(16).encode("ascii")
        return padded + codec.number(int(decoded["model_code"]), bits=16)
    if kind == "blocks":
        flat: list[int] = []
        for block in decoded:
            flat.extend(int(value) for value in block)
        return codec.words(flat)
    if kind == "random":
        points = _command_points(command)
        head: list[bytes] = []
        tail: list[bytes] = []
        for point, value in zip(points, decoded, strict=True):
            if point.width is AccessWidth.WORD:
                head.append(codec.number(_word_value(point.kind, value), bits=16))
            else:
                tail.append(codec.number(_dword_value(point.kind, value), bits=32))
        return b"".join(head) + b"".join(tail)
    raise AssertionError(f"unknown decoded_kind {kind!r} in row {row['id']!r}")


def _command_points(command: Command[Any]) -> tuple[RandomPoint, ...]:
    if isinstance(command, ReadRandom):
        return command.points
    if isinstance(command, ExecuteMonitor):
        return command.registration.points
    raise AssertionError(f"{type(command).__name__} has no random points")


def normalise(value: Any) -> Any:
    """JSON lists become tuples so a decoded tuple compares equal to a corpus row."""
    if isinstance(value, list):
        return tuple(normalise(item) for item in value)
    return value


def expected_value(row: dict[str, Any]) -> Any:
    kind = row["decoded_kind"]
    decoded = row["decoded"]
    if kind == "none" or kind == "registration":
        return None
    if kind == "echo":
        return bytes.fromhex(decoded)
    if kind == "typename":
        return decoded
    return normalise(decoded)


def check_decoded(row: dict[str, Any], got: Any) -> None:
    kind = row["decoded_kind"]
    if kind == "registration":
        assert isinstance(got, MonitorRegistration)
        return
    if kind == "typename":
        assert got.model == row["decoded"]["model"]
        assert got.model_code == row["decoded"]["model_code"]
        return
    assert got == expected_value(row)


# ======================================================================================
# The corpus, in its own coding
# ======================================================================================


@pytest.mark.parametrize("row", ROWS, ids=IDS)
def test_request_bytes_match_the_source(row: dict[str, Any]) -> None:
    """``encode(ctx)`` is the hex the manual prints, or the bench captured.

    Deliberately does NOT call ``validate()`` first: the exact-bytes assertion is the
    one an outsider could have written, and it must not be skipped because a profile is
    missing a point-budget row. ``test_validate_accepts_every_row`` covers validation.
    """
    ctx = own_context(row)
    command = make_command(row)
    assert command.encode(ctx).hex() == row["request"].lower(), (
        f"{row['id']}: {row['meaning']}"
    )


@pytest.mark.parametrize("row", ROWS, ids=IDS)
def test_validate_accepts_every_row(row: dict[str, Any]) -> None:
    """Nothing in the corpus is refused by its own command's pre-transport checks."""
    ctx = own_context(row)
    validated(row, ctx)


@pytest.mark.parametrize("row", ROWS, ids=IDS)
def test_subcommand_matches_the_source(row: dict[str, Any]) -> None:
    """The subcommand is derived from unit and spec, and must equal the printed one."""
    ctx = own_context(row)
    assert make_command(row).subcommand(ctx) == row["subcommand"]


@pytest.mark.parametrize("row", ROWS, ids=IDS)
def test_command_code_matches_the_source(row: dict[str, Any]) -> None:
    assert row["command"] == make_command(row).CODE


@pytest.mark.parametrize("row", ROWS, ids=IDS)
def test_response_decodes_to_the_recorded_value(row: dict[str, Any]) -> None:
    """``decode(bytes)`` is the meaning the source prints. The other direction."""
    if row["response"] is None:
        pytest.skip(f"{row['id']} records no response: {row['note']}")
    ctx = own_context(row)
    command = make_command(row)
    check_decoded(row, command.decode(bytes.fromhex(row["response"]), ctx))


@pytest.mark.parametrize("row", ROWS, ids=IDS)
def test_response_renderer_reproduces_the_recorded_bytes(row: dict[str, Any]) -> None:
    """The test-side response builder agrees with the source's own hex.

    This is what makes the cross-codec sweep below worth anything: if
    :func:`render_response` were wrong, the sweep would be checking the implementation
    against itself. Here it is checked against the printed page.
    """
    if row["response"] is None or row["decoded_kind"] == "registration":
        pytest.skip(f"{row['id']} records no decodable response")
    ctx = own_context(row)
    rendered = render_response(row, make_command(row), ctx)
    assert rendered.hex() == row["response"].lower()


# ======================================================================================
# Every codec x frame combination
# ======================================================================================


def sweep() -> list[tuple[dict[str, Any], Codec, Encoding, FrameFormat]]:
    cases: list[tuple[dict[str, Any], Codec, Encoding, FrameFormat]] = []
    for row in ROWS:
        for codec in (BINARY, ASCII):
            for encoding in encodings_for(row, codec):
                for frame in FRAMES:
                    cases.append((row, codec, encoding, frame))
    return cases


SWEEP = sweep()
SWEEP_IDS = [
    f"{row['id']}-{codec.name}-{encoding.value}-{frame.frame_type.value}"
    for row, codec, encoding, frame in SWEEP
]


@pytest.mark.parametrize(("row", "codec", "encoding", "frame"), SWEEP, ids=SWEEP_IDS)
def test_payload_len_agrees_with_encode(
    row: dict[str, Any], codec: Codec, encoding: Encoding, frame: FrameFormat
) -> None:
    """The invariant that guards the overstate-hangs asymmetry, in every combination."""
    del frame
    ctx = context(row, codec=codec, encoding=encoding)
    command = validated(row, ctx)
    payload = command.encode(ctx)
    assert command.payload_len(ctx) == len(payload)
    assert command.checked_encode(ctx) == payload


@pytest.mark.parametrize(("row", "codec", "encoding", "frame"), SWEEP, ids=SWEEP_IDS)
def test_request_round_trips_through_the_frame(
    row: dict[str, Any], codec: Codec, encoding: Encoding, frame: FrameFormat
) -> None:
    """Build the whole request frame and parse it back: command, subcommand, payload."""
    ctx = context(row, codec=codec, encoding=encoding)
    command = validated(row, ctx)
    payload = command.encode(ctx)
    serial = 0x1234 if frame.carries_serial else None
    body = request_body(
        codec,
        monitoring_timer=0x0000,
        command=command.CODE,
        subcommand=command.subcommand(ctx),
        payload=payload,
    )
    built = frame.build(
        route=Route.OWN_STATION,
        body=body,
        codec=codec,
        serial=serial,
        expect_body_len=command.body_len(ctx),
    )
    parsed = frame.parse_request(built, codec)
    assert parsed.command == command.CODE
    assert parsed.subcommand == command.subcommand(ctx)
    assert parsed.payload == payload
    assert parsed.serial == serial
    assert len(built) == frame.prefix_units(codec) + len(body)


@pytest.mark.parametrize(("row", "codec", "encoding", "frame"), SWEEP, ids=SWEEP_IDS)
def test_response_round_trips_through_the_frame(
    row: dict[str, Any], codec: Codec, encoding: Encoding, frame: FrameFormat
) -> None:
    """Render the response from the decoded values, frame it, parse it, decode it."""
    if row["response"] is None:
        pytest.skip(f"{row['id']} records no response")
    ctx = context(row, codec=codec, encoding=encoding)
    command = validated(row, ctx)
    rendered = render_response(row, command, ctx)
    serial = 0x1234 if frame.carries_serial else None
    built = frame.build_response(
        route=Route.OWN_STATION,
        body=response_body(codec, end_code=0x0000, payload=rendered),
        codec=codec,
        serial=serial,
    )
    parsed = frame.parse(built, codec, expect_serial=serial)
    assert parsed.ok
    check_decoded(row, command.decode(parsed.payload, ctx))


# ======================================================================================
# Provenance
# ======================================================================================


@pytest.mark.parametrize("row", ROWS, ids=IDS)
def test_every_row_carries_its_provenance(row: dict[str, Any]) -> None:
    """A manual, a revision and a section -- or a CPU, a firmware and a date."""
    provenance = row["provenance"]
    assert provenance in {"manual", "live", "inferred"}
    if provenance == "live":
        for field in ("cpu", "firmware", "measured"):
            assert row.get(field), f"{row['id']} is LIVE but carries no {field}"
    else:
        for field in ("manual", "revision", "section"):
            assert row.get(field), f"{row['id']} is {provenance} but has no {field}"
    assert row["meaning"].strip(), f"{row['id']} says nothing about what it means"


def test_the_corpus_covers_every_registered_command() -> None:
    """A command with no golden vector is a command nobody has checked.

    ``0x2101`` Ondemand is excluded: it is PLC-originated, no client class implements
    it, and ``tests/unit/test_commands_ondemand.py`` covers the receive path.
    """
    from aslmp.commands.registry import COMMANDS

    covered = {row["command"] for row in ROWS}
    missing = sorted(
        code
        for code, spec in COMMANDS.items()
        if spec.direction == "request" and code not in covered
    )
    assert not missing, (
        "no golden vector for "
        + ", ".join(f"0x{code:04X}" for code in missing)
        + ". Every command this package can send must have at least one row typed in "
        "from a manual page or a bench log."
    )


def test_the_corpus_exercises_both_codings() -> None:
    """The field-order swap only shows up if both codings are in the corpus."""
    codings = {row["codec"] for row in ROWS}
    assert codings == {"binary", "ascii"}


def test_the_corpus_carries_hardware_rows() -> None:
    """Measured rows are the ones that outrank the manuals where they disagree."""
    live = [row for row in ROWS if row["provenance"] == "live"]
    assert len(live) >= 8
    for row in live:
        assert row["cpu"] == "FX5U-32MT/DS"
        assert row["firmware"] == "1.065"
