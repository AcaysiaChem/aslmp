"""Argument shapes and printing shared by the subcommands. Layer 9, private.

Two jobs.

**One spelling of the connection facts.** ``transport``, ``frame`` and ``encoding`` are
GX Works3 connection-entry settings, not things a client may probe for, so every
subcommand that opens a socket takes the same five flags with the same defaults as
:class:`aslmp.client.Plc`. Declaring them once means ``aslmp read --encoding ascii-xy-oct``
and ``aslmp bench --encoding ascii-xy-oct`` cannot drift.

**One rendering of a failure.** ``SlmpError.__str__`` is already the multi-line
diagnostic of DESIGN section 3.6 -- target, request, sent, received, routes, timing,
source, note. The command line's whole job is to print it unmangled and return a status,
never to summarise it into one line. A CLI that swallows that block is throwing away the
artifact that saves the afternoon.

Nothing here imports :mod:`aslmp.client`: the import happens inside
:func:`build_client`, so ``aslmp cite`` and ``aslmp ambiguities`` stay socket-free.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Coroutine, Sequence
from typing import TYPE_CHECKING, Any, Final, TypeVar

from aslmp.errors import SlmpError
from aslmp.profile import Encoding, Link
from aslmp.profiles import KEYS as PROFILE_KEYS
from aslmp.tools import EXIT_FAILURE, EXIT_OK, EXIT_USAGE

if TYPE_CHECKING:
    from aslmp.client import Plc

__all__ = [
    "ENCODINGS",
    "FRAMES",
    "LINKS",
    "TRANSPORTS",
    "add_connection_arguments",
    "build_client",
    "columns",
    "guarded",
    "hexdump",
    "ok",
    "parse_or_exit",
    "usage",
    "warn",
]

T = TypeVar("T")

ENCODINGS: Final[tuple[str, ...]] = tuple(member.value for member in Encoding)
"""``binary``, ``ascii-xy-oct``, ``ascii-xy-hex`` -- the CPU's own-node data code."""

FRAMES: Final[tuple[str, ...]] = ("3E", "4E")
"""The two frame formats this library speaks. Named as the manuals name them."""

TRANSPORTS: Final[tuple[str, ...]] = ("tcp", "udp")
"""Which socket. A connection-entry fact; never probed for."""

LINKS: Final[tuple[str, ...]] = tuple(member.value for member in Link)
"""``cpu`` or ``enet``: which limit table applies. The ``0403`` ceiling is 192 on the
built-in port and 123 through an FX5-ENET (JY997D56001-K p.77 footnote)."""


def add_connection_arguments(
    parser: argparse.ArgumentParser, *, require_profile: bool = True
) -> None:
    """Add ``host``, ``--port`` and the five connection-entry facts.

    ``--profile`` is required wherever a device address is involved, exactly as
    ``Plc(profile=...)`` is: ``Y20`` is output 16 on an FX5U and output 32 on an iQ-R,
    and both CPUs answer end code ``0x0000``. There is no generic profile to fall back
    to and the fallback is the bug.
    """
    parser.add_argument("host", help="the PLC's IP address or hostname")
    parser.add_argument(
        "--port", type=int, default=5000, help="the connection entry's port (default: 5000)"
    )
    if require_profile:
        parser.add_argument(
            "--profile",
            required=True,
            metavar="KEY",
            help=f"CPU profile; one of {', '.join(PROFILE_KEYS)}. `aslmp identify` prints it",
        )
    parser.add_argument(
        "--transport",
        choices=TRANSPORTS,
        default="tcp",
        help="the entry's protocol (default: tcp; TCP wins the latency tail)",
    )
    parser.add_argument(
        "--frame", choices=FRAMES, default="3E", help="the entry's frame format (default: 3E)"
    )
    parser.add_argument(
        "--encoding",
        choices=ENCODINGS,
        default="binary",
        help="the port-wide Communication Data Code (default: binary)",
    )
    parser.add_argument(
        "--link",
        choices=LINKS,
        default="cpu",
        help="cpu = built-in Ethernet port, enet = FX5-ENET module (default: cpu)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=3.0,
        help="client-side deadline in seconds (default: 3.0)",
    )
    parser.add_argument(
        "--capture-frames",
        action="store_true",
        help="keep request and response bytes on every transaction, for diagnostics",
    )


def build_client(args: argparse.Namespace, **overrides: Any) -> Plc:
    """Construct a :class:`~aslmp.client.Plc` from parsed arguments.

    The import of ``aslmp.client`` -- and therefore of ``asyncio`` and ``socket`` --
    happens here, on the first line of a subcommand that is about to open a socket, and
    never at ``aslmp --help``.
    """
    from aslmp.client import Plc
    from aslmp.transport import TransportKind
    from aslmp.wire.frames import FrameType

    kwargs: dict[str, Any] = {
        "port": args.port,
        "profile": args.profile,
        "transport": TransportKind(args.transport),
        "frame": FrameType(args.frame),
        "encoding": Encoding(args.encoding),
        "link": Link(args.link),
        "timeout": args.timeout,
        "connect_timeout": args.timeout,
        "capture_frames": args.capture_frames,
    }
    kwargs.update(overrides)
    return Plc(args.host, **kwargs)


def parse_or_exit(parser: argparse.ArgumentParser, argv: Sequence[str]) -> argparse.Namespace:
    """``parser.parse_args`` -- factored out so every subcommand parses identically."""
    return parser.parse_args(list(argv))


def warn(message: str) -> None:
    """One line on stderr. Never a ``logging`` call: DESIGN section 5.1.5 permits the
    ``logging`` module in ``observability.attach_logging`` and nowhere else."""
    sys.stderr.write(message.rstrip("\n") + "\n")


def guarded(work: Callable[[], Coroutine[Any, Any, int]]) -> int:
    """Run an async subcommand body, rendering an ``SlmpError`` the way it renders itself.

    ``KeyboardInterrupt`` returns 130 (128 + SIGINT) rather than dumping a traceback:
    interrupting a bench run is a normal way to end one.

    Nothing else is caught. An unexpected exception is a bug in this library and its
    traceback is the report we want.
    """
    import asyncio

    try:
        return asyncio.run(work())
    except SlmpError as exc:
        warn(f"\n{type(exc).__module__}.{type(exc).__name__}: {exc}")
        return EXIT_FAILURE
    except KeyboardInterrupt:  # pragma: no cover -- needs a real signal
        warn("interrupted")
        return 130


def columns(rows: Sequence[Sequence[str]], *, gap: str = "  ") -> str:
    """Left-aligned fixed-width columns. The last column is never padded.

    Deliberately not a Markdown table: this output is read in a terminal beside a GX
    Works3 window, and pasted into an email to a Mitsubishi engineer.
    """
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    padded = [list(row) + [""] * (width - len(row)) for row in rows]
    sizes = [max(len(row[i]) for row in padded) for i in range(width)]
    lines = []
    for row in padded:
        cells = [cell.ljust(sizes[i]) for i, cell in enumerate(row[:-1])]
        cells.append(row[-1])
        lines.append(gap.join(cells).rstrip())
    return "\n".join(lines)


def hexdump(data: bytes, *, limit: int = 64) -> str:
    """``50 00 00 FF FF 03 00 ...`` -- the form every Mitsubishi manual prints.

    Space-separated upper-case byte pairs, truncated with a count so a 1935-byte
    response does not fill a terminal.
    """
    shown = data[:limit]
    text = " ".join(f"{byte:02X}" for byte in shown)
    if len(data) > limit:
        return f"{text} ... ({len(data)} bytes)"
    return text


def ok(message: str = "") -> int:
    """Print ``message`` if there is one and return success."""
    if message:
        sys.stdout.write(message.rstrip("\n") + "\n")
    return EXIT_OK


def usage(message: str) -> int:
    """Print a usage complaint on stderr and return :data:`~aslmp.tools.EXIT_USAGE`."""
    warn(f"aslmp: {message}")
    return EXIT_USAGE
