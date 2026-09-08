"""Layer 2 -- the layout of a ``@plc_block`` class, computed once and free of I/O.

**Pure.** Everything here runs at class-definition time, knows nothing about a profile,
a connection or a socket, and is unit-testable with no PLC anywhere in the process
(``tests/unit/test_layering.py`` proves the last part in a subprocess). That split is the
point of DESIGN.md section 4.9: a layout computed at import cannot know which CPU it will
be read from, so it computes only what is true of the *declaration* -- field order, per
field width and decoder, which bits share a window, where each field's bytes land in the
response -- and leaves every question that needs a CPU to
:func:`aslmp.blocks.plan.bind`.

**Why this is worth a module of its own.** Field order is load-bearing: the ``0403``
response is bare data with no framing between access points, so a layout that got the
order wrong would decode a full set of plausible values from the wrong registers with end
code ``0x0000``. Python guarantees ``dataclasses.fields()`` order by contract, which is
why the layout is derived from it rather than from any reflection API that does not
promise one.

**Bit folding is decided here and finished at bind.** Which declared bits *could* share a
16-point window is a property of the declaration; which window they *do* share is a
property of the CPU, because ``M100`` is not an index until a profile says what base its
digits are in and the window's anchor may have to be lowered to fit the device's range.
So this module computes the grouping arithmetic -- :func:`plan_folds` and
:func:`lower_anchor`, both pure functions over integers -- and ``bind()`` calls them with
the indices a profile resolved. The fold is never hidden: the anchor it chose is printed
by ``plan.describe()``, carried in the transaction record and quoted in any range error.
"""

from __future__ import annotations

import dataclasses
import inspect
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, TypeVar, dataclass_transform, get_type_hints

from aslmp.blocks.fields import (
    AddressLike,
    BitSpec,
    BlockTransaction,
    Bounds,
    FieldOverride,
    FieldSpec,
    NumberSpec,
    Str,
    StringSpec,
    at,
)
from aslmp.commands.base import WordOrder
from aslmp.errors import SlmpBlockLayoutError
from aslmp.wire.citations import Citation, Measurement, Source

__all__ = [
    "BIT_WINDOW",
    "FOLD_ANCHOR",
    "BitFold",
    "BlockLayout",
    "FieldPlan",
    "FoldWindow",
    "cites",
    "compile_layout",
    "layout_of",
    "lower_anchor",
    "plan_folds",
    "plc_block",
]

T = TypeVar("T")

BIT_WINDOW: Final = Citation(
    manual="SH(NA)-080956ENG",
    revision="M",
    section="section 6.4 p.54",
    note=(
        "One word access point on a bit device is 16 consecutive bits with the named "
        "device as the LEAST significant bit. That is what makes a fold possible at "
        "all: eight status bits declared across M100-M107 are one access point, and "
        "bit n of the returned word is the device at anchor + n."
    ),
)

FOLD_ANCHOR: Final = Measurement(
    cpu="FX5U-32MT/DS",
    firmware="1.065",
    date="2026-09-06",
    note=(
        "The 0403 point ceiling is 192 on the built-in port: 193 points returned "
        "0xC054. Folding is what keeps a block of status bits inside that budget, so "
        "the anchor it picks is printed rather than assumed -- the whole 16-point "
        "window is range-checked at bind, and a window that would run off the end of "
        "the device is lowered only if every named bit still falls inside it."
    ),
)

_TX_FIELD: Final = "tx"
"""The one field ``@plc_block`` adds. Never a wire point; never part of the layout."""


# ========================================================================================
# The layout value types
# ========================================================================================


@dataclass(frozen=True, slots=True)
class FieldPlan:
    """One declared field: what it is, where it is, and how wide.

    ``address`` is the literal the caller gave :func:`~aslmp.blocks.fields.at`, or
    ``None`` for a field addressed from the block's running base. ``word_offset`` is that
    running position, in **registers** from the base, and it is ``None`` for an
    explicitly addressed field -- an ``at()`` field does not consume the cursor, so
    inserting one does not silently move every field after it.
    """

    name: str
    spec: FieldSpec
    address: AddressLike | None = None
    word_offset: int | None = None

    @property
    def words(self) -> int:
        """Registers this field occupies. ``0`` for a bit, which folds into a window."""
        if isinstance(self.spec, BitSpec):
            return 0
        return self.spec.words

    @property
    def points(self) -> int:
        """Access points this field costs on its own. ``0`` for a bit."""
        if isinstance(self.spec, BitSpec):
            return 0
        return self.spec.points

    @property
    def dword(self) -> bool:
        """Whether this field is read as double-word access points."""
        return isinstance(self.spec, NumberSpec) and self.spec.dword

    @property
    def label(self) -> str:
        """``"F32"``, ``"Bit"``, ``"Str(8)"`` -- what ``describe()`` prints.

        A bounded field keeps the plain label. The range is a column of its own in
        ``describe()``, because a Mitsubishi engineer reading that table is looking down
        the type column for the one that disagrees with a global label.
        """
        if isinstance(self.spec, StringSpec):
            return f"Str({self.spec.length})"
        return self.spec.label

    @property
    def bounds(self) -> Bounds | None:
        """The plausibility range this field promises, or ``None`` for most fields."""
        spec = self.spec
        return spec.bounds if isinstance(spec, NumberSpec) else None

    def __str__(self) -> str:
        where = self.address if self.address is not None else f"base+{self.word_offset}"
        return f"{self.name}: {self.label} @ {where}"


@dataclass(frozen=True, slots=True)
class FoldWindow:
    """One 16-point window and the declared bits inside it, as pure integers.

    Produced by :func:`plan_folds` from resolved device indices and turned into a
    :class:`BitFold` by ``bind()`` once the anchor has been checked against the CPU's
    range. ``bits`` is ``(field name, bit position within the window)``, and the bit
    position is ``index - anchor`` because the named device is the window's least
    significant bit (:data:`BIT_WINDOW`).
    """

    anchor: int
    bits: tuple[tuple[str, int], ...]

    @property
    def span(self) -> int:
        """Always 16 points. A window is a word access point on a bit device."""
        return 16


@dataclass(frozen=True, slots=True)
class BitFold:
    """One folded window as it will go on the wire, with the address it resolved to.

    Carried by the bound plan, printed by ``describe()``, and quoted verbatim in a range
    failure -- DESIGN.md section 2.7 requires the fold be named in all three places, so
    that "eight bits cost one point" never becomes "the library read a register you did
    not ask for".
    """

    device: str
    anchor_index: int
    anchor_text: str
    bits: tuple[tuple[str, int], ...]
    lowered: bool = False

    @property
    def names(self) -> tuple[str, ...]:
        """The declared field names in this window, in declaration order."""
        return tuple(name for name, _bit in self.bits)

    def __str__(self) -> str:
        note = " (anchor lowered to fit the device range)" if self.lowered else ""
        inside = ", ".join(f"{name}=bit {bit}" for name, bit in self.bits)
        return f"{self.anchor_text} +16 points: {inside}{note}"


@dataclass(frozen=True, slots=True)
class BlockLayout:
    """Everything a ``@plc_block`` class says about itself, with no CPU in sight.

    Public, frozen and printable: ``LoopState.__layout__`` is part of the API, because
    the field table is the first thing anybody asks to see when a block reads the wrong
    registers.
    """

    block_name: str
    fields: tuple[FieldPlan, ...]
    base: AddressLike | None = None
    word_order: WordOrder | None = None

    def __post_init__(self) -> None:
        if not self.fields:
            raise SlmpBlockLayoutError(
                f"@plc_block class {self.block_name} declares no fields. An empty "
                f"request is not a cheap one: a 0403 with zero access points is "
                f"answered 0xC052 on FX5U-32MT/DS fw 1.065 (measured 2026-09-06)."
            )

    # -- derived views -------------------------------------------------------

    @property
    def bit_fields(self) -> tuple[FieldPlan, ...]:
        """The fields that fold into windows, in declaration order."""
        return tuple(f for f in self.fields if isinstance(f.spec, BitSpec))

    @property
    def auto_fields(self) -> tuple[FieldPlan, ...]:
        """The fields addressed from the running base, in declaration order."""
        return tuple(f for f in self.fields if f.address is None)

    @property
    def words_from_base(self) -> int:
        """Registers the auto-addressed run occupies, base included."""
        return sum(f.words for f in self.auto_fields)

    def field(self, name: str) -> FieldPlan:
        """One field by name, or a refusal that lists the names there are."""
        for plan in self.fields:
            if plan.name == name:
                return plan
        known = ", ".join(f.name for f in self.fields)
        raise SlmpBlockLayoutError(
            f"{self.block_name} has no field {name!r}. Its fields are: {known}."
        )

    def table(self) -> tuple[tuple[str, str, str, str], ...]:
        """``(name, type, where, words)`` rows, for ``plan.describe()`` and ``repr``."""
        rows: list[tuple[str, str, str, str]] = []
        for plan in self.fields:
            where = (
                str(plan.address)
                if plan.address is not None
                else f"{self.base}+{plan.word_offset}"
            )
            words = "folded" if isinstance(plan.spec, BitSpec) else str(plan.words)
            rows.append((plan.name, plan.label, where, words))
        return tuple(rows)

    def __str__(self) -> str:
        return f"{self.block_name}({len(self.fields)} field(s), base={self.base})"


# ========================================================================================
# Folding -- pure integer arithmetic, so it is testable without a CPU
# ========================================================================================


def plan_folds(items: Sequence[tuple[str, int]]) -> tuple[FoldWindow, ...]:
    """Group ``(field name, device index)`` pairs into as few 16-point windows as fit.

    Greedy from the lowest index, which is optimal for fixed-width covering: anchor the
    window at the lowest index not yet covered and take every named bit inside
    ``[anchor, anchor + 16)``. The anchor is therefore always a device the caller named,
    which is what makes the folded address recognisable in a diagnostic rather than an
    arbitrary multiple of 16.

    ``bits`` keeps the caller's declaration order inside each window, so ``describe()``
    prints the fields in the order the class body lists them.
    """
    order = {name: position for position, (name, _index) in enumerate(items)}
    remaining = sorted(items, key=lambda item: (item[1], order[item[0]]))
    windows: list[FoldWindow] = []
    cursor = 0
    while cursor < len(remaining):
        anchor = remaining[cursor][1]
        inside: list[tuple[str, int]] = []
        while cursor < len(remaining) and remaining[cursor][1] < anchor + 16:
            name, index = remaining[cursor]
            inside.append((name, index - anchor))
            cursor += 1
        inside.sort(key=lambda pair: order[pair[0]])
        windows.append(FoldWindow(anchor, tuple(inside)))
    return tuple(windows)


def lower_anchor(window: FoldWindow, *, device: str, last: int | None) -> FoldWindow:
    """Slide a window down so its 16 points end at ``last``, or raise saying why not.

    A window anchored at a device the caller named can run off the end of the family --
    ``M7679`` on an FX5U leaves five points, not sixteen -- and the whole window is
    read, so the whole window has to exist. The anchor is lowered to ``last - 15``
    **only if** every named bit still falls inside it; otherwise this raises, at bind,
    with the fold named (:data:`FOLD_ANCHOR`).

    ``last is None`` means the profile publishes no static range for the family, so
    there is nothing to slide against and the window is returned unchanged.
    """
    if last is None or window.anchor + 15 <= last:
        return window
    lowered = last - 15
    highest = window.anchor + max(bit for _name, bit in window.bits)
    if lowered < 0 or highest > last:
        raise SlmpBlockLayoutError(
            f"the bit window folding {_names(window)} cannot fit inside {device}: the "
            f"named bits run from {device}{window.anchor} to {device}{highest} and the "
            f"family ends at {device}{last}, so no 16-point window holds all of them. "
            f"One word access point on a bit device is 16 consecutive points "
            f"({BIT_WINDOW.reference}) and this library reads the whole window or "
            f"nothing -- it does not quietly read a shorter one."
        )
    # Past this point the lowering always succeeds, and it is worth saying why rather
    # than writing a guard nothing can reach: every named bit is at most `last`, the new
    # window is [last - 15, last], and every bit was at or above the old anchor, which is
    # above `last - 15`. So each bit's new position is `index - last + 15`, which is 15
    # or less.
    shift = window.anchor - lowered
    return FoldWindow(lowered, tuple((name, bit + shift) for name, bit in window.bits))


def _names(window: FoldWindow) -> str:
    return ", ".join(name for name, _bit in window.bits)


# ========================================================================================
# Compiling a class body into a layout
# ========================================================================================


def compile_layout(
    block: type,
    *,
    base: AddressLike | None = None,
    word_order: WordOrder | None = None,
    overrides: Mapping[str, FieldOverride] | None = None,
) -> BlockLayout:
    """Turn a dataclass into a :class:`BlockLayout`. Pure; no profile, no I/O.

    ``overrides`` carries what :func:`~aslmp.blocks.fields.at` and
    :func:`~aslmp.blocks.fields.Str` left in the class body before ``@plc_block``
    stripped them out, keyed by field name.

    The refusals here are all the ones that are true of the *declaration* alone: a field
    with no aslmp alias, a bit field with no address of its own, a string field whose
    annotation is not ``str``. Everything that needs a CPU -- does this device exist,
    does the span fit, does the point count fit the budget -- belongs to ``bind()`` and
    is deliberately absent.
    """
    marks = dict(overrides or {})
    hints = _hints(block)
    plans: list[FieldPlan] = []
    cursor = 0
    for field in dataclasses.fields(block):
        if field.name == _TX_FIELD:
            continue
        spec = _spec_for(block, field.name, hints.get(field.name), marks.get(field.name))
        override = marks.get(field.name)
        address = None if override is None else override.address
        if isinstance(spec, BitSpec) and address is None:
            raise SlmpBlockLayoutError(
                f"{block.__name__}.{field.name} is a Bit field with no address. A bit "
                f"lives on a bit device and a block's base runs over registers, so "
                f"there is no offset from the base that could reach it: give it one "
                f"with at(...), as in `{field.name}: Bit = at(\"M100\")`."
            )
        if address is not None:
            plans.append(FieldPlan(field.name, spec, address=address))
            continue
        plans.append(FieldPlan(field.name, spec, word_offset=cursor))
        cursor += 0 if isinstance(spec, BitSpec) else spec.words
    layout = BlockLayout(
        block_name=block.__name__,
        fields=tuple(plans),
        base=base,
        word_order=word_order,
    )
    if layout.auto_fields and base is None:
        auto = ", ".join(f.name for f in layout.auto_fields)
        raise SlmpBlockLayoutError(
            f"{block.__name__} has fields addressed from a base ({auto}) but no base "
            f"was given. Pass one to the decorator -- @plc_block(base=\"D0\") -- or to "
            f"bind(..., base=\"D0\"), or address every field with at(...)."
        )
    return layout


def _hints(block: type) -> Mapping[str, object]:
    """Resolved annotations, ``Annotated`` metadata kept."""
    try:
        return get_type_hints(block, include_extras=True)
    except Exception as exc:
        raise SlmpBlockLayoutError(
            f"the annotations of @plc_block class {block.__name__} cannot be resolved: "
            f"{type(exc).__name__}: {exc}. Every name used in an annotation has to be "
            f"importable at run time, because the layout is computed from the "
            f"annotations rather than from a copy of them."
        ) from exc


def _spec_for(
    block: type, name: str, hint: object, override: FieldOverride | None
) -> FieldSpec:
    """The one :class:`FieldSpec` this annotation and default declare, or a refusal."""
    if isinstance(hint, NumberSpec):
        raise SlmpBlockLayoutError(
            f"{block.__name__}.{name} is annotated with the *result* of "
            f"{hint.label}(...) rather than with a type. A bounded field is declared in "
            f"the metadata position of an Annotated, where the bare type stays visible "
            f"to a type checker: `{name}: Annotated[{hint.python}, "
            f"{hint.label}(minimum=..., maximum=...)]`. Written the way you have it, "
            f"mypy reads the annotation as a call and refuses it, and {name} would be "
            f"typed by nothing at all."
        )
    metadata = getattr(hint, "__metadata__", ())
    marks = [item for item in metadata if isinstance(item, NumberSpec | BitSpec)]
    string = None if override is None else override.string
    if len(marks) > 1:
        labels = ", ".join(mark.label for mark in marks)
        raise SlmpBlockLayoutError(
            f"{block.__name__}.{name} is annotated with more than one aslmp field "
            f"alias ({labels}). A field is one width or another; nothing here picks."
        )
    if marks and string is not None:
        raise SlmpBlockLayoutError(
            f"{block.__name__}.{name} is annotated {marks[0].label} and given a "
            f"Str(...) default. Declare a string field as `{name}: str = "
            f"Str(length=...)`."
        )
    if string is not None:
        if hint is not str:
            raise SlmpBlockLayoutError(
                f"{block.__name__}.{name} is declared with Str(...) so its annotation "
                f"must be `str`; it is {_render(hint)}."
            )
        return string
    if marks:
        mark = marks[0]
        if isinstance(mark, NumberSpec):
            _check_bare_type(block, name, hint, mark)
        return mark
    raise SlmpBlockLayoutError(
        f"{block.__name__}.{name} is annotated {_render(hint)}, which says nothing "
        f"about how many registers it occupies or how they decode. Declare it with one "
        f"of the aslmp aliases -- F32, F64, I32, U32, I16, U16, Word, Bit -- or as "
        f"`{name}: str = Str(length=...)`. There is no default width: a field silently "
        f"read as one register when the program stores two returns the low half of the "
        f"value with end code 0x0000."
    )


_PYTHON_TYPES: Final[Mapping[str, type]] = {"int": int, "float": float}
"""The bare type each numeric field is read back as. There is no third one."""


def _check_bare_type(block: type, name: str, hint: object, spec: NumberSpec) -> None:
    """The bare half of an ``Annotated`` must agree with the alias in its metadata.

    Free before bounds existed, because the alias wrote both halves itself. It is worth
    checking now that a caller writes one of them by hand:
    ``Annotated[int, F32(minimum=0.0)]`` would otherwise decode two registers as a float
    and hand it back through an annotation that promised an ``int``, which is the same
    class of silent wrong answer bounds are here to close.
    """
    expected = _PYTHON_TYPES.get(spec.python)
    bare = getattr(hint, "__origin__", None)
    if expected is None or not isinstance(bare, type):  # pragma: no cover - an invariant
        return
    if bare is not bool and issubclass(bare, expected):
        return
    raise SlmpBlockLayoutError(
        f"{block.__name__}.{name} is annotated Annotated[{_render(bare)}, "
        f"{spec.label}(...)], and a {spec.label} reads back as a {spec.python}. Write "
        f"`Annotated[{spec.python}, {spec.label}(minimum=..., maximum=...)]`: the bare "
        f"type is the one your call site is handed, and it is the half a type checker "
        f"reads."
    )


def _render(hint: object) -> str:
    """An annotation as a person wrote it, near enough to recognise in a message."""
    if hint is None:
        return "(nothing)"
    name = getattr(hint, "__name__", None)
    return name if isinstance(name, str) else str(hint)


# ========================================================================================
# The decorator
# ========================================================================================


@dataclass_transform(
    frozen_default=True, kw_only_default=True, field_specifiers=(at, Str)
)
def plc_block(
    *,
    base: AddressLike | None = None,
    word_order: WordOrder | None = None,
) -> Callable[[type[T]], type[T]]:
    """Declare a block of typed fields once; read it in one ``0403``.

    The decorated class becomes a frozen, slotted, keyword-only dataclass with one extra
    field, ``tx``, holding the transaction that produced the instance -- so a control
    loop reads ``state.tx.timing.wire_ms`` with no ceremony. ``tx`` defaults to ``None``
    because a block you built yourself, to hand to ``plan.write_block(...)``, was not
    read from a PLC and has no transaction to carry.

    Two of this class's own attributes are added at run time and a type checker, which
    reads the class body rather than the result, cannot see either. Both have a door:
    inherit :class:`~aslmp.blocks.fields.PlcBlock` (empty ``__slots__``, no runtime cost)
    to declare ``tx`` statically, and call :func:`layout_of` for the ``__layout__`` this
    caches on the class.

    The layout is computed here, at class-definition time, and cached on
    ``cls.__layout__``. Nothing about a CPU is decided: ``@plc_block`` does not know
    whether ``M100`` is index 100, whether ``D8000`` exists, or how many access points
    this CPU allows. That is ``bind()``'s job, it is synchronous, and it happens once
    against a connected client.

    ``word_order`` is accepted for the same reason every read method accepts it -- it is
    a PLC-program convention rather than a protocol fact -- but a block has almost
    nothing for it to reorder: ``F32``/``I32``/``U32`` are single double-word access
    points, which are one native low-word-first value on this hardware
    (:data:`~aslmp.blocks.fields.WORD_ORDER_PROOF`). ``bind()`` refuses
    ``HIGH_FIRST`` on a block that contains one rather than accepting a parameter it
    would then ignore.
    """

    def decorate(cls: type[T]) -> type[T]:
        overrides = _strip_overrides(cls)
        _reject_plain_defaults(cls, overrides)
        _inject_tx(cls)
        made = dataclasses.dataclass(frozen=True, slots=True, kw_only=True)(cls)
        layout = compile_layout(
            made, base=base, word_order=word_order, overrides=overrides
        )
        made.__layout__ = layout  # type: ignore[attr-defined]  # public per DESIGN 2.7
        return made

    return decorate


def _strip_overrides(cls: type) -> dict[str, FieldOverride]:
    """Take the ``at()`` / ``Str()`` markers out of the class body, keeping what they said.

    They must not survive: a marker left in place would become the field's dataclass
    default, so ``LoopState()`` would build an instance whose ``fault`` is a
    ``FieldOverride`` and whose ``mode`` is one too.
    """
    found: dict[str, FieldOverride] = {}
    for name in inspect.get_annotations(cls):
        value = cls.__dict__.get(name)
        if isinstance(value, FieldOverride):
            found[name] = value
            delattr(cls, name)
    return found


def _reject_plain_defaults(cls: type, overrides: Mapping[str, FieldOverride]) -> None:
    """A block field may not carry an ordinary default value.

    Every field is written by the response, so a default is a value that can never be
    read -- and ``mode: U16 = 3`` beside ``mode: U16 = at("D400")`` is exactly the typo
    that would otherwise look deliberate.
    """
    for name in inspect.get_annotations(cls):
        if name in overrides or name == _TX_FIELD:
            continue
        if name in cls.__dict__:
            raise SlmpBlockLayoutError(
                f"{cls.__name__}.{name} has a default value "
                f"({cls.__dict__[name]!r}). A block field is always written by the "
                f"response it decodes, so a default is a value nothing can ever read. "
                f"Only at(...) and Str(...) belong in that slot."
            )


def _inject_tx(cls: type) -> None:
    """Add the ``tx`` field, unless the class declared one itself."""
    declared = inspect.get_annotations(cls)
    if _TX_FIELD in declared:
        return
    declared[_TX_FIELD] = BlockTransaction | None
    cls.__annotations__ = declared
    cls.tx = None  # type: ignore[attr-defined]  # the dataclass default for the field


def layout_of(block: type) -> BlockLayout:
    """The layout ``@plc_block`` cached on ``block``, or a refusal naming the decorator."""
    layout = getattr(block, "__layout__", None)
    if isinstance(layout, BlockLayout):
        return layout
    name = getattr(block, "__name__", repr(block))
    raise SlmpBlockLayoutError(
        f"{name} is not a @plc_block class: it has no "
        f"__layout__. bind() takes a decorated class, because the layout is what says "
        f"which registers the fields are and there is no way to guess one."
    )


def cites() -> tuple[Source, ...]:
    """The provenance of the layout rules themselves, for ``plan.describe()``."""
    return (BIT_WINDOW, FOLD_ANCHOR)
