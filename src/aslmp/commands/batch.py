"""``0401`` Device Read (Batch) and ``1401`` Device Write (Batch).

Layer 2. One head device, one point count, one contiguous run. The cheapest way to move
a lot of registers and the wrong way to sample three separate ones: three ``0401`` round
trips cost 21.8 ms at p50 against 7.2 ms for one ``0403`` carrying the same three values,
and worse, they sample the plant at three moments up to 27 ms apart (FX5U-32MT/DS fw
1.065, n=200 each, interleaved, 2026-09-06). Batch is for runs; ``commands/random.py``
is for the control loop.

**The point ceilings are per-CPU, per-coding, per-link and measured, not generic.** An
FX5U-32MT/DS on firmware 1.065 was binary-searched to 960 word points (961 ->
``0xC052``) and **3584** bit points (3585 -> ``0xC051``), against SH(NA)-080956ENG-M's
generic bit figure of 7168 -- exactly double. A client that hard-codes the reference
manual's numbers builds frames this CPU rejects, so the budget comes from
``profile.check_points`` and the exception quotes the end code the CPU would have
answered.

**Zero points is a point-count error, not an address error.** ``00 00 00 A8 00 00`` --
zero word points from ``D0`` -- returned ``0xC052`` on that CPU (measured 2026-09-06),
which is what :meth:`~aslmp.profile.CpuProfile.check_points` quotes when it refuses.

**Response shapes.** A word read returns two bytes per point in binary and four
characters per point in ASCII. A bit read returns one *nibble* per point in binary, the
first point in the high nibble, an odd count padded low -- and one character per point
in ASCII. So an ASCII bit read is ``2 x binary - (count % 2)`` units long and never
twice it; the arithmetic is ``codec.bit_data_len`` and is never a doubling
(SH(NA)-080956ENG-M section 6.2 p.40).
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import ClassVar, TypeVar

from aslmp.commands.base import (
    AddressLike,
    Command,
    EncodeContext,
    expect_empty_payload,
    expect_payload_len,
    unsigned,
)
from aslmp.errors import SlmpConfigurationError, SlmpDeviceNotAllowedHereError
from aslmp.profile import Capability
from aslmp.wire.address import DeviceAddress
from aslmp.wire.citations import Citation, Measurement, Source
from aslmp.wire.codec import Unit

__all__ = [
    "BATCH_BIT_CEILING",
    "BATCH_LAYOUT",
    "BATCH_WORD_CEILING",
    "FX5_BATCH",
    "ReadBits",
    "ReadWords",
    "WriteBits",
    "WriteWords",
]


BATCH_LAYOUT: Citation = Citation(
    manual="SH(NA)-080956ENG",
    revision="M",
    section="6.1 pp.45-52",
    note=(
        "Device Read (Batch) 0401H and Device Write (Batch) 1401H. Binary request: "
        "command, subcommand, head device number (3 bytes low byte first), device code "
        "(1 byte), number of points (2 bytes low byte first). ASCII request: command, "
        "subcommand, device code (2 characters), head device number (6 characters), "
        "number of points (4 characters) -- the device number and the device code swap "
        "order between the codings. Response: the read data alone, with no header of "
        "its own; a write has no response data."
    ),
)
"""The generic layout both directions implement."""

FX5_BATCH: Citation = Citation(
    manual="JY997D56001",
    revision="K",
    section="4.3 pp.68-75",
    note=(
        "The FX5's own copy, with the worked ASCII write '1401' '0000' 'D*' '000100' "
        "'0003' on p.75 and the FX5 point ceilings on p.69: 1-960 word points and "
        "1-3584 bit points in binary, 1-480 and 1-1792 in ASCII. Batch Read (0401H) is "
        "not applicable to double word devices or to the long index register LZ."
    ),
)
"""The FX5-specific restatement, and the source of the halved ASCII budgets."""

BATCH_WORD_CEILING: Measurement = Measurement(
    cpu="FX5U-32MT/DS",
    firmware="1.065",
    date="2026-09-06",
    note=(
        "Binary-searched from D0: 960 word points returned 0x0000 and 961 returned "
        "0xC052. 960 words is 1920 bytes of response data and 1931 bytes on the wire, "
        "which is also where the 1460-byte TCP segmentation was observed."
    ),
)
"""The measured word ceiling, which happens to agree with both manuals."""

BATCH_BIT_CEILING: Measurement = Measurement(
    cpu="FX5U-32MT/DS",
    firmware="1.065",
    date="2026-09-06",
    note=(
        "Binary-searched from M0: 3584 bit points returned 0x0000 and 3585 returned "
        "0xC051. SH(NA)-080956ENG-M's generic figure is 7168, exactly double, and a "
        "client that ships it builds frames this CPU rejects. See ambiguity "
        "A-BATCH-BIT-LIMIT."
    ),
)
"""The measured bit ceiling, which contradicts the generic reference by a factor of two."""

_CITES: tuple[Source, ...] = (
    BATCH_LAYOUT,
    FX5_BATCH,
    BATCH_WORD_CEILING,
    BATCH_BIT_CEILING,
)

R_co = TypeVar("R_co", covariant=True)


def _check_count(count: int, what: str) -> None:
    """A point count is a non-negative int. Zero is refused by ``validate``, with the
    ``0xC052`` this hardware answers quoted from the profile's evidence."""
    if not isinstance(count, int) or isinstance(count, bool):
        raise TypeError(f"{what} must be an int, not {type(count).__name__}")
    if count < 0:
        raise SlmpConfigurationError(
            f"{what} cannot be negative; got {count}. Nothing here clamps to zero, and "
            f"zero itself is refused by validate() quoting the 0xC052 an FX5U-32MT/DS "
            f"fw 1.065 returned for a zero point count (measured 2026-09-06)."
        )


class _Batch(Command[R_co], abstract=True):
    """Shared validation and field emission for ``0401`` / ``1401``.

    The device specification block and the point count are the whole request in both
    directions; a write appends its data and nothing else changes.
    """

    __slots__ = ()

    UNIT: ClassVar[Unit]

    address: AddressLike

    def subcommand(self, ctx: EncodeContext) -> int:
        return ctx.subcommand(self.UNIT)

    @abc.abstractmethod
    def points(self) -> int:
        """How many points this request carries."""

    def validate(self, ctx: EncodeContext) -> None:
        what = self.describe()
        ctx.profile.require(Capability.BATCH_ACCESS, what=what)
        ctx.require_spec(what=what)
        address = ctx.address(self.address)
        if not address.type.batch_ok:
            raise SlmpDeviceNotAllowedHereError(
                f"{address.type.name} ({address.type.long_name.lower()}) cannot be used "
                f"with 0x{self.CODE:04X} {self.NAME}. {FX5_BATCH.reference}: 'Batch Read "
                f"(0401H) is not applicable to double word devices or long index "
                f"registers (LZ)', and SH(NA)-080956ENG-M p.46 excludes the long timer "
                f"and long retentive timer contacts and coils. 0x0403 Device Read Random "
                f"does reach the long current values."
            )
        count = self.points()
        ctx.check_span(address, count, width=self.UNIT)
        key = (self.CODE, ctx.encoding, self.UNIT, ctx.link)
        if self.UNIT is Unit.BIT:
            ctx.profile.check_points(key, bit=count)
        else:
            ctx.profile.check_points(key, word=count)

    def _head(self, ctx: EncodeContext) -> bytes:
        address: DeviceAddress = ctx.address(self.address)
        return ctx.device_spec(address) + ctx.codec.number(self.points(), bits=16)

    def _head_len(self, ctx: EncodeContext) -> int:
        return ctx.device_spec_len() + ctx.codec.number_len(16)


# ----------------------------------------------------------------------------------------
# 0401 Device Read (Batch)
# ----------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReadWords(_Batch[tuple[int, ...]]):
    """``0401`` subcommand ``0000``: ``count`` consecutive words from ``address``.

    Golden vector, SH(NA)-080956ENG-M p.49 -- ``T100`` for 3 points, binary::

        request data : 64 00 00 C2 03 00
        response data: 34 12 02 00 EF 1D      T100=0x1234 T101=0x0002 T102=0x1DEF

    On a bit device one point is 16 consecutive bits with the named device as the least
    significant bit, so ``ReadWords("M100", 1)`` returns one word holding M100..M115.
    """

    address: AddressLike
    count: int

    CODE = 0x0401
    NAME = "Device Read (Batch)"
    mutates = False
    CITES = _CITES
    UNIT: ClassVar[Unit] = Unit.WORD

    def __post_init__(self) -> None:
        _check_count(self.count, "read_words(count=...)")

    def points(self) -> int:
        return self.count

    def payload_len(self, ctx: EncodeContext) -> int:
        return self._head_len(ctx)

    def encode(self, ctx: EncodeContext) -> bytes:
        return self._head(ctx)

    def decode(self, payload: bytes, ctx: EncodeContext) -> tuple[int, ...]:
        expect_payload_len(
            payload, ctx.codec.word_data_len(self.count), what=self.describe()
        )
        return ctx.codec.read_words(payload, 0, self.count)

    def describe(self) -> str:
        return f"read_words({str(self.address)!r}, {self.count})"


@dataclass(frozen=True, slots=True)
class ReadBits(_Batch[tuple[bool, ...]]):
    """``0401`` subcommand ``0001``: ``count`` consecutive bit points from ``address``.

    Golden vector, SH(NA)-080956ENG-M p.47 -- ``M100`` for 8 points, binary::

        request data : 64 00 00 90 08 00
        response data: 00 01 00 11           nibbles 0,0 / 0,1 / 0,0 / 1,1

    Bit units address bit devices only. ``ReadBits("D0", 16)`` is refused here, not by
    the PLC: read the word and select the bit from the value.
    """

    address: AddressLike
    count: int

    CODE = 0x0401
    NAME = "Device Read (Batch)"
    mutates = False
    CITES = _CITES
    UNIT: ClassVar[Unit] = Unit.BIT

    def __post_init__(self) -> None:
        _check_count(self.count, "read_bits(count=...)")

    def points(self) -> int:
        return self.count

    def payload_len(self, ctx: EncodeContext) -> int:
        return self._head_len(ctx)

    def encode(self, ctx: EncodeContext) -> bytes:
        return self._head(ctx)

    def decode(self, payload: bytes, ctx: EncodeContext) -> tuple[bool, ...]:
        expect_payload_len(
            payload, ctx.codec.bit_data_len(self.count), what=self.describe()
        )
        return ctx.codec.read_bits(payload, 0, self.count)

    def describe(self) -> str:
        return f"read_bits({str(self.address)!r}, {self.count})"


# ----------------------------------------------------------------------------------------
# 1401 Device Write (Batch)
# ----------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WriteWords(_Batch[None]):
    """``1401`` subcommand ``0000``: write ``values`` from ``address`` onwards.

    Golden vector, measured on FX5U-32MT/DS fw 1.065 (2026-09-06) -- the restore half of
    the live setpoint round trip, writing ``00 00 70 42`` (60.0 as an f32, low word
    first) to ``D0``::

        request data : 00 00 00 A8 02 00 00 00 70 42
        response     : end code only, L = 0x0002, no response data

    There is no response data at all: the frame that came back was the 11-byte minimum
    ``D0 00 00 FF FF 03 00 02 00 00 00``. A parser that expects data breaks here.
    """

    address: AddressLike
    values: tuple[int, ...]

    CODE = 0x1401
    NAME = "Device Write (Batch)"
    mutates = True
    CITES = _CITES
    UNIT: ClassVar[Unit] = Unit.WORD

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", tuple(self.values))
        for index, value in enumerate(self.values):
            unsigned(value, bits=16, what=f"write_words() value {index}")

    def points(self) -> int:
        return len(self.values)

    def payload_len(self, ctx: EncodeContext) -> int:
        return self._head_len(ctx) + ctx.codec.word_data_len(len(self.values))

    def encode(self, ctx: EncodeContext) -> bytes:
        wire = [
            unsigned(value, bits=16, what=f"write_words() value {index}")
            for index, value in enumerate(self.values)
        ]
        return self._head(ctx) + ctx.codec.words(wire)

    def decode(self, payload: bytes, ctx: EncodeContext) -> None:
        del ctx
        expect_empty_payload(payload, what=self.describe())

    def describe(self) -> str:
        return f"write_words({str(self.address)!r}, {len(self.values)} value(s))"


@dataclass(frozen=True, slots=True)
class WriteBits(_Batch[None]):
    """``1401`` subcommand ``0001``: write ``values`` as bit points from ``address``.

    The odd-count case is the one that breaks the "ASCII is twice binary" rule: five bit
    points are five characters in ASCII and three bytes in binary, the last low nibble
    being documented padding (SH(NA)-080956ENG-M p.40). Computing the ASCII length as
    twice the binary one overstates ``L`` by one, and an overstated ``L`` gets no
    response at all on this hardware.
    """

    address: AddressLike
    values: tuple[bool, ...]

    CODE = 0x1401
    NAME = "Device Write (Batch)"
    mutates = True
    CITES = _CITES
    UNIT: ClassVar[Unit] = Unit.BIT

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", tuple(bool(value) for value in self.values))

    def points(self) -> int:
        return len(self.values)

    def payload_len(self, ctx: EncodeContext) -> int:
        return self._head_len(ctx) + ctx.codec.bit_data_len(len(self.values))

    def encode(self, ctx: EncodeContext) -> bytes:
        return self._head(ctx) + ctx.codec.bits(self.values)

    def decode(self, payload: bytes, ctx: EncodeContext) -> None:
        del ctx
        expect_empty_payload(payload, what=self.describe())

    def describe(self) -> str:
        return f"write_bits({str(self.address)!r}, {len(self.values)} value(s))"
