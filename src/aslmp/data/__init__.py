"""Checked-in, diffable sources of truth, and the strict reader for them.

The tables in this directory are the only place a device code, an end code, a point
limit or a device range is written down. Python that needs one of those facts at
import time is **generated** from a table here and committed
(``tools/gen_devicetable.py``, ``tools/gen_endcodes.py``); nothing in ``aslmp.wire``
or ``aslmp.errors`` parses a TSV at run time, so the tables cost the wheel a few
kilobytes and cost a control loop nothing.

Every row carries the same eleven-column provenance tail:

``provenance``
    ``live`` | ``manual`` | ``inferred`` (:class:`aslmp.wire.citations.Provenance`).
``cpu``, ``firmware``, ``measured``
    Required when ``provenance`` is ``live``, blank otherwise. A measured fact that
    does not name the silicon and the firmware it was measured on is a rumour.
``host``, ``medium``, ``samples``
    **Optional, and only on a ``live`` row.** Where the client ran, what it ran over,
    and the ``n`` behind the number. They are optional because most rows here are not
    timings and a device code answers the same over any medium; they exist because the
    one row-shaped claim this project had to withdraw in public — "TCP wins the latency
    tail" — was measured correctly and generalised past the link it came from. A row
    fills them in when they could have changed the answer, and leaves them empty
    rather than inventing them. See :class:`aslmp.wire.citations.Measurement`.

    The column is ``medium`` and not ``link`` because ``limits.tsv`` already spends
    ``link`` on the CPU-port-versus-ENET-module distinction, and a tail column that
    collided with a table's own would be silently overwritten by :func:`read_table`'s
    ``zip``. :data:`TABLES` is checked for that collision in ``test_citations.py``.
``manual``, ``revision``, ``section``
    A row in ``manuals.tsv``, its revision, and where in it. Required unless the row
    is purely a measurement.
``note``
    Free text: what a reader should notice. This is where a manual's disagreement
    with the hardware is written down in prose, next to the ambiguity key.

Format rules, enforced by :func:`read_table`: tab separated, no quoting, no escapes;
``#`` at column 0 is a comment; blank lines are ignored; the first non-comment line is
the header and must equal the declared schema exactly, in order; every data line must
have exactly as many fields as the header. An empty cell means "not applicable" and
never means "unknown" — that is what ``note`` is for.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

__all__ = [
    "DATA_DIR",
    "PROVENANCE_TAIL",
    "TABLES",
    "Row",
    "read_table",
    "table_path",
]

Row = Mapping[str, str]

DATA_DIR: Final[Path] = Path(__file__).resolve().parent

PROVENANCE_TAIL: Final[tuple[str, ...]] = (
    "provenance",
    "cpu",
    "firmware",
    "measured",
    "host",
    "medium",
    "samples",
    "manual",
    "revision",
    "section",
    "note",
)

_MANUALS: Final[tuple[str, ...]] = (
    "manual",
    "revision",
    "title",
    "pages",
    "date",
    "status",
    "url",
    "note",
)

_DEVICES: Final[tuple[str, ...]] = (
    "name",
    "long_name",
    "unit",
    "words_per_point",
    "ascii2",
    "ascii4",
    "code_short",
    "code_long",
    "radix",
    "min_spec",
    "batch_ok",
    "random_ok",
    "monitor_ok",
    "block_ok",
    *PROVENANCE_TAIL,
)

_END_CODES: Final[tuple[str, ...]] = (
    "code",
    "name",
    "exception_class",
    "description",
    "likely_cause",
    "caller_action",
    *PROVENANCE_TAIL,
)

_LIMITS: Final[tuple[str, ...]] = (
    "profile_key",
    "command",
    "coding",
    "unit",
    "link",
    "rule",
    "end_code",
    *PROVENANCE_TAIL,
)

_MODEL_CODES: Final[tuple[str, ...]] = (
    "code",
    "model",
    "family",
    "profile_key",
    *PROVENANCE_TAIL,
)

_RANGES: Final[tuple[str, ...]] = (
    "profile_key",
    "device",
    "present",
    "first",
    "last",
    "points",
    "radix",
    "notation",
    "configurable",
    *PROVENANCE_TAIL,
)

_AMBIGUITIES: Final[tuple[str, ...]] = (
    "key",
    "question",
    "readings",
    "chosen",
    "reason",
    "probe",
    "status",
    *PROVENANCE_TAIL,
)

TABLES: Final[Mapping[str, tuple[str, ...]]] = {
    "ambiguities": _AMBIGUITIES,
    "devices": _DEVICES,
    "end_codes": _END_CODES,
    "limits": _LIMITS,
    "manuals": _MANUALS,
    "model_codes": _MODEL_CODES,
    "ranges_iqf": _RANGES,
    "ranges_iqr": _RANGES,
}
"""Table name -> the exact header the file must carry, in order."""


def table_path(name: str) -> Path:
    """Return the path of the shipped ``<name>.tsv``, or raise for an unknown table."""
    if name not in TABLES:
        known = ", ".join(sorted(TABLES))
        raise KeyError(f"no such aslmp data table: {name!r}; known tables are {known}")
    return DATA_DIR / f"{name}.tsv"


def read_table(name: str) -> tuple[Row, ...]:
    """Parse ``<name>.tsv`` into rows, or raise naming the file and line.

    The parse is strict on purpose. A row that has lost a column, or a header that has
    drifted from :data:`TABLES`, is a data-integrity failure in the one place this
    library keeps its facts; recovering from it quietly would mean shipping a device
    code or a point limit that nobody wrote down.
    """
    path = table_path(name)
    schema = TABLES[name]
    text = path.read_text(encoding="ascii")
    rows: list[Row] = []
    header: Sequence[str] | None = None
    for lineno, raw in enumerate(text.splitlines(), start=1):
        if not raw or raw.startswith("#"):
            continue
        fields = raw.split("\t")
        if header is None:
            header = fields
            if tuple(header) != schema:
                raise ValueError(
                    f"{path}:{lineno}: header does not match the declared schema for "
                    f"{name!r}\n  file:   {fields}\n  schema: {list(schema)}"
                )
            continue
        if len(fields) != len(schema):
            raise ValueError(
                f"{path}:{lineno}: expected {len(schema)} tab-separated fields, "
                f"got {len(fields)}"
            )
        rows.append(dict(zip(schema, fields, strict=True)))
    if header is None:
        raise ValueError(f"{path}: no header line found")
    if not rows:
        raise ValueError(f"{path}: table is empty; an empty source of truth is a bug")
    return tuple(rows)
