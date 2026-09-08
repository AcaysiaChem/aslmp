"""``aslmp capabilities`` -- what a profile can do, with the evidence for each claim.

DESIGN section 5.11 requires that every path we could not test on iron ships labelled:
in the profile as ``Evidence(provenance=MANUAL)``, in the docstring, in the README, and
**in this command's output**. That last one is the only label a user sees without
reading source, so this is where the honesty either happens or does not.

Three states, and they are different things:

``allowed``
    The CPU can do it, and the row says whether we watched it do so (``live``) or read
    that it could (``manual``).
``refused``
    The profile refuses it pre-transport, with the reason and the end code the CPU would
    have returned. ``0x0801`` / ``0x0802`` on an iQ-F are the case that makes this a
    mechanism: they answer ``0xC059``, not the ``0xC05D`` a reader of the generic SLMP
    reference would expect, and the tempting fix -- emulating them with a ``0x0403`` --
    is the silent substitution this library forbids.
``unstated``
    The profile says nothing, and :meth:`~aslmp.profile.CpuProfile.require` treats that
    as a refusal too.

No socket is opened. This is what the shipped profile claims, not what your CPU does;
``aslmp probe`` and ``aslmp verify-ranges`` are the commands that ask the silicon.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from aslmp.tools import EXIT_OK
from aslmp.tools._common import columns, parse_or_exit, usage

__all__ = ["build_parser", "run"]

_UNVERIFIED_NOTE = """
Rows marked `manual` or `inferred` were never sent to hardware by us. Our iron is ONE
FX5U-32MT/DS on firmware 1.065, binary 3E and 4E, over TCP and UDP. We have no iQ-R and
no ASCII connection entry (Communication Data Code is a port-wide own-node setting on
iQ-F, so ASCII cannot coexist with the binary entries we needed).

Remote control, said precisely, because this note used to say we had never sent one:
0x1001 RUN, 0x1002 STOP and 0x1003 PAUSE WERE sent to that CPU on 2026-09-07, from the
laptop at 192.168.10.41 over Wi-Fi, on TCP entries 5003 and 5004, and each did what it
says -- checked against the free-running scan counter the PLC program keeps in D8, which
only the CPU can advance, and not only against the SD203 the library itself reads.

Still not verified, and each for its own reason:
  * 0x1005 Latch Clear and 0x1006 Remote RESET have never been sent, and are not going
    to be on a machine nobody is watching.
  * The manual's claim that Remote RUN answers 0x0000 while the CPU's switch is in STOP
    and the CPU does not run (SH(NA)-080956ENG-M p.131) -- the reason verify=True is the
    default -- was never forced: our bench switch is in RUN and RUN was truthful there.

Those paths are implemented, gated and labelled. Labelled is not verified.
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aslmp capabilities",
        description=(
            "Print what a CPU profile allows and refuses, and the evidence behind each "
            "row. Opens no socket."
        ),
    )
    parser.add_argument(
        "profile",
        nargs="?",
        help="a profile key, e.g. melsec:iq-f/fx5u. Omit with --all for every profile",
    )
    parser.add_argument("--all", action="store_true", help="every shipped profile")
    parser.add_argument(
        "--unverified-only",
        action="store_true",
        help="only the rows whose provenance is not a measurement on real hardware",
    )
    return parser


def run(argv: Sequence[str]) -> int:
    args = parse_or_exit(build_parser(), argv)
    from aslmp.profile import Capability, Evidence, Refusal
    from aslmp.profiles import ALL, KEYS, by_key
    from aslmp.wire.citations import Provenance

    if args.all:
        profiles = list(ALL.values())
    elif args.profile:
        profiles = [by_key(args.profile)]
    else:
        return usage(f"name a profile ({', '.join(KEYS)}) or pass --all")

    for profile in profiles:
        print(f"{profile.key}  --  {profile.description}")
        print(f"  family {profile.family.value}, default spec {profile.default_spec.value}")
        print(
            "  encodings: "
            + ", ".join(sorted(enc.value for enc in profile.allowed_encodings))
        )
        rows: list[list[str]] = [["  capability", "state", "provenance", "evidence"]]
        for capability in Capability:
            entry = profile.capabilities.get(capability)
            if entry is None:
                if args.unverified_only:
                    continue
                rows.append(
                    ["  " + capability.value, "unstated", "--", "not claimed by this profile"]
                )
                continue
            if isinstance(entry, Refusal):
                end = (
                    ""
                    if entry.end_code_if_attempted is None
                    else f" (would be 0x{entry.end_code_if_attempted:04X})"
                )
                provenance = entry.evidence.provenance
                if args.unverified_only and provenance is Provenance.LIVE:
                    continue
                rows.append(
                    [
                        "  " + capability.value,
                        "REFUSED",
                        provenance.value,
                        f"{entry.reason}{end} [{entry.evidence.source}]",
                    ]
                )
                continue
            evidence: Evidence = entry
            if args.unverified_only and evidence.provenance is Provenance.LIVE:
                continue
            rows.append(
                [
                    "  " + capability.value,
                    "allowed",
                    evidence.provenance.value,
                    f"{evidence.source}{' -- ' + evidence.note if evidence.note else ''}",
                ]
            )
        print(columns(rows))
        counts = profile.provenance_counts()
        print(
            "  provenance across every fact in this profile: "
            + ", ".join(f"{key.value} {counts.get(key, 0)}" for key in Provenance)
        )
        print()

    print(_UNVERIFIED_NOTE, end="")
    return EXIT_OK
