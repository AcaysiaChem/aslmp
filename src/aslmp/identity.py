"""Who is on the other end of the socket, and what state it is in.

Layer 2. May import ``aslmp.wire``, ``aslmp.profile``, ``aslmp.profiles`` and
``aslmp.commands``. No I/O: these are the value types and the pure decoders that the
connect handshake and ``RemoteControl.status()`` build their answers out of.

**Identity is not a convenience.** ``profile=`` is a required constructor argument and
there is no generic profile, because ``X`` and ``Y`` are octal on an iQ-F and hexadecimal
on every other family: the literal ``Y20`` is output 16 on an FX5U and output 32 on an
iQ-R, and **both CPUs answer end code 0x0000**. Nothing on the wire distinguishes them.
So the ``0101`` half of the handshake exists to catch a caller who declared the wrong
family, and :func:`resolve_profile` raises
:class:`~aslmp.errors.SlmpProfileMismatchError` for a model code no profile claims
rather than guessing a radix from a prefix.

**CPU status comes from SD203, read as an ordinary word.** SLMP has no "what state are
you in" command: the operating status is a special register, and reading it is one
``0401`` (:func:`cpu_status_command`). That is the second round trip graft G15 spends on
``verify=True``, and it is what makes the difference between "Remote RUN returned
``0x0000``" and "the CPU is running" -- which Mitsubishi documents as two different
things.
"""

from __future__ import annotations

import calendar
import enum
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final, final

from aslmp.commands.batch import ReadWords
from aslmp.commands.info import TypeName
from aslmp.commands.random import RandomPoint, word
from aslmp.errors import SlmpPayloadShapeError, SlmpProfileMismatchError
from aslmp.profile import CpuProfile, Family
from aslmp.profiles import by_model_code
from aslmp.wire.citations import Citation, Measurement

__all__ = [
    "CPU_DIAGNOSTICS_MEASURED",
    "CPU_STATUS_REGISTER",
    "SD203",
    "CpuDiagnostics",
    "CpuIdentity",
    "CpuStatus",
    "cpu_diagnostics_points",
    "cpu_status_command",
    "decode_cpu_diagnostics",
    "decode_cpu_status",
    "resolve_profile",
]


CPU_STATUS_REGISTER: Citation = Citation(
    manual="JY997D55401",
    revision="unknown",
    section="Appendix: special registers, SD203",
    note=(
        "SD203 holds the CPU operating status: 0 = RUN, 1 = STEP-RUN, 2 = STOP, "
        "3 = PAUSE. SLMP has no command that reports CPU state, so the status is read "
        "as an ordinary word with 0401. The revision letter of JY997D55401 was not "
        "recorded by the survey that read it, which is why this citation says so rather "
        "than inventing one."
    ),
)
"""Where the four status values come from, and the gap in the citation."""

SD203: str = "SD203"
"""The special register the CPU's operating status lives in."""


class CpuStatus(enum.Enum):
    """The operating status of a CPU module, decoded from SD203.

    ``verify=True`` on remote run, stop and pause reads this back, because Mitsubishi
    documents Remote RUN as completing normally with the switch in STOP while the CPU
    does not run (SH(NA)-080956ENG-M p.131). Returning that end code as success would be
    a silent lie; comparing this against what was asked for is the second round trip
    that makes the answer true.
    """

    RUN = 0
    STEP_RUN = 1
    STOP = 2
    PAUSE = 3

    @property
    def running(self) -> bool:
        """Whether the program is executing. ``STEP_RUN`` counts; ``PAUSE`` does not."""
        return self in (CpuStatus.RUN, CpuStatus.STEP_RUN)

    def __str__(self) -> str:
        return self.name.replace("_", "-")


def cpu_status_command() -> ReadWords:
    """The ``0401`` that reads SD203. One word, one round trip, no side effects."""
    return ReadWords(SD203, 1)


def decode_cpu_status(words: tuple[int, ...]) -> CpuStatus:
    """Turn the SD203 word into a :class:`CpuStatus`, or raise naming the value.

    An undocumented value is **not** rounded to the nearest state. A client that reported
    "STOP" for a status it did not recognise would tell a caller that a machine is
    stopped on no evidence, which is the failure ``verify=True`` exists to prevent.
    """
    if len(words) != 1:
        raise SlmpPayloadShapeError(
            f"reading {SD203} returns exactly one word; got {len(words)}"
        )
    value = words[0]
    for status in CpuStatus:
        if status.value == value:
            return status
    known = ", ".join(f"{s.value} = {s}" for s in CpuStatus)
    raise SlmpPayloadShapeError(
        f"{SD203} holds {value} (0x{value:04X}), which is not a documented CPU "
        f"operating status ({known}; {CPU_STATUS_REGISTER.reference}). Refusing to "
        f"round it to the nearest state: 'the CPU is stopped' is not something to say "
        f"on no evidence. Please report this with the CPU model and firmware."
    )



CPU_DIAGNOSTICS_MEASURED: Final = Measurement(
    cpu="FX5U-32MT/DS",
    firmware="1.065",
    date="2026-09-26",
    host="192.168.10.41 (the development box)",
    note=(
        "The layout decode_cpu_diagnostics reads, observed rather than read out of a "
        "manual. With a module on the bench left unpowered the CPU stayed in RUN while "
        "reporting an error, and one 0403 returned: SM0 and SM1 ON; SD0 = 0x3081; "
        "SD1-SD7 = year, month, day, hour, minute, second and day of week of that error, "
        "each a plain binary word; SD203 = 0 (RUN); SD210-SD216 = the clock, in the same "
        "order and coding. First read word by word on 2026-09-25, when SD1-SD7 tracked "
        "SD210-SD216 to within a second across three reads eight seconds apart: a fault "
        "that persists is re-stamped continuously. Measured on this one CPU and applied "
        "to the iQ-F family, as SD203 already is; no other family has been measured, "
        "and read_diagnostics refuses them rather than assume the layout carries over."
    ),
)
"""Where the diagnostic register layout comes from: silicon, not a manual."""

_ERROR_REGISTERS: Final = tuple(f"SD{index}" for index in range(8))
"""SD0, the latest self-diagnostic error code, then SD1-SD7, when it was stamped."""

_CLOCK_REGISTERS: Final = tuple(f"SD{index}" for index in range(210, 217))
"""SD210-SD216: year, month, day, hour, minute, second, day of week."""

_SNAPSHOT_POINTS: Final = 1 + len(_ERROR_REGISTERS) + 1 + len(_CLOCK_REGISTERS)


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class CpuDiagnostics:
    """What state a CPU is in and whether it is reporting an error, as ONE instant.

    Read in a single ``0403``, so every field describes the same moment: a status taken
    from one read beside an error flag from another is how "RUN, no error" gets printed
    about a CPU that stopped in between. Asking the questions separately is also five
    round trips instead of one.

    ``error`` is SM0. ``error_code`` is SD0 exactly as read. This library has no table of
    self-diagnostic codes and does not guess what one means -- GX Works3's module
    diagnostics names it. ``error_at`` and ``clock`` are ``None`` when their registers do
    not form a real date, rather than a date made up to fill the field.

    Both times come from the PLC's own clock, which need not be right: the bench FX5U's
    read 1980-01-22 on 2026-09-26, evidently never set. :attr:`error_age_s` is therefore
    the difference between two readings of that clock taken in the same transaction, so
    it is right even when the date is not, and it never involves this host's clock.
    """

    status: CpuStatus
    error: bool
    error_code: int
    error_at: datetime | None
    clock: datetime | None

    @property
    def error_age_s(self) -> float | None:
        """Seconds from the latest error's stamp to the clock, both by the PLC's clock."""
        if self.error_at is None or self.clock is None:
            return None
        return (self.clock - self.error_at).total_seconds()


def cpu_diagnostics_points() -> tuple[RandomPoint, ...]:
    """The seventeen word points of one diagnostic snapshot, in the order they decode.

    SM0 is read as a word: SM0-SM15 packed, of which bit 0 is SM0. A ``0403`` carries
    word points only, and one word is the whole cost of the flag.
    """
    return (
        word("SM0", kind="bits"),
        *(word(name) for name in _ERROR_REGISTERS),
        word(SD203),
        *(word(name) for name in _CLOCK_REGISTERS),
    )


def decode_cpu_diagnostics(values: Sequence[object]) -> CpuDiagnostics:
    """Turn one snapshot into a :class:`CpuDiagnostics`, or raise naming what was wrong."""
    if len(values) != _SNAPSHOT_POINTS:
        raise SlmpPayloadShapeError(
            f"a diagnostic snapshot is {_SNAPSHOT_POINTS} word points; got {len(values)}"
        )
    flags = values[0]
    if not isinstance(flags, tuple) or not flags:
        raise SlmpPayloadShapeError(
            f"SM0 was read as a packed word of flags; got {flags!r}"
        )
    words: list[int] = []
    for position, value in enumerate(values[1:], start=1):
        if isinstance(value, bool) or not isinstance(value, int):
            raise SlmpPayloadShapeError(
                f"point {position} of a diagnostic snapshot should be a word; got {value!r}"
            )
        words.append(value)
    code, *stamp = words[: len(_ERROR_REGISTERS)]
    status = words[len(_ERROR_REGISTERS)]
    clock = words[len(_ERROR_REGISTERS) + 1 :]
    return CpuDiagnostics(
        status=decode_cpu_status((status,)),
        error=bool(flags[0]),
        error_code=code,
        error_at=_plc_time(stamp),
        clock=_plc_time(clock),
    )


def _plc_time(fields: Sequence[int]) -> datetime | None:
    """Year, month, day, hour, minute, second -- or ``None`` when they are not a date.

    ``None`` is a decoded fact, not a recovered error, and it is written as validation
    rather than as ``except ValueError`` for that reason: six registers that do not name a
    moment -- zeros, say -- have no date to report, and saying so is the reading. This
    library does not catch an exception to hand back something plausible.

    The seventh register, the day of the week, is ignored: it is implied by the other
    six, and a disagreement between them is not something this library can adjudicate.
    """
    year, month, day, hour, minute, second = fields[:6]
    if not (datetime.min.year <= year <= datetime.max.year and 1 <= month <= 12):
        return None
    if not 1 <= day <= calendar.monthrange(year, month)[1]:
        return None
    if not (0 <= hour < 24 and 0 <= minute < 60 and 0 <= second < 60):
        return None
    return datetime(year, month, day, hour, minute, second)


def resolve_profile(model_code: int) -> CpuProfile:
    """The profile that owns ``model_code``, or raise. Never a fallback.

    ``model_code`` is the second field of an ``0101`` Read Type Name response. An
    unrecognised one raises :class:`~aslmp.errors.SlmpProfileMismatchError` naming
    ``aslmp identify``; it never resolves to a family guessed from the code's high byte,
    because an unrecognised FX5 read as hexadecimal ``X``/``Y`` is silently wrong from
    ``Y10`` onwards with end code ``0x0000``.
    """
    return by_model_code(model_code)


@dataclass(frozen=True, slots=True)
class CpuIdentity:
    """Which CPU answered, as the CPU itself said it.

    ``model`` is the ``0101`` name with its trailing spaces stripped, ``raw`` is the
    16-character field exactly as it arrived, and ``family`` comes from the profile that
    claims ``model_code`` -- never from parsing the name, which is a marketing string
    whose shape has changed between families.
    """

    model: str
    model_code: int
    family: Family
    raw: bytes

    def __post_init__(self) -> None:
        object.__setattr__(self, "raw", bytes(self.raw))

    @classmethod
    def of(cls, type_name: TypeName) -> CpuIdentity:
        """Build an identity from a decoded ``0101`` response, resolving the family.

        Raises :class:`~aslmp.errors.SlmpProfileMismatchError` if no shipped profile
        claims the model code: an identity with a guessed family would be worse than no
        identity at all.
        """
        profile = resolve_profile(type_name.model_code)
        return cls(
            model=type_name.model,
            model_code=type_name.model_code,
            family=profile.family,
            raw=type_name.raw,
        )

    def check(self, profile: CpuProfile) -> None:
        """Raise unless ``profile`` claims this model code.

        The identify half of the connect handshake, and the reason declaring
        ``profile=`` does not carry its own silent-wrong-radix risk: a caller who names
        ``melsec:iq-r`` against an FX5U is told so in one round trip, rather than reading
        ``Y20`` as output 32 forever.
        """
        if self.model_code in profile.model_codes:
            return
        claimed = ", ".join(
            f"0x{code:04X} ({name})" for code, name in sorted(profile.model_codes.items())
        )
        raise SlmpProfileMismatchError(
            f"the CPU identified itself as {self.model!r}, model code "
            f"0x{self.model_code:04X}, and the profile in use is {profile.key}, which "
            f"claims {claimed}. Nothing here switches profiles under you: X and Y are "
            f"octal on an iQ-F and hexadecimal everywhere else, so the wrong profile "
            f"reads Y20 as a different output and the PLC answers 0x0000 either way. "
            f"Run `aslmp identify <host>` and pass the profile it prints.",
            model_code=self.model_code,
            model=self.model,
            profile_key=profile.key,
        )

    def __str__(self) -> str:
        return f"{self.model} (0x{self.model_code:04X}, {self.family.value})"
