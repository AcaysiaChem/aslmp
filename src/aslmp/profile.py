"""What one CPU model can actually do, and where every one of those facts came from.

Layer 1. May import ``aslmp.wire`` and ``aslmp.errors``; imports nothing else and does
no I/O. Importing it must not pull ``socket``, ``ssl``, ``asyncio``, ``selectors``,
``threading`` or ``logging`` into ``sys.modules`` (``tests/unit/test_layering.py``
proves it in a subprocess), and it parses no file at import time: the shipped profiles
in ``aslmp.profiles`` are hand-written Python checked against ``aslmp/data/*.tsv`` by
``tests/unit/test_profiles.py``.

A profile answers four questions and refuses to answer any of them by guessing:

**What base is a device number written in?** ``radix_for``. On an iQ-F, ``X`` and ``Y``
are octal, which is not a cosmetic difference: GX Works3's ``Y20`` is the seventeenth
output and goes on the wire as 16. Measured on FX5U-32MT/DS fw 1.065 by lighting one
output at a chosen wire number and reading the block back. iQ-R is hexadecimal instead,
so the radix belongs to the **profile**, not to the letter ``Y``.

**Does this CPU have that device, and does the whole span fit?** ``check_range``, which
takes the span and never only the head address. ``D7999`` alone is legal on an FX5U and
``D7999`` read as two words is not -- ``D8000`` returned ``0xC056``, measured. A client
that validates the start address passes that request straight to the PLC.

**Do these points fit in one request?** ``check_points``, against a :class:`Limit` whose
:class:`LimitRule` is ``Flat``, ``Weighted`` or ``BlockRule``. A flat count is wrong in
both directions for ``1402``: 160 word points fit under ``word x 12 + dword x 14 <=
1920`` and 138 double-word points do not. The :data:`LimitKey` carries the
:class:`Link`, because 192 is the FX5 CPU built-in port's Read Random ceiling and an
FX5-ENET module's is 123.

**Is the command available at all?** ``require``. ``0x0801`` and ``0x0802`` return
``0xC059`` on an iQ-F, measured twice through independent code paths, so
``monitor_register()`` raises :class:`~aslmp.errors.SlmpCapabilityError` before a byte
is built. It is never attempted on the wire and never silently substituted with a
``0403``.

Every limit, capability, device range and radix override carries an :class:`Evidence`
saying whether it is ``LIVE`` (measured on named silicon), ``MANUAL`` (read at a named
section of a named revision) or ``INFERRED`` (carried across, and not located in the
sources we read). That provenance is not decoration. It is what lets this package ship
iQ-R, Q and L support and say honestly, in machine-readable form, that no iQ-R has ever
been in the building.

**There is deliberately no generic profile.** ``aslmp.profiles.by_key`` raises for an
unknown name and lists the ones that exist; an unrecognised model code from the connect
handshake raises :class:`~aslmp.errors.SlmpProfileMismatchError`. An unrecognised FX5
falling back to a hexadecimal ``X``/``Y`` reading is silently two points wrong at
``Y20`` and worse as the address grows, with end code ``0x0000`` and nothing anywhere
to say so.
"""

from __future__ import annotations

import dataclasses
import enum
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final, TypeAlias

from aslmp.errors import (
    Diagnostics,
    SlmpAddressRangeError,
    SlmpCapabilityError,
    SlmpConfigurationError,
    SlmpDeviceNotAllowedHereError,
    SlmpDeviceNotOnCpuError,
    SlmpEncodingNotSupportedError,
    SlmpPointLimitError,
)
from aslmp.wire.address import DeviceAddress
from aslmp.wire.citations import Ambiguity, Citation, Measurement, Provenance, Source
from aslmp.wire.codec import Notation, SpecFormat, Unit
from aslmp.wire.devicetable import DEVICE_TABLE, DeviceType, Radix

__all__ = [
    "BlockRule",
    "Capability",
    "ClearMode",
    "CpuProfile",
    "DeviceRange",
    "Encoding",
    "Evidence",
    "Family",
    "Flat",
    "Limit",
    "LimitKey",
    "LimitRule",
    "Link",
    "Refusal",
    "Weighted",
]


# ========================================================================================
# Connection-entry and family vocabulary
# ========================================================================================


class Family(enum.Enum):
    """The CPU family a profile belongs to.

    The family decides things no per-model table can: an iQ-F writes ``00 00`` in the
    fixed field of ``1002``/``1005``/``1006`` where the SLMP reference families write
    ``01 00``, and an iQ-F reads ``X``/``Y`` in octal where every other family reads
    them in hexadecimal.
    """

    IQ_F = "iq-f"
    IQ_R = "iq-r"
    Q = "q"
    L = "l"


class Link(enum.Enum):
    """Which piece of hardware the SLMP connection entry lives on.

    Part of every :data:`LimitKey`, because the budgets differ and a client that models
    them as one per-CPU constant is wrong on one of them: Read Random is 192 points on
    the FX5 CPU's built-in Ethernet port and 123 through an FX5-ENET module
    (JY997D56001-K p.77 footnote), and batch write is 960 against 949.

    It is a constructor fact, never inferred. Nothing on the wire distinguishes the two.
    """

    CPU_BUILTIN = "cpu"
    ETHERNET_MODULE = "enet"


class Encoding(enum.Enum):
    """The Communication Data Code of the connection, as GX Works3 sets it.

    Three members, not four: ``ASCII`` and "ASCII with hexadecimal X/Y" are the same
    wire rendering for every device except ``X`` and ``Y``, so an alias would be a
    fourth name for a third behaviour.

    ``BINARY``
        The GX Works3 factory default and this library's default.
    ``ASCII_XY_OCT``
        The iQ-F "ASCII code (X, Y OCT)" own-node setting: an ``X``/``Y`` device number
        is written as octal digits, so index 37 goes out as ``"000045"``.
        :meth:`CpuProfile.notation_for` refuses it on any non-iQ-F profile.
    ``ASCII_XY_HEX``
        iQ-F firmware 1.040+ with GX Works3 1.030G+, and the only ASCII coding an
        iQ-R, Q or L has. Index 37 goes out as ``"000025"``.

    The whole ASCII path is unverified on our bench: the FX5's Communication Data Code
    is a single Own Node parameter for the entire Ethernet port, so switching it to test
    ASCII breaks every binary connection at once.

    Sending ASCII into a connection entry configured for binary produces end code
    ``0xC06F``, which the FX5 answers with **silence** -- hence
    :class:`~aslmp.errors.TimeoutCause` ranking ``CODING_MISMATCH`` first on a
    zero-byte handshake timeout. There is no ``AUTO`` member and nothing anywhere
    retries in the other coding.
    """

    BINARY = "binary"
    ASCII_XY_OCT = "ascii-xy-oct"
    ASCII_XY_HEX = "ascii-xy-hex"

    @property
    def coding(self) -> str:
        """``"binary"`` or ``"ascii"`` -- which codec renders this encoding.

        Both ASCII members share one codec and one set of point budgets; they differ
        only in how an ``X`` or ``Y`` device *number* is rendered, which changes no
        length and no field width.
        """
        return "binary" if self is Encoding.BINARY else "ascii"

    @property
    def is_ascii(self) -> bool:
        """True for both ASCII members. Present so no caller writes the ``!=`` form."""
        return self is not Encoding.BINARY


class ClearMode(enum.IntEnum):
    """The device-clear mode of ``1001`` Remote Run. The value is the wire byte.

    An iQ-F accepts ``NONE`` only: JY997D56001-K p.105's clear-mode table has exactly
    one row, while the communication example on the same page prints ``02H``. The
    profile ships the table's reading and refuses the others before the request is
    built rather than choosing between two printed pages at run time (ambiguity
    ``A-CLEAR-MODE``).
    """

    NONE = 0
    EXCEPT_LATCH = 1
    INCLUDING_LATCH = 2


class Capability(enum.Enum):
    """A thing a CPU can be asked to do, which some CPUs cannot.

    Gating is pre-transport and typed. ``0x0801``/``0x0802`` on an iQ-F are the case
    that makes this a mechanism rather than a docstring: they return ``0xC059``, not
    the ``0xC05D`` "monitor not registered" a reader of the generic reference would
    expect, and the temptation to emulate them with a ``0403`` is exactly the silent
    substitution this library forbids.
    """

    BATCH_ACCESS = "batch-access"
    RANDOM_ACCESS = "random-access"
    BLOCK_ACCESS = "block-access"
    MONITOR = "monitor"
    LONG_DEVICE_SPEC = "long-device-spec"
    SELF_TEST = "self-test"
    READ_TYPE_NAME = "read-type-name"
    CLEAR_ERROR = "clear-error"
    REMOTE_CONTROL = "remote-control"
    REMOTE_RESET = "remote-reset"
    REMOTE_PASSWORD = "remote-password"
    FOUR_E_FRAME = "4e-frame"

    @property
    def commands(self) -> str:
        """The SLMP commands this capability gates, for an error message."""
        return _CAPABILITY_COMMANDS[self]


_CAPABILITY_COMMANDS: Final[Mapping[Capability, str]] = MappingProxyType(
    {
        Capability.BATCH_ACCESS: "0x0401 / 0x1401 Device Read (Batch) / Write (Batch)",
        Capability.RANDOM_ACCESS: "0x0403 / 0x1402 Device Read Random / Write Random",
        Capability.BLOCK_ACCESS: "0x0406 / 0x1406 Device Read Block / Write Block",
        Capability.MONITOR: "0x0801 / 0x0802 Monitor Registration / Execute Monitor",
        Capability.LONG_DEVICE_SPEC: "subcommand 0x0002 / 0x0003 (long device spec)",
        Capability.SELF_TEST: "0x0619 Self Test",
        Capability.READ_TYPE_NAME: "0x0101 Read Type Name",
        Capability.CLEAR_ERROR: "0x1617 Clear Error",
        Capability.REMOTE_CONTROL: "0x1001 / 0x1002 / 0x1003 Remote Run / Stop / Pause",
        Capability.REMOTE_RESET: "0x1006 Remote Reset",
        Capability.REMOTE_PASSWORD: "0x1630 / 0x1631 Remote Password Unlock / Lock",
        Capability.FOUR_E_FRAME: "4E frames (serial-numbered request/response)",
    }
)


# ========================================================================================
# Provenance
# ========================================================================================


@dataclass(frozen=True, slots=True)
class Evidence:
    """Where one shipped fact came from, in a form a person can go and check.

    ``provenance``
        ``LIVE``, ``MANUAL`` or ``INFERRED``.
    ``source``
        The quotable reference: ``"FX5U-32MT/DS fw 1.065, 2026-09-06"`` or
        ``"JY997D56001-K 4.2 (p.77, footnote)"``. Never blank.
    ``note``
        What a reader should notice -- the binary search that found the ceiling, the
        manual figure it contradicts, the ambiguity key.
    ``origin``
        The structured :class:`~aslmp.wire.citations.Citation` or
        :class:`~aslmp.wire.citations.Measurement` behind ``source``, when there is
        one. ``aslmp cite`` prints it; an exception renders it on its ``observed`` or
        ``manual`` line.

    Build one with :meth:`measured` or :meth:`documented` rather than by hand: those
    keep ``provenance`` and ``source`` in step with ``origin``, and a mismatch between
    them is exactly the kind of quiet lie this type exists to prevent.
    """

    provenance: Provenance
    source: str
    note: str = ""
    origin: Source | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.provenance, Provenance):
            raise TypeError("Evidence.provenance must be a Provenance")
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("Evidence.source must be a non-blank str")
        if not isinstance(self.note, str):
            raise TypeError("Evidence.note must be a str")
        origin = self.origin
        if origin is None:
            return
        if isinstance(origin, Measurement):
            if self.provenance is not Provenance.LIVE:
                raise ValueError(
                    f"Evidence.origin is a Measurement ({origin.reference}) but "
                    f"provenance is {self.provenance.value}; a measurement is LIVE"
                )
        elif origin.provenance is not self.provenance:
            raise ValueError(
                f"Evidence.origin is a Citation with provenance "
                f"{origin.provenance.value} but Evidence.provenance is "
                f"{self.provenance.value}"
            )
        if self.source != origin.reference:
            raise ValueError(
                f"Evidence.source {self.source!r} does not match its origin "
                f"{origin.reference!r}; build it with Evidence.measured() or "
                f"Evidence.documented() so the two cannot drift"
            )

    @classmethod
    def measured(cls, measurement: Measurement, *, note: str = "") -> Evidence:
        """A fact observed on named silicon. Always :attr:`Provenance.LIVE`."""
        if not isinstance(measurement, Measurement):
            raise TypeError(
                f"Evidence.measured() takes a Measurement, not "
                f"{type(measurement).__name__}"
            )
        return cls(Provenance.LIVE, measurement.reference, note, measurement)

    @classmethod
    def documented(cls, citation: Citation, *, note: str = "") -> Evidence:
        """A fact read at a named section. ``MANUAL``, or ``INFERRED`` if the citation is."""
        if not isinstance(citation, Citation):
            raise TypeError(
                f"Evidence.documented() takes a Citation, not {type(citation).__name__}"
            )
        return cls(citation.provenance, citation.reference, note, citation)

    @property
    def measurement(self) -> Measurement | None:
        """The measurement behind this evidence, if it is a measurement."""
        return self.origin if isinstance(self.origin, Measurement) else None

    @property
    def citation(self) -> Citation | None:
        """The citation behind this evidence, if it is a citation."""
        return self.origin if isinstance(self.origin, Citation) else None

    def __str__(self) -> str:
        label = {
            Provenance.LIVE: "measured",
            Provenance.MANUAL: "documented",
            Provenance.INFERRED: "inferred",
        }[self.provenance]
        rendered = f"{label}: {self.source}"
        return f"{rendered} -- {self.note}" if self.note else rendered


def _diagnostics(evidence: Evidence, *, action: str = "") -> Diagnostics:
    """Render an :class:`Evidence` onto the section 3.6 ``observed``/``manual`` lines.

    The ``observed`` line carries the evidence's own note **and** the note on its origin,
    because that is where the detail lives: ``Measurement.note`` is the sentence that
    says ``V0`` returned ``0xC05C`` and not the ``0xC05B`` the documentation predicted.
    Printing only the reference would name the bench and drop what it found.
    """
    origin = evidence.origin
    parts = [evidence.note]
    if origin is not None and origin.note and origin.note not in evidence.note:
        parts.append(origin.note)
    return Diagnostics(
        note=" ".join(part for part in parts if part),
        measurement=evidence.measurement,
        manual=evidence.citation,
        action=action,
    )


# ========================================================================================
# Limits
# ========================================================================================


class LimitRule:
    """How a points-per-request budget is spent. Sealed: three shapes, defined here.

    A scalar cannot express the ``1402`` budget, and modelling it as one is wrong in
    **both** directions: 160 word points fit under ``word x 12 + dword x 14 <= 1920``
    where a flat 192 would allow them and a flat 120 would not, and 138 double-word
    points do not fit where a flat 192 says they do.

    Subclassing outside this module is refused, so ``check_points`` can enumerate the
    shapes it knows how to feed and a new one cannot arrive unnoticed.
    """

    __slots__ = ()

    def __init_subclass__(cls, **kwargs: object) -> None:
        if cls.__module__ != __name__:
            raise TypeError(
                f"LimitRule is sealed: {cls.__module__}.{cls.__qualname__} may not "
                f"subclass it. CpuProfile.check_points feeds each rule shape a "
                f"different set of counts, and a shape it has never seen would be "
                f"silently unchecked."
            )
        super().__init_subclass__(**kwargs)

    def exceeded_by(self, *, word: int, dword: int, bit: int, blocks: int) -> str | None:
        """``None`` when the request fits, else the sentence that names why it does not."""
        raise NotImplementedError

    def describe(self) -> str:
        """The rule as a person would write it, e.g. ``"word x 12 + dword x 14 <= 1920"``."""
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class Flat(LimitRule):
    """``word + dword + bit <= maximum``. One count, one ceiling.

    The shape of the batch commands (``0401`` word 960 / bit 3584, both measured) and
    of ``0403``, where the budget is word points **plus** double-word points regardless
    of how many words come back: 192 word points and 192 double-word points both
    returned ``0x0000``, and 96 + 96 did too.
    """

    maximum: int

    def __post_init__(self) -> None:
        if self.maximum < 1:
            raise ValueError(f"Flat.maximum must be at least 1, not {self.maximum}")

    def exceeded_by(self, *, word: int, dword: int, bit: int, blocks: int) -> str | None:
        del blocks
        total = word + dword + bit
        if total <= self.maximum:
            return None
        return f"{total} points, and the limit is {self.maximum}"

    def describe(self) -> str:
        return f"points <= {self.maximum}"


@dataclass(frozen=True, slots=True)
class Weighted(LimitRule):
    """``word x word_weight + dword x dword_weight <= maximum``.

    The ``1402`` Device Write Random rule: 12 and 14 in binary against 1920, doubled to
    24 and 28 in ASCII because the manual states the ASCII form as the binary formula
    times two. Bit points have their own key with :class:`Flat`, so a bit count reaching
    this rule is a caller error and ``check_points`` refuses it rather than ignoring it.
    """

    word_weight: int
    dword_weight: int
    maximum: int

    def __post_init__(self) -> None:
        for name, value in (
            ("word_weight", self.word_weight),
            ("dword_weight", self.dword_weight),
            ("maximum", self.maximum),
        ):
            if value < 1:
                raise ValueError(f"Weighted.{name} must be at least 1, not {value}")

    def exceeded_by(self, *, word: int, dword: int, bit: int, blocks: int) -> str | None:
        del bit, blocks
        cost = word * self.word_weight + dword * self.dword_weight
        if cost <= self.maximum:
            return None
        return (
            f"{word} word x {self.word_weight} + {dword} double-word x "
            f"{self.dword_weight} = {cost}, and the budget is {self.maximum}"
        )

    def describe(self) -> str:
        return (
            f"word x {self.word_weight} + dword x {self.dword_weight} "
            f"<= {self.maximum}"
        )


@dataclass(frozen=True, slots=True)
class BlockRule(LimitRule):
    """``blocks <= max_blocks`` and ``points + blocks x per_block_overhead <= max_total``.

    The ``0406``/``1406`` shape. ``per_block_overhead`` is the per-block cost charged
    against the total budget: 0 for the read direction, 4 for ``1406`` -- and that 4,
    with the 760-point total, is ``Provenance.INFERRED`` in every shipped profile
    because it was carried from the locked design and not located in the manual.
    """

    max_blocks: int
    per_block_overhead: int
    max_total: int

    def __post_init__(self) -> None:
        if self.max_blocks < 1:
            raise ValueError(f"BlockRule.max_blocks must be >= 1, not {self.max_blocks}")
        if self.per_block_overhead < 0:
            raise ValueError(
                f"BlockRule.per_block_overhead must be >= 0, not "
                f"{self.per_block_overhead}"
            )
        if self.max_total < 1:
            raise ValueError(f"BlockRule.max_total must be >= 1, not {self.max_total}")

    def exceeded_by(self, *, word: int, dword: int, bit: int, blocks: int) -> str | None:
        if blocks > self.max_blocks:
            return f"{blocks} blocks, and the limit is {self.max_blocks}"
        cost = word + dword + bit + blocks * self.per_block_overhead
        if cost <= self.max_total:
            return None
        overhead = (
            f" plus {blocks} x {self.per_block_overhead} words of per-block overhead"
            if self.per_block_overhead
            else ""
        )
        return (
            f"{word + dword + bit} points across {blocks} blocks{overhead} = {cost}, "
            f"and the budget is {self.max_total}"
        )

    def describe(self) -> str:
        return (
            f"blocks <= {self.max_blocks}, points + blocks x "
            f"{self.per_block_overhead} <= {self.max_total}"
        )


@dataclass(frozen=True, slots=True)
class Limit:
    """One points-per-request budget, its rule, and what the PLC says when you exceed it.

    ``end_code_if_exceeded`` is quoted verbatim in the refusal so a user can match our
    client-side "no" against what the CPU itself would have answered. On this hardware
    that mapping is not the obvious one: a **zero** point count returns ``0xC052``, a
    point-count error, where a reader of the documentation would expect an address
    error.
    """

    rule: LimitRule
    end_code_if_exceeded: int
    evidence: Evidence

    def __post_init__(self) -> None:
        if not isinstance(self.rule, LimitRule):
            raise TypeError(f"Limit.rule must be a LimitRule, not {type(self.rule).__name__}")
        if not 0 <= self.end_code_if_exceeded <= 0xFFFF:
            raise ValueError(
                f"Limit.end_code_if_exceeded must be a 16-bit end code, not "
                f"{self.end_code_if_exceeded}"
            )
        if not isinstance(self.evidence, Evidence):
            raise TypeError("Limit.evidence must be an Evidence")


LimitKey: TypeAlias = tuple[int, Encoding, Unit, Link]
"""``(command, encoding, unit, link)``.

The ``link`` is in the key because 192 is the FX5 CPU built-in port's Read Random
ceiling and 123 is an FX5-ENET module's; the ``unit`` is in it because a batch read of
960 words and one of 3584 bits are the same command with different budgets. Both ASCII
encodings map to their own key with the same value: one ``ascii`` row in the data serves
both, and this table writes it out twice rather than normalising at lookup time.
"""


def _limit_key_text(key: LimitKey) -> str:
    command, encoding, unit, link = key
    return f"0x{command:04X}/{encoding.value}/{unit.value}/{link.value}"


# ========================================================================================
# Device ranges
# ========================================================================================


@dataclass(frozen=True, slots=True)
class DeviceRange:
    """Whether a CPU has a device family, and which device numbers it has of it.

    ``first``/``last`` are **index** values -- the integer that goes on the wire -- and
    not the digits GX Works3 prints: an iQ-F ``X`` range is ``X0`` to ``X1777`` in
    GX Works3 and indices 0 to 1023 here.

    Four states, and none of them is "unknown":

    ``present=False``
        The silicon has no such family. ``V``, ``ZR``, ``DX`` and ``DY`` on an iQ-F,
        all of which returned ``0xC05C`` -- not the ``0xC05B`` the documentation
        predicted. ``absent_reason`` is required and is quoted in the refusal.
    ``present=True`` with ``points == 0``
        The family exists but this CPU has none allocated. An out-of-the-box iQ-R has
        no file register at all, so the reference manual's own ``ZR16384`` example
        fails on it, and "address out of range" would be the wrong sentence.
    ``present=True`` with ``first``/``last`` set
        The ordinary case. ``check_range`` validates the whole span against it.
    ``present=True`` with ``first``/``last`` ``None`` and ``points`` ``None``
        There is no static range to validate and ``note`` says why -- the module access
        device ``G`` depends on which intelligent function module is mounted. Span
        checking is skipped for this family and the evidence says so.

    ``configurable`` marks a range GX Works3 can repartition, so the shipped figure is
    a **default** that goes stale. :meth:`CpuProfile.with_ranges` and
    ``aslmp verify-ranges`` exist for that. Device *existence* is not defeatable by any
    of it: that is a property of the silicon, not of the parameter file.
    """

    device: str
    present: bool
    first: int | None = None
    last: int | None = None
    points: int | None = None
    configurable: bool = False
    notation: str = ""
    absent_reason: str = ""
    evidence: Evidence = dataclasses.field(
        default_factory=lambda: Evidence(Provenance.INFERRED, "unstated")
    )

    def __post_init__(self) -> None:
        if self.device not in DEVICE_TABLE:
            known = ", ".join(sorted(DEVICE_TABLE))
            raise ValueError(
                f"DeviceRange.device {self.device!r} is not a device family in the "
                f"generic table; known families are {known}"
            )
        if not isinstance(self.evidence, Evidence):
            raise TypeError("DeviceRange.evidence must be an Evidence")
        if not self.present:
            if (self.first, self.last, self.points) != (None, None, None):
                raise ValueError(
                    f"DeviceRange {self.device!r} is absent but carries a range; an "
                    f"absent family has no device numbers"
                )
            if not self.absent_reason.strip():
                raise ValueError(
                    f"DeviceRange {self.device!r} is absent and gives no reason. The "
                    f"reason is quoted in SlmpDeviceNotOnCpuError; 'this CPU does not "
                    f"have it' without saying which manual or measurement says so is "
                    f"not a diagnosis."
                )
            return
        if self.absent_reason:
            raise ValueError(
                f"DeviceRange {self.device!r} is present but carries an absent_reason"
            )
        if self.points == 0:
            if (self.first, self.last) != (None, None):
                raise ValueError(
                    f"DeviceRange {self.device!r} has zero points but a first/last"
                )
            if not self.evidence.note.strip():
                raise ValueError(
                    f"DeviceRange {self.device!r} has zero points and no note saying "
                    f"why; 'address out of range' is the wrong sentence for a family "
                    f"the parameter file never allocated"
                )
            return
        if self.first is None or self.last is None:
            if (self.first, self.last, self.points) != (None, None, None):
                raise ValueError(
                    f"DeviceRange {self.device!r} has a half-stated range: "
                    f"first={self.first}, last={self.last}, points={self.points}"
                )
            if not self.evidence.note.strip():
                raise ValueError(
                    f"DeviceRange {self.device!r} ships no static range and no note "
                    f"saying why. An empty range never means 'unknown'."
                )
            return
        if self.first < 0:
            raise ValueError(f"DeviceRange {self.device!r} starts at {self.first}")
        if self.last < self.first:
            raise ValueError(
                f"DeviceRange {self.device!r} ends at {self.last}, before its first "
                f"device number {self.first}"
            )
        expected = self.last - self.first + 1
        if self.points is None:
            object.__setattr__(self, "points", expected)
        elif self.points != expected:
            raise ValueError(
                f"DeviceRange {self.device!r} says {self.points} points but "
                f"{self.first}..{self.last} is {expected}"
            )

    @property
    def type(self) -> DeviceType:
        """The generic device-table row this range refines."""
        return DEVICE_TABLE[self.device]

    @property
    def span_text(self) -> str:
        """``"D0 to D7999, 8000 points"`` -- the range as a message quotes it."""
        if not self.present:
            return "not on this CPU"
        if self.points == 0:
            return "0 points allocated"
        if self.first is None or self.last is None:
            return "no static range"
        printed = self.notation or f"{self.device}{self.first} to {self.device}{self.last}"
        return f"{printed}, {self.points} points"

    def __str__(self) -> str:
        return f"{self.device}: {self.span_text}"


# ========================================================================================
# Capabilities
# ========================================================================================


@dataclass(frozen=True, slots=True)
class Refusal:
    """Why a capability is unavailable, with the evidence and what to do instead.

    ``end_code_if_attempted`` is the code the PLC would have answered. It is printed so
    that a user who has seen ``0xC059`` in a packet capture can match it against our
    pre-transport "no", and so that the refusal is falsifiable: if a future firmware
    answers ``0x0000`` there, this row is wrong and can be shown to be wrong.
    """

    reason: str
    evidence: Evidence
    end_code_if_attempted: int | None = None
    alternative: str = ""

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ValueError("Refusal.reason must not be blank")
        if not isinstance(self.evidence, Evidence):
            raise TypeError("Refusal.evidence must be an Evidence")
        code = self.end_code_if_attempted
        if code is not None and not 0 <= code <= 0xFFFF:
            raise ValueError(f"Refusal.end_code_if_attempted must be 16-bit, not {code}")


# ========================================================================================
# The profile
# ========================================================================================


_PROFILE_FACT_NAMES: Final[tuple[str, ...]] = (
    "default_spec",
    "allowed_clear_modes",
    "allowed_encodings",
    "model_codes",
    "remote_fixed_field",
)


@dataclass(frozen=True, slots=True, eq=False, repr=False)
class CpuProfile:
    """One CPU model's abilities, ranges, budgets and radix rules, each with its source.

    Equality is identity: the shipped profiles are module-level singletons, and a
    profile derived with :meth:`replace` or :meth:`with_ranges` is a *different* target
    description even when every table in it happens to match.

    Construction validates. A profile that does not declare every
    :class:`Capability`, that keys a limit under an encoding it does not allow, or that
    gives a device range for a family the generic table has never heard of, does not
    build.
    """

    key: str
    family: Family
    description: str
    model_codes: Mapping[int, str]
    devices: Mapping[str, DeviceRange]
    radix_overrides: Mapping[str, Radix]
    limits: Mapping[LimitKey, Limit]
    capabilities: Mapping[Capability, Evidence | Refusal]
    default_spec: SpecFormat
    allowed_encodings: frozenset[Encoding]
    remote_fixed_field: bytes
    allowed_clear_modes: frozenset[ClearMode]
    ambiguities: tuple[Ambiguity, ...] = ()
    facts: Mapping[str, Evidence] = dataclasses.field(default_factory=dict)

    # -- construction --------------------------------------------------------

    def __post_init__(self) -> None:
        if not self.key.strip():
            raise ValueError("CpuProfile.key must not be blank")
        if not isinstance(self.family, Family):
            raise TypeError("CpuProfile.family must be a Family")
        if not self.description.strip():
            raise ValueError(f"CpuProfile {self.key!r} must carry a description")
        self._check_model_codes()
        self._check_devices()
        self._check_radix_overrides()
        self._check_encodings()
        self._check_limits()
        self._check_capabilities()
        self._check_remote_fields()
        self._freeze()

    def _check_model_codes(self) -> None:
        for code, model in self.model_codes.items():
            if not 0 <= code <= 0xFFFF:
                raise ValueError(
                    f"CpuProfile {self.key!r}: model code {code} is not 16-bit"
                )
            if not model.strip():
                raise ValueError(
                    f"CpuProfile {self.key!r}: model code 0x{code:04X} has no model name"
                )

    def _check_devices(self) -> None:
        for name, rng in self.devices.items():
            if not isinstance(rng, DeviceRange):
                raise TypeError(
                    f"CpuProfile {self.key!r}: devices[{name!r}] is not a DeviceRange"
                )
            if rng.device != name:
                raise ValueError(
                    f"CpuProfile {self.key!r}: devices[{name!r}] describes "
                    f"{rng.device!r}"
                )
        missing = sorted(set(DEVICE_TABLE) - set(self.devices))
        if missing:
            raise ValueError(
                f"CpuProfile {self.key!r} says nothing about {missing}. Every family in "
                f"the generic device table must be declared present or absent: a family "
                f"a profile has simply forgotten looks exactly like one it refuses, and "
                f"the difference is a wrong 0xC05C at 3am."
            )

    def _check_radix_overrides(self) -> None:
        for name, radix in self.radix_overrides.items():
            if name not in DEVICE_TABLE:
                raise ValueError(
                    f"CpuProfile {self.key!r}: radix override for unknown family {name!r}"
                )
            if not isinstance(radix, Radix):
                raise TypeError(
                    f"CpuProfile {self.key!r}: radix override for {name!r} is not a Radix"
                )

    def _check_encodings(self) -> None:
        if not self.allowed_encodings:
            raise ValueError(f"CpuProfile {self.key!r} allows no encoding at all")
        if Encoding.BINARY not in self.allowed_encodings:
            raise ValueError(
                f"CpuProfile {self.key!r} does not allow binary. Binary is the GX "
                f"Works3 factory default on every family in this package."
            )
        if Encoding.ASCII_XY_OCT in self.allowed_encodings and self.family is not Family.IQ_F:
            raise ValueError(
                f"CpuProfile {self.key!r} allows Encoding.ASCII_XY_OCT, which is the "
                f"iQ-F 'ASCII code (X, Y OCT)' own-node setting and exists on no other "
                f"family"
            )

    def _check_limits(self) -> None:
        for limit_key, limit in self.limits.items():
            command, encoding, unit, link = limit_key
            if not isinstance(limit, Limit):
                raise TypeError(
                    f"CpuProfile {self.key!r}: limit {_limit_key_text(limit_key)} is "
                    f"not a Limit"
                )
            if not 0 <= command <= 0xFFFF:
                raise ValueError(
                    f"CpuProfile {self.key!r}: limit command {command} is not 16-bit"
                )
            if not isinstance(encoding, Encoding) or not isinstance(unit, Unit):
                raise TypeError(
                    f"CpuProfile {self.key!r}: limit key {limit_key!r} is malformed"
                )
            if not isinstance(link, Link):
                raise TypeError(
                    f"CpuProfile {self.key!r}: limit key {limit_key!r} has no Link"
                )
            if encoding not in self.allowed_encodings:
                raise ValueError(
                    f"CpuProfile {self.key!r}: limit {_limit_key_text(limit_key)} is "
                    f"keyed under an encoding this profile does not allow"
                )

    def _check_capabilities(self) -> None:
        for cap, entry in self.capabilities.items():
            if not isinstance(cap, Capability):
                raise TypeError(
                    f"CpuProfile {self.key!r}: {cap!r} is not a Capability"
                )
            if not isinstance(entry, Evidence | Refusal):
                raise TypeError(
                    f"CpuProfile {self.key!r}: capability {cap.value} maps to "
                    f"{type(entry).__name__}, not an Evidence or a Refusal"
                )
        missing = sorted(cap.value for cap in Capability if cap not in self.capabilities)
        if missing:
            raise ValueError(
                f"CpuProfile {self.key!r} says nothing about the capabilities "
                f"{missing}. Every capability must be declared supported (with "
                f"evidence) or refused (with a Refusal): silence is how a command "
                f"reaches a CPU that answers 0xC059."
            )

    def _check_remote_fields(self) -> None:
        if len(self.remote_fixed_field) != 2:
            raise ValueError(
                f"CpuProfile {self.key!r}: remote_fixed_field is "
                f"{len(self.remote_fixed_field)} bytes; the fixed field of 0x1002 / "
                f"0x1005 / 0x1006 is exactly two"
            )
        if not self.allowed_clear_modes:
            raise ValueError(f"CpuProfile {self.key!r} allows no clear mode at all")
        if ClearMode.NONE not in self.allowed_clear_modes:
            raise ValueError(
                f"CpuProfile {self.key!r} does not allow ClearMode.NONE, which every "
                f"documented family accepts"
            )

    def _freeze(self) -> None:
        """Replace the mutable mappings with read-only views of copies.

        A profile is held for the life of a control loop and read by a prebuilt plan.
        A caller who kept a reference to the dict they passed in could otherwise widen
        a device range after ``bind()`` validated against it.
        """
        for name, value in (
            ("model_codes", dict(self.model_codes)),
            ("devices", dict(self.devices)),
            ("radix_overrides", dict(self.radix_overrides)),
            ("limits", dict(self.limits)),
            ("capabilities", dict(self.capabilities)),
            ("facts", dict(self.facts)),
        ):
            object.__setattr__(self, name, MappingProxyType(value))
        object.__setattr__(self, "allowed_encodings", frozenset(self.allowed_encodings))
        object.__setattr__(self, "allowed_clear_modes", frozenset(self.allowed_clear_modes))
        object.__setattr__(self, "ambiguities", tuple(self.ambiguities))
        object.__setattr__(self, "remote_fixed_field", bytes(self.remote_fixed_field))

    def __repr__(self) -> str:
        return f"CpuProfile({self.key!r})"

    def __str__(self) -> str:
        return self.key

    # -- radix and notation --------------------------------------------------

    def radix_for(self, dt: DeviceType) -> Radix:
        """The base a device number is **written** in on this CPU.

        The generic table's radix for every family except the ones this profile
        overrides: ``X`` and ``Y`` are octal on an iQ-F, where the generic table (and
        every other family) has them hexadecimal.

        This is the parse radix. What goes on the wire is always the linear index --
        GX Works3 ``Y20`` is index 16 (measured, FX5U-32MT/DS fw 1.065) -- except in
        ASCII with :attr:`Encoding.ASCII_XY_OCT`, which is :meth:`notation_for`.
        """
        if not isinstance(dt, DeviceType):
            raise TypeError(f"radix_for() takes a DeviceType, not {type(dt).__name__}")
        return self.radix_overrides.get(dt.name, dt.radix)

    def notation_for(self, dt: DeviceType, enc: Encoding) -> Notation:
        """Which base the **ASCII** device-number digits are written in.

        :attr:`Notation.OCTAL_DIGITS` only for a family this profile makes octal --
        ``X`` and ``Y`` on an iQ-F -- and only under :attr:`Encoding.ASCII_XY_OCT`,
        which no non-iQ-F profile allows. :attr:`Notation.VALUE` everywhere else,
        including binary, where a device number is the linear index as little-endian
        bytes and has no notation at all.

        Raises :class:`~aslmp.errors.SlmpEncodingNotSupportedError` for an encoding
        this profile does not allow, rather than quietly treating it as the nearest
        one it does.
        """
        if not isinstance(dt, DeviceType):
            raise TypeError(f"notation_for() takes a DeviceType, not {type(dt).__name__}")
        if not isinstance(enc, Encoding):
            raise TypeError(f"notation_for() takes an Encoding, not {type(enc).__name__}")
        if enc not in self.allowed_encodings:
            allowed = ", ".join(sorted(e.value for e in self.allowed_encodings))
            raise SlmpEncodingNotSupportedError(
                f"{self.key} cannot use Encoding.{enc.name}: it allows {allowed}. "
                f"'ASCII code (X, Y OCT)' is an iQ-F own-node setting and exists on no "
                f"other family; nothing here falls back to the hexadecimal rendering, "
                f"which would put X and Y at a different device on every request."
            )
        if enc is Encoding.ASCII_XY_OCT and self.radix_for(dt) is Radix.OCTAL:
            return Notation.OCTAL_DIGITS
        return Notation.VALUE

    # -- ranges --------------------------------------------------------------

    def range_for(self, dt: DeviceType) -> DeviceRange:
        """The declared range for ``dt``, present or absent. Raises for an unknown family."""
        rng = self.devices.get(dt.name)
        if rng is None:  # pragma: no cover - __post_init__ makes this unreachable
            raise SlmpConfigurationError(
                f"{self.key} declares nothing about device family {dt.name}"
            )
        return rng

    def check_range(self, addr: DeviceAddress, points: int, *, width: Unit) -> None:
        """Refuse unless the **whole span** ``addr`` implies is on this CPU.

        ``points`` is counted in ``width`` units, which is what makes this a span check
        rather than an address check: 2 word points from ``M0`` reach ``M31``, and 2
        word points from ``D7999`` reach ``D8000``, which is the case a start-only
        check waves through. ``D8000`` returned ``0xC056`` on FX5U-32MT/DS fw 1.065.

        Raises :class:`~aslmp.errors.SlmpDeviceNotOnCpuError` for a family the CPU does
        not have, :class:`~aslmp.errors.SlmpDeviceNotAllowedHereError` for a bit-unit
        request against a word device, :class:`~aslmp.errors.SlmpPointLimitError` for a
        zero point count (which is ``0xC052``, measured -- a point-count error and not
        an address error), and :class:`~aslmp.errors.SlmpAddressRangeError` for a span
        that leaves the range.
        """
        if not isinstance(addr, DeviceAddress):
            raise TypeError(f"check_range() takes a DeviceAddress, not {type(addr).__name__}")
        if not isinstance(width, Unit):
            raise TypeError(f"check_range() width must be a Unit, not {type(width).__name__}")
        if points < 0:
            raise SlmpConfigurationError(
                f"a point count cannot be negative; check_range({addr}, {points}) "
                f"was asked for {points} {width.value} points"
            )
        if points == 0:
            raise SlmpPointLimitError(
                f"a request for zero {width.value} points at {addr} has nothing to "
                f"read or write. On FX5U-32MT/DS fw 1.065 a zero point count returns "
                f"0xC052, a point-count error -- not the address error the "
                f"documentation implies."
            )
        rng = self.range_for(addr.type)
        self._require_present(addr, rng)
        span = self._span_of(addr, points, width)
        if rng.first is None or rng.last is None:
            return
        last = addr.index + span - 1
        if addr.index >= rng.first and last <= rng.last:
            return
        raise SlmpAddressRangeError(
            self._range_message(addr, points, width, span, last, rng),
            diagnostics=_diagnostics(rng.evidence),
        )

    def _require_present(self, addr: DeviceAddress, rng: DeviceRange) -> None:
        if not rng.present:
            raise SlmpDeviceNotOnCpuError(
                f"{self.key} has no {addr.type.name} ({addr.type.long_name.lower()}) "
                f"device, so {addr} cannot be addressed. {rng.absent_reason}",
                diagnostics=_diagnostics(rng.evidence),
            )
        if rng.points == 0:
            raise SlmpAddressRangeError(
                f"{self.key} has 0 points of {addr.type.name} "
                f"({addr.type.long_name.lower()}) allocated, so {addr} does not exist "
                f"on it. {rng.evidence.note}",
                diagnostics=_diagnostics(
                    rng.evidence,
                    action=(
                        f"allocate {addr.type.name} points in the CPU parameters, then "
                        f"pass the real range with profile.with_ranges()"
                    ),
                ),
            )

    def _span_of(self, addr: DeviceAddress, points: int, width: Unit) -> int:
        """How many **device numbers** ``points`` units of ``width`` cover from ``addr``."""
        dt = addr.type
        if width is dt.unit:
            return points
        if width is Unit.WORD:
            return points * 16
        raise SlmpDeviceNotAllowedHereError(
            f"{addr.type.name} is a word device, so a bit-unit request cannot address "
            f"{addr}. Bit-unit commands (0x0401 subcommand 0x0001 and its relatives) "
            f"take bit devices only; read {addr} as words and select the bit from the "
            f"value."
        )

    def _range_message(
        self,
        addr: DeviceAddress,
        points: int,
        width: Unit,
        span: int,
        last: int,
        rng: DeviceRange,
    ) -> str:
        dt = addr.type
        reach = DeviceAddress.of(dt, last, radix=self.radix_for(dt))
        counted = f"{points} {width.value} point{'s' if points != 1 else ''}"
        if span != points:
            counted += f" ({span} {dt.unit.value} device numbers)"
        return (
            f"{addr} with {counted} reaches {reach}, which is outside "
            f"{dt.name} on {self.key} ({rng.span_text}). Reading or writing past the "
            f"end of a device range returns 0xC056; this is a span check, not an "
            f"address check, because {addr} on its own is legal."
        )

    # -- limits --------------------------------------------------------------

    def limit(self, command: int, encoding: Encoding, unit: Unit, link: Link) -> Limit:
        """The budget for one ``(command, encoding, unit, link)``, or raise.

        There is no default and no nearest match. A key this profile does not ship is a
        gap in the data, and answering it with another link's or another coding's
        ceiling is how a client builds a frame the CPU rejects -- or, worse, one it
        accepts and truncates.
        """
        key: LimitKey = (command, encoding, unit, link)
        found = self.limits.get(key)
        if found is not None:
            return found
        known = ", ".join(sorted(_limit_key_text(k) for k in self.limits))
        raise SlmpConfigurationError(
            f"{self.key} ships no points-per-request limit for "
            f"{_limit_key_text(key)}. Nothing here substitutes a neighbouring one: the "
            f"budgets differ by link (192 on the FX5 built-in port, 123 through an "
            f"FX5-ENET) and by coding. Limits this profile does have: {known}."
        )

    def check_points(
        self,
        key: LimitKey,
        *,
        word: int = 0,
        dword: int = 0,
        bit: int = 0,
        blocks: int = 0,
    ) -> None:
        """Refuse unless these counts fit the budget for ``key``.

        Zero points in total is refused first and separately: it is not "trivially
        within the limit", it is a request with nothing in it, and this hardware
        answers it with ``0xC052``.

        The counts a rule is fed must match its shape. Passing ``blocks`` to a
        :class:`Flat` rule, or ``bit`` to a :class:`Weighted` one, raises
        :class:`~aslmp.errors.SlmpConfigurationError` rather than being ignored --
        an ignored count is an unchecked count.
        """
        for name, value in (("word", word), ("dword", dword), ("bit", bit), ("blocks", blocks)):
            if value < 0:
                raise SlmpConfigurationError(
                    f"check_points() was given {value} {name} points; a point count "
                    f"cannot be negative"
                )
        command, encoding, unit, link = key
        limit = self.limit(command, encoding, unit, link)
        self._check_rule_shape(key, limit.rule, bit=bit, blocks=blocks)
        if word + dword + bit + blocks == 0:
            raise SlmpPointLimitError(
                f"a 0x{command:04X} request with no points at all has nothing to read "
                f"or write. On FX5U-32MT/DS fw 1.065 a zero point count returns "
                f"0xC052, a point-count error -- not the address error the "
                f"documentation implies.",
                diagnostics=_diagnostics(limit.evidence),
            )
        over = limit.rule.exceeded_by(word=word, dword=dword, bit=bit, blocks=blocks)
        if over is None:
            return
        raise SlmpPointLimitError(
            f"0x{command:04X} on {self.key} over {link.value} in {encoding.value} takes "
            f"{limit.rule.describe()}; this request is {over}. The CPU answers "
            f"0x{limit.end_code_if_exceeded:04X}. Evidence: {limit.evidence}.",
            diagnostics=_diagnostics(limit.evidence),
        )

    def _check_rule_shape(
        self, key: LimitKey, rule: LimitRule, *, bit: int, blocks: int
    ) -> None:
        text = _limit_key_text(key)
        if blocks and not isinstance(rule, BlockRule):
            raise SlmpConfigurationError(
                f"{self.key} limit {text} is {type(rule).__name__}, which counts "
                f"points and not blocks; {blocks} blocks were offered to it. Block "
                f"access is 0x0406 / 0x1406."
            )
        if isinstance(rule, BlockRule) and not blocks:
            raise SlmpConfigurationError(
                f"{self.key} limit {text} is a BlockRule and no blocks were offered "
                f"to it"
            )
        if bit and isinstance(rule, Weighted):
            raise SlmpConfigurationError(
                f"{self.key} limit {text} is a Weighted word/double-word budget; bit "
                f"points have their own key with a flat cap and must be checked "
                f"against it"
            )

    # -- capabilities --------------------------------------------------------

    def supports(self, cap: Capability) -> bool:
        """Whether this CPU has ``cap``. Never raises; :meth:`require` is the gate."""
        if not isinstance(cap, Capability):
            raise TypeError(f"supports() takes a Capability, not {type(cap).__name__}")
        return isinstance(self.capabilities[cap], Evidence)

    def require(self, cap: Capability, *, what: str) -> None:
        """Refuse, pre-transport, unless this CPU has ``cap``.

        ``what`` names the call the user made, so the message reads as an answer to
        their question rather than as a table lookup. The refusal quotes the evidence
        and the end code the PLC would have returned, and offers the alternative where
        there is one: on an iQ-F, ``monitor_register()`` says to use ``read_random()``,
        which is one round trip either way.

        Nothing here attempts the command to find out, and nothing substitutes a
        different command that happens to return similar-looking data.
        """
        if not isinstance(cap, Capability):
            raise TypeError(f"require() takes a Capability, not {type(cap).__name__}")
        entry = self.capabilities[cap]
        if isinstance(entry, Evidence):
            return
        code = (
            f" The CPU answers 0x{entry.end_code_if_attempted:04X}."
            if entry.end_code_if_attempted is not None
            else ""
        )
        raise SlmpCapabilityError(
            f"{what} needs {cap.commands}, which {self.key} does not have: "
            f"{entry.reason}{code} Evidence: {entry.evidence}.",
            diagnostics=_diagnostics(entry.evidence, action=entry.alternative),
        )

    # -- provenance ----------------------------------------------------------

    def evidence_for(self, name: str) -> Evidence:
        """The evidence behind one named fact, for ``aslmp cite`` and for an exception.

        Names that resolve: a capability value (``"monitor"``), a device family
        (``"D"``, ``"X"``), a limit key as :func:`_limit_key_text` renders it
        (``"0x0403/binary/word/cpu"``), and the profile-level facts
        (``"remote_fixed_field"``, ``"default_spec"``, ``"allowed_encodings"``,
        ``"allowed_clear_modes"``, ``"model_codes"``).
        """
        found = self._lookup_evidence(name)
        if found is not None:
            return found
        raise ValueError(
            f"{self.key} has no fact named {name!r}. Names are a capability "
            f"({', '.join(sorted(c.value for c in Capability))}), a device family, a "
            f"limit key such as '0x0403/binary/word/cpu', or one of "
            f"{', '.join(_PROFILE_FACT_NAMES)}."
        )

    def _lookup_evidence(self, name: str) -> Evidence | None:
        fact = self.facts.get(name)
        if fact is not None:
            return fact
        for cap, entry in self.capabilities.items():
            if cap.value == name:
                return entry if isinstance(entry, Evidence) else entry.evidence
        rng = self.devices.get(name)
        if rng is not None:
            return rng.evidence
        for limit_key, limit in self.limits.items():
            if _limit_key_text(limit_key) == name:
                return limit.evidence
        return None

    def sources(self) -> tuple[Source, ...]:
        """Every distinct manual section and measurement this profile rests on."""
        seen: list[Source] = []
        for evidence in self._all_evidence():
            origin = evidence.origin
            if origin is not None and origin not in seen:
                seen.append(origin)
        return tuple(seen)

    def _all_evidence(self) -> Iterable[Evidence]:
        yield from self.facts.values()
        for entry in self.capabilities.values():
            yield entry if isinstance(entry, Evidence) else entry.evidence
        for rng in self.devices.values():
            yield rng.evidence
        for limit in self.limits.values():
            yield limit.evidence

    def provenance_counts(self) -> Mapping[Provenance, int]:
        """How many shipped facts are ``LIVE``, ``MANUAL`` and ``INFERRED``.

        ``aslmp capabilities`` prints it. For an iQ-R it is all ``MANUAL`` and
        ``INFERRED``, which is the honest headline: there has never been an iQ-R here.
        """
        counts = dict.fromkeys(Provenance, 0)
        for evidence in self._all_evidence():
            counts[evidence.provenance] += 1
        return MappingProxyType(counts)

    # -- derivation ----------------------------------------------------------

    def replace(self, **changes: Any) -> CpuProfile:
        """A copy with ``changes`` applied, revalidated by ``__post_init__``."""
        return dataclasses.replace(self, **changes)

    def with_ranges(self, ranges: Mapping[str, DeviceRange]) -> CpuProfile:
        """A copy whose named device ranges are replaced by ``ranges``.

        The escape hatch for a repartitioned iQ-R, where every range is a GX Works3
        default that a project can move. It merges: families not named keep the shipped
        figures. It cannot conjure a family the CPU does not have -- that is a property
        of the silicon and no parameter file changes it -- so replacing an absent
        family's range is refused.
        """
        merged = dict(self.devices)
        for name, rng in ranges.items():
            if rng.device != name:
                raise SlmpConfigurationError(
                    f"with_ranges() was given {name!r} -> a DeviceRange for "
                    f"{rng.device!r}; the key and the range must name one family"
                )
            if name not in merged:
                raise SlmpConfigurationError(
                    f"{self.key} declares nothing about device family {name!r}; "
                    f"with_ranges() refines the shipped table and does not extend it"
                )
            if not merged[name].present and rng.present:
                raise SlmpConfigurationError(
                    f"{self.key} has no {name} device: {merged[name].absent_reason} "
                    f"Device existence is a property of the silicon and with_ranges() "
                    f"cannot grant it."
                )
            merged[name] = rng
        return self.replace(devices=merged)
