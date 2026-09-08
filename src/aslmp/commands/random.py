"""``0403`` Device Read Random and ``1402`` Device Write Random -- the control loop.

Layer 2. This is the module the rest of the library exists to serve.

**Why it matters, measured.** Reading three floats as three ``0401`` batch reads costs
21.8 ms at p50 against 7.2 ms for one ``0403`` carrying all three -- 3.04x at p50, 4.08x
at p99, and 4.1x less jitter (stdev 1.27 ms against 5.22 ms; FX5U-32MT/DS fw 1.065,
n=200 each, interleaved, 2026-09-06). The latency is the smaller half of the argument.
Three separate reads sample the plant at three moments **up to 27 ms apart**; one
``0403`` returns one consistent snapshot. For a control loop that is a correctness
property, not a performance one.

**One double-word access point is one IEEE-754 f32, natively.** On a word device a
double-word point is two consecutive registers, **low word first**, which is exactly the
FX5U's f32 convention -- proved four ways on that CPU (2026-09-06), including writing
1234.5 as one ``1402`` double-word point and reading back ``D104 = 0x5000``,
``D105 = 0x449A``. No byte-swapping helper exists in this library, and
``WordOrder`` deliberately does not apply here: the binary-versus-ASCII flip is a
property of the codec, where ``BINARY.number(v, bits=32)`` puts the low word first and
``ASCII.number(v, bits=32)`` puts the high word first, and both are the same value.

**The ordering rules are the whole request format.** There is no per-item type tag and
no count echo in the response. The two one-byte counts at the head of the request are
the only thing that says where the word section ends and the double-word section begins,
and the response is bare data (SH(NA)-080956ENG-M section 6.4 pp.53-56). So:

1. every word-access device specification comes first, in result order;
2. then every double-word-access specification, in result order;
3. no interleaving, and a section with a count of zero is absent entirely;
4. the response is one word per word point then two words per double-word point, and
   nothing else.

This package therefore keeps the request shape alongside the transaction, tags every
point with its caller index, sorts stably into the two wire groups, and restores caller
order before returning. Aliasing is legal SLMP and is allowed: reading ``D0`` as a
``u16`` and ``D0``-``D1`` as an ``f32`` in one request is a well-formed thing to want.

**Client-side validation is load-bearing here, not defensive.** JY997D56001-K p.78
forbids ``TS``, ``TC``, ``STS``, ``STC``, ``CS`` and ``CC`` in Read Random and predicts
CPU error ``0x4032``. An FX5U-32MT/DS on firmware 1.065 was sent ``0403`` with one word
point at ``TS0`` (device code ``0xC1``) and answered **``0x0000`` with a word of data**
(measured 2026-09-06). Any design that leans on the PLC to reject documented-illegal
requests inherits whatever that firmware happens to do, so ``DeviceType.random_ok``
refuses them here.

**The 192-point ceiling only exists client-side.** The two point-count fields are one
byte each, so 255 is expressible and nothing on the wire stops you; 193 points returned
``0xC054`` on that CPU (measured 2026-09-06).
"""

from __future__ import annotations

import enum
import struct
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Final, Literal, TypeAlias, TypeVar

from aslmp.commands.base import (
    AddressLike,
    Command,
    EncodeContext,
    expect_empty_payload,
    expect_payload_len,
    render_addresses,
    signed,
    unsigned,
)
from aslmp.errors import (
    SlmpConfigurationError,
    SlmpDeviceNotAllowedHereError,
    SlmpPointLimitError,
)
from aslmp.profile import Capability
from aslmp.wire.address import DeviceAddress
from aslmp.wire.citations import Citation, Measurement, Source
from aslmp.wire.codec import Codec, SpecFormat, Unit
from aslmp.wire.devicetable import DeviceType

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable, Mapping

__all__ = [
    "MAX_POINT_COUNT_FIELD",
    "RANDOM_CEILING",
    "RANDOM_LAYOUT",
    "TS_ACCEPTED_ANYWAY",
    "WORD_ORDER_PROOF",
    "AccessWidth",
    "BitWrite",
    "PointKind",
    "RandomPoint",
    "RandomValue",
    "RandomWrite",
    "ReadRandom",
    "WriteRandom",
    "WriteRandomBits",
    "bit_point",
    "dword",
    "point_specs",
    "point_specs_len",
    "split_points",
    "word",
]


RANDOM_LAYOUT: Citation = Citation(
    manual="SH(NA)-080956ENG",
    revision="M",
    section="6.4 pp.53-61",
    note=(
        "Device Read Random 0403H and Device Write Random 1402H. Binary request: "
        "command, subcommand, number of word access points (ONE byte), number of "
        "double-word access points (ONE byte), then every word-access device "
        "specification followed by every double-word-access one. ASCII: the same, with "
        "each count as two characters. There is no bit-unit variant of 0403. The "
        "response is one word per word point then two words per double-word point, with "
        "no count echo and no per-item framing. p.55 prints the same request in both "
        "codings, and p.56 the binary response 4E 4F 54 4C for D1500=4F4EH D1501=4C54H "
        "-- low word first -- against the ASCII '4C54' '4F4E', high word first."
    ),
)
"""The request layout, the ordering rules, and the codings' opposite dword word order."""

FX5_RANDOM: Citation = Citation(
    manual="JY997D56001",
    revision="K",
    section="4.3 pp.76-88",
    note=(
        "The FX5's own copy. p.77 footnote 1 gives 123 points through an FX5-ENET "
        "module against 192 on the CPU built-in port, which is why every LimitKey "
        "carries the Link. p.78 forbids TS, TC, STS, STC, CS and CC in Read Random. "
        "1402 word units: word points x 12 + double-word points x 14 <= 1920; bit "
        "units: 188 points."
    ),
)
"""The FX5 restatement, the per-link ceiling and the device exclusions."""

RANDOM_CEILING: Measurement = Measurement(
    cpu="FX5U-32MT/DS",
    firmware="1.065",
    date="2026-09-06",
    note=(
        "192 word-access points returned 0x0000 with 384 bytes; 193 returned 0xC054. "
        "192 double-word points returned 0x0000 with 768 bytes, and 96 word + 96 "
        "double-word returned 0x0000 with 576 bytes. The constraint is word points plus "
        "double-word points <= 192, independent of how many words come back. The "
        "point-count fields are one byte each, so the wire format will not stop you: "
        "this limit exists only client-side."
    ),
)
"""The measured Read Random ceiling."""

TS_ACCEPTED_ANYWAY: Measurement = Measurement(
    cpu="FX5U-32MT/DS",
    firmware="1.065",
    date="2026-09-06",
    note=(
        "0403 with one word point at TS0 (device code 0xC1) returned end code 0x0000 "
        "and one word of data, although JY997D56001-K p.78 forbids TS in Read Random "
        "and predicts CPU error 0x4032. DeviceType.random_ok is the only thing that "
        "stops it. See ambiguity A-TS-IN-RANDOM."
    ),
)
"""Why the device exclusions are enforced here and not left to the CPU."""

WORD_ORDER_PROOF: Measurement = Measurement(
    cpu="FX5U-32MT/DS",
    firmware="1.065",
    date="2026-09-06",
    note=(
        "1234.5 written as one 1402 double-word point at D104 put 00 50 9A 44 on the "
        "wire and read back D104 = 0x5000, D105 = 0x449A: the lower-numbered device "
        "holds the low word. Confirmed three more ways -- a 1401 batch write read back "
        "as a 0403 double-word point, the live plant read as raw word pairs, and a "
        "round trip on the live setpoint D0/D1."
    ),
)
"""One double-word access point is one f32, low word first."""

MAX_POINT_COUNT_FIELD: int = 0xFF
"""The largest value the one-byte point-count field can carry.

``slmp-rs`` computes a point count as ``.count() as u8``, so 256 points wrap to 0 and
the PLC is asked for nothing. Here anything above this raises, and the real ceiling --
192 on the FX5 built-in port -- is refused earlier still by the profile.
"""

_CITES: tuple[Source, ...] = (
    RANDOM_LAYOUT,
    FX5_RANDOM,
    RANDOM_CEILING,
    TS_ACCEPTED_ANYWAY,
    WORD_ORDER_PROOF,
)

PointKind: TypeAlias = Literal["u16", "i16", "u32", "i32", "f32", "bits"]
"""How a point's raw words are interpreted. Not a wire fact: the wire carries words."""

RandomValue: TypeAlias = int | float | tuple[bool, ...]
"""What one decoded point is worth. A union, deliberately, and never ``Any``: the type
depends on the point's ``kind``, and ``RandomReading``'s typed accessors narrow it."""

_WORD_KINDS: frozenset[str] = frozenset({"u16", "i16", "bits"})
_DWORD_KINDS: frozenset[str] = frozenset({"u32", "i32", "f32"})
_WRITE_KINDS: frozenset[str] = frozenset({"u16", "i16", "u32", "i32", "f32"})

_POINT_DOMAINS: Final[Mapping[PointKind, bool | None]] = {
    "i16": True,
    "i32": True,
    "u16": None,
    "u32": None,
    "f32": None,
    "bits": None,
}
"""The integer domain each ``kind`` names, in :func:`~aslmp.commands.base.unsigned`'s
vocabulary. One table, read by :attr:`RandomPoint.signed_field`.

``i16``/``i32`` name a signed type and are enforced as one. ``u16``/``u32`` are the kinds
:meth:`RandomPoint.__str__` prints with **no suffix at all**, because they are what a
register is when nobody has said otherwise: the union of the two renderings stays legal
there, exactly as it does for :meth:`~aslmp.client.Plc.write_words`. ``f32`` and ``bits``
are ``None`` because they never reach ``unsigned()`` -- an f32 point is checked by
:func:`~aslmp.commands.base.real` and packed through ``struct``, and a ``bits`` point
cannot be written by ``1402`` in word units at all (:class:`RandomWrite` refuses it at
construction). They are in the table rather than absent so that a lookup here is a total
function: a ``.get()`` with a permissive fallback is how a new kind would silently
inherit the union."""


class AccessWidth(enum.Enum):
    """Whether one access point is one word or two consecutive words.

    On a **word** device a word point is one register and a double-word point is two,
    low word first. On a **bit** device a word point is 16 consecutive bits with the
    named device as the least significant bit, and a double-word point is 32
    (SH(NA)-080956ENG-M p.54). The wire carries no type tag at all: the two counts at
    the head of the request are the only boundary.
    """

    WORD = "word"
    DWORD = "dword"

    @property
    def words(self) -> int:
        """How many 16-bit words one point of this width occupies: 1 or 2."""
        return 1 if self is AccessWidth.WORD else 2

    @property
    def bits(self) -> Literal[16, 32]:
        """The numeric field width one point of this width is read and written as."""
        return 16 if self is AccessWidth.WORD else 32


@dataclass(frozen=True, slots=True)
class RandomPoint:
    """One access point of a ``0403`` / ``1402`` / ``0801`` request.

    ``address`` may be a literal until a profile has resolved it: ``"Y20"`` is output 16
    on an iQ-F and output 32 on an iQ-R, and both CPUs answer ``0x0000``, so a string is
    not an address until :meth:`resolve` has been given the profile that says which.
    ``kind`` is a client-side interpretation of the point's words and changes no byte on
    the wire; ``width`` does.
    """

    address: AddressLike
    width: AccessWidth
    kind: PointKind

    def __post_init__(self) -> None:
        if not isinstance(self.width, AccessWidth):
            raise TypeError(
                f"RandomPoint.width must be an AccessWidth, not {self.width!r}; build "
                f"points with word(), dword() or bit_point()"
            )
        allowed = _WORD_KINDS if self.width is AccessWidth.WORD else _DWORD_KINDS
        if self.kind not in allowed:
            raise SlmpConfigurationError(
                f"a {self.width.value}-access point cannot be read as {self.kind!r}; "
                f"the kinds for that width are {sorted(allowed)}. A word point is one "
                f"word (or 16 bits on a bit device) and a double-word point is two "
                f"consecutive words, low word first ({RANDOM_LAYOUT.reference})."
            )

    def resolve(self, ctx: EncodeContext) -> RandomPoint:
        """This point with its address resolved against ``ctx``'s profile. Idempotent."""
        if isinstance(self.address, DeviceAddress):
            return self
        return RandomPoint(ctx.address(self.address), self.width, self.kind)

    @property
    def words(self) -> int:
        """How many 16-bit words this point contributes to the response."""
        return self.width.words

    @property
    def signed_field(self) -> bool | None:
        """This point's declared integer domain, for
        :func:`~aslmp.commands.base.unsigned` (:data:`_POINT_DOMAINS`).

        A point carries its own type, so a write through it is a write to a *named*
        type, not to a raw register -- which is the whole of the fix in
        :meth:`RandomWrite.wire_value`.
        """
        return _POINT_DOMAINS[self.kind]

    def __str__(self) -> str:
        suffix = "" if self.kind in ("u16", "u32") else f":{self.kind}"
        return f"{self.address}{suffix}"


def word(address: AddressLike, *, kind: Literal["u16", "i16", "bits"] = "u16") -> RandomPoint:
    """One word-access point: one register, or 16 bits from a bit device."""
    return RandomPoint(address, AccessWidth.WORD, kind)


def dword(
    address: AddressLike, *, kind: Literal["u32", "i32", "f32"] = "u32"
) -> RandomPoint:
    """One double-word access point: two consecutive registers, low word first.

    ``dword("D0", kind="f32")`` is one IEEE-754 float in one point, natively, with no
    byte-swapping helper anywhere (:data:`WORD_ORDER_PROOF`).
    """
    return RandomPoint(address, AccessWidth.DWORD, kind)


def bit_point(address: AddressLike) -> RandomPoint:
    """One word-access point over a bit device: 16 bits, LSB = the named device."""
    return RandomPoint(address, AccessWidth.WORD, "bits")


# ----------------------------------------------------------------------------------------
# Shared point machinery -- also used by commands/monitor.py for 0801
# ----------------------------------------------------------------------------------------


def split_points(
    points: tuple[RandomPoint, ...],
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """Caller indices of the word points and of the double-word points, in order.

    A stable partition, not a sort: the wire demands all word specifications before all
    double-word ones, and the caller's relative order inside each group is the order the
    response data arrives in. Returning indices rather than points is what lets
    :meth:`ReadRandom.decode` put the values back where the caller asked for them.
    """
    words: list[int] = []
    dwords: list[int] = []
    for index, point in enumerate(points):
        target = words if point.width is AccessWidth.WORD else dwords
        target.append(index)
    return tuple(words), tuple(dwords)


def point_specs(points: tuple[RandomPoint, ...], ctx: EncodeContext) -> bytes:
    """The two one-byte counts and every device specification, in wire order."""
    word_ix, dword_ix = split_points(points)
    out = [
        ctx.codec.number(len(word_ix), bits=8),
        ctx.codec.number(len(dword_ix), bits=8),
    ]
    for index in (*word_ix, *dword_ix):
        out.append(ctx.device_spec(ctx.address(points[index].address)))
    return b"".join(out)


def point_specs_len(points: tuple[RandomPoint, ...], ctx: EncodeContext) -> int:
    """Wire length of :func:`point_specs`, without building it."""
    return 2 * ctx.codec.number_len(8) + len(points) * ctx.device_spec_len()


def _decode_one(point: RandomPoint, raw: int) -> RandomValue:
    """One point's raw 16- or 32-bit wire value, as the caller asked to see it."""
    if point.kind == "u16" or point.kind == "u32":
        return raw
    if point.kind == "i16":
        return signed(raw, bits=16)
    if point.kind == "i32":
        return signed(raw, bits=32)
    if point.kind == "f32":
        return float(struct.unpack("<f", struct.pack("<I", raw))[0])
    return tuple(bool(raw >> bit & 1) for bit in range(16))


def decode_point_values(
    points: tuple[RandomPoint, ...], payload: bytes, ctx: EncodeContext, *, what: str
) -> tuple[RandomValue, ...]:
    """The response data for ``points``, restored to caller order.

    The word section first, one word per point, then the double-word section, two words
    per point. The length is checked exactly: a response one word short is not good
    values and a zero.
    """
    word_ix, dword_ix = split_points(points)
    codec: Codec = ctx.codec
    expected = len(word_ix) * codec.number_len(16) + len(dword_ix) * codec.number_len(32)
    expect_payload_len(payload, expected, what=what)
    values: list[RandomValue] = [0] * len(points)
    offset = 0
    for index in word_ix:
        values[index] = _decode_one(points[index], codec.read_number(payload, offset, bits=16))
        offset += codec.number_len(16)
    for index in dword_ix:
        values[index] = _decode_one(points[index], codec.read_number(payload, offset, bits=32))
        offset += codec.number_len(32)
    return tuple(values)


def _random_ok(dt: DeviceType) -> bool:
    """The ``0403`` / ``1402`` device gate: contacts and coils are excluded."""
    return dt.random_ok


def validate_points(
    points: tuple[RandomPoint, ...],
    ctx: EncodeContext,
    *,
    what: str,
    command: int,
    capability: Capability,
    allowed: Callable[[DeviceType], bool],
    limit_command: int | None = None,
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """Every pre-transport refusal shared by ``0403``, ``1402`` and ``0801``.

    ``allowed`` is the ``DeviceType`` column that gates this command -- ``random_ok``
    for ``0403`` and ``1402``, ``monitor_ok`` for ``0801`` -- so the message says which
    list the device is missing from. ``limit_command`` is for ``0801``, whose points-
    per-request budget is ``0403``'s: SH(NA)-080956ENG-M p.65 says the limits are
    identical and no profile ships a separate key.
    """
    ctx.profile.require(capability, what=what)
    ctx.require_spec(what=what)
    for point in points:
        address = ctx.address(point.address)
        if not allowed(address.type):
            raise SlmpDeviceNotAllowedHereError(
                f"{address.type.name} ({address.type.long_name.lower()}) cannot be used "
                f"as an access point of 0x{command:04X}. {FX5_RANDOM.reference} excludes "
                f"the timer, retentive timer and counter contacts and coils; the current "
                f"values TN, STN, CN and LCN are fine. This refusal is client-side on "
                f"purpose: {TS_ACCEPTED_ANYWAY.reference} accepted TS0 in a Read Random "
                f"and answered 0x0000 with data."
            )
        if point.kind == "bits" and address.type.unit is not Unit.BIT:
            raise SlmpDeviceNotAllowedHereError(
                f"{address} is a word device, so a word access point there is one "
                f"register and not 16 bits. Read it with kind='u16' and select the bit "
                f"from the value; kind='bits' is for bit devices, where one word access "
                f"point IS 16 consecutive bits ({RANDOM_LAYOUT.reference})."
            )
        ctx.check_span(address, point.width.words, width=Unit.WORD)
    word_ix, dword_ix = split_points(points)
    for name, group in (("word", word_ix), ("double-word", dword_ix)):
        if len(group) > MAX_POINT_COUNT_FIELD:
            raise SlmpPointLimitError(
                f"{what} has {len(group)} {name} access points and the count field is "
                f"one byte, so {MAX_POINT_COUNT_FIELD} is the most that can be "
                f"expressed. Nothing here truncates the count to fit -- a wrapped count "
                f"asks the PLC for a different request that completes normally."
            )
    budget = command if limit_command is None else limit_command
    key = (budget, ctx.encoding, Unit.WORD, ctx.link)
    ctx.profile.check_points(key, word=len(word_ix), dword=len(dword_ix))
    return word_ix, dword_ix


def _normalise(points: object, what: str) -> tuple[RandomPoint, ...]:
    if isinstance(points, RandomPoint):
        raise TypeError(
            f"{what} takes a sequence of RandomPoint, not a single one; wrap it in a "
            f"list. A one-point Read Random is legal but it is a batch read with extra "
            f"steps."
        )
    if not isinstance(points, tuple | list):
        raise TypeError(f"{what} takes a sequence of RandomPoint, not {type(points).__name__}")
    out = tuple(points)
    for index, point in enumerate(out):
        if not isinstance(point, RandomPoint):
            raise TypeError(
                f"{what} point {index} is {type(point).__name__}; build points with "
                f"word(), dword() or bit_point()"
            )
    return out


R_co = TypeVar("R_co", covariant=True)


class _Random(Command[R_co], abstract=True):
    """Shared subcommand derivation for the random-access family."""

    __slots__ = ()

    UNIT: ClassVar[Unit] = Unit.WORD

    def subcommand(self, ctx: EncodeContext) -> int:
        return ctx.subcommand(self.UNIT)


# ----------------------------------------------------------------------------------------
# 0403 Device Read Random
# ----------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReadRandom(_Random[tuple[RandomValue, ...]]):
    """``0403``: one consistent snapshot of scattered devices, in one round trip.

    Golden vector, SH(NA)-080956ENG-M p.56 -- four word points (``D0``, ``TN0``,
    ``M100``, ``X20``) and three double-word points (``D1500``, ``Y160``, ``M1111``),
    binary::

        request data : 04 03
                       00 00 00 A8  00 00 00 C2  64 00 00 90  20 00 00 9C
                       DC 05 00 A8  60 01 00 9D  57 04 00 90
        response data: 95 19  02 12  30 20  49 48
                       4E 4F 54 4C  AF B9 DE C3  B7 BC DD BA

    ``4E 4F 54 4C`` is ``D1500 = 0x4F4E`` then ``D1501 = 0x4C54``: the low word first,
    which is why one double-word point is one f32 with no helper.
    """

    points: tuple[RandomPoint, ...]

    CODE = 0x0403
    NAME = "Device Read Random"
    mutates = False
    CITES = _CITES

    def __post_init__(self) -> None:
        object.__setattr__(self, "points", _normalise(self.points, "read_random()"))

    def validate(self, ctx: EncodeContext) -> None:
        validate_points(
            self.points,
            ctx,
            what=self.describe(),
            command=self.CODE,
            capability=Capability.RANDOM_ACCESS,
            allowed=_random_ok,
        )

    def payload_len(self, ctx: EncodeContext) -> int:
        return point_specs_len(self.points, ctx)

    def encode(self, ctx: EncodeContext) -> bytes:
        return point_specs(self.points, ctx)

    def decode(self, payload: bytes, ctx: EncodeContext) -> tuple[RandomValue, ...]:
        return decode_point_values(self.points, payload, ctx, what=self.describe())

    def describe(self) -> str:
        return f"read_random({render_addresses(self.points)})"


# ----------------------------------------------------------------------------------------
# 1402 Device Write Random
# ----------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RandomWrite:
    """One word- or double-word access point and the value to put in it."""

    point: RandomPoint
    value: int | float

    def __post_init__(self) -> None:
        if self.point.kind not in _WRITE_KINDS:
            raise SlmpConfigurationError(
                f"a {self.point.kind!r} point cannot be written by 0x1402 word units. A "
                f"word access point on a bit device writes the whole 16-bit window, so "
                f"write it as kind='u16' with the mask you mean; single bits are "
                f"0x1402 in bit units (WriteRandomBits)."
            )
        if self.point.kind == "f32":
            if not isinstance(self.value, int | float) or isinstance(self.value, bool):
                raise TypeError(
                    f"an f32 point takes a real number, not {type(self.value).__name__}"
                )
            return
        if not isinstance(self.value, int) or isinstance(self.value, bool):
            raise TypeError(
                f"a {self.point.kind} point takes an int, not "
                f"{type(self.value).__name__}"
            )

    def wire_value(self) -> int:
        """The unsigned 16- or 32-bit field this write puts on the wire.

        **The point's own ``kind`` is the declared type and is enforced here.** This
        method used to call the 16/32-bit helper with no declared domain at all, so
        ``RandomWrite(word('D100', kind='i16'), 40000).wire_value()`` returned
        ``0x9C40`` -- 40000 masked into a field the caller had just said was signed,
        reading back as ``-25536`` with end code ``0x0000``.
        :meth:`~aslmp.client.Plc.write_random` refused it one layer up, so the masking
        only ever ran for a caller who had built the command directly; a refusal that
        depends on which door you came through is not a refusal.
        """
        bits = self.point.width.bits
        if self.point.kind == "f32":
            return int(struct.unpack("<I", struct.pack("<f", float(self.value)))[0])
        return unsigned(
            int(self.value),
            bits=bits,
            what=f"write_random({self.point})",
            signed_field=self.point.signed_field,
        )


@dataclass(frozen=True, slots=True)
class WriteRandom(_Random[None]):
    """``1402`` subcommand ``0000``: scattered word and double-word writes, one frame.

    Golden vector, measured on FX5U-32MT/DS fw 1.065 (2026-09-06) -- three word points
    and one double-word f32 in one transaction::

        request data : 03 01
                       64 00 00 A8 11 11   65 00 00 A8 22 22   66 00 00 A8 33 33
                       68 00 00 A8 00 50 9A 44
        response     : end code only, L = 0x0002

    Readback confirmed ``D100 = 0x1111``, ``D101 = 0x2222``, ``D102 = 0x3333``,
    ``D104 = 0x5000``, ``D105 = 0x449A`` -- 1234.5, low word first.

    The budget is weighted, not flat: ``word x 12 + dword x 14 <= 1920``. A flat count is
    wrong in both directions -- 160 word points fit and 138 double-word points do not.
    """

    writes: tuple[RandomWrite, ...]

    CODE = 0x1402
    NAME = "Device Write Random"
    mutates = True
    CITES = _CITES

    def __post_init__(self) -> None:
        writes = tuple(self.writes)
        for index, item in enumerate(writes):
            if not isinstance(item, RandomWrite):
                raise TypeError(
                    f"write_random() item {index} is {type(item).__name__}, not a "
                    f"RandomWrite"
                )
        object.__setattr__(self, "writes", writes)

    @property
    def points(self) -> tuple[RandomPoint, ...]:
        """The access points, in caller order."""
        return tuple(item.point for item in self.writes)

    def validate(self, ctx: EncodeContext) -> None:
        validate_points(
            self.points,
            ctx,
            what=self.describe(),
            command=self.CODE,
            capability=Capability.RANDOM_ACCESS,
            allowed=_random_ok,
        )

    def payload_len(self, ctx: EncodeContext) -> int:
        total = 2 * ctx.codec.number_len(8)
        for item in self.writes:
            total += ctx.device_spec_len() + ctx.codec.number_len(item.point.width.bits)
        return total

    def encode(self, ctx: EncodeContext) -> bytes:
        points = self.points
        word_ix, dword_ix = split_points(points)
        out = [
            ctx.codec.number(len(word_ix), bits=8),
            ctx.codec.number(len(dword_ix), bits=8),
        ]
        for index in (*word_ix, *dword_ix):
            item = self.writes[index]
            out.append(ctx.device_spec(ctx.address(item.point.address)))
            out.append(ctx.codec.number(item.wire_value(), bits=item.point.width.bits))
        return b"".join(out)

    def decode(self, payload: bytes, ctx: EncodeContext) -> None:
        del ctx
        expect_empty_payload(payload, what=self.describe())

    def describe(self) -> str:
        return f"write_random({render_addresses(self.points)})"


@dataclass(frozen=True, slots=True)
class BitWrite:
    """One bit device and the state to drive it to, for ``1402`` in bit units."""

    address: AddressLike
    value: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", bool(self.value))


@dataclass(frozen=True, slots=True)
class WriteRandomBits(_Random[None]):
    """``1402`` subcommand ``0001``: set or reset scattered single bits.

    Golden vector, SH(NA)-080956ENG-M p.59 -- turn ``M50`` off and ``Y2F`` on, binary
    (a Q-series example, so ``Y`` is hexadecimal there)::

        request data : 02  32 00 00 90 00  2F 00 00 9D 01

    The set/reset field is **one byte** under the short device specification and two
    under the long one, where ON is the byte pair ``01 00`` -- the manual's "0100H" is
    the on-wire sequence, matching its own ASCII column of ``"0001"`` (ambiguity
    ``A-1402-BIT-SET-VALUE``). Emitting it through ``codec.number(1, bits=...)`` makes
    the two codings agree by construction.
    """

    writes: tuple[BitWrite, ...]

    CODE = 0x1402
    NAME = "Device Write Random"
    mutates = True
    CITES = _CITES
    UNIT: ClassVar[Unit] = Unit.BIT

    def __post_init__(self) -> None:
        writes = tuple(self.writes)
        for index, item in enumerate(writes):
            if not isinstance(item, BitWrite):
                raise TypeError(
                    f"write_random_bits() item {index} is {type(item).__name__}, not a "
                    f"BitWrite"
                )
        object.__setattr__(self, "writes", writes)

    def _value_bits(self, ctx: EncodeContext) -> Literal[8, 16]:
        """The set/reset field width: one byte short spec, two long."""
        return 8 if ctx.spec is SpecFormat.SHORT else 16

    def validate(self, ctx: EncodeContext) -> None:
        what = self.describe()
        ctx.profile.require(Capability.RANDOM_ACCESS, what=what)
        ctx.require_spec(what=what)
        for item in self.writes:
            address = ctx.address(item.address)
            if not address.type.random_ok:
                raise SlmpDeviceNotAllowedHereError(
                    f"{address.type.name} ({address.type.long_name.lower()}) cannot be "
                    f"used as an access point of 0x1402 ({FX5_RANDOM.reference})."
                )
            if address.type.unit is not Unit.BIT:
                raise SlmpDeviceNotAllowedHereError(
                    f"{address} is a word device, so it has no single bit to set. "
                    f"0x1402 in bit units addresses bit devices only "
                    f"({RANDOM_LAYOUT.reference})."
                )
            ctx.check_span(address, 1, width=Unit.BIT)
        if len(self.writes) > MAX_POINT_COUNT_FIELD:
            raise SlmpPointLimitError(
                f"{what} has {len(self.writes)} bit access points and the count field is "
                f"one byte, so {MAX_POINT_COUNT_FIELD} is the most that can be expressed."
            )
        key = (self.CODE, ctx.encoding, Unit.BIT, ctx.link)
        ctx.profile.check_points(key, bit=len(self.writes))

    def payload_len(self, ctx: EncodeContext) -> int:
        per = ctx.device_spec_len() + ctx.codec.number_len(self._value_bits(ctx))
        return ctx.codec.number_len(8) + len(self.writes) * per

    def encode(self, ctx: EncodeContext) -> bytes:
        bits = self._value_bits(ctx)
        out = [ctx.codec.number(len(self.writes), bits=8)]
        for item in self.writes:
            out.append(ctx.device_spec(ctx.address(item.address)))
            out.append(ctx.codec.number(1 if item.value else 0, bits=bits))
        return b"".join(out)

    def decode(self, payload: bytes, ctx: EncodeContext) -> None:
        del ctx
        expect_empty_payload(payload, what=self.describe())

    def describe(self) -> str:
        return (
            "write_random_bits("
            + render_addresses([item.address for item in self.writes])
            + ")"
        )
