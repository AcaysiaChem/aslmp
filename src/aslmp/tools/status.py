"""``aslmp status`` -- what state a CPU is in, and whether it is reporting an error.

``aslmp probe`` answers whether a connection entry is served, which is a different
question. On 2026-09-25 the bench FX5U probed perfectly healthy -- entry free, 7.7 ms
round trips -- while it ran with a self-diagnostic error raised continuously for a module
left unpowered. Nothing in ``probe``'s output hinted at it, because nothing about the
connection was wrong.

This reads the other answer in a single ``0403``: operating state, the error flag, the
latest error code and when it was stamped, and the PLC's clock -- one instant, not five
reads that can disagree about a CPU that changed between them. The layout was measured
on an iQ-F CPU (:data:`aslmp.identity.CPU_DIAGNOSTICS_MEASURED`); on any other family
only the operating state is read, and the output says why.

Read-only. It never clears an error: ``clear_error`` is a write, and a fault that
persists comes straight back.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import datetime

from aslmp.tools import EXIT_OK
from aslmp.tools._common import (
    add_connection_arguments,
    build_client,
    columns,
    guarded,
    parse_or_exit,
)

__all__ = ["build_parser", "run"]

_UNSET_BEFORE = 2000
"""A PLC clock earlier than this has almost certainly never been set. The bench FX5U's
read 1980-01-22 on 2026-09-26."""

_NO_MEANING = """
aslmp has no table of self-diagnostic codes and does not guess what 0x{code:04X} means.
GX Works3 names it under Diagnostics -> Module Diagnostics.
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aslmp status",
        description=(
            "Read the CPU's operating state, error flag, latest self-diagnostic error "
            "code and its time stamp, and its clock, in one 0x0403 snapshot. Reads only."
        ),
    )
    add_connection_arguments(parser)
    return parser


def _when(stamp: datetime | None) -> str:
    return "not a valid date" if stamp is None else f"{stamp:%Y-%m-%d %H:%M:%S}"


def _clock_line(clock: datetime | None) -> str:
    if clock is None:
        return "SD210-SD216 do not hold a valid date"
    line = _when(clock)
    if clock.year < _UNSET_BEFORE:
        line += (
            f" -- before {_UNSET_BEFORE}, so almost certainly never set; error stamps "
            f"are only meaningful relative to it"
        )
    return line


def run(argv: Sequence[str]) -> int:
    args = parse_or_exit(build_parser(), argv)

    async def body() -> int:
        from aslmp.profile import Family

        plc = build_client(args)
        async with plc:
            info = plc.info
            if info is None:  # pragma: no cover -- connect() raises instead
                raise RuntimeError("connected without a ConnectionInfo")
            rows = [["peer", f"{info.peer[0]}:{info.peer[1]}"]]
            identity = plc.identity
            if identity is not None:
                rows.append(
                    ["cpu", f"{identity.model} (model code 0x{identity.model_code:04X})"]
                )

            if plc.profile.family is not Family.IQ_F:
                status = await plc.read_cpu_status()
                rows.append(["state", f"{status} (SD203 = {status.value})"])
                rows.append(
                    [
                        "error",
                        f"not read: the error and clock registers were measured on an "
                        f"iQ-F CPU only, and {plc.profile.family.value} is not assumed "
                        f"to match",
                    ]
                )
                print(columns(rows))
                return EXIT_OK

            found = await plc.read_diagnostics()
            rows.append(["state", f"{found.status} (SD203 = {found.status.value})"])
            if found.error:
                rows.append(
                    [
                        "error",
                        f"YES -- SM0 is ON; latest self-diagnostic code "
                        f"0x{found.error_code:04X} (SD0)",
                    ]
                )
                stamped = f"{_when(found.error_at)} by the PLC's clock"
                age = found.error_age_s
                if age is not None:
                    stamped += f", {age:.0f} s before this read"
                rows.append(["stamped", stamped])
            else:
                rows.append(["error", "none -- SM0 is OFF"])
            rows.append(["clock", _clock_line(found.clock)])
            print(columns(rows))
            if found.error:
                print(_NO_MEANING.format(code=found.error_code), end="")
        return EXIT_OK

    return guarded(body)
