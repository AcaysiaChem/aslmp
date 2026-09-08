"""The field aliases: what a block field is before any CPU is involved.

Four things are worth a test here and the rest is arithmetic.

1. **The static type is exactly ``float`` / ``int`` / ``bool``** (graft G13). The whole
   point of ``Annotated[float, _F32]`` over a class or a ``NewType`` is that
   ``state.setpoint`` needs no cast at the call site, and the only thing that could
   silently undo it is somebody replacing the alias with a wrapper. The *static* half of
   that is asserted where it can be -- ``tests/typing/consumer.py`` says
   ``assert_type(state.setpoint, float)`` and ``mypy`` fails the build if it is not --
   and the run-time half is asserted here: the bare type of every alias is a subclass of
   the bare Python type, never a wrapper that would arrive at a call site instead of it.

   The two halves are not identical, and the difference is deliberate.
   ``F32(minimum=..., maximum=...)`` has to *evaluate*, and ``Annotated[X, ...](...)``
   calls ``X``; ``float(minimum=...)`` is a ``TypeError``, so at run time the bare type
   is a private ``float`` subclass whose constructor declares bounds. It is never
   instantiated as a number.
2. **A string field's register count includes the NUL word.** ``read_str(length=8)``
   reads four registers and a ``Str(length=8)`` block field reads five, and the
   difference is deliberate: one returns the characters asked for and the other declares
   what the PLC program stores. Getting it wrong reads a neighbouring register into a
   recipe name, or leaves the terminator behind.
3. **A declared encoding that does not exist fails at class definition**, not at the
   first decode of a live response.
4. **A nonsensical bound is refused where it is written**, for the same reason: a
   ``minimum`` above a ``maximum``, a NaN end, or a bound the field's own width cannot
   reach are all mistakes in the source file, and every one of them would otherwise be
   discovered as "this read always raises" or "this read never raises" months later.
"""

from __future__ import annotations

import struct
from typing import Annotated, Any, get_args, get_origin

import pytest

from aslmp.blocks.fields import (
    F32,
    F64,
    I16,
    I32,
    IMPLAUSIBLE_VALUE_FINDING,
    STRING_WORDS,
    U16,
    U32,
    WORD_ORDER_PROOF,
    Bit,
    BitSpec,
    Bounds,
    FieldOverride,
    NumberSpec,
    Str,
    StringSpec,
    Word,
    at,
    outside,
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
    """``F32`` is a ``float`` and nothing else, which is what keeps call sites cast-free.

    ``is a float`` rather than ``is float`` at run time: the bare type is the subclass
    that makes ``F32(minimum=..., maximum=...)`` evaluate. Every run-time question anyone
    asks of it -- ``issubclass``, ``isinstance`` of a decoded value, arithmetic -- has
    the same answer it had when it was ``float`` itself, and ``mypy`` sees ``float``.
    """
    alias, python, words, code, kind = ALIASES[name]
    assert get_origin(alias) is Annotated
    bare, marker = get_args(alias)
    assert issubclass(bare, python)
    assert bare.__name__ == f"_{name}Declaration"
    assert isinstance(marker, NumberSpec)
    assert (marker.words, marker.struct_code, marker.kind) == (words, code, kind)
    assert marker.bounds is None, "an alias promises nothing until asked to"


@pytest.mark.parametrize("name", sorted(ALIASES))
def test_the_declaration_type_is_never_instantiated_as_a_number(name: str) -> None:
    """Calling an alias declares bounds; it does not build a value of that width.

    The whole reason the bare type is a subclass rather than ``float`` itself. A field's
    value comes from ``struct.unpack`` and is an ordinary ``float`` or ``int``; nothing
    in the library ever constructs one of these classes to hold data.
    """
    alias, _python, _words, _code, _kind = ALIASES[name]
    bare, _marker = get_args(alias)
    made = bare(minimum=1, maximum=2)
    assert isinstance(made, NumberSpec)
    assert not isinstance(made, bare)


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
# Bounds, as declarations. What they do to a live read is tests/unit/test_blocks_bounds.py
# ----------------------------------------------------------------------------------------


def declare(alias: Any, **bounds: Any) -> NumberSpec:
    """Call one alias the way the README documents, from a file ``mypy`` also checks.

    Real code writes ``F32(minimum=0.0, maximum=1.0e7)`` in the metadata position of an
    ``Annotated``, which a type checker does not analyse -- that is what keeps the bare
    type, and therefore ``state.scan``, exactly ``float``. Written bare in an expression
    the same call *is* analysed, as ``float(minimum=...)``, so these tests reach the
    alias through an ``Any``. Same object, same call, same result; only the checker's
    view of the expression differs, and ``test_a_bounded_field_reads_back_as_a_float``
    in ``test_blocks_bounds.py`` covers the annotation form itself.
    """
    made = alias(**bounds)
    assert isinstance(made, NumberSpec)
    return made


def test_the_documented_spelling_produces_a_bounded_field() -> None:
    """``F32(minimum=0.0, maximum=1.0e7)`` -- the exact call the README documents."""
    spec = declare(F32, minimum=0.0, maximum=1.0e7)
    assert spec.label == "F32"
    assert (spec.words, spec.struct_code, spec.kind) == (2, "f", "f32")
    assert spec.bounds == Bounds(0.0, 1.0e7)


@pytest.mark.parametrize("name", sorted(ALIASES))
def test_every_numeric_alias_takes_the_same_two_keywords(name: str) -> None:
    """Bounds are not an ``F32`` feature: a scan counter declared ``U32`` needs them too."""
    alias, _python, words, code, kind = ALIASES[name]
    _bare, marker = get_args(alias)
    spec = declare(alias, minimum=1, maximum=2)
    assert (spec.label, spec.words, spec.struct_code, spec.kind) == (
        marker.label,
        words,
        code,
        kind,
    )
    assert spec.bounds == Bounds(1, 2)


def test_one_end_is_enough() -> None:
    """A tank level has a floor and no ceiling worth naming; that is a whole declaration."""
    assert declare(F32, minimum=0.0).bounds == Bounds(0.0, None)
    assert declare(F32, maximum=100.0).bounds == Bounds(None, 100.0)


def test_an_alias_called_with_no_bounds_at_all_is_refused() -> None:
    """``F32()`` promises nothing, which is what the bare alias ``F32`` already says."""
    with pytest.raises(SlmpBlockLayoutError, match="spelled `F32`"):
        declare(F32)


def test_a_minimum_above_its_maximum_is_refused_where_it_is_written() -> None:
    with pytest.raises(SlmpBlockLayoutError, match="no value can ever be inside"):
        declare(F32, minimum=5.0, maximum=1.0)


@pytest.mark.parametrize("end", ["minimum", "maximum"])
def test_a_nan_bound_is_refused_rather_than_never_firing(end: str) -> None:
    """NaN compares false against everything, so a NaN bound is a bound that is off."""
    with pytest.raises(SlmpBlockLayoutError, match="never fire"):
        declare(F32, **{end: float("nan")})


@pytest.mark.parametrize(
    ("alias", "bounds"),
    [
        (U16, {"maximum": 70000}),
        (U16, {"minimum": -1}),
        (I16, {"minimum": -40000}),
        (U32, {"maximum": 2**32}),
        (I32, {"maximum": 2**31}),
        (F32, {"maximum": 1.0e300}),
    ],
)
def test_a_bound_the_field_cannot_reach_is_refused(
    alias: Any, bounds: dict[str, float]
) -> None:
    """``U16(maximum=70000)`` can never fire; ``U16(minimum=70000)`` always does."""
    with pytest.raises(SlmpBlockLayoutError, match="outside what a"):
        declare(alias, **bounds)


def test_a_bound_that_is_not_a_number_is_refused() -> None:
    with pytest.raises(SlmpBlockLayoutError, match="is a number, not str"):
        declare(F32, minimum="0.0")


def test_bounds_render_both_ends_and_say_which_is_missing() -> None:
    """``describe()`` prints this; "no maximum" beats an empty half of a range."""
    assert str(Bounds(0.0, 1.0e7)) == "[0.0 .. 10000000.0]"
    assert str(Bounds(0.0, None)) == "[0.0 .. no maximum]"
    assert str(Bounds(None, 1.0)) == "[no minimum .. 1.0]"
    assert Bounds(None, None).declared is False
    assert Bounds(0.0, None).declared is True


@pytest.mark.parametrize(
    ("value", "minimum", "maximum", "expected"),
    [
        (5.0, 0.0, 10.0, False),
        (0.0, 0.0, 10.0, False),
        (10.0, 0.0, 10.0, False),
        (-0.001, 0.0, 10.0, True),
        (10.001, 0.0, 10.0, True),
        (1e30, 0.0, None, False),
        (-1.0, 0.0, None, True),
        (1e30, None, 10.0, True),
        (float("nan"), 0.0, 10.0, True),
        (float("inf"), 0.0, 10.0, True),
        (float("-inf"), None, 10.0, False),
    ],
)
def test_outside_is_inclusive_at_both_ends_and_excludes_nan(
    value: float, minimum: float | None, maximum: float | None, expected: bool
) -> None:
    """A declared range includes its ends. A NaN is inside no range anybody meant."""
    assert outside(value, minimum, maximum) is expected
    assert Bounds(minimum, maximum).excludes(value) is expected


# ----------------------------------------------------------------------------------------
# Provenance
# ----------------------------------------------------------------------------------------


def test_the_word_order_claim_names_the_cpu_and_firmware_it_was_measured_on() -> None:
    """House rule: a wire-affecting decision cites a manual section or a measurement."""
    assert WORD_ORDER_PROOF.provenance is Provenance.LIVE
    assert WORD_ORDER_PROOF.cpu == "FX5U-32MT/DS"
    assert WORD_ORDER_PROOF.firmware == "1.065"
    assert "low word first" in WORD_ORDER_PROOF.note


def test_the_implausible_value_finding_names_the_cpu_the_firmware_and_the_number() -> None:
    """House rule: a hardware-derived claim cites the CPU and firmware it came off.

    The bounds feature is arithmetic, not a wire decision -- but the reason it exists is
    a measurement, and the number in it is the one an outside reviewer was handed.
    """
    assert IMPLAUSIBLE_VALUE_FINDING.provenance is Provenance.LIVE
    assert IMPLAUSIBLE_VALUE_FINDING.cpu == "FX5U-32MT/DS"
    assert IMPLAUSIBLE_VALUE_FINDING.firmware == "1.065"
    note = IMPLAUSIBLE_VALUE_FINDING.note
    assert "1226168560" in note
    assert "IO_Scan := IO_Scan + 1.0" in note
    assert struct.unpack("<I", struct.pack("<f", 613775.0))[0] == 1226168560


def test_the_string_word_count_cites_a_manual_and_a_revision() -> None:
    assert STRING_WORDS.provenance is Provenance.MANUAL
    assert STRING_WORDS.manual and STRING_WORDS.revision and STRING_WORDS.section


def test_a_string_length_is_bytes_and_a_double_byte_codec_is_refused_not_truncated() -> None:
    """``Str(length=n)`` reserves n BYTES, and the two units differ for shift_jis.

    The word "characters" survived in this module longer than anywhere else in the package
    because it is correct for ``ascii``, which every example uses. It is wrong for any
    double-byte codec: ``Str(length=4, encoding="shift_jis")`` holds two Japanese
    characters, not four.

    What makes this a naming defect rather than a wrong-data one -- and the reason it is
    pinned here rather than fixed by widening the field -- is the second half: the write
    path refuses the overflow and says so. Nothing is truncated, so a recipe name cannot be
    silently cut in half on its way to the plant.

    The docstring assertion checks the FIRST LINE only. An earlier version of this test
    looked for "BYTES" anywhere in the docstring, and passed after the summary line was
    reverted to "characters" because the word still appeared further down -- a test that
    could not fail, which is the defect this whole file exists to prevent.
    """
    from aslmp.blocks.fields import StringSpec, string_words
    from aslmp.blocks.plan import _string_bytes

    spec = StringSpec(length=4, encoding="shift_jis")
    capacity = 2 * spec.words - 1
    assert (spec.words, capacity) == (3, 5), "three registers, minus the NUL byte"

    # A single-byte codec is where the two units coincide, which is why this went unnoticed.
    assert len(_string_bytes("ABCD", "name", spec)) == 2 * spec.words

    two_japanese = "あい"
    assert len(two_japanese.encode("shift_jis")) == 4 <= capacity
    assert len(_string_bytes(two_japanese, "name", spec)) == 2 * spec.words

    four_japanese = "あいうえ"
    assert len(four_japanese.encode("shift_jis")) == 8 > capacity
    with pytest.raises(SlmpBlockLayoutError, match="capacity is in BYTES"):
        _string_bytes(four_japanese, "name", spec)

    # "characters" plural is the UNIT. "character-string" is the field's type name and
    # is correct MELSEC vocabulary, so the check is deliberately on the plural.
    docs = (("StringSpec", StringSpec.__doc__), ("string_words", string_words.__doc__))
    for what, doc in docs:
        summary = (doc or "").strip().splitlines()[0].lower()
        assert "byte" in summary, f"{what} summary must name the unit: {summary!r}"
        assert "characters" not in summary, f"{what} summary still says characters"
