"""MELSEC iQ-F FX5UJ. The same family with a materially smaller device table.

Layer 1. **Unverified**: every row is ``Provenance.MANUAL``.

Nineteen of the twenty-eight present device ranges are smaller than the FX5U's, and
they are the reason per-model profiles exist at all rather than one ``iq-f`` profile
with the FX5U's numbers. ``M`` ends at 7679 instead of 32767, ``F`` at 127 instead of
32767, and the retentive timers at ``ST15`` instead of ``ST1023``. A client that shipped
the FX5U table for an FX5UJ would validate ``M20000`` happily and hand the CPU a request
it answers with ``0xC056`` -- the exact failure mode this package refuses to have.

``X``, ``Y``, ``S``, ``SM``, ``SD``, ``Z``, ``LZ``, ``R`` and ``D`` are unchanged, so
``D`` still ends at ``D7999``.
"""

from __future__ import annotations

from typing import Final

from aslmp.profile import CpuProfile
from aslmp.profiles.fx5u import IQF_PRESENT, PresentRow, build_iqf_profile

__all__ = ["FX5UJ", "FX5UJ_CARRIED_OVER", "FX5UJ_PRESENT"]

_SMALLER: Final[tuple[PresentRow, ...]] = (
    ("M", 0, 7679, "M0 to M7679", True),
    ("L", 0, 7679, "L0 to L7679", True),
    ("F", 0, 127, "F0 to F127", True),
    ("B", 0, 2047, "B0 to B7FF", True),
    ("SB", 0, 2047, "SB0 to SB7FF", True),
    ("TS", 0, 511, "T0 to T511", True),
    ("TC", 0, 511, "T0 to T511", True),
    ("TN", 0, 511, "T0 to T511", True),
    ("STS", 0, 15, "ST0 to ST15", True),
    ("STC", 0, 15, "ST0 to ST15", True),
    ("STN", 0, 15, "ST0 to ST15", True),
    ("CS", 0, 255, "C0 to C255", True),
    ("CC", 0, 255, "C0 to C255", True),
    ("CN", 0, 255, "C0 to C255", True),
    ("LCS", 0, 63, "LC0 to LC63", True),
    ("LCC", 0, 63, "LC0 to LC63", True),
    ("LCN", 0, 63, "LC0 to LC63", True),
    ("W", 0, 1023, "W0 to W3FF", True),
    ("SW", 0, 1023, "SW0 to SW3FF", True),
)
"""Every FX5UJ range that differs from :data:`~aslmp.profiles.fx5u.IQF_PRESENT`."""

_OVERRIDE: Final[dict[str, PresentRow]] = {row[0]: row for row in _SMALLER}

FX5UJ_CARRIED_OVER: Final[frozenset[str]] = frozenset(
    row[0] for row in IQF_PRESENT if row[0] not in _OVERRIDE
)
"""The nine ranges the sources read do NOT state separately for an FX5UJ.

``X``, ``Y``, ``S``, ``SM``, ``SD``, ``Z``, ``LZ``, ``R`` and ``D`` are the FX5U's
figures carried across, and they ship as ``Provenance.INFERRED`` for exactly that
reason. They are the ones to point ``aslmp verify-ranges`` at first."""

FX5UJ_PRESENT: Final[tuple[PresentRow, ...]] = tuple(
    _OVERRIDE.get(row[0], row) for row in IQF_PRESENT
)
"""The FX5U table with the nineteen smaller ranges substituted, in the same order."""

FX5UJ: Final[CpuProfile] = build_iqf_profile(
    key="melsec:iq-f/fx5uj",
    description=(
        "MELSEC iQ-F FX5UJ. Nineteen device ranges smaller than the FX5U's; never "
        "measured here."
    ),
    model_codes={
        0x4B0D: "FX5UJ-24MR/ES",
        0x4B0E: "FX5UJ-40MR/ES",
        0x4B0F: "FX5UJ-60MR/ES",
        0x4B14: "FX5UJ-24MT/ES",
        0x4B15: "FX5UJ-40MT/ES",
        0x4B16: "FX5UJ-60MT/ES",
        0x4B1B: "FX5UJ-24MT/ESS",
        0x4B1C: "FX5UJ-40MT/ESS",
        0x4B1D: "FX5UJ-60MT/ESS",
    },
    present=FX5UJ_PRESENT,
    inferred_ranges=FX5UJ_CARRIED_OVER,
)
