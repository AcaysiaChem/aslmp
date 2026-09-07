"""MELSEC iQ-R. **Nothing in this module has ever been verified on hardware.**

Layer 1. There is no iQ-R in this building and there never has been, so every row here
is ``Provenance.MANUAL`` or ``Provenance.INFERRED`` and not one is ``LIVE``. That is not
a disclaimer in a README; it is machine-readable, per row, and
``profile.provenance_counts()`` will say so. ``aslmp capabilities melsec:iq-r`` prints
it. The measured column on the FX5U exists precisely so that this column can be honest
about being empty.

What differs from an iQ-F, and why each difference matters:

* ``X`` and ``Y`` are **hexadecimal**, from the generic device table, with no profile
  override. On an iQ-F they are octal. This is the single most dangerous difference in
  the package: the same literal ``Y20`` is output 16 on an FX5U and output 32 here, and
  both CPUs answer ``0x0000``. It is also why ``profile=`` is required and why an
  unrecognised model code raises instead of falling back.
* The long device specification (subcommand ``0x0002``/``0x0003``) is available, so
  ``LTS``, ``LTC``, ``LTN``, ``LSTS``, ``LSTC``, ``LSTN``, ``LCS``, ``LCC``, ``LCN``,
  ``LZ`` and ``RD`` are reachable. An FX5U answers ``0xC059`` to that subcommand.
* Monitor (``0x0801``/``0x0802``) is an iQ-R command and is allowed here. It has never
  been exercised: registration lifetime, whether registration is per connection or
  global, and what two concurrent clients do to each other are all unknown.
* The fixed field of ``0x1002``/``0x1005``/``0x1006`` is ``01 00``, not the iQ-F's
  ``00 00`` (ambiguity ``A-REMOTE-FIXED``).
* ``R`` and ``ZR`` default to **zero points**. An out-of-the-box iQ-R has no file
  register at all, so SH(NA)-080956ENG's own ``ZR16384`` example fails on a default CPU.
  ``check_range`` says that in those words rather than "address out of range", and
  ``with_ranges()`` is how a project that allocated some tells us.

Every iQ-R range is a GX Works3 **default** that a project can repartition, which is
what ``validate_ranges=False``, :meth:`~aslmp.profile.CpuProfile.with_ranges` and
``aslmp verify-ranges`` are for. Device existence and point limits are not defeatable
by any of them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final

from aslmp.profile import (
    BlockRule,
    Capability,
    ClearMode,
    CpuProfile,
    DeviceRange,
    Encoding,
    Evidence,
    Family,
    Flat,
    Limit,
    LimitKey,
    Link,
    Refusal,
    Weighted,
)
from aslmp.profiles.fx5u import (
    AMBIGUITY_BATCH_BIT_LIMIT,
    AMBIGUITY_REMOTE_FIXED,
    PresentRow,
)
from aslmp.wire.citations import Ambiguity, Citation, Provenance
from aslmp.wire.codec import SpecFormat, Unit

__all__ = [
    "IQR_AMBIGUITIES",
    "IQR_R00",
    "IQ_R",
    "R00_PRESENT",
    "R04_PRESENT",
    "SLMP_REF_AMBIGUITIES",
    "build_iqr_profile",
]

# ----------------------------------------------------------------------------------------
# The manuals. Two of them, and no measurements at all.
# ----------------------------------------------------------------------------------------

IQR_POINTS: Final = Citation(
    "SH(NA)-081263ENG", "AA",
    "2.1 Performance specifications, device points (p.47, p.49-50)",
    note="The default parameter assignment. Every one of these ranges is repartitionable "
    "in GX Works3, so the shipped figure is a default and not a property of the silicon.",
)
IQR_POINTS_INFERRED: Final = Citation(
    "SH(NA)-081263ENG", "AA",
    "2.1 Performance specifications, device points (p.47, p.49-50)",
    note="INFERRED: the performance table does not list this device separately, so no "
    "static range is validated for it.",
    provenance=Provenance.INFERRED,
)
SLMP_REF_POINTS: Final = Citation(
    "SH(NA)-080956ENG", "M", "Device access commands, points per request"
)
SLMP_REF_POINTS_INFERRED: Final = Citation(
    "SH(NA)-080956ENG", "M", "Device access commands, points per request",
    note="INFERRED: the surveyed SLMP reference column for this command was blank, so "
    "the figure is the FX5/Q one carried across. Nothing here has been verified on an "
    "iQ-R.",
    provenance=Provenance.INFERRED,
)
SLMP_REF_DEVICES: Final = Citation("SH(NA)-080956ENG", "M", "5.2 Device codes (p.35-38)")
SLMP_REF_MONITOR: Final = Citation(
    "SH(NA)-080956ENG", "M", "Monitor Registration 0801 / Execute Monitor 0802 (p.75-80)"
)
SLMP_REF_REMOTE: Final = Citation(
    "SH(NA)-080956ENG", "M", "Remote operation 1001-1006 (p.130-137)",
    note="The fixed field of 1002 / 1005 / 1006 is printed as 01 00 here and as 00 00 "
    "in JY997D56001-K; see A-REMOTE-FIXED.",
)
SLMP_REF_PASSWORD: Final = Citation(
    "SH(NA)-080956ENG", "M", "Remote password 1630 / 1631 (p.140-145)"
)
SLMP_REF_CLEAR_ERROR: Final = Citation("SH(NA)-080956ENG", "M", "Error clear 1617")
SLMP_REF_SELF_TEST: Final = Citation("SH(NA)-080956ENG", "M", "Self Test 0619")
SLMP_REF_TYPE_NAME: Final = Citation(
    "SH(NA)-080956ENG", "M", "Read Type Name 0101 and CPU model codes (p.138-139)"
)
SLMP_REF_FRAMES: Final = Citation("SH(NA)-080956ENG", "M", "4.2 Message formats (p.17-28)")
SLMP_REF_LONG_SPEC: Final = Citation(
    "SH(NA)-080956ENG", "M", "5.2 Device specification formats (p.35-38)",
    note="The 4-byte device number with a 2-byte device code, subcommand 0002/0003. "
    "Its byte order is disputed between two Mitsubishi documents; see "
    "A-LONG-SPEC-BYTE-ORDER.",
)

# ----------------------------------------------------------------------------------------
# Ambiguities
# ----------------------------------------------------------------------------------------

AMBIGUITY_ZR_RADIX: Final = Ambiguity(
    key="A-ZR-RADIX",
    question="Is the ZR file-register device number hexadecimal or decimal?",
    readings=(
        "Hexadecimal -- SH(NA)-080956ENG-M p.36 and SH(NA)-080008-AB p.68",
        "Decimal -- JY997D56001-K p.67",
    ),
    chosen="Hexadecimal, per SH(NA)-080956ENG.",
    reason=(
        "Moot on iQ-F, where ZR does not exist at all (ZR0 returned 0xC05C on our "
        "FX5U), so the FX5 manual is describing a device its own CPU does not have. "
        "Every third-party library we surveyed also treats ZR as hexadecimal."
    ),
    probe=(
        "iQ-R only. Write known values to ZR0 and ZR16 from GX Works3, then read ZR16 "
        "over SLMP with device number 0x10 and with 0x16 and see which returns it."
    ),
)

AMBIGUITY_LONG_SPEC_ORDER: Final = Ambiguity(
    key="A-LONG-SPEC-BYTE-ORDER",
    question=(
        "In the 2-byte (subcommand 0002/0003) binary device code, which byte goes first?"
    ),
    readings=(
        "9C 00 for X -- SH(NA)-080956ENG-M p.35/38 and SH(NA)-080008-AB p.68 write the "
        "code as 009CH",
        "00 9C for X -- JY997D56001-K p.36/66 writes it as 9C00H and draws the wire "
        "that way",
    ),
    chosen=(
        "9C 00, per SH(NA)-080956ENG. Moot on iQ-F, which refuses subcommand 0002 "
        "outright."
    ),
    reason=(
        "Exactly reversed between two Mitsubishi documents. pymcprotocol 0.3.0 "
        "implements 9C 00 and is reported working against iQ-R. JY997D56001-K contains "
        "no worked byte-level example using the long format anywhere, so its figure "
        "cannot be cross-checked inside its own manual."
    ),
    probe=(
        "iQ-R only: send 0401 subcommand 0002 for D0 with device code bytes 9C 00, then "
        "with 00 9C. Exactly one should return 0x0000. Our FX5U cannot answer this -- "
        "subcommand 0002 returned 0xC059."
    ),
)

AMBIGUITY_IQR_CPU_CODES: Final = Ambiguity(
    key="A-IQR-CPU-CODES",
    question=(
        "What do the individual iQ-R end codes in the 0x4000-0x4FFF block mean?"
    ),
    readings=(
        "The same as the FX5 codes of the same value, per JY997D55401 Appendix 3",
        "iQ-R specific, per SH(NA)-081264ENG, which we have not read",
    ),
    chosen=(
        "The FX5 meanings are shipped for the codes JY997D55401 documents. Any "
        "0x4000-0x4FFF code not in the table raises SlmpCpuError with 'undocumented end "
        "code', the code formatted 0x%04X, and a request to report it with the CPU "
        "model and firmware."
    ),
    reason=(
        "SH(NA)-081264ENG was never obtained. Inventing iQ-R meanings from the FX5 "
        "manual would be exactly the sort of plausible-looking fabrication the citation "
        "rule exists to prevent."
    ),
    probe=(
        "Source SH(NA)-081264ENG (MELSEC iQ-R CPU Module User's Manual (Application)) "
        "and add the rows."
    ),
)

SLMP_REF_AMBIGUITIES: Final[tuple[Ambiguity, ...]] = (
    AMBIGUITY_BATCH_BIT_LIMIT,
    AMBIGUITY_REMOTE_FIXED,
    AMBIGUITY_ZR_RADIX,
)
"""The disagreements every SLMP-reference family carries. Two of them were found by
measuring the FX5U against the generic reference, which is why they are defined in
``fx5u.py`` and imported here."""

IQR_AMBIGUITIES: Final[tuple[Ambiguity, ...]] = (
    *SLMP_REF_AMBIGUITIES,
    AMBIGUITY_LONG_SPEC_ORDER,
    AMBIGUITY_IQR_CPU_CODES,
)

# ----------------------------------------------------------------------------------------
# Device ranges
# ----------------------------------------------------------------------------------------

R00_PRESENT: Final[tuple[PresentRow, ...]] = (
    ("X", 0, 8191, "X0 to X1FFF", False),
    ("Y", 0, 8191, "Y0 to Y1FFF", False),
    ("M", 0, 8191, "M0 to M8191", True),
    ("B", 0, 8191, "B0 to B1FFF", True),
    ("SB", 0, 2047, "SB0 to SB7FF", True),
    ("F", 0, 2047, "F0 to F2047", True),
    ("V", 0, 2047, "V0 to V2047", True),
    ("L", 0, 8191, "L0 to L8191", True),
    ("D", 0, 18431, "D0 to D18431", True),
    ("W", 0, 8191, "W0 to W1FFF", True),
    ("SW", 0, 2047, "SW0 to SW7FF", True),
    ("SM", 0, 4095, "SM0 to SM4095", False),
    ("SD", 0, 4095, "SD0 to SD4095", False),
    ("TS", 0, 1023, "T0 to T1023", True),
    ("TC", 0, 1023, "T0 to T1023", True),
    ("TN", 0, 1023, "T0 to T1023", True),
    ("LTS", 0, 1023, "LT0 to LT1023", True),
    ("LTC", 0, 1023, "LT0 to LT1023", True),
    ("LTN", 0, 1023, "LT0 to LT1023", True),
    ("CS", 0, 511, "C0 to C511", True),
    ("CC", 0, 511, "C0 to C511", True),
    ("CN", 0, 511, "C0 to C511", True),
    ("LCS", 0, 511, "LC0 to LC511", True),
    ("LCC", 0, 511, "LC0 to LC511", True),
    ("LCN", 0, 511, "LC0 to LC511", True),
    ("Z", 0, 19, "Z0 to Z19", True),
    ("LZ", 0, 1, "LZ0 to LZ1", True),
)
"""R00CPU / R01CPU / R02CPU default assignment: 8192 X/Y points and 8192 ``M``."""

_LARGER: Final[tuple[PresentRow, ...]] = (
    ("X", 0, 12287, "X0 to X2FFF", False),
    ("Y", 0, 12287, "Y0 to Y2FFF", False),
    ("M", 0, 12287, "M0 to M12287", True),
)

_OVERRIDE: Final[dict[str, PresentRow]] = {row[0]: row for row in _LARGER}

R04_PRESENT: Final[tuple[PresentRow, ...]] = tuple(
    _OVERRIDE.get(row[0], row) for row in R00_PRESENT
)
"""R04CPU through R120CPU and their EN / P / SF / PSF variants: 12288 X/Y and ``M``.

The R00 group and this one differ in exactly three ranges. Everything else, including
``D0`` to ``D18431``, is the same."""

_ZERO_POINTS: Final[Mapping[str, str]] = {
    "S": "Step relay: zero points unless an SFC program is configured.",
    "STS": "Retentive timer contact: zero points unless allocated in GX Works3.",
    "STC": "Retentive timer coil: zero points unless allocated in GX Works3.",
    "STN": "Retentive timer current value: zero points unless allocated in GX Works3.",
    "LSTS": "Long retentive timer contact: zero points unless allocated in GX Works3.",
    "LSTC": "Long retentive timer coil: zero points unless allocated in GX Works3.",
    "LSTN": "Long retentive timer current value: zero unless allocated in GX Works3.",
    "R": (
        "File register: ZERO POINTS on an out-of-the-box iQ-R. SH(NA)-080956ENG's own "
        "ZR16384 example fails on a default CPU. Allocate file register capacity in the "
        "CPU parameters, then pass the real range with profile.with_ranges()."
    ),
    "ZR": (
        "File register (serial number access): ZERO POINTS by default, as for R. Its "
        "radix is disputed between two Mitsubishi documents; see A-ZR-RADIX."
    ),
}

_NO_STATIC_RANGE: Final[Mapping[str, str]] = {
    "DX": (
        "Direct access input. The iQ-R performance table does not list DX separately; "
        "it addresses the same I/O as X. No static range is validated for it."
    ),
    "DY": (
        "Direct access output. The iQ-R performance table does not list DY separately; "
        "it addresses the same I/O as Y. No static range is validated for it."
    ),
    "RD": (
        "Refresh data register. Its size follows the module refresh setting, so there "
        "is no static range to validate."
    ),
    "G": (
        "Module access device U[n]\\G. The range depends on which intelligent function "
        "module is mounted in which slot."
    ),
    "BL": (
        "SFC block device. Depends on the SFC program. Marked SLMP-incompatible in the "
        "FX5 table; not established for iQ-R."
    ),
}


def build_iqr_devices(present: Sequence[PresentRow]) -> dict[str, DeviceRange]:
    """The full 41-family table for one iQ-R group. No family is absent on an iQ-R."""
    devices: dict[str, DeviceRange] = {}
    for name, first, last, notation, configurable in present:
        devices[name] = DeviceRange(
            device=name,
            present=True,
            first=first,
            last=last,
            configurable=configurable,
            notation=notation,
            evidence=Evidence.documented(IQR_POINTS),
        )
    for name, note in _ZERO_POINTS.items():
        devices[name] = DeviceRange(
            device=name,
            present=True,
            points=0,
            configurable=True,
            notation="none by default",
            evidence=Evidence.documented(IQR_POINTS, note=note),
        )
    for name, note in _NO_STATIC_RANGE.items():
        devices[name] = DeviceRange(
            device=name,
            present=True,
            configurable=True,
            evidence=Evidence.documented(IQR_POINTS_INFERRED, note=note),
        )
    return devices


# ----------------------------------------------------------------------------------------
# Limits
# ----------------------------------------------------------------------------------------


def build_slmp_ref_limits(
    *, carried_note: str = "", batch_is_inferred: bool = False
) -> dict[LimitKey, Limit]:
    """The generic SLMP-reference budgets, shared by iQ-R, Q and L.

    ``batch_is_inferred`` is for the families where even the batch figure was carried
    from the generic column rather than found under that family's own heading -- Q and
    L, where ``limits.tsv`` marks every row inferred.

    The batch figures are the reference manual's own. The random and block figures are
    ``Provenance.INFERRED``: the surveyed reference column for Read Random was blank, so
    the FX5 figure was carried across, and 192 on an iQ-R has never been checked by
    anyone here.

    Only ``Link.CPU_BUILTIN`` rows exist. There is no shipped Ethernet-module table for
    these families, and :meth:`~aslmp.profile.CpuProfile.limit` raises for a key it does
    not have rather than lending it the built-in port's figure.
    """
    batch_citation = SLMP_REF_POINTS_INFERRED if batch_is_inferred else SLMP_REF_POINTS
    documented = Evidence.documented(
        batch_citation,
        note=(carried_note + " " if carried_note else "")
        + "The bit ceiling of 7168 is double the measured iQ-F figure; see "
        "A-BATCH-BIT-LIMIT.",
    )
    inferred = Evidence.documented(
        SLMP_REF_POINTS_INFERRED,
        note=carried_note or "Carried from the FX5 figure; never checked on this family.",
    )
    cpu = Link.CPU_BUILTIN
    limits: dict[LimitKey, Limit] = {}
    for command in (0x0401, 0x1401):
        limits[(command, Encoding.BINARY, Unit.WORD, cpu)] = Limit(
            Flat(960), 0xC052, documented
        )
        limits[(command, Encoding.BINARY, Unit.BIT, cpu)] = Limit(
            Flat(7168), 0xC051, documented
        )
        limits[(command, Encoding.ASCII_XY_HEX, Unit.WORD, cpu)] = Limit(
            Flat(960), 0xC052, documented
        )
        limits[(command, Encoding.ASCII_XY_HEX, Unit.BIT, cpu)] = Limit(
            Flat(3584), 0xC051, documented
        )
    limits[(0x0403, Encoding.BINARY, Unit.WORD, cpu)] = Limit(Flat(192), 0xC054, inferred)
    limits[(0x1402, Encoding.BINARY, Unit.WORD, cpu)] = Limit(
        Weighted(12, 14, 1920), 0xC054, inferred
    )
    limits[(0x1402, Encoding.BINARY, Unit.BIT, cpu)] = Limit(Flat(188), 0xC053, inferred)
    limits[(0x0406, Encoding.BINARY, Unit.WORD, cpu)] = Limit(
        BlockRule(120, 0, 960), 0xC0D8, inferred
    )
    return limits


# ----------------------------------------------------------------------------------------
# Capabilities
# ----------------------------------------------------------------------------------------


def build_slmp_ref_capabilities(
    *, long_spec: Evidence | Refusal, monitor: Evidence | Refusal
) -> dict[Capability, Evidence | Refusal]:
    """The capability set every SLMP-reference family shares, with two holes to fill.

    ``long_spec`` and ``monitor`` are parameters because they are exactly what the Q and
    L families do not clearly share with the iQ-R, and defaulting either of them would
    be a guess wearing a default's clothes.
    """
    return {
        Capability.BATCH_ACCESS: Evidence.documented(SLMP_REF_POINTS),
        Capability.RANDOM_ACCESS: Evidence.documented(SLMP_REF_POINTS),
        Capability.BLOCK_ACCESS: Evidence.documented(SLMP_REF_POINTS),
        Capability.MONITOR: monitor,
        Capability.LONG_DEVICE_SPEC: long_spec,
        Capability.SELF_TEST: Evidence.documented(SLMP_REF_SELF_TEST),
        Capability.READ_TYPE_NAME: Evidence.documented(SLMP_REF_TYPE_NAME),
        Capability.CLEAR_ERROR: Evidence.documented(SLMP_REF_CLEAR_ERROR),
        Capability.REMOTE_CONTROL: Evidence.documented(SLMP_REF_REMOTE),
        Capability.REMOTE_RESET: Evidence.documented(SLMP_REF_REMOTE),
        Capability.REMOTE_PASSWORD: Evidence.documented(SLMP_REF_PASSWORD),
        Capability.FOUR_E_FRAME: Evidence.documented(SLMP_REF_FRAMES),
    }


IQR_MONITOR: Final = Evidence.documented(
    SLMP_REF_MONITOR,
    note="Monitor is an iQ-R command and is allowed here. It has never been exercised: "
    "registration lifetime, whether registration is per connection or global, and what "
    "two concurrent clients do to each other are all untested.",
)

IQR_LONG_SPEC: Final = Evidence.documented(
    SLMP_REF_LONG_SPEC,
    note="Available on iQ-R, and the only way to reach LT, LST, LC, LZ and RD. The "
    "2-byte device code's byte order is disputed; this package emits 9C 00 per "
    "SH(NA)-080956ENG (A-LONG-SPEC-BYTE-ORDER).",
)


# ----------------------------------------------------------------------------------------
# The profiles
# ----------------------------------------------------------------------------------------


def build_iqr_profile(
    *,
    key: str,
    description: str,
    model_codes: Mapping[int, str],
    present: Sequence[PresentRow],
) -> CpuProfile:
    """One iQ-R profile. Nothing here is measured; every row says so."""
    return CpuProfile(
        key=key,
        family=Family.IQ_R,
        description=description,
        model_codes=dict(model_codes),
        devices=build_iqr_devices(present),
        radix_overrides={},
        limits=build_slmp_ref_limits(),
        capabilities=build_slmp_ref_capabilities(
            long_spec=IQR_LONG_SPEC, monitor=IQR_MONITOR
        ),
        default_spec=SpecFormat.SHORT,
        allowed_encodings=frozenset({Encoding.BINARY, Encoding.ASCII_XY_HEX}),
        remote_fixed_field=b"\x01\x00",
        allowed_clear_modes=frozenset(
            {ClearMode.NONE, ClearMode.EXCEPT_LATCH, ClearMode.INCLUDING_LATCH}
        ),
        ambiguities=IQR_AMBIGUITIES,
        facts={
            "remote_fixed_field": Evidence.documented(
                SLMP_REF_REMOTE,
                note="01 00 on the SLMP-reference families, against the iQ-F's 00 00. "
                "Never guessed and never retried in the other value (A-REMOTE-FIXED).",
            ),
            "allowed_clear_modes": Evidence.documented(
                SLMP_REF_REMOTE,
                note="The reference manual's clear-mode table lists 00H, 01H and 02H. "
                "The FX5 manual lists one row, which is why the iQ-F profiles differ.",
            ),
            "allowed_encodings": Evidence.documented(
                SLMP_REF_FRAMES,
                note="Binary and ASCII. Encoding.ASCII_XY_OCT is an iQ-F own-node "
                "setting and is refused on this profile.",
            ),
            "default_spec": Evidence.documented(
                SLMP_REF_DEVICES,
                note="SHORT is the default because it is what every worked example in "
                "the reference manual uses; LONG is available here, unlike on an iQ-F.",
            ),
            "model_codes": Evidence.documented(
                SLMP_REF_TYPE_NAME,
                note="0x0360 RCPU is deliberately absent from every profile: it "
                "identifies a family rather than a model, so connect() must raise "
                "SlmpProfileMismatchError rather than claim it.",
            ),
            "xy-radix": Evidence.documented(
                SLMP_REF_DEVICES,
                note="X and Y are HEXADECIMAL on an iQ-R and octal on an iQ-F. The same "
                "literal Y20 is a different physical output on the two families, and "
                "both answer 0x0000, which is why profile= is required.",
            ),
        },
    )


IQ_R: Final[CpuProfile] = build_iqr_profile(
    key="melsec:iq-r",
    description=(
        "MELSEC iQ-R, R04CPU through R120CPU and their EN / P / SF / PSF variants. "
        "Unverified: no iQ-R has ever been connected to this library."
    ),
    model_codes={
        0x4800: "R04CPU",
        0x4801: "R08CPU",
        0x4802: "R16CPU",
        0x4803: "R32CPU",
        0x4804: "R120CPU",
        0x4805: "R04ENCPU",
        0x4806: "R08ENCPU",
        0x4807: "R16ENCPU",
        0x4808: "R32ENCPU",
        0x4809: "R120ENCPU",
        0x4820: "R12CCPU-V",
        0x4841: "R08PCPU",
        0x4842: "R16PCPU",
        0x4843: "R32PCPU",
        0x4844: "R120PCPU",
        0x4851: "R08PSFCPU",
        0x4852: "R16PSFCPU",
        0x4853: "R32PSFCPU",
        0x4854: "R120PSFCPU",
        0x4891: "R08SFCPU",
        0x4892: "R16SFCPU",
        0x4893: "R32SFCPU",
        0x4894: "R120SFCPU",
        0x48A0: "R00CPU",
        0x48A1: "R01CPU",
        0x48A2: "R02CPU",
    },
    present=R04_PRESENT,
)
"""The family profile, and the one ``by_model_code`` resolves every iQ-R code to.

It carries the R04-group default ranges. An **R00CPU, R01CPU or R02CPU** owner should
name :data:`IQR_R00` instead: those three have 8192 ``X``/``Y``/``M`` points where this
table says 12288, so this profile would let ``M9000`` through and the CPU would answer
``0xC056``. Both profiles claim those three model codes, so ``connect()`` accepts either
declaration; nothing here chooses between them silently, which is why the narrower one
has to be asked for by name."""

IQR_R00: Final[CpuProfile] = build_iqr_profile(
    key="melsec:iq-r/r00",
    description=(
        "MELSEC iQ-R R00CPU / R01CPU / R02CPU: 8192 X, Y and M points where the rest of "
        "the family has 12288. Unverified."
    ),
    model_codes={0x48A0: "R00CPU", 0x48A1: "R01CPU", 0x48A2: "R02CPU"},
    present=R00_PRESENT,
)
"""The three smallest iQ-R CPUs, reachable by name from ``by_key`` only."""
