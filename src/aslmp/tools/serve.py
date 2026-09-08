"""``aslmp serve`` -- run the conformance simulator.

A PLC-shaped socket you can point anything at: this library's own test suite, another
vendor's client, a colleague's HMI, or ``aslmp probe`` itself. It is not a mock. It is
the behaviour of one measured CPU, including the parts nobody would design on purpose:
TCP request coalescing that answers only the LAST request with end code ``0x0000``, an
entry that accepts a second connection and immediately FINs it, silence instead of an
end code on a coding mismatch, and a request whose declared length is overstated hanging
forever.

Three targets, and the difference between them is a document:

``fx5u-32mt-ds``
    Our bench CPU, reproducing what we measured. The CI default.
``pedantic``
    The manuals with none of the bugs. Running a suite against this and against the FX5U
    and diffing the two IS the list of behaviours that exist only to accommodate real
    silicon.
``r04cpu``
    Manual-derived and reported as **unverified**: we have no iQ-R and every expectation
    in it is a reading, not an observation.

Simulator entries mirror our bench: five connection entries with distinct ports and
protocols. One of them is ASCII, which is a **deliberate departure from the iQ-F**, where
Communication Data Code is a port-wide own-node setting and binary and ASCII cannot
coexist -- and which is exactly why the simulator has to offer it.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from typing import TYPE_CHECKING

from aslmp.tools import EXIT_FAILURE, EXIT_OK
from aslmp.tools._common import columns, parse_or_exit, warn

if TYPE_CHECKING:
    from aslmp.testing import PlcSimulator

__all__ = ["build_parser", "run"]

_MISSING = """\
aslmp serve needs the conformance simulator, aslmp.testing, which is not importable in
this environment. It has no third-party dependencies, so this normally means the wheel
was built with the simulator excluded. Install it with:

    pip install 'aslmp[testing]'
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aslmp serve",
        description=(
            "Run the aslmp conformance simulator: a PLC-shaped socket that reproduces "
            "what an FX5U-32MT/DS on firmware 1.065 actually does, bugs included."
        ),
        epilog=(
            "Nothing here is a substitute for hardware, and it is not evidence about "
            "yours. The simulator shares this package's codec with the client, so a "
            "codec bug is invisible to every client-to-server test; the golden vectors "
            "are the external oracle. That is a named residual weakness, not a caveat."
        ),
    )
    parser.add_argument(
        "--target",
        default="fx5u-32mt-ds",
        help="which CPU to imitate: fx5u-32mt-ds, pedantic, r04cpu",
    )
    parser.add_argument(
        "--host", default="127.0.0.1", help="address to bind (default: 127.0.0.1)"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        metavar="N",
        help=(
            "fixed port for the plain TCP binary 3E entry (default: 0, an ephemeral "
            "port; whichever it lands on is printed in the address column below, and "
            "that line is flushed as soon as the socket is bound)"
        ),
    )
    parser.add_argument(
        "--healthy",
        action="store_true",
        help="drop the measured pathologies: no coalescing, no FIN, no silence",
    )
    parser.add_argument(
        "--transcript",
        action="store_true",
        help="print every frame as it is served",
    )
    return parser


async def _stream_transcript(simulator: PlcSimulator) -> None:  # pragma: no cover
    """Print each served frame as it appears, by polling the simulator's transcript.

    Polling rather than a callback because the transcript is the simulator's own record
    and this command must not change what it records. A tenth of a second is far below
    any human's reading rate and far above the simulator's own cost.
    """
    seen = 0
    while True:
        records = simulator.transcript
        for record in records[seen:]:
            print(record, flush=True)
        seen = len(records)
        await asyncio.sleep(0.1)


def run(argv: Sequence[str]) -> int:
    args = parse_or_exit(build_parser(), argv)
    try:
        from aslmp.testing import HEALTHY, PlcSimulator, by_key
        from aslmp.testing.server import BENCH_ENTRIES, Entry
    except ImportError:  # pragma: no cover -- needs a wheel built without the simulator
        warn(_MISSING)
        return EXIT_FAILURE

    try:
        target = by_key(args.target)
    except KeyError as exc:
        warn(exc.args[0] if exc.args else str(exc))
        return EXIT_FAILURE

    entries = tuple(
        Entry(
            name=entry.name,
            protocol=entry.protocol,
            port=args.port if (entry.name == "tcp" and args.port) else entry.port,
            encoding=entry.encoding,
            frame=entry.frame,
            max_connections=entry.max_connections,
        )
        for entry in BENCH_ENTRIES
    )

    async def body() -> int:
        simulator = PlcSimulator(
            target=target,
            entries=entries,
            pathology=HEALTHY if args.healthy else None,
            host=args.host,
        )
        async with simulator:
            rows: list[list[str]] = [["entry", "address", "protocol", "frame", "encoding"]]
            for entry in simulator.entries:
                host, port = simulator.address(entry.name)
                rows.append(
                    [
                        entry.name,
                        f"{host}:{port}",
                        entry.protocol,
                        entry.frame.value,
                        entry.encoding.value,
                    ]
                )
            banner = [f"{target.label}  (model code 0x{target.model_code:04X})"]
            if not target.verified:
                banner.append(f"  {target.warn_if_unverified()}")
            banner.append(columns(rows))
            banner.append(
                "\nPathologies: "
                + ("OFF (--healthy)" if args.healthy else "the measured FX5U set")
            )
            banner.append("Ctrl-C to stop.")
            # Flushed, not printed and left in the buffer. The default --port is 0, so
            # the banner's address column is the ONLY place the ephemeral port is
            # written down -- and the obvious way a harness uses this command is to
            # background it with stdout on a pipe or a file, where Python's block
            # buffering holds 8 KB of banner until the process exits. That left the log
            # empty and the port unknowable for as long as the simulator was useful.
            print("\n".join(banner), flush=True)
            if args.transcript:
                await _stream_transcript(simulator)
            else:
                await asyncio.Event().wait()
        return EXIT_OK

    try:
        return asyncio.run(body())
    except KeyboardInterrupt:  # pragma: no cover -- needs a real signal
        print("\nsimulator stopped", flush=True)
        return EXIT_OK
