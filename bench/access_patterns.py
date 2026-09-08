"""Batch against random against block: four registers, four ways to ask for them.

Run it against a PLC you are allowed to read:

    python bench/access_patterns.py --host 192.168.10.250 \\
        --profile melsec:iq-f/fx5u --port 5002

**The question this answers.** A control loop wants four floats -- setpoint, process
value, output, error. There are four ways to fetch them and they are not equivalent:

``0x0403`` random read, 4 double-word points
    One round trip, **one snapshot**. Every value was sampled by the same CPU service,
    which is the entire reason to use this command. The library's block plans prebuild
    exactly this frame.
``0x0401`` batch read, 8 contiguous words
    One round trip, one snapshot, but only because the registers happen to be
    contiguous. Move one field and this becomes several reads.
``0x0401`` batch read, four separate reads
    Four round trips, and the values are up to four round trips apart. On our bench a
    split like this sampled the plant up to 27 ms apart -- FX5U-32MT/DS fw 1.065 at
    192.168.10.250, 2026-09-06, from the laptop at 192.168.10.41 over Wi-Fi at ~7 ms
    median RTT, which is the link that number belongs to. This row exists to show what
    the atomicity is worth in milliseconds.
``0x0406`` block read
    Never exercised on our bench -- it is one of the paths shipped labelled unverified.
    Expect this row to be a refusal or a surprise; either is information.

**Time is not the only axis, and the fast row is not automatically the right one.** The
four-reads row will often not be four times slower, because the CPU's service time
dominates the wire time. It is still wrong for a loop, because the four values did not
happen at the same moment, and no latency number will tell you that.

Nothing here writes to the PLC.
"""

from __future__ import annotations

import argparse
import asyncio

# ``_report`` is the sibling file, and a script's own directory is sys.path[0].
# Run these as ``python bench/<name>.py``; there is no package here to install.
from _report import Row, preamble, report

from aslmp.client import Plc
from aslmp.commands import BlockSpec, dword
from aslmp.errors import SlmpError
from aslmp.tools.bench import Distribution, raw_control, timed_samples
from aslmp.wire.address import parse_address


def parse(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--port", type=int, default=5002)
    parser.add_argument(
        "--address",
        default="D0",
        help="the first of four consecutive f32 registers (default: D0, the bench's IO_SP)",
    )
    parser.add_argument("--samples", type=int, default=300)
    parser.add_argument("--warmup", type=int, default=20)
    return parser.parse_args(argv)


async def main() -> int:
    args = parse()
    control = {"samples": args.samples, "warmup": args.warmup}
    rows = [
        Row(
            raw_control(args.host, args.port, label="raw socket (before)", **control),
            control=True,
        )
    ]

    plc = Plc(args.host, args.port, profile=args.profile)
    async with plc:
        base = parse_address(args.address, plc.profile)
        points = [dword(base.offset(2 * step), kind="f32") for step in range(4)]
        singles = [base.offset(2 * step) for step in range(4)]

        async def four_reads() -> None:
            for address in singles:
                await plc.read_words(address, 2)

        cases = [
            ("0x0403 random, 4 dwords (one snapshot)", lambda: plc.read_random(points)),
            ("0x0401 batch, 8 contiguous words", lambda: plc.read_words(base, 8)),
            ("0x0401 batch, 4 separate reads", four_reads),
            ("0x0406 block, 1 block of 8 words", lambda: plc.read_blocks([BlockSpec(base, 8)])),
        ]
        for label, call in cases:
            try:
                samples = await timed_samples(call, samples=args.samples, warmup=args.warmup)
            except SlmpError as exc:
                rows.append(
                    Row(Distribution(label, (), f"{type(exc).__name__}: {exc.args[0][:70]}"))
                )
            else:
                rows.append(Row(Distribution(label, samples)))

    rows.append(
        Row(
            raw_control(args.host, args.port, label="raw socket (after)", **control),
            control=True,
        )
    )
    print(preamble("Access patterns: batch, random and block"))
    print(
        report(
            "One control loop's four floats, four ways",
            rows,
            target=f"{args.host}:{args.port}, profile {args.profile}, tcp / binary / 3E",
        )
    )
    print(
        "The `4 separate reads` row is the one to look at twice. Whatever it costs in\n"
        "milliseconds, it also costs the snapshot: those four values were sampled at four\n"
        "different moments, and no latency column shows that. It is why splitting a random\n"
        "read returns a SplitReading and not a RandomReading -- the loss is in the type."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
