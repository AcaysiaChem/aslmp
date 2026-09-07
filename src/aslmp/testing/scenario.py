"""Scripted answers: "the third read comes back 0xC056, then normal service resumes".

Layer 2.5 (``aslmp.testing``). Pure data plus one cursor.

A :class:`~aslmp.testing.pathology.Pathology` says *how this CPU behaves*. A
:class:`Scenario` says *what happens next*, in order, once. The two are different
questions and mixing them produces switches like ``fail_sometimes`` that no test can
assert against.

Scenarios exist because several of the failures this library must survive are
**transient**: an entry that is busy for one connect and free for the next, a reply that
arrives after the client gave up, a CPU that answers one request abnormally in the middle
of a control loop. Reproducing those with a pathology switch would mean leaving it on,
and a test that can only observe the broken state cannot observe the recovery.

Every step is consumed exactly once and in order, so a scenario is spent when the script
runs out and the simulator returns to ordinary service. :meth:`Scenario.exhausted` is
what a test asserts at the end: a script that did not run is a test that did not test
what it says it did.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from aslmp.testing.dispatch import Reply, Silence

if TYPE_CHECKING:  # pragma: no cover - typing only
    from aslmp.testing.dispatch import Outcome

__all__ = ["Scenario", "Step", "abnormal", "silent"]


@dataclass(frozen=True, slots=True)
class Step:
    """One scripted answer, applied to the next matching request and then spent.

    ``command`` narrows the step to one command code; ``None`` matches any. ``times``
    repeats it, which is how "the next three reads all fail" is written without three
    copies.
    """

    outcome: Outcome
    command: int | None = None
    times: int = 1
    note: str = ""

    def __post_init__(self) -> None:
        if self.times < 1:
            raise ValueError(f"a Step applies at least once; times={self.times}")
        if self.command is not None and not 0 <= self.command <= 0xFFFF:
            raise ValueError(f"command 0x{self.command:X} is not a 16-bit command code")

    def matches(self, command: int) -> bool:
        """Whether this step answers a request carrying ``command``."""
        return self.command is None or self.command == command

    def __str__(self) -> str:
        where = "any command" if self.command is None else f"0x{self.command:04X}"
        return f"{where} -> {self.outcome}{f' ({self.note})' if self.note else ''}"


def abnormal(
    end_code: int, *, command: int | None = None, times: int = 1, note: str = ""
) -> Step:
    """A step that answers one request with an abnormal end code.

    ``abnormal(0xC056, command=0x0401)`` is "the next batch read says the address is out
    of range", which is what a control loop's error handling has to survive without
    losing the connection: an FX5U-32MT/DS answered every provoked ``0xC056`` and kept
    the socket (measured 2026-09-06).
    """
    return Step(outcome=Reply(end_code), command=command, times=times, note=note)


def silent(reason: str, *, command: int | None = None, times: int = 1) -> Step:
    """A step that answers nothing at all.

    The measured shape of a coding mismatch, a frame-type mismatch and an overstated
    data length, all three of which are indistinguishable from a dead PLC from the
    client's side.
    """
    return Step(outcome=Silence(reason), command=command, times=times, note=reason)


@dataclass(slots=True)
class Scenario:
    """An ordered script of steps, consumed one matching request at a time."""

    steps: tuple[Step, ...] = ()
    _cursor: int = field(default=0, init=False)
    _used: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        steps = tuple(self.steps)
        for index, step in enumerate(steps):
            if not isinstance(step, Step):
                raise TypeError(f"Scenario step {index} is {type(step).__name__}, not a Step")
        self.steps = steps

    def take(self, command: int) -> Outcome | None:
        """The scripted answer for a request carrying ``command``, or ``None``.

        Only the **head** of the script is considered. A step for ``0x0403`` sitting
        behind an unconsumed step for ``0x0401`` does not jump the queue: a script whose
        steps could be taken out of order is a set of switches wearing a list's clothes.
        """
        if self._cursor >= len(self.steps):
            return None
        step = self.steps[self._cursor]
        if not step.matches(command):
            return None
        self._used += 1
        if self._used >= step.times:
            self._cursor += 1
            self._used = 0
        return step.outcome

    @property
    def exhausted(self) -> bool:
        """Whether every step has been consumed. Assert this at the end of a test."""
        return self._cursor >= len(self.steps)

    @property
    def remaining(self) -> tuple[Step, ...]:
        """The steps that never fired."""
        return self.steps[self._cursor :]

    def reset(self) -> None:
        """Rewind to the first step."""
        self._cursor = 0
        self._used = 0

    def __len__(self) -> int:
        return len(self.steps)

    def __str__(self) -> str:
        if self.exhausted:
            return f"Scenario({len(self.steps)} step(s), exhausted)"
        step = self.steps[self._cursor]
        return f"Scenario(at step {self._cursor + 1} of {len(self.steps)}: {step})"
