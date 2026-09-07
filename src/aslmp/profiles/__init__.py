"""The shipped CPU profiles, and the two ways to reach one.

Layer 1. Importing this package builds every profile, which is a few hundred frozen
dataclasses and no file I/O; ``aslmp.profile`` and ``aslmp.profiles`` are importable in
a process that has no event loop and no socket.

**There is no generic profile, and ``by_key`` has no fallback.** A user must name their
CPU, and the point of this module is to make that a good experience rather than a
guessing game: an unknown name is refused with the list of names that exist, and an
unknown model code is refused with :class:`~aslmp.errors.SlmpProfileMismatchError`
naming ``aslmp identify``.

The reason is one measured fact. ``X`` and ``Y`` are octal on an iQ-F and hexadecimal on
every other family, so the literal ``Y20`` is output 16 on an FX5U and output 32 on an
iQ-R -- and **both CPUs answer end code 0x0000**. There is no response, no timing and no
byte anywhere on the wire that tells the two apart. A library that guessed the family
from a model-code prefix would be silently two outputs off on an unrecognised FX5, and
worse as the address grows.

``by_model_code`` is the connect handshake's half of the same rule: ``0x0101`` Read Type
Name returns the code, it must be one this profile claims, and if it is not, connect
raises rather than switching profiles under the caller.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from aslmp.errors import SlmpConfigurationError, SlmpProfileMismatchError
from aslmp.profile import CpuProfile
from aslmp.profiles.fx5s import FX5S
from aslmp.profiles.fx5u import FX5U
from aslmp.profiles.fx5uc import FX5UC
from aslmp.profiles.fx5uj import FX5UJ
from aslmp.profiles.iq_r import IQ_R, IQR_R00
from aslmp.profiles.l import L
from aslmp.profiles.q import Q

__all__ = [
    "ALL",
    "FX5S",
    "FX5U",
    "FX5UC",
    "FX5UJ",
    "IQR_R00",
    "IQ_R",
    "KEYS",
    "L",
    "Q",
    "by_key",
    "by_model_code",
    "claiming",
]

_PROFILES: Final[tuple[CpuProfile, ...]] = (
    FX5U,
    FX5UC,
    FX5UJ,
    FX5S,
    IQ_R,
    IQR_R00,
    Q,
    L,
)


def _build_registry() -> Mapping[str, CpuProfile]:
    registry: dict[str, CpuProfile] = {}
    for profile in _PROFILES:
        if profile.key in registry:  # pragma: no cover - a typo in this module only
            raise ValueError(f"two profiles share the key {profile.key!r}")
        registry[profile.key] = profile
    return MappingProxyType(registry)


ALL: Final[Mapping[str, CpuProfile]] = _build_registry()
"""Every shipped profile, keyed by its canonical name, in family order."""

KEYS: Final[tuple[str, ...]] = tuple(ALL)
"""The valid strings for ``Plc(profile=...)``, in the order this module lists them."""


# ----------------------------------------------------------------------------------------
# Model-code ownership
#
# IQ_R and IQR_R00 both claim 0x48A0-0x48A2: the R00 group is a NARROWER range table for
# CPUs the family profile also covers, and either declaration is a truthful thing for a
# user to write. by_model_code has to answer with exactly one profile, so ownership is
# declared here rather than derived, and the duplicate is listed explicitly instead of
# being resolved by whichever profile happened to be built first. `claiming()` returns
# both, which is what `aslmp identify` prints.
# ----------------------------------------------------------------------------------------

_MODEL_CODE_OWNERS: Final[tuple[CpuProfile, ...]] = (FX5U, FX5UC, FX5UJ, FX5S, IQ_R, Q, L)


def _build_model_codes() -> Mapping[int, CpuProfile]:
    owners: dict[int, CpuProfile] = {}
    for profile in _MODEL_CODE_OWNERS:
        for code in profile.model_codes:
            existing = owners.get(code)
            if existing is not None:  # pragma: no cover - a typo in this module only
                raise ValueError(
                    f"model code 0x{code:04X} is claimed for lookup by both "
                    f"{existing.key!r} and {profile.key!r}. by_model_code must have "
                    f"exactly one answer; add the narrower profile to KEYS and leave it "
                    f"out of _MODEL_CODE_OWNERS."
                )
            owners[code] = profile
    return MappingProxyType(owners)


_BY_MODEL_CODE: Final[Mapping[int, CpuProfile]] = _build_model_codes()


def by_key(key: str) -> CpuProfile:
    """The profile named ``key``, or raise listing every name that exists.

    Exact match only. Nothing here normalises case, strips a prefix or accepts
    ``"fx5u"`` for ``"melsec:iq-f/fx5u"``: an alias is a second name for a profile, and
    the day someone adds a second FX5 variant one of the two aliases becomes ambiguous
    silently. The *message* is where the help goes -- a near miss is named -- and the
    resolution is always a real key.
    """
    if not isinstance(key, str):
        raise TypeError(f"a profile key is a str, not {type(key).__name__}")
    found = ALL.get(key)
    if found is not None:
        return found
    lowered = key.strip().lower()
    near = [name for name in KEYS if lowered and lowered in name]
    hint = f" Did you mean {' or '.join(repr(n) for n in near)}?" if near else ""
    raise SlmpConfigurationError(
        f"{key!r} is not a CPU profile. There is deliberately no generic profile: X and "
        f"Y are octal on an iQ-F and hexadecimal everywhere else, and a wrong guess is "
        f"silently off by two outputs at Y20 with end code 0x0000. Name one of "
        f"{', '.join(repr(name) for name in KEYS)}.{hint} "
        f"`aslmp identify <host>` prints the string to pass."
    )


def by_model_code(code: int) -> CpuProfile:
    """The profile that owns model code ``code``, or raise. Never a fallback.

    ``code`` is the second field of an ``0x0101`` Read Type Name response. An
    unrecognised one raises :class:`~aslmp.errors.SlmpProfileMismatchError`, including
    for ``0x0360`` RCPU, which identifies a *family* rather than a model and is
    deliberately claimed by no profile.
    """
    if not isinstance(code, int) or isinstance(code, bool):
        raise TypeError(f"a model code is an int, not {type(code).__name__}")
    found = _BY_MODEL_CODE.get(code)
    if found is not None:
        return found
    raise SlmpProfileMismatchError(
        f"model code 0x{code:04X} is in no shipped profile. There is no generic "
        f"profile and no radix guess: an unrecognised FX5 read as hexadecimal X/Y is "
        f"silently wrong from Y10 onwards, with end code 0x0000. Run "
        f"`aslmp identify <host>` and report the model name it prints together with "
        f"this code so the table can gain a row. 0x0360 (RCPU) is excluded on purpose: "
        f"it names a family, not a model.",
        model_code=code,
        model="",
        profile_key="",
    )


def claiming(code: int) -> tuple[CpuProfile, ...]:
    """Every profile that would accept ``code`` at connect, in registry order.

    Usually one. It is two for the R00/R01/R02 codes, which both ``melsec:iq-r`` and
    the narrower ``melsec:iq-r/r00`` claim; ``by_model_code`` answers with the family
    profile and this function is how ``aslmp identify`` offers the other.
    """
    if not isinstance(code, int) or isinstance(code, bool):
        raise TypeError(f"a model code is an int, not {type(code).__name__}")
    return tuple(profile for profile in _PROFILES if code in profile.model_codes)
