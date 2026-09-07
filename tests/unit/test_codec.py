"""The two codecs, against Mitsubishi's own hex and against the bench.

Three kinds of check live here, in descending order of how much they are worth.

**Golden byte vectors** (``tests/vectors/codec_vectors.jsonl``) are the independent
oracle: every expected byte string in that file was typed in from a named manual page or
a named bench log, not produced by this library. If the codec and the corpus disagree,
the corpus is right. Each row carries its own source, so "every frame builder cites its
manual section" is enforced by data a Mitsubishi engineer can check against the printed
page rather than by a comment.

**Properties** (hypothesis) hold the seven invariants of DESIGN section 4.1 over
generated input -- in particular that a 32-bit field is ``struct.pack("<I", v)`` in
binary and ``f"{v:08X}"`` in ASCII, which is the single place the dword word-order flip
is expressed, and that ASCII bit lengths are ``2 x binary - (n % 2)`` rather than
``2 x binary``.

**Refusals.** A codec that returns 0 for a bad nibble, truncates a short buffer or masks
an over-wide value produces plausible wrong data with end code 0x0000 and nothing to
catch it. Every one of those is asserted to raise.
"""

from __future__ import annotations

import json
import math
import struct
from pathlib import Path
from typing import Any, Literal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from aslmp.data import read_table
from aslmp.wire.codec import (
    ASCII,
    BINARY,
    CODECS,
    LOOPBACK_MAX_BYTES,
    Codec,
    Notation,
    SlmpCodecValueError,
    SlmpHexDigitError,
    SlmpShortBufferError,
    SpecFormat,
    Unit,
    parse_ascii_device_code,
)
from aslmp.wire.devicetable import DEVICE_TABLE

VECTORS = Path(__file__).resolve().parents[1] / "vectors" / "codec_vectors.jsonl"

BY_NAME: dict[str, Codec] = {"binary": BINARY, "ascii": ASCII}
SPECS: dict[str, SpecFormat] = {"short": SpecFormat.SHORT, "long": SpecFormat.LONG}

Bits = Literal[8, 16, 32]
WIDTHS: tuple[Bits, ...] = (8, 16, 32)


def load_vectors() -> tuple[dict[str, Any], ...]:
    lines = VECTORS.read_text(encoding="ascii").splitlines()
    rows: list[dict[str, Any]] = []
    for lineno, line in enumerate(lines, start=1):
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


def parametrize_vectors(op: str) -> pytest.MarkDecorator:
    rows = [r for r in VECTOR_ROWS if r["op"] == op]
    if not rows:  # pragma: no cover - a corpus that lost a whole op is a bug
        raise AssertionError(f"no {op!r} vectors in {VECTORS}")
    return pytest.mark.parametrize("row", rows, ids=[vector_id(r) for r in rows])


def hex_bytes(text: str) -> bytes:
    return bytes.fromhex(text)


# ==========================================================================================
# The corpus itself: shape and provenance
# ==========================================================================================


def test_the_corpus_is_not_empty_and_has_unique_ids() -> None:
    ids = [vector_id(row) for row in VECTOR_ROWS]
    assert len(ids) >= 30, "the codec corpus has shrunk; vectors are never deleted quietly"
    duplicates = sorted({name for name in ids if ids.count(name) > 1})
    assert not duplicates, f"duplicate vector ids: {duplicates}"


@pytest.mark.parametrize("row", VECTOR_ROWS, ids=[vector_id(r) for r in VECTOR_ROWS])
def test_every_vector_carries_a_checkable_source(row: dict[str, Any]) -> None:
    """A vector with no source is folklore with a test around it.

    Either a manual, its revision and a section a reader can open, or a CPU model, a
    firmware version and a date. Nothing else counts.
    """
    provenance = row.get("provenance")
    assert provenance in {"manual", "live"}, (
        f"{row['id']}: provenance must be 'manual' or 'live', got {provenance!r}"
    )
    if provenance == "live":
        for field in ("cpu", "firmware", "measured"):
            assert row.get(field), (
                f"{row['id']}: a measured vector must name {field}. Everything we know "
                f"about this hardware is n=1: one CPU, one firmware, one afternoon."
            )
    else:
        for field in ("manual", "revision", "section"):
            assert row.get(field), f"{row['id']}: a manual vector must name {field}"
    assert row.get("meaning"), f"{row['id']}: say what the bytes mean"


def test_every_cited_manual_is_one_we_actually_read() -> None:
    """A citation may only name a manual in ``data/manuals.tsv``, at its read revision."""
    manuals = read_table("manuals")
    known = {(r["manual"], r["revision"]) for r in manuals if r["status"] == "read"}
    for row in VECTOR_ROWS:
        if row.get("provenance") != "manual":
            continue
        pair = (row["manual"], row["revision"])
        assert pair in known, (
            f"{row['id']} cites {pair}, which is not a manual/revision recorded as read "
            f"in data/manuals.tsv"
        )


def test_the_measured_vectors_name_the_one_cpu_we_have() -> None:
    for row in VECTOR_ROWS:
        if row.get("provenance") != "live":
            continue
        assert row["cpu"] == "FX5U-32MT/DS", row["id"]
        assert row["firmware"] == "1.065", row["id"]


# ==========================================================================================
# The golden vectors
# ==========================================================================================


@parametrize_vectors("number")
def test_vector_number(row: dict[str, Any]) -> None:
    codec = BY_NAME[row["codec"]]
    expected = hex_bytes(row["hex"])
    assert codec.number(row["value"], bits=row["bits"]) == expected, row["meaning"]
    assert codec.read_number(expected, 0, bits=row["bits"]) == row["value"]
    assert codec.number_len(row["bits"]) == len(expected)


@parametrize_vectors("words")
def test_vector_words(row: dict[str, Any]) -> None:
    codec = BY_NAME[row["codec"]]
    expected = hex_bytes(row["hex"])
    values = tuple(row["values"])
    assert codec.words(values) == expected, row["meaning"]
    assert codec.read_words(expected, 0, len(values)) == values
    assert codec.word_data_len(len(values)) == len(expected)


@parametrize_vectors("bits")
def test_vector_bits(row: dict[str, Any]) -> None:
    codec = BY_NAME[row["codec"]]
    expected = hex_bytes(row["hex"])
    values = tuple(bool(v) for v in row["values"])
    assert codec.bits(values) == expected, row["meaning"]
    assert codec.read_bits(expected, 0, len(values)) == values
    assert codec.bit_data_len(len(values)) == len(expected)


@parametrize_vectors("bit_data_len")
def test_vector_bit_data_len(row: dict[str, Any]) -> None:
    codec = BY_NAME[row["codec"]]
    assert codec.bit_data_len(row["count"]) == row["length"], row["meaning"]


@parametrize_vectors("device_code")
def test_vector_device_code(row: dict[str, Any]) -> None:
    codec = BY_NAME[row["codec"]]
    device = DEVICE_TABLE[row["device"]]
    rendered = codec.device_code(device, SPECS[row["spec"]])
    assert rendered == hex_bytes(row["hex"]), row["meaning"]


@parametrize_vectors("device_number")
def test_vector_device_number(row: dict[str, Any]) -> None:
    codec = BY_NAME[row["codec"]]
    spec = SPECS[row["spec"]]
    expected = hex_bytes(row["hex"])
    rendered = codec.device_number(row["wire_value"], spec, base=row["base"])
    assert rendered == expected, row["meaning"]
    assert codec.device_number_len(spec) == len(expected)


@parametrize_vectors("loopback")
def test_vector_loopback(row: dict[str, Any]) -> None:
    codec = BY_NAME[row["codec"]]
    payload = hex_bytes(row["payload_hex"])
    assert codec.loopback_payload_ok(payload) is row["ok"], row["meaning"]


# ==========================================================================================
# The manual's own bit vector, asserted exactly
# ==========================================================================================

M100_TO_M107 = (False, False, False, True, False, False, True, True)


def test_binary_bit_packing_is_the_manuals_own_vector() -> None:
    """SH(NA)-080956ENG-M p.47: 0401 bit read of M100-M107 answers ``00 01 00 11``.

    Nibbles ``0,0 / 0,1 / 0,0 / 1,1``: the FIRST point is the HIGH nibble. Reverse the
    packing order and this response decodes to a different eight relays, with end code
    0x0000 and nothing anywhere to say so.
    """
    assert BINARY.bits(M100_TO_M107) == b"\x00\x01\x00\x11"
    assert BINARY.read_bits(b"\x00\x01\x00\x11", 0, 8) == M100_TO_M107


def test_the_same_eight_points_in_ascii_are_eight_characters() -> None:
    """SH(NA)-080956ENG-M p.40: one character per point, 31H ON / 30H OFF."""
    assert ASCII.bits(M100_TO_M107) == b"00010011"
    assert ASCII.bits(M100_TO_M107) == bytes([0x30, 0x30, 0x30, 0x31, 0x30, 0x30, 0x31, 0x31])
    assert ASCII.read_bits(b"00010011", 0, 8) == M100_TO_M107


def test_five_points_from_m10_break_the_two_times_rule() -> None:
    """SH(NA)-080956ENG-M p.40: ``10 10 10`` binary, ``"10101"`` ASCII. 5 != 2 x 3."""
    five = (True, False, True, False, True)
    assert BINARY.bits(five) == b"\x10\x10\x10"
    assert ASCII.bits(five) == b"10101"
    assert BINARY.bit_data_len(5) == 3
    assert ASCII.bit_data_len(5) == 5
    assert ASCII.bit_data_len(5) != 2 * BINARY.bit_data_len(5)


# ==========================================================================================
# DESIGN section 4.1, invariant by invariant
# ==========================================================================================


@given(
    value=st.integers(min_value=0, max_value=0xFFFFFFFF),
    bits=st.sampled_from(WIDTHS),
)
def test_invariant_1_field_length_is_octets_times_width(value: int, bits: Bits) -> None:
    """``len(number(v, bits=n)) == (n // 8) * width`` for both codecs."""
    for codec in CODECS:
        masked = value & ((1 << bits) - 1)
        encoded = codec.number(masked, bits=bits)
        assert len(encoded) == (bits // 8) * codec.width
        assert len(encoded) == codec.number_len(bits)


@given(value=st.integers(min_value=0, max_value=0xFFFFFFFF))
def test_invariant_2_the_32_bit_field_is_where_the_word_order_flip_lives(value: int) -> None:
    """``BINARY.number(v, 32) == struct.pack("<I", v)``; ASCII is ``f"{v:08X}"``.

    Low word first in binary, high word first in ASCII, from the same value and the same
    call. Nothing else in the library needs a word-order branch.
    """
    assert BINARY.number(value, bits=32) == struct.pack("<I", value)
    assert ASCII.number(value, bits=32) == f"{value:08X}".encode("ascii")

    binary = BINARY.number(value, bits=32)
    low = int.from_bytes(binary[:2], "little")
    high = int.from_bytes(binary[2:], "little")
    assert low == value & 0xFFFF
    assert high == value >> 16
    assert ASCII.number(value, bits=32)[:4] == f"{high:04X}".encode("ascii")


@given(value=st.integers(min_value=0, max_value=0xFFFFFFFF))
def test_f32_has_exactly_one_implementation(value: int) -> None:
    """``struct.unpack("<f", struct.pack("<I", v))[0]`` and nothing else.

    Measured four ways on FX5U-32MT/DS fw 1.065 (2026-09-06): one 0403 double-word
    access point IS one IEEE-754 f32, low word first.
    """
    expected = struct.unpack("<f", struct.pack("<I", value))[0]
    from_codec = struct.unpack("<f", BINARY.number(value, bits=32))[0]
    if math.isnan(expected):
        assert math.isnan(from_codec)
    else:
        assert from_codec == expected


def test_f32_from_the_bench() -> None:
    """The four measured floats, through the number field and nothing else."""
    for raw, value in ((0x449A5000, 1234.5), (0x42718000, 60.375), (0x42700000, 60.0)):
        assert struct.unpack("<f", BINARY.number(raw, bits=32))[0] == value
    # D110 = 0xE6B8, D111 = 0xC7C0 read back as one dword point.
    words = BINARY.words((0xE6B8, 0xC7C0))
    assert struct.unpack("<f", words)[0] == -98765.4375
    assert BINARY.read_number(words, 0, bits=32) == 0xC7C0E6B8


@given(count=st.integers(min_value=0, max_value=4000))
def test_invariant_3_bit_lengths_are_per_codec(count: int) -> None:
    """``ascii_len(n) == 2 * binary_len(n) - (n % 2)``, which is why nothing doubles."""
    assert BINARY.bit_data_len(count) == (count + 1) // 2
    assert ASCII.bit_data_len(count) == count
    assert ASCII.bit_data_len(count) == 2 * BINARY.bit_data_len(count) - (count % 2)


@given(values=st.lists(st.booleans(), min_size=0, max_size=64))
def test_invariant_4_bit_packing_round_trips_in_both_codecs(values: list[bool]) -> None:
    """High nibble first, odd counts padded low, and decode inverts encode."""
    wanted = tuple(values)
    for codec in CODECS:
        packed = codec.bits(wanted)
        assert len(packed) == codec.bit_data_len(len(wanted))
        assert codec.read_bits(packed, 0, len(wanted)) == wanted
    if values:
        assert (BINARY.bits(wanted)[0] >> 4) == int(values[0]), "first point, high nibble"


@given(values=st.lists(st.integers(min_value=0, max_value=0xFFFF), min_size=0, max_size=64))
def test_word_data_round_trips_in_both_codecs(values: list[int]) -> None:
    wanted = tuple(values)
    for codec in CODECS:
        packed = codec.words(wanted)
        assert len(packed) == codec.word_data_len(len(wanted))
        assert codec.read_words(packed, 0, len(wanted)) == wanted


@given(
    value=st.integers(min_value=0, max_value=0xFFFFFFFF),
    bits=st.sampled_from(WIDTHS),
    pad=st.integers(min_value=0, max_value=5),
)
def test_numbers_round_trip_at_a_non_zero_offset(value: int, bits: Bits, pad: int) -> None:
    """Fields are read at an offset inside a larger frame, not at 0."""
    masked = value & ((1 << bits) - 1)
    for codec in CODECS:
        buf = b"\x5a" * pad + codec.number(masked, bits=bits) + b"\x5a" * pad
        assert codec.read_number(buf, pad, bits=bits) == masked


def test_invariant_5_ascii_device_codes_are_mnemonics_not_hex() -> None:
    """``D`` is ``b"D*"``, never ``b"A8"``. Padding is ``*`` (2AH)."""
    assert ASCII.device_code(DEVICE_TABLE["D"], SpecFormat.SHORT) == b"D*"
    assert ASCII.device_code(DEVICE_TABLE["D"], SpecFormat.LONG) == b"D***"
    assert ASCII.device_code(DEVICE_TABLE["TN"], SpecFormat.SHORT) == b"TN"
    assert ASCII.device_code(DEVICE_TABLE["STS"], SpecFormat.SHORT) == b"SS"
    assert ASCII.device_code(DEVICE_TABLE["X"], SpecFormat.LONG) == b"X***"
    assert BINARY.device_code(DEVICE_TABLE["D"], SpecFormat.SHORT) == b"\xa8"
    for device in DEVICE_TABLE.values():
        if device.code_short is None:
            continue
        rendered = ASCII.device_code(device, SpecFormat.SHORT)
        assert rendered != f"{device.code_short:02X}".encode("ascii"), device.name


def test_padding_is_a_star_on_emit_and_star_or_space_on_parse() -> None:
    """SH(NA)-080956ENG-M p.38 footnote 1 permits a space instead of ``*``."""
    assert ASCII.device_code(DEVICE_TABLE["D"], SpecFormat.LONG) == b"D***"
    assert parse_ascii_device_code(b"D***") == "D"
    assert parse_ascii_device_code(b"D   ") == "D"
    assert parse_ascii_device_code(b"D*") == "D"
    assert parse_ascii_device_code(b"D ") == "D"
    assert parse_ascii_device_code(b"STS*") == "STS"
    assert parse_ascii_device_code(b"SS") == "SS"
    assert parse_ascii_device_code(b"D*  ") == "D"
    assert parse_ascii_device_code(b"D  *") == "D"
    assert parse_ascii_device_code(b"D0") == "D0", "'0' is a mnemonic character, not padding"


@pytest.mark.parametrize("field", [b"D", b"D****", b"", b"d*", b"D-**", b"****", b"    "])
def test_a_malformed_device_code_field_raises(field: bytes) -> None:
    with pytest.raises(SlmpCodecValueError):
        parse_ascii_device_code(field)


@pytest.mark.parametrize(
    "field",
    [b"00G8", b"00a8", b"    ", b"00 8", b"0x18", b"\x00\x00\x00\x00"],
)
def test_invariant_6_ascii_read_number_raises_on_a_bad_nibble(field: bytes) -> None:
    """It never returns 0. ``int(b"00G8", 16)`` would raise naming nothing."""
    with pytest.raises(SlmpHexDigitError) as info:
        ASCII.read_number(field, 0, bits=16)
    assert "0-9" in str(info.value)


def test_lower_case_hex_is_refused_rather_than_normalised() -> None:
    """A-ASCII-CASE: we emit upper case and refuse anything else on the way in."""
    assert ASCII.number(0x00A8, bits=16) == b"00A8"
    with pytest.raises(SlmpHexDigitError) as info:
        ASCII.read_number(b"00a8", 0, bits=16)
    assert "capitalis" in str(info.value) or "capitaliz" in str(info.value)


def test_a_bad_nibble_names_its_offset_in_the_buffer() -> None:
    with pytest.raises(SlmpHexDigitError) as info:
        ASCII.read_number(b"0000" + b"00G8", 4, bits=16)
    assert "offset 6" in str(info.value)


@pytest.mark.parametrize(
    ("payload", "ok"),
    [
        (b"ABCD", True),
        (b"0619" + b"BEEF", True),
        (b"0", True),
        (b"0" * LOOPBACK_MAX_BYTES, True),
        (b"", False),
        (b"0" * (LOOPBACK_MAX_BYTES + 1), False),
        (b"ASLMP0", False),
        (b"ASLMPACE", False),
        (b"ASLMP000", False),
        (b"abcd", False),
        (b"AB CD", False),
        (b"\x00\x01", False),
    ],
)
def test_invariant_7_loopback_payload_rule(payload: bytes, ok: bool) -> None:
    """1-960 bytes of ``[0-9A-F]``, identical in both codings.

    Three of the candidate architectures shipped ``ASLMP0`` / ``ASLMPACE`` /
    ``ASLMP000`` as handshake defaults, all of which both manuals forbid.
    """
    for codec in CODECS:
        assert codec.loopback_payload_ok(payload) is ok


def test_every_default_payload_this_library_ships_passes_the_rule() -> None:
    """``self_test()``'s ``b"ABCD"`` and the handshake's ``b"0619" + 4 hex digits``."""
    assert BINARY.loopback_payload_ok(b"ABCD")
    for nonce in (0x0000, 0x1234, 0xFFFF, 0xBEEF):
        assert BINARY.loopback_payload_ok(b"0619" + f"{nonce:04X}".encode("ascii"))


# ==========================================================================================
# The singletons and their identities
# ==========================================================================================


def test_the_codecs_report_their_own_name_and_width() -> None:
    assert BINARY.name == "binary"
    assert BINARY.width == 1
    assert ASCII.name == "ascii"
    assert ASCII.width == 2
    assert repr(BINARY) == "BINARY"
    assert repr(ASCII) == "ASCII"


def test_both_singletons_satisfy_the_protocol_at_runtime() -> None:
    """``CODECS`` is annotated ``tuple[Codec, ...]``, so mypy checks this statically too."""
    for codec in CODECS:
        for name in (
            "number",
            "read_number",
            "number_len",
            "device_code",
            "device_code_len",
            "device_number",
            "device_number_len",
            "words",
            "read_words",
            "word_data_len",
            "bits",
            "read_bits",
            "bit_data_len",
            "loopback_payload_ok",
        ):
            assert callable(getattr(codec, name)), f"{codec.name}.{name}"


def test_field_widths_match_the_manuals_table() -> None:
    """SH(NA)-080956ENG-M pp.35-38, the specification-format width table."""
    assert BINARY.device_code_len(SpecFormat.SHORT) == 1
    assert BINARY.device_code_len(SpecFormat.LONG) == 2
    assert BINARY.device_number_len(SpecFormat.SHORT) == 3
    assert BINARY.device_number_len(SpecFormat.LONG) == 4
    assert ASCII.device_code_len(SpecFormat.SHORT) == 2
    assert ASCII.device_code_len(SpecFormat.LONG) == 4
    assert ASCII.device_number_len(SpecFormat.SHORT) == 6
    assert ASCII.device_number_len(SpecFormat.LONG) == 8


def test_enums_carry_the_members_the_rest_of_the_package_imports() -> None:
    assert {member.value for member in SpecFormat} == {"short", "long"}
    assert {member.value for member in Unit} == {"bit", "word"}
    assert {member.value for member in Notation} == {"value", "octal-digits"}


# ==========================================================================================
# Refusals. Nothing masks, clamps, truncates or substitutes.
# ==========================================================================================


@pytest.mark.parametrize("codec", CODECS, ids=lambda c: c.name)
@pytest.mark.parametrize(("value", "bits"), [(-1, 16), (0x10000, 16), (256, 8), (1 << 32, 32)])
def test_a_number_that_does_not_fit_its_field_raises(
    codec: Codec, value: int, bits: Bits
) -> None:
    with pytest.raises(SlmpCodecValueError) as info:
        codec.number(value, bits=bits)
    assert "mask" in str(info.value) or "fit" in str(info.value)


@pytest.mark.parametrize("codec", CODECS, ids=lambda c: c.name)
def test_an_unsupported_field_width_raises(codec: Codec) -> None:
    with pytest.raises(SlmpCodecValueError):
        # bits is Literal[8, 16, 32]; the ignore exercises the RUNTIME guard, which
        # exists because untyped callers reach these methods through the protocol.
        codec.number(0, bits=24)  # type: ignore[arg-type]
    with pytest.raises(SlmpCodecValueError):
        codec.number_len(0)  # type: ignore[arg-type]  # same runtime guard


@pytest.mark.parametrize("codec", CODECS, ids=lambda c: c.name)
def test_a_short_buffer_raises_instead_of_slicing_short(codec: Codec) -> None:
    """Python slicing silently shortens. That becomes a wrong number three lines later."""
    with pytest.raises(SlmpShortBufferError):
        codec.read_number(b"\x00", 0, bits=32)
    with pytest.raises(SlmpShortBufferError):
        codec.read_words(codec.words((1, 2)), 0, 3)
    with pytest.raises(SlmpShortBufferError):
        codec.read_bits(codec.bits((True,)), 0, 8)
    with pytest.raises(SlmpShortBufferError):
        codec.read_number(codec.number(1, bits=16), 1, bits=16)


@pytest.mark.parametrize("codec", CODECS, ids=lambda c: c.name)
def test_a_negative_offset_or_count_raises(codec: Codec) -> None:
    with pytest.raises(SlmpCodecValueError):
        codec.read_number(b"\x00" * 8, -1, bits=16)
    with pytest.raises(SlmpCodecValueError):
        codec.bit_data_len(-1)
    with pytest.raises(SlmpCodecValueError):
        codec.word_data_len(-1)


@pytest.mark.parametrize("codec", CODECS, ids=lambda c: c.name)
def test_a_word_value_outside_16_bits_raises_and_names_the_point(codec: Codec) -> None:
    with pytest.raises(SlmpCodecValueError) as info:
        codec.words((0, 1, 0x10000))
    assert "word point 2" in str(info.value)


def test_an_undocumented_binary_bit_nibble_raises() -> None:
    """SH(NA)-080956ENG-M p.40 documents 0 and 1. ``bool(5)`` would be a silent fix-up."""
    with pytest.raises(SlmpCodecValueError) as info:
        BINARY.read_bits(b"\x50", 0, 2)
    assert "0x5" in str(info.value)


def test_a_non_zero_padding_nibble_on_an_odd_count_raises() -> None:
    """``00 01 00 1F`` for seven points hides a byte we cannot explain."""
    with pytest.raises(SlmpCodecValueError) as info:
        BINARY.read_bits(b"\x00\x01\x00\x11", 0, 7)
    assert "padding" in str(info.value)
    assert BINARY.read_bits(b"\x00\x01\x00\x10", 0, 7) == M100_TO_M107[:7]


def test_an_undocumented_ascii_bit_character_raises() -> None:
    with pytest.raises(SlmpCodecValueError) as info:
        ASCII.read_bits(b"012", 0, 3)
    assert "'2'" in str(info.value)


def test_a_long_only_device_cannot_be_addressed_with_the_short_specification() -> None:
    """LTN, LSTN, LZ and friends exist only under subcommand 0002/0003."""
    lz = DEVICE_TABLE["LZ"]
    assert lz.code_short is None
    assert lz.ascii2 == ""
    with pytest.raises(SlmpCodecValueError) as info:
        BINARY.device_code(lz, SpecFormat.SHORT)
    assert "0002/0003" in str(info.value)
    with pytest.raises(SlmpCodecValueError) as info:
        ASCII.device_code(lz, SpecFormat.SHORT)
    assert "0002/0003" in str(info.value)
    assert BINARY.device_code(lz, SpecFormat.LONG) == b"\x62\x00"
    assert ASCII.device_code(lz, SpecFormat.LONG) == b"LZ**"


@pytest.mark.parametrize("codec", CODECS, ids=lambda c: c.name)
def test_a_device_number_too_wide_for_its_specification_raises(codec: Codec) -> None:
    """A SHORT device number is 24 bits; 0x1000000 needs the long specification."""
    assert codec.device_number(0xFFFFFF, SpecFormat.SHORT, base=16)
    with pytest.raises(SlmpCodecValueError):
        codec.device_number(0x1000000, SpecFormat.SHORT, base=16)
    assert codec.device_number(0xFFFFFFFF, SpecFormat.LONG, base=16)
    with pytest.raises(SlmpCodecValueError):
        codec.device_number(0x100000000, SpecFormat.LONG, base=16)


def test_a_decimal_device_number_can_outgrow_the_ascii_field() -> None:
    """The ASCII field is narrower than the binary one for every radix below 16.

    ``D1000000`` fits three binary bytes and does not fit six decimal digits. That is a
    refusal with a message, never a truncation to ``"000000"``.
    """
    assert BINARY.device_number(1_000_000, SpecFormat.SHORT, base=10) == b"\x40\x42\x0f"
    assert ASCII.device_number(999_999, SpecFormat.SHORT, base=10) == b"999999"
    with pytest.raises(SlmpCodecValueError) as info:
        ASCII.device_number(1_000_000, SpecFormat.SHORT, base=10)
    assert "0002/0003" in str(info.value)
    assert ASCII.device_number(0o777777, SpecFormat.SHORT, base=8) == b"777777"
    with pytest.raises(SlmpCodecValueError):
        ASCII.device_number(0o1000000, SpecFormat.SHORT, base=8)


@pytest.mark.parametrize("codec", CODECS, ids=lambda c: c.name)
@pytest.mark.parametrize("base", [0, 1, 2, 9, 10.0, 15, 17, 36, None])
def test_a_device_number_base_that_is_not_8_10_or_16_raises(codec: Codec, base: object) -> None:
    """Both codecs check it, so a caller cannot reach the binary path without deciding."""
    with pytest.raises(SlmpCodecValueError) as info:
        # base is typed int; the ignore feeds it the wrong TYPE as well as the wrong
        # value, because a float base silently satisfies a dict lookup (10.0 == 10).
        codec.device_number(1, SpecFormat.SHORT, base=base)  # type: ignore[arg-type]
    assert "base 8, 10 or 16" in str(info.value)


def test_the_ascii_device_number_is_the_devices_radix_not_hexadecimal() -> None:
    """SH(NA)-080956ENG-M p.55, the Read Random request printed in both codings.

    ``M1111`` is ``57 04 00`` and ``"001111"``. Render the ASCII field as the
    hexadecimal of the index and the request reaches ``M4369`` with end code ``0x0000``.
    """
    assert BINARY.device_number(1111, SpecFormat.SHORT, base=10) == b"\x57\x04\x00"
    assert ASCII.device_number(1111, SpecFormat.SHORT, base=10) == b"001111"
    assert ASCII.device_number(1111, SpecFormat.SHORT, base=10) != b"000457"
    # M1234 and B1234: the same ASCII characters, different binary bytes.
    assert ASCII.device_number(1234, SpecFormat.LONG, base=10) == b"00001234"
    assert ASCII.device_number(4660, SpecFormat.LONG, base=16) == b"00001234"
    assert BINARY.device_number(1234, SpecFormat.LONG, base=10) == b"\xd2\x04\x00\x00"
    assert BINARY.device_number(4660, SpecFormat.LONG, base=16) == b"\x34\x12\x00\x00"


def test_the_notation_enum_carries_the_only_base_override() -> None:
    """``Notation.VALUE`` defers to the device's radix; ``OCTAL_DIGITS`` forces base 8."""
    assert Notation.VALUE.fixed_base is None
    assert Notation.OCTAL_DIGITS.fixed_base == 8
    # iQ-F Y20, linear index 16: three renderings of one point.
    assert BINARY.device_number(16, SpecFormat.SHORT, base=8) == b"\x10\x00\x00"
    assert ASCII.device_number(16, SpecFormat.SHORT, base=8) == b"000020"
    assert ASCII.device_number(16, SpecFormat.SHORT, base=16) == b"000010"


def test_every_device_in_the_table_renders_under_its_own_radix() -> None:
    """``Radix`` is an ``IntEnum`` whose value is the base, so it passes straight in."""
    for device in DEVICE_TABLE.values():
        spec = SpecFormat.SHORT if device.code_short is not None else SpecFormat.LONG
        for codec in CODECS:
            rendered = codec.device_number(7, spec, base=device.radix)
            assert len(rendered) == codec.device_number_len(spec), device.name


@pytest.mark.parametrize("codec", CODECS, ids=lambda c: c.name)
def test_a_bool_is_not_an_int_here(codec: Codec) -> None:
    """``True`` is an ``int`` in Python. It is not a device number."""
    with pytest.raises(SlmpCodecValueError):
        codec.number(True, bits=16)


def test_loopback_refuses_a_non_bytes_payload() -> None:
    with pytest.raises(SlmpCodecValueError):
        # str, not bytes: every character of "ABCD" is a legal hex digit, so a codec
        # that iterated it would return True for a payload it cannot send.
        BINARY.loopback_payload_ok("ABCD")  # type: ignore[arg-type]


# ==========================================================================================
# Cross-codec: the same request, both codings, no doubling anywhere
# ==========================================================================================


@given(count=st.integers(min_value=1, max_value=960))
def test_a_word_request_is_exactly_twice_as_long_in_ascii(count: int) -> None:
    assert ASCII.word_data_len(count) == 2 * BINARY.word_data_len(count)


@given(count=st.integers(min_value=1, max_value=3584))
def test_a_bit_request_is_twice_as_long_in_ascii_only_for_even_counts(count: int) -> None:
    doubled = 2 * BINARY.bit_data_len(count)
    if count % 2 == 0:
        assert ASCII.bit_data_len(count) == doubled
    else:
        assert ASCII.bit_data_len(count) == doubled - 1


def test_the_full_ascii_request_fields_from_the_bench() -> None:
    """The 42-character ASCII 3E request that was actually transmitted.

    ``'500000FF03FF000018000004010000D*0000000002'`` -- a batch read of D0, 2 words. The
    FX5U-32MT/DS (fw 1.065) connection entry was configured for binary, so it answered
    with four seconds of silence and no FIN, exactly as 0xC06F is documented to behave.
    The frame is nonetheless well formed, and every field below is this codec's output.
    """
    assert ASCII.number(24, bits=16) == b"0018"  # L, in characters
    assert ASCII.number(0x0401, bits=16) == b"0401"
    assert ASCII.number(0x0000, bits=16) == b"0000"
    assert ASCII.device_code(DEVICE_TABLE["D"], SpecFormat.SHORT) == b"D*"
    assert ASCII.device_number(0, SpecFormat.SHORT, base=DEVICE_TABLE["D"].radix) == b"000000"
    assert ASCII.number(2, bits=16) == b"0002"
    body = (
        ASCII.number(0x0000, bits=16)
        + ASCII.number(0x0401, bits=16)
        + ASCII.number(0x0000, bits=16)
        + ASCII.device_code(DEVICE_TABLE["D"], SpecFormat.SHORT)
        + ASCII.device_number(0, SpecFormat.SHORT, base=DEVICE_TABLE["D"].radix)
        + ASCII.number(2, bits=16)
    )
    assert body == b"000004010000D*0000000002"
    assert len(body) == 24, "L is len(body) and nothing else"
