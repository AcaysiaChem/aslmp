"""``read_diagnostics()`` and ``aslmp status``: is anything wrong with the CPU, in one read.

``aslmp probe`` answers whether a connection entry is served. On 2026-09-25 the bench
FX5U probed perfectly healthy while it ran with a self-diagnostic error raised
continuously for a module left unpowered, and nothing in the library could say so short
of reading special registers by hand. The values below are what those registers held.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from typing import Final

import pytest

from aslmp import CpuDiagnostics, CpuStatus, Plc
from aslmp.errors import SlmpCapabilityError, SlmpPayloadShapeError
from aslmp.identity import cpu_diagnostics_points, decode_cpu_diagnostics
from aslmp.testing.server import PlcSimulator
from aslmp.testing.targets import R04CPU

pytestmark = pytest.mark.simulator

_FLAGS_ON: Final = (True, True, *([False] * 14))
_FLAGS_OFF: Final = (False,) * 16

BENCH_2026_09_26: Final[tuple[object, ...]] = (
    _FLAGS_ON,  # SM0 and SM1 ON
    0x3081,  # SD0, the latest self-diagnostic code
    1980, 1, 22, 11, 46, 52, 2,  # SD1-SD7, when it was stamped
    0,  # SD203, RUN
    1980, 1, 22, 11, 46, 53, 2,  # SD210-SD216, the clock
)
"""One real ``0403`` from the bench FX5U-32MT/DS fw 1.065, 2026-09-26."""


# ------------------------------------------------------------------ decoding


def test_the_bench_snapshot_decodes_to_what_the_registers_said() -> None:
    found = decode_cpu_diagnostics(BENCH_2026_09_26)
    assert found == CpuDiagnostics(
        status=CpuStatus.RUN,
        error=True,
        error_code=0x3081,
        error_at=datetime(1980, 1, 22, 11, 46, 52),
        clock=datetime(1980, 1, 22, 11, 46, 53),
    )
    assert found.error_age_s == 1.0


def test_the_error_age_comes_from_the_plc_clock_not_this_host() -> None:
    """Both stamps are the PLC's, so the age is right even when the date is 1980."""
    found = decode_cpu_diagnostics(BENCH_2026_09_26)
    assert found.clock is not None
    assert found.clock.year == 1980
    assert found.error_age_s == 1.0


def test_registers_that_are_not_a_date_decode_to_none_not_to_a_made_up_one() -> None:
    no_error_yet = list(BENCH_2026_09_26)
    no_error_yet[0] = _FLAGS_OFF
    no_error_yet[1:9] = [0] * 8  # nothing stamped: year 0, month 0
    found = decode_cpu_diagnostics(no_error_yet)
    assert found.error is False
    assert found.error_at is None
    assert found.error_age_s is None
    assert found.clock == datetime(1980, 1, 22, 11, 46, 53)


@pytest.mark.parametrize(
    ("year", "month", "day", "hour"),
    [(2026, 2, 30, 12), (2026, 13, 1, 12), (2026, 9, 26, 24), (0, 1, 1, 0)],
    ids=["february-30", "month-13", "hour-24", "year-0"],
)
def test_a_clock_that_names_no_moment_is_none(
    year: int, month: int, day: int, hour: int
) -> None:
    snapshot = list(BENCH_2026_09_26)
    snapshot[10:14] = [year, month, day, hour]  # SD210-SD213
    assert decode_cpu_diagnostics(snapshot).clock is None


def test_a_snapshot_of_the_wrong_shape_is_refused() -> None:
    with pytest.raises(SlmpPayloadShapeError, match="17 word points"):
        decode_cpu_diagnostics(BENCH_2026_09_26[:-1])
    flags_as_word = list(BENCH_2026_09_26)
    flags_as_word[0] = 3
    with pytest.raises(SlmpPayloadShapeError, match="SM0"):
        decode_cpu_diagnostics(flags_as_word)
    a_bool_for_a_word = list(BENCH_2026_09_26)
    a_bool_for_a_word[1] = True
    with pytest.raises(SlmpPayloadShapeError, match="point 1"):
        decode_cpu_diagnostics(a_bool_for_a_word)


def test_an_undocumented_state_is_refused_rather_than_rounded() -> None:
    unknown = list(BENCH_2026_09_26)
    unknown[9] = 7  # SD203
    with pytest.raises(SlmpPayloadShapeError, match="not a documented CPU operating status"):
        decode_cpu_diagnostics(unknown)


def test_the_snapshot_is_seventeen_points_in_one_request() -> None:
    assert len(cpu_diagnostics_points()) == len(BENCH_2026_09_26) == 17


# ------------------------------------------------------------------ the client


async def test_read_diagnostics_reads_the_bench_state_in_one_transaction() -> None:
    async with PlcSimulator() as simulator:
        simulator.memory.write_bits("SM", 0, [True, True])
        simulator.memory.write_words("SD", 0, [0x3081, 1980, 1, 22, 11, 46, 52, 2])
        simulator.memory.write_words("SD", 210, [1980, 1, 22, 11, 46, 53, 2])
        host, port = simulator.address("tcp")
        async with Plc(host, port, profile="melsec:iq-f/fx5u", timeout=2.0) as plc:
            before = plc.counters.transactions_started
            reading = await plc.timed.read_diagnostics()
            assert plc.counters.transactions_started == before + 1, "one 0403, not five reads"
        assert reading.value == decode_cpu_diagnostics(BENCH_2026_09_26)


async def test_another_family_is_refused_before_anything_is_sent() -> None:
    async with PlcSimulator(target=R04CPU) as simulator:
        host, port = simulator.address("tcp")
        async with Plc(host, port, profile="melsec:iq-r", timeout=2.0) as plc:
            before = plc.counters.transactions_started
            with pytest.raises(SlmpCapabilityError, match="measured on an iQ-F"):
                await plc.read_diagnostics()
            assert plc.counters.transactions_started == before, "nothing was sent"


# ------------------------------------------------------------------ the command


async def _status(port: int, profile: str) -> tuple[int, str]:
    """Run the real ``aslmp status`` in a subprocess against this loop's simulator."""
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "aslmp", "status", "127.0.0.1",
        "--port", str(port), "--profile", profile, "--timeout", "5",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await asyncio.wait_for(process.communicate(), 60.0)
    assert process.returncode is not None
    return process.returncode, out.decode()


async def test_aslmp_status_says_what_the_bench_needed_said() -> None:
    async with PlcSimulator() as simulator:
        simulator.memory.write_bits("SM", 0, [True, True])
        simulator.memory.write_words("SD", 0, [0x3081, 1980, 1, 22, 11, 46, 52, 2])
        simulator.memory.write_words("SD", 210, [1980, 1, 22, 11, 46, 53, 2])
        code, out = await _status(simulator.address("tcp")[1], "melsec:iq-f/fx5u")
    assert code == 0, out
    assert "RUN (SD203 = 0)" in out
    assert "YES -- SM0 is ON" in out
    assert "0x3081" in out
    assert "1 s before this read" in out
    assert "almost certainly never set" in out
    assert "does not guess" in out


async def test_aslmp_status_reports_no_error_plainly() -> None:
    async with PlcSimulator() as simulator:
        simulator.memory.write_words("SD", 210, [2026, 9, 26, 12, 0, 0, 6])
        code, out = await _status(simulator.address("tcp")[1], "melsec:iq-f/fx5u")
    assert code == 0, out
    assert "none -- SM0 is OFF" in out
    assert "never set" not in out, "a clock in 2026 is not called unset"


async def test_aslmp_status_on_another_family_reads_the_state_and_says_why_only_that() -> None:
    async with PlcSimulator(target=R04CPU) as simulator:
        code, out = await _status(simulator.address("tcp")[1], "melsec:iq-r")
    assert code == 0, out
    assert "SD203" in out
    assert "measured on an iQ-F CPU only" in out
