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

import enum
from dataclasses import dataclass

from aslmp.commands.batch import ReadWords
from aslmp.commands.info import TypeName
from aslmp.errors import SlmpPayloadShapeError, SlmpProfileMismatchError
from aslmp.profile import CpuProfile, Family
from aslmp.profiles import by_model_code
from aslmp.wire.citations import Citation

__all__ = [
    "CPU_STATUS_REGISTER",
    "SD203",
    "CpuIdentity",
    "CpuStatus",
    "cpu_status_command",
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
