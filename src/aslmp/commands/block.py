"""``0406`` Device Read Block and ``1406`` Device Write Block.

Layer 2. Several contiguous runs in one request: the middle ground between ``0401``
(one run) and ``0403`` (scattered single points). Word device blocks come first, then bit
device blocks, and **for a bit device block one point is 16 bits** -- so a bit block of 2
points is 32 consecutive relays and comes back as two words
(SH(NA)-080956ENG-M section 6.5 pp.69-76).

Golden vector, SH(NA)-080956ENG-M p.72 -- two word blocks (``D0`` 4 points, ``W100`` 8
points) and three bit blocks (``M0`` 2, ``M128`` 2, ``B100`` 3), binary::

    request data : 02 03
                   00 00 00 A8 04 00
                   00 01 00 B4 08 00
                   00 00 00 90 02 00
                   80 00 00 90 02 00
                   00 01 00 A0 03 00

The response is the first word block's data, then the second's, then each bit block's,
each block being its own point count of 16-bit words. There is no per-block header and
no count echo, so -- exactly as with ``0403`` -- the request is the only thing that can
parse the response.

**Nothing in this module has ever been exercised on hardware.** The bench never sent a
``0406`` or a ``1406``. The block budgets shipped by the iQ-F profiles are
``Provenance.MANUAL`` for the read direction and ``Provenance.INFERRED`` for ``1406``'s
per-block overhead of 4 and its 760-point total, and the refusal quotes that provenance
rather than implying a measurement.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import TypeVar

from aslmp.commands.base import (
    AddressLike,
    Command,
    EncodeContext,
    expect_empty_payload,
    expect_payload_len,
    render_addresses,
    unsigned,
)
from aslmp.errors import SlmpConfigurationError, SlmpDeviceNotAllowedHereError
from aslmp.profile import Capability
from aslmp.wire.address import DeviceAddress
from aslmp.wire.citations import Citation, Source
from aslmp.wire.codec import Unit

__all__ = [
    "BLOCK_LAYOUT",
    "FX5_BLOCK",
    "BlockSpec",
    "BlockWrite",
    "ReadBlocks",
    "WriteBlocks",
]


BLOCK_LAYOUT: Citation = Citation(
    manual="SH(NA)-080956ENG",
    revision="M",
    section="6.5 pp.69-76",
    note=(
        "Device Read Block 0406H and Device Write Block 1406H. Binary request: command, "
        "subcommand, number of word device blocks (1 byte), number of bit device blocks "
        "(1 byte), then per block the head device number (3 bytes), the device code (1 "
        "byte) and the number of points (2 bytes low byte first); word blocks first, "
        "then bit blocks. One point of a BIT device block is 16 bits. Response: each "
        "block's data in request order, each block being its own point count of words. "
        "1406 appends the write data after each block's point count and has no response "
        "data. Limits: word blocks + bit blocks <= 120 and total points <= 960 for "
        "subcommand 0000. Timer and counter contacts and coils must go in a bit device "
        "block."
    ),
)
"""The layout, the ordering rule and the 16-bits-per-bit-block-point rule."""

FX5_BLOCK: Citation = Citation(
    manual="JY997D56001",
    revision="K",
    section="4.3 pp.89-96",
    note=(
        "The FX5's copy. The 1406 total is 760 points rather than the generic 960, and "
        "770 through an Ethernet module -- another target-dependent limit. Never "
        "exercised on our bench: no 0406 or 1406 was ever sent to the FX5U."
    ),
)
"""The FX5 restatement and the reduced write total."""

_CITES: tuple[Source, ...] = (BLOCK_LAYOUT, FX5_BLOCK)

_LONG_COUNTER_EXCLUSIONS: frozenset[str] = frozenset({"LCS", "LCC", "LCN"})
"""Refused here even though ``DeviceType.block_ok`` allows them.

SH(NA)-080956ENG-M p.70 lists LTS/LTC/LTN, LSTS/LSTC/LSTN, **LCS/LCC/LCN** and LZ as
devices that cannot be specified for block access. ``data/devices.tsv`` carries
``block_ok = yes`` for the three long-counter families, which contradicts that page; the
discrepancy is reported against build unit U1 and refused in the safe direction here
until the table is corrected. Nothing is silently allowed on the strength of a column
that disagrees with the manual it was transcribed from.
"""


@dataclass(frozen=True, slots=True)
class BlockSpec:
    """One contiguous run inside a ``0406`` request.

    Whether it is a *word device block* or a *bit device block* is decided by the
    device, not by the caller: ``D0`` is always a word block and ``M0`` is always a bit
    block. The distinction changes the wire order (word blocks first) and what a point
    means (one register, or sixteen relays).
    """

    address: AddressLike
    points: int

    def __post_init__(self) -> None:
        if not isinstance(self.points, int) or isinstance(self.points, bool):
            raise TypeError(f"BlockSpec.points must be an int, not {self.points!r}")
        if self.points < 0:
            raise SlmpConfigurationError(
                f"BlockSpec({self.address!r}) cannot have {self.points} points; nothing "
                f"here clamps a negative count to zero"
            )

    def __str__(self) -> str:
        return f"{self.address}x{self.points}"


@dataclass(frozen=True, slots=True)
class BlockWrite:
    """One contiguous run and the words to write into it, for ``1406``."""

    address: AddressLike
    values: tuple[int, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", tuple(self.values))
        for index, value in enumerate(self.values):
            # signed_field=None: a block is raw registers, so -1 and 65535 are the same
            # word. unsigned() has no default for this; see its docstring.
            unsigned(
                value,
                bits=16,
                what=f"write_blocks({self.address!r}) word {index}",
                signed_field=None,
            )

    @property
    def points(self) -> int:
        """One point per word, including on a bit device block where it is 16 relays."""
        return len(self.values)

    def __str__(self) -> str:
        return f"{self.address}x{len(self.values)}"


R_co = TypeVar("R_co", covariant=True)


def _is_bit_block(address: DeviceAddress) -> bool:
    return address.type.unit is Unit.BIT


class _Block(Command[R_co], abstract=True):
    """Shared ordering, validation and header emission for ``0406`` / ``1406``."""

    __slots__ = ()

    def subcommand(self, ctx: EncodeContext) -> int:
        return ctx.subcommand(Unit.WORD)

    @abc.abstractmethod
    def _blocks(self) -> tuple[tuple[AddressLike, int], ...]:
        """Every block as ``(address, points)`` in caller order."""

    def _order(self, ctx: EncodeContext) -> tuple[tuple[int, ...], tuple[int, ...]]:
        """Caller indices of the word device blocks and of the bit device blocks.

        Stable inside each group: the response arrives in exactly this order and the
        caller gets it back in theirs.
        """
        words: list[int] = []
        bits: list[int] = []
        for index, (address, _points) in enumerate(self._blocks()):
            target = bits if _is_bit_block(ctx.address(address)) else words
            target.append(index)
        return tuple(words), tuple(bits)

    def _validate_blocks(self, ctx: EncodeContext) -> None:
        what = self.describe()
        ctx.profile.require(Capability.BLOCK_ACCESS, what=what)
        ctx.require_spec(what=what)
        blocks = self._blocks()
        total = 0
        for address_like, points in blocks:
            address = ctx.address(address_like)
            if not address.type.block_ok or address.type.name in _LONG_COUNTER_EXCLUSIONS:
                raise SlmpDeviceNotAllowedHereError(
                    f"{address.type.name} ({address.type.long_name.lower()}) cannot be "
                    f"specified for block access. {BLOCK_LAYOUT.reference} excludes the "
                    f"long timer, long retentive timer, long counter and long index "
                    f"register families from 0x0406 and 0x1406."
                )
            ctx.check_span(address, points, width=Unit.WORD)
            total += points
        word_ix, bit_ix = self._order(ctx)
        count = len(word_ix) + len(bit_ix)
        if len(word_ix) > 0xFF or len(bit_ix) > 0xFF:
            raise SlmpConfigurationError(
                f"{what} has {len(word_ix)} word device blocks and {len(bit_ix)} bit "
                f"device blocks; each count is a one-byte field, so 255 is the most that "
                f"can be expressed. The real ceiling is lower and the profile refuses it "
                f"first."
            )
        key = (self.CODE, ctx.encoding, Unit.WORD, ctx.link)
        ctx.profile.check_points(key, word=total, blocks=count)

    def _header(self, ctx: EncodeContext) -> bytes:
        word_ix, bit_ix = self._order(ctx)
        return ctx.codec.number(len(word_ix), bits=8) + ctx.codec.number(
            len(bit_ix), bits=8
        )

    def _header_len(self, ctx: EncodeContext) -> int:
        return 2 * ctx.codec.number_len(8)

    def _block_head(
        self, ctx: EncodeContext, address_like: AddressLike, points: int
    ) -> bytes:
        return ctx.device_spec(ctx.address(address_like)) + ctx.codec.number(
            points, bits=16
        )

    def _block_head_len(self, ctx: EncodeContext) -> int:
        return ctx.device_spec_len() + ctx.codec.number_len(16)


@dataclass(frozen=True, slots=True)
class ReadBlocks(_Block[tuple[tuple[int, ...], ...]]):
    """``0406``: several contiguous runs read in one round trip, results per block."""

    blocks: tuple[BlockSpec, ...]

    CODE = 0x0406
    NAME = "Device Read Block"
    mutates = False
    CITES = _CITES

    def __post_init__(self) -> None:
        blocks = tuple(self.blocks)
        for index, block in enumerate(blocks):
            if not isinstance(block, BlockSpec):
                raise TypeError(
                    f"read_blocks() block {index} is {type(block).__name__}, not a "
                    f"BlockSpec"
                )
        object.__setattr__(self, "blocks", blocks)

    def _blocks(self) -> tuple[tuple[AddressLike, int], ...]:
        return tuple((block.address, block.points) for block in self.blocks)

    def validate(self, ctx: EncodeContext) -> None:
        self._validate_blocks(ctx)

    def payload_len(self, ctx: EncodeContext) -> int:
        return self._header_len(ctx) + len(self.blocks) * self._block_head_len(ctx)

    def encode(self, ctx: EncodeContext) -> bytes:
        word_ix, bit_ix = self._order(ctx)
        out = [self._header(ctx)]
        for index in (*word_ix, *bit_ix):
            block = self.blocks[index]
            out.append(self._block_head(ctx, block.address, block.points))
        return b"".join(out)

    def decode(
        self, payload: bytes, ctx: EncodeContext
    ) -> tuple[tuple[int, ...], ...]:
        word_ix, bit_ix = self._order(ctx)
        total = sum(block.points for block in self.blocks)
        expect_payload_len(payload, ctx.codec.word_data_len(total), what=self.describe())
        out: list[tuple[int, ...]] = [() for _ in self.blocks]
        offset = 0
        for index in (*word_ix, *bit_ix):
            points = self.blocks[index].points
            out[index] = ctx.codec.read_words(payload, offset, points)
            offset += ctx.codec.word_data_len(points)
        return tuple(out)

    def describe(self) -> str:
        return f"read_blocks({render_addresses(self.blocks)})"


@dataclass(frozen=True, slots=True)
class WriteBlocks(_Block[None]):
    """``1406``: several contiguous runs written in one round trip. No response data.

    The budget charges four words of overhead per block on top of the points
    (``BlockRule(120, 4, 760)`` on the iQ-F profiles), and that overhead figure is
    ``Provenance.INFERRED``: it was carried from the locked design and not located in the
    manual. The refusal says so.
    """

    blocks: tuple[BlockWrite, ...]

    CODE = 0x1406
    NAME = "Device Write Block"
    mutates = True
    CITES = _CITES

    def __post_init__(self) -> None:
        blocks = tuple(self.blocks)
        for index, block in enumerate(blocks):
            if not isinstance(block, BlockWrite):
                raise TypeError(
                    f"write_blocks() block {index} is {type(block).__name__}, not a "
                    f"BlockWrite"
                )
        object.__setattr__(self, "blocks", blocks)

    def _blocks(self) -> tuple[tuple[AddressLike, int], ...]:
        return tuple((block.address, block.points) for block in self.blocks)

    def validate(self, ctx: EncodeContext) -> None:
        self._validate_blocks(ctx)

    def payload_len(self, ctx: EncodeContext) -> int:
        total = self._header_len(ctx)
        for block in self.blocks:
            total += self._block_head_len(ctx) + ctx.codec.word_data_len(block.points)
        return total

    def encode(self, ctx: EncodeContext) -> bytes:
        word_ix, bit_ix = self._order(ctx)
        out = [self._header(ctx)]
        for index in (*word_ix, *bit_ix):
            block = self.blocks[index]
            out.append(self._block_head(ctx, block.address, block.points))
            out.append(ctx.codec.words(block.values))
        return b"".join(out)

    def decode(self, payload: bytes, ctx: EncodeContext) -> None:
        del ctx
        expect_empty_payload(payload, what=self.describe())

    def describe(self) -> str:
        return f"write_blocks({render_addresses(self.blocks)})"
