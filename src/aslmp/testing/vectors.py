"""Golden byte vectors: the independent oracle, as typed rows rather than dicts.

Layer 2.5 (``aslmp.testing``). Pure: no sockets, no asyncio.

The simulator and the client share :mod:`aslmp.wire.codec` and the generated device
table. That is the one place a bug could hide from every client-against-server test in
the suite, it is named as a residual risk in DESIGN section 7, and **the golden-vector
corpus is its only mitigation**: a byte sequence printed in a Mitsubishi manual or
captured off a wire owes nothing to either side of this library.

So the corpus needs to be as easy to read as it is to write. A JSONL file of dicts is
easy to write and awful to consume -- ``row["hex"]`` and ``row.get("serial")`` and a
``KeyError`` three tests later -- so this module gives it a shape:

* :class:`Vector` carries the bytes, the provenance and the note, with ``bytes`` already
  unhexed and ``provenance`` already an enum;
* :meth:`Corpus.manual` and :meth:`Corpus.hardware` split the two kinds, because a
  disagreement with a printed page and a disagreement with our own silicon are different
  news;
* :meth:`Corpus.excluding` carries the **exclusion as data**. The 3E binary ``X``/``Y``
  examples in JY997D56001 are excluded from the corpus by DESIGN section 0.3, and an
  exclusion that lives in a comment is an exclusion nobody can audit.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aslmp.wire.citations import Provenance

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable, Iterable, Iterator, Mapping

__all__ = ["Corpus", "Vector", "load_corpus", "load_vectors", "merge"]


@dataclass(frozen=True, slots=True)
class Vector:
    """One golden row: some bytes, where they came from, and what they mean."""

    key: str
    data: bytes
    provenance: Provenance
    meaning: str
    source: str
    note: str
    fields: Mapping[str, Any]

    @property
    def hex(self) -> str:
        """The bytes as a manual prints them."""
        return self.data.hex(" ").upper()

    @property
    def measured(self) -> bool:
        """Whether this row came off a wire rather than out of a page."""
        return self.provenance is Provenance.LIVE

    def get(self, name: str, default: Any = None) -> Any:
        """One of the row's own extra fields, whatever the corpus put there."""
        return self.fields.get(name, default)

    def __str__(self) -> str:
        return f"{self.key} [{self.provenance.value}] {self.meaning}"


_PROVENANCE: Mapping[str, Provenance] = {
    "live": Provenance.LIVE,
    "manual": Provenance.MANUAL,
    "inferred": Provenance.INFERRED,
}


def _provenance_of(raw: object) -> Provenance:
    if isinstance(raw, str) and raw.lower() in _PROVENANCE:
        return _PROVENANCE[raw.lower()]
    raise ValueError(
        f"vector provenance {raw!r} is not one of {sorted(_PROVENANCE)}. A row whose "
        f"provenance is unknown is a row nobody can weigh: a printed example and a "
        f"capture off our own silicon are different kinds of evidence and the three "
        f"places they disagree are exactly what this corpus is for."
    )


def _vector_from(row: Mapping[str, Any], *, index: int, path: Path) -> Vector:
    key = str(row.get("id") or row.get("key") or f"{path.stem}:{index}")
    raw_hex = row.get("hex") or row.get("request") or ""
    if not isinstance(raw_hex, str):
        raise ValueError(
            f"{key}: the byte field must be a hex string, not "
            f"{type(raw_hex).__name__}"
        )
    try:
        data = bytes.fromhex(raw_hex)
    except ValueError as exc:
        raise ValueError(f"{key}: {raw_hex!r} is not hexadecimal") from exc
    manual = str(row.get("manual", ""))
    revision = str(row.get("revision", ""))
    section = str(row.get("section", ""))
    cpu = str(row.get("cpu", ""))
    firmware = str(row.get("firmware", ""))
    if cpu:
        source = f"{cpu} fw {firmware}".strip()
    else:
        source = " ".join(part for part in (manual, revision, section) if part)
    return Vector(
        key=key,
        data=data,
        provenance=_provenance_of(row.get("provenance", "manual")),
        meaning=str(row.get("meaning", "")),
        source=source,
        note=str(row.get("note", "")),
        fields=dict(row),
    )


def load_vectors(path: str | Path) -> tuple[Vector, ...]:
    """Every row of a JSONL corpus, typed. Blank lines and ``#`` comments are skipped."""
    file = Path(path)
    out: list[Vector] = []
    with file.open(encoding="utf-8") as handle:
        for index, line in enumerate(handle, start=1):
            text = line.strip()
            if not text or text.startswith("#"):
                continue
            try:
                row = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{file}:{index} is not JSON: {exc}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"{file}:{index} is a {type(row).__name__}, not an object")
            out.append(_vector_from(row, index=index, path=file))
    return tuple(out)


@dataclass(frozen=True, slots=True)
class Corpus:
    """A set of vectors, with the filters a test actually wants."""

    name: str
    vectors: tuple[Vector, ...]
    excluded: tuple[tuple[str, str], ...] = ()
    """``(key, why)`` for every row deliberately left out, as data rather than a comment."""

    def __iter__(self) -> Iterator[Vector]:
        return iter(self.vectors)

    def __len__(self) -> int:
        return len(self.vectors)

    def __getitem__(self, key: str) -> Vector:
        for vector in self.vectors:
            if vector.key == key:
                return vector
        raise KeyError(f"{key!r} is not in the {self.name} corpus")

    def manual(self) -> tuple[Vector, ...]:
        """Rows printed in a manual."""
        return tuple(v for v in self.vectors if v.provenance is Provenance.MANUAL)

    def hardware(self) -> tuple[Vector, ...]:
        """Rows captured off a wire."""
        return tuple(v for v in self.vectors if v.provenance is Provenance.LIVE)

    def where(self, predicate: Callable[[Vector], bool]) -> tuple[Vector, ...]:
        """Rows a test cares about."""
        return tuple(v for v in self.vectors if predicate(v))

    def excluding(self, keys: Mapping[str, str]) -> Corpus:
        """This corpus without ``keys``, recording why each was dropped.

        DESIGN section 0.3 excludes the 3E binary ``X``/``Y`` examples in JY997D56001
        from the corpus, because X and Y were never sent to the bench when the rule was
        written and the manual's printed example contradicts the reading this library
        ships. The exclusion is data so that ``aslmp ambiguities`` can print it and so
        that the day someone measures it, the row that comes back has a name.
        """
        missing = [key for key in keys if not any(v.key == key for v in self.vectors)]
        if missing:
            raise KeyError(
                f"cannot exclude {missing} from the {self.name} corpus: no such row(s). "
                f"An exclusion that names nothing is an exclusion that stopped working."
            )
        return Corpus(
            name=self.name,
            vectors=tuple(v for v in self.vectors if v.key not in keys),
            excluded=(*self.excluded, *sorted(keys.items())),
        )

    def __str__(self) -> str:
        return (
            f"{self.name}: {len(self.vectors)} vector(s) "
            f"({len(self.hardware())} measured), {len(self.excluded)} excluded"
        )


def load_corpus(path: str | Path, *, name: str = "") -> Corpus:
    """A named :class:`Corpus` from one JSONL file."""
    file = Path(path)
    return Corpus(name=name or file.stem, vectors=load_vectors(file))


def merge(name: str, corpora: Iterable[Corpus]) -> Corpus:
    """Several corpora as one, refusing duplicate keys.

    A duplicate key means two rows claim to be the same evidence, and a test that
    silently used the second one would be asserting against a row nobody meant.
    """
    seen: dict[str, str] = {}
    rows: list[Vector] = []
    excluded: list[tuple[str, str]] = []
    for corpus in corpora:
        for vector in corpus:
            if vector.key in seen:
                raise ValueError(
                    f"vector key {vector.key!r} appears in both {seen[vector.key]!r} and "
                    f"{corpus.name!r}"
                )
            seen[vector.key] = corpus.name
            rows.append(vector)
        excluded.extend(corpus.excluded)
    return Corpus(name=name, vectors=tuple(rows), excluded=tuple(excluded))
