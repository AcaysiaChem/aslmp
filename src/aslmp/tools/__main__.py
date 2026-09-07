"""``aslmp <subcommand> ...`` -- the dispatcher, and the only thing on ``PATH``.

Hand-rolled rather than ``argparse`` subparsers, for one measured reason: building a
subparser tree requires importing every subcommand module to ask it for its arguments,
and importing ``aslmp.tools.read`` imports ``aslmp.client``, which imports
``aslmp.transport``, which imports ``socket``. ``aslmp --help`` would then pay for the
event loop, the socket module and the simulator in order to print eleven lines of text.
``tests/unit/test_tools.py`` asserts in a subprocess that it does not.

The split is therefore: this module owns the subcommand *name*, and the subcommand
module owns everything after it. ``aslmp read --help`` imports exactly one subcommand.
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Sequence

from aslmp.tools import EXIT_OK, EXIT_USAGE, SUBCOMMANDS

__all__ = ["main"]

_USAGE = """\
usage: aslmp <command> [options]

An async SLMP client for Mitsubishi MELSEC PLCs.

commands:
{commands}

Every command takes --help. Reads are safe; `write` changes PLC memory, and no
subcommand can issue Remote RUN, STOP, PAUSE, LATCH CLEAR or RESET at all -- those
live behind Plc(allow_remote_control=True) in the library, never on a command line.

  aslmp identify 192.168.10.250          # what CPU is that, and which profile?
  aslmp probe 192.168.10.250 --profile melsec:iq-f/fx5u
  aslmp read 192.168.10.250 D0 --as f32 --profile melsec:iq-f/fx5u
"""


def _usage() -> str:
    width = max(len(name) for name in SUBCOMMANDS)
    rows = "\n".join(
        f"  {row.name.ljust(width)}  {row.summary}" for row in SUBCOMMANDS.values()
    )
    return _USAGE.format(commands=rows)


def _version() -> str:
    """Read the version without importing the package facade.

    ``aslmp._version`` is layer 0 and holds one string. Importing ``aslmp`` itself would
    work too -- its ``__getattr__`` is lazy -- but this keeps ``aslmp --version`` down to
    one module.
    """
    from aslmp._version import __version__

    return f"aslmp {__version__}"


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch one subcommand. Returns a process exit status; never calls ``sys.exit``.

    An unknown subcommand is a usage error naming the ones that exist. It is never
    guessed at, spell-corrected or matched by prefix: ``aslmp w`` is ambiguous between
    ``write`` and nothing else today and between ``write`` and ``watch`` tomorrow, and a
    prefix match that changes meaning when a command is added is a footgun in a shell
    script that has been running for a year.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help", "help"):
        sys.stdout.write(_usage())
        return EXIT_OK
    if args[0] in ("-V", "--version"):
        sys.stdout.write(_version() + "\n")
        return EXIT_OK

    name = args[0]
    row = SUBCOMMANDS.get(name)
    if row is None:
        sys.stderr.write(
            f"aslmp: unknown command {name!r}. Available: "
            f"{', '.join(SUBCOMMANDS)}.\nRun `aslmp --help`.\n"
        )
        return EXIT_USAGE

    try:
        module = importlib.import_module(row.module)
    except ImportError as exc:  # pragma: no cover -- a broken install, not a code path
        sys.stderr.write(
            f"aslmp {name}: could not import {row.module}: {exc}\n"
            f"This subcommand is part of the aslmp distribution; a failure here means "
            f"the installed wheel is incomplete.\n"
        )
        return EXIT_USAGE
    run = module.run
    result: int = run(args[1:])
    return result


if __name__ == "__main__":  # pragma: no cover -- exercised as a subprocess
    raise SystemExit(main())
