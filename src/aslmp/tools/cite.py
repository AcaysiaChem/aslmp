"""``aslmp cite`` -- print the manual sections behind a command or an end code.

The artifact you hand a Mitsubishi engineer. Every command in this package carries at
least one :class:`~aslmp.wire.citations.Citation` or
:class:`~aslmp.wire.citations.Measurement` (a test asserts it, and asserts that every
cited manual exists in ``data/manuals.tsv`` with its revision), and every end code row
carries the same. This command prints them, so "why does your library send that?" has an
answer you can open the page for rather than an answer you have to trust.

Where a measurement and a manual disagree, both are printed and the measurement is
marked, because the measurement is what the code implements.

Nothing here opens a socket. It is a query against committed Python literals.
"""

from __future__ import annotations

import argparse
import textwrap
from collections.abc import Sequence

from aslmp.tools import EXIT_FAILURE, EXIT_OK
from aslmp.tools._common import columns, parse_or_exit, usage, warn

__all__ = ["build_parser", "run"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aslmp cite",
        description=(
            "Print the sources behind an SLMP command or end code: which manual, which "
            "revision, which section, and which facts came from hardware instead."
        ),
        epilog=(
            "Codes may be written 0x0403, 403 or 0403. `aslmp cite --all` prints every "
            "command; `aslmp cite --end-code 0xC059` prints an end-code row."
        ),
    )
    parser.add_argument(
        "code", nargs="*", help="one or more command codes, e.g. 0x0403 0x1402"
    )
    parser.add_argument("--all", action="store_true", help="every command in the registry")
    parser.add_argument(
        "--end-code",
        action="append",
        default=[],
        metavar="CODE",
        help="an end code to explain, e.g. 0xC059 (repeatable)",
    )
    parser.add_argument(
        "--manuals", action="store_true", help="list every manual this package cites"
    )
    return parser


def _code(text: str) -> int:
    """``"0x0403"``, ``"403"`` and ``"0403"`` all mean 0x0403. Hexadecimal always.

    SLMP command and end codes are printed in hexadecimal in every Mitsubishi document
    and nowhere in decimal, so a bare ``403`` is read as hex rather than as 1027. There
    is no ambiguity to resolve and no decimal spelling to support.
    """
    cleaned = text.strip().lower().removeprefix("0x")
    if not cleaned or any(ch not in "0123456789abcdef" for ch in cleaned):
        raise ValueError(f"{text!r} is not a hexadecimal code")
    return int(cleaned, 16)


def _print_command(code: int) -> int:
    from aslmp.commands.registry import by_code

    spec = by_code(code)
    print(f"0x{spec.code:04X}  {spec.name}")
    print(
        columns(
            [
                ["  direction", spec.direction],
                ["  mutates", "yes -- a failure after send is an unknown outcome"
                 if spec.mutates else "no -- a failed read has no outcome to be unknown about"],
                [
                    "  implemented by",
                    ", ".join(cls.__qualname__ for cls in spec.commands)
                    or "(receive side only)",
                ],
            ]
        )
    )
    for source in spec.cites:
        # The note, not just the reference. Three measurements on the same CPU and the
        # same afternoon render identically without it, and a reader would reasonably
        # conclude the table had duplicated a row rather than recorded three facts.
        note = getattr(source, "note", "")
        print(f"  source  {source}")
        if note:
            for line in textwrap.wrap(note, width=88, initial_indent=" " * 10,
                                      subsequent_indent=" " * 10):
                print(line)
    for ambiguity in spec.ambiguities:
        print(f"  ambiguity  {ambiguity.key}: {ambiguity.question}")
        print(f"             chosen: {ambiguity.chosen}")
    print()
    return EXIT_OK


def _print_end_code(code: int) -> int:
    from aslmp.errors.endcodes import END_CODES

    info = END_CODES.get(code)
    if info is None:
        warn(
            f"0x{code:04X} is not in the end-code table. That is not the same as it "
            f"being impossible: aslmp raises SlmpEndCodeError with a synthesised row "
            f"for an undocumented code rather than a bare integer. Please report it "
            f"with the CPU model and firmware."
        )
        return EXIT_FAILURE
    print(f"0x{info.code:04X}  {info.name}")
    rows = [
        ["  raises", info.exception_class or "SlmpEndCodeError"],
        ["  means", info.description],
        ["  likely cause", info.likely_cause],
        ["  do", info.caller_action],
        ["  provenance", info.provenance.value],
    ]
    print(columns(rows))
    if info.measurement is not None:
        print(f"  measured  {info.measurement}")
    if info.citation is not None:
        print(f"  manual    {info.citation}")
    if info.note:
        print(f"  note      {info.note}")
    print()
    return EXIT_OK


def _print_manuals() -> int:
    from aslmp.data import read_table

    rows = [["manual", "revision", "title"]]
    for row in read_table("manuals"):
        rows.append([row["manual"], row.get("revision", ""), row.get("title", "")])
    print(columns(rows))
    return EXIT_OK


def run(argv: Sequence[str]) -> int:
    args = parse_or_exit(build_parser(), argv)
    if args.manuals:
        return _print_manuals()
    if not args.code and not args.all and not args.end_code:
        return usage("name a command code, or pass --all, --end-code or --manuals")

    failed = False
    try:
        wanted = [_code(text) for text in args.code]
        end_codes = [_code(text) for text in args.end_code]
    except ValueError as exc:
        return usage(str(exc))

    if args.all:
        from aslmp.commands.registry import codes

        wanted = list(codes())
    for code in wanted:
        try:
            _print_command(code)
        except KeyError as exc:
            # by_code raises KeyError with the full sentence; KeyError's repr quotes it.
            warn(exc.args[0] if exc.args else str(exc))
            failed = True
    for code in end_codes:
        if _print_end_code(code) != EXIT_OK:
            failed = True
    return EXIT_FAILURE if failed else EXIT_OK
