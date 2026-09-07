"""MELSEC iQ-F FX5UC. Same device points as the FX5U, in a connector-terminal body.

Layer 1. **Unverified.** Every row here is ``Provenance.MANUAL``: the figures come from
the same JY997D55401 table as the FX5U's, but a measurement names one piece of silicon
and no FX5UC has ever been in this building. Where the FX5U profile carries a
``Measurement``, this one carries the manual section the measurement agreed with, and
says in its note that the confirmation was on a different CPU.

The monitor and long-device-specification refusals are carried across anyway. Both come
from JY997D56001-K's own FX5 command list, which the bench then confirmed on an FX5U,
and a refusal is the safe direction to carry: it costs a typed error naming
``capability_overrides``, where a wrong "supported" costs a round trip and a ``0xC059``.
"""

from __future__ import annotations

from typing import Final

from aslmp.profile import CpuProfile
from aslmp.profiles.fx5u import build_iqf_profile

__all__ = ["FX5UC"]

FX5UC: Final[CpuProfile] = build_iqf_profile(
    key="melsec:iq-f/fx5uc",
    description=(
        "MELSEC iQ-F FX5UC. Device points identical to the FX5U; never measured here."
    ),
    model_codes={
        0x4A91: "FX5UC-32MT/D",
        0x4A92: "FX5UC-64MT/D",
        0x4A93: "FX5UC-96MT/D",
        0x4A99: "FX5UC-32MT/DSS",
        0x4A9A: "FX5UC-64MT/DSS",
        0x4A9B: "FX5UC-96MT/DSS",
        0x4AA9: "FX5UC-32MR/DS-TS",
        0x4AB1: "FX5UC-32MT/DS-TS",
        0x4AB9: "FX5UC-32MT/DSS-TS",
    },
)
