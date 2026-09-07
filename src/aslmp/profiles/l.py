"""MELSEC-L. The same shape as the Q profile, with the L model codes.

Layer 1. **Unverified**, and carrying the same declared gap as ``q.py``: no L
device-range table exists in ``aslmp/data/``, so device *existence* is checked and
device *spans* are not. See :mod:`aslmp.profiles.q` for why that gap is left open
rather than filled with the iQ-R numbers.

The L series is the Q series' rail-mounted successor and shares the SLMP reference
manual, the ``01 00`` remote fixed field, the three clear modes and hexadecimal
``X``/``Y``. It is a separate profile rather than an alias so that
``CpuIdentity.model`` and every diagnostic say ``melsec:l``, and so that an L-specific
range table can land here without touching the Q one.
"""

from __future__ import annotations

from typing import Final

from aslmp.profile import CpuProfile, Family
from aslmp.profiles.q import build_legacy_profile

__all__ = ["L"]

L: Final[CpuProfile] = build_legacy_profile(
    key="melsec:l",
    family=Family.L,
    description=(
        "MELSEC-L. Unverified, and shipped without a device-range table: presence is "
        "checked, spans are not."
    ),
    model_codes={
        0x0541: "L02CPU / L02CPU-P",
        0x0542: "L26CPU-BT / L26CPU-PBT",
        0x0543: "L02SCPU / L02SCPU-P",
        0x0544: "L06CPU / L06CPU-P",
        0x0545: "L26CPU / L26CPU-P",
    },
)
