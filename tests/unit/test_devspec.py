"""The device specification block, and the access route.

Two wire fragments that every command carries, both with a byte order that is *reversed
between codings* and therefore invisible when you get it wrong.

**The device specification** is ``[number][code]`` in binary and ``[code][number]`` in
ASCII (SH(NA)-080956ENG-M pp.45-49; the command catalogue calls it "the single most
common place to get it wrong"). A swapped ASCII block is still eight legal characters
and still reaches *a* register.

**The ASCII device number is not hexadecimal** -- it is the linear index written in the
device's own radix, so ``M1111`` is ``"001111"`` while its binary field is ``57 04 00``.
SH(NA)-080956ENG-M p.55 prints exactly that request in both codings, which is why the
vectors below can be typed straight off a printed page.

**And the emit radix is not the parse radix.** ``Y45`` on an iQ-F is octal in both ASCII
modes -- it is index 37 either way -- but it is rendered ``"000045"`` under
``ASCII_XY_OCT`` and ``"000025"`` under ``ASCII_XY_HEX``, and ``25 00 00`` in binary.
Three renderings, one address, one test each.

``Route`` lives here too rather than in a file of its own: it is the other five bytes
whose binary and ASCII forms are not transformations of one another, and the test that
proves that is one assertion long.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from aslmp.wire.address import AddressProfile, DeviceAddress, parse_address
from aslmp.wire.codec import (
    ASCII,
    BINARY,
    CODECS,
    Codec,
    Notation,
    SlmpShortBufferError,
    SpecFormat,
)
from aslmp.wire.devicetable import DEVICE_TABLE, DeviceType, Radix
from aslmp.wire.devspec import (
    SlmpNotationError,
    SlmpSpecFormatError,
    devspec_len,
    emit_base,
    encode_device_number,
    encode_device_spec,
)
from aslmp.wire.route import ACCESS_ROUTE, Route, SlmpRouteError


@dataclass(frozen=True, slots=True)
class Stub:
    """The smallest thing that satisfies :class:`AddressProfile`; see test_address.py.

    Deliberately re-declared rather than imported from the sibling test module: this
    tree has no ``tests`` package, and a cross-file import would make the same source
    file reachable under two module names.
    """

    key: str
    octal_xy: bool

    def radix_for(self, dt: DeviceType) -> Radix:
        if self.octal_xy and dt.name in ("X", "Y"):
            return Radix.OCTAL
        return dt.radix


FX5U: AddressProfile = Stub("melsec:iq-f/fx5u", octal_xy=True)
IQ_R: AddressProfile = Stub("melsec:iq-r/r04cpu", octal_xy=False)


def address(literal: str, *, octal_xy: bool = False) -> DeviceAddress:
    return parse_address(literal, FX5U if octal_xy else IQ_R)


SHORT = SpecFormat.SHORT
LONG = SpecFormat.LONG
VALUE = Notation.VALUE
OCT = Notation.OCTAL_DIGITS


# ======================================================================================
# Golden vectors, one row per printed page
# ======================================================================================

# (literal, octal_xy profile, codec, spec, notation, expected bytes, source)
VECTORS: tuple[tuple[str, bool, Codec, SpecFormat, Notation, bytes, str], ...] = (
    (
        "M100", False, BINARY, SHORT, VALUE, bytes.fromhex("64 00 00 90"),
        "SH(NA)-080956ENG-M p.47, read M100-M107 in bit units",
    ),
    (
        # The manual writes this example as "T100"; the SLMP device is the timer CURRENT
        # VALUE, code C2H, whose name in the device table is TN. There is no bare T
        # device: TS is the contact, TC the coil, TN the current value. parse_address
        # refuses "T100" for exactly that reason.
        "TN100", False, BINARY, SHORT, VALUE, bytes.fromhex("64 00 00 C2"),
        "SH(NA)-080956ENG-M p.49, read T100-T102 in word units",
    ),
    (
        "M1111", False, BINARY, SHORT, VALUE, bytes.fromhex("57 04 00 90"),
        "SH(NA)-080956ENG-M p.55, Read Random, binary",
    ),
    (
        "M1111", False, ASCII, SHORT, VALUE, b"M*001111",
        "SH(NA)-080956ENG-M p.55, the SAME request in ASCII: not the hex of 0457H",
    ),
    (
        "D1500", False, BINARY, SHORT, VALUE, bytes.fromhex("DC 05 00 A8"),
        "SH(NA)-080956ENG-M p.55, Read Random, binary",
    ),
    (
        "D1500", False, ASCII, SHORT, VALUE, b"D*001500",
        "SH(NA)-080956ENG-M p.55, ASCII",
    ),
    (
        "B1234", False, BINARY, SHORT, VALUE, bytes.fromhex("34 12 00 A0"),
        "SH(NA)-080956ENG-M p.55: B is hexadecimal, so B1234 is 4660",
    ),
    (
        "B1234", False, ASCII, SHORT, VALUE, b"B*001234",
        "SH(NA)-080956ENG-M p.55: the digits are the device's own radix, hexadecimal",
    ),
    (
        "D100", False, ASCII, SHORT, VALUE, b"D*000100",
        "JY997D56001-K p.75, the worked 1401 write: '1401' '0000' 'D*' '000100' '0003'",
    ),
    (
        "X20", False, ASCII, SHORT, VALUE, b"X*000020",
        "SH(NA)-080956ENG-M pp.38, 55: X is hexadecimal on iQ-R, so X20 is index 32",
    ),
    (
        "Y45", True, BINARY, SHORT, VALUE, bytes.fromhex("25 00 00 9D"),
        "measured FX5U-32MT/DS fw 1.065: Y45 is octal, index 37 = 0x25, on the wire",
    ),
    (
        "Y45", True, ASCII, SHORT, VALUE, b"Y*000025",
        "JY997D56001-K p.12, ASCII code (X, Y HEX): the index rendered in hexadecimal",
    ),
    (
        "Y45", True, ASCII, SHORT, OCT, b"Y*000045",
        "JY997D56001-K p.12, ASCII code (X, Y OCT): the index rendered in octal",
    ),
)


@pytest.mark.parametrize(
    ("literal", "octal_xy", "codec", "spec", "notation", "expected", "source"),
    VECTORS,
    ids=[f"{row[0]}-{row[2].name}-{row[4].value}" for row in VECTORS],
)
def test_golden_device_specification_block(
    literal: str,
    octal_xy: bool,
    codec: Codec,
    spec: SpecFormat,
    notation: Notation,
    expected: bytes,
    source: str,
) -> None:
    block = encode_device_spec(
        address(literal, octal_xy=octal_xy), codec=codec, spec=spec, notation=notation
    )
    assert block == expected, f"{literal} in {codec.name}: {source}"


def test_the_three_renderings_of_one_iq_f_output() -> None:
    """One address, one index, three wire forms. DESIGN section 4.3's table.

    The literal is octal in all three: the parse radix is a profile fact and does not
    move. Only the ASCII rendering follows the GX Works3 own-node parameter.
    """
    y45 = address("Y45", octal_xy=True)
    assert y45.index == 37
    binary = encode_device_spec(y45, codec=BINARY, spec=SHORT, notation=VALUE)
    assert binary[:3] == b"\x25\x00\x00"
    assert encode_device_number(y45, codec=ASCII, spec=SHORT, notation=VALUE) == b"000025"
    assert encode_device_number(y45, codec=ASCII, spec=SHORT, notation=OCT) == b"000045"


# ======================================================================================
# The field-order swap
# ======================================================================================


@pytest.mark.parametrize("name", ["D", "M", "Y", "TN", "B", "SB", "STN"])
def test_binary_is_number_then_code_and_ascii_is_code_then_number(name: str) -> None:
    """The swap, asserted structurally for every short-form device in the table."""
    dt = DEVICE_TABLE[name]
    index = 0x1234 if dt.radix is Radix.HEXADECIMAL else 100
    addr = DeviceAddress.of(dt, index, radix=dt.radix)
    for codec in CODECS:
        number = encode_device_number(addr, codec=codec, spec=SHORT, notation=VALUE)
        code = codec.device_code(dt, SHORT)
        block = encode_device_spec(addr, codec=codec, spec=SHORT, notation=VALUE)
        if codec.name == "binary":
            assert block == number + code
            assert block.endswith(code)
        else:
            assert block == code + number
            assert block.startswith(code)


def test_a_swapped_block_would_still_look_like_a_legal_address() -> None:
    """Why this has a test at all: the wrong order is not detectable downstream.

    ``"000100D*"`` is eight ASCII characters, ``A8 64 00 00`` is four bytes, and both are
    shaped exactly like a device specification. The PLC answers one of them ``0x0000``.
    """
    d100 = address("D100")
    block = encode_device_spec(d100, codec=ASCII, spec=SHORT, notation=VALUE)
    swapped = block[2:] + block[:2]
    assert swapped == b"000100D*"
    assert len(swapped) == len(block)
    assert block != swapped


@pytest.mark.parametrize(("codec", "spec", "expected"), [
    (BINARY, SHORT, 4),
    (BINARY, LONG, 6),
    (ASCII, SHORT, 8),
    (ASCII, LONG, 12),
])
def test_devspec_len_matches_the_bytes_actually_emitted(
    codec: Codec, spec: SpecFormat, expected: int
) -> None:
    """``payload_len`` uses this to fill ``L`` without building a throwaway buffer.

    An overstated ``L`` is the one failure on this hardware that produces no response at
    all and looks exactly like a dead PLC (FX5U-32MT/DS fw 1.065, measured 2026-09-06),
    so the length and the bytes are asserted against each other rather than separately.
    """
    assert devspec_len(codec, spec) == expected
    dt = DEVICE_TABLE["D"] if spec is SHORT else DEVICE_TABLE["LCN"]
    addr = DeviceAddress.of(dt, 100, radix=Radix.DECIMAL)
    assert len(encode_device_spec(addr, codec=codec, spec=spec, notation=VALUE)) == expected


def test_the_long_specification_widens_both_fields() -> None:
    """Subcommand 0002/0003: 2-byte code and 4-byte number, or 4 and 8 characters."""
    d0 = address("D0")
    assert encode_device_spec(d0, codec=BINARY, spec=LONG, notation=VALUE) == bytes.fromhex(
        "00 00 00 00 A8 00"
    )
    assert encode_device_spec(d0, codec=ASCII, spec=LONG, notation=VALUE) == b"D***00000000"


# ======================================================================================
# Refusals
# ======================================================================================


def test_octal_digits_with_binary_raises_rather_than_being_coerced() -> None:
    """DESIGN section 4.3, verbatim: it raises rather than being silently coerced.

    A caller asking for octal digits in binary coding believes something about the
    connection that is not true, and quietly giving them ``Notation.VALUE`` -- which
    happens to produce the right bytes -- leaves that belief in place until the next
    connection is ASCII.
    """
    y45 = address("Y45", octal_xy=True)
    with pytest.raises(SlmpNotationError) as caught:
        encode_device_spec(y45, codec=BINARY, spec=SHORT, notation=OCT)
    assert "ASCII_XY_OCT" in str(caught.value)


@pytest.mark.parametrize("name", ["D", "M", "B", "W", "ZR"])
def test_octal_digits_for_anything_but_x_and_y_raises(name: str) -> None:
    """``D100`` in base 8 would go out as ``"000144"`` and reach ``D144``."""
    addr = DeviceAddress.of(DEVICE_TABLE[name], 100, radix=DEVICE_TABLE[name].radix)
    with pytest.raises(SlmpNotationError):
        encode_device_spec(addr, codec=ASCII, spec=SHORT, notation=OCT)


@pytest.mark.parametrize("name", ["LTN", "LSTN", "LCN", "LZ", "RD", "LTS", "LSTC"])
@pytest.mark.parametrize("codec", CODECS, ids=[c.name for c in CODECS])
def test_a_long_only_device_cannot_use_the_short_specification(
    name: str, codec: Codec
) -> None:
    """And on an iQ-F it cannot use the long one either: 0xC059, measured."""
    addr = DeviceAddress.of(DEVICE_TABLE[name], 0, radix=Radix.DECIMAL)
    with pytest.raises(SlmpSpecFormatError) as caught:
        encode_device_spec(addr, codec=codec, spec=SHORT, notation=VALUE)
    assert "0002/0003" in str(caught.value)
    assert "0xC059" in str(caught.value)
    assert encode_device_spec(addr, codec=codec, spec=LONG, notation=VALUE)


def test_the_notation_argument_is_required_and_typed() -> None:
    with pytest.raises(TypeError):
        encode_device_spec(
            address("D0"), codec=BINARY, spec=SHORT, notation="value"  # type: ignore[arg-type]
        )


def test_emit_base_is_the_device_table_radix_not_the_parse_radix() -> None:
    """The distinction the whole module turns on, asserted on its own.

    ``Y`` is hexadecimal in the generic device table and octal on an iQ-F *literal*. The
    emit base under ``Notation.VALUE`` is the former; using the latter would render every
    X/Y ASCII field in octal on a connection configured for ``ASCII_XY_HEX``.
    """
    y = DEVICE_TABLE["Y"]
    parsed = address("Y45", octal_xy=True)
    assert parsed.radix is Radix.OCTAL
    assert emit_base(y, VALUE, codec=ASCII) == 16
    assert emit_base(y, OCT, codec=ASCII) == 8
    assert emit_base(DEVICE_TABLE["D"], VALUE, codec=ASCII) == 10


# ======================================================================================
# Route -- the other five bytes
# ======================================================================================


def test_the_own_station_route_is_the_manuals_own_bytes() -> None:
    """SH(NA)-080008-AB p.48 prints both forms verbatim."""
    assert Route() == Route.OWN_STATION
    assert Route.OWN_STATION.encode(BINARY) == bytes.fromhex("00 FF FF 03 00")
    assert Route.OWN_STATION.encode(ASCII) == b"00FF03FF00"
    assert Route.OWN_STATION.network == 0x00
    assert Route.OWN_STATION.station == 0xFF
    assert Route.OWN_STATION.module_io == 0x03FF
    assert Route.OWN_STATION.multidrop == 0x00


def test_the_ascii_route_is_not_the_hexlified_binary_route() -> None:
    """The module I/O number is little-endian in binary and MSD-first in ASCII.

    Hexlifying the binary route gives ``"00FFFF0300"``; the correct ASCII route is
    ``"00FF03FF00"``. A client that builds one frame and transliterates it into the other
    coding gets module I/O ``FF03H`` -- which is why DESIGN section 4.1 says frames are
    built from values and never by hexlify-ing the binary frame.
    """
    hexlified = Route.OWN_STATION.encode(BINARY).hex().upper().encode("ascii")
    assert hexlified == b"00FFFF0300"
    assert Route.OWN_STATION.encode(ASCII) == b"00FF03FF00"
    assert hexlified != Route.OWN_STATION.encode(ASCII)


def test_the_module_io_number_is_little_endian_in_binary() -> None:
    assert Route(module_io=0x03FF).encode(BINARY)[2:4] == b"\xff\x03"
    assert Route(module_io=0x03E1).encode(BINARY)[2:4] == b"\xe1\x03"
    assert Route(module_io=0x03E1).encode(ASCII)[4:8] == b"03E1"


@pytest.mark.parametrize("codec", CODECS, ids=[c.name for c in CODECS])
@pytest.mark.parametrize(
    "route",
    [
        Route(),
        Route(network=0x01, station=0x01, module_io=0x0000, multidrop=0x00),
        Route(network=0xEF, station=0x78, module_io=0xFFFF, multidrop=0x1F),
        Route(station=0x7D),
    ],
)
def test_route_round_trips_through_both_codings(route: Route, codec: Codec) -> None:
    encoded = route.encode(codec)
    assert len(encoded) == Route.wire_len(codec)
    assert Route.decode(encoded, 0, codec) == route
    assert Route.decode(b"\x00" * 7 + encoded, 7, codec) == route


def test_route_wire_length_is_five_units() -> None:
    """Part of the 7-unit prefix that the length field ``L`` deliberately excludes."""
    assert Route.wire_len(BINARY) == 5
    assert Route.wire_len(ASCII) == 10


def test_a_short_buffer_does_not_decode_into_a_plausible_route() -> None:
    with pytest.raises(SlmpShortBufferError):
        Route.decode(bytes.fromhex("00 FF FF"), 0, BINARY)


@pytest.mark.parametrize(
    ("kwargs", "why"),
    [
        ({"network": 0xF0}, "networks 240-255 are not accessible"),
        ({"network": 0x100}, "one byte"),
        ({"network": -1}, "not signed"),
        ({"station": 0x00}, "00H is not a documented station number"),
        ({"station": 0x79}, "stations run 01H-78H"),
        ({"station": 0x7B}, "7BH is not one of the special values"),
        ({"module_io": 0x10000}, "two bytes"),
        ({"multidrop": 0x20}, "multidrop stations run 00H-1FH"),
    ],
)
def test_an_undocumented_route_field_is_refused(kwargs: dict[str, int], why: str) -> None:
    """A route this library cannot name is a route it will not send.

    Every message names the manual section, because a route that is legal on somebody's
    CC-Link IE network and refused here is a bug report we want with the page attached.
    """
    with pytest.raises(SlmpRouteError) as caught:
        Route(**kwargs)
    assert ACCESS_ROUTE.manual in str(caught.value), why


def test_a_non_integer_route_field_is_a_type_error() -> None:
    with pytest.raises(TypeError):
        Route(network="00")  # type: ignore[arg-type]  # the runtime half of the check


def test_a_route_is_frozen_and_prints_itself() -> None:
    route = Route.OWN_STATION
    with pytest.raises((AttributeError, TypeError)):
        route.network = 1  # type: ignore[misc]  # frozen dataclass, checked at runtime
    assert str(route) == "own station (00 FF FF 03 00)"
    assert "0x03E1" in str(Route(module_io=0x03E1))
