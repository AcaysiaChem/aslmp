"""The ``aslmp`` command line. Layer 9.

**One console script, not fourteen.** DESIGN section 1.16 puts every tool behind a
single entry point, because a library that installs ``aslmp-probe``, ``aslmp-identify``,
``aslmp-read`` and nine more siblings on a plant PC's ``PATH`` is a library that will be
uninstalled by whoever maintains that PC.

**Every subcommand is imported lazily and that is a tested property.** This module and
:mod:`aslmp.tools.__main__` are the only ones that run for ``aslmp --help``, and neither
imports ``asyncio``, ``socket`` or anything above layer 1. The help text below is a
table of strings for exactly that reason: an ``argparse`` subparser tree would have to
import all eleven modules -- and therefore the client, the transport and the simulator --
to render one screen of text.

Each subcommand module exports ``run(argv) -> int`` and ``build_parser() ->
ArgumentParser``. ``run`` returns a process exit status and never calls ``sys.exit``, so
the subcommands are testable in-process: ``tests/unit/test_tools.py`` calls them
directly.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

__all__ = ["EXIT_FAILURE", "EXIT_OK", "EXIT_USAGE", "SUBCOMMANDS", "Subcommand"]

EXIT_OK: Final = 0
"""The command did what it was asked."""

EXIT_FAILURE: Final = 1
"""The PLC, the network or the profile said no. The diagnostic is on stderr."""

EXIT_USAGE: Final = 2
"""The arguments were wrong. Nothing was sent."""


@dataclass(frozen=True, slots=True)
class Subcommand:
    """One row of the table: what a subcommand is called and what it does."""

    name: str
    module: str
    summary: str
    needs_simulator: bool = False


def _row(name: str, summary: str, *, needs_simulator: bool = False) -> Subcommand:
    return Subcommand(
        name=name,
        module=f"aslmp.tools.{name.replace('-', '_')}",
        summary=summary,
        needs_simulator=needs_simulator,
    )


SUBCOMMANDS: Final[Mapping[str, Subcommand]] = MappingProxyType(
    {
        row.name: row
        for row in (
            _row("probe", "prove a connection entry is live, and say what that proves"),
            _row("status", "what state the CPU is in, and whether it reports an error"),
            _row("identify", "ask a CPU what it is and print the profile key to pass"),
            _row("read", "read one address, or an array, and print the value"),
            _row("write", "write one address, optionally verifying the read-back"),
            _row("cite", "print the manual sections behind a command or an end code"),
            _row("capabilities", "what a profile can do, with the evidence for each claim"),
            _row("ambiguities", "where the sources disagree, what we chose, and the probe"),
            _row("verify-ranges", "measure a CPU's real device ranges against the profile"),
            _row("proxy", "forward SLMP unconditionally and decode a copy in flight"),
            _row("bench", "latency distributions beside a same-session raw-socket control"),
            _row("serve", "run the conformance simulator", needs_simulator=True),
        )
    }
)
"""Every subcommand, in the order ``aslmp --help`` prints them.

Ordered by how often a new user reaches for them rather than alphabetically: the first
thing you do with a PLC you have never met is ``probe`` it, and the last is ``serve`` a
simulator of it.
"""
