"""The layout half: everything a ``@plc_block`` class knows with no CPU in the process.

Not one test in this file constructs a client, opens a socket or names a profile, and
that is the property under test as much as anything asserted below. DESIGN.md section 4.9
splits layout from bind precisely so the part that can be got wrong at your desk fails at
your desk: field order, register offsets, which bits share a window, how wide a string is.

**Field order is load-bearing.** A ``0403`` response is bare data with no framing between
access points, so a layout that got the order wrong decodes a full set of plausible
values out of the wrong registers and the PLC answers ``0x0000`` throughout. That is why
the order is taken from ``dataclasses.fields()``, whose order Python guarantees by
contract, and why it is asserted here rather than assumed.
"""

from __future__ import annotations

import dataclasses
from typing import Annotated

import pytest

from aslmp.blocks.fields import F32, F64, U16, U32, Bit, PlcBlock, Str, at
from aslmp.blocks.layout import (
    BlockLayout,
    FoldWindow,
    compile_layout,
    layout_of,
    lower_anchor,
    plan_folds,
    plc_block,
)
from aslmp.commands.base import WordOrder
from aslmp.errors import SlmpBlockLayoutError

TwoWidths = Annotated[float, *F32.__metadata__, *U16.__metadata__]
"""A field annotated with two aslmp aliases at once, for the refusal test.

Written at module scope because a class body's annotations are resolved against module
globals, so an alias built inside a test function would fail as an unresolvable name
instead of as the mistake under test.
"""


@plc_block(base="D0")
class LoopState:
    """DESIGN.md section 2.7's own example, kept verbatim as the reference case."""

    setpoint: F32
    process_value: F32
    output: F32
    error: F32
    scan: U32
    fault: Bit = at("M100")
    mode: U16 = at("D400")


# ========================================================================================
# The reference layout
# ========================================================================================


def test_the_design_example_lays_out_exactly_as_written() -> None:
    layout = layout_of(LoopState)
    assert isinstance(layout, BlockLayout)
    assert layout.block_name == "LoopState"
    assert layout.base == "D0"
    assert [field.name for field in layout.fields] == [
        "setpoint",
        "process_value",
        "output",
        "error",
        "scan",
        "fault",
        "mode",
    ]
    offsets = {f.name: f.word_offset for f in layout.fields}
    assert offsets == {
        "setpoint": 0,
        "process_value": 2,
        "output": 4,
        "error": 6,
        "scan": 8,
        "fault": None,
        "mode": None,
    }
    assert layout.words_from_base == 10


def test_an_explicitly_addressed_field_does_not_move_the_ones_after_it() -> None:
    """Inserting an at(...) field must not silently shift every auto field past it."""

    @plc_block(base="D0")
    class Mixed:
        first: U16
        elsewhere: U16 = at("D900")
        second: U16
        third: U16

    offsets = {f.name: f.word_offset for f in layout_of(Mixed).fields}
    assert offsets == {"first": 0, "elsewhere": None, "second": 1, "third": 2}


def test_a_string_field_advances_the_cursor_by_its_registers_nul_included() -> None:
    @plc_block(base="D10")
    class Recipe:
        code: U16
        name: str = Str(length=8)
        checksum: U16

    offsets = {f.name: f.word_offset for f in layout_of(Recipe).fields}
    assert offsets == {"code": 0, "name": 1, "checksum": 6}


def test_an_f64_occupies_four_registers_of_the_cursor() -> None:
    @plc_block(base="D0")
    class Wide:
        big: F64
        after: U16

    offsets = {f.name: f.word_offset for f in layout_of(Wide).fields}
    assert offsets == {"big": 0, "after": 4}


def test_the_layout_is_public_frozen_and_printable() -> None:
    """``cls.__layout__`` is the attribute; ``layout_of(cls)`` is the typed door to it."""
    assert layout_of(LoopState) is LoopState.__layout__  # type: ignore[attr-defined]
    layout = layout_of(LoopState)
    with pytest.raises(dataclasses.FrozenInstanceError):
        layout.fields = ()  # type: ignore[misc]  # frozen by construction
    assert "LoopState" in str(layout)
    rows = layout.table()
    assert ("setpoint", "F32", "D0+0", "2") in rows
    assert ("fault", "Bit", "M100", "folded") in rows


def test_field_lookup_names_the_fields_there_are() -> None:
    assert layout_of(LoopState).field("mode").label == "U16"
    with pytest.raises(SlmpBlockLayoutError, match="setpoint"):
        layout_of(LoopState).field("setpiont")


# ========================================================================================
# What the decorator builds
# ========================================================================================


def test_the_decorated_class_is_frozen_slotted_and_keyword_only() -> None:
    state = LoopState(
        setpoint=1.0,
        process_value=2.0,
        output=3.0,
        error=0.5,
        scan=7,
        fault=True,
        mode=2,
    )
    assert state.setpoint == 1.0
    assert state.fault is True
    with pytest.raises(dataclasses.FrozenInstanceError):
        state.setpoint = 2.0  # type: ignore[misc]  # frozen by construction
    assert not hasattr(state, "__dict__")  # slots: no attribute can appear later
    with pytest.raises(TypeError):
        LoopState(1.0, 2.0, 3.0, 0.5, 7, True, 2)  # type: ignore[call-arg]  # kw-only


def test_every_instance_carries_a_tx_field_that_defaults_to_none() -> None:
    """A block you built yourself was not read from a PLC and has no transaction."""
    names = [f.name for f in dataclasses.fields(LoopState)]
    assert names[-1] == "tx"
    state = LoopState(
        setpoint=0.0,
        process_value=0.0,
        output=0.0,
        error=0.0,
        scan=0,
        fault=False,
        mode=0,
    )
    # ``tx`` is injected by the decorator, so a type checker reading the class body
    # cannot see it; ``dataclasses.fields`` can, and that is the door a caller who wants
    # it statically typed uses by declaring ``tx`` themselves (the next test).
    values = {f.name: getattr(state, f.name) for f in dataclasses.fields(state)}
    assert values["tx"] is None


def test_a_class_that_declares_its_own_tx_keeps_it() -> None:
    """So a caller who wants the field statically typed can say so themselves."""

    @plc_block(base="D0")
    class WithTx:
        value: U16
        tx: object = None

    assert [f.name for f in dataclasses.fields(WithTx)] == ["value", "tx"]
    assert "tx" not in {f.name for f in layout_of(WithTx).fields}


def test_the_optional_base_class_declares_tx_and_costs_nothing() -> None:
    """``PlcBlock`` is how a caller gets ``state.tx`` past a type checker.

    A base class without ``__slots__`` would give every block instance a ``__dict__`` --
    one dictionary per cycle in the hot path -- so the empty tuple is load-bearing, and
    the annotation alone must not become a second field.
    """

    @plc_block(base="D0")
    class Typed(PlcBlock):
        value: U16

    assert [f.name for f in dataclasses.fields(Typed)] == ["value", "tx"]
    assert [f.name for f in layout_of(Typed).fields] == ["value"]
    instance = Typed(value=3)
    assert instance.tx is None
    assert not hasattr(instance, "__dict__")


def test_the_markers_never_survive_class_definition() -> None:
    """A left-behind at(...) would become the field's default value.

    ``hasattr`` cannot see it either way -- ``slots=True`` puts a descriptor of the same
    name on the class -- so the assertion that matters is the dataclass field's default,
    and the constructor refusing to build an instance without one.
    """
    defaults = {f.name: f.default for f in dataclasses.fields(LoopState)}
    assert defaults["fault"] is dataclasses.MISSING
    assert defaults["mode"] is dataclasses.MISSING
    with pytest.raises(TypeError):
        LoopState(  # type: ignore[call-arg]  # the missing fields are the point
            setpoint=1.0, process_value=1.0, output=1.0, error=1.0, scan=1
        )


# ========================================================================================
# The refusals a declaration alone can earn
# ========================================================================================


def test_a_field_with_no_aslmp_alias_is_refused_by_name() -> None:
    with pytest.raises(SlmpBlockLayoutError, match="no default width"):

        @plc_block(base="D0")
        class Untyped:
            value: int


def test_a_bit_field_with_no_address_is_refused() -> None:
    """A base cursor counts registers, so no offset from it can reach a bit device."""
    with pytest.raises(SlmpBlockLayoutError, match=r"at\(\"M100\"\)"):

        @plc_block(base="D0")
        class Floating:
            flag: Bit


def test_a_plain_default_value_is_refused() -> None:
    """Every field is written by the response, so a default is a value nothing reads."""
    with pytest.raises(SlmpBlockLayoutError, match="default value"):

        @plc_block(base="D0")
        class Defaulted:
            mode: U16 = 3  # the refusal is the point


def test_two_aliases_on_one_field_are_refused() -> None:
    with pytest.raises(SlmpBlockLayoutError, match="more than one"):

        @plc_block(base="D0")
        class Confused:
            value: TwoWidths


def test_a_str_field_annotated_as_something_else_is_refused() -> None:
    with pytest.raises(SlmpBlockLayoutError, match="must be `str`"):

        @plc_block(base="D0")
        class Wrong:
            name: int = Str(length=4)  # noqa: RUF100


def test_an_alias_field_given_a_str_default_is_refused() -> None:
    with pytest.raises(SlmpBlockLayoutError, match="Str"):

        @plc_block(base="D0")
        class Wrong:
            value: F32 = Str(length=4)


def test_auto_addressed_fields_with_no_base_are_refused() -> None:
    with pytest.raises(SlmpBlockLayoutError, match="no base was given"):

        @plc_block()
        class Homeless:
            value: U16


def test_a_block_of_only_addressed_fields_needs_no_base() -> None:
    @plc_block()
    class Scattered:
        a: U16 = at("D100")
        b: Bit = at("M0")

    assert layout_of(Scattered).base is None
    assert layout_of(Scattered).auto_fields == ()


def test_an_empty_block_is_refused() -> None:
    """A 0403 with no access points is answered 0xC052, measured."""
    with pytest.raises(SlmpBlockLayoutError, match="no fields"):

        @plc_block(base="D0")
        class Empty:
            pass


def test_layout_of_refuses_a_class_that_was_never_decorated() -> None:
    class Plain:
        value: U16

    with pytest.raises(SlmpBlockLayoutError, match="not a @plc_block class"):
        layout_of(Plain)


def test_an_unresolvable_annotation_is_refused_with_the_reason() -> None:
    """The layout is computed from the annotations, so they have to be resolvable."""

    @dataclasses.dataclass(frozen=True, kw_only=True)
    class Broken:
        value: NoSuchAlias  # type: ignore[name-defined]  # noqa: F821

    with pytest.raises(SlmpBlockLayoutError, match="cannot be resolved"):
        compile_layout(Broken, base="D0")


def test_the_word_order_a_block_declares_is_kept_for_bind_to_judge() -> None:
    @plc_block(base="D0", word_order=WordOrder.HIGH_FIRST)
    class Reversed:
        value: U16

    assert layout_of(Reversed).word_order is WordOrder.HIGH_FIRST
    assert layout_of(LoopState).word_order is None


# ========================================================================================
# Folding: pure integer arithmetic
# ========================================================================================


def test_eight_adjacent_bits_fold_into_one_window() -> None:
    """The whole reason folding exists: eight status bits cost one point, not eight."""
    windows = plan_folds([(f"b{i}", 100 + i) for i in range(8)])
    assert len(windows) == 1
    assert windows[0].anchor == 100
    assert windows[0].bits == tuple((f"b{i}", i) for i in range(8))
    assert windows[0].span == 16


def test_a_window_is_anchored_at_the_lowest_named_bit_not_a_multiple_of_sixteen() -> None:
    """So the folded address is one the caller can recognise in a diagnostic."""
    (window,) = plan_folds([("late", 107), ("early", 100)])
    assert window.anchor == 100
    assert dict(window.bits) == {"late": 7, "early": 0}


def test_bits_more_than_fifteen_apart_need_two_windows() -> None:
    windows = plan_folds([("a", 0), ("b", 15), ("c", 16), ("d", 31)])
    assert [w.anchor for w in windows] == [0, 16]
    assert [dict(w.bits) for w in windows] == [{"a": 0, "b": 15}, {"c": 0, "d": 15}]


def test_a_window_keeps_the_declaration_order_of_the_fields_inside_it() -> None:
    """``describe()`` prints the class body's order, not the wire's."""
    (window,) = plan_folds([("high", 105), ("low", 100), ("middle", 102)])
    assert [name for name, _bit in window.bits] == ["high", "low", "middle"]
    assert dict(window.bits) == {"high": 5, "low": 0, "middle": 2}


def test_folding_is_greedy_from_the_lowest_bit_and_uses_as_few_windows_as_fit() -> None:
    windows = plan_folds([("a", 0), ("b", 20), ("c", 21), ("d", 40)])
    assert [w.anchor for w in windows] == [0, 20, 40]


def test_a_window_inside_the_range_is_left_exactly_where_it_was() -> None:
    window = FoldWindow(100, (("a", 0), ("b", 7)))
    assert lower_anchor(window, device="M", last=32767) is window


def test_a_family_with_no_static_range_is_left_alone() -> None:
    """``G`` depends on which module is mounted; there is nothing to slide against."""
    window = FoldWindow(100, (("a", 0),))
    assert lower_anchor(window, device="G", last=None) is window


def test_a_window_at_the_end_of_the_family_is_lowered_and_says_so() -> None:
    """M ends at M32767 on an FX5U, and the whole 16-point window is read."""
    window = FoldWindow(32760, (("a", 0), ("b", 7)))
    fitted = lower_anchor(window, device="M", last=32767)
    assert fitted.anchor == 32752
    assert dict(fitted.bits) == {"a": 8, "b": 15}


def test_a_single_bit_at_the_very_last_point_still_folds() -> None:
    fitted = lower_anchor(FoldWindow(32767, (("a", 0),)), device="M", last=32767)
    assert fitted.anchor == 32752
    assert dict(fitted.bits) == {"a": 15}


def test_a_bit_past_the_end_of_the_family_is_refused_by_the_fold() -> None:
    """And the refusal names the fold rather than a bare address, per DESIGN 2.7."""
    window = FoldWindow(32768, (("a", 0),))
    with pytest.raises(SlmpBlockLayoutError, match="16-point window"):
        lower_anchor(window, device="M", last=32767)
