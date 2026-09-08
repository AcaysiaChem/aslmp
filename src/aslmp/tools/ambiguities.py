"""``aslmp ambiguities`` -- where the sources disagree, what we chose, and the probe.

Every SLMP implementation makes these choices. Most of them make the choice in a
comment, or in nothing at all. This library makes it a shipped record with five fields:
the question, at least two mutually exclusive readings, what this library actually does
(which may be "refuse"), why, and **the one experiment that would settle it**.

The reason it is a record rather than a comment is that the honest answer to "why does
your library send ``00 00`` there?" is a table a Mitsubishi engineer can read and
disagree with. The probe field is the part that matters: an ambiguity with no stated
experiment is an opinion.

The shipped table ``aslmp/data/ambiguities.tsv`` is the catalogue, and this command
reads **all** of it. It used to walk the :class:`~aslmp.profile.CpuProfile` and
:class:`~aslmp.commands.registry.CommandSpec` objects instead, which are hand-written
Python carrying an :class:`~aslmp.wire.citations.Ambiguity` only where a profile or a
command needed to name one. The result was 30 rows in the file, 12 distinct keys
reachable from the command line, two of them printed twice, and ``--key
A-UDP-TAIL-LATENCY`` -- our own published retraction -- answering "no such ambiguity".
A record that ships in the wheel and cannot be printed is not a record.

The Python objects have not moved: a profile still carries the ones it needs, and
``tests/unit/test_profiles.py`` still holds them verbatim against this table. What
changed is which of the two the command line treats as the catalogue.

Opens no socket. Reads one file, at call time, never at import.
"""

from __future__ import annotations

import argparse
import textwrap
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from aslmp.tools import EXIT_FAILURE, EXIT_OK
from aslmp.tools._common import parse_or_exit, warn
from aslmp.wire.citations import Ambiguity

if TYPE_CHECKING:
    from aslmp.data import Row

__all__ = ["Record", "build_parser", "collect", "records", "run"]


@dataclass(frozen=True, slots=True)
class Record:
    """One row of ``ambiguities.tsv``: the record, its status, and its provenance tail.

    :class:`~aslmp.wire.citations.Ambiguity` deliberately carries neither -- it is the
    shape a profile and a command class embed, and neither of those should have to
    restate where a row was settled. The table carries both and a reader needs them:
    ``resolved_by_measurement`` on ``A-UDP-TAIL-LATENCY`` is the difference between an
    open question and a retraction we published.
    """

    ambiguity: Ambiguity
    status: str
    """``open`` | ``resolved_by_measurement`` | ``resolved_by_choice``."""
    provenance: str
    """``live`` | ``manual`` | ``inferred``, as the table spells it."""
    source: str
    """The provenance tail rendered for a human: the silicon, or the manual section."""


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
        help="only the rows this profile carries (default: every row of the table)",
    )
    parser.add_argument(
        "--key", metavar="A-XXX", default=None, help="only the ambiguity with this key"
    )
    parser.add_argument(
        "--keys-only", action="store_true", help="just the keys, one per line"
    )
    parser.add_argument(
        "--status",
        metavar="NAME",
        default=None,
        help="only rows with this status, e.g. open or resolved_by_measurement",
    )
    return parser


_RECORD_COLUMNS: Final = frozenset(
    {"key", "question", "readings", "chosen", "reason", "probe", "status"}
)
"""The columns of ``ambiguities.tsv`` that make the record itself, not its provenance."""

_SPELLED_OUT: Final = frozenset(
    {"provenance", "cpu", "firmware", "measured", "manual", "revision", "section", "note"}
)
"""The provenance columns :func:`_source` renders in prose rather than as ``name=value``."""


def _source(row: Row) -> str:
    """The provenance tail of one row as a single line a reader can act on.

    Every column is read with ``.get``, and every one this function does not spell out
    is printed as ``name=value`` under the name the table itself gives it. That is not
    laziness: the tail grows, and it grew *for this row*. ``host``, ``medium`` and
    ``samples`` were added after the ``A-UDP-TAIL-LATENCY`` measurement turned out to be
    a property of the link rather than of the protocol -- and ``medium`` was called
    ``link`` for part of a morning. A renderer that indexes a fixed list of column names
    either crashes on a table edit or, worse, quietly stops printing the column that was
    added because a number had been published without it.
    """
    parts: list[str] = []
    cpu = row.get("cpu", "")
    if cpu:
        firmware = row.get("firmware", "")
        measured = row.get("measured", "")
        head = f"{cpu} fw {firmware}" if firmware else cpu
        parts.append(f"{head}, {measured}" if measured else head)
    parts += [
        f"{name}={value}"
        for name, value in row.items()
        if value and name not in _RECORD_COLUMNS and name not in _SPELLED_OUT
    ]
    manual = row.get("manual", "")
    if manual:
        revision = row.get("revision", "")
        section = row.get("section", "")
        head = f"{manual}-{revision}" if revision else manual
        parts.append(f"{head} {section}".strip())
    return "; ".join(parts)


def records(profile_key: str | None = None) -> tuple[Record, ...]:
    """Every ambiguity this package ships, in the shipped table's own order.

    The table is the catalogue and its keys are unique -- ``tests/unit/test_citations.py``
    proves that -- so nothing needs de-duplicating and no row can be reached twice. That
    is also what stopped ``A-CLEAR-MODE`` and ``A-REMOTE-FIXED`` printing twice each: as
    a walk over profiles and commands, the same key arrived as two ``Ambiguity`` values
    whose text differed, and a de-duplication by value cannot merge those.

    ``profile_key`` narrows to the rows that profile's
    :class:`~aslmp.profile.CpuProfile` actually names. A key a profile names that the
    table does not hold raises here rather than being dropped: the two are copies of one
    fact and the whole reason the table ships is that they agree.
    """
    from aslmp.data import read_table

    rows = read_table("ambiguities")
    wanted: frozenset[str] | None = None
    if profile_key is not None:
        from aslmp.profiles import by_key

        wanted = frozenset(item.key for item in by_key(profile_key).ambiguities)
        missing = sorted(wanted - {row["key"] for row in rows})
        if missing:
            raise ValueError(
                f"profile {profile_key!r} names ambiguities that aslmp/data/"
                f"ambiguities.tsv does not hold: {', '.join(missing)}. The table is the "
                f"catalogue; a profile naming a row it does not contain means one of the "
                f"two was edited alone."
            )
    found: list[Record] = []
    for row in rows:
        if wanted is not None and row["key"] not in wanted:
            continue
        found.append(
            Record(
                ambiguity=Ambiguity(
                    key=row["key"],
                    question=row["question"],
                    readings=tuple(row["readings"].split("|")),
                    chosen=row["chosen"],
                    reason=row["reason"],
                    probe=row["probe"],
                ),
                status=row["status"],
                provenance=row["provenance"],
                source=_source(row),
            )
        )
    return tuple(found)


def collect(profile_key: str | None = None) -> tuple[Ambiguity, ...]:
    """Just the :class:`~aslmp.wire.citations.Ambiguity` of every row of the table."""
    return tuple(record.ambiguity for record in records(profile_key))


def _render(record: Record) -> str:
    wrap = textwrap.TextWrapper(width=92, initial_indent="      ", subsequent_indent="      ")
    ambiguity = record.ambiguity
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
    lines.append("  status:")
    status = f"{record.status} [{record.provenance}]"
    lines.extend(wrap.wrap(f"{status} -- {record.source}" if record.source else status))
    return "\n".join(lines)


def run(argv: Sequence[str]) -> int:
    args = parse_or_exit(build_parser(), argv)
    items = records(args.profile)
    if args.status is not None:
        known = sorted({record.status for record in records()})
        if args.status not in known:
            warn(
                f"no ambiguity has status {args.status!r}. The statuses in the shipped "
                f"table are: {', '.join(known)}."
            )
            return EXIT_FAILURE
        items = tuple(record for record in items if record.status == args.status)
    if args.key is not None:
        items = tuple(record for record in items if record.ambiguity.key == args.key)
        if not items:
            warn(
                f"no ambiguity is keyed {args.key!r}. Run `aslmp ambiguities --keys-only` "
                f"for the ones that exist."
            )
            return EXIT_FAILURE
    if args.keys_only:
        for record in items:
            print(record.ambiguity.key)
        return EXIT_OK
    for record in items:
        print(_render(record))
        print()
    print(f"{len(items)} ambiguity record(s). Each one is a question this library answers")
    print("by choosing, not by knowing. The probe column is how you would find out.")
    return EXIT_OK
