"""Address parsing, against the bench and against the manuals.

This file guards the single most valuable correctness property in the library, so it is
worth saying plainly what that property is.

**GX Works3 numbers an FX5's X and Y in octal, and the wire carries the VALUE of the
literal, not its digits.** ``Y20`` is the seventeenth output and goes out as 16. A client
that sends the digits as written puts ``Y10`` on the wire as 10 and moves the *eleventh*
output instead of the ninth: end code ``0x0000``, correct-looking response, wrong
physical output, and the error grows with the address. Measured on FX5U-32MT/DS firmware
1.065 on 2026-09-07 by setting one bit at a chosen wire number and reading back which
linear output lit.

The same bench showed the PLC will not catch a bad literal for you: a write at wire
number 8 for ``Y`` -- which is what the illegal octal literal ``Y8`` would have to mean
-- was accepted and answered ``0x0000``. So ``test_y8_and_y9_are_refused_because_the_plc
_accepts_them`` is not pedantry; it is the only thing in the stack that will ever say no.

And the radix belongs to the **profile**, not to the letter: ``X1F`` is 31 on an iQ-R and
a parse error on an iQ-F. Any test here that passes a profile is testing that, too.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from hypothesis import given
from hypothesis import strategies as st

from aslmp.wire.address import (
    MAX_DEVICE_INDEX,
    XY_IS_THE_LINEAR_INDEX,
    XY_OCTAL_IS_NOT_VALIDATED_BY_THE_PLC,
    AddressProfile,
    DeviceAddress,
    SlmpAddressError,
    SlmpAddressTextError,
    SlmpDeviceIndexError,
    SlmpRadixDigitError,
    SlmpUnknownPrefixError,
    format_address,
    parse_address,
)
from aslmp.wire.devicetable import DEVICE_TABLE, PREFIXES_LONGEST_FIRST, DeviceType, Radix


@dataclass(frozen=True, slots=True)
class Stub:
    """The smallest thing that satisfies :class:`AddressProfile`.

    Build unit U6's ``CpuProfile`` is not written yet, and layer 0 may not import layer 1
    in any case. This stands in for it, and the ``AddressProfile`` annotations below are
    a static assertion that the protocol is small enough for a real profile to satisfy by
    accident -- a ``key`` string and a ``radix_for`` method, nothing else.
    """

    key: str
    octal_xy: bool

    def radix_for(self, dt: DeviceType) -> Radix:
        if self.octal_xy and dt.name in ("X", "Y"):
            return Radix.OCTAL
        return dt.radix


FX5U: AddressProfile = Stub("melsec:iq-f/fx5u", octal_xy=True)
IQ_R: AddressProfile = Stub("melsec:iq-r/r04cpu", octal_xy=False)
PROFILES = (FX5U, IQ_R)


# ======================================================================================
# The measured golden vectors. If one of these breaks, an output moves.
# ======================================================================================

# GX Works3 literal -> wire device number. FX5U-32MT/DS fw 1.065, 2026-09-07.
XY_VECTORS: tuple[tuple[str, int, str], ...] = (
    ("Y0", 0, "1st output"),
    ("Y7", 7, "8th output -- the last before the octal carry"),
    ("Y10", 8, "9th output; sending the digits as written would reach the 11th"),
    ("Y17", 15, "16th output"),
    ("Y20", 16, "17th output; 0x10 on the wire, measured directly"),
    ("Y70", 56, "the error grows with the address: 0x70 would be 56 outputs away"),
    ("X0", 0, "inputs are numbered the same way"),
    ("X17", 15, "and X17 is 23 on an iQ-R, where X is hexadecimal"),
)


@pytest.mark.parametrize(("literal", "wire", "why"), XY_VECTORS)
def test_xy_wire_number_is_the_value_of_the_octal_literal(
    literal: str, wire: int, why: str
) -> None:
    """The golden vectors from the bench. Read the message below before changing this."""
    address = parse_address(literal, FX5U)
    assert address.index == wire, (
        f"\n{literal} parsed to wire device number {address.index}, not {wire}.\n"
        f"{literal} is the {why}.\n\n"
        f"GX Works3 shows an FX5's X and Y in OCTAL, and the SLMP device number field "
        f"carries the VALUE of that literal, never its digits. So Y10 is 8 on the wire "
        f"and Y20 is 16. If this test fails because the parser now sends the digits as "
        f"written, the library is off by two at Y10 and by 56 at Y70, the PLC answers "
        f"end code 0x0000, and the only symptom is that the wrong output moves.\n\n"
        f"Measured: {XY_IS_THE_LINEAR_INDEX.reference}\n{XY_IS_THE_LINEAR_INDEX.note}"
    )


def test_the_same_literals_are_hexadecimal_on_an_iq_r() -> None:
    """The radix is a property of the CPU family, not of the letter X.

    This is why DESIGN section 0.3 deletes the generic profile: an unrecognised model
    code that fell back to hexadecimal would make every one of the vectors above wrong
    by a different amount on hardware nobody tested.
    """
    assert parse_address("Y10", IQ_R).index == 16
    assert parse_address("Y20", IQ_R).index == 32
    assert parse_address("X17", IQ_R).index == 23


@pytest.mark.parametrize("literal", ["Y8", "Y9", "X8", "X9", "Y18", "Y29", "X108"])
def test_y8_and_y9_are_refused_because_the_plc_accepts_them(literal: str) -> None:
    """8 and 9 are not octal digits, and the FX5U does not care.

    A bit-unit write at wire number 8 for Y was accepted and answered ``0x0000``
    (measured). Client-side refusal is the only guard that exists.
    """
    with pytest.raises(SlmpRadixDigitError) as caught:
        parse_address(literal, FX5U)
    error = caught.value
    assert error.radix is Radix.OCTAL
    assert error.character in "89"
    assert error.text == literal
    assert XY_OCTAL_IS_NOT_VALIDATED_BY_THE_PLC.cpu in str(error)
    assert "0x0000" in str(error)


def test_the_octal_refusal_says_what_the_neighbouring_addresses_are() -> None:
    """An error a person can act on without opening a manual."""
    with pytest.raises(SlmpRadixDigitError) as caught:
        parse_address("Y8", FX5U)
    message = str(caught.value)
    assert "there is no Y8" in message
    assert "after Y7 comes Y10 (wire number 8)" in message
    assert "after Y17 comes Y20 (wire number 16)" in message


# ======================================================================================
# The four parse traps of DESIGN section 6, U3
# ======================================================================================


def test_x1f_is_a_parse_error_on_iq_f_and_an_address_on_iq_r() -> None:
    with pytest.raises(SlmpRadixDigitError) as caught:
        parse_address("X1F", FX5U)
    assert caught.value.character == "F"
    assert caught.value.position == 2
    assert "iQ-R" in str(caught.value)
    assert parse_address("X1F", IQ_R).index == 31


def test_xfff_and_x0fff_are_the_same_address_on_iq_r() -> None:
    """Leading zeros are padding, not a different radix."""
    assert parse_address("XFFF", IQ_R).index == 4095
    assert parse_address("X0FFF", IQ_R).index == 4095
    assert parse_address("X0000FFF", IQ_R).index == 4095


def test_d100x_names_the_offending_character_and_its_position() -> None:
    with pytest.raises(SlmpRadixDigitError) as caught:
        parse_address("D100x", FX5U)
    error = caught.value
    assert error.character == "X"
    assert error.position == 4
    assert error.device == "D"
    assert "position 4" in str(error)


@pytest.mark.parametrize("literal", ["D0", "M0", "X0", "Y0", "M100", "D7999", "R0"])
@pytest.mark.parametrize("profile", PROFILES, ids=["fx5u", "iq_r"])
def test_device_zero_is_an_ordinary_address(literal: str, profile: AddressProfile) -> None:
    """Regression, named: ``PySLMPClient``'s ``assert 0 < start_num < 0xFFF``.

    That one line makes ``D0``, ``M0`` and ``X0`` unreadable when it runs and validates
    nothing at all when Python runs with ``-O``. ``tests/unit/test_layering.py`` forbids
    ``assert`` in the library; this asserts the behaviour it was supposed to protect.
    """
    address = parse_address(literal, profile)
    assert address.index >= 0


# ======================================================================================
# Longest-prefix matching
# ======================================================================================

PREFIX_CASES: tuple[tuple[str, str], ...] = (
    ("SB0", "SB"),
    ("SW1F", "SW"),
    ("SD203", "SD"),
    ("SM400", "SM"),
    ("S100", "S"),
    ("B0", "B"),
    ("D0", "D"),
    ("DX10", "DX"),
    ("DY10", "DY"),
    ("STS5", "STS"),
    ("STN5", "STN"),
    ("TS5", "TS"),
    ("TN5", "TN"),
    ("LCN5", "LCN"),
    ("LZ0", "LZ"),
    ("L0", "L"),
    ("ZR100", "ZR"),
    ("Z1", "Z"),
    ("W1F", "W"),
    ("RD0", "RD"),
    ("R0", "R"),
    ("G0", "G"),
    ("BL0", "BL"),
    ("CN0", "CN"),
    ("CS0", "CS"),
    ("CC0", "CC"),
    ("F0", "F"),
    ("V0", "V"),
)


@pytest.mark.parametrize(("literal", "family"), PREFIX_CASES)
def test_the_longest_device_prefix_wins(literal: str, family: str) -> None:
    """``SB0`` is link special relay 0 and never step relay ``B0``.

    Both readings are well-formed and both reach a real register, which is why a
    shortest-prefix parser answers ``0x0000`` with somebody else's data.
    """
    assert parse_address(literal, IQ_R).type.name == family


def test_every_device_in_the_table_can_be_parsed() -> None:
    """No device family is unreachable because a longer name shadows it."""
    for name, dt in DEVICE_TABLE.items():
        address = parse_address(f"{name}0", IQ_R)
        assert address.type is dt, f"{name}0 parsed as {address.type.name}"


def test_the_prefix_table_really_is_longest_first() -> None:
    """The invariant the matcher depends on, asserted rather than assumed."""
    lengths = [len(prefix) for prefix in PREFIXES_LONGEST_FIRST]
    assert lengths == sorted(lengths, reverse=True)
    assert set(PREFIXES_LONGEST_FIRST) == set(DEVICE_TABLE)


# ======================================================================================
# Refusals: shape, radix, magnitude
# ======================================================================================


@pytest.mark.parametrize(
    "literal",
    ["", "   ", "D", "M", "X", "D 100", "D-1", "D100.5", "D+1", "D_1", "D1_0", "\tD1\n0"],
)
def test_malformed_literals_raise_a_syntax_error(literal: str) -> None:
    """No separators, no sign, no bit suffix, no underscores, no inner whitespace.

    ``int("1_0", 10)`` is 10 and ``int(" 10 ", 10)`` is 10; both would silently accept a
    literal nobody meant. The parse is anchored precisely so that neither reaches ``int``.
    """
    with pytest.raises(SlmpAddressTextError):
        parse_address(literal, FX5U)


@pytest.mark.parametrize("literal", ["Q0", "AB1", "0", "100", "?D0"])
def test_an_unknown_device_family_raises_and_lists_the_known_ones(literal: str) -> None:
    with pytest.raises(SlmpAddressError) as caught:
        parse_address(literal, FX5U)
    assert isinstance(caught.value, SlmpUnknownPrefixError | SlmpAddressTextError)


def test_the_unknown_family_message_names_the_families() -> None:
    with pytest.raises(SlmpUnknownPrefixError) as caught:
        parse_address("Q0", FX5U)
    assert "SB0" in str(caught.value)
    assert "LSTN" in str(caught.value)


def test_a_device_number_wider_than_the_field_is_refused() -> None:
    """The arithmetic ceiling of the field, not a CPU range check.

    Whether this CPU has ``D8000`` is ``profile.check_range``, which takes the span; this
    is only "no frame can express that number".
    """
    assert parse_address(f"D{MAX_DEVICE_INDEX}", IQ_R).index == MAX_DEVICE_INDEX
    with pytest.raises(SlmpDeviceIndexError):
        parse_address(f"D{MAX_DEVICE_INDEX + 1}", IQ_R)
    with pytest.raises(SlmpDeviceIndexError):
        parse_address("D" + "9" * 400, IQ_R)


def test_a_non_string_is_a_type_error() -> None:
    with pytest.raises(TypeError):
        parse_address(100, FX5U)  # type: ignore[arg-type]  # the runtime half of the check


def test_a_profile_that_returns_a_non_radix_is_refused() -> None:
    """The profile is structural, so it may be a stub. The radix is still checked.

    A radix that is not one of the three is the one value that would silently address a
    different register, so it is verified rather than trusted.
    """

    class Broken:
        key = "broken"

        def radix_for(self, dt: DeviceType) -> Radix:
            return 8  # type: ignore[return-value]  # deliberately wrong, checked at runtime

    with pytest.raises(TypeError):
        parse_address("D0", Broken())


# ======================================================================================
# Case
# ======================================================================================


@pytest.mark.parametrize(
    ("written", "canonical"), [("d0", "D0"), ("m100", "M100"), ("y20", "Y20"), ("Sb0", "SB0")]
)
def test_lower_case_literals_are_accepted_and_canonicalised(
    written: str, canonical: str
) -> None:
    """A device literal is a human string; an ASCII wire field is not.

    ``Codec.read_number`` refuses lower case because SH(NA)-080956ENG-M pp.39-41 say to
    use capitalised code and no manual says what a PLC does with anything else. Nothing
    about accepting ``d0`` changes a byte we emit: the byte is the index.
    """
    address = parse_address(written, FX5U)
    assert address.text == canonical
    assert address.source == written
    assert address.index == parse_address(canonical, FX5U).index


def test_x1f_lower_case_is_still_hexadecimal_on_iq_r() -> None:
    assert parse_address("x1f", IQ_R).index == 31


def test_surrounding_whitespace_is_stripped_but_inner_whitespace_is_not() -> None:
    assert parse_address("  D100  ", FX5U).index == 100
    with pytest.raises(SlmpAddressTextError):
        parse_address("D 100", FX5U)


# ======================================================================================
# DeviceAddress itself
# ======================================================================================


def test_an_address_may_not_disagree_with_its_own_text() -> None:
    """The forged-address guard: ``Y20`` beside ``index=20`` is the bug in disguise.

    Every diagnostic in the library prints ``source`` and every frame carries ``index``.
    If those two could disagree, the one failure this module exists to prevent would be
    invisible in exactly the place a person would look for it.
    """
    y = DEVICE_TABLE["Y"]
    assert DeviceAddress(y, 16, "Y20", Radix.OCTAL).index == 16
    with pytest.raises(SlmpAddressTextError):
        DeviceAddress(y, 20, "Y20", Radix.OCTAL)
    with pytest.raises(SlmpAddressTextError):
        DeviceAddress(y, 16, "D20", Radix.OCTAL)
    with pytest.raises(SlmpAddressTextError):
        DeviceAddress(y, 8, "Y8", Radix.OCTAL)


def test_of_renders_the_source_in_the_radix_it_is_given() -> None:
    y = DEVICE_TABLE["Y"]
    assert DeviceAddress.of(y, 16, radix=Radix.OCTAL).text == "Y20"
    assert DeviceAddress.of(y, 16, radix=Radix.HEXADECIMAL).text == "Y10"
    assert DeviceAddress.of(y, 16, radix=Radix.OCTAL).index == 16
    assert DeviceAddress.of(y, 16, radix=Radix.HEXADECIMAL).index == 16


def test_offset_walks_the_wire_numbering_not_the_written_digits() -> None:
    """The second word of a dword point, the next element of a batch, a fold anchor.

    ``Y17`` plus one is ``Y20``, because the arithmetic is on the index and the
    rendering follows.
    """
    address = parse_address("Y17", FX5U)
    assert address.index == 15
    assert address.offset(1).index == 16
    assert address.offset(1).text == "Y20"
    assert address.offset(1).radix is Radix.OCTAL
    assert address.offset(0).text == "Y17"
    assert address.offset(0) == DeviceAddress.of(address.type, 15, radix=Radix.OCTAL)


def test_equality_is_wire_identity_and_not_spelling() -> None:
    """``D0`` and ``D00`` are one register; ``Y20`` octal and ``Y10`` hex are one output.

    A block plan looking for duplicate or overlapping points is asking "does this reach
    the same register", so ``source`` and ``radix`` are excluded from ``==`` and from
    ``hash``. Rendering stays exact: only equality is about the bytes.
    """
    assert parse_address("D0", FX5U) == parse_address("D00", FX5U)
    assert len({parse_address("D0", FX5U), parse_address("D000", FX5U)}) == 1
    y = DEVICE_TABLE["Y"]
    octal = DeviceAddress.of(y, 16, radix=Radix.OCTAL)
    hexadecimal = DeviceAddress.of(y, 16, radix=Radix.HEXADECIMAL)
    assert octal == hexadecimal
    assert octal.text == "Y20"
    assert hexadecimal.text == "Y10"
    assert parse_address("D0", FX5U) != parse_address("M0", FX5U)
    assert parse_address("D0", FX5U) != parse_address("D1", FX5U)


def test_offset_before_the_first_device_raises_rather_than_clamping() -> None:
    with pytest.raises(SlmpDeviceIndexError):
        parse_address("D0", FX5U).offset(-1)


def test_offset_past_the_field_raises_rather_than_wrapping() -> None:
    """Named after ``slmp-rs``'s ``.count() as u8``, which wraps 256 to 0."""
    with pytest.raises(SlmpDeviceIndexError):
        DeviceAddress.of(DEVICE_TABLE["D"], MAX_DEVICE_INDEX, radix=Radix.DECIMAL).offset(1)


def test_an_address_is_frozen() -> None:
    address = parse_address("D100", FX5U)
    with pytest.raises((AttributeError, TypeError)):
        address.index = 200  # type: ignore[misc]  # frozen dataclass, checked at runtime


def test_str_shows_what_the_caller_wrote() -> None:
    assert str(parse_address("y20", FX5U)) == "y20"
    assert parse_address("y20", FX5U).text == "Y20"


# ======================================================================================
# Properties
# ======================================================================================

LITERALS = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=126), min_size=0, max_size=20
)


@given(text=LITERALS)
@pytest.mark.parametrize("profile", PROFILES, ids=["fx5u", "iq_r"])
def test_parse_address_is_total(text: str, profile: AddressProfile) -> None:
    """For any string it returns an address or raises a named refusal. Never a partial one.

    DESIGN section 5.4. The failure mode this excludes is the interesting one: a parser
    that reads ``D100x`` as ``D100`` and drops the rest reaches a real register and is
    answered ``0x0000``.
    """
    try:
        address = parse_address(text, profile)
    except SlmpAddressError:
        return
    assert isinstance(address.index, int)
    assert 0 <= address.index <= MAX_DEVICE_INDEX
    assert address.source == text.strip()


@given(index=st.integers(min_value=0, max_value=0xFFFF))
@pytest.mark.parametrize("name", ["D", "M", "Y", "X", "B", "W", "ZR", "TN"])
@pytest.mark.parametrize("profile", PROFILES, ids=["fx5u", "iq_r"])
def test_format_then_parse_is_the_identity(
    index: int, name: str, profile: AddressProfile
) -> None:
    """Round trip through the human notation, in every radix the profiles give.

    The reason this matters is that ``format_address`` is what a diagnostic, a
    ``describe()`` and an error message print. If it rendered ``Y16`` for wire number 16,
    a user copying it back into their code would address the fifteenth output.
    """
    dt = DEVICE_TABLE[name]
    address = DeviceAddress.of(dt, index, radix=profile.radix_for(dt))
    reparsed = parse_address(format_address(address), profile)
    assert reparsed.index == index
    assert reparsed.type is dt
    assert reparsed.text == address.text
    assert reparsed == address


@given(index=st.integers(min_value=0, max_value=0xFFFF))
def test_the_octal_rendering_never_contains_an_eight_or_a_nine(index: int) -> None:
    rendered = DeviceAddress.of(DEVICE_TABLE["Y"], index, radix=Radix.OCTAL).text
    assert "8" not in rendered
    assert "9" not in rendered
