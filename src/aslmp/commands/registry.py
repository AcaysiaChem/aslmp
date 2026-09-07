"""One catalogue of every command this package speaks, keyed by command code.

Layer 2. Shared by the client, the conformance simulator, the pass-through proxy and
``aslmp cite``, so that "which commands does aslmp implement, and where is each one
written down?" has exactly one answer and it is data rather than a docstring.

**Why a :class:`CommandSpec` rather than a bare class.** DESIGN section 1.5 asks for
``COMMANDS: Mapping[int, type[Command]]``, and one command code does not map to one
class: ``0401`` is :class:`~aslmp.commands.batch.ReadWords` in word units and
:class:`~aslmp.commands.batch.ReadBits` in bit units, ``1402`` is
:class:`~aslmp.commands.random.WriteRandom` and
:class:`~aslmp.commands.random.WriteRandomBits`, and ``2101`` Ondemand has no class at
all because an external device cannot send one. A mapping to a single class would have
to drop one of each pair silently. Each row therefore carries the code, Mitsubishi's own
name, the direction, whether it mutates the target, its citations and the classes that
implement it -- which is also exactly what ``aslmp cite 0x0403`` prints. The deviation is
recorded with build unit U7.

Every row is validated at import: the classes it lists must declare the code it is keyed
by, must agree with it about ``mutates``, and must each carry at least one citation.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final, Literal, TypeAlias

from aslmp.commands.base import Command
from aslmp.commands.batch import ReadBits, ReadWords, WriteBits, WriteWords
from aslmp.commands.block import ReadBlocks, WriteBlocks
from aslmp.commands.info import ClearError, ReadTypeName, SelfTest
from aslmp.commands.monitor import ExecuteMonitor, RegisterMonitor
from aslmp.commands.ondemand import ONDEMAND_COMMAND, ONDEMAND_LAYOUT
from aslmp.commands.password import LockPassword, UnlockPassword
from aslmp.commands.random import ReadRandom, WriteRandom, WriteRandomBits
from aslmp.commands.remote import (
    RemoteLatchClear,
    RemotePause,
    RemoteReset,
    RemoteRun,
    RemoteStop,
)
from aslmp.wire.citations import Ambiguity, Source

__all__ = ["COMMANDS", "CommandSpec", "Direction", "by_code", "codes"]

AnyCommand: TypeAlias = type[Command[Any]]
"""A command class, whatever it decodes to. ``Any`` here is the erased return type of a
heterogeneous table, not a value that escapes into a caller's hands."""

Direction: TypeAlias = Literal["request", "ondemand"]
"""``"request"`` -- the external device sends it. ``"ondemand"`` -- the PLC does."""


@dataclass(frozen=True, slots=True)
class CommandSpec:
    """One SLMP command code and everything this package knows about it."""

    code: int
    name: str
    mutates: bool
    direction: Direction
    commands: tuple[AnyCommand, ...]
    cites: tuple[Source, ...]
    ambiguities: tuple[Ambiguity, ...] = ()

    def __post_init__(self) -> None:
        if not 0 <= self.code <= 0xFFFF:
            raise ValueError(f"command code {self.code} is not 16-bit")
        if not self.cites:
            raise ValueError(f"0x{self.code:04X} {self.name} carries no citation")
        for command in self.commands:
            if self.code != command.CODE:
                raise ValueError(
                    f"{command.__qualname__} declares CODE 0x{command.CODE:04X} but is "
                    f"registered under 0x{self.code:04X}"
                )
            if command.mutates != self.mutates:
                raise ValueError(
                    f"{command.__qualname__}.mutates is {command.mutates} but the "
                    f"registry row for 0x{self.code:04X} says {self.mutates}. The flag "
                    f"drives the SlmpNotSentError / SlmpOutcomeUnknownError split and "
                    f"the two statements of it must not drift."
                )
        if self.direction == "ondemand" and self.commands:
            raise ValueError(
                f"0x{self.code:04X} is PLC-originated, so no client command class may "
                f"implement it"
            )

    def __str__(self) -> str:
        return f"0x{self.code:04X} {self.name}"


def _spec(
    code: int,
    name: str,
    *,
    mutates: bool,
    commands: tuple[AnyCommand, ...],
    direction: Direction = "request",
    extra_cites: tuple[Source, ...] = (),
) -> CommandSpec:
    """Build a row, gathering the citations and ambiguities off the classes themselves.

    Gathered rather than restated, so that a command whose citation changes cannot leave
    a stale copy of it in this table.
    """
    cites: list[Source] = []
    ambiguities: list[Ambiguity] = []
    for command in commands:
        for source in command.CITES:
            if source not in cites:
                cites.append(source)
        for ambiguity in command.AMBIGUITIES:
            if ambiguity not in ambiguities:
                ambiguities.append(ambiguity)
    for source in extra_cites:
        if source not in cites:
            cites.append(source)
    return CommandSpec(
        code=code,
        name=name,
        mutates=mutates,
        direction=direction,
        commands=commands,
        cites=tuple(cites),
        ambiguities=tuple(ambiguities),
    )


_ROWS: Final[tuple[CommandSpec, ...]] = (
    _spec(0x0101, "Read Type Name", mutates=False, commands=(ReadTypeName,)),
    _spec(0x0401, "Device Read (Batch)", mutates=False, commands=(ReadWords, ReadBits)),
    _spec(0x0403, "Device Read Random", mutates=False, commands=(ReadRandom,)),
    _spec(0x0406, "Device Read Block", mutates=False, commands=(ReadBlocks,)),
    _spec(0x0619, "Self Test", mutates=False, commands=(SelfTest,)),
    _spec(0x0801, "Monitor Registration", mutates=True, commands=(RegisterMonitor,)),
    _spec(0x0802, "Execute Monitor", mutates=False, commands=(ExecuteMonitor,)),
    _spec(0x1001, "Remote Run", mutates=True, commands=(RemoteRun,)),
    _spec(0x1002, "Remote Stop", mutates=True, commands=(RemoteStop,)),
    _spec(0x1003, "Remote Pause", mutates=True, commands=(RemotePause,)),
    _spec(0x1005, "Remote Latch Clear", mutates=True, commands=(RemoteLatchClear,)),
    _spec(0x1006, "Remote Reset", mutates=True, commands=(RemoteReset,)),
    _spec(
        0x1401, "Device Write (Batch)", mutates=True, commands=(WriteWords, WriteBits)
    ),
    _spec(
        0x1402,
        "Device Write Random",
        mutates=True,
        commands=(WriteRandom, WriteRandomBits),
    ),
    _spec(0x1406, "Device Write Block", mutates=True, commands=(WriteBlocks,)),
    _spec(0x1617, "Clear Error", mutates=True, commands=(ClearError,)),
    _spec(0x1630, "Remote Password Unlock", mutates=True, commands=(UnlockPassword,)),
    _spec(0x1631, "Remote Password Lock", mutates=True, commands=(LockPassword,)),
    _spec(
        ONDEMAND_COMMAND,
        "Ondemand",
        mutates=False,
        commands=(),
        direction="ondemand",
        extra_cites=(ONDEMAND_LAYOUT,),
    ),
)


def _build() -> dict[int, CommandSpec]:
    table: dict[int, CommandSpec] = {}
    for row in _ROWS:
        if row.code in table:  # pragma: no cover - a typo in this module only
            raise ValueError(f"0x{row.code:04X} is registered twice")
        table[row.code] = row
    return table


COMMANDS: Final[MappingProxyType[int, CommandSpec]] = MappingProxyType(_build())
"""Every command code this package implements, in ascending order."""


def codes() -> tuple[int, ...]:
    """Every registered command code, ascending."""
    return tuple(sorted(COMMANDS))


def by_code(code: int) -> CommandSpec:
    """The row for ``code``, or raise listing the ones that exist.

    There is no ``None`` return and no default row: a command this package does not
    implement is a question it cannot answer, and answering with a neighbouring row is
    how ``aslmp cite`` would print the wrong manual page.
    """
    if not isinstance(code, int) or isinstance(code, bool):
        raise TypeError(f"a command code is an int, not {type(code).__name__}")
    found = COMMANDS.get(code)
    if found is not None:
        return found
    known = ", ".join(f"0x{value:04X}" for value in codes())
    raise KeyError(
        f"0x{code:04X} is not a command aslmp implements. Registered: {known}. "
        f"Plc.raw_command() sends an arbitrary command code without validation, and is "
        f"the honest way to reach one this package has never seen."
    )
