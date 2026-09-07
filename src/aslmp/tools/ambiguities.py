"""``aslmp ambiguities`` -- where the sources disagree, what we chose, and the probe.

Every SLMP implementation makes these choices. Most of them make the choice in a
comment, or in nothing at all. This library makes it a shipped record with five fields:
the question, at least two mutually exclusive readings, what this library actually does
(which may be "refuse"), why, and **the one experiment that would settle it**.

The reason it is a record rather than a comment is that the honest answer to "why does
your library send ``00 00`` there?" is a table a Mitsubishi engineer can read and
disagree with. The probe field is the part that matters: an ambiguity with no stated
experiment is an opinion.

Ambiguities live in two places and this command reads both: on a
:class:`~aslmp.profile.CpuProfile`, where they are properties of a CPU family, and on a
command class, where they are properties of a frame this package builds.

Opens no socket.
"""

from __future__ import annotations

import argparse
import textwrap
from collections.abc import Iterable, Sequence

from aslmp.tools import EXIT_FAILURE, EXIT_OK
from aslmp.tools._common import parse_or_exit, warn
from aslmp.wire.citations import Ambiguity

__all__ = ["build_parser", "collect", "run"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aslmp ambiguities",
        description=(
            "Print every place where the Mitsubishi documents contradict each other or "
            "the hardware, what aslmp chose, and the probe that would settle it."
        ),
        epilog=(
            "A-IQF-XY was open in the locked design and is now CLOSED by measurement: "
            "the wire device number is the linear index, so Y20 (octal) is 0x10. It is "
            "kept here with the measurement attached, because the reading it rules out "
            "is still what several other libraries implement."
        ),
    )
    parser.add_argument(
        "--profile",
        metavar="KEY",
        default=None,
        help="only this profile's ambiguities (default: every profile and every command)",
    )
    parser.add_argument(
        "--key", metavar="A-XXX", default=None, help="only the ambiguity with this key"
    )
    parser.add_argument(
        "--keys-only", action="store_true", help="just the keys, one per line"
    )
    return parser


def collect(profile_key: str | None = None) -> tuple[Ambiguity, ...]:
    """Every ambiguity this package ships, de-duplicated, in a stable order.

    The same record is reachable from a profile and from a command -- ``A-IQF-XY``
    belongs to both the iQ-F profile and the batch-read command -- and is printed once.
    Frozen dataclasses make identity a value comparison, so de-duplication is exact
    rather than by key.
    """
    from aslmp.commands.registry import COMMANDS
    from aslmp.profiles import ALL, by_key

    found: list[Ambiguity] = []

    def add(items: Iterable[Ambiguity]) -> None:
        for item in items:
            if item not in found:
                found.append(item)

    profiles = [by_key(profile_key)] if profile_key else list(ALL.values())
    for profile in profiles:
        add(profile.ambiguities)
    if profile_key is None:
        for spec in COMMANDS.values():
            add(spec.ambiguities)
    return tuple(found)


def _render(ambiguity: Ambiguity) -> str:
    wrap = textwrap.TextWrapper(width=92, initial_indent="      ", subsequent_indent="      ")
    lines = [f"{ambiguity.key}", *wrap.wrap(ambiguity.question), "", "  readings:"]
    for index, reading in enumerate(ambiguity.readings, start=1):
        lines.extend(
            textwrap.wrap(
                reading, width=92, initial_indent=f"    {index}. ", subsequent_indent="       "
            )
        )
    lines.append("")
    lines.append("  aslmp does:")
    lines.extend(wrap.wrap(ambiguity.chosen))
    lines.append("  because:")
    lines.extend(wrap.wrap(ambiguity.reason))
    lines.append("  probe:")
    lines.extend(wrap.wrap(ambiguity.probe))
    return "\n".join(lines)


def run(argv: Sequence[str]) -> int:
    args = parse_or_exit(build_parser(), argv)
    items = collect(args.profile)
    if args.key is not None:
        items = tuple(item for item in items if item.key == args.key)
        if not items:
            warn(
                f"no ambiguity is keyed {args.key!r}. Run `aslmp ambiguities --keys-only` "
                f"for the ones that exist."
            )
            return EXIT_FAILURE
    if args.keys_only:
        for item in items:
            print(item.key)
        return EXIT_OK
    for item in items:
        print(_render(item))
        print()
    print(f"{len(items)} ambiguity record(s). Each one is a question this library answers")
    print("by choosing, not by knowing. The probe column is how you would find out.")
    return EXIT_OK
