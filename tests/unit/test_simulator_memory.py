"""``aslmp.testing.memory`` -- the words and bits a simulated CPU holds.

The three things worth testing here are the ones a naive model gets wrong:

* a **word** access point on a bit device is 16 consecutive bits with the named device as
  bit 0, not one register (SH(NA)-080956ENG-M p.54);
* the **span** is validated, never the start address. ``D7999`` alone is legal on an FX5U
  and ``D7999`` for two points is not, and both were measured on FX5U-32MT/DS fw 1.065
  (2026-09-06);
* the bench's scan counter at ``D8``/``D9`` is a ``REAL`` and not a double word. This
  file used to say the opposite, in the one method whose whole job was to model that
  register, and that is why a client reading it as ``u32`` passed ~4100 tests.
"""

from __future__ import annotations

import math
import struct

import pytest

from aslmp.profiles import FX5U, IQ_R
from aslmp.testing.memory import (
    BENCH_SCAN_STEP,
    BENCH_SCAN_WRAP,
    AbsentDeviceError,
    DeviceMemory,
    MemoryRange,
    OutOfRangeError,
    ranges_from_profile,
)


@pytest.fixture
def memory() -> DeviceMemory:
    return DeviceMemory.for_profile(FX5U)


def test_ranges_come_from_the_profile_and_keep_its_span(memory: DeviceMemory) -> None:
    """D0..D7999 on an FX5U, confirmed on hardware and not restated here."""
    assert memory.range_of("D").first == 0
    assert memory.range_of("D").last == 7999
    assert memory.range_of("D").points == 8000


def test_absent_families_are_absent_not_empty() -> None:
    """V, ZR, DX and DY are not on an iQ-F at all; all four returned 0xC05C."""
    memory = DeviceMemory.for_profile(FX5U)
    for device in ("V", "ZR", "DX", "DY"):
        assert not memory.has(device)
        with pytest.raises(AbsentDeviceError):
            memory.read_words(device, 0, 1)


def test_iq_r_zero_point_families_are_not_allocated() -> None:
    """An out-of-the-box iQ-R has no file register, so ZR is present with zero points.

    ``ranges_from_profile`` leaves it out rather than allocating an empty array: "the
    parameter file never allocated it" and "the address is past the end" are different
    diagnoses, and the profile is the thing that knows which.
    """
    allocated = {rng.device for rng in ranges_from_profile(IQ_R)}
    assert "D" in allocated
    assert IQ_R.range_for(IQ_R.devices["ZR"].type).points == 0
    assert "ZR" not in allocated


def test_words_round_trip(memory: DeviceMemory) -> None:
    memory.write_words("D", 100, (0x1111, 0x2222, 0x3333))
    assert memory.read_words("D", 100, 3) == (0x1111, 0x2222, 0x3333)
    assert memory.read_words("D", 101, 1) == (0x2222,)


def test_f32_is_low_word_first(memory: DeviceMemory) -> None:
    """1234.5 written as one double-word point read back D104=0x5000, D105=0x449A.

    Measured on FX5U-32MT/DS fw 1.065 (2026-09-06), proved four ways. A simulator that
    stored it high word first would let a client with the same mistake pass.
    """
    memory.set_f32("D", 104, 1234.5)
    assert memory.read_words("D", 104, 2) == (0x5000, 0x449A)
    assert memory.get_f32("D", 104) == pytest.approx(1234.5)


def test_a_word_point_on_a_bit_device_is_sixteen_bits(memory: DeviceMemory) -> None:
    """M100 read as one word is M100..M115, with M100 in bit 0."""
    memory.write_bits("M", 100, (True, False, False, True))
    memory.write_bits("M", 115, (True,))
    assert memory.read_words("M", 100, 1) == (0b1000_0000_0000_1001,)


def test_writing_a_bit_window_drives_sixteen_relays(memory: DeviceMemory) -> None:
    memory.write_words("M", 100, (0x0003,))
    assert memory.read_bits("M", 100, 4) == (True, True, False, False)


def test_bit_units_refuse_a_word_device(memory: DeviceMemory) -> None:
    with pytest.raises(OutOfRangeError, match="word device"):
        memory.read_bits("D", 0, 1)


def test_the_span_is_validated_not_the_start(memory: DeviceMemory) -> None:
    """D7999 for one point is legal; for two it is not. Both are measured."""
    assert memory.read_words("D", 7999, 1) == (0,)
    with pytest.raises(OutOfRangeError):
        memory.read_words("D", 7999, 2)
    with pytest.raises(OutOfRangeError):
        memory.read_words("D", 8000, 1)


def test_a_bit_window_at_the_range_edge_is_refused(memory: DeviceMemory) -> None:
    """A word point at the last relay needs 16 bits and there are not 16 left."""
    last = memory.range_of("M").last
    with pytest.raises(OutOfRangeError):
        memory.read_words("M", last, 1)


def test_nothing_masks_an_over_wide_word(memory: DeviceMemory) -> None:
    with pytest.raises(OutOfRangeError, match="not a 16-bit word"):
        memory.write_words("D", 0, (0x1FFFF,))


def test_double_word_families_are_refused_rather_than_halved(memory: DeviceMemory) -> None:
    """LZ is two words per point and needs the long device specification.

    An FX5U-32MT/DS answers subcommand 0x0002 with 0xC059 (measured 2026-09-06), so
    serving LZ as one word per point would be inventing a CPU nobody has.
    """
    with pytest.raises(OutOfRangeError, match="long device specification"):
        memory.read_words("LZ", 0, 1)


def test_snapshot_and_restore_put_everything_back(memory: DeviceMemory) -> None:
    """The bench rule for scratch registers, available in CI."""
    memory.write_words("D", 100, (0xAAAA,))
    memory.write_bits("M", 100, (True,))
    before = memory.snapshot()
    memory.write_words("D", 100, (0xBBBB,))
    memory.write_bits("M", 100, (False,))
    memory.restore(before)
    assert memory.read_words("D", 100, 1) == (0xAAAA,)
    assert memory.read_bits("M", 100, 1) == (True,)


def test_a_foreign_snapshot_is_refused(memory: DeviceMemory) -> None:
    other = DeviceMemory((MemoryRange("D", 0, 10),))
    with pytest.raises(ValueError, match="differently shaped"):
        memory.restore(other.snapshot())


def test_the_bench_scan_counter_is_a_real_and_moves_like_one(memory: DeviceMemory) -> None:
    """D8 on the bench is ``IO_Scan``, a ``REAL``. The simulator has to hold one.

    The point is the second assertion. Bumping the register leaves it holding a *float*,
    so the ``u32`` view of it is a bit pattern in the billions rather than a count of 2 --
    which is exactly what a client that declared this register ``u32`` would have read
    off the silicon, with end code ``0x0000`` and nothing to say so.
    """
    first = memory.bump_f32("D", 8)
    second = memory.bump_f32("D", 8)
    assert (first, second) == (1.0, 2.0)
    assert memory.get_f32("D", 8) == 2.0
    assert memory.get_u32("D", 8) == 0x40000000, "the u32 view of 2.0f, not the count 2"


def test_the_bench_scan_counter_wraps_where_the_plc_program_wraps(
    memory: DeviceMemory,
) -> None:
    """``IF IO_Scan > 1.0E7`` is one line of the CPU's own ST, so it is one line here.

    Only the threshold is measured. The value the counter resumes from was never seen on
    a wire -- 1.0e7 counts at 1018 scans/s is about 2.7 hours -- so this asserts that it
    dropped below the threshold and not what it dropped to.
    """
    memory.set_f32("D", 8, BENCH_SCAN_WRAP - BENCH_SCAN_STEP)
    assert memory.bump_f32("D", 8) == BENCH_SCAN_WRAP
    assert memory.bump_f32("D", 8) < BENCH_SCAN_WRAP


def test_every_count_below_the_wrap_is_an_exact_single(memory: DeviceMemory) -> None:
    """The ulp claim the docstrings rest on, checked rather than asserted in prose.

    An ``f32``'s ulp is 1.0 across ``[2**23, 2**24)`` and finer below, and the wrap is
    under ``2**24``, so ``+1.0`` never loses a count and never stalls. It reaches 128 only
    above ``2**30``, which this register never sees.
    """
    for start in (0.0, 2.0**22, 2.0**23, BENCH_SCAN_WRAP - 2.0):
        memory.set_f32("D", 8, start)
        assert memory.bump_f32("D", 8) == start + 1.0
    assert math.ulp(BENCH_SCAN_WRAP) == 2.0 ** (-52 + 23), "the double's ulp, for contrast"
    assert struct.unpack("<f", struct.pack("<f", BENCH_SCAN_WRAP + 1.0))[0] != BENCH_SCAN_WRAP


def test_bump_u32_is_for_a_counter_that_is_really_an_integer(memory: DeviceMemory) -> None:
    """It still exists, and it still means a ``DWORD``/``DINT`` and nothing else.

    Kept because "a genuinely integer counter" is a real thing to want; renamed in its
    docstring because pointing it at D8 is what made ~4100 tests agree with a defect.
    """
    memory.set_u32("D", 100, 0xFFFFFFFF)
    assert memory.bump_u32("D", 100) == 0, "a 32-bit integer wraps at 32 bits"
    assert memory.bump_u32("D", 100, step=7) == 7


def test_a_family_cannot_be_allocated_twice() -> None:
    with pytest.raises(ValueError, match="allocated twice"):
        DeviceMemory((MemoryRange("D", 0, 10), MemoryRange("D", 0, 20)))


def test_an_unknown_family_is_refused() -> None:
    with pytest.raises(ValueError, match="not a family"):
        MemoryRange("NOPE", 0, 1)
