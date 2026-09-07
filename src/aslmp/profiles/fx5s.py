"""MELSEC iQ-F FX5S. Same device points as the FX5U in the tables we read.

Layer 1. **Unverified**, like every profile in this package except the FX5U: all
``Provenance.MANUAL``.

The four model codes ``0x4B55``-``0x4B58`` carry a caveat of their own. The survey's
extract of JY997D56001-K p.110 read "FX5S-30/40/60 MT/ES, 80MT/ESS" against that block,
which cannot be right because ``0x4B5C``-``0x4B5F`` are already the MT/ESS block. The
**codes** are the manual's; the four **names** are our reading of it, recorded as
ambiguity ``A-FX5S-MODEL-CODES``. A wrong name here costs a user a confusing string in
``CpuIdentity.model``; it cannot make ``connect()`` accept the wrong CPU, because the
code is what is compared.
"""

from __future__ import annotations

from typing import Final

from aslmp.profile import CpuProfile
from aslmp.profiles.fx5u import build_iqf_profile

__all__ = ["FX5S"]

FX5S: Final[CpuProfile] = build_iqf_profile(
    key="melsec:iq-f/fx5s",
    description=(
        "MELSEC iQ-F FX5S. Device points as the FX5U in the tables read; never "
        "measured here, and four of its model-code names are our reading."
    ),
    model_codes={
        0x4B4E: "FX5S-30MR/ES",
        0x4B4F: "FX5S-40MR/ES",
        0x4B50: "FX5S-60MR/ES",
        0x4B51: "FX5S-80MR/ES",
        0x4B55: "FX5S-30MT/ES",
        0x4B56: "FX5S-40MT/ES",
        0x4B57: "FX5S-60MT/ES",
        0x4B58: "FX5S-80MT/ES",
        0x4B5C: "FX5S-30MT/ESS",
        0x4B5D: "FX5S-40MT/ESS",
        0x4B5E: "FX5S-60MT/ESS",
        0x4B5F: "FX5S-80MT/ESS",
    },
)
