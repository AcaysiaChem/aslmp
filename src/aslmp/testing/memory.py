"""Device memory: the words and bits a simulated CPU actually holds.

Layer 2.5 (``aslmp.testing``). May import ``aslmp.wire``, ``aslmp.errors``,
``aslmp.profile`` and ``aslmp.commands`` -- nothing above them. A simulator built on the
client's transport would make a transport bug invisible to every client-against-server
test in the suite (DESIGN section 1.15).

**Storage is per family, in that family's own unit.** A bit device is a ``bytearray`` of
one byte per point; a word device is a ``list[int]`` of one 16-bit value per word. The
distinction is not cosmetic: SH(NA)-080956ENG-M p.54 says a *word* access point on a bit
device is 16 consecutive bits with the named device as the least significant bit, and a
model that stored M100 as a word would answer a ``0403`` word point at M100 with one
register instead of the window M100-M115.

**Nothing here clamps, wraps or zero-fills.** A read that leaves the allocated span
raises :class:`OutOfRangeError` and the dispatcher turns it into the end code its target
declares -- ``0xC056`` on both the manual and our silicon. That is the whole point of a
conformance simulator: the failure has to be reachable, and reachable at the exact
boundary the hardware put it at. An FX5U-32MT/DS on firmware 1.065 answered ``D7999``
with one point ``0x0000`` and ``D7999`` with **two** points ``0xC056`` (measured
2026-09-06), so the span is checked, never the start address.

Values are unsigned 16-bit throughout. Signedness, ``f32`` and word order are the
*caller's* interpretation of a register pair; the CPU holds words. The convenience
accessors :meth:`DeviceMemory.set_f32` and :meth:`DeviceMemory.get_f32` write the low
word first because that is what an FX5U-32MT/DS does -- writing 1234.5 to one ``1402``
double-word point put ``00 50 9A 44`` on the wire and read back ``D104 = 0x5000``,
``D105 = 0x449A`` (measured 2026-09-06, proved four ways).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from aslmp.wire.codec import Unit
from aslmp.wire.devicetable import DEVICE_TABLE, DeviceType

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterable, Iterator, Mapping, Sequence

    from aslmp.profile import CpuProfile

__all__ = [
    "BENCH_SCAN_STEP",
    "BENCH_SCAN_WRAP",
    "AbsentDeviceError",
    "DeviceMemory",
    "MemoryRange",
    "MemorySnapshot",
    "OutOfRangeError",
    "SimulatorMemoryError",
    "ranges_from_profile",
]


WORD_MASK: Final = 0xFFFF
"""Every device word is 16 bits. A value outside it is a bug in the dispatcher, not a
datum to be masked into range."""

BENCH_SCAN_STEP: Final = 1.0
"""What the bench CPU adds to ``IO_Scan`` each scan: its ST is ``IO_Scan := IO_Scan + 1.0``.

A ``float`` and not an ``int``, because ``IO_Scan`` is declared ``REAL`` in that program
(FX5U-32MT/DS fw 1.065 at 192.168.10.250, read out of GX Works3 2026-09-07)."""

BENCH_SCAN_WRAP: Final = 1.0e7
"""Where the bench CPU's own ST resets ``IO_Scan``: ``IF IO_Scan > 1.0E7``.

Not a protocol constant and not a manual figure -- it is one line of the program running
on that CPU, and it is here so the simulator's counter has the same range the silicon's
does. Every value below it is an *exact* single: an IEEE-754 ``f32``'s ulp is 1.0 across
``[2**23, 2**24)`` and finer below, and ``1.0e7 < 2**24``, so adding
:data:`BENCH_SCAN_STEP` never loses a count anywhere in this register's range."""


class SimulatorMemoryError(Exception):
    """Base class for the two ways a device access can be refused.

    Not the builtin ``MemoryError``, which is about the host's RAM: shadowing that inside
    a package other people import into their own test suites is the kind of small
    rudeness that costs somebody an afternoon.
    """


class AbsentDeviceError(SimulatorMemoryError):
    """The simulated CPU has no such device family at all.

    ``V``, ``ZR``, ``DX`` and ``DY`` on an iQ-F: all four returned ``0xC05C`` on an
    FX5U-32MT/DS fw 1.065 (measured 2026-09-06), **not** the ``0xC05B`` the generic
    documentation predicted. Which end code a target answers with is the target's
    business (:mod:`aslmp.testing.targets`); this class only says which of the two
    refusals happened.
    """


class OutOfRangeError(SimulatorMemoryError):
    """The family exists and the requested span leaves its allocated points."""


@dataclass(frozen=True, slots=True)
class MemoryRange:
    """One device family's allocation in a simulated CPU.

    ``first`` and ``last`` are **index** values -- the integer that goes on the wire --
    and never the digits GX Works3 prints. An iQ-F ``X`` range is ``X0`` to ``X1777`` in
    GX Works3 and 0 to 1023 here, because the wire number is the linear index of the
    octal literal (measured on FX5U-32MT/DS fw 1.065, 2026-09-07: a bit written at wire
    number 16 lit the 17th output, which GX Works3 calls ``Y20``).
    """

    device: str
    first: int
    last: int

    def __post_init__(self) -> None:
        if self.device not in DEVICE_TABLE:
            raise ValueError(
                f"MemoryRange.device {self.device!r} is not a family in the generic "
                f"device table; known families are {sorted(DEVICE_TABLE)}"
            )
        if self.first < 0:
            raise ValueError(f"MemoryRange {self.device!r} starts at {self.first}")
        if self.last < self.first:
            raise ValueError(
                f"MemoryRange {self.device!r} ends at {self.last}, before its first "
                f"device number {self.first}"
            )

    @property
    def type(self) -> DeviceType:
        """The generic device-table row this allocation refines."""
        return DEVICE_TABLE[self.device]

    @property
    def points(self) -> int:
        """How many device points are allocated."""
        return self.last - self.first + 1

    @property
    def words(self) -> int:
        """How many 16-bit words back this family. One point of ``LZ`` is two words."""
        return self.points * self.type.words_per_point

    def __str__(self) -> str:
        return f"{self.device}{self.first} to {self.device}{self.last}"


@dataclass(frozen=True, slots=True)
class MemorySnapshot:
    """Every word and bit of a :class:`DeviceMemory`, taken at one instant.

    The bench rule for scratch registers is "stability-check, then restore", and this is
    what makes the same discipline available in CI: take one before a write test and
    :meth:`DeviceMemory.restore` it afterwards.
    """

    words: Mapping[str, tuple[int, ...]]
    bits: Mapping[str, bytes]


def ranges_from_profile(profile: CpuProfile) -> tuple[MemoryRange, ...]:
    """The allocations a :class:`~aslmp.profile.CpuProfile` declares, as memory ranges.

    Device *ranges* are ground-truth data generated from ``aslmp/data/ranges_*.tsv``, so
    the simulator reads them rather than restating them: a second hand-typed copy of
    ``D0..D7999`` would drift, and the two halves of a conformance test agreeing because
    one of them was copied from the other is worse than not testing it.

    Everything the simulator does with those numbers -- the point ceilings, the end codes,
    the capability refusals, the notation rule -- is declared independently in
    :mod:`aslmp.testing.targets`, because those are *behaviour* and behaviour is what a
    conformance suite exists to compare.

    Families the profile marks absent, or allocates zero points of, or ships without a
    static range (the module access device ``G``, whose size depends on which
    intelligent function module is mounted) are omitted. An omitted family raises
    :class:`AbsentDeviceError`.
    """
    out: list[MemoryRange] = []
    for name in sorted(DEVICE_TABLE):
        rng = profile.range_for(DEVICE_TABLE[name])
        if not rng.present or rng.first is None or rng.last is None:
            continue
        if not rng.points:
            continue
        out.append(MemoryRange(device=name, first=rng.first, last=rng.last))
    return tuple(out)


class DeviceMemory:
    """The device memory of one simulated CPU.

    Word devices are stored as words, bit devices as bits, and the two are never
    conflated. Reads and writes are span-checked against the allocation and refuse
    rather than truncate.
    """

    __slots__ = ("_bits", "_ranges", "_words")

    def __init__(self, ranges: Iterable[MemoryRange]) -> None:
        self._ranges: dict[str, MemoryRange] = {}
        self._words: dict[str, list[int]] = {}
        self._bits: dict[str, bytearray] = {}
        for rng in ranges:
            if rng.device in self._ranges:
                raise ValueError(f"device family {rng.device!r} was allocated twice")
            self._ranges[rng.device] = rng
            if rng.type.unit is Unit.BIT:
                self._bits[rng.device] = bytearray(rng.points)
            else:
                self._words[rng.device] = [0] * rng.words

    @classmethod
    def for_profile(cls, profile: CpuProfile) -> DeviceMemory:
        """Memory shaped by a profile's device ranges."""
        return cls(ranges_from_profile(profile))

    # -- introspection -------------------------------------------------------------

    @property
    def ranges(self) -> Mapping[str, MemoryRange]:
        """Every allocated family, keyed by device name."""
        return dict(self._ranges)

    def has(self, device: str) -> bool:
        """Whether this CPU has the family ``device`` at all."""
        return device in self._ranges

    def range_of(self, device: str) -> MemoryRange:
        """The allocation for ``device``, or raise :class:`AbsentDeviceError`."""
        found = self._ranges.get(device)
        if found is None:
            raise AbsentDeviceError(
                f"this CPU has no {device} device. On an FX5U-32MT/DS fw 1.065 a "
                f"request naming an absent family came back 0xC05C, not the 0xC05B the "
                f"generic documentation predicts (measured 2026-09-06)."
            )
        return found

    def __iter__(self) -> Iterator[MemoryRange]:
        return iter(self._ranges.values())

    def __len__(self) -> int:
        return len(self._ranges)

    def __repr__(self) -> str:
        return f"DeviceMemory({len(self._ranges)} families)"

    # -- span checking -------------------------------------------------------------

    def check_span(self, device: str, index: int, points: int) -> MemoryRange:
        """Refuse unless ``[index, index + points)`` is inside the allocation.

        Always the span, never the start. ``D7999`` alone is legal on an FX5U and
        ``D7999`` for two points is not: both were measured, and a simulator that
        checked only the head address would let the client's own span validation pass
        untested.
        """
        rng = self.range_of(device)
        if points < 0:
            raise OutOfRangeError(f"{device}{index}: negative point count {points}")
        if points == 0:
            return rng
        if index < rng.first or index + points - 1 > rng.last:
            raise OutOfRangeError(
                f"{device}{index} for {points} point(s) reaches "
                f"{device}{index + points - 1}, and this CPU has {rng}."
            )
        return rng

    # -- word devices --------------------------------------------------------------

    def read_words(self, device: str, index: int, count: int) -> tuple[int, ...]:
        """``count`` consecutive 16-bit words from ``device`` starting at ``index``.

        For a **bit** device this is the word-access reading: each word is 16
        consecutive bits with the lowest-numbered one in bit 0
        (SH(NA)-080956ENG-M p.54).
        """
        rng = self.range_of(device)
        if rng.type.unit is Unit.BIT:
            self.check_span(device, index, count * 16)
            return tuple(self._bit_window(device, index + 16 * i) for i in range(count))
        self._require_single_word(rng)
        self.check_span(device, index, count)
        store = self._words[device]
        start = index - rng.first
        return tuple(store[start : start + count])

    def write_words(self, device: str, index: int, values: Sequence[int]) -> None:
        """Write consecutive words. A bit device takes 16 bits per word."""
        rng = self.range_of(device)
        count = len(values)
        for position, value in enumerate(values):
            if not 0 <= value <= WORD_MASK:
                raise OutOfRangeError(
                    f"{device}{index + position}: {value} is not a 16-bit word value. "
                    f"Nothing here masks it into range."
                )
        if rng.type.unit is Unit.BIT:
            self.check_span(device, index, count * 16)
            for position, value in enumerate(values):
                self._set_bit_window(device, index + 16 * position, value)
            return
        self._require_single_word(rng)
        self.check_span(device, index, count)
        store = self._words[device]
        start = index - rng.first
        store[start : start + count] = list(values)

    @staticmethod
    def _require_single_word(rng: MemoryRange) -> None:
        """Refuse a double-word family, which needs the long device specification.

        ``LZ``, ``LTN``, ``LSTN``, ``LCN`` and ``RD`` are two words per point and are
        reachable only with subcommand ``0002``/``0003``. An FX5U-32MT/DS on firmware
        1.065 answers that subcommand ``0xC059`` (measured 2026-09-06), so on an iQ-F
        these families are unreachable rather than merely differently addressed, and a
        simulator that quietly served them as one word per point would let a client bug
        pass.
        """
        if rng.type.words_per_point == 1:
            return
        raise OutOfRangeError(
            f"{rng.device} ({rng.type.long_name.lower()}) is "
            f"{rng.type.words_per_point} words per point and is reachable only with the "
            f"long device specification, which this simulator serves per its target's "
            f"declared capability rather than through word arithmetic here."
        )

    # -- bit devices ---------------------------------------------------------------

    def read_bits(self, device: str, index: int, count: int) -> tuple[bool, ...]:
        """``count`` consecutive single bits, refusing a word device."""
        rng = self._require_bit(device)
        self.check_span(device, index, count)
        store = self._bits[device]
        start = index - rng.first
        return tuple(bool(store[start + i]) for i in range(count))

    def write_bits(self, device: str, index: int, values: Sequence[bool]) -> None:
        """Drive ``len(values)`` consecutive single bits."""
        rng = self._require_bit(device)
        self.check_span(device, index, len(values))
        store = self._bits[device]
        start = index - rng.first
        for position, value in enumerate(values):
            store[start + position] = 1 if value else 0

    def _require_bit(self, device: str) -> MemoryRange:
        rng = self.range_of(device)
        if rng.type.unit is not Unit.BIT:
            raise OutOfRangeError(
                f"{device} ({rng.type.long_name.lower()}) is a word device, so it has "
                f"no single bits to address in bit units."
            )
        return rng

    def _bit_window(self, device: str, index: int) -> int:
        rng = self._ranges[device]
        store = self._bits[device]
        start = index - rng.first
        value = 0
        for offset in range(16):
            if store[start + offset]:
                value |= 1 << offset
        return value

    def _set_bit_window(self, device: str, index: int, value: int) -> None:
        rng = self._ranges[device]
        store = self._bits[device]
        start = index - rng.first
        for offset in range(16):
            store[start + offset] = (value >> offset) & 1

    # -- convenience for seeding a bench-shaped CPU --------------------------------

    def get_u16(self, device: str, index: int) -> int:
        """One word."""
        return self.read_words(device, index, 1)[0]

    def set_u16(self, device: str, index: int, value: int) -> None:
        """One word."""
        self.write_words(device, index, (value,))

    def get_u32(self, device: str, index: int) -> int:
        """Two consecutive words as one unsigned 32-bit value, **low word first**."""
        low, high = self.read_words(device, index, 2)
        return (high << 16) | low

    def set_u32(self, device: str, index: int, value: int) -> None:
        """Two consecutive words from one unsigned 32-bit value, low word first."""
        if not 0 <= value <= 0xFFFFFFFF:
            raise OutOfRangeError(f"{device}{index}: {value} is not a 32-bit value")
        self.write_words(device, index, (value & WORD_MASK, (value >> 16) & WORD_MASK))

    def get_f32(self, device: str, index: int) -> float:
        """Two consecutive words as one IEEE-754 single, low word first.

        One expression, exactly as the library has: ``struct.unpack("<f",
        struct.pack("<I", v))[0]``.
        """
        return float(struct.unpack("<f", struct.pack("<I", self.get_u32(device, index)))[0])

    def set_f32(self, device: str, index: int, value: float) -> None:
        """Two consecutive words from one IEEE-754 single, low word first."""
        self.set_u32(device, index, int(struct.unpack("<I", struct.pack("<f", value))[0]))

    def bump_u32(self, device: str, index: int, step: int = 1) -> int:
        """Advance an **integer** double-word counter and return its new value.

        For a counter the CPU's own program declares as a ``DWORD``/``DINT``: the value
        wraps at ``0xFFFFFFFF`` because that is what a 32-bit integer does.

        **This is not the bench's D8.** ``IO_Scan`` on the FX5U-32MT/DS at
        192.168.10.250 is a ``REAL``; :meth:`bump_f32` is what models it. Reaching for
        this method because a counter "is really an integer" is precisely how this
        library came to publish a float's bit pattern as a scan count, and how the
        simulator agreed with it in ~4100 tests: the oracle had the same bug as the code
        it was checking. Pick the one the silicon uses, not the one the value looks like.
        """
        value = (self.get_u32(device, index) + step) & 0xFFFFFFFF
        self.set_u32(device, index, value)
        return value

    def bump_f32(
        self,
        device: str,
        index: int,
        step: float = BENCH_SCAN_STEP,
        *,
        wrap_above: float | None = BENCH_SCAN_WRAP,
        wrap_to: float = 0.0,
    ) -> float:
        """Advance a **floating-point** free-running counter and return its new value.

        This is the bench CPU's ``D8``/``D9``. ``IO_Scan`` there is a ``REAL`` and the
        program's own two lines are ``IO_Scan := IO_Scan + 1.0`` and
        ``IF IO_Scan > 1.0E7`` (FX5U-32MT/DS fw 1.065 at 192.168.10.250, 2026-09-07),
        which is exactly :data:`BENCH_SCAN_STEP` and :data:`BENCH_SCAN_WRAP`.

        Modelling that register as an integer double word is a test that cannot fail: a
        client decoding ``D8`` as ``u32`` reads the float's bit pattern, gets a
        plausible rising number and end code ``0x0000``, and a simulator holding a real
        integer there hands it the number it expected. Holding an ``f32`` here is what
        makes the wrong declaration produce a wrong answer in a test.

        ``wrap_above`` is the ST's own threshold; pass ``None`` for a counter that only
        climbs. ``wrap_to`` is what the simulator resumes from and is a **choice, not a
        measurement** -- 1.0e7 counts at the measured 1018 scans/s (``docs/hardware.md``
        section 17) is about 2.7 hours, so
        the reset has never been seen on a wire. No test may assert the value the counter
        resumes from; assert that it dropped.

        Returns the value as stored, i.e. after the round trip through the register pair,
        so the simulator never reports a count its own memory does not hold.
        """
        value = self.get_f32(device, index) + step
        if wrap_above is not None and value > wrap_above:
            value = wrap_to
        self.set_f32(device, index, value)
        return self.get_f32(device, index)

    # -- snapshot / restore --------------------------------------------------------

    def snapshot(self) -> MemorySnapshot:
        """Every word and bit, copied."""
        return MemorySnapshot(
            words={name: tuple(store) for name, store in self._words.items()},
            bits={name: bytes(store) for name, store in self._bits.items()},
        )

    def restore(self, snapshot: MemorySnapshot) -> None:
        """Put back exactly what :meth:`snapshot` took. Refuses a foreign snapshot."""
        if set(snapshot.words) != set(self._words) or set(snapshot.bits) != set(self._bits):
            raise ValueError(
                "this snapshot was taken from a differently shaped DeviceMemory; "
                "restoring it would leave families untouched and others invented"
            )
        for name, values in snapshot.words.items():
            self._words[name][:] = list(values)
        for name, raw in snapshot.bits.items():
            self._bits[name][:] = raw

    def clear(self) -> None:
        """Zero every word and bit."""
        for store in self._words.values():
            store[:] = [0] * len(store)
        for bits in self._bits.values():
            bits[:] = bytearray(len(bits))
