"""Provenance value types: how this library says where a fact came from.

Layer 0. **stdlib only.** Nothing here does I/O, and importing this module must not
pull ``socket``, ``ssl``, ``asyncio``, ``selectors``, ``threading`` or ``logging``
into ``sys.modules`` (``tests/unit/test_layering.py`` proves it in a subprocess).

Every constant table row, every frame builder and every limit in ``aslmp`` carries one
of these. A :class:`Citation` names a Mitsubishi document, its revision and the section
a reader can open. A :class:`Measurement` names the CPU model and firmware the fact was
observed on, because where a manual and the silicon disagree the silicon wins and the
code must say so. An :class:`Ambiguity` is the third case: the sources contradict each
other, we had to choose, and the choice plus the probe that would settle it are data
rather than a comment.

The types validate at construction and raise. There is no "unknown manual" default and
no empty-string sentinel: a fact with no provenance cannot be represented.
"""

from __future__ import annotations

import datetime
import enum
import re
from dataclasses import dataclass
from typing import TypeAlias

__all__ = ["Ambiguity", "Citation", "Measurement", "Provenance", "Source"]


class Provenance(enum.Enum):
    """How a shipped fact was established.

    ``LIVE``
        Observed on real hardware. Requires a :class:`Measurement`.
    ``MANUAL``
        Read out of a Mitsubishi document at a named section.
    ``INFERRED``
        Neither: carried across from a related figure, or from the locked design, and
        not independently located in the sources we read. Ships labelled so that
        ``aslmp ambiguities`` and ``aslmp cite`` can tell a user not to bet on it.
    """

    LIVE = "live"
    MANUAL = "manual"
    INFERRED = "inferred"


_AMBIGUITY_KEY = re.compile(r"\AA-[A-Z0-9]+(?:-[A-Z0-9]+)*\Z")
_ISO_DATE = re.compile(r"\A[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")


def _text(value: object, owner: str, field: str) -> str:
    """Return ``value`` as a non-blank ``str``, or raise naming the field."""
    if not isinstance(value, str):
        raise TypeError(f"{owner}.{field} must be a str, not {type(value).__name__}")
    if not value.strip():
        raise ValueError(f"{owner}.{field} must not be blank")
    return value


def _optional_text(value: object, owner: str, field: str) -> str:
    """Return ``value`` as a ``str`` that may be empty, or raise naming the field."""
    if not isinstance(value, str):
        raise TypeError(f"{owner}.{field} must be a str, not {type(value).__name__}")
    return value


@dataclass(frozen=True, slots=True)
class Citation:
    """A pointer into a Mitsubishi document, precise enough to open the page.

    ``manual``
        The document number as printed, e.g. ``"JY997D56001"`` or ``"SH(NA)-080956ENG"``.
    ``revision``
        The revision letter or group, e.g. ``"K"``, ``"AB"``. Never blank: two revisions
        of JY997D56001 disagree about the FX5 X/Y examples, so a citation without a
        revision cannot be checked.
    ``section``
        Where in that revision, in the document's own numbering, e.g. ``"§5.2 p.35"``.
    ``note``
        Optional: what the reader should notice when they get there.
    ``provenance``
        ``MANUAL`` (default) when the figure is printed at that section, or ``INFERRED``
        when it was carried from elsewhere and could not be located there. ``LIVE`` is
        rejected: a measured fact is a :class:`Measurement`, not a citation.
    """

    manual: str
    revision: str
    section: str
    note: str = ""
    provenance: Provenance = Provenance.MANUAL

    def __post_init__(self) -> None:
        _text(self.manual, "Citation", "manual")
        _text(self.revision, "Citation", "revision")
        _text(self.section, "Citation", "section")
        _optional_text(self.note, "Citation", "note")
        if not isinstance(self.provenance, Provenance):
            raise TypeError("Citation.provenance must be a Provenance")
        if self.provenance is Provenance.LIVE:
            raise ValueError(
                "Citation.provenance may not be LIVE; a measured fact is a Measurement, "
                "which names the CPU model and firmware it was observed on"
            )

    @property
    def reference(self) -> str:
        """``"JY997D56001-K §4.2 p.69"`` — the string to type into a search box."""
        return f"{self.manual}-{self.revision} {self.section}"

    def __str__(self) -> str:
        if self.provenance is Provenance.INFERRED:
            return f"{self.reference} (inferred, not located at that section)"
        return self.reference


@dataclass(frozen=True, slots=True)
class Measurement:
    """A fact observed on real hardware, naming the hardware.

    A measurement outranks a manual everywhere in this library, so it has to say
    which silicon it came from: ``FX5U-32MT/DS`` firmware ``1.065`` is not evidence
    about an R04CPU, and a firmware update can invalidate every row that names it.
    """

    cpu: str
    firmware: str
    date: str
    note: str = ""

    def __post_init__(self) -> None:
        _text(self.cpu, "Measurement", "cpu")
        _text(self.firmware, "Measurement", "firmware")
        _text(self.date, "Measurement", "date")
        _optional_text(self.note, "Measurement", "note")
        if _ISO_DATE.match(self.date) is None:
            raise ValueError(
                f"Measurement.date must be an ISO YYYY-MM-DD date; got {self.date!r}"
            )
        try:
            datetime.date.fromisoformat(self.date)
        except ValueError as exc:
            raise ValueError(
                f"Measurement.date is not a real calendar date: {self.date!r}"
            ) from exc

    @property
    def provenance(self) -> Provenance:
        """Always :attr:`Provenance.LIVE`. That is what this type means."""
        return Provenance.LIVE

    @property
    def reference(self) -> str:
        """``"FX5U-32MT/DS fw 1.065, 2026-09-06"`` — quotable in an exception."""
        return f"{self.cpu} fw {self.firmware}, {self.date}"

    def __str__(self) -> str:
        return self.reference


@dataclass(frozen=True, slots=True)
class Ambiguity:
    """A place where the sources contradict each other or the hardware.

    This is a shipped, enumerable record rather than a comment, because the honest
    answer to "why does your library send ``00 00`` there?" is a table a Mitsubishi
    engineer can read: the question, both readings, what we chose, why, and the one
    experiment that would settle it. ``aslmp ambiguities`` prints them.

    ``readings`` holds at least two mutually exclusive answers; ``chosen`` says what
    this library actually does, which may be "refuse" rather than either reading.
    """

    key: str
    question: str
    readings: tuple[str, ...]
    chosen: str
    reason: str
    probe: str

    def __post_init__(self) -> None:
        _text(self.key, "Ambiguity", "key")
        if _AMBIGUITY_KEY.match(self.key) is None:
            raise ValueError(
                f"Ambiguity.key must look like 'A-IQF-XY' (A- then upper-case "
                f"hyphen-separated words); got {self.key!r}"
            )
        _text(self.question, "Ambiguity", "question")
        _text(self.chosen, "Ambiguity", "chosen")
        _text(self.reason, "Ambiguity", "reason")
        _text(self.probe, "Ambiguity", "probe")
        if not isinstance(self.readings, tuple):
            raise TypeError(
                f"Ambiguity.readings must be a tuple, not "
                f"{type(self.readings).__name__}; a list would make the record mutable"
            )
        if len(self.readings) < 2:
            raise ValueError(
                f"Ambiguity {self.key!r} needs at least two readings; one reading is "
                f"not an ambiguity"
            )
        for index, reading in enumerate(self.readings):
            _text(reading, "Ambiguity", f"readings[{index}]")
        if len(set(self.readings)) != len(self.readings):
            raise ValueError(f"Ambiguity {self.key!r} has duplicate readings")

    def __str__(self) -> str:
        return f"{self.key}: {self.question} -> {self.chosen}"


Source: TypeAlias = Citation | Measurement
"""Where a shipped fact came from: a manual section, or a measurement on named silicon."""
