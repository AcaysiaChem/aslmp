"""``aslmp bench`` -- latency distributions beside a same-session raw-socket control.

**This command refuses to print a table without a control row.** The laptop at
192.168.10.41, the same PLC and the same Wi-Fi link gave p50 7.1 / p99 18.8 ms on one day
and p50 10.3 / p99 95.2 ms on another (``docs/benchmarking.md``, which is where that
figure and its conditions live). A published number with no control is not a measurement,
it is a souvenir of an afternoon. So every run brackets the library's own suites with a raw
socket that shares **no code with this library**: it builds the 3E binary frame from
literal bytes and ``struct``, reads the response by its declared length, and stamps
``time.monotonic_ns`` on either side. If that control cannot run, nothing is printed.

**The control runs twice, before and after.** A drift between the two bracketing runs is
the run telling you the link changed underneath it, and it is far more common on a real
plant network than anybody's overhead. Compare the two before you compare anything else.

**Distributions, never a mean.** A control loop is a jitter problem: min / p50 / p90 /
p99 / max / stdev, from the same nearest-rank function for both sides so the two columns
are comparable. A single mean would hide the one thing worth knowing, and the transport
comparison is the case in point: on Wi-Fi (2026-09-06, the laptop) the two transports
split the columns -- UDP took the median, 6.20 against 7.41 ms, and TCP took the tail,
p99 10.49 against 13.80 -- while on wire (2026-09-07, ``argus-bench``, interleaved,
control drift 0.01 ms at p50) there is no split at all and UDP takes p50, p90 and p99
alike, 2.42 / 3.40 / 3.56 against 3.63 / 4.05 / 4.69. Same client, same CPU, same
command; two different shapes. Only percentiles show that they *are* different shapes,
and only the link label says which one you are reading.

**Bracketing the run means releasing an entry and taking it straight back**, twice. An
SLMP connection entry is not instantly available to the next ``connect()`` after its own
close, so a connect that arrives too soon gets ``SlmpConnectionEntryBusyError`` with
nothing else connected.
Every seam here takes :data:`ENTRY_RELEASE_SETTLE_S`, a 5 ms wait against a 2 ms measured
window; see that constant for the table it comes from. It is a settle, not a retry.

**Nothing here writes to the PLC.** Every suite is a read, and there is no flag that
makes one a write: a benchmark is the last place you want a mutation, and a write
benchmark against a machine in RUN is somebody else's decision to make deliberately.
"""

from __future__ import annotations

import argparse
import math

# The raw-socket control is the whole point of this module, so this is the one file
# outside transport/ that may open one. The ban exists to keep wire/ and commands/
# I/O-free; tools/ is layer 9 and the control must share no code with the library.
import socket  # noqa: TID251
import struct
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from aslmp.errors import SlmpError, SlmpUsageError
from aslmp.tools import EXIT_OK
from aslmp.tools._common import (
    add_connection_arguments,
    build_client,
    columns,
    guarded,
    parse_or_exit,
    usage,
    warn,
)

if TYPE_CHECKING:
    from aslmp.client import Plc

__all__ = [
    "ENTRY_RELEASE_SETTLE_S",
    "Distribution",
    "build_parser",
    "raw_control",
    "run",
    "settle_after_release",
    "timed_samples",
]


# ----------------------------------------------------------------------------------------
# Releasing an entry and taking it again in the same breath
# ----------------------------------------------------------------------------------------

ENTRY_RELEASE_SETTLE_S: Final = 0.005
"""Seconds to wait after releasing an SLMP connection entry before connecting to it again.

A benchmark run is the one place that closes a connection to an entry and immediately
opens another to the same entry -- the control brackets the library's rows, so there are
two such seams in every run. Without this wait the second connect can lose a race the CPU
does not know it is running.

**Measured on FX5U-32MT/DS fw 1.065, 2026-09-07**, from a wired host on the same /24 with
a median RTT of 3.64 ms, six trials per gap:

    gap after a clean close() -> next connect
       0 ms                1/6 OK
       1 ms                2/6 OK
       2 ms                6/6 OK
       5 ms and above      6/6 OK   (tested to 200 ms)

It does **not behave like a fixed hold period**: the same test over Wi-Fi at ~7 ms RTT was
30/30 at every gap including 0 ms, so what has to elapse tracks the link rather than the
clock, and the slower link's own latency already covers it. That fits a race against the
CPU's own FIN processing, which is the reading ``A-ENTRY-RELEASE-RACE`` records -- an
inference from the timings, not something visible from out here. The consequence holds
either way: the number is **link-dependent, and a faster link should widen the window**,
so 2 ms is what one CPU did on one link from one host on one day rather than a spec value.
5 ms is that measurement with a 2.5x margin **on that link**, which is not a 2.5x margin
on a faster one; it is chosen because it is free once per bench run.

If it is ever not enough the run fails loudly with
:class:`~aslmp.errors.SlmpConnectionEntryBusyError`, which now says so. It is deliberately
not a retry loop: retrying a connect would be the silent recovery this package refuses
everywhere else, and it would hide a genuinely busy entry behind a slow success.
"""


def settle_after_release() -> None:
    """Block for :data:`ENTRY_RELEASE_SETTLE_S` after releasing a TCP entry.

    Called by the *releaser*, straight after the socket is closed, so that whatever
    connects next to that entry -- the library's client, or the trailing control -- is
    not racing this process's own FIN. Blocking is correct here: a bench has nothing else
    to do, and the wait must have happened before the next connect starts.
    """
    time.sleep(ENTRY_RELEASE_SETTLE_S)


# ----------------------------------------------------------------------------------------
# Statistics -- one implementation, used for both columns
# ----------------------------------------------------------------------------------------


def nearest_rank(sorted_ms: Sequence[float], percentile: float) -> float:
    """The nearest-rank percentile: the smallest sample at or above ``percentile``.

    No interpolation. An interpolated p99 of 300 samples reports a latency that was
    never observed, which is the wrong kind of number to publish about a machine.
    """
    if not sorted_ms:
        raise ValueError("a percentile of no samples is not a number")
    rank = max(1, math.ceil(percentile / 100.0 * len(sorted_ms)))
    return sorted_ms[min(rank, len(sorted_ms)) - 1]


@dataclass(frozen=True, slots=True)
class Distribution:
    """One suite's samples, in milliseconds, and the shape of them.

    Frozen and carrying every sample, not just the summary: a caller who wants a
    histogram, a CSV or a different percentile method has the raw data, and the summary
    cannot drift from it.
    """

    label: str
    samples: tuple[float, ...] = field(default_factory=tuple)
    note: str = ""

    @property
    def n(self) -> int:
        return len(self.samples)

    @property
    def ordered(self) -> tuple[float, ...]:
        return tuple(sorted(self.samples))

    @property
    def mean(self) -> float:
        return sum(self.samples) / len(self.samples)

    @property
    def stdev(self) -> float:
        """Population standard deviation. Zero for a single sample, never undefined."""
        if len(self.samples) < 2:
            return 0.0
        mean = self.mean
        return math.sqrt(sum((value - mean) ** 2 for value in self.samples) / len(self.samples))

    def row(self) -> list[str]:
        """One table row: label, n, min, p50, p90, p99, max, stdev."""
        if not self.samples:
            return [self.label, "0", "--", "--", "--", "--", "--", "--", self.note]
        ordered = self.ordered
        return [
            self.label,
            str(self.n),
            f"{ordered[0]:.2f}",
            f"{nearest_rank(ordered, 50):.2f}",
            f"{nearest_rank(ordered, 90):.2f}",
            f"{nearest_rank(ordered, 99):.2f}",
            f"{ordered[-1]:.2f}",
            f"{self.stdev:.2f}",
            self.note,
        ]


async def timed_samples(
    call: Callable[[], Awaitable[object]], *, samples: int, warmup: int
) -> tuple[float, ...]:
    """Time ``call`` ``samples`` times after ``warmup`` untimed round trips.

    The clock is around the whole ``await``, so what it measures is what a caller
    measures: encode, gate, wire, decode and the event-loop hop, not the transport's own
    ``wire_ns``. Comparing this against the raw-socket control is the point, and the
    control has no event loop, so excluding the loop from one side would flatter us.

    Warmup exists because the first transaction on a fresh connection pays for the
    connect handshake's page faults and the CPU's first service of this entry, and
    because a benchmark whose first sample is its worst is reporting a startup cost as a
    latency.
    """
    if samples < 1:
        raise ValueError("a distribution of no samples is not a measurement")
    timings: list[float] = []
    for index in range(samples + warmup):
        started = time.monotonic_ns()
        await call()
        elapsed = (time.monotonic_ns() - started) / 1e6
        if index >= warmup:
            timings.append(elapsed)
    return tuple(timings)


HEADER: tuple[str, ...] = ("suite", "n", "min", "p50", "p90", "p99", "max", "stdev", "note")
"""The columns, in the order a control-loop engineer reads them: the tail last but one,
because the tail is the number that decides whether a cadence holds."""


# ----------------------------------------------------------------------------------------
# The control. Shares no code with the library, on purpose.
# ----------------------------------------------------------------------------------------

_SUBHEADER_3E = b"\x50\x00"
_ROUTE = b"\x00\xff\xff\x03\x00"
_PREFIX_LEN = 9
"""Subheader (2) + network (1) + station (1) + module I/O (2) + multidrop (1) + L (2)."""


def _control_request(head: int, count: int) -> bytes:
    """A 3E binary ``0x0401`` word read, built from literal bytes and ``struct``.

    Deliberately hand-written. If this used ``aslmp.wire`` it would be measuring the
    library against itself, and the length arithmetic -- the one field whose overstatement
    hangs the CPU with no response at all -- would be shared with the thing under test.
    """
    payload = struct.pack("<I", head)[:3] + b"\xa8" + struct.pack("<H", count)
    body = struct.pack("<HHH", 0x0000, 0x0401, 0x0000) + payload
    return _SUBHEADER_3E + _ROUTE + struct.pack("<H", len(body)) + body


def _control_response(sock: socket.socket, expect_words: int) -> None:
    """Read exactly one response by its declared length, and check the end code."""
    buf = bytearray()
    while len(buf) < _PREFIX_LEN:
        chunk = sock.recv(_PREFIX_LEN - len(buf))
        if not chunk:
            raise ConnectionError(
                "the control socket read 0 bytes. On this hardware that is the CPU "
                "closing an SLMP connection entry it is already serving."
            )
        buf += chunk
    declared = struct.unpack_from("<H", buf, 7)[0]
    total = _PREFIX_LEN + declared
    while len(buf) < total:
        chunk = sock.recv(total - len(buf))
        if not chunk:
            raise ConnectionError("the control socket lost the connection mid-frame")
        buf += chunk
    end_code = struct.unpack_from("<H", buf, 9)[0]
    if end_code != 0:
        raise ConnectionError(f"the control read returned end code 0x{end_code:04X}")
    if declared != 2 + 2 * expect_words:
        raise ConnectionError(
            f"the control read declared L = {declared}, not the "
            f"{2 + 2 * expect_words} of {expect_words} words plus an end code"
        )


def raw_control(
    host: str,
    port: int,
    *,
    label: str,
    udp: bool = False,
    head: int = 0,
    words: int = 2,
    samples: int = 200,
    warmup: int = 10,
    timeout: float = 3.0,
) -> Distribution:
    """Time ``samples`` word reads through a bare socket. No ``aslmp`` code is involved.

    Blocking on purpose: an event loop between the clock and the socket is exactly the
    overhead this control exists to exclude.

    On TCP it calls :func:`settle_after_release` on the way out. The control holds the
    same connection entry the library's rows are about, and whoever connects next --
    normally within microseconds -- would otherwise race this close. The wait is outside
    every timed section and cannot touch a sample.
    """
    request = _control_request(head, words)
    timings: list[float] = []
    kind = socket.SOCK_DGRAM if udp else socket.SOCK_STREAM
    sock = socket.socket(socket.AF_INET, kind)
    try:
        sock.settimeout(timeout)
        sock.connect((host, port))
        if not udp:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        for index in range(samples + warmup):
            started = time.monotonic_ns()
            sock.sendall(request)
            if udp:
                datagram = sock.recv(65535)
                if len(datagram) < _PREFIX_LEN:
                    raise ConnectionError(f"short control datagram: {len(datagram)} bytes")
                end_code = struct.unpack_from("<H", datagram, 9)[0]
                if end_code != 0:
                    raise ConnectionError(f"control datagram end code 0x{end_code:04X}")
            else:
                _control_response(sock, words)
            elapsed = (time.monotonic_ns() - started) / 1e6
            if index >= warmup:
                timings.append(elapsed)
    finally:
        sock.close()
        if not udp:
            # We have just released the entry. A UDP entry is bound to a peer address
            # rather than to a socket -- two UDP sockets from different source ports
            # were served concurrently on the bench -- so only TCP has this to settle.
            settle_after_release()
    return Distribution(label, tuple(timings), "raw socket, no aslmp code")


# ----------------------------------------------------------------------------------------
# The suites
# ----------------------------------------------------------------------------------------

SUITES: tuple[str, ...] = (
    "self-test",
    "batch-1w",
    "batch-2w",
    "batch-960w",
    "random-4dw",
    "block",
)
"""What ``--suite`` accepts. ``batch-2w`` is the one that lines up with the control: same
command, same device, same point count, so the difference between those two rows is this
library's overhead and nothing else."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aslmp bench",
        description=(
            "Measure this library's latency against a same-session raw-socket control. "
            "Prints distributions; refuses to print anything without the control."
        ),
        epilog=(
            "Run it twice on different days before you believe a tail number. Our own "
            "rig moved 5x at p99 between two afternoons with nothing changed but the "
            "weather on the Wi-Fi link."
        ),
    )
    add_connection_arguments(parser)
    parser.add_argument(
        "--samples", type=int, default=200, metavar="N", help="samples per suite (default: 200)"
    )
    parser.add_argument(
        "--warmup",
        type=int,
        default=10,
        metavar="N",
        help="untimed round trips before each suite (default: 10)",
    )
    parser.add_argument(
        "--address", default="D0", help="the register a read suite starts at (default: D0)"
    )
    parser.add_argument(
        "--suite",
        action="append",
        default=[],
        choices=SUITES,
        help="run only this suite (repeatable; default: all of them)",
    )
    parser.add_argument(
        "--no-control",
        action="store_true",
        help=argparse.SUPPRESS,  # exists only so the refusal below can name it
    )
    return parser


def run(argv: Sequence[str]) -> int:
    args = parse_or_exit(build_parser(), argv)
    if args.no_control:
        return usage(
            "there is no --no-control. A latency table without a same-session raw-socket "
            "control is not a measurement: this rig moved 5x at p99 between two "
            "afternoons. The control is the row that makes the others mean anything."
        )
    if args.samples < 1:
        return usage(f"--samples must be at least 1; got {args.samples}")
    if args.encoding != "binary" or args.frame != "3E":
        return usage(
            f"the raw-socket control speaks 3E binary only, so --encoding "
            f"{args.encoding} --frame {args.frame} would leave the library's rows with "
            f"nothing to be compared against. Bench the binary 3E entry."
        )

    wanted = tuple(args.suite) if args.suite else SUITES
    udp = args.transport == "udp"

    async def body() -> int:
        try:
            before = raw_control(
                args.host,
                args.port,
                label="raw socket (before)",
                udp=udp,
                samples=args.samples,
                warmup=args.warmup,
                timeout=args.timeout,
            )
        except OSError as exc:
            warn(
                f"the raw-socket control could not run: {exc}\n"
                f"Nothing is printed. A library latency number with no control beside it "
                f"is not a measurement, so this command has nothing to say."
            )
            return 1

        rows: list[list[str]] = [list(HEADER), before.row()]
        plc = build_client(args)
        async with plc:
            for name in wanted:
                rows.append((await _run_suite(plc, name, args)).row())
        if not udp:
            # The client has just released the entry and the trailing control is about
            # to take it again. Same seam, same settle, same measurement.
            settle_after_release()

        after = raw_control(
            args.host,
            args.port,
            label="raw socket (after)",
            udp=udp,
            samples=args.samples,
            warmup=args.warmup,
            timeout=args.timeout,
        )
        rows.append(after.row())
        print(
            f"{plc.profile.key} at {args.host}:{args.port} -- "
            f"{args.transport} / {args.encoding} / {args.frame}, all times in ms"
        )
        print(columns(rows))
        _print_drift(before, after)
        return EXIT_OK

    return guarded(body)


def _print_drift(before: Distribution, after: Distribution) -> None:
    """Say how far the two control runs moved. This is the honesty line."""
    if not before.samples or not after.samples:
        return
    first = nearest_rank(before.ordered, 50)
    second = nearest_rank(after.ordered, 50)
    drift = abs(second - first)
    print(
        f"\nControl drift across the run: p50 {first:.2f} -> {second:.2f} ms "
        f"({drift:.2f} ms). Every library row above sits between those two controls; a "
        f"drift comparable to the library's own overhead means this session measured the "
        f"link, not the library."
    )


async def _run_suite(plc: Plc, name: str, args: argparse.Namespace) -> Distribution:
    """One suite's samples. A refusal is recorded as a row, never as a crash.

    A capability the profile refuses -- block access on an iQ-F, say -- is a fact about
    the CPU and belongs in the table. It is reported with the refusal's own words and
    the suite is not silently skipped.
    """
    from aslmp.commands import BlockSpec, dword
    from aslmp.wire.address import parse_address

    address: str = args.address
    samples: int = args.samples
    warmup: int = args.warmup
    timings: list[float] = []
    try:
        # Built once, outside the loop: a bench that re-parses an address every cycle is
        # measuring its own argument handling.
        base = parse_address(address, plc.profile)
        random_points = [dword(base.offset(2 * step), kind="f32") for step in range(4)]
        blocks = [BlockSpec(base, 2)]
        for index in range(samples + warmup):
            started = time.monotonic_ns()
            if name == "self-test":
                await plc.self_test()
            elif name == "batch-1w":
                await plc.read_words(address, 1)
            elif name == "batch-2w":
                await plc.read_words(address, 2)
            elif name == "batch-960w":
                await plc.read_words(address, 960)
            elif name == "random-4dw":
                await plc.read_random(random_points)
            else:
                await plc.read_blocks(blocks)
            elapsed = (time.monotonic_ns() - started) / 1e6
            if index >= warmup:
                timings.append(elapsed)
    except SlmpUsageError as exc:
        return Distribution(name, tuple(timings), f"refused pre-transport: {exc.args[0][:60]}")
    except SlmpError as exc:
        return Distribution(name, tuple(timings), f"{type(exc).__name__}: {exc.args[0][:60]}")
    return Distribution(name, tuple(timings))
