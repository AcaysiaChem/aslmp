"""TCP against UDP, on the same CPU, in the same minute, each beside its own control.

Run it against a PLC you are allowed to read:

    python bench/transports.py --host 192.168.10.250 \\
        --profile melsec:iq-f/fx5u --tcp-port 5002 --udp-port 5001

**Why both transports in one script.** The comparison is only meaningful if the two
distributions were taken minutes apart on the same link, and it is only interpretable if
each has its own raw-socket control -- UDP's control is a different code path in the
kernel, so borrowing TCP's would compare two different things.

**What we measured, and the conclusion the link overturned.** FX5U-32MT/DS fw 1.065,
300 sequential 2-word reads each way.

*2026-09-06, from the laptop at 192.168.10.41 over Wi-Fi at ~7 ms median RTT:* UDP p50
6.20 / p99 13.80 / stdev 1.79 and TCP
p50 7.41 / p99 10.49 / stdev 1.03. UDP won the median, TCP won the tail, and that tail
was published as the reason TCP is the default.

*2026-09-07, from ``argus-bench`` at 192.168.10.36 over the wired link at 3.64 ms median
RTT*, TCP/UDP interleaved with controls
before and after that drifted 0.01 ms at p50: UDP p50 2.42 / p90 3.40 / p99 3.56 / sd
0.40 against TCP p50 3.63 / p90 4.05 / p99 4.69 / sd 0.36. **UDP wins at every
percentile, including the tail.** The Wi-Fi tail result was a property of the radio,
where a lost datagram costs a full client timeout and TCP fast retransmits; over 600
wired UDP samples that never fired.

**The default did not change; its justification did.** TCP is the default for
configurability: a UDP SLMP entry on iQ-F is point-to-point, GX Works3 will not save one
without a destination IP, there are at most eight entries on the CPU, and a TCP entry
serves any peer. Loss is also silent on UDP. Where an entry exists for your host and the
link is wired, UDP is the faster choice and you should take it deliberately. Run this
script on *your* link before quoting either table.

**Why this script would not run at all until 2026-09-07.** Each table is control, client,
control, and on TCP all three want the same connection entry. The CPU serves one TCP
connection per entry and releases it as it processes the FIN, so a connect issued
microseconds after the previous close arrives before the entry is free and raises
``SlmpConnectionEntryBusyError`` with nothing else connected. Measured wired at a median
RTT of 3.64 ms: 1/6 reconnects worked at a 0 ms gap, 2/6 at 1 ms, 6/6 from 2 ms out to
200 ms. It is a race against FIN processing, not a hold period -- over Wi-Fi at ~7 ms RTT
it never reproduced, because the link latency already covers the window. Every seam here
therefore takes ``aslmp.tools.bench.ENTRY_RELEASE_SETTLE_S`` (5 ms), and nothing retries.

Nothing here writes to the PLC.
"""

from __future__ import annotations

import argparse
import asyncio

# ``_report`` is the sibling file, and a script's own directory is sys.path[0].
# Run these as ``python bench/<name>.py``; there is no package here to install.
from _report import Row, preamble, report

from aslmp.client import Plc
from aslmp.tools.bench import (
    Distribution,
    raw_control,
    settle_after_release,
    timed_samples,
)
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
    if not udp:
        # The client has just released the entry and the trailing control takes it back.
        # `raw_control` settles on its own way out, so the leading control -> client seam
        # is already covered; this is the other one. See ENTRY_RELEASE_SETTLE_S.
        settle_after_release()
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
        "TCP is the library's default for CONFIGURABILITY, not speed: a UDP SLMP entry "
        "on iQ-F is point-to-point and there are at most eight entries on the CPU, "
        "while a TCP entry serves any peer, and loss is silent on UDP. On the wired "
        "retest (2026-09-07) UDP was faster at every percentile; on Wi-Fi (2026-09-06) "
        "TCP won the tail. Which of those two shapes you just measured is a property of "
        "YOUR link, so read the whole distribution above -- and write down the link "
        "before quoting any of it."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
