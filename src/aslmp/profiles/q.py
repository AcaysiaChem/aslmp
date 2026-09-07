"""MELSEC-Q, and the shared shape the L series is built from.

Layer 1. **Unverified, and thinner than the other profiles.** No Q or L CPU has ever
been connected to this library, and ``aslmp/data/`` ships no device-range table for
either family: ``ranges_iqf.tsv`` and ``ranges_iqr.tsv`` are the only two that exist.

That gap is declared rather than filled. Every family the generic device table lists
with the **short** device specification is declared present **with no static range**, so
:meth:`~aslmp.profile.CpuProfile.check_range` validates that the device exists and does
not validate the span; the eleven families that need the long specification, plus the
SFC block device, are declared absent with the reason. A user who wants span checking on
a Q or an L supplies the real numbers with
:meth:`~aslmp.profile.CpuProfile.with_ranges`, which is the same mechanism a
repartitioned iQ-R uses.

Inventing Q ranges from the iQ-R table would have been easy and would have looked exactly
like knowledge. It is the one thing this package must not do: a fabricated ``D0`` to
``D12287`` would refuse a legitimate ``D14000`` on a Q26UDEHCPU, with a message citing a
manual section that says no such thing.

What **is** known and shipped: the SLMP reference manual's own point budgets (via
:func:`~aslmp.profiles.iq_r.build_slmp_ref_limits`), the ``01 00`` remote fixed field,
the three clear modes, hexadecimal ``X``/``Y``, and the model codes from
SH(NA)-080956ENG-M pp.138-139.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from aslmp.profile import (
    Capability,
    ClearMode,
    CpuProfile,
    DeviceRange,
    Encoding,
    Evidence,
    Family,
    Refusal,
)
from aslmp.profiles.iq_r import (
    SLMP_REF_AMBIGUITIES,
    SLMP_REF_DEVICES,
    SLMP_REF_FRAMES,
    SLMP_REF_MONITOR,
    SLMP_REF_REMOTE,
    SLMP_REF_TYPE_NAME,
    build_slmp_ref_capabilities,
    build_slmp_ref_limits,
)
from aslmp.wire.citations import Citation, Provenance
from aslmp.wire.codec import SpecFormat
from aslmp.wire.devicetable import DEVICE_TABLE

__all__ = ["Q", "build_legacy_profile"]

_NO_RANGE_TABLE: Final = Citation(
    "SH(NA)-080956ENG", "M", "5.2 Device codes (p.35-38)",
    note=(
        "INFERRED presence: the SLMP reference manual gives the device CODE table, "
        "which says the family is addressable, and not a per-CPU point table. No Q or L "
        "device-range source was obtained, so no span is validated for this family. "
        "Supply the real range with profile.with_ranges()."
    ),
    provenance=Provenance.INFERRED,
)

_LONG_SPEC_ABSENT: Final = Citation(
    "SH(NA)-080956ENG", "M", "5.2 Device specification formats (p.35-38)",
    note=(
        "INFERRED absence: the long timer, long retentive timer, long counter, long "
        "index register and refresh data register are reachable only through the long "
        "device specification, which is documented for the RCPU. No Q or L source was "
        "obtained that offers either. Declared absent so a request for one is a typed "
        "refusal here rather than an end code from the CPU."
    ),
    provenance=Provenance.INFERRED,
)

_BL_ABSENT: Final = Citation(
    "SH(NA)-080956ENG", "M", "5.2 Device codes (p.35-38)",
    note=(
        "The SFC block device is marked unreachable by every access command in the "
        "generic device table -- no batch, random, monitor or block access -- so it is "
        "declared absent rather than present and unusable."
    ),
)

_LONG_SPEC_REASON: Final = (
    "Reachable only with the long device specification (subcommand 0x0002 / 0x0003), "
    "which this package does not offer on a Q or an L: no source for it on this family "
    "was obtained. If your CPU does support it, override with "
    "capability_overrides={Capability.LONG_DEVICE_SPEC: '<your evidence>'} and supply "
    "the ranges with profile.with_ranges()."
)


def build_legacy_devices() -> dict[str, DeviceRange]:
    """The 41-family table for a Q or an L: presence only, no spans.

    Derived from the generic device table rather than written out, so that a family
    added to ``devices.tsv`` cannot silently go undeclared here -- ``CpuProfile``
    refuses a profile that says nothing about a family, and this is the rule that keeps
    the two in step.
    """
    devices: dict[str, DeviceRange] = {}
    for name, dt in DEVICE_TABLE.items():
        if name == "BL":
            devices[name] = DeviceRange(
                device=name,
                present=False,
                absent_reason=(
                    "The SFC block device is not accessible by any SLMP device command."
                ),
                evidence=Evidence.documented(_BL_ABSENT),
            )
        elif dt.min_spec is SpecFormat.LONG:
            devices[name] = DeviceRange(
                device=name,
                present=False,
                absent_reason=_LONG_SPEC_REASON,
                evidence=Evidence.documented(_LONG_SPEC_ABSENT),
            )
        else:
            devices[name] = DeviceRange(
                device=name,
                present=True,
                configurable=True,
                evidence=Evidence.documented(
                    _NO_RANGE_TABLE,
                    note=(
                        f"{dt.long_name} is addressable on this family; no Q/L point "
                        f"table was obtained, so no span is validated for it."
                    ),
                ),
            )
    return devices


def build_legacy_capabilities() -> dict[Capability, Evidence | Refusal]:
    """The Q/L capability set: the reference manual's, minus the long device spec."""
    return build_slmp_ref_capabilities(
        long_spec=Refusal(
            reason=(
                "The long device specification (subcommand 0x0002 / 0x0003) is "
                "documented for the RCPU. No Q or L source offering it was obtained, "
                "and guessing wrong costs a 0xC059 round trip."
            ),
            evidence=Evidence.documented(_LONG_SPEC_ABSENT),
            end_code_if_attempted=0xC059,
            alternative=(
                "Use SpecFormat.SHORT, this profile's default_spec. If your CPU does "
                "accept the long format, say so with capability_overrides."
            ),
        ),
        monitor=Evidence.documented(
            SLMP_REF_MONITOR,
            note="Documented for this family in the SLMP reference. Never exercised "
            "here, on any CPU.",
        ),
    )


def build_legacy_profile(
    *, key: str, family: Family, description: str, model_codes: Mapping[int, str]
) -> CpuProfile:
    """One Q or L profile. Presence without spans, and not one measured row."""
    carried = (
        "INFERRED: no separate Q/L figure was located; carried from the generic "
        "SLMP-reference column."
    )
    return CpuProfile(
        key=key,
        family=family,
        description=description,
        model_codes=dict(model_codes),
        devices=build_legacy_devices(),
        radix_overrides={},
        limits=build_slmp_ref_limits(carried_note=carried, batch_is_inferred=True),
        capabilities=build_legacy_capabilities(),
        default_spec=SpecFormat.SHORT,
        allowed_encodings=frozenset({Encoding.BINARY, Encoding.ASCII_XY_HEX}),
        remote_fixed_field=b"\x01\x00",
        allowed_clear_modes=frozenset(
            {ClearMode.NONE, ClearMode.EXCEPT_LATCH, ClearMode.INCLUDING_LATCH}
        ),
        ambiguities=SLMP_REF_AMBIGUITIES,
        facts={
            "remote_fixed_field": Evidence.documented(
                SLMP_REF_REMOTE,
                note="01 00, the historical Mode field fixed at 0001H, which is why "
                "01 00 has evidently worked in the field on Q and L (A-REMOTE-FIXED).",
            ),
            "allowed_clear_modes": Evidence.documented(SLMP_REF_REMOTE),
            "allowed_encodings": Evidence.documented(
                SLMP_REF_FRAMES,
                note="Binary and ASCII. Encoding.ASCII_XY_OCT is an iQ-F own-node "
                "setting and is refused on this profile.",
            ),
            "default_spec": Evidence.documented(SLMP_REF_DEVICES),
            "model_codes": Evidence.documented(SLMP_REF_TYPE_NAME),
            "xy-radix": Evidence.documented(
                SLMP_REF_DEVICES,
                note="X and Y are hexadecimal here, as on iQ-R and unlike iQ-F.",
            ),
            "device_ranges": Evidence.documented(
                _NO_RANGE_TABLE,
                note="There is no Q/L range table in aslmp/data/. Device existence is "
                "checked; spans are not. This is the largest known gap in the package.",
            ),
        },
    )


Q: Final[CpuProfile] = build_legacy_profile(
    key="melsec:q",
    family=Family.Q,
    description=(
        "MELSEC-Q. Unverified, and shipped without a device-range table: presence is "
        "checked, spans are not."
    ),
    model_codes={
        0x0041: "Q02CPU / Q02HCPU / Q02PHCPU",
        0x0042: "Q06HCPU / Q06PHCPU",
        0x0043: "Q12HCPU / Q12PHCPU",
        0x0044: "Q25HCPU / Q25PHCPU",
        0x0230: "QS001CPU",
        0x0250: "Q00JCPU",
        0x0251: "Q00CPU",
        0x0252: "Q01CPU",
        0x0268: "Q03UDCPU / Q03UDECPU",
        0x0269: "Q04UDHCPU / Q04UDEHCPU",
        0x026A: "Q06UDHCPU / Q06UDEHCPU",
        0x0366: "Q03UDVCPU",
        0x0367: "Q04UDVCPU / Q04UDPVCPU",
        0x0368: "Q06UDVCPU / Q06UDPVCPU",
    },
)
