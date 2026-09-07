"""TCP against UDP, on the same CPU, in the same minute, each beside its own control.

Run it against a PLC you are allowed to read:

    python bench/transports.py --host 192.168.10.250 \\
        --profile melsec:iq-f/fx5u --tcp-port 5002 --udp-port 5001

**Why both transports in one script.** The comparison is only meaningful if the two
distributions were taken minutes apart on the same link, and it is only interpretable if
each has its own raw-socket control -- UDP's control is a different code path in the
kernel, so borrowing TCP's would compare two different things.

**What we measured, 2026-09-06, FX5U-32MT/DS fw 1.065, Wi-Fi:** 300 sequential 2-word
reads each way gave UDP p50 6.20 / p99 13.80 / stdev 1.79 and TCP p50 7.41 / p99 10.49 /
stdev 1.03. UDP wins the median by about 1.2 ms because there is no ACK round trip; TCP
wins the tail and has nearly half the standard deviation, because on this link a lost
datagram costs a full client timeout rather than a fast retransmit. **A control loop is
a jitter problem, so TCP is the library's default.** Do not read the median and switch.

Nothing here writes to the PLC.
"""

from __future__ import annotations

import argparse
import asyncio

# ``_report`` is the sibling file, and a script's own directory is sys.path[0].
# Run these as ``python bench/<name>.py``; there is no package here to install.
from _report import Row, preamble, report

from aslmp.client import Plc
from aslmp.tools.bench import Distribution, raw_control, timed_samples
from aslmp.transport import TransportKind


def parse(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--tcp-port", type=int, default=5002)
    parser.add_argument("--udp-port", type=int, default=5001)
    parser.add_argument("--address", default="D0")
    parser.add_argument("--samples", type=int, default=300)
    parser.add_argument("--warmup", type=int, default=20)
    return parser.parse_args(argv)


async def one_transport(args: argparse.Namespace, kind: TransportKind, port: int) -> str:
    """One transport's table: control, three read shapes, control again."""
    udp = kind is TransportKind.UDP
    rows = [
        Row(
            raw_control(
                args.host,
                port,
                label=f"raw socket {kind.value} (before)",
                udp=udp,
                samples=args.samples,
                warmup=args.warmup,
            ),
            control=True,
        )
    ]
    plc = Plc(args.host, port, profile=args.profile, transport=kind)
    async with plc:
        for label, call in (
            ("self test 0x0619", lambda: plc.self_test()),
            ("batch read 2 words", lambda: plc.read_words(args.address, 2)),
            ("batch read 960 words", lambda: plc.read_words(args.address, 960)),
        ):
            samples = await timed_samples(call, samples=args.samples, warmup=args.warmup)
            rows.append(Row(Distribution(label, samples)))
    rows.append(
        Row(
            raw_control(
                args.host,
                port,
                label=f"raw socket {kind.value} (after)",
                udp=udp,
                samples=args.samples,
                warmup=args.warmup,
            ),
            control=True,
        )
    )
    note = (
        "A 960-word read is 1935 bytes, well over the 1500-byte Ethernet MTU, so a UDP "
        "read of that size is IP-fragmented and every fragment must arrive. That is one "
        "reason large block reads belong on TCP."
        if udp
        else "TCP responses segment: 1 of 3 identical 1931-byte reads split at the "
        "1460-byte MSS on our bench, which is why the receive stamp is taken after the "
        "LAST chunk."
    )
    return report(
        f"{kind.value.upper()} on port {port}",
        rows,
        target=f"{args.host}:{port}, profile {args.profile}. {note}",
    )


async def main() -> int:
    args = parse()
    sections = [preamble("Transport comparison: TCP against UDP")]
    for kind, port in ((TransportKind.TCP, args.tcp_port), (TransportKind.UDP, args.udp_port)):
        sections.append(await one_transport(args, kind, port))
    print("\n".join(sections))
    print(
        "TCP is the library's default. UDP wins the median and loses the tail, and a "
        "control loop is a jitter problem. Compare the p99 and stdev columns, not the "
        "p50, before switching."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
