"""One command is one frozen object that carries its encoder AND its decoder.

Layer 2. May import ``aslmp.wire``, ``aslmp.errors`` and ``aslmp.profile``. Importing
this module must not pull ``socket``, ``ssl``, ``asyncio``, ``selectors``, ``threading``
or ``logging`` into ``sys.modules`` (``tests/unit/test_layering.py`` proves it in a
subprocess).

**Why encode and decode live on the same object.** A ``0403`` Read Random response is
bare data: no count echo, no per-item framing, nothing that says where the word section
ends and the double-word section begins (SH(NA)-080956ENG-M section 6.4 pp.53-56). The
only thing that can parse it is the request that produced it. Splitting the two into a
"builder" and a "parser" means two places that must agree about point order, and the
day they stop agreeing the symptom is plausible numbers in the wrong fields, with end
code ``0x0000`` and nothing anywhere to say so.

**Five methods, and each exists for a measured reason.**

``validate(ctx)``
    Raises :class:`~aslmp.errors.SlmpUsageError` before a byte is built. It is separate
    from :meth:`Command.encode` so a bound plan can validate once at bind and skip it in
    the hot path, and so every refusal is unit-testable with no socket. It is
    load-bearing rather than defensive: an FX5U-32MT/DS on firmware 1.065 **accepted** a
    ``TS0`` word point in a Read Random and answered ``0x0000`` with data, against its
    own manual, and it accepted the write of ``Y8`` -- an address octal notation cannot
    express (measured 2026-09-06, 2026-09-07). A design that leans on the PLC to reject
    illegal requests inherits whatever that firmware happens to do.
``payload_len(ctx)``
    The request data length, computed without building a throwaway buffer, so
    ``FrameFormat.build(expect_body_len=...)`` can check ``L`` against the bytes that
    were actually emitted. The failure it guards is asymmetric and both halves are
    measured: an **understated** ``L`` returns end code ``0xC061`` and the connection
    recovers, while an **overstated** one gets no response at all -- the CPU blocks
    waiting for bytes that never come and it is indistinguishable from a dead PLC.
``encode(ctx)`` / ``decode(payload, ctx)``
    Pure functions of the context. Neither touches a socket, a clock or a profile it was
    not handed.
``describe()``
    The call as the user wrote it, for the ``request`` line of every exception.

``mutates`` is a :class:`typing.ClassVar` with **no default**. A command class that
does not declare it does not import: :meth:`Command.__init_subclass__` raises. The flag
is what lets the client tell :class:`~aslmp.errors.SlmpNotSentError` ("provably did not
happen") from :class:`~aslmp.errors.SlmpOutcomeUnknownError` ("may have happened") on a
mid-flight failure, and a new command must not be able to forget the distinction.

``CITES`` is a non-empty tuple of :class:`~aslmp.wire.citations.Citation` or
:class:`~aslmp.wire.citations.Measurement`. It is checked at class creation, not by a
test that someone can forget to run: a Mitsubishi engineer must be able to open the page
that describes any byte this package emits, and ``aslmp cite 0x0403`` prints it.
"""

from __future__ import annotations

import abc
import enum
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Generic, Literal, TypeVar

from aslmp.errors import (
    SlmpAddressRangeError,
    SlmpConfigurationError,
    SlmpDeviceNotAllowedHereError,
    SlmpDeviceNotOnCpuError,
    SlmpFrameFormatError,
    SlmpPayloadShapeError,
    SlmpValueRangeError,
)
from aslmp.errors.routing import usage_error_for
from aslmp.profile import Capability, CpuProfile, Encoding, Link
from aslmp.wire.address import DeviceAddress, SlmpAddressError, parse_address
from aslmp.wire.citations import Ambiguity, Citation, Measurement, Source
from aslmp.wire.codec import Codec, Notation, SlmpCodecError, SpecFormat, Unit
from aslmp.wire.devicetable import DeviceType
from aslmp.wire.devspec import SlmpDeviceSpecError, devspec_len, encode_device_spec
from aslmp.wire.subcommand import subcommand as derive_subcommand

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

__all__ = [
    "COMMAND_LAYOUT",
    "FIXED_FIELD_UNITS",
    "AddressLike",
    "Command",
    "CommandSummary",
    "EncodeContext",
    "WordOrder",
    "expect_empty_payload",
    "expect_payload_len",
    "render_addresses",
    "signed",
    "unsigned",
]


COMMAND_LAYOUT: Citation = Citation(
    manual="SH(NA)-080956ENG",
    revision="M",
    section="6 pp.45-204",
    note=(
        "The command reference: every request is the 2-unit command, the 2-unit "
        "subcommand and then the command's own request data, and every response is the "
        "2-unit end code and then the command's own response data. A command's request "
        "data and its response layout are one specification, printed together."
    ),
)
"""The chapter every command in this package implements a section of."""

VALIDATE_CLIENT_SIDE: Measurement = Measurement(
    cpu="FX5U-32MT/DS",
    firmware="1.065",
    date="2026-09-06",
    note=(
        "A 0403 Read Random carrying one word point at TS0 (device code 0xC1) returned "
        "end code 0x0000 and one word of data, although JY997D56001-K p.78 forbids TS "
        "in Read Random and predicts CPU error 0x4032. The CPU accepted a request its "
        "own manual forbids, so every restriction this package knows about is enforced "
        "here, before the frame is built."
    ),
)
"""Why ``validate()`` refuses things the PLC would have allowed."""

LENGTH_ASYMMETRY: Measurement = Measurement(
    cpu="FX5U-32MT/DS",
    firmware="1.065",
    date="2026-09-06",
    note=(
        "A request whose data length L was understated by 2 was answered 0xC061 and the "
        "connection recovered. The same request with L OVERSTATED by 2 produced no "
        "response at all: the CPU blocked waiting for bytes that never came, and the "
        "3000 ms timeout is indistinguishable from a dead PLC. payload_len() exists so "
        "that L can be checked against the emitted bytes before the frame is sent."
    ),
)
"""Why ``payload_len`` is a separate method and not ``len(encode(...))``."""

FIXED_FIELD_UNITS: Literal[16] = 16
"""Width in bits of a two-byte fixed request field. Rendered by the codec, so it is
``00 00`` in binary and ``"0000"`` in ASCII, never a byte transformation of the other."""


class WordOrder(enum.Enum):
    """Which half of a 32-bit value the lower-numbered device holds.

    A **PLC-program convention, not a protocol fact**, which is why it is exposed even
    though low-word-first is measured four ways on FX5U-32MT/DS fw 1.065 (2026-09-06):
    writing 1234.5 as one ``1402`` double-word point put ``00 50 9A 44`` on the wire and
    read back ``D104 = 0x5000``, ``D105 = 0x449A``.

    It applies only to values a *caller* assembles from a batch read. It never applies
    to a ``0403`` / ``1402`` double-word access point, which is one IEEE-754 f32 natively
    and whose binary-versus-ASCII field order is a property of the codec
    (``BINARY.number(v, bits=32)`` is low word first, ``ASCII.number(v, bits=32)`` is
    high word first, and both are the same value). No command in this package reads
    ``EncodeContext.word_order``; it rides along so that the client and a bound plan
    share one frozen context object.
    """

    LOW_FIRST = "low-first"
    HIGH_FIRST = "high-first"


AddressLike = str | DeviceAddress
"""A device literal, or an address a profile has already resolved.

A string is **not** an address until a profile has said what base its digits are in:
``Y20`` is output 16 on an iQ-F and output 32 on an iQ-R, and both CPUs answer
``0x0000``. :meth:`EncodeContext.address` is the only place the two are joined.
"""


# ----------------------------------------------------------------------------------------
# The encoding context
# ----------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EncodeContext:
    """Everything a command needs to turn itself into bytes, and nothing else.

    Frozen: it is built once per connection and captured by every prebuilt frame, and a
    mutable one would let a plan validated against ``SpecFormat.SHORT`` be encoded with
    ``LONG`` underneath a live socket.

    ``codec`` and ``encoding`` are checked against each other at construction. They are
    two views of one GX Works3 parameter -- ``Encoding.ASCII_XY_OCT`` and
    ``Encoding.ASCII_XY_HEX`` share the ASCII codec and differ only in how an ``X``/``Y``
    device *number* is rendered -- and a context whose pair disagreed would emit binary
    bytes with ASCII lengths, which is the overstated-``L`` hang.

    ``allow_remote_control`` and ``validate_ranges`` are here rather than on the client
    alone because DESIGN section 1.5 makes them ``validate()`` refusals: a command must
    be able to say no before a byte is built, and ``validate()`` is handed nothing but
    this object.
    """

    codec: Codec
    spec: SpecFormat
    profile: CpuProfile
    encoding: Encoding
    link: Link
    word_order: WordOrder = WordOrder.LOW_FIRST
    allow_remote_control: bool = False
    validate_ranges: bool = True

    def __post_init__(self) -> None:
        if self.encoding not in self.profile.allowed_encodings:
            allowed = ", ".join(sorted(e.value for e in self.profile.allowed_encodings))
            raise SlmpConfigurationError(
                f"{self.profile.key} cannot use Encoding.{self.encoding.name}; it "
                f"allows {allowed}. 'ASCII code (X, Y OCT)' is an iQ-F own-node setting "
                f"and exists on no other family."
            )
        if self.codec.name != self.encoding.coding:
            raise SlmpConfigurationError(
                f"EncodeContext pairs the {self.codec.name} codec with "
                f"Encoding.{self.encoding.name}, whose coding is "
                f"{self.encoding.coding}. They are two views of one GX Works3 "
                f"Communication Data Code setting. A context whose pair disagrees emits "
                f"one coding's bytes with the other coding's lengths, and an overstated "
                f"data length gets no response at all ({LENGTH_ASYMMETRY.reference})."
            )
        if not isinstance(self.spec, SpecFormat):
            raise TypeError(f"EncodeContext.spec must be a SpecFormat, not {self.spec!r}")
        if not isinstance(self.link, Link):
            raise TypeError(f"EncodeContext.link must be a Link, not {self.link!r}")
        if not isinstance(self.word_order, WordOrder):
            raise TypeError(
                f"EncodeContext.word_order must be a WordOrder, not {self.word_order!r}"
            )

    # -- addresses -----------------------------------------------------------

    def address(self, value: AddressLike) -> DeviceAddress:
        """Resolve a literal against this profile, or pass an address through.

        The one door from ``"Y20"`` to a wire index. There is no module-level default
        profile and no generic radix: an unrecognised family read as hexadecimal ``X``/
        ``Y`` is silently two outputs off at ``Y20`` and worse as the address grows.

        It is also the one place a layer-0 parse refusal becomes its public face.
        ``aslmp.wire`` cannot raise ``SlmpDeviceRadixError`` -- layer 0 importing layer
        0.5 closes an import cycle -- so ``parse_address`` raises its own
        ``ValueError`` and :func:`~aslmp.errors.routing.usage_error_for` translates it
        here, at the first layer that may import :mod:`aslmp.errors`. Without that,
        ``V0`` (refused by the profile) would raise a ``SlmpUsageError`` and ``Y8``
        (refused by the parser) would not, and the two are the same mistake to the
        person who typed them.
        """
        if isinstance(value, DeviceAddress):
            return value
        if not isinstance(value, str):
            raise TypeError(
                f"a device address is a str or a DeviceAddress, not "
                f"{type(value).__name__}"
            )
        try:
            return parse_address(value, self.profile)
        except SlmpAddressError as parse_error:
            raise usage_error_for(parse_error) from parse_error

    def notation(self, dt: DeviceType) -> Notation:
        """Which base this connection writes ``dt``'s ASCII device number in."""
        return self.profile.notation_for(dt, self.encoding)

    def device_spec(self, address: DeviceAddress) -> bytes:
        """One device specification block: ``[number][code]`` binary, ``[code][number]``
        ASCII. Emitted by ``wire/devspec.py`` and nowhere else.

        Its refusals -- a device with no code in this spec format, ``OCTAL_DIGITS``
        against a binary codec -- are translated the same way ``address()`` translates
        the parser's, so ``SlmpCapabilityError`` and
        ``SlmpEncodingNotSupportedError`` are what a caller sees.
        """
        try:
            return encode_device_spec(
                address,
                codec=self.codec,
                spec=self.spec,
                notation=self.notation(address.type),
            )
        except SlmpDeviceSpecError as spec_error:
            raise usage_error_for(spec_error) from spec_error

    def device_spec_len(self) -> int:
        """Wire length of one device specification block: 4/6 binary, 8/12 ASCII."""
        return devspec_len(self.codec, self.spec)

    # -- subcommand ----------------------------------------------------------

    def subcommand(self, unit: Unit) -> int:
        """The subcommand for a request in ``unit`` units under this context's spec.

        Derived from the three facts that *are* the subcommand, never a literal at a
        call site: ``0401`` with a hard-coded ``0000`` against a bit device returns
        word-packed data that decodes into plausible booleans.
        """
        return derive_subcommand(unit, self.spec, False)

    def require_spec(self, *, what: str) -> None:
        """Refuse ``SpecFormat.LONG`` on a CPU that does not have it.

        An FX5U-32MT/DS on firmware 1.065 answered subcommand ``0x0002`` with
        ``0xC059`` (measured 2026-09-06), so on an iQ-F the long device specification is
        unreachable and every device that needs it -- ``LTN``, ``LSTN``, ``LZ``, ``RD``
        -- is unreachable with it.
        """
        if self.spec is SpecFormat.LONG:
            self.profile.require(Capability.LONG_DEVICE_SPEC, what=what)

    # -- ranges --------------------------------------------------------------

    def check_span(self, address: DeviceAddress, points: int, *, width: Unit) -> None:
        """Refuse unless the whole span ``points`` implies is on this CPU.

        Always the span, never the start: ``D7999`` alone is legal on an FX5U and
        ``D7999`` read as two words is not -- ``D8000`` returned ``0xC056``, measured.

        ``validate_ranges=False`` (DESIGN section 2.2) turns off the **range** check
        only, because on an iQ-R every range is repartitionable in GX Works3 and a stale
        table would block legitimate work. Device *existence* and the bit-unit-versus-
        word-device rule stay on: those are properties of the silicon, not of the
        parameter file.
        """
        if self.validate_ranges:
            self.profile.check_range(address, points, width=width)
            return
        self._require_present(address)
        self._require_width(address, width)

    def _require_present(self, address: DeviceAddress) -> None:
        rng = self.profile.range_for(address.type)
        if not rng.present:
            raise SlmpDeviceNotOnCpuError(
                f"{self.profile.key} has no {address.type.name} "
                f"({address.type.long_name.lower()}) device, so {address} cannot be "
                f"addressed. {rng.absent_reason} validate_ranges=False turns off the "
                f"range table, not the device table: whether the silicon has a family "
                f"is not a parameter."
            )
        if rng.points == 0:
            raise SlmpAddressRangeError(
                f"{self.profile.key} has 0 points of {address.type.name} allocated, so "
                f"{address} does not exist on it. {rng.evidence.note}"
            )

    def _require_width(self, address: DeviceAddress, width: Unit) -> None:
        if width is Unit.BIT and address.type.unit is not Unit.BIT:
            raise SlmpDeviceNotAllowedHereError(
                f"{address.type.name} is a word device, so a bit-unit request cannot "
                f"address {address}. Read it as words and select the bit from the value."
            )


# ----------------------------------------------------------------------------------------
# What an exception may carry about a request
# ----------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CommandSummary:
    """A :class:`~aslmp.wire.raw.RequestSummary` for a built command.

    ``aslmp.errors`` is layer 0.5 and this package is layer 2, so an exception may not
    name a command class. It carries one of these instead: four members, structurally
    typed, no import edge.
    """

    command: int
    subcommand: int
    request_bytes: int
    text: str

    def describe(self) -> str:
        """The call as the user wrote it: ``read_random(['D0', 'D4'])``."""
        return self.text

    def __str__(self) -> str:
        return self.text


# ----------------------------------------------------------------------------------------
# Decode-side helpers
# ----------------------------------------------------------------------------------------


def expect_payload_len(payload: bytes, expected: int, *, what: str) -> None:
    """Refuse a response whose length does not match the request's point layout.

    A response one word short is not three good values and a zero. ``pymcprotocol``
    turns ``[111, 222, 333, 444]`` into ``[111, 222, 0, 0]`` at exactly this point.
    """
    if len(payload) == expected:
        return
    raise SlmpPayloadShapeError(
        f"{what}: the request implies {expected} wire unit(s) of response data, but "
        f"{len(payload)} arrived. The end code was 0x0000, so this is a response whose "
        f"shape we do not understand, not a failure the PLC reported. Nothing here "
        f"zero-fills the difference or truncates to fit."
    )


def expect_empty_payload(payload: bytes, *, what: str) -> None:
    """Refuse response data on a command that has none.

    ``1402``, ``1401``, ``0801`` and every remote-control command answer with the
    11-byte minimum frame ``D0 00 00 FF FF 03 00 02 00 00 00`` -- ``L = 2``, end code,
    nothing after it (measured on FX5U-32MT/DS fw 1.065, 2026-09-06). Bytes after the
    end code here mean the response is not the one this request implies.
    """
    if not payload:
        return
    raise SlmpPayloadShapeError(
        f"{what}: this command defines no response data, but {len(payload)} wire "
        f"unit(s) followed the end code. On FX5U-32MT/DS fw 1.065 the response to a "
        f"write is the 11-byte minimum frame with L = 0x0002 and nothing after the end "
        f"code (measured 2026-09-06). Refusing to guess whose bytes these are."
    )


def unsigned(value: int, *, bits: int, what: str) -> int:
    """``value`` as an unsigned ``bits``-wide field, refusing anything that does not fit.

    Never masked and never truncated: ``pymcprotocol`` writes ``0x1FFFF`` into a 16-bit
    register as ``0xFFFF`` and reports success. A negative value is accepted as its
    two's-complement rendering, which is what a signed device value is.

    Raises :class:`~aslmp.errors.SlmpValueRangeError` -- DESIGN section 3.1's class for
    "value outside the declared field's domain" -- and not the general
    ``SlmpConfigurationError``, so that a caller can tell a bad *datum* from an
    incoherently *configured* client in one ``except`` clause. Both are
    ``SlmpUsageError`` and both mean nothing was sent.
    """
    limit = 1 << bits
    if -(limit >> 1) <= value < limit:
        return value & (limit - 1)
    raise SlmpValueRangeError(
        f"{what}: {value} does not fit a {bits}-bit device field. The signed range is "
        f"{-(limit >> 1)}..{(limit >> 1) - 1} and the unsigned range is 0..{limit - 1}. "
        f"Nothing here masks, clamps or wraps."
    )


def signed(value: int, *, bits: int) -> int:
    """Reinterpret an unsigned wire value as two's complement."""
    limit = 1 << bits
    return value - limit if value >= (limit >> 1) else value


# ----------------------------------------------------------------------------------------
# The command base
# ----------------------------------------------------------------------------------------

R_co = TypeVar("R_co", covariant=True)


def _check_class(cls: type[Command[object]]) -> None:
    """Refuse a command class that does not declare what every command must declare."""
    code = getattr(cls, "CODE", None)
    if not isinstance(code, int) or isinstance(code, bool) or not 0 <= code <= 0xFFFF:
        raise TypeError(
            f"{cls.__qualname__} must declare CODE as a 16-bit SLMP command, e.g. "
            f"CODE = 0x0403; got {code!r}"
        )
    name = getattr(cls, "NAME", None)
    if not isinstance(name, str) or not name.strip():
        raise TypeError(
            f"{cls.__qualname__} must declare NAME, the command's Mitsubishi name, e.g. "
            f"NAME = 'Device Read Random'; got {name!r}"
        )
    mutates = cls.__dict__.get("mutates", getattr(cls, "mutates", None))
    if not isinstance(mutates, bool):
        raise TypeError(
            f"{cls.__qualname__} does not declare `mutates: ClassVar[bool]`. It drives "
            f"the difference between SlmpNotSentError ('provably did not happen') and "
            f"SlmpOutcomeUnknownError ('may have happened') when a transaction fails "
            f"mid-flight, and a new command must not be able to forget it."
        )
    cites = getattr(cls, "CITES", None)
    if not isinstance(cites, tuple) or not cites:
        raise TypeError(
            f"{cls.__qualname__} must declare CITES as a non-empty tuple of Citation or "
            f"Measurement. A Mitsubishi engineer has to be able to open the page that "
            f"describes any byte this package emits; got {cites!r}"
        )
    for index, source in enumerate(cites):
        if not isinstance(source, Citation | Measurement):
            raise TypeError(
                f"{cls.__qualname__}.CITES[{index}] is {type(source).__name__}; every "
                f"entry is a Citation (a manual, its revision and a section) or a "
                f"Measurement (a CPU model and a firmware)"
            )


class Command(abc.ABC, Generic[R_co]):
    """One SLMP command: its request bytes and its response decoder, together.

    Subclasses are frozen, slotted dataclasses. An intermediate base that is not itself
    a command passes ``abstract=True`` and skips the declaration checks::

        class _DeviceCommand(Command[R_co], abstract=True): ...

    ``R_co`` is what :meth:`decode` returns: ``tuple[int, ...]`` for a word batch read,
    ``None`` for a write, ``bytes`` for the ``0619`` echo. A command that returns
    ``None`` is not a command whose result was thrown away -- it is a command SLMP
    defines no response data for, and :func:`expect_empty_payload` proves the wire
    agreed.
    """

    __slots__ = ()

    ABSTRACT: ClassVar[bool] = False
    """Set by ``abstract=True``. Kept as a class attribute rather than only as a class
    keyword because ``@dataclass(slots=True)`` rebuilds the class, which re-runs
    ``__init_subclass__`` without the original keywords."""

    CODE: ClassVar[int]
    """The 16-bit command, e.g. ``0x0403``. On the wire it is ``codec.number(CODE, 16)``
    -- ``03 04`` in binary, ``"0403"`` in ASCII."""

    NAME: ClassVar[str]
    """Mitsubishi's own name for the command, quoted verbatim in diagnostics."""

    mutates: ClassVar[bool]
    """Whether a completed request changes the target. Abstract: see
    :func:`_check_class`."""

    CITES: ClassVar[tuple[Source, ...]]
    """At least one manual section or measurement. Checked at class creation."""

    AMBIGUITIES: ClassVar[tuple[Ambiguity, ...]] = ()
    """Places where the sources contradict each other about THIS command's bytes.

    Separate from :attr:`CITES` because an ambiguity is not a source: it is a record
    that two sources disagree, what this library chose, and the one experiment that
    would settle it. ``aslmp ambiguities`` prints them next to the profile's own.
    """

    response_optional: ClassVar[bool] = False
    """Whether an ABSENT response is a documented outcome rather than a timeout.

    True for ``0x1006`` Remote Reset alone: SH(NA)-080956ENG-M p.136 says that when the
    reset succeeds "the response request is not be sent back to the external device",
    and over TCP the connection is torn down with it. Every other command in this
    package treats silence as a failure.
    """

    def __init_subclass__(cls, *, abstract: bool = False, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if abstract:
            cls.ABSTRACT = True
        if cls.__dict__.get("ABSTRACT", False):
            return
        _check_class(cls)

    # -- the five methods ----------------------------------------------------

    @abc.abstractmethod
    def subcommand(self, ctx: EncodeContext) -> int:
        """The subcommand this request carries. Derived, never a literal."""

    @abc.abstractmethod
    def validate(self, ctx: EncodeContext) -> None:
        """Raise :class:`~aslmp.errors.SlmpUsageError` if this request must not be sent.

        No I/O, no clock, no state. Everything it refuses, it refuses before a byte is
        built, which is why every refusal has its own test.
        """

    @abc.abstractmethod
    def payload_len(self, ctx: EncodeContext) -> int:
        """Wire length of the request data, without building it."""

    @abc.abstractmethod
    def encode(self, ctx: EncodeContext) -> bytes:
        """The request data: everything after the subcommand."""

    @abc.abstractmethod
    def decode(self, payload: bytes, ctx: EncodeContext) -> R_co:
        """The response data for **this** request. Raises rather than guessing."""

    @abc.abstractmethod
    def describe(self) -> str:
        """The call as a user would recognise it, for an exception's request line."""

    # -- shared machinery ----------------------------------------------------

    def body_len(self, ctx: EncodeContext) -> int:
        """``L`` for this request: monitoring timer, command, subcommand, payload.

        The value to hand :meth:`aslmp.wire.frames.FrameFormat.build` as
        ``expect_body_len``. It is computed from :meth:`payload_len`, never from
        ``len(encode(...))``, so the two can be checked against each other
        (:meth:`checked_encode`).
        """
        return 3 * ctx.codec.number_len(16) + self.payload_len(ctx)

    def checked_encode(self, ctx: EncodeContext) -> bytes:
        """:meth:`encode`, with the ``payload_len`` agreement asserted as a raise.

        The property test asserts ``payload_len(ctx) == len(encode(ctx))`` over
        generated inputs; this is the same invariant as a runtime guard, for a caller
        who would rather pay a comparison than risk the hang
        (:data:`LENGTH_ASYMMETRY`).
        """
        payload = self.encode(ctx)
        declared = self.payload_len(ctx)
        if declared == len(payload):
            return payload
        raise SlmpConfigurationError(
            f"{type(self).__qualname__}.payload_len() says {declared} wire unit(s) and "
            f"encode() produced {len(payload)}. Refusing to send: an understated data "
            f"length returns end code 0xC061 and an OVERSTATED one gets no response at "
            f"all ({LENGTH_ASYMMETRY.reference}). This is a bug in aslmp, not in your "
            f"call -- please report it with the command and the coding."
        )

    def checked_decode(self, payload: bytes, ctx: EncodeContext) -> R_co:
        """:meth:`decode`, with a codec refusal translated into the public tree.

        **This is the door the client must use**, not :meth:`decode` directly.

        ``ASCII.read_words`` refuses a character outside ``[0-9A-F]`` and the binary
        bit reader refuses a nibble that is neither ``0`` nor ``1``, both raising
        :class:`~aslmp.wire.codec.SlmpCodecError` -- a plain ``ValueError``, because
        layer 0 cannot import :mod:`aslmp.errors` without closing an import cycle.
        Reading it as zero instead is ``libslmp2``'s ``wordcodec.c:27``, which turns
        a corrupt response into a plausible ``0``; letting it escape untranslated is
        almost as bad, because a caller's ``except SlmpProtocolError`` around a read
        would not catch the one failure it exists for.

        DESIGN section 3.1 files "non-hex in ASCII" under ``SlmpFrameFormatError``.
        The codec error stays as ``__cause__``, carrying its own offset and character.
        """
        try:
            return self.decode(payload, ctx)
        except SlmpCodecError as codec_error:
            raise SlmpFrameFormatError(
                f"the response payload of {self.describe()} does not decode in "
                f"{ctx.codec.name}: {codec_error}"
            ) from codec_error

    def summary(self, ctx: EncodeContext, *, request_bytes: int = 0) -> CommandSummary:
        """What an exception may carry about this request.

        Takes the context because the subcommand is not a property of the command
        alone: ``0401`` is ``0000`` in word units and ``0001`` in bit units, and both
        are the same class.
        """
        return CommandSummary(
            command=self.CODE,
            subcommand=self.subcommand(ctx),
            request_bytes=request_bytes,
            text=self.describe(),
        )

    def cites(self) -> tuple[Source, ...]:
        """Every manual section and measurement behind this command's bytes."""
        return self.CITES

    def __str__(self) -> str:
        return self.describe()


def render_addresses(addresses: Sequence[object]) -> str:
    """``['D0', 'D4', 'D8']`` -- how a describe() line names a point list."""
    shown = [str(address) for address in addresses[:6]]
    if len(addresses) > 6:
        shown.append(f"... {len(addresses)} total")
    return "[" + ", ".join(repr(text) for text in shown) + "]"
