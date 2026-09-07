"""``0801`` Monitor Registration and ``0802`` Execute Monitor.

Layer 2. A two-step device monitor: send the device list once with ``0801``, then read
it back as often as you like with ``0802``, whose entire request payload is the command
and the subcommand. The saving is bandwidth, not latency -- the response is byte-for-byte
the ``0403`` response for the registered list (SH(NA)-080956ENG-M section 6.6 pp.62-68).

**On an iQ-F this never reaches the wire.** Both commands were sent to an
FX5U-32MT/DS on firmware 1.065 and both returned ``0xC059`` -- "there is a command or
subcommand that cannot be used by the CPU module" -- measured twice through independent
code paths (2026-09-06)::

    0801 req : 50 00 00 FF FF 03 00 14 00 10 00 01 08 00 00 00 03 ...
    0801 resp: D0 00 00 FF FF 03 00 0B 00 59 C0 00 FF FF 03 00 01 08 00 00
    0802 req : 50 00 00 FF FF 03 00 06 00 10 00 02 08 00 00
    0802 resp: D0 00 00 FF FF 03 00 0B 00 59 C0 00 FF FF 03 00 02 08 00 00

Note that ``0802`` answered ``0xC059`` and **not** the ``0xC05D`` "monitor not
registered" a reader of the generic reference would expect: the command itself is
rejected, so there is no way to mistake it for "you forgot to register". The iQ-F
profiles therefore carry :attr:`~aslmp.profile.Capability.MONITOR` as a
:class:`~aslmp.profile.Refusal`, and :meth:`RegisterMonitor.validate` raises
:class:`~aslmp.errors.SlmpCapabilityError` before a byte is built.

**It is never emulated.** ``0403`` returns the same values and would be a plausible
substitute, and substituting it is exactly the silent lie this library refuses: a caller
who asked for a monitor registration and got a Read Random has different bandwidth,
different limits and a different failure mode from the one they asked for. The refusal
names ``read_random()`` as the alternative and leaves the choice to the caller.

**Registration is CPU state, not connection state.** SH(NA)-080956ENG-M p.66: *"If the
access destination is restarted, the registered data will be deleted. Execute Entry
Monitor Device again and register the device to read."* There is no handle, no
generation counter and no per-connection scoping, so two clients registering different
lists against one CPU clobber each other. :class:`MonitorRegistration` records what
*this* client registered; it cannot know whether that is still what the CPU holds, and
nothing here re-registers automatically after a ``0xC05D``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from aslmp.commands.base import (
    Command,
    EncodeContext,
    expect_empty_payload,
    render_addresses,
)
from aslmp.commands.random import (
    RandomPoint,
    RandomValue,
    decode_point_values,
    point_specs,
    point_specs_len,
    validate_points,
)
from aslmp.errors import SlmpConfigurationError, SlmpDeviceNotAllowedHereError
from aslmp.profile import Capability
from aslmp.wire.citations import Citation, Measurement, Source
from aslmp.wire.codec import Unit
from aslmp.wire.devicetable import DeviceType

__all__ = [
    "MONITOR_LAYOUT",
    "MONITOR_REFUSED_ON_IQF",
    "ExecuteMonitor",
    "MonitorRegistration",
    "RegisterMonitor",
]


MONITOR_LAYOUT: Citation = Citation(
    manual="SH(NA)-080956ENG",
    revision="M",
    section="6.6 pp.62-68",
    note=(
        "Monitor Registration 0801H takes exactly the 0403 request layout -- the word "
        "access point count, the double-word access point count and then the device "
        "specifications -- and has no response data. Execute Monitor 0802H has no "
        "request data at all beyond the command and subcommand 0000H, and its response "
        "is the 0403 response for the registered list. p.63 excludes TS, TC, LTS, LTC, "
        "LTN, STS, STC, LSTS, LSTC, LSTN, CS, CC, LCS, LCC and LCN from registration, a "
        "broader list than 0403's. p.66: 0802 without a prior 0801 is an error, and a "
        "restart of the access destination deletes the registration."
    ),
)
"""The pair's layout, its exclusions and its lifetime."""

MONITOR_REFUSED_ON_IQF: Measurement = Measurement(
    cpu="FX5U-32MT/DS",
    firmware="1.065",
    date="2026-09-06",
    note=(
        "0801 and 0802 both returned end code 0xC059, measured twice through "
        "independent code paths. JY997D56001-K's command list does not mention either "
        "command, and its section 4.1 goes straight from Device Write Block 1406H to "
        "Remote Run 1001H. 0802 answering 0xC059 rather than 0xC05D is useful: the "
        "command is rejected outright, so it cannot be mistaken for a missing "
        "registration. See ambiguity A-IQF-MONITOR."
    ),
)
"""Why the iQ-F profiles refuse this capability rather than attempting it."""

_CITES: tuple[Source, ...] = (MONITOR_LAYOUT, MONITOR_REFUSED_ON_IQF)

_LONG_CURRENT_VALUES: frozenset[str] = frozenset({"LTN", "LSTN", "LCN"})
"""Refused here even though ``DeviceType.monitor_ok`` allows them.

:data:`MONITOR_LAYOUT` lists LTN, LSTN and LCN among the devices that cannot be
registered -- a broader exclusion than ``0403``'s, which does accept the long current
values. ``data/devices.tsv`` carries ``monitor_ok = yes`` for all three, which
contradicts that page; the discrepancy is reported against build unit U1 and refused in
the safe direction here until the table is corrected.
"""


def _monitor_ok(dt: DeviceType) -> bool:
    """The ``0801`` device gate: ``0403``'s exclusions plus the long current values."""
    return dt.monitor_ok and dt.name not in _LONG_CURRENT_VALUES


@dataclass(frozen=True, slots=True)
class MonitorRegistration:
    """What this client last asked one CPU to monitor, and how to decode the answer.

    Frozen, and deliberately not a handle: SLMP issues none. It is the request shape
    :class:`ExecuteMonitor` needs in order to parse a response that carries no framing of
    its own, plus the profile key it was registered against so that carrying one into a
    different CPU is visible rather than silent.

    It is **not** evidence that the CPU still holds this list. A restart clears the
    registration and another client can replace it; the next ``0802`` then fails, and
    this library surfaces that end code rather than re-registering behind the caller.
    """

    points: tuple[RandomPoint, ...]
    profile_key: str
    subcommand: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "points", tuple(self.points))
        if not self.points:
            raise SlmpConfigurationError(
                "a MonitorRegistration with no points describes nothing to read"
            )

    def __str__(self) -> str:
        return f"monitor({render_addresses(self.points)}) on {self.profile_key}"


@dataclass(frozen=True, slots=True)
class RegisterMonitor(Command[MonitorRegistration]):
    """``0801``: register a device list on the access destination. No response data.

    ``mutates`` is **True**. Nothing in device memory changes, but module state does:
    the previous registration is replaced, for every client talking to that CPU. A
    mid-flight failure therefore leaves the outcome genuinely unknown, which is what
    ``mutates`` exists to say.
    """

    points: tuple[RandomPoint, ...]

    CODE = 0x0801
    NAME = "Monitor Registration"
    mutates = True
    CITES = _CITES

    def __post_init__(self) -> None:
        object.__setattr__(self, "points", tuple(self.points))

    def subcommand(self, ctx: EncodeContext) -> int:
        return ctx.subcommand(Unit.WORD)

    def validate(self, ctx: EncodeContext) -> None:
        validate_points(
            self.points,
            ctx,
            what=self.describe(),
            command=self.CODE,
            capability=Capability.MONITOR,
            allowed=_monitor_ok,
            limit_command=0x0403,
        )

    def payload_len(self, ctx: EncodeContext) -> int:
        return point_specs_len(self.points, ctx)

    def encode(self, ctx: EncodeContext) -> bytes:
        return point_specs(self.points, ctx)

    def decode(self, payload: bytes, ctx: EncodeContext) -> MonitorRegistration:
        expect_empty_payload(payload, what=self.describe())
        return MonitorRegistration(
            points=tuple(point.resolve(ctx) for point in self.points),
            profile_key=ctx.profile.key,
            subcommand=self.subcommand(ctx),
        )

    def describe(self) -> str:
        return f"monitor_register({render_addresses(self.points)})"


@dataclass(frozen=True, slots=True)
class ExecuteMonitor(Command[tuple[RandomValue, ...]]):
    """``0802``: read the registered list back. Four bytes of request, in binary.

    The subcommand is ``0000`` and only ``0000`` -- there is no bit variant, no long
    device specification and no extension form, because the request carries no device
    specification to lengthen.
    """

    registration: MonitorRegistration

    CODE = 0x0802
    NAME = "Execute Monitor"
    mutates = False
    CITES = _CITES
    SUBCOMMAND: ClassVar[int] = 0x0000

    def subcommand(self, ctx: EncodeContext) -> int:
        del ctx
        return self.SUBCOMMAND

    def validate(self, ctx: EncodeContext) -> None:
        what = self.describe()
        ctx.profile.require(Capability.MONITOR, what=what)
        if self.registration.profile_key != ctx.profile.key:
            raise SlmpConfigurationError(
                f"this registration was made against {self.registration.profile_key} "
                f"and is being executed against {ctx.profile.key}. SLMP issues no "
                f"registration handle, so the only thing that can parse a 0802 response "
                f"is the request that registered it; a list registered against a "
                f"different device layout decodes into plausible values from the wrong "
                f"registers ({MONITOR_LAYOUT.reference})."
            )
        for point in self.registration.points:
            address = ctx.address(point.address)
            if not _monitor_ok(address.type):
                raise SlmpDeviceNotAllowedHereError(
                    f"{address.type.name} cannot be registered for monitoring "
                    f"({MONITOR_LAYOUT.reference})"
                )

    def payload_len(self, ctx: EncodeContext) -> int:
        del ctx
        return 0

    def encode(self, ctx: EncodeContext) -> bytes:
        del ctx
        return b""

    def decode(self, payload: bytes, ctx: EncodeContext) -> tuple[RandomValue, ...]:
        return decode_point_values(
            self.registration.points, payload, ctx, what=self.describe()
        )

    def describe(self) -> str:
        return f"monitor_read({render_addresses(self.registration.points)})"
