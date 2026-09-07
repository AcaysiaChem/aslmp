"""The field aliases: what a block field is before any CPU is involved.

Three things are worth a test here and the rest is arithmetic.

1. **The static type is exactly ``float`` / ``int`` / ``bool``** (graft G13). The whole
   point of ``Annotated[float, _F32]`` over a class or a ``NewType`` is that
   ``state.setpoint`` needs no cast at the call site, and the only thing that could
   silently undo it is somebody replacing the alias with a wrapper.
2. **A string field's register count includes the NUL word.** ``read_str(length=8)``
   reads four registers and a ``Str(length=8)`` block field reads five, and the
   difference is deliberate: one returns the characters asked for and the other declares
   what the PLC program stores. Getting it wrong reads a neighbouring register into a
   recipe name, or leaves the terminator behind.
3. **A declared encoding that does not exist fails at class definition**, not at the
   first decode of a live response.
"""

from __future__ import annotations

from typing import Annotated, get_args, get_origin

import pytest

from aslmp.blocks.fields import (
    F32,
    F64,
    I16,
    I32,
    STRING_WORDS,
    U16,
    U32,
    WORD_ORDER_PROOF,
    Bit,
    BitSpec,
    FieldOverride,
    NumberSpec,
    Str,
    StringSpec,
    Word,
    at,
    string_words,
)
from aslmp.errors import SlmpBlockLayoutError
from aslmp.wire.citations import Provenance

ALIASES = {
    "Word": (Word, int, 1, "H", "u16"),
    "U16": (U16, int, 1, "H", "u16"),
    "I16": (I16, int, 1, "h", "i16"),
    "U32": (U32, int, 2, "I", "u32"),
    "I32": (I32, int, 2, "i", "i32"),
    "F32": (F32, float, 2, "f", "f32"),
    "F64": (F64, float, 4, "d", "u32"),
}


@pytest.mark.parametrize("name", sorted(ALIASES))
def test_every_numeric_alias_is_annotated_over_its_bare_python_type(name: str) -> None:
    """``F32`` is a ``float`` and nothing else, which is what keeps call sites cast-free."""
    alias, python, words, code, kind = ALIASES[name]
    assert get_origin(alias) is Annotated
    bare, marker = get_args(alias)
    assert bare is python
    assert isinstance(marker, NumberSpec)
    assert (marker.words, marker.struct_code, marker.kind) == (words, code, kind)


def test_bit_is_a_bool_and_costs_no_access_point_of_its_own() -> None:
    bare, marker = get_args(Bit)
    assert bare is bool
    assert isinstance(marker, BitSpec)


def test_a_double_word_field_is_one_access_point_and_an_f64_is_two() -> None:
    """One ``0403`` double-word point is two registers; an f64 needs two of them."""
    _bare, f32 = get_args(F32)
    _bare, f64 = get_args(F64)
    assert (f32.words, f32.points, f32.dword) == (2, 1, True)
    assert (f64.words, f64.points, f64.dword) == (4, 2, True)
    _bare, u16 = get_args(U16)
    assert (u16.words, u16.points, u16.dword) == (1, 1, False)


# ----------------------------------------------------------------------------------------
# Strings
# ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("length", "words"),
    [(1, 1), (2, 2), (3, 2), (4, 3), (7, 4), (8, 5), (9, 5), (20, 11)],
)
def test_a_string_field_reserves_the_nul_register(length: int, words: int) -> None:
    """Two characters per register plus a terminator that needs one of its own.

    ``length=8`` is the case that matters: eight characters fill four registers exactly,
    so the NUL the PLC program's own ``$MOV`` wrote lands in a fifth.
    """
    assert string_words(length) == words
    assert StringSpec(length).words == words
    assert StringSpec(length).points == words
    assert StringSpec(length).struct_code == f"{2 * words}s"


@pytest.mark.parametrize("length", [0, -1, True])
def test_a_string_length_that_is_not_a_positive_count_is_refused(length: object) -> None:
    with pytest.raises(SlmpBlockLayoutError):
        string_words(length)  # type: ignore[arg-type]  # the refusal is the point


def test_an_encoding_this_interpreter_does_not_have_fails_at_declaration() -> None:
    """Not at the first decode of a live response, three months into a deployment."""
    with pytest.raises(SlmpBlockLayoutError, match="not a codec"):
        Str(length=4, encoding="definitely-not-a-codec")


def test_str_and_at_produce_a_field_override_rather_than_a_default() -> None:
    """Both live in the default slot and neither survives class definition."""
    assert at("D100") == FieldOverride(address="D100")
    override = Str(length=6, encoding="latin-1", address="D500")
    assert isinstance(override, FieldOverride)
    assert override.address == "D500"
    assert override.string == StringSpec(6, "latin-1")


def test_str_defaults_to_ascii_and_no_address() -> None:
    override = Str(length=4)
    assert override.address is None
    assert override.string == StringSpec(4, "ascii")


# ----------------------------------------------------------------------------------------
# Provenance
# ----------------------------------------------------------------------------------------


def test_the_word_order_claim_names_the_cpu_and_firmware_it_was_measured_on() -> None:
    """House rule: a wire-affecting decision cites a manual section or a measurement."""
    assert WORD_ORDER_PROOF.provenance is Provenance.LIVE
    assert WORD_ORDER_PROOF.cpu == "FX5U-32MT/DS"
    assert WORD_ORDER_PROOF.firmware == "1.065"
    assert "low word first" in WORD_ORDER_PROOF.note


def test_the_string_word_count_cites_a_manual_and_a_revision() -> None:
    assert STRING_WORDS.provenance is Provenance.MANUAL
    assert STRING_WORDS.manual and STRING_WORDS.revision and STRING_WORDS.section
