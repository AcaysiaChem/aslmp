"""``aslmp identify`` -- ask a CPU what it is, and print the profile key to pass.

This is the answer to the one required argument that cannot be defaulted.
:class:`~aslmp.client.Plc` demands ``profile=`` and there is no generic profile,
because ``X`` and ``Y`` are octal on an iQ-F and hexadecimal on an iQ-R: ``Y20`` is
output 16 on one and output 32 on the other, **and both CPUs answer end code
0x0000**. Nothing on the wire distinguishes a right answer from a wrong one, so the
library refuses to guess and this command exists to remove the guessing.

``0x0619`` Self Test and ``0x0101`` Read Type Name carry no device address at all, so
the profile used to send them cannot change a byte of the request. That is why
identifying does not require the answer it is looking for.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from aslmp.tools import EXIT_FAILURE, EXIT_OK
from aslmp.tools._common import (
    ENCODINGS,
    FRAMES,
    TRANSPORTS,
    columns,
    guarded,
    parse_or_exit,
    warn,
)

__all__ = ["build_parser", "run"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aslmp identify",
        description=(
            "Ask a CPU for its model code (0x0101) and print the profile key to pass to "
            "Plc(profile=...). Reads nothing else and changes nothing."
        ),
        epilog=(
            "The three connection facts still have to be right: a wrong data code, a "
            "wrong frame type or a wrong protocol all fail by SILENCE on this hardware, "
            "not by an error. If this times out with zero bytes, the first suspect is "
            "--encoding, not the cable."
        ),
    )
    parser.add_argument("host", help="the PLC's IP address or hostname")
    parser.add_argument("--port", type=int, default=5000, help="connection entry port")
    parser.add_argument("--transport", choices=TRANSPORTS, default="tcp")
    parser.add_argument("--frame", choices=FRAMES, default="3E")
    parser.add_argument("--encoding", choices=ENCODINGS, default="binary")
    parser.add_argument("--timeout", type=float, default=3.0, help="seconds (default: 3.0)")
    return parser


def run(argv: Sequence[str]) -> int:
    args = parse_or_exit(build_parser(), argv)

    async def body() -> int:
        from aslmp.client import Plc
        from aslmp.profile import Encoding
        from aslmp.profiles import claiming
        from aslmp.transport import TransportKind
        from aslmp.wire.frames import FrameType

        identity = await Plc.identify(
            args.host,
            args.port,
            encoding=Encoding(args.encoding),
            frame=FrameType(args.frame),
            transport=TransportKind(args.transport),
            timeout=args.timeout,
        )
        profiles = claiming(identity.model_code)
        if not profiles:  # pragma: no cover -- identify() raises before this
            warn(f"no shipped profile claims model code 0x{identity.model_code:04X}")
            return EXIT_FAILURE
        rows = [
            ["model", identity.model],
            ["model code", f"0x{identity.model_code:04X}"],
            ["family", identity.family.value],
            ["raw 0x0101 field", identity.raw.hex(" ").upper()],
            ["profile", profiles[0].key],
        ]
        print(columns(rows))
        if len(profiles) > 1:
            print(
                "\nMore than one shipped profile claims this model code. They differ in "
                "their device RANGE tables, not in their radix or their limits:"
            )
            print(columns([["  " + p.key, p.description] for p in profiles]))
            print(
                "Pick the one that matches the CPU's parameter file; `aslmp verify-ranges` "
                "measures which."
            )
        print(f"\n  Plc({args.host!r}, {args.port}, profile={profiles[0].key!r})")
        return EXIT_OK

    return guarded(body)
