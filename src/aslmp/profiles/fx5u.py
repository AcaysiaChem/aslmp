"""MELSEC iQ-F FX5U, and the shared iQ-F material every FX5 profile is built from.

Layer 1. This is the only CPU this library has ever talked to: an **FX5U-32MT/DS on
firmware 1.065**, over one SLMP connection entry, TCP / binary / 3E, measured on
2026-09-06 and 2026-09-07. Every ``Provenance.LIVE`` row in the package names that
silicon, and the facts below that disagree with a Mitsubishi manual disagree with it
because the CPU did.

Four of those disagreements are load-bearing:

* ``X``/``Y`` device numbers are the **linear index** of the octal literal. GX Works3
  ``Y20`` -- the seventeenth output -- goes on the wire as 16. JY997D56001-K's own 3E
  binary examples on p.79 and p.86 say otherwise, and are traceable to figures that
  rev D/E printed under Q-series hexadecimal captions and nobody recomputed
  (ambiguity ``A-IQF-XY``).
* The batch **bit** ceiling is 3584, not the 7168 of the generic SLMP reference:
  3584 points from ``M0`` returned ``0x0000`` and 3585 returned ``0xC051``
  (``A-BATCH-BIT-LIMIT``).
* ``0x0801`` and ``0x0802`` return ``0xC059``. Monitor is not an iQ-F command, and this
  package refuses it before the wire rather than emulating it with a ``0403``
  (``A-IQF-MONITOR``).
* A device family this CPU does not have answers ``0xC05C``, not the ``0xC05B`` the
  documentation predicts -- so ``V``, ``ZR``, ``DX`` and ``DY`` are declared absent and
  refused here, and a user never has to decode that code.

``fx5uc.py``, ``fx5uj.py`` and ``fx5s.py`` import the builders below. They are the same
CPU family with different device-point tables, and **none of them has been measured**:
their rows are ``Provenance.MANUAL`` even where the identical figure is ``LIVE`` on the
FX5U, because a measurement names one piece of silicon.
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
from aslmp.wire.citations import Ambiguity, Citation, Measurement, Provenance
from aslmp.wire.codec import SpecFormat, Unit
from aslmp.wire.devicetable import Radix

__all__ = [
    "FX5U",
    "IQF_ABSENT",
    "IQF_AMBIGUITIES",
    "IQF_PRESENT",
    "PresentRow",
    "build_iqf_profile",
]

# ----------------------------------------------------------------------------------------
# The bench
# ----------------------------------------------------------------------------------------

BENCH_CPU: Final = "FX5U-32MT/DS"
BENCH_FIRMWARE: Final = "1.065"


def _measured(date: str, note: str) -> Measurement:
    """A measurement on the one CPU this package has ever had in the building."""
    return Measurement(cpu=BENCH_CPU, firmware=BENCH_FIRMWARE, date=date, note=note)


XY_LINEAR_INDEX: Final = _measured(
    "2026-09-07",
    "Y0..Y63 were cleared, one bit set at a chosen WIRE device number, and the whole "
    "block read back: wire 0 lit linear output 0, wire 8 lit output 8, and wire 16 lit "
    "output 16, which GX Works3 shows as Y20. The device number field carries the VALUE "
    "of the octal literal, never its digits. Binary, 3E, TCP, device code 0x9D.",
)

XY_OCTAL_NOT_CHECKED: Final = _measured(
    "2026-09-07",
    "Writing a bit at wire number 8 for Y returned end code 0x0000 and succeeded, "
    "although Y8 is not expressible in octal and the CPU has no such output. The client "
    "is the only thing that will refuse it.",
)

D_RANGE_END: Final = _measured(
    "2026-09-06",
    "D7999 with 1 point returned 0x0000, D8000 returned 0xC056, and D7999 with 2 points "
    "returned 0xC056 -- which is why range checking takes the span and not the head "
    "address.",
)

ABSENT_DEVICE_CODE: Final = _measured(
    "2026-09-06",
    "V0 (code 0x94), ZR0 (0xB0) and DX0 (0xA2) each returned 0xC05C, an error in "
    "request contents -- not the 0xC05B 'cannot access the specified device' that the "
    "doc-derived mapping predicted (ambiguity A-C05C-NOT-C05B).",
)

BATCH_WORD_CEILING: Final = _measured(
    "2026-09-06",
    "Binary-searched from D0: 960 points returned 0x0000 (1920 payload bytes, 1931 on "
    "the wire) and 961 returned 0xC052. Reconfirmed over UDP.",
)

BATCH_BIT_CEILING: Final = _measured(
    "2026-09-06",
    "Binary-searched from M0: 3584 points returned 0x0000 and 3585 returned 0xC051. "
    "SH(NA)-080956ENG-M gives 7168 for the generic case -- exactly double -- and a "
    "client that ships that figure builds frames this CPU rejects (A-BATCH-BIT-LIMIT).",
)

RANDOM_CEILING: Final = _measured(
    "2026-09-06",
    "192 word-access points returned 0x0000 and 193 returned 0xC054. 192 double-word "
    "points also returned 0x0000, and 96 word + 96 double-word did too, so the cap is "
    "on the SUM of the two counts and not on the words that come back.",
)

MONITOR_UNSUPPORTED: Final = _measured(
    "2026-09-06",
    "0x0801 Monitor Registration and 0x0802 Execute Monitor both returned 0xC059 -- "
    "'a command or subcommand that cannot be used by the CPU module' -- measured twice "
    "through independent code paths. 0x0802 answering 0xC059 rather than 0xC05D is "
    "useful: there is no way to mistake it for 'you forgot to register'.",
)

LONG_SPEC_UNSUPPORTED: Final = _measured(
    "2026-09-06",
    "Subcommand 0x0002, the iQ-R long device specification (4-byte device number, "
    "2-byte device code), returned 0xC059. The FX5U rejects the 32-bit device spec "
    "outright, which also puts LCS, LCC and LCN out of reach on this CPU.",
)

SELF_TEST_OK: Final = _measured(
    "2026-09-06",
    "0x0619 Self Test with loopback data 'ABCD' (41 42 43 44) echoed those four bytes "
    "back in about 7 ms, with no side effect of any kind. It is the connect handshake.",
)

IDENTIFY_OK: Final = _measured(
    "2026-09-06",
    "0x0101 Read Type Name returned the 16-character name 'FX5U-32MT/DS    ' (space "
    "padded, ASCII even in binary coding) and model code 0x4A49, matching "
    "JY997D56001-K p.110 exactly.",
)

FOUR_E_ACCEPTED: Final = _measured(
    "2026-09-06",
    "A well-formed 4E binary 0401 returned a 4E response with serial 0x1234 echoed "
    "correctly as 34 12, on a connection entry explicitly configured for 3E. Two "
    "Mitsubishi manuals say that is impossible, so 4E is permitted and NOT made the "
    "default, and this acceptance is not contractual (A-IQF-4E).",
)

BATCH_WORKS: Final = _measured(
    "2026-09-06",
    "0x0401 and 0x1401 were exercised in both codings' binary form against D, M, X and "
    "Y across the whole shipped range, in 3E over TCP and UDP.",
)

RANDOM_WORKS: Final = _measured(
    "2026-09-06",
    "0x0403 was exercised with word, double-word and mixed point lists; a double-word "
    "access point is one IEEE-754 float low-word-first, proved four ways.",
)

# ----------------------------------------------------------------------------------------
# The manuals
# ----------------------------------------------------------------------------------------

FX5_DEVICE_RANGE: Final = Citation(
    "JY997D56001", "K", "4.2 Device range (p.66-67)",
    note="The FX5 accessible-device table, including the FX5-incompatible column.",
)
FX5_DEVICE_POINTS: Final = Citation(
    "JY997D55401", "unknown", "14.4 Range of use of device points",
    note=(
        "The revision of this manual was not recorded when the table was read; the "
        "figures are stable across the revisions we saw, but the citation cannot be "
        "pinned the way JY997D56001-K's can."
    ),
)
FX5_DEVICE_POINTS_INFERRED: Final = Citation(
    "JY997D55401", "unknown", "14.4 Range of use of device points",
    note=(
        "INFERRED: this model's figure was not separately documented in the sources "
        "read; it is the FX5U figure carried across. Run `aslmp verify-ranges` against "
        "the CPU before relying on it, or pass the real range with "
        "profile.with_ranges()."
    ),
    provenance=Provenance.INFERRED,
)
FX5_IO_POINTS: Final = Citation(
    "JY997D55301", "F", "2.6 Number of device points",
    note="X and Y are printed in octal notation, X0 to X1777.",
)
FX5_BATCH: Final = Citation(
    "JY997D56001", "K", "4.2 Device Read (Batch) / Device Write (Batch) (p.69-75)"
)
FX5_RANDOM: Final = Citation(
    "JY997D56001", "K", "4.2 Device Read Random / Device Write Random (p.77-93)"
)
FX5_BLOCK: Final = Citation(
    "JY997D56001", "K", "4.2 Device Read Block / Device Write Block (p.95-101)"
)
FX5_ENET_RANDOM: Final = Citation(
    "JY997D56001", "K", "4.2 (p.77, footnote)",
    note="An FX5-ENET module caps Read Random at 123 points; the built-in port allows 192.",
)
FX5_REMOTE: Final = Citation(
    "JY997D56001", "K", "4.2 Remote operation (p.103-108)",
    note=(
        "The fixed field of 1002 / 1005 / 1006 is printed as 00 00 here and as 01 00 in "
        "SH(NA)-080956ENG-M; see A-REMOTE-FIXED. We deliberately never sent a CPU-state "
        "command to the bench, so every remote row is documentary."
    ),
)
FX5_CLEAR_ERROR: Final = Citation(
    "JY997D56001", "K", "4.2 Error clear (p.109)",
    note="The subcommand is 0000 in the detail section and 0001 in the command list; "
    "see A-1617-SUBCOMMAND.",
)
FX5_PASSWORD: Final = Citation(
    "JY997D56001", "K", "4.2 Remote password (p.111-114)",
    note="Never exercised: the bench CPU has no remote password set.",
)
FX5_COMMAND_LIST: Final = Citation(
    "JY997D56001", "K", "2.1 Applicable commands and frames (p.25-28)"
)
FX5_1406_INFERRED: Final = Citation(
    "JY997D56001", "K", "4.2 Device Read Block / Device Write Block (p.95-101)",
    note=(
        "INFERRED: the 760-point total and the 4-word per-block overhead were carried "
        "from the locked design and were NOT located in the sources read. Verify "
        "against JY997D56001-K before relying on either. Block access has never been "
        "exercised on the bench in any direction."
    ),
    provenance=Provenance.INFERRED,
)

# ----------------------------------------------------------------------------------------
# Ambiguities carried by every iQ-F profile
#
# Verbatim from aslmp/data/ambiguities.tsv, which is the copy `aslmp ambiguities` and
# tests/unit/test_profiles.py both read. Two of them (A-BATCH-BIT-LIMIT, A-REMOTE-FIXED)
# are imported by iq_r.py as well: both were found by measuring THIS CPU against the
# generic SLMP reference, so this is where they are written down.
# ----------------------------------------------------------------------------------------

AMBIGUITY_XY: Final = Ambiguity(
    key="A-IQF-XY",
    question=(
        "For an iQ-F, does the X/Y device-number field carry the linear index of the "
        "octal literal, or the octal digits reinterpreted as hexadecimal?"
    ),
    readings=(
        "Linear index -- Y45 goes on the wire as 0x25 (JY997D56001-K's 1E examples, "
        "written fresh for FX5 in rev G, and the device-table footnote)",
        "Octal digits as hex -- Y27 goes on the wire as 0x27 (JY997D56001-K's 3E binary "
        "examples on p.79 and p.86)",
    ),
    chosen=(
        "Linear index. parse_address treats X and Y as base 8 on iQ-F and transmits the "
        "integer value."
    ),
    reason=(
        "Measured directly: writing a single bit at wire number 0, 8 and 16 lit linear "
        "output positions 0, 8 and 16, so GX Works3 Y20 is 16 on the wire. The rev K 3E "
        "binary examples are traceable to un-recomputed copy-paste; rev D/E prints the "
        "identical byte strings under Q-series hexadecimal captions."
    ),
    probe=(
        "Settled. The original probe was: force a known pattern on physical outputs, "
        "batch-read 16 bits from head 0 with device code 0x9D, and see which linear "
        "position moved."
    ),
)

AMBIGUITY_XY_LEGALITY: Final = Ambiguity(
    key="A-XY-OCTAL-LEGALITY",
    question=(
        "Does an FX5 reject an X/Y literal containing the digits 8 or 9, which octal "
        "notation cannot express?"
    ),
    readings=(
        "The PLC rejects it, because Y8 is not an address the CPU has",
        "The PLC accepts it and writes somewhere -- observed",
    ),
    chosen=(
        "parse_address raises SlmpDeviceRadixError for any 8 or 9 in an X/Y literal on "
        "an iQ-F. The client is the only thing that will refuse it."
    ),
    reason=(
        "Writing a bit at wire number 8 for Y returned end code 0x0000 and succeeded. "
        "This is the second confirmed case of this CPU accepting a documented-illegal "
        "address, the first being a TS point in a Read Random."
    ),
    probe="Settled.",
)

AMBIGUITY_MONITOR: Final = Ambiguity(
    key="A-IQF-MONITOR",
    question="Are 0801 Monitor Registration and 0802 Execute Monitor usable on an iQ-F?",
    readings=(
        "No -- JY997D56001-K's command list does not list them for the FX5 CPU",
        "No, and the refusal is 0xC059 rather than 0xC05D -- observed",
    ),
    chosen=(
        "Capability-gated off on every iQ-F profile. monitor_register() raises "
        "SlmpCapabilityError citing the measurement, pre-transport. It is never "
        "emulated with a 0403 substitution."
    ),
    reason=(
        "Both 0801 and 0802 returned 0xC059, measured twice through independent code "
        "paths. Silently substituting a Read Random for a requested monitor would be "
        "exactly the kind of substitution this library refuses."
    ),
    probe=(
        "Settled for FX5U. The positive iQ-R path -- registration lifetime, whether it "
        "is per connection or global, what two concurrent clients do -- has never been "
        "tested by anyone here."
    ),
)

AMBIGUITY_CLEAR_MODE: Final = Ambiguity(
    key="A-CLEAR-MODE",
    question="Which clear modes does 1001 Remote Run accept on an FX5?",
    readings=(
        "Only 00H -- JY997D56001-K p.105 clear-mode table has exactly one row",
        "00H, 01H and 02H -- the communication example on the SAME page sets clear mode "
        "to 'clear all devices including that in the latch range' and prints 02H",
    ),
    chosen=(
        "iQ-F profiles allow ClearMode.NONE only; anything else raises before the "
        "request is built."
    ),
    reason=(
        "The FX5 manual contradicts itself on one page, and the consequence of guessing "
        "wrong is clearing a running machine's latch range."
    ),
    probe=(
        "1001 with clear mode 01H and then 02H against a scratch program on a CPU in "
        "STOP: record the end code, and check whether devices actually cleared."
    ),
)

AMBIGUITY_4E: Final = Ambiguity(
    key="A-IQF-4E",
    question=(
        "Does the FX5 built-in Ethernet port accept 4E frames on a connection entry "
        "configured as 3E?"
    ),
    readings=(
        "No -- JY997D56001-K 2.1 lists applicable frames as 3E and 1E only",
        "No -- JY997D56201-B p.25 says communication is possible by SLMP 3E frames",
        "Yes -- observed",
    ),
    chosen=(
        "Default to 3E, permit 4E on iQ-F. The acceptance is labelled as observed on "
        "one CPU and one firmware, and is not contractual."
    ),
    reason=(
        "A well-formed 4E binary 0401 returned a 4E response with serial 0x1234 echoed "
        "correctly, on a connection explicitly configured 3E. The CPU evidently "
        "dispatches on the subheader rather than on the entry's frame parameter, and "
        "the 4E serial is the only in-band defence against the measured TCP coalescing "
        "corruption."
    ),
    probe=(
        "Ask MEAU whether the behaviour is intentional. If it is, defaulting to 4E on "
        "iQ-F would turn a silent wrong-data failure into a loud SlmpSerialMismatchError."
    ),
)

AMBIGUITY_BATCH_BIT_LIMIT: Final = Ambiguity(
    key="A-BATCH-BIT-LIMIT",
    question=(
        "How many bit points fit in one binary 0401 Device Read (Batch) on an iQ-F?"
    ),
    readings=(
        "7168 -- SH(NA)-080956ENG-M's generic/iQ-R figure",
        "3584 -- JY997D56001-K's FX5 CPU module column",
        "1792 -- the FX5 ASCII figure, sometimes quoted as if it were the binary one",
    ),
    chosen="3584 for every iQ-F profile in binary, 1792 in ASCII; 7168 for iQ-R.",
    reason=(
        "Binary-searched on the CPU: 3584 points from M0 returned 0x0000 and 3585 "
        "returned 0xC051. The family-specific manual wins over the generic one, and "
        "here the hardware agrees with it."
    ),
    probe=(
        "Already settled for FX5U. Repeat the binary search on an iQ-R to confirm 7168 "
        "there; it has never been tested."
    ),
)

AMBIGUITY_REMOTE_FIXED: Final = Ambiguity(
    key="A-REMOTE-FIXED",
    question=(
        "What are the two fixed bytes after the subcommand in 1002 Remote Stop, 1005 "
        "Remote Latch Clear and 1006 Remote Reset?"
    ),
    readings=(
        "01 00 -- SH(NA)-080956ENG-M p.133/135/136, and what pymcprotocol 0.3.0 sends",
        "00 00 -- JY997D56001-K p.106/107/108",
    ),
    chosen=(
        "profile.remote_fixed_field: 00 00 on iQ-F, 01 00 on the SLMP-REF families. "
        "Never guessed, never retried in the other value."
    ),
    reason=(
        "Both readings were confirmed from rendered page images rather than a text "
        "layer, so this is a genuine difference between two Mitsubishi documents. The "
        "family-specific manual wins for its own family."
    ),
    probe=(
        "FX5U in STOP with a scratch program: send 1002 with 01 00 and record the end "
        "code, then repeat with 00 00. We deliberately never sent any CPU-state command "
        "to the bench, so this is untested in both directions."
    ),
)

IQF_AMBIGUITIES: Final[tuple[Ambiguity, ...]] = (
    AMBIGUITY_XY,
    AMBIGUITY_XY_LEGALITY,
    AMBIGUITY_MONITOR,
    AMBIGUITY_CLEAR_MODE,
    AMBIGUITY_4E,
    AMBIGUITY_BATCH_BIT_LIMIT,
    AMBIGUITY_REMOTE_FIXED,
)

# ----------------------------------------------------------------------------------------
# Device ranges
# ----------------------------------------------------------------------------------------

PresentRow = tuple[str, int, int, str, bool]
"""``(device, first index, last index, the notation GX Works3 prints, configurable)``."""

IQF_PRESENT: Final[tuple[PresentRow, ...]] = (
    ("X", 0, 1023, "X0 to X1777", False),
    ("Y", 0, 1023, "Y0 to Y1777", False),
    ("M", 0, 32767, "M0 to M32767", True),
    ("L", 0, 32767, "L0 to L32767", True),
    ("F", 0, 32767, "F0 to F32767", True),
    ("B", 0, 32767, "B0 to B7FFF", True),
    ("SB", 0, 32767, "SB0 to SB7FFF", True),
    ("S", 0, 4095, "S0 to S4095", False),
    ("TS", 0, 1023, "T0 to T1023", True),
    ("TC", 0, 1023, "T0 to T1023", True),
    ("TN", 0, 1023, "T0 to T1023", True),
    ("STS", 0, 1023, "ST0 to ST1023", True),
    ("STC", 0, 1023, "ST0 to ST1023", True),
    ("STN", 0, 1023, "ST0 to ST1023", True),
    ("CS", 0, 1023, "C0 to C1023", True),
    ("CC", 0, 1023, "C0 to C1023", True),
    ("CN", 0, 1023, "C0 to C1023", True),
    ("LCS", 0, 1023, "LC0 to LC1023", True),
    ("LCC", 0, 1023, "LC0 to LC1023", True),
    ("LCN", 0, 1023, "LC0 to LC1023", True),
    ("W", 0, 32767, "W0 to W7FFF", True),
    ("SW", 0, 32767, "SW0 to SW7FFF", True),
    ("SM", 0, 9999, "SM0 to SM9999", False),
    ("SD", 0, 11999, "SD0 to SD11999", False),
    ("Z", 0, 23, "Z0 to Z23", True),
    ("LZ", 0, 11, "LZ0 to LZ11", True),
    ("R", 0, 32767, "R0 to R32767", True),
    ("D", 0, 7999, "D0 to D7999", True),
)
"""FX5U / FX5UC / FX5S device points. ``fx5uj.py`` overrides sixteen of these rows."""

IQF_ABSENT: Final[Mapping[str, str]] = {
    "V": "Edge relay: marked FX5-incompatible in the accessible-device table.",
    "ZR": (
        "Serial-access file register: marked FX5-incompatible. Use R, which an FX5 has "
        "0 to 32767 of."
    ),
    "DX": "Direct access input: not listed for FX5 at all.",
    "DY": "Direct access output: not listed for FX5 at all.",
    "LTS": "Long timer contact: marked FX5-incompatible.",
    "LTC": "Long timer coil: marked FX5-incompatible.",
    "LTN": "Long timer current value: marked FX5-incompatible.",
    "LSTS": "Long retentive timer contact: marked FX5-incompatible.",
    "LSTC": "Long retentive timer coil: marked FX5-incompatible.",
    "LSTN": "Long retentive timer current value: marked FX5-incompatible.",
    "RD": "Refresh data register: not present on FX5.",
    "BL": (
        "SFC block device: marked SLMP-incompatible in the FX5 table, and unreachable "
        "by every access command in the generic device table."
    ),
}
"""Families an FX5 does not have, and why. ``V0``, ``ZR0`` and ``DX0`` were sent to the
bench and each returned ``0xC05C``; the rest are the manual's own table."""

_MEASURED_ABSENT: Final[frozenset[str]] = frozenset({"V", "ZR", "DX"})

_RANGE_NOTES: Final[Mapping[str, str]] = {
    "Z": "Z and LZ share one 24-word budget, so the two ceilings cannot both be taken.",
    "LZ": (
        "One LZ point is two words, and Z and LZ share one 24-word budget. LZ also "
        "needs the long device specification, which this CPU refuses."
    ),
    "LCS": (
        "Present per the device-range table, but only reachable with subcommand "
        "0002/0003, which this CPU answers 0xC059."
    ),
    "LCC": (
        "Present per the device-range table, but only reachable with subcommand "
        "0002/0003, which this CPU answers 0xC059."
    ),
    "LCN": (
        "Present per the device-range table, but only reachable with subcommand "
        "0002/0003, which this CPU answers 0xC059."
    ),
}

_G_NOTE: Final = (
    "Module access device U[n]\\G. The range depends on which intelligent function "
    "module is mounted in which slot, so there is no static table to validate against "
    "and span checking is skipped for it. It is not unknown; it is per-installation. "
    "The FX5 extension specifier is itself unresolved (F8H or F9H, ambiguity "
    "A-FX5-EXT-CODE), and the extension subcommand is not implemented."
)


def _present_range(row: PresentRow, *, measured: bool, inferred: bool) -> DeviceRange:
    name, first, last, notation, configurable = row
    if inferred:
        evidence = Evidence.documented(
            FX5_DEVICE_POINTS_INFERRED, note=_RANGE_NOTES.get(name, "")
        )
    elif measured and name in {"X", "Y"}:
        evidence = Evidence.measured(
            XY_LINEAR_INDEX,
            note=(
                f"{FX5_IO_POINTS.reference} prints the range in octal as {notation}; "
                f"the wire carries indices {first} to {last}."
            ),
        )
    elif measured and name == "D":
        evidence = Evidence.measured(
            D_RANGE_END, note=f"{FX5_DEVICE_POINTS.reference} gives {notation}."
        )
    else:
        evidence = Evidence.documented(
            FX5_DEVICE_POINTS, note=_RANGE_NOTES.get(name, "")
        )
    return DeviceRange(
        device=name,
        present=True,
        first=first,
        last=last,
        configurable=configurable,
        notation=notation,
        evidence=evidence,
    )


def _absent_range(name: str, reason: str, *, measured: bool) -> DeviceRange:
    if measured and name in _MEASURED_ABSENT:
        evidence = Evidence.measured(ABSENT_DEVICE_CODE, note=reason)
    else:
        evidence = Evidence.documented(FX5_DEVICE_RANGE, note=reason)
    return DeviceRange(device=name, present=False, absent_reason=reason, evidence=evidence)


def build_iqf_devices(
    present: Sequence[PresentRow],
    *,
    measured: bool,
    inferred: frozenset[str] = frozenset(),
) -> dict[str, DeviceRange]:
    """The full 41-family table for one iQ-F model.

    ``measured`` is true for the FX5U alone. The FX5UC, FX5UJ and FX5S carry the same
    numbers from the same manual, and calling them measured would attach this bench's
    firmware to silicon nobody here has plugged in.

    ``inferred`` names the families whose figure was **carried across from the FX5U**
    rather than found in this model's own table. On an FX5UJ that is nine of the
    twenty-eight, and they ship labelled ``Provenance.INFERRED`` so that
    ``aslmp verify-ranges`` knows which ones to go and check first.
    """
    devices = {
        row[0]: _present_range(row, measured=measured, inferred=row[0] in inferred)
        for row in present
    }
    for name, reason in IQF_ABSENT.items():
        devices[name] = _absent_range(name, reason, measured=measured)
    devices["G"] = DeviceRange(
        device="G",
        present=True,
        configurable=False,
        evidence=Evidence.documented(FX5_DEVICE_RANGE, note=_G_NOTE),
    )
    return devices


# ----------------------------------------------------------------------------------------
# Limits
# ----------------------------------------------------------------------------------------

_ASCII: Final[tuple[Encoding, ...]] = (Encoding.ASCII_XY_OCT, Encoding.ASCII_XY_HEX)


def build_iqf_limits(*, measured: bool) -> dict[LimitKey, Limit]:
    """The points-per-request budgets of one iQ-F model, built-in port and FX5-ENET.

    The binary ``0401`` word and bit ceilings and the ``0403`` ceiling are
    ``Provenance.LIVE`` on the FX5U and ``MANUAL`` everywhere else; the write
    directions were never binary-searched in either place, and ``1406`` is ``INFERRED``
    on every profile in this package.
    """

    def batch(rule_note: Measurement | None, citation: Citation, note: str) -> Evidence:
        if measured and rule_note is not None:
            return Evidence.measured(rule_note, note=note)
        return Evidence.documented(citation, note=note)

    word_evidence = batch(
        BATCH_WORD_CEILING,
        FX5_BATCH,
        "SH(NA)-080956ENG's generic figure is also 960, so manual and silicon agree.",
    )
    bit_evidence = batch(
        BATCH_BIT_CEILING,
        FX5_BATCH,
        "The generic reference gives 7168, exactly double. See A-BATCH-BIT-LIMIT.",
    )
    random_evidence = batch(
        RANDOM_CEILING,
        FX5_RANDOM,
        "The budget is word points plus double-word points. The wire's point-count "
        "fields are one byte each, so nothing on the wire stops you: this limit only "
        "exists client-side.",
    )
    write_batch = Evidence.documented(
        FX5_BATCH,
        note="Only the read direction was binary-searched; the FX5 manual gives the "
        "same figure for both.",
    )
    write_random = Evidence.documented(
        FX5_RANDOM,
        note="A flat point count is wrong in both directions here: 160 word points fit "
        "but 138 double-word points do not.",
    )
    ascii_note = (
        "Half the binary figure, because each byte costs two characters. The whole "
        "ASCII path is unverified on our bench: the FX5's Communication Data Code is a "
        "single Own Node setting for the entire Ethernet port, so testing ASCII would "
        "break every binary connection at once."
    )
    ascii_batch = Evidence.documented(FX5_BATCH, note=ascii_note)
    ascii_random = Evidence.documented(
        FX5_RANDOM,
        note="The manual states the ASCII rule as the binary formula times two; the "
        "doubled weights are that formula with the x 2 folded in.",
    )
    block_read = Evidence.documented(
        FX5_BLOCK,
        note="Word blocks + bit blocks <= 120, and the total points across all blocks "
        "<= 960. Never exercised on the bench.",
    )
    block_write = Evidence.documented(FX5_1406_INFERRED)
    enet_batch = Evidence.documented(
        FX5_BATCH,
        note="An FX5-ENET module, not the CPU built-in port: 949, not 960. This is why "
        "every LimitKey carries the Link.",
    )
    enet_random = Evidence.documented(FX5_ENET_RANDOM)

    cpu = Link.CPU_BUILTIN
    enet = Link.ETHERNET_MODULE
    binary = Encoding.BINARY
    limits: dict[LimitKey, Limit] = {
        (0x0401, binary, Unit.WORD, cpu): Limit(Flat(960), 0xC052, word_evidence),
        (0x0401, binary, Unit.BIT, cpu): Limit(Flat(3584), 0xC051, bit_evidence),
        (0x1401, binary, Unit.WORD, cpu): Limit(Flat(960), 0xC052, write_batch),
        (0x1401, binary, Unit.BIT, cpu): Limit(Flat(3584), 0xC051, write_batch),
        (0x0403, binary, Unit.WORD, cpu): Limit(Flat(192), 0xC054, random_evidence),
        (0x1402, binary, Unit.WORD, cpu): Limit(
            Weighted(12, 14, 1920), 0xC054, write_random
        ),
        (0x1402, binary, Unit.BIT, cpu): Limit(Flat(188), 0xC053, write_random),
        (0x0406, binary, Unit.WORD, cpu): Limit(
            BlockRule(120, 0, 960), 0xC0D8, block_read
        ),
        (0x1406, binary, Unit.WORD, cpu): Limit(
            BlockRule(120, 4, 760), 0xC0D8, block_write
        ),
        (0x1401, binary, Unit.WORD, enet): Limit(Flat(949), 0xC052, enet_batch),
        (0x0403, binary, Unit.WORD, enet): Limit(Flat(123), 0xC054, enet_random),
    }
    for encoding in _ASCII:
        limits[(0x0401, encoding, Unit.WORD, cpu)] = Limit(Flat(480), 0xC052, ascii_batch)
        limits[(0x0401, encoding, Unit.BIT, cpu)] = Limit(Flat(1792), 0xC051, ascii_batch)
        limits[(0x1401, encoding, Unit.WORD, cpu)] = Limit(Flat(480), 0xC052, ascii_batch)
        limits[(0x1401, encoding, Unit.BIT, cpu)] = Limit(Flat(1792), 0xC051, ascii_batch)
        limits[(0x0403, encoding, Unit.WORD, cpu)] = Limit(Flat(96), 0xC054, ascii_random)
        limits[(0x1402, encoding, Unit.WORD, cpu)] = Limit(
            Weighted(24, 28, 1920), 0xC054, ascii_random
        )
        limits[(0x1402, encoding, Unit.BIT, cpu)] = Limit(Flat(94), 0xC053, ascii_random)
        limits[(0x0406, encoding, Unit.WORD, cpu)] = Limit(
            BlockRule(60, 0, 960), 0xC0D8, block_read
        )
        limits[(0x1406, encoding, Unit.WORD, cpu)] = Limit(
            BlockRule(60, 4, 760), 0xC0D8, block_write
        )
    return limits


# ----------------------------------------------------------------------------------------
# Capabilities
# ----------------------------------------------------------------------------------------


def _observed(measurement: Measurement, citation: Citation, *, measured: bool) -> Evidence:
    """The measurement itself on the FX5U; the manual it agreed with everywhere else."""
    if measured:
        return Evidence.measured(measurement)
    return Evidence.documented(citation, note=f"Measured on {measurement.reference}.")


def build_iqf_capabilities(*, measured: bool) -> dict[Capability, Evidence | Refusal]:
    """What an iQ-F can and cannot be asked to do, with the evidence for each.

    Monitor and the long device specification are :class:`Refusal` on every iQ-F
    profile, citing the FX5U measurement even on the models we have not measured: both
    refusals come from the command list of the family manual, which the bench then
    confirmed. A refusal is the safe direction to carry across -- it costs a user a
    typed error naming the override, where a wrong "supported" costs a round trip and
    an end code.
    """

    def observed(measurement: Measurement, citation: Citation) -> Evidence:
        return _observed(measurement, citation, measured=measured)

    return {
        Capability.BATCH_ACCESS: observed(BATCH_WORKS, FX5_BATCH),
        Capability.RANDOM_ACCESS: observed(RANDOM_WORKS, FX5_RANDOM),
        Capability.BLOCK_ACCESS: Evidence.documented(
            FX5_BLOCK, note="Listed for the FX5 CPU module; never exercised on the bench."
        ),
        Capability.MONITOR: Refusal(
            reason=(
                "Monitor Registration and Execute Monitor are iQ-R commands. "
                "JY997D56001-K's command list does not offer them for the FX5 CPU, and "
                "the CPU refuses both."
            ),
            evidence=Evidence.measured(MONITOR_UNSUPPORTED),
            end_code_if_attempted=0xC059,
            alternative=(
                "Use read_random(): one round trip either way, and 3.0x faster at p50 "
                "than three batch reads. It is never substituted for you."
            ),
        ),
        Capability.LONG_DEVICE_SPEC: Refusal(
            reason=(
                "Subcommand 0x0002 / 0x0003, the 4-byte device number with a 2-byte "
                "device code, is refused outright by this CPU. LCS, LCC, LCN and LZ are "
                "in the FX5 device table but are unreachable without it."
            ),
            evidence=Evidence.measured(LONG_SPEC_UNSUPPORTED),
            end_code_if_attempted=0xC059,
            alternative="Use SpecFormat.SHORT, which is this profile's default_spec.",
        ),
        Capability.SELF_TEST: observed(SELF_TEST_OK, FX5_COMMAND_LIST),
        Capability.READ_TYPE_NAME: observed(IDENTIFY_OK, FX5_COMMAND_LIST),
        Capability.CLEAR_ERROR: Evidence.documented(FX5_CLEAR_ERROR),
        Capability.REMOTE_CONTROL: Evidence.documented(FX5_REMOTE),
        Capability.REMOTE_RESET: Evidence.documented(
            FX5_REMOTE,
            note="Remote Reset needs 'Enable Remote Reset' set in the CPU parameters; "
            "without it the CPU answers 0x408B. An absent response is the expected "
            "outcome when it works.",
        ),
        Capability.REMOTE_PASSWORD: Evidence.documented(FX5_PASSWORD),
        Capability.FOUR_E_FRAME: observed(FOUR_E_ACCEPTED, FX5_COMMAND_LIST),
    }


# ----------------------------------------------------------------------------------------
# The profile factory
# ----------------------------------------------------------------------------------------

IQF_FACT_NOTE: Final = (
    "iQ-F: JY997D56001-K p.106-108 prints the fixed field of 1002 / 1005 / 1006 as "
    "00 00 where SH(NA)-080956ENG-M prints 01 00. The family manual wins for its own "
    "family, and nothing retries in the other value (A-REMOTE-FIXED)."
)


def build_iqf_profile(
    *,
    key: str,
    description: str,
    model_codes: Mapping[int, str],
    present: Sequence[PresentRow] = IQF_PRESENT,
    measured: bool = False,
    inferred_ranges: frozenset[str] = frozenset(),
) -> CpuProfile:
    """One iQ-F profile. ``measured=True`` is reserved for the FX5U on this bench."""
    return CpuProfile(
        key=key,
        family=Family.IQ_F,
        description=description,
        model_codes=dict(model_codes),
        devices=build_iqf_devices(present, measured=measured, inferred=inferred_ranges),
        radix_overrides={"X": Radix.OCTAL, "Y": Radix.OCTAL},
        limits=build_iqf_limits(measured=measured),
        capabilities=build_iqf_capabilities(measured=measured),
        default_spec=SpecFormat.SHORT,
        allowed_encodings=frozenset(
            {Encoding.BINARY, Encoding.ASCII_XY_OCT, Encoding.ASCII_XY_HEX}
        ),
        remote_fixed_field=b"\x00\x00",
        allowed_clear_modes=frozenset({ClearMode.NONE}),
        ambiguities=IQF_AMBIGUITIES,
        facts={
            "remote_fixed_field": Evidence.documented(FX5_REMOTE, note=IQF_FACT_NOTE),
            "allowed_clear_modes": Evidence.documented(
                FX5_REMOTE,
                note="JY997D56001-K p.105's clear-mode table has exactly one row, 00H, "
                "while the example on the same page prints 02H. We ship the table and "
                "refuse the rest (A-CLEAR-MODE).",
            ),
            "allowed_encodings": Evidence.documented(
                Citation(
                    "JY997D56001", "K", "2.1 Communication data code (p.12)",
                    note="'ASCII code (X, Y OCT): octal; ASCII code (X, Y HEX): "
                    "hexadecimal' -- an Own Node parameter that cannot be read off "
                    "the wire.",
                )
            ),
            "default_spec": _observed(
                LONG_SPEC_UNSUPPORTED, FX5_COMMAND_LIST, measured=measured
            ),
            "model_codes": Evidence.documented(
                Citation(
                    "JY997D56001", "K", "Appendix, CPU module model codes (p.110)"
                )
            ),
            "xy-radix": _observed(XY_LINEAR_INDEX, FX5_IO_POINTS, measured=measured),
            "xy-octal-legality": _observed(
                XY_OCTAL_NOT_CHECKED, FX5_IO_POINTS, measured=measured
            ),
        },
    )


FX5U: Final = build_iqf_profile(
    key="melsec:iq-f/fx5u",
    description=(
        "MELSEC iQ-F FX5U. The one CPU this library has been measured against: an "
        "FX5U-32MT/DS on firmware 1.065, TCP / binary / 3E, 2026-09-06 and 2026-09-07."
    ),
    model_codes={
        0x4A21: "FX5U-32MR/ES",
        0x4A23: "FX5U-64MR/ES",
        0x4A24: "FX5U-80MR/ES",
        0x4A29: "FX5U-32MT/ES",
        0x4A2B: "FX5U-64MT/ES",
        0x4A2C: "FX5U-80MT/ES",
        0x4A31: "FX5U-32MT/ESS",
        0x4A33: "FX5U-64MT/ESS",
        0x4A34: "FX5U-80MT/ESS",
        0x4A41: "FX5U-32MR/DS",
        0x4A43: "FX5U-64MR/DS",
        0x4A44: "FX5U-80MR/DS",
        0x4A49: "FX5U-32MT/DS",
        0x4A4B: "FX5U-64MT/DS",
        0x4A4C: "FX5U-80MT/DS",
        0x4A51: "FX5U-32MT/DSS",
        0x4A53: "FX5U-64MT/DSS",
        0x4A54: "FX5U-80MT/DSS",
    },
    measured=True,
)
"""The measured baseline. ``0x4A49`` is the only model code ever seen off a wire here."""
