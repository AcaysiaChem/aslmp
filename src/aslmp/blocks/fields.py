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
than ``NewType`` or a class of their own. (The bare type of the run-time object is a
private ``float`` subclass rather than ``float`` itself, so that ``F32(minimum=...,
maximum=...)`` can evaluate; see :func:`_declaring`. A type checker sees ``float``, and
``tests/typing/consumer.py`` fails the build if it ever stops.)

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

**Plausibility bounds, and the one thing they are not.** A D register carries no type on
the wire: sixteen bits are sixteen bits, and a field declared ``U32`` over a register
pair the PLC program writes as a ``REAL`` decodes to a large, plausible integer with end
code ``0x0000``. Nothing in the protocol can detect that, and this library does not try
-- it never guesses a register's type and never sniffs whether a value "looks like" a
float. What it can do is hold a caller to a promise they made in the declaration::

    scan: Annotated[float, F32(minimum=0.0, maximum=1.0e7)]

:class:`Bounds` are that promise, they are optional, and an unbounded field behaves
exactly as it did before they existed. When one is broken the read raises
:class:`SlmpImplausibleValueError` rather than returning a number nothing can stand
behind, and a write outside the declared range raises
:class:`~aslmp.errors.SlmpValueRangeError` before a byte leaves this process.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, Final, Literal, Protocol, TypeAlias

from aslmp.errors import (
    ClientSummary,
    Diagnostics,
    SlmpBlockLayoutError,
    SlmpConfigurationError,
    SlmpSemanticError,
    SlmpValueRangeError,
)
from aslmp.wire.address import DeviceAddress
from aslmp.wire.citations import Citation, Measurement, Source

__all__ = [
    "F32",
    "F64",
    "I16",
    "I32",
    "IMPLAUSIBLE_VALUE_FINDING",
    "STRING_WORDS",
    "U16",
    "U32",
    "WORD_ORDER_PROOF",
    "AddressLike",
    "Bit",
    "BitSpec",
    "BlockTiming",
    "BlockTransaction",
    "Bounds",
    "FieldOverride",
    "FieldSpec",
    "NumberSpec",
    "PlcBlock",
    "PointKind",
    "SlmpImplausibleValueError",
    "Str",
    "StringSpec",
    "Word",
    "at",
    "check_reading",
    "implausible",
    "outside",
    "refuse_write",
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
# Plausibility bounds -- a promise the caller makes, never a guess this library makes
# ========================================================================================


IMPLAUSIBLE_VALUE_FINDING: Final = Measurement(
    cpu="FX5U-32MT/DS",
    firmware="1.065",
    date="2026-09-07",
    note=(
        "A field declared U32 over D8/D9, which the CPU's own ST writes as "
        "IO_Scan := IO_Scan + 1.0, returned 1226168560 with end code 0x0000. That is "
        "the f32 613775.0 read as an unsigned double word, and it is undetectable in "
        "band: a D register carries no type on the wire. It is also undetectable by "
        "the obvious sanity check, because IEEE-754 bit patterns rise monotonically "
        "for positive floats, so the mis-typed counter still increased every cycle -- "
        "the only symptom was its rate. That rate is wrong by a factor that is not "
        "even constant: +1.0 in the REAL moves the U32 reading by one ulp-step, which "
        "is 16 at the 613775.0 measured here and halves every time the counter crosses "
        "a power of two (8 above 2^20, 4 above 2^21). A wrong rate that drifts is "
        "harder to notice than a wrong rate that does not. Bounds exist for this."
    ),
)

_REPRESENTABLE: Final[Mapping[str, tuple[float, float]]] = {
    "H": (0.0, 65535.0),
    "h": (-32768.0, 32767.0),
    "I": (0.0, 4294967295.0),
    "i": (-2147483648.0, 2147483647.0),
    "f": (-3.4028234663852886e38, 3.4028234663852886e38),
    "d": (-1.7976931348623157e308, 1.7976931348623157e308),
}
"""What each field width can hold at all, keyed by its ``struct`` format character.

Keyed on the format character rather than on ``kind`` because ``F64`` is four registers
read as two ``u32`` access points: its ``kind`` says how the *wire* carries it and its
``struct_code`` says what the value is. A bound outside the row for its own field is
refused at class-definition time -- ``U16(maximum=70000)`` can never fire and
``U16(minimum=70000)`` fires on every read, and both are the declaration being wrong
rather than the plant being wrong.
"""


def outside(value: float, minimum: float | None, maximum: float | None) -> bool:
    """Whether ``value`` breaks the promise ``minimum`` and ``maximum`` make.

    **Allocates nothing**, which is why it is a module-level function and not a method
    on :class:`Bounds`: ``bounds.excludes(v)`` builds a bound-method object on every
    call and a block read calls this once per bounded field per cycle
    (``tests/unit/test_blocks_bounds.py`` measures it, the way ``LatencyRecorder``'s
    own no-allocation property is measured).

    Only meaningful for a field that declared at least one bound; a NaN counts as
    outside, because a register pair that decodes to a NaN is inside no range anybody
    could have meant.
    """
    if minimum is not None and value < minimum:
        return True
    if maximum is not None and value > maximum:
        return True
    return value != value


@dataclass(frozen=True, slots=True)
class Bounds:
    """The range a caller **promises** a field's value lies inside. Not type inference.

    Optional on every field and absent by default. This is a tool for people who know
    their process ranges -- a tank level is 0 to 100 percent, a scan counter is positive
    and under ten million -- and it is deliberately not a ceremony every declaration has
    to perform.

    What it is not: evidence about the register. Nothing here inspects the bytes to
    decide what type they are, because nothing on the wire could support that. A bound
    that fires says the value disagrees with the declaration; it does not say which of
    the two is wrong, and :class:`SlmpImplausibleValueError` says so in as many words.
    """

    minimum: float | None = None
    maximum: float | None = None

    def __post_init__(self) -> None:
        for name, value in (("minimum", self.minimum), ("maximum", self.maximum)):
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise SlmpBlockLayoutError(
                    f"a {name} bound is a number, not {type(value).__name__}."
                )
            if value != value:
                raise SlmpBlockLayoutError(
                    f"a {name} bound of NaN compares false against every value, so it "
                    f"would silently never fire. Leave it out to declare no bound."
                )
        if (
            self.minimum is not None
            and self.maximum is not None
            and self.minimum > self.maximum
        ):
            raise SlmpBlockLayoutError(
                f"minimum={self.minimum!r} is above maximum={self.maximum!r}, so no "
                f"value can ever be inside this range and every read would raise."
            )

    @property
    def declared(self) -> bool:
        """Whether either end was given. A :class:`Bounds` with neither promises nothing."""
        return self.minimum is not None or self.maximum is not None

    def excludes(self, value: float) -> bool:
        """Whether ``value`` is outside this range. :func:`outside` is the hot-path form."""
        return outside(value, self.minimum, self.maximum)

    def __str__(self) -> str:
        low = "no minimum" if self.minimum is None else repr(self.minimum)
        high = "no maximum" if self.maximum is None else repr(self.maximum)
        return f"[{low} .. {high}]"


class SlmpImplausibleValueError(SlmpSemanticError):
    """A value arrived intact and outside the range its field declares.

    A :class:`~aslmp.errors.SlmpSemanticError` because that is exactly what happened:
    the PLC answered ``0x0000``, the frame parsed, the registers are the registers it
    sent -- and the number is still not one the caller can use. Nothing was retried and
    nothing was clamped.

    Carries ``field``, ``bounds``, ``value``, ``address`` and the ``registers`` the
    value was decoded from, because "1226168560 is out of range" is not actionable and
    "D8/D9 held 0xD8F0 0x4915" is: those are the bytes, and they are the same bytes
    whichever type they were read as (:data:`IMPLAUSIBLE_VALUE_FINDING`).

    .. note::

       This class belongs in the DESIGN.md section 3.1 tree beside the other
       :class:`~aslmp.errors.SlmpError` subclasses and should move to
       ``aslmp/errors/__init__.py`` when that module is next opened -- the same note
       :class:`aslmp.loop.SlmpCadenceOverrunError` carries, for the same reason. It is
       defined here because bounds are declared here and ``errors/`` was closed when
       this was written.
    """

    def __init__(
        self,
        message: str,
        *,
        field: str,
        bounds: Bounds,
        value: float,
        address: str,
        registers: tuple[int, ...],
        diagnostics: Diagnostics | None = None,
    ) -> None:
        super().__init__(message, diagnostics=diagnostics)
        self.field = field
        self.bounds = bounds
        self.minimum = bounds.minimum
        self.maximum = bounds.maximum
        self.value = value
        self.address = address
        self.registers = registers


_GLOBAL_LABEL: Final = (
    "A D register carries no type on the wire -- sixteen bits are sixteen bits -- so "
    "nothing here can tell a wrong declaration from a wrong process value, and nothing "
    "here guesses. The common cause is a declared type that disagrees with the PLC "
    "program's own global label: an f32 read as U32 returns a large integer that is "
    "really the float's bit pattern, and because IEEE-754 patterns rise monotonically "
    "for positive floats it even keeps counting up. Check the type in GX Works3 under "
    "Label -> Global Label, in the Data Type column, and declare what it says there."
)


def implausible(
    *,
    field: str,
    bounds: Bounds,
    value: float,
    address: str,
    registers: tuple[int, ...],
    diagnostics: Diagnostics | None = None,
) -> SlmpImplausibleValueError:
    """Build the refusal for one out-of-range read. The cold path; allocation is fine here.

    A function rather than a constructor call at each site so that the sentence a person
    reads at 3 a.m. is written once. It is deliberately long: this is the one failure
    whose cause is almost never where the traceback points.
    """
    words = " ".join(f"0x{register:04X}" for register in registers)
    return SlmpImplausibleValueError(
        f"{field} read {value!r} from {address}, which is outside the declared range "
        f"{bounds}. The end code was 0x0000 and the registers on the wire were {words}, "
        f"so nothing failed and nothing was retried. {_GLOBAL_LABEL} If the declaration "
        f"is right and the plant really did go there, the bound is what you asked for.",
        field=field,
        bounds=bounds,
        value=value,
        address=address,
        registers=registers,
        diagnostics=diagnostics,
    )


def refuse_write(
    *, field: str, label: str, bounds: Bounds, value: float, address: str
) -> SlmpValueRangeError:
    """Build the refusal for one out-of-range write. Raised **before** anything is sent.

    A :class:`~aslmp.errors.SlmpValueRangeError` and therefore a
    :class:`~aslmp.errors.SlmpUsageError`, which in DESIGN section 3.1 means precisely
    "no byte left this process" -- and it did not. The read-side
    :class:`SlmpImplausibleValueError` is a *semantic* error because there the PLC
    answered; here there is nothing to answer.
    """
    return SlmpValueRangeError(
        f"field {field} is declared {label} with bounds {bounds} and was given "
        f"{value!r}, which is outside them. Nothing here clamps to fit: a setpoint "
        f"quietly pulled back to the top of its range is a different setpoint written "
        f"to the plant. Nothing was sent. Change the value, or change the bounds on "
        f"{field} ({address}) if the range you declared is not the range you meant."
    )


def check_reading(
    value: float,
    minimum: float | None,
    maximum: float | None,
    *,
    field: str,
    address: object,
    registers: Sequence[int],
    client: ClientSummary | None = None,
) -> None:
    """Hold one freshly decoded value to the bounds a caller passed. Raises, or returns.

    The shared enforcement behind ``plc.read_f32("D0", minimum=..., maximum=...)``.
    Returns immediately when neither bound was given, which is the cost of the feature
    for every caller who does not use it: one call and two ``is None`` tests, no
    allocation, no branch taken.

    ``address`` is an ``object`` and ``client`` is the client rather than a built
    :class:`~aslmp.errors.Diagnostics`, both for the same reason: a read that passes
    must not pay for the report of a read that fails. Neither is rendered, and the
    diagnostic bundle is not built, until the path that is about to raise.
    """
    if minimum is None and maximum is None:
        return
    if minimum is not None and minimum != minimum:
        raise SlmpConfigurationError(
            f"{field}: a minimum of NaN compares false against every value, so it "
            f"would silently never fire. Leave it out to ask for no bound."
        )
    if maximum is not None and maximum != maximum:
        raise SlmpConfigurationError(
            f"{field}: a maximum of NaN compares false against every value, so it "
            f"would silently never fire. Leave it out to ask for no bound."
        )
    if minimum is not None and maximum is not None and minimum > maximum:
        raise SlmpConfigurationError(
            f"{field}: minimum={minimum!r} is above maximum={maximum!r}, so no value "
            f"could ever be inside the range and every read would raise."
        )
    if not outside(value, minimum, maximum):
        return
    raise implausible(
        field=field,
        bounds=Bounds(minimum, maximum),
        value=value,
        address=str(address),
        registers=tuple(registers),
        diagnostics=None if client is None else Diagnostics(client=client),
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

    bounds: Bounds | None = None
    """The optional plausibility range this field promises. ``None`` promises nothing."""

    def __post_init__(self) -> None:
        bounds = self.bounds
        if bounds is None:
            return
        if not bounds.declared:
            raise SlmpBlockLayoutError(
                f"{self.label}() was given no bounds. A field with no range to promise "
                f"is spelled `{self.label}`; the call form is for declaring one."
            )
        low, high = _REPRESENTABLE[self.struct_code]
        for name, value in (("minimum", bounds.minimum), ("maximum", bounds.maximum)):
            if value is not None and not low <= value <= high:
                raise SlmpBlockLayoutError(
                    f"{self.label}({name}={value!r}) is outside what a {self.label} can "
                    f"hold at all ({low!r} to {high!r}). A bound the field's own width "
                    f"cannot reach either never fires or fires on every read, and both "
                    f"of those are the declaration being wrong rather than the plant."
                )

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


def _declaring(spec: NumberSpec, base: type) -> type:
    """The run-time bare type of one alias: ``base``, plus the bounds constructor.

    ``F32(minimum=0.0, maximum=1.0e7)`` has to *evaluate* -- it is the spelling the
    README documents -- and ``Annotated[X, ...](...)`` calls ``X``. Making ``X`` plain
    ``float`` would make that call ``float(minimum=..., maximum=...)``, which is a
    ``TypeError``, so the bare type is a ``float`` (or ``int``) subclass whose ``__new__``
    returns a bounded :class:`NumberSpec` instead of a number.

    **It is never instantiated as a value.** A field's decoded value comes from
    ``struct.unpack`` and is an ordinary ``float`` or ``int``; this class exists only so
    that the alias is callable. ``issubclass(bare, float)`` holds, so every run-time
    introspection of the alias stays true.
    """

    def declare(
        cls: type,
        *,
        minimum: float | None = None,
        maximum: float | None = None,
    ) -> Any:
        """``__new__``, under a name ``ruff``'s N807 does not read as a dunder helper."""
        del cls
        return dataclasses.replace(spec, bounds=Bounds(minimum, maximum))

    return type(
        f"_{spec.label}Declaration",
        (base,),
        {
            "__slots__": (),
            "__new__": declare,
            "__doc__": f"The run-time bare type of {spec.label}. See _declaring().",
        },
    )


if TYPE_CHECKING:
    # A type checker must see the bare type of every numeric alias as exactly ``float``
    # or ``int`` -- graft G13, and ``tests/typing/consumer.py`` asserts it with
    # ``assert_type(state.setpoint, float)``. It never sees the subclass above, whose
    # only job is to make the alias callable at run time, and it never sees the call
    # either: ``mypy`` does not analyse the metadata position of an ``Annotated``.
    # This is the whole of the divergence, it is one line per alias, and it is a
    # narrowing rather than a lie -- the run-time class *is* a ``float``/``int``.
    _WordDeclaration: TypeAlias = int
    _U16Declaration: TypeAlias = int
    _I16Declaration: TypeAlias = int
    _U32Declaration: TypeAlias = int
    _I32Declaration: TypeAlias = int
    _F32Declaration: TypeAlias = float
    _F64Declaration: TypeAlias = float
else:
    _WordDeclaration = _declaring(_WORD, int)
    _U16Declaration = _declaring(_U16, int)
    _I16Declaration = _declaring(_I16, int)
    _U32Declaration = _declaring(_U32, int)
    _I32Declaration = _declaring(_I32, int)
    _F32Declaration = _declaring(_F32, float)
    _F64Declaration = _declaring(_F64, float)

Word = Annotated[_WordDeclaration, _WORD]
"""One register, unsigned, with no interpretation imposed on it."""

U16 = Annotated[_U16Declaration, _U16]
"""One register, 0..65535."""

I16 = Annotated[_I16Declaration, _I16]
"""One register, -32768..32767."""

U32 = Annotated[_U32Declaration, _U32]
"""One double-word access point: two registers, low word first, unsigned."""

I32 = Annotated[_I32Declaration, _I32]
"""One double-word access point: two registers, low word first, signed."""

F32 = Annotated[_F32Declaration, _F32]
"""One double-word access point: one IEEE-754 float, natively (:data:`WORD_ORDER_PROOF`).

Also the way a bounded field is declared. ``F32(minimum=0.0, maximum=1.0e7)`` returns
the same field with a :class:`Bounds` on it, for the metadata position of an
``Annotated``::

    scan: Annotated[float, F32(minimum=0.0, maximum=1.0e7)]

That position, and not the default slot, because the bare type written there is what a
type checker reads: ``state.scan`` stays exactly ``float`` and the call is never
analysed as an attempt to build one. Every numeric alias takes the same two keywords.
"""

F64 = Annotated[_F64Declaration, _F64]
"""Two double-word access points: four registers, low word first, IEEE-754 double."""

Bit = Annotated[bool, _BIT]
"""One bit of a bit device. Folded into a shared 16-point window; needs :func:`at`.

The one alias with no bounds constructor: a ``bool`` has two values and neither of them
is implausible.
"""


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
