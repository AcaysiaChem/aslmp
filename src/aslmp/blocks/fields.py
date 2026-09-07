"""Layer 1 -- what a block field *is*, before anything knows which CPU it lives on.

Pure. This module holds the ``Annotated`` aliases a caller writes in a ``@plc_block``
class body, the frozen marker objects behind them, and the two default-slot helpers
:func:`at` and :func:`Str`. It has no profile, no connection, no socket and no opinion
about addresses: ``"D100"`` is a literal here and stays one until
:func:`aslmp.blocks.plan.bind` hands it to a profile that says what base its digits are
in (``Y20`` is output 16 on an iQ-F and output 32 on an iQ-R, and **both CPUs answer
0x0000** -- DESIGN.md section 4.3).

**The static type of a field is exactly ``float``, ``int``, ``bool`` or ``str``**
(graft G13). ``F32`` is ``Annotated[float, _F32]``, so ``state.setpoint`` is a ``float``
to ``mypy`` with no ``cast`` at the call site; the marker in the second position is what
this library reads to decide the field is one double-word access point rather than two
registers. That is the whole trick, and it is why the aliases are ``Annotated`` rather
than ``NewType`` or a class of their own.

**Why ``at()`` rather than ``field(device=...)``.** ``dataclasses.field`` is a name every
Python programmer already has bound, and a block class body is exactly where they would
reach for it; shadowing it in the annotation namespace of a dataclass-shaped decorator is
how ``field(default=0)`` silently becomes something else (DESIGN.md section 0.3). ``at()``
is unmistakable and reads as what it is: this field is *at* that device.

**Widths, and where the words go.**

======  =========  =======================================================
alias   words      one ``0403`` access point of
======  =========  =======================================================
Word    1          word access
U16     1          word access
I16     1          word access
F32     2          double-word access -- natively one IEEE-754 float
U32     2          double-word access
I32     2          double-word access
F64     4          two double-word access points, low word first
Bit     0          folded into a shared 16-point window (see ``layout.py``)
Str(n)  (n+2)//2   that many word access points, NUL word included
======  =========  =======================================================

One double-word access point on a word device is two consecutive registers **low word
first**, which is exactly the FX5U's f32 convention -- proved four ways on
FX5U-32MT/DS fw 1.065 (2026-09-06), including writing 1234.5 as one ``1402`` double-word
point and reading back ``D104 = 0x5000``, ``D105 = 0x449A``. There is no byte-swapping
helper in this library and there is none here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, Final, Literal, Protocol, TypeAlias

from aslmp.errors import SlmpBlockLayoutError
from aslmp.wire.address import DeviceAddress
from aslmp.wire.citations import Citation, Measurement, Source

__all__ = [
    "F32",
    "F64",
    "I16",
    "I32",
    "STRING_WORDS",
    "U16",
    "U32",
    "WORD_ORDER_PROOF",
    "AddressLike",
    "Bit",
    "BitSpec",
    "BlockTiming",
    "BlockTransaction",
    "FieldOverride",
    "FieldSpec",
    "NumberSpec",
    "PlcBlock",
    "PointKind",
    "Str",
    "StringSpec",
    "Word",
    "at",
    "string_words",
]

PointKind: TypeAlias = Literal["u16", "i16", "u32", "i32", "f32", "bits"]
"""How one access point is read back.

The same alias :mod:`aslmp.commands.random` declares, restated here because that module
is Layer 2 and this one is Layer 1. Structurally identical, so a field's ``kind`` is
accepted straight into a :class:`~aslmp.commands.random.RandomPoint` with no cast.
"""

AddressLike = str | DeviceAddress
"""A device literal, or an address a profile has already resolved.

The same union :mod:`aslmp.commands.base` declares, restated here because that module is
Layer 2 and this one is Layer 1. A string is **not** an address until a profile has said
what base its digits are in.
"""


WORD_ORDER_PROOF: Final = Measurement(
    cpu="FX5U-32MT/DS",
    firmware="1.065",
    date="2026-09-06",
    note=(
        "One 0403 double-word access point is one IEEE-754 f32, low word first. "
        "1234.5 written as one 1402 double-word point put 00 50 9A 44 on the wire "
        "and read back D104 = 0x5000, D105 = 0x449A. F32/I32/U32 therefore need no "
        "byte-swapping helper and no word-order parameter."
    ),
)

STRING_WORDS: Final = Citation(
    manual="JY997D56001",
    revision="K",
    section="section 4.2 p.69",
    note=(
        "A MELSEC character string occupies two characters per register and is "
        "terminated by a NUL, so n declared characters need (n + 2) // 2 registers -- "
        "the terminator's own register included. A block that read only ceil(n/2) "
        "registers would return the string with its terminator missing whenever the "
        "declared length is even, and the PLC program's own $MOV wrote one."
    ),
)


# ========================================================================================
# What a block instance carries back, without naming a layer above this one
# ========================================================================================


class BlockTiming(Protocol):
    """The durations a control loop reads off a block it just read.

    A structural stand-in, exactly as :mod:`aslmp.errors` uses for the same reason:
    :class:`aslmp.timing.TransactionTiming` is Layer 2.5 and this module is Layer 1, so
    the shape is declared here and the real class satisfies it. ``tests`` bind a genuine
    record to this type, which makes the compatibility a build failure rather than a hope.
    """

    @property
    def wire_ms(self) -> float: ...

    @property
    def wire_ns(self) -> int: ...

    @property
    def total_ns(self) -> int: ...

    @property
    def host_gap_ns(self) -> int: ...


class BlockTransaction(Protocol):
    """The transaction record a block instance carries in its ``tx`` field.

    The members a control loop reads. The connection-entry enums (frame, coding,
    transport) are deliberately absent: they are constants of the client, which is the
    object to ask, and restating them here would mean a second structural stand-in for
    each one.
    """

    @property
    def timing(self) -> BlockTiming: ...

    @property
    def sequence(self) -> int: ...

    @property
    def connection_id(self) -> str: ...

    @property
    def generation(self) -> int: ...

    @property
    def after_reconnect(self) -> bool: ...

    @property
    def command(self) -> int: ...

    @property
    def subcommand(self) -> int: ...

    @property
    def request_bytes(self) -> int: ...

    @property
    def response_bytes(self) -> int: ...

    @property
    def end_code(self) -> int: ...

    @property
    def ok(self) -> bool: ...

    @property
    def prebuilt(self) -> bool: ...

    @property
    def serial(self) -> int | None: ...

    @property
    def plc_clock(self) -> int | None: ...


class PlcBlock:
    """Optional base class that declares ``tx`` for a type checker. Runtime cost: none.

    ``@plc_block`` adds a ``tx`` field to every block it decorates, but a decorator
    cannot add an attribute a type checker can see -- it is handed the class body, not
    the result. Inheriting this declares the same field statically::

        @plc_block(base="D0")
        class LoopState(PlcBlock):
            setpoint: F32

        state = await plan.read()
        assert state.tx is not None       # a block you built yourself has no transaction
        print(state.tx.timing.wire_ms)

    ``__slots__`` is empty on purpose: a base without it would give every block instance
    a ``__dict__``, which is one dictionary per cycle in the hot path and the end of the
    "no attribute can appear later" guarantee. The annotation alone creates no attribute,
    so the field the decorator injects is the only one there is.
    """

    __slots__ = ()

    tx: BlockTransaction | None


# ========================================================================================
# The markers behind the aliases
# ========================================================================================


@dataclass(frozen=True, slots=True)
class NumberSpec:
    """One numeric field: its width in registers and how those bytes read.

    ``struct_code`` is the little-endian format character for the whole field, which is
    what lets a bound plan decode an entire response with **one** compiled
    :class:`struct.Struct` rather than a loop of per-field slicing: a ``0403`` response
    is bare data with no framing between points, so consecutive fields are consecutive
    bytes (SH(NA)-080956ENG-M section 6.4 pp.53-56).
    """

    label: str
    python: str
    words: int
    struct_code: str
    kind: PointKind
    """The :class:`~aslmp.commands.random.RandomPoint` kind one point of this field is."""

    @property
    def points(self) -> int:
        """How many access points this field costs: 1, or 2 for an ``F64``."""
        return 1 if self.words <= 2 else self.words // 2

    @property
    def dword(self) -> bool:
        """Whether this field is read as double-word access points."""
        return self.words >= 2


@dataclass(frozen=True, slots=True)
class BitSpec:
    """One bit field. Costs **no** access point of its own: it folds into a window.

    Eight status bits declared at ``M100``-``M107`` are one word access point against the
    192-point budget, not eight (measured ceiling: 193 points returns ``0xC054`` on
    FX5U-32MT/DS fw 1.065). The fold is named rather than hidden --
    :meth:`aslmp.blocks.plan.BlockPlan.describe` prints the anchor, the transaction record
    carries the folded address, and a range failure quotes it.
    """

    label: str = "Bit"
    python: str = "bool"


@dataclass(frozen=True, slots=True)
class StringSpec:
    """One character-string field: ``length`` characters plus the NUL word.

    ``encoding`` is checked here, at class-definition time, rather than at the first
    decode of a live response. A block whose declared encoding does not exist is a
    mistake in the source file, and finding it at start-up is the entire reason
    ``bind()`` is separate from ``read()``.
    """

    length: int
    encoding: str = "ascii"
    label: str = "Str"
    python: str = "str"

    def __post_init__(self) -> None:
        if not isinstance(self.length, int) or isinstance(self.length, bool):
            raise SlmpBlockLayoutError(
                f"Str(length=...) is a count of characters, not "
                f"{type(self.length).__name__}."
            )
        if self.length < 1:
            raise SlmpBlockLayoutError(
                f"Str(length={self.length}) has no characters in it. A zero-length "
                f"string field would still cost the NUL register and decode to '' "
                f"forever; declare the field you mean or leave it out."
            )
        if not isinstance(self.encoding, str):
            raise SlmpBlockLayoutError(
                f"Str(encoding=...) is the name of a Python codec, not "
                f"{type(self.encoding).__name__}."
            )
        try:
            "".encode(self.encoding)
        except LookupError as exc:
            raise SlmpBlockLayoutError(
                f"Str(encoding={self.encoding!r}) is not a codec this interpreter "
                f"knows. Nothing here falls back to ASCII: a string decoded with the "
                f"wrong codec is wrong data reported as success."
            ) from exc

    @property
    def words(self) -> int:
        """Registers this string occupies, NUL word included (:data:`STRING_WORDS`)."""
        return string_words(self.length)

    @property
    def points(self) -> int:
        """Word access points: one per register."""
        return self.words

    @property
    def struct_code(self) -> str:
        """One fixed-width byte field covering every register of the string."""
        return f"{2 * self.words}s"


FieldSpec = NumberSpec | BitSpec | StringSpec
"""What one declared field is on the wire. Closed: there is no fourth kind."""


def string_words(length: int) -> int:
    """Registers ``length`` characters need, **including the NUL word**.

    ``(length + 2) // 2``: two characters per register plus the terminator, which needs
    a register of its own whenever the declared length is even (:data:`STRING_WORDS`).
    Deliberately one register more than :meth:`aslmp.client.Plc.read_str` reads for the
    same ``length``: that method returns exactly the characters asked for, while a block
    field is a declaration of what the PLC program *stores* there.
    """
    if not isinstance(length, int) or isinstance(length, bool) or length < 1:
        raise SlmpBlockLayoutError(
            f"a string length is a positive count of characters, not {length!r}."
        )
    return (length + 2) // 2


# ========================================================================================
# The aliases
# ========================================================================================

_WORD: Final = NumberSpec("Word", "int", 1, "H", "u16")
_U16: Final = NumberSpec("U16", "int", 1, "H", "u16")
_I16: Final = NumberSpec("I16", "int", 1, "h", "i16")
_U32: Final = NumberSpec("U32", "int", 2, "I", "u32")
_I32: Final = NumberSpec("I32", "int", 2, "i", "i32")
_F32: Final = NumberSpec("F32", "float", 2, "f", "f32")
_F64: Final = NumberSpec("F64", "float", 4, "d", "u32")
_BIT: Final = BitSpec()

Word = Annotated[int, _WORD]
"""One register, unsigned, with no interpretation imposed on it."""

U16 = Annotated[int, _U16]
"""One register, 0..65535."""

I16 = Annotated[int, _I16]
"""One register, -32768..32767."""

U32 = Annotated[int, _U32]
"""One double-word access point: two registers, low word first, unsigned."""

I32 = Annotated[int, _I32]
"""One double-word access point: two registers, low word first, signed."""

F32 = Annotated[float, _F32]
"""One double-word access point: one IEEE-754 float, natively (:data:`WORD_ORDER_PROOF`)."""

F64 = Annotated[float, _F64]
"""Two double-word access points: four registers, low word first, IEEE-754 double."""

Bit = Annotated[bool, _BIT]
"""One bit of a bit device. Folded into a shared 16-point window; needs :func:`at`."""


# ========================================================================================
# The default-slot helpers
# ========================================================================================


@dataclass(frozen=True, slots=True)
class FieldOverride:
    """What :func:`at` and :func:`Str` put in a field's default slot.

    It never survives class definition: ``@plc_block`` reads it, records it in the
    layout and removes it, so the field ends up **required** rather than defaulted. A
    block field with a real default would be a value that is never used -- every field
    is written by the response -- and would quietly make a typo like ``mode: U16 = 3``
    look deliberate.
    """

    address: AddressLike | None = None
    string: StringSpec | None = None

    def cites(self) -> tuple[Source, ...]:
        """The provenance a plan reprints for a field declared this way."""
        return (STRING_WORDS,) if self.string is not None else ()


def at(address: AddressLike) -> Any:
    """Give this field its own address instead of the block's running base.

    ``fault: Bit = at("M100")`` and ``mode: U16 = at("D400")``. A field addressed this
    way does **not** consume the base cursor, so inserting one does not move every
    auto-addressed field after it.

    Returns ``Any`` because it stands in a default slot whose declared type is the
    field's own: ``mypy`` reads ``fault: Bit = at(...)`` as a ``bool`` field through the
    ``dataclass_transform`` on :func:`~aslmp.blocks.layout.plc_block`, which is what keeps
    the call site free of casts.
    """
    return FieldOverride(address=address)


def Str(  # noqa: N802 - it names the type it declares, beside F32/I32/U16
    *,
    length: int,
    encoding: str = "ascii",
    address: AddressLike | None = None,
) -> Any:
    """Declare a character-string field: ``name: str = Str(length=8)``.

    ``length`` is characters, and the field occupies ``(length + 2) // 2`` registers --
    the NUL word included (:data:`STRING_WORDS`). ``address`` is the same override
    :func:`at` gives a numeric field; the two are one keyword rather than two helpers
    because a string field needs both facts and ``at(Str(...))`` would read as an
    address that is a string.
    """
    return FieldOverride(address=address, string=StringSpec(length, encoding))
