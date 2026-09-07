"""The subcommand, derived from the three facts that are the subcommand.

JY997D56001-K p.69 prints the bit field as an explicit three-column table: ``0x0001``
selects bit units over word units, ``0x0002`` selects the 4-digit-code / 8-digit-number
device specification over the 2-digit / 6-digit one, and ``0x0080`` says a device memory
extension specification is in use. SH(NA)-080956ENG-M p.35 lists the same values as
opaque constants, which is how they end up hard-coded at call sites in other libraries.

A wrong subcommand does not crash: ``0401`` with ``0000`` against a bit device returns
word-packed data that decodes into plausible booleans, and ``0001`` against a word device
returns nibbles. So the whole of this module's value is that the value cannot disagree
with the request beside it -- which is a statement about call sites, and is asserted here
by pinning all eight derivations to the manual's own table.
"""

from __future__ import annotations

import pytest

from aslmp.wire.codec import SlmpCodecValueError, SpecFormat, Unit
from aslmp.wire.subcommand import (
    BIT_FIELD,
    BIT_UNITS,
    EXTENSION,
    LONG_SPEC,
    SUBCOMMAND_BITS,
    decode_subcommand,
    subcommand,
)

WORD = Unit.WORD
BIT = Unit.BIT
SHORT = SpecFormat.SHORT
LONG = SpecFormat.LONG

# (unit, spec, extension) -> value, from the tables on SH(NA)-080956ENG-M p.35 and
# JY997D56001-K p.69. The FX5 manual offers exactly the six values whose comment says
# "FX5": bare 0002 and 0003 are never listed for that CPU, and an FX5U-32MT/DS on fw
# 1.065 answered 0xC059 to subcommand 0x0002 (measured 2026-09-06).
TABLE: tuple[tuple[Unit, SpecFormat, bool, int, str], ...] = (
    (WORD, SHORT, False, 0x0000, "FX5, Q, L, iQ-R-compatible"),
    (BIT, SHORT, False, 0x0001, "FX5, Q, L, iQ-R-compatible"),
    (WORD, LONG, False, 0x0002, "iQ-R / iQ-L only"),
    (BIT, LONG, False, 0x0003, "iQ-R / iQ-L only"),
    (WORD, SHORT, True, 0x0080, "FX5, extension specification"),
    (BIT, SHORT, True, 0x0081, "FX5, extension specification"),
    (WORD, LONG, True, 0x0082, "FX5, extension specification"),
    (BIT, LONG, True, 0x0083, "FX5, extension specification"),
)


@pytest.mark.parametrize(
    ("unit", "spec", "extension", "expected", "where"),
    TABLE,
    ids=[f"{u.value}-{s.value}-{'ext' if e else 'plain'}" for u, s, e, _, _ in TABLE],
)
def test_the_manuals_subcommand_table(
    unit: Unit, spec: SpecFormat, extension: bool, expected: int, where: str
) -> None:
    assert subcommand(unit, spec, extension) == expected, where


def test_the_function_is_total_over_its_domain() -> None:
    """Eight inputs, eight documented values, no error path and no default."""
    produced = {
        subcommand(unit, spec, extension)
        for unit in Unit
        for spec in SpecFormat
        for extension in (False, True)
    }
    assert produced == {row[3] for row in TABLE}
    assert len(produced) == 8


def test_the_bits_are_independent() -> None:
    """Each flag moves exactly one bit and no other.

    This is the property that makes the derivation trustworthy: if the units bit also
    changed the specification bit, every value in the table above would still be
    reachable and the coupling would be invisible.
    """
    base = subcommand(WORD, SHORT, False)
    assert subcommand(BIT, SHORT, False) ^ base == BIT_UNITS
    assert subcommand(WORD, LONG, False) ^ base == LONG_SPEC
    assert subcommand(WORD, SHORT, True) ^ base == EXTENSION
    assert SUBCOMMAND_BITS == BIT_UNITS | LONG_SPEC | EXTENSION == 0x0083


@pytest.mark.parametrize(("unit", "spec", "extension", "value", "_where"), TABLE)
def test_decode_is_the_inverse(
    unit: Unit, spec: SpecFormat, extension: bool, value: int, _where: str
) -> None:
    """For the simulator, the proxy and ``aslmp explain``, which read somebody else's frame."""
    assert decode_subcommand(value) == (unit, spec, extension)
    assert subcommand(*decode_subcommand(value)) == value


@pytest.mark.parametrize("value", [0x0004, 0x0010, 0x0100, 0x8000, 0xFFFF, 0x0084])
def test_an_undocumented_subcommand_bit_raises_rather_than_being_masked(value: int) -> None:
    """A subcommand we do not fully understand is a response layout we cannot predict.

    Masking the unknown bits away would turn ``0x0084`` into ``0x0080`` and then decode
    whatever came back against the wrong point layout -- plausible numbers from a frame
    nobody parsed.
    """
    with pytest.raises(SlmpCodecValueError) as caught:
        decode_subcommand(value)
    assert BIT_FIELD.manual in str(caught.value)
    assert "report" in str(caught.value)


@pytest.mark.parametrize("value", [-1, 0x10000])
def test_a_subcommand_outside_the_16_bit_field_raises(value: int) -> None:
    with pytest.raises(SlmpCodecValueError):
        decode_subcommand(value)


@pytest.mark.parametrize("value", ["0000", None, 1.0, True])
def test_a_non_integer_subcommand_raises(value: object) -> None:
    with pytest.raises(SlmpCodecValueError):
        decode_subcommand(value)  # type: ignore[arg-type]  # the runtime half of the check


def test_the_arguments_are_typed_rather_than_positional_luck() -> None:
    """Three distinct types, so a call site cannot silently swap them.

    ``subcommand(SpecFormat.LONG, Unit.BIT, False)`` is a type error at check time and a
    ``TypeError`` at run time, not ``0x0003``.
    """
    with pytest.raises(TypeError):
        subcommand(SHORT, WORD, False)  # type: ignore[arg-type]  # deliberately swapped
    with pytest.raises(TypeError):
        subcommand(WORD, SHORT, 1)  # type: ignore[arg-type]  # 1 is not a bool
    with pytest.raises(TypeError):
        subcommand("word", SHORT, False)  # type: ignore[arg-type]  # not a Unit


def test_the_bit_field_cites_the_page_that_decodes_it() -> None:
    """Rule 4: every constant table row cites a manual, a revision and a section."""
    assert BIT_FIELD.manual == "JY997D56001"
    assert BIT_FIELD.revision == "K"
    assert "p.69" in BIT_FIELD.section
