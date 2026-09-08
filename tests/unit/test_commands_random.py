"""``0403`` ordering, ``1402`` ordering, and the f32 that falls out of the codec.

The Read Random response carries no framing of its own: no count echo, no per-item tag,
nothing that says where the word section ends and the double-word section begins. The
two one-byte counts at the head of the *request* are the whole boundary
(SH(NA)-080956ENG-M section 6.4 pp.53-56). Everything in this file is about that.

The ordering rules have four parts and each one has a test here: word specifications
first, double-word specifications second, no interleaving, and the response mapped back
to the caller's own order. The last of those is the one that has no wire symptom -- get
it wrong and every value is a plausible number in the wrong field, with end code
``0x0000`` and nothing anywhere to say so.
"""

from __future__ import annotations

import struct
from typing import get_args

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from aslmp.commands import (
    AccessWidth,
    EncodeContext,
    RandomPoint,
    RandomWrite,
    ReadRandom,
    WriteRandom,
    bit_point,
    dword,
    word,
)
from aslmp.commands.random import _POINT_DOMAINS, PointKind, split_points
from aslmp.errors import SlmpValueRangeError
from aslmp.profile import Encoding, Link
from aslmp.profiles import FX5U
from aslmp.wire.codec import ASCII, BINARY, Codec, SpecFormat

BIN = EncodeContext(
    codec=BINARY,
    spec=SpecFormat.SHORT,
    profile=FX5U,
    encoding=Encoding.BINARY,
    link=Link.CPU_BUILTIN,
)
ASC = EncodeContext(
    codec=ASCII,
    spec=SpecFormat.SHORT,
    profile=FX5U,
    encoding=Encoding.ASCII_XY_HEX,
    link=Link.CPU_BUILTIN,
)


# ======================================================================================
# Ordering
# ======================================================================================


def test_word_specifications_come_first_whatever_order_the_caller_used() -> None:
    """The wire order is fixed; the caller's is not."""
    command = ReadRandom((dword("D10"), word("D0"), dword("D20"), word("D2")))
    payload = command.encode(BIN)
    assert payload[:2] == b"\x02\x02"
    specs = [payload[2 + 4 * i : 6 + 4 * i] for i in range(4)]
    assert specs == [
        b"\x00\x00\x00\xa8",  # D0   word
        b"\x02\x00\x00\xa8",  # D2   word
        b"\x0a\x00\x00\xa8",  # D10  double-word
        b"\x14\x00\x00\xa8",  # D20  double-word
    ]


def test_a_section_with_no_points_is_absent_entirely() -> None:
    """'The specification is not necessary when the number of word access points is 0'."""
    only_dwords = ReadRandom((dword("D0"), dword("D2")))
    assert only_dwords.encode(BIN)[:2] == b"\x00\x02"
    assert len(only_dwords.encode(BIN)) == 2 + 2 * 4
    only_words = ReadRandom((word("D0"),))
    assert only_words.encode(BIN)[:2] == b"\x01\x00"


def test_results_come_back_in_the_callers_order_not_the_wires() -> None:
    """The failure this prevents has no wire symptom at all."""
    command = ReadRandom((dword("D10"), word("D0"), dword("D20"), word("D2")))
    # wire: D0, D2 (one word each), then D10, D20 (two words each)
    payload = (
        b"\x11\x11"  # D0
        b"\x22\x22"  # D2
        b"\x33\x33\x44\x44"  # D10/D11
        b"\x55\x55\x66\x66"  # D20/D21
    )
    assert command.decode(payload, BIN) == (
        0x44443333,
        0x1111,
        0x66665555,
        0x2222,
    )


def test_split_points_is_a_stable_partition_and_not_a_sort() -> None:
    points = (dword("D10"), word("D0"), dword("D20"), word("D2"), word("D4"))
    assert split_points(points) == ((1, 3, 4), (0, 2))


def test_aliasing_the_same_register_at_two_widths_is_legal() -> None:
    """Reading D0 as a u16 and D0-D1 as an f32 in one request is well-formed SLMP.

    ``plc-comm-slmp`` bans this harmless case while allowing ['D0:F', 'D1:U'] -- the
    confusing physical overlap. Neither is refused here; both are the caller's business.
    """
    command = ReadRandom((word("D0"), dword("D0", kind="f32")))
    command.validate(BIN)
    payload = b"\x00\x00" + b"\x00\x00\x70\x42"
    assert command.decode(payload, BIN) == (0x0000, 60.0)


# ======================================================================================
# Widths, kinds and the f32
# ======================================================================================


def test_one_double_word_point_is_one_f32_low_word_first() -> None:
    """Measured four ways on FX5U-32MT/DS fw 1.065: no byte-swapping helper exists."""
    command = ReadRandom((dword("D0", kind="f32"),))
    assert command.decode(b"\x00\x00\x70\x42", BIN) == (60.0,)
    assert command.decode(b"\x00\x80\x71\x42", BIN) == (60.375,)


def test_the_binary_and_ascii_codings_decode_a_dword_to_the_same_value() -> None:
    """Opposite word order on the wire, identical value, one implementation."""
    command = ReadRandom((dword("D0", kind="f32"),))
    assert command.decode(b"\x00\x00\x70\x42", BIN) == (60.0,)
    assert command.decode(b"42700000", ASC) == (60.0,)


def test_a_word_point_on_a_bit_device_is_sixteen_bits_lsb_first() -> None:
    command = ReadRandom((bit_point("M100"),))
    decoded = command.decode(b"\x30\x20", BIN)[0]
    assert decoded == tuple(bool(0x2030 >> i & 1) for i in range(16))
    assert decoded[4] is True and decoded[5] is True and decoded[13] is True


def test_signed_kinds_are_twos_complement() -> None:
    command = ReadRandom((word("D0", kind="i16"), dword("D2", kind="i32")))
    assert command.decode(b"\xff\xff" + b"\xff\xff\xff\xff", BIN) == (-1, -1)


def test_a_dword_point_spans_two_device_numbers_in_the_range_check() -> None:
    ReadRandom((dword("D7998"),)).validate(BIN)
    with pytest.raises(Exception, match="0xC056"):
        ReadRandom((dword("D7999"),)).validate(BIN)


# ======================================================================================
# 1402 word/double-word writes
# ======================================================================================


def test_a_write_random_puts_its_data_after_each_specification() -> None:
    """Measured request: 03 01, three word points, then the f32 double-word point."""
    command = WriteRandom(
        (
            RandomWrite(word("D100"), 0x1111),
            RandomWrite(word("D101"), 0x2222),
            RandomWrite(word("D102"), 0x3333),
            RandomWrite(dword("D104", kind="f32"), 1234.5),
        )
    )
    assert command.encode(BIN).hex() == (
        "0301"
        "640000a8" "1111"
        "650000a8" "2222"
        "660000a8" "3333"
        "680000a8" "00509a44"
    )


def test_a_write_random_reorders_its_data_with_its_specifications() -> None:
    command = WriteRandom(
        (RandomWrite(dword("D10"), 0x11223344), RandomWrite(word("D0"), 0x5566))
    )
    assert command.encode(BIN).hex() == "0101" "000000a8" "6655" "0a0000a8" "44332211"


def test_the_ascii_dword_write_puts_the_high_word_first() -> None:
    """The same value, the opposite word order, and one expression in the codec."""
    command = WriteRandom((RandomWrite(dword("D1500", kind="u32"), 0x04391202),))
    assert command.encode(BIN).endswith(b"\x02\x12\x39\x04")
    assert command.encode(ASC).endswith(b"04391202")


def test_an_f32_write_and_an_f32_read_agree_bit_for_bit() -> None:
    """The write path and the read path must agree, including on a value float32
    cannot hold exactly: 1.5e-8 is 1.4999999464748726e-08 once it is a float32, and it
    is that, not 0.0. ``pymelsec``'s ``%.6f`` turned a live 1.5e-8 into 0.0 here."""
    for value in (0.0, 1.0, -1.0, 60.375, 1234.5, 3.4028234663852886e38, 1.5e-8):
        rounded = struct.unpack("<f", struct.pack("<f", value))[0]
        written = WriteRandom((RandomWrite(dword("D0", kind="f32"), value),)).encode(BIN)
        data = written[len(written) - 4 :]
        assert ReadRandom((dword("D0", kind="f32"),)).decode(data, BIN) == (rounded,)
    assert struct.unpack("<f", struct.pack("<f", 1.5e-8))[0] != 0.0


# ======================================================================================
# Properties
# ======================================================================================

ADDRESSES = st.integers(min_value=0, max_value=7000).map(lambda i: f"D{i}")
KINDS_WORD = st.sampled_from(["u16", "i16"])
KINDS_DWORD = st.sampled_from(["u32", "i32", "f32"])

POINTS = st.lists(
    st.one_of(
        st.builds(
            lambda a, k: RandomPoint(a, AccessWidth.WORD, k), ADDRESSES, KINDS_WORD
        ),
        st.builds(
            lambda a, k: RandomPoint(a, AccessWidth.DWORD, k), ADDRESSES, KINDS_DWORD
        ),
    ),
    min_size=1,
    max_size=24,
)


@given(points=POINTS, ascii_coding=st.booleans())
@settings(max_examples=200, deadline=None)
def test_payload_len_always_equals_len_encode(
    points: list[RandomPoint], ascii_coding: bool
) -> None:
    """The invariant that guards the measured overstate-hangs asymmetry."""
    ctx: EncodeContext = ASC if ascii_coding else BIN
    command = ReadRandom(tuple(points))
    assert command.payload_len(ctx) == len(command.encode(ctx))


@given(points=POINTS, ascii_coding=st.booleans())
@settings(max_examples=200, deadline=None)
def test_the_two_counts_always_sum_to_the_point_count(
    points: list[RandomPoint], ascii_coding: bool
) -> None:
    codec: Codec = ASCII if ascii_coding else BINARY
    ctx: EncodeContext = ASC if ascii_coding else BIN
    payload = ReadRandom(tuple(points)).encode(ctx)
    width = codec.number_len(8)
    words = codec.read_number(payload, 0, bits=8)
    dwords = codec.read_number(payload, width, bits=8)
    assert words + dwords == len(points)
    assert len(payload) == 2 * width + len(points) * ctx.device_spec_len()


@given(
    raw=st.integers(min_value=0, max_value=0xFFFFFFFF), ascii_coding=st.booleans()
)
@settings(max_examples=400, deadline=None)
def test_f32_decoding_is_bit_exact_in_both_codings(
    raw: int, ascii_coding: bool
) -> None:
    """Every float32 bit pattern, including subnormals and both words above 0x7FFF.

    ``pymelsec``'s ``%.6f`` turned a live 1.5e-8 into 0.0 at exactly this point.
    """
    ctx: EncodeContext = ASC if ascii_coding else BIN
    codec: Codec = ASCII if ascii_coding else BINARY
    expected = struct.unpack("<f", struct.pack("<I", raw))[0]
    decoded = ReadRandom((dword("D0", kind="f32"),)).decode(
        codec.number(raw, bits=32), ctx
    )[0]
    if expected != expected:  # NaN
        assert decoded != decoded
        return
    assert struct.pack("<f", decoded) == struct.pack("<f", expected)


@given(points=POINTS)
@settings(max_examples=200, deadline=None)
def test_decode_of_encode_restores_caller_order(points: list[RandomPoint]) -> None:
    """Build a response from known per-point values and assert every one comes back."""
    command = ReadRandom(tuple(points))
    word_ix, dword_ix = split_points(command.points)
    values = {index: (index * 7 + 1) & 0xFFFF for index in word_ix}
    values.update({index: (index * 131071 + 3) & 0xFFFFFFFF for index in dword_ix})
    payload = b"".join(
        BINARY.number(values[index], bits=16) for index in word_ix
    ) + b"".join(BINARY.number(values[index], bits=32) for index in dword_ix)
    decoded = command.decode(payload, BIN)
    for index, point in enumerate(command.points):
        raw = values[index]
        if point.kind == "u16" or point.kind == "u32":
            assert decoded[index] == raw
        elif point.kind == "i16":
            assert decoded[index] == (raw - 0x10000 if raw >= 0x8000 else raw)
        elif point.kind == "i32":
            assert decoded[index] == (
                raw - 0x100000000 if raw >= 0x80000000 else raw
            )
        else:
            assert struct.pack("<f", decoded[index]) == struct.pack("<I", raw)


# ======================================================================================
# A point carries its own type, and wire_value is where that is enforced
# ======================================================================================


def test_wire_value_holds_a_write_to_the_type_the_point_itself_declares() -> None:
    """The regression. ``wire_value()`` called the 16/32-bit helper with no domain.

    So the type the caller had *just written down* -- ``word("D100", kind="i16")`` --
    was not enforced anywhere on this path: 40000 masked to 0x9C40 and would read back
    as -25536, with end code 0x0000 at every step. ``Plc.write_random`` refused it one
    layer up, which made the refusal a property of the door rather than of the request;
    a caller holding a ``WriteRandom`` (a bound block plan builds them, and so does
    anyone composing a frame by hand) got the mask.

    Measured before the fix on FX5U-32MT/DS fw 1.065 from this host over TCP 5002
    (2026-09-07): a 1402 carrying that masked word answered 0x0000 and D100 read back
    -25536 as an i16.
    """
    with pytest.raises(SlmpValueRangeError, match="signed 16-bit field"):
        RandomWrite(word("D100", kind="i16"), 40_000).wire_value()
    with pytest.raises(SlmpValueRangeError, match="signed 32-bit field"):
        RandomWrite(dword("D100", kind="i32"), 3_000_000_000).wire_value()

    # In range for the declared type, and still two's complement on the wire.
    assert RandomWrite(word("D100", kind="i16"), -1).wire_value() == 0xFFFF
    assert RandomWrite(word("D100", kind="i16"), 32_767).wire_value() == 0x7FFF
    assert RandomWrite(dword("D100", kind="i32"), -1).wire_value() == 0xFFFFFFFF


def test_a_u16_point_is_unsigned_the_way_every_other_u16_door_is() -> None:
    """``u16`` prints with no suffix because it is the default kind for its width, not
    because it is untyped -- and it was being enforced as untyped.

    ``RandomWrite(word('D101', kind='u16'), -1)`` masked to ``0xFFFF`` and
    ``write_random`` sent it while ``Plc.write_u16(-1)`` refused, so ``D101`` read back
    65535 through one door and nothing at all through the other (FX5U-32MT/DS fw 1.065
    over TCP 5002 from this host, 2026-09-07). The raw-register door, where -1 and 65535
    are the same sixteen bits, is ``write_words`` and ``BlockWrite``; a named type is a
    named type wherever it is named.
    """
    assert str(word("D100")) == "D100"
    assert RandomWrite(word("D100"), 65_535).wire_value() == 0xFFFF
    assert RandomWrite(dword("D100"), 0xFFFF_FFFF).wire_value() == 0xFFFFFFFF
    for outside in (-1, 70_000):
        with pytest.raises(SlmpValueRangeError, match="does not fit"):
            RandomWrite(word("D100"), outside).wire_value()
    with pytest.raises(SlmpValueRangeError, match="does not fit"):
        RandomWrite(dword("D100"), -1).wire_value()


def test_every_point_kind_maps_to_exactly_one_declared_domain() -> None:
    """A total function, not a ``.get()`` with a permissive fallback: that fallback is
    how a kind added later would silently inherit the union of the two ranges."""
    kinds = {"u16", "i16", "u32", "i32", "f32", "bits"}
    assert set(get_args(PointKind)) == kinds
    assert set(_POINT_DOMAINS) == kinds
    assert word("D0", kind="i16").signed_field is True
    assert dword("D0", kind="i32").signed_field is True
    # False, not None: u16/u32 name an unsigned type and are held to it. None is
    # reserved for the kinds that never reach unsigned() at all.
    assert word("D0").signed_field is False
    assert dword("D0").signed_field is False
    assert dword("D0", kind="f32").signed_field is None
    assert bit_point("D0").signed_field is None
