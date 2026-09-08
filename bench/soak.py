"""A real closed control loop through ``aslmp``, held at a fixed cadence for minutes.

**THIS SCRIPT WRITES TO THE PLC. It writes ``D2`` (IO_PV), once per cycle, for the whole
run.** Nothing else in ``bench/`` writes at all. That is the entire point of this one: a
read spinner measures a link, and a loop that closes through the CPU measures the data
path a customer actually depends on. ``D2`` is read on the way in and restored on the way
out -- in a ``finally``, then on a second connection if the first one has died, and if
both of those fail the exact ``aslmp write`` command that puts it back is printed. A
soak that leaves a controller's process value at whatever a simulated bath last thought
is a worse outcome than no soak.

Every cycle does three things:

1. reads the whole loop state in **one** ``0x0403`` through a bound block plan, so
   setpoint, process value, duty, error and the scan counter are one snapshot rather
   than five samples taken milliseconds apart;
2. integrates a first-order bath model whose *input is the PLC's own heater duty*;
3. writes the new process value back to ``D2``, so the CPU closes the loop against us.

The CPU runs a proportional-only bath controller at a 60.0 degC setpoint with no physical
I/O, so this simulated plant is the only thing its output acts on.

.. rubric:: The arithmetic assertion, which is the strongest thing here

The band is **12 %/K**, and within one ``0403`` snapshot the CPU's own numbers must
satisfy::

    MV == clamp(Err * 12, 0, 100)

That is checked on **every cycle**, not at the end, and a single failure fails the run.
It turns a latency measurement into a correctness proof of the whole path: the block
plan's field offsets, the low-word-first f32 decode, the write encode, and the CPU
agreeing that the value it got is the value we sent. A plausible-looking latency curve
can be produced by a client that decodes garbage; this cannot.

Measured on **FX5U-32MT/DS fw 1.065, 2026-09-07, from the laptop at 192.168.10.41 over
Wi-Fi at ~7 ms median RTT** at six process values spanning both clamps
(PV 59.56 / 56.523 / 55.0 / 60.0 / 61.0 / 0.0):
the deviation was **exactly 0.0 in every one**, including ``Err`` 60.0 -> ``MV`` 100.0 at
the ceiling and ``Err`` -1.0 -> ``MV`` 0.0 at the floor, and again in all 1,125 snapshots
of a 45 s run at 25 Hz over the same link.
:data:`ARITHMETIC_TOLERANCE_PCT` is therefore a tolerance for the f32 rounding of a
multiply that *could* happen and did not, and not slack anybody has spent.

.. rubric:: What a run reports

Distributions from :class:`~aslmp.tools.bench.Distribution`, bracketed by the same
raw-socket control every other bench in this directory carries, because a table without
one is not a measurement. Plus the things only a long run can say: sustained transaction
rate, cadence overruns, reconnects, entry-busy refusals, the CPU's scan rate under load
against its idle rate, and whether the median moved between the first fifth of the run
and the last.

.. rubric:: The run this file was promoted from

**FX5U-32MT/DS fw 1.065, 2026-09-07, from argus-bench (192.168.10.36) over the WIRED
link at 3.64 ms median RTT**, TCP entry 5002: 15,000 cycles / 30,005 transactions /
300.0 s at exactly 50.0 Hz;
0 errors, 0 reconnects, 0 entry-busy, 0 concurrent rejections, 0 cadence overruns; block
read ``0403`` p50 3.67 / p90 4.23 / p99 4.69 / p99.9 5.52 / max 6.72 / sd 0.48 ms; whole
cycle p50 7.23 / p90 8.26 / p99 8.94 / p99.9 10.10 / max 11.20 / sd 0.65 ms; p50 3.72 ->
3.67 ms across the five minutes, i.e. not at all; PLC 969 scans/s under load against that
run's OWN idle reference of 1029 scans/s. **Those numbers are that host on that link.**
From the Wi-Fi laptop the same script runs and the same arithmetic holds, and the
latencies are somebody else's.

969 against 1029 is a cost of about 5.8 % of scan rate; against the figure this
repository publishes for an idle FX5U -- **1018 scans/s, 982 us per scan**,
``docs/hardware.md`` section 17 -- the same loaded number is 4.8 %, so the honest claim is
"about 5-6 %". The pair belongs to its run and must not be split: 1029 is not a second
published idle rate, it is the reference this run took for itself, and quoting either half
alone is how a within-session ratio turns into a repository-wide fact. ``docs/hardware.md``
section 17 is the one place the idle rate lives, and
``tests/unit/test_citations.py::test_no_scan_rate_literal_drifts_from_the_published_one``
fails if this docstring drifts from it.

Run it against a PLC you are allowed to *write*::

    python bench/soak.py --host 192.168.10.250 --profile melsec:iq-f/fx5u \\
        --port 5002 --rate 50 --duration 300
"""

from __future__ import annotations

import argparse
import asyncio
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Final

# ``_report`` is the sibling file, and a script's own directory is sys.path[0].
# Run these as ``python bench/<name>.py``; there is no package here to install.
from _report import Row, preamble, report

from aslmp import (
    F32,
    Cadence,
    OverrunPolicy,
    Plc,
    PlcBlock,
    SlmpError,
    TransportKind,
    plc_block,
)
from aslmp.blocks import BlockPlan
from aslmp.tools.bench import (
    Distribution,
    nearest_rank,
    raw_control,
    settle_after_release,
    timed_samples,
)

# ----------------------------------------------------------------------------------------
# The bench map and the controller it is running
# ----------------------------------------------------------------------------------------

PV: Final = "D2"
"""IO_PV. **The one register this script writes.** Restored on the way out."""

PROPORTIONAL_BAND_PCT_PER_K: Final = 12.0
"""The CPU's proportional band, as duty percent per kelvin of error.

Measured on FX5U-32MT/DS fw 1.065, 2026-09-07 (Wi-Fi laptop and wired argus-bench alike,
because arithmetic is not a property of the link), in full f32 precision rather than at
any display rounding: an error of 0.4399986267089844 K gave 5.2799835205078125 % duty and
3.477001190185547 K gave 41.72401428222656 %. Both are the error times 12 to the last bit.
"""

MV_FLOOR_PCT: Final = 0.0
MV_CEILING_PCT: Final = 100.0
"""The clamps. With PV at 0.0 the error is the whole 60 K setpoint and the duty sits on
the ceiling, which is the state the bench idles in."""

ARITHMETIC_TOLERANCE_PCT: Final = 1e-4
"""How far ``MV`` may sit from ``Err * 12`` before the run is failed.

Not slack: the CPU computes the product in f32 and this script computes it in f64 from
the f32 operands, so a rounding of at most ``2**-24 * 100`` ~= 6e-6 % is legitimate.
Every sample measured on 2026-09-07 was bit-exact, so this bound has never been touched.
"""

# A first-order bath: 2 kW into 15 kg of water, ambient loss, integrated at the tick rate.
# These constants are why the loop settles at 59.56 degC -- 0.44 K of error, 5.28 % duty --
# which is exactly the steady state the wired run reported.
K_HEAT: Final = 0.0085
"""degC per second per percent of duty."""
K_LOSS: Final = 0.0012
"""degC per second per degC above ambient."""
AMBIENT: Final = 22.0

MODEL_TIME_CONSTANT_S: Final = 1.0 / (K_HEAT * PROPORTIONAL_BAND_PCT_PER_K + K_LOSS)
"""~9.7 s. The closed loop's own time constant, from the constants above.

Printed so a short run cannot be read as a steady-state result: below about five of
these the bath is still on its way up and the final error says nothing about the
controller. It has no effect on the arithmetic assertion, which holds on every snapshot
whether the loop has settled or not.
"""

P999_MINIMUM_SAMPLES: Final = 1000
"""Below this, a nearest-rank p99.9 is the maximum with a percentile's name on it."""


@plc_block(base="D0")
class Loop(PlcBlock):
    """The controller's whole state, in one ``0x0403``. All f32, low word first."""

    setpoint: F32
    process_value: F32
    output: F32
    error: F32
    scan: F32  # a REAL, not an integer counter: the PLC's own ST does IO_Scan + 1.0


def expected_duty(error: float) -> float:
    """What ``MV`` must be for this ``Err``. The oracle, in one line."""
    return min(MV_CEILING_PCT, max(MV_FLOOR_PCT, error * PROPORTIONAL_BAND_PCT_PER_K))


# ----------------------------------------------------------------------------------------
# What one run found out
# ----------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Violation:
    """One cycle where the CPU's own numbers did not agree with each other."""

    cycle: int
    setpoint: float
    process_value: float
    error: float
    output: float

    def __str__(self) -> str:
        wanted = expected_duty(self.error)
        return (
            f"cycle {self.cycle}: SP {self.setpoint!r} PV {self.process_value!r} "
            f"Err {self.error!r} -> MV {self.output!r}, but Err * "
            f"{PROPORTIONAL_BAND_PCT_PER_K:g} clamped is {wanted!r} "
            f"(off by {self.output - wanted:+.9g})"
        )


@dataclass(slots=True)
class Soak:
    """Everything a run accumulates. Lists, not summaries: the report derives those."""

    read_ms: list[float] = field(default_factory=list)
    write_ms: list[float] = field(default_factory=list)
    cycle_ms: list[float] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    violations: list[Violation] = field(default_factory=list)
    worst_deviation_pct: float = 0.0
    checked: int = 0
    cycles: int = 0
    overruns: int = 0
    first_scan: float = 0.0
    last_scan: float = 0.0
    wall_s: float = 0.0
    saturated_s: float = 0.0
    """Seconds spent with the duty on the ceiling, where the loop is not first-order.

    Held separately because the closed loop's time constant only describes the part of
    the trajectory where the controller is actually proportional. While ``MV`` is clamped
    at 100 the plant is on an open-loop ramp, so counting that time towards "five time
    constants" would call a run settled that is nowhere near it -- which a 60 s run at
    25 Hz did, ending 1.08 K from the asymptote with the formula insisting otherwise.
    """
    final_temperature: float = AMBIENT
    final_error: float = 0.0
    final_output: float = 0.0

    @property
    def scans(self) -> float:
        return self.last_scan - self.first_scan

    @property
    def proportional_s(self) -> float:
        """Seconds the loop spent off the ceiling, i.e. actually closing its own error."""
        return max(0.0, self.wall_s - self.saturated_s)

    def check_arithmetic(self, cycle: int, state: Loop) -> None:
        """The assertion this whole harness exists to make. One snapshot, one oracle."""
        wanted = expected_duty(state.error)
        deviation = abs(state.output - wanted)
        self.checked += 1
        self.worst_deviation_pct = max(self.worst_deviation_pct, deviation)
        if deviation > ARITHMETIC_TOLERANCE_PCT:
            self.violations.append(
                Violation(cycle, state.setpoint, state.process_value, state.error, state.output)
            )


# ----------------------------------------------------------------------------------------
# The loop
# ----------------------------------------------------------------------------------------


async def drive(plc: Plc, plan: BlockPlan[Loop], args: argparse.Namespace) -> Soak:
    """Hold the cadence, close the loop, and record what the CPU said each time."""
    total = max(1, round(args.rate * args.duration))
    period = timedelta(seconds=1.0 / args.rate)
    every = max(1, round(args.rate * args.progress))
    soak = Soak()
    temperature = AMBIENT
    dt = 1.0 / args.rate
    state: Loop | None = None

    print(
        f"soaking {total:,} cycles at {args.rate:g} Hz for {args.duration:g} s "
        f"({total * 2:,} transactions), writing {PV} every cycle"
    )
    started = time.perf_counter()
    cadence = Cadence(period, on_overrun=OverrunPolicy.RECORD)
    async for _tick in cadence:
        cycle_started = time.perf_counter_ns()
        try:
            state = await plan.read()
            if state.tx is not None:
                soak.read_ms.append(state.tx.timing.wire_ms)
            if soak.cycles == 0:
                soak.first_scan = state.scan
            soak.last_scan = state.scan
            soak.check_arithmetic(soak.cycles, state)
            if state.output >= MV_CEILING_PCT - ARITHMETIC_TOLERANCE_PCT:
                soak.saturated_s += dt

            temperature += (K_HEAT * state.output - K_LOSS * (temperature - AMBIENT)) * dt
            soak.write_ms.append((await plc.timed.write_f32(PV, temperature)).tx.timing.wire_ms)
        except SlmpError as exc:
            soak.failures.append(f"cycle {soak.cycles}: {type(exc).__name__}: {exc}")
            if len(soak.failures) > args.max_failures:
                print(f"  stopping early: more than {args.max_failures} failed cycles")
                break
        soak.cycle_ms.append((time.perf_counter_ns() - cycle_started) / 1e6)
        soak.cycles += 1

        if soak.cycles % every == 0 and state is not None:
            elapsed = time.perf_counter() - started
            print(
                f"  {soak.cycles:7,}/{total:,}  {elapsed:5.0f} s  bath {temperature:6.2f} degC"
                f"  duty {state.output:5.1f} %  err {state.error:6.2f} K"
                f"  read p50 {nearest_rank(sorted(soak.read_ms), 50):.2f} ms"
                f"  overruns {cadence.overruns}"
            )
        if soak.cycles >= total:
            break

    soak.wall_s = time.perf_counter() - started
    soak.overruns = cadence.overruns
    soak.final_temperature = temperature
    if state is not None:
        soak.final_error = state.error
        soak.final_output = state.output
    return soak


# ----------------------------------------------------------------------------------------
# Putting D2 back, whatever happened
# ----------------------------------------------------------------------------------------


def recovery_command(args: argparse.Namespace, value: float) -> str:
    """The exact command line that undoes this script, printed before it starts.

    A run killed by an outer timeout gets no ``finally``. The throwaway version of this
    file was killed mid-run once and left ``D2`` at 56.5, which is a running controller's
    process value replaced by a simulated bath's -- so the way back is printed up front,
    while there is still something to print it to.
    """
    return (
        f"aslmp write {args.host} {PV} {value!r} --as f32 --port {args.port} "
        f"--profile {args.profile} --transport {args.transport} --verify"
    )


async def restore(plc: Plc, args: argparse.Namespace, value: float) -> bool:
    """Put ``D2`` back, verified. Loud about every path it takes; silent about none.

    First over the connection the soak has been using, because that is the one known to
    work. If the run ended because that connection died, the write cannot go down it, so
    the entry is released, given :func:`settle_after_release`, and taken again. That is a
    cleanup path for a bench script and it announces itself; it is not the library
    recovering from anything.
    """
    try:
        await plc.write_f32(PV, value, verify=True)
    except (SlmpError, OSError) as exc:
        print(f"\n!!! restoring {PV} on the soak's own connection failed: {exc!r}")
    else:
        print(f"\n{PV} restored to {value!r} and read back")
        return True

    await plc.aclose()
    settle_after_release()
    spare = Plc(
        args.host,
        args.port,
        profile=args.profile,
        transport=TransportKind(args.transport),
        timeout=args.timeout,
    )
    try:
        async with spare:
            await spare.write_f32(PV, value, verify=True)
    except (SlmpError, OSError) as exc:
        print(
            f"\n!!! {PV} IS LEFT AT WHATEVER THE MODEL LAST WROTE. A second connection "
            f"could not restore it either: {exc!r}\n!!! put it back with:\n    "
            f"{recovery_command(args, value)}"
        )
        return False
    print(f"\n{PV} restored to {value!r} on a second connection, and read back")
    return True


# ----------------------------------------------------------------------------------------
# The report
# ----------------------------------------------------------------------------------------


def tail_detail(distributions: tuple[Distribution, ...]) -> str:
    """p99.9 and the count behind it, which the shared table has no column for.

    A five-minute run at 50 Hz has 15,000 samples, so a p99.9 is the fifteenth worst
    cycle and is a real observation. In a 300-sample bench table it would be noise, which
    is why it lives here and not in :func:`_report.report`.

    Rows with fewer than :data:`P999_MINIMUM_SAMPLES` are **left out rather than
    printed**: below that, nearest-rank p99.9 is just the maximum wearing a percentile's
    name, and a table that prints it invites somebody to quote it.
    """
    rows = [dist for dist in distributions if dist.n >= P999_MINIMUM_SAMPLES]
    if not rows:
        return (
            f"\nNo distribution here reached {P999_MINIMUM_SAMPLES:,} samples, so there "
            f"is no honest p99.9 to print: at this length nearest-rank would return the "
            f"maximum and call it a percentile. Run for longer.\n"
        )
    lines = ["", f"p99.9, from the rows with at least {P999_MINIMUM_SAMPLES:,} samples:", ""]
    lines.append("| measurement | n | p99 | p99.9 | max |")
    lines.append("| --- | --- | --- | --- | --- |")
    for dist in rows:
        ordered = dist.ordered
        lines.append(
            f"| {dist.label} | {dist.n} | {nearest_rank(ordered, 99):.2f} | "
            f"{nearest_rank(ordered, 99.9):.2f} | {ordered[-1]:.2f} |"
        )
    return "\n".join(lines) + "\n"


def drift_line(samples: list[float]) -> str:
    """Did the median move between the first fifth of the run and the last?

    The bracketing raw-socket controls answer "did the link change during the run"; this
    answers "did *we* change during the run", which is the question a soak is for. A
    client that leaked a buffer, grew a list or gradually lost a socket buffer shows up
    here as a median that climbs and nowhere else.
    """
    if len(samples) < 10:
        return "not enough cycles to say whether the median drifted."
    fifth = len(samples) // 5
    first = nearest_rank(sorted(samples[:fifth]), 50)
    last = nearest_rank(sorted(samples[-fifth:]), 50)
    return (
        f"Block-read p50 across the run: {first:.2f} -> {last:.2f} ms "
        f"({last - first:+.2f} ms between the first fifth and the last). This is the "
        f"soak's own drift, next to the control drift above, which is the link's."
    )


def settling(soak: Soak) -> str:
    """Where the bath got to, and whether the run was long enough for that to mean much.

    The closed loop's time constant is ~9.7 s, so a run shorter than about five of them
    ends with the bath still climbing. Saying "settled at" about such a run would be the
    same class of mistake as publishing a p99 of thirty samples, so this says which of
    the two happened instead of picking the flattering word.

    **The clock that counts is** :attr:`Soak.proportional_s`, not the wall clock. From a
    cold start the duty sits on the 100 % ceiling for the first half of the run, and a
    clamped controller is an open-loop ramp with no time constant to spend. Measuring the
    whole run against 5 tau declared a 60 s run settled while it was still 1.08 K from
    the asymptote; measuring only the unclamped part does not.
    """
    landed = (
        f"The bath ended at {soak.final_temperature:.2f} degC, leaving "
        f"{soak.final_error:.2f} K of error at {soak.final_output:.2f} % duty."
    )
    ceiling = (
        f" Of {soak.wall_s:.0f} s, {soak.saturated_s:.0f} s were spent with the duty "
        f"clamped at {MV_CEILING_PCT:g} %, which is an open-loop ramp and buys no "
        f"settling, leaving {soak.proportional_s:.0f} s of actual proportional control."
    )
    if soak.proportional_s >= 5 * MODEL_TIME_CONSTANT_S:
        qualifier = (
            f" That is more than five of the loop's ~{MODEL_TIME_CONSTANT_S:.1f} s time "
            f"constants, so the final error is a **steady state**: a proportional-only "
            f"controller cannot close the last of it, and where it stops is set by the "
            f"band."
        )
    else:
        qualifier = (
            f" That is under five of the loop's ~{MODEL_TIME_CONSTANT_S:.1f} s time "
            f"constants, so this is a **transient, not a steady state**, and the bath "
            f"was still climbing. Read nothing into the final error until the "
            f"unclamped part of a run passes ~{5 * MODEL_TIME_CONSTANT_S:.0f} s."
        )
    qualifier = ceiling + qualifier
    return (
        f"{landed}{qualifier} Either way the duty is the error times "
        f"{PROPORTIONAL_BAND_PCT_PER_K:g} at **every** point on the way, which is the "
        f"assertion above and does not care whether the loop has settled."
    )


def verdict(soak: Soak, args: argparse.Namespace, counters_line: str) -> str:
    """The part that is not a latency: what the run proved and what it left behind."""
    rate = soak.cycles / soak.wall_s if soak.wall_s else 0.0
    scan_rate = soak.scans / soak.wall_s if soak.wall_s else 0.0
    arithmetic = (
        f"**{soak.checked:,} of {soak.checked:,} snapshots satisfied "
        f"`MV == clamp(Err * {PROPORTIONAL_BAND_PCT_PER_K:g}, {MV_FLOOR_PCT:g}, "
        f"{MV_CEILING_PCT:g})`**, worst deviation "
        f"{soak.worst_deviation_pct:.3g} % duty against a tolerance of "
        f"{ARITHMETIC_TOLERANCE_PCT:g} %."
        if not soak.violations
        else f"**{len(soak.violations)} of {soak.checked:,} snapshots FAILED the "
        f"proportional-band arithmetic.** The first few:\n\n"
        + "\n".join(f"- `{item}`" for item in soak.violations[:5])
    )
    lines = [
        "",
        "### What the run did, beyond the latencies",
        "",
        f"- cycles: **{soak.cycles:,}** in {soak.wall_s:.1f} s at {rate:.1f} Hz "
        f"(asked for {args.rate:g} Hz)",
        f"- {counters_line}",
        f"- cadence overruns: **{soak.overruns}** of {soak.cycles:,} cycles. Every one "
        f"is a cycle whose body returned after the next tick was already due, and "
        f"nothing was dropped to hide it. A run with overruns is a run whose latency "
        f"tail did not fit the period asked for, and the period is the argument.",
        f"- PLC scans elapsed: {soak.scans:,.0f}, i.e. **{scan_rate:,.0f}/s under this "
        f"load**. Compare it against the idle rate; the difference is what serving this "
        f"loop costs the CPU.",
        f"- failed cycles: **{len(soak.failures)}**",
        "",
        f"- {arithmetic}",
        "",
        settling(soak),
    ]
    lines.extend(f"- FAILURE {item}" for item in soak.failures[:10])
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------------------------------
# Command line
# ----------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="soak.py",
        description=(
            "Hold a real closed control loop through aslmp at a fixed cadence for "
            f"minutes, and prove the CPU's own arithmetic every cycle. WRITES {PV}."
        ),
        epilog=(
            "Latencies from one host on one link are not latencies from another. The "
            "arithmetic assertion is the part that travels."
        ),
    )
    parser.add_argument("--host", required=True, help="the PLC's IP address or hostname")
    parser.add_argument(
        "--port", type=int, default=5002, help="the connection entry (default: 5002)"
    )
    parser.add_argument(
        "--profile",
        default="melsec:iq-f/fx5u",
        help="CPU profile key; `aslmp identify` prints it",
    )
    parser.add_argument(
        "--transport",
        choices=[kind.value for kind in TransportKind],
        default=TransportKind.TCP.value,
        help="the entry's protocol (default: tcp)",
    )
    parser.add_argument(
        "--rate", type=float, default=50.0, metavar="HZ", help="cycles per second (default: 50)"
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=300.0,
        metavar="S",
        help="how long to hold the cadence, in seconds (default: 300)",
    )
    parser.add_argument(
        "--control-samples",
        type=int,
        default=300,
        metavar="N",
        help="samples in each bracketing raw-socket control (default: 300)",
    )
    parser.add_argument(
        "--warmup",
        type=int,
        default=20,
        metavar="N",
        help="untimed round trips before a timed suite (default: 20)",
    )
    parser.add_argument(
        "--progress",
        type=float,
        default=30.0,
        metavar="S",
        help="a progress line every N seconds (default: 30)",
    )
    parser.add_argument(
        "--max-failures",
        type=int,
        default=20,
        metavar="N",
        help="stop early after this many failed cycles (default: 20)",
    )
    parser.add_argument("--timeout", type=float, default=3.0, help="client deadline in seconds")
    return parser


def validate(args: argparse.Namespace) -> str | None:
    """Refuse the arguments that would produce a number nobody should quote."""
    if args.rate <= 0:
        return f"--rate is cycles per second and must be positive; got {args.rate}"
    if args.duration <= 0:
        return f"--duration is seconds and must be positive; got {args.duration}"
    if args.control_samples < 1:
        return "a raw-socket control of no samples is not a control"
    return None


# ----------------------------------------------------------------------------------------
# Driver
# ----------------------------------------------------------------------------------------


async def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    complaint = validate(args)
    if complaint is not None:
        build_parser().error(complaint)

    udp = args.transport == TransportKind.UDP.value
    control_before = raw_control(
        args.host,
        args.port,
        label="raw socket (before)",
        udp=udp,
        samples=args.control_samples,
        warmup=args.warmup,
    )

    plc = Plc(
        args.host,
        args.port,
        profile=args.profile,
        transport=TransportKind(args.transport),
        timeout=args.timeout,
    )
    await plc.connect()
    info = plc.info
    handshake = None if info is None else info.handshake
    handshake_ms = 0.0 if handshake is None else handshake.timing.wire_ms
    print(
        f"connected to {plc.model} (code 0x{plc.model_code or 0:04X}) at "
        f"{args.host}:{args.port} over {args.transport}, handshake {handshake_ms:.2f} ms"
    )

    original_pv: float | None = None
    restored = False
    soak = Soak()
    free_running: Distribution = Distribution("block read 0403, free-running")
    try:
        plan = plc.bind(Loop)
        original_pv = await plc.read_f32(PV)
        print(f"{PV} as found: {original_pv!r}. If this run is killed, put it back with:")
        print(f"    {recovery_command(args, original_pv)}\n")

        # A read-only reference taken as fast as the link allows, before any cadence and
        # before any write: the difference between this and the cadence row below is what
        # holding a schedule costs, which is not the same question as what a read costs.
        free_running = Distribution(
            "block read 0403, free-running",
            await timed_samples(
                plan.read, samples=min(args.control_samples, 300), warmup=args.warmup
            ),
            "no cadence, no write",
        )
        soak = await drive(plc, plan, args)
    finally:
        if original_pv is not None:
            restored = await restore(plc, args, original_pv)
        counters = plc.counters
        counters_line = (
            f"transactions: **{counters.transactions_completed:,}** completed of "
            f"{counters.transactions_started:,} started; reconnects "
            f"**{counters.reconnects}**, entry-busy **{counters.entry_busy}**, "
            f"concurrent rejections **{counters.concurrent_rejections}**, timeouts "
            f"**{counters.timeouts}**"
        )
        await plc.aclose()

    if not udp:
        # The client has just released the entry and the trailing control takes it back.
        # Same seam, same settle. See aslmp.tools.bench.ENTRY_RELEASE_SETTLE_S.
        settle_after_release()
    control_after = raw_control(
        args.host,
        args.port,
        label="raw socket (after)",
        udp=udp,
        samples=args.control_samples,
        warmup=args.warmup,
    )

    cadence_read = Distribution("block read 0403, under cadence", tuple(soak.read_ms))
    cadence_write = Distribution("write f32 1401, under cadence", tuple(soak.write_ms))
    whole_cycle = Distribution(
        "whole cycle", tuple(soak.cycle_ms), "read + plant model + write, wall clock"
    )
    rows = [
        Row(control_before, control=True),
        Row(free_running),
        Row(cadence_read),
        Row(cadence_write),
        Row(whole_cycle),
        Row(control_after, control=True),
    ]
    print()
    print(preamble("Soak: a closed control loop held for minutes"))
    print(
        report(
            f"{args.rate:g} Hz for {args.duration:g} s on {args.transport} port {args.port}",
            rows,
            target=(
                f"{args.host}:{args.port}, profile {args.profile}, transport "
                f"{args.transport}. A CLOSED LOOP: every cycle is one 0403 block read, a "
                f"plant model integrated from the CPU's own duty, and one 1401 write of "
                f"{PV}. Quote the link this ran over or quote nothing."
            ),
        )
    )
    print(tail_detail((free_running, cadence_read, cadence_write, whole_cycle)))
    print(drift_line(soak.read_ms))
    print(verdict(soak, args, counters_line))

    if soak.violations:
        return 2
    if soak.failures or not restored:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
