"""``aslmp probe`` -- prove a connection entry is live, and say what that proves.

``socket.connect()`` lies on this hardware. A second TCP connection to a busy SLMP
connection entry completes its three-way handshake in 5.4 ms and is then closed by the
CPU, with the incumbent undisturbed (FX5U-32MT/DS fw 1.065, 2026-09-06). A connect that
returns without an error has demonstrated almost nothing.

So this command does what :meth:`aslmp.client.Plc.connect` does: a ``0x0619`` Self Test
with a per-generation nonce, compared **byte for byte** against the echo. That one round
trip -- ~7 ms, zero side effects, no device address anywhere in it -- simultaneously
proves that the connection entry was free, that the Communication Data Code matches,
that the frame type is accepted, that the route bytes are right, that the protocol is
right, and that the CPU is answering *now*. Its latency is also a real read's latency,
which is why the samples below are worth printing.

Everything it does is read-only. There is no flag here that reaches a Remote RUN.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from aslmp.tools import EXIT_OK
from aslmp.tools._common import (
    add_connection_arguments,
    build_client,
    columns,
    guarded,
    parse_or_exit,
)

__all__ = ["build_parser", "run"]

_PROVED = """
What the 0x0619 handshake above proves, in one round trip:
  * the connection entry was free -- a busy entry accepts the TCP connection and then
    FINs, so a clean handshake is the only evidence that this socket is really served;
  * the Communication Data Code matches -- ASCII into a binary entry is 0xC06F, which
    the FX5U reports as SILENCE, not as an end code;
  * the frame format is accepted, the route bytes are right, the protocol is right;
  * the CPU answered inside the deadline, just now.

What it does not prove: that any particular device exists, that the profile's ranges
match this CPU's parameter file (`aslmp verify-ranges` measures that), or that the entry
will still be free in a second.
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aslmp probe",
        description=(
            "Connect, prove liveness with a 0x0619 Self Test echo compare, identify the "
            "CPU with 0x0101, and time a handful of further self tests. Reads only."
        ),
    )
    add_connection_arguments(parser)
    parser.add_argument(
        "--samples",
        type=int,
        default=10,
        metavar="N",
        help="extra 0x0619 round trips to time after the handshake (default: 10)",
    )
    return parser


def run(argv: Sequence[str]) -> int:
    args = parse_or_exit(build_parser(), argv)
    if args.samples < 0:
        from aslmp.tools._common import usage

        return usage(f"--samples must not be negative; got {args.samples}")

    async def body() -> int:
        plc = build_client(args)
        async with plc:
            info = plc.info
            if info is None:  # pragma: no cover -- connect() raises instead
                raise RuntimeError("connected without a ConnectionInfo")
            rows = [
                ["peer", f"{info.peer[0]}:{info.peer[1]}"],
                ["local", f"{info.local[0]}:{info.local[1]}"],
                [
                    "entry",
                    f"{plc.transport.value} / {plc.encoding.value} / {plc.frame.value}",
                ],
                ["profile", plc.profile.key],
                ["connection", f"{info.connection_id} (generation {info.generation})"],
            ]
            if info.handshake is not None:
                shake = info.handshake
                rows.append(
                    [
                        "handshake",
                        f"0x{shake.command:04X} sub 0x{shake.subcommand:04X}, "
                        f"{shake.timing.wire_ms:.2f} ms wire, "
                        f"{len(shake.timing.chunks)} chunk(s)",
                    ]
                )
            else:
                rows.append(["handshake", "SKIPPED (Handshake.NONE): liveness unproven"])
            identity = plc.identity
            if identity is not None:
                rows.append(
                    ["cpu", f"{identity.model} (model code 0x{identity.model_code:04X})"]
                )
            print(columns(rows))

            if args.samples:
                samples = [await plc.ping() for _ in range(args.samples)]
                ordered = sorted(samples)
                print(
                    f"\n0x0619 round trips (n={len(samples)}): "
                    f"min {ordered[0]:.2f} ms, "
                    f"median {ordered[len(ordered) // 2]:.2f} ms, "
                    f"max {ordered[-1]:.2f} ms"
                )
                print(
                    "  A handful of samples is a smoke test, not a measurement. "
                    "`aslmp bench` prints a distribution beside a raw-socket control."
                )
            counters = plc.counters
            if counters.segmented_responses:
                print(
                    f"\n{counters.segmented_responses} response(s) arrived in more than "
                    f"one TCP segment. That is normal and is why the receive stamp is "
                    f"taken after the LAST chunk."
                )
        print(_PROVED, end="")
        return EXIT_OK

    return guarded(body)
