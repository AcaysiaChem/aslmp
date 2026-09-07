"""``aslmp verify-ranges`` -- measure a CPU's real device ranges against the profile.

**Static range tables go stale by construction.** On an iQ-R every device range is
repartitionable in GX Works3, and ``R`` and ``ZR`` default to *zero* points, so the SLMP
reference manual's own ``ZR16384`` example fails on an out-of-the-box iQ-R. On an iQ-F
the ranges are fixed by the model, but the model we measured is one of four in the
family. DESIGN section 7 lists this as a residual weakness shipped knowingly: the
default table will be wrong for someone on day one. This command is the mitigation.

**Method.** For each device family the profile says is present, one 1-point read at the
declared last index, one at the declared last + 1, and -- if the answers disagree with
the table -- a binary search for the real boundary. A read that succeeds proves the
point exists; ``0xC056`` proves it does not. Nothing is written and no CPU state changes.

**It reads devices the profile would normally refuse**, so it runs with
``validate_ranges=False``. That flag turns off the *range* table and nothing else:
device existence and the bit-versus-word rule are properties of the silicon, not of the
parameter file, and stay enforced. A family the profile says is absent is skipped rather
than probed, because ``V``, ``ZR``, ``DX`` and ``DY`` on an iQ-F answer ``0xC05C`` and a
sweep across them would be one long list of the same refusal.

The output is a table plus, with ``--python``, a paste-ready
:meth:`~aslmp.profile.CpuProfile.with_ranges` call.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from aslmp.errors import SlmpEndCodeError, SlmpUsageError
from aslmp.tools import EXIT_OK
from aslmp.tools._common import (
    add_connection_arguments,
    build_client,
    columns,
    guarded,
    parse_or_exit,
)

if TYPE_CHECKING:
    from aslmp.client import Plc
    from aslmp.wire.devicetable import DeviceType, Radix

__all__ = ["Finding", "build_parser", "run"]


@dataclass(frozen=True, slots=True)
class Finding:
    """What one device family's boundary turned out to be."""

    device: str
    declared_last: int | None
    measured_last: int | None
    reads: int
    note: str

    @property
    def agrees(self) -> bool:
        """Whether the shipped table and the CPU say the same thing."""
        return self.declared_last == self.measured_last


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aslmp verify-ranges",
        description=(
            "Probe a CPU for its real device range boundaries and compare them with the "
            "shipped profile. Reads only; writes nothing."
        ),
        epilog=(
            "Every probe is a 1-point read. A run over a full iQ-F profile is a few "
            "hundred round trips, about 3 seconds at the measured 7 ms median. Point it "
            "at a CPU you are allowed to talk to; it does not care whether it is in RUN."
        ),
    )
    add_connection_arguments(parser)
    parser.add_argument(
        "--device",
        action="append",
        default=[],
        metavar="NAME",
        help="only this device family, e.g. D (repeatable; default: every present family)",
    )
    parser.add_argument(
        "--ceiling",
        type=int,
        default=1 << 22,
        metavar="N",
        help="stop searching upward at this index (default: 4194304)",
    )
    parser.add_argument(
        "--python",
        action="store_true",
        help="also print a profile.with_ranges(...) call for what was measured",
    )
    return parser


class _Probe:
    """One device family's 1-point reads, and how many of them were spent.

    A small object rather than a closure because a closure defined inside the family
    loop would capture the loop variable, which is a real bug the moment anything here
    becomes concurrent -- and it cannot become concurrent: the in-flight gate allows one
    transaction per TCP connection, because two requests written before the first
    response is read return ONE response, for the LAST request, with end code 0x0000
    (FX5U-32MT/DS fw 1.065).
    """

    __slots__ = ("_plc", "_radix", "_type", "reads")

    def __init__(self, plc: Plc, device_type: DeviceType, radix: Radix) -> None:
        self._plc = plc
        self._type = device_type
        self._radix = radix
        self.reads = 0

    async def readable(self, index: int) -> bool:
        """Whether one point at ``index`` reads back with end code 0x0000."""
        from aslmp.wire.address import DeviceAddress
        from aslmp.wire.codec import Unit

        self.reads += 1
        address = DeviceAddress.of(self._type, index, radix=self._radix)
        present = True
        try:
            if self._type.unit is Unit.BIT:
                await self._plc.read_bits(address, 1)
            else:
                await self._plc.read_words(address, 1)
        except SlmpEndCodeError:
            # The CPU answered, in its own words, that this point is not there. Recorded
            # rather than returned from the handler: a refusal IS this function's result,
            # not a default substituted for one, and the single exit below is what makes
            # that readable. Nothing retries and nothing is swallowed -- the caller is
            # asking exactly this question.
            present = False
        except SlmpUsageError:
            # Refused before the wire: a family this CPU does not have, or a bit device
            # asked for in word units. Also an answer to the question, not a failure.
            present = False
        return present


async def _boundary(probe: _Probe, first: int, declared_last: int, ceiling: int) -> int:
    """The highest readable index at or above ``first``, found in O(log n) reads.

    Starts at the profile's declared boundary rather than at zero, so a table that is
    right costs two reads. When the CPU has MORE than the table says, the search doubles
    upward -- the table is what is under test, so it is never used as a ceiling.
    """
    if not await probe.readable(declared_last + 1):
        low, high = first, declared_last + 1
    else:
        low = declared_last + 1
        step = 1
        while low + step <= ceiling and await probe.readable(low + step):
            low += step
            step *= 2
        high = min(low + step, ceiling)
    while low + 1 < high:
        middle = (low + high) // 2
        if await probe.readable(middle):
            low = middle
        else:
            high = middle
    return low


def run(argv: Sequence[str]) -> int:
    args = parse_or_exit(build_parser(), argv)

    async def body() -> int:
        plc = build_client(args, validate_ranges=False)
        wanted = {name.upper() for name in args.device}
        findings: list[Finding] = []
        async with plc:
            profile = plc.profile
            for name, declared in sorted(profile.devices.items()):
                if wanted and name not in wanted:
                    continue
                if not declared.present or declared.points == 0:
                    continue
                probe = _Probe(plc, declared.type, profile.radix_for(declared.type))
                first = declared.first if declared.first is not None else 0
                if not await probe.readable(first):
                    findings.append(
                        Finding(
                            name, declared.last, None, probe.reads, "first point unreadable"
                        )
                    )
                    continue
                declared_last = declared.last if declared.last is not None else first
                measured = await _boundary(probe, first, declared_last, args.ceiling)
                findings.append(Finding(name, declared.last, measured, probe.reads, ""))
            profile_key = profile.key

        header = ["device", "profile last", "measured last", "reads", "verdict"]
        rows: list[list[str]] = [header]
        for finding in findings:
            verdict = "agrees" if finding.agrees else "DIFFERS"
            if finding.note:
                verdict = f"{verdict} -- {finding.note}"
            rows.append(
                [
                    finding.device,
                    "--" if finding.declared_last is None else str(finding.declared_last),
                    "--" if finding.measured_last is None else str(finding.measured_last),
                    str(finding.reads),
                    verdict,
                ]
            )
        print(columns(rows))
        differing = [finding for finding in findings if not finding.agrees]
        print(
            f"\n{len(findings)} device famil(ies) probed, "
            f"{len(differing)} disagree with "
            f"{profile_key}."
        )
        if differing and not args.python:
            print(
                "Pass --python for a profile.with_ranges(...) call carrying what was "
                "measured."
            )
        if args.python:
            print(_python_for(profile_key, findings))
        return EXIT_OK

    return guarded(body)


def _python_for(profile_key: str, findings: Sequence[Finding]) -> str:
    """A paste-ready ``with_ranges`` call for the measured boundaries.

    Printed rather than written to a file, and never applied automatically: replacing a
    shipped range table on the strength of one sweep is exactly the kind of silent
    substitution the rest of this library refuses to do.
    """
    lines = [
        "",
        "# Measured on the CPU this command just talked to. Paste it where you build",
        "# the client; it replaces the shipped table for these families only.",
        "from aslmp.profile import DeviceRange, Evidence",
        "from aslmp.profiles import by_key",
        "from aslmp.wire.citations import Provenance",
        "",
        f"profile = by_key({profile_key!r}).with_ranges({{",
    ]
    for finding in findings:
        if finding.measured_last is None:
            continue
        lines.append(
            f"    {finding.device!r}: DeviceRange("
            f"device={finding.device!r}, present=True, first=0, "
            f"last={finding.measured_last}, points={finding.measured_last + 1},"
        )
        lines.append(
            "        evidence=Evidence(provenance=Provenance.LIVE, "
            'source="measured by aslmp verify-ranges"),'
        )
        lines.append("    ),")
    lines.append("})")
    return "\n".join(lines)
