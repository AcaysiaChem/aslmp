"""Tier 9 -- remote RUN / STOP / PAUSE against the real FX5U, with an independent oracle.

``tests/hardware/test_fx5u.py`` refuses to reach remote control at all and asserts that
refusal by walking its own AST. This file is the deliberate exception, and it is a
separate module for exactly that reason: the ban in the main suite stays absolute and
mechanically checked, and everything that can halt a CPU lives here where the guards can
be written for it.

**Why this became testable.** The original prohibition was specific: a memory-card error
(H2121) once made this CPU refuse a remote RUN and need a physical power cycle. The card
is out, and GX Works3 drove remote STOP and RUN repeatedly on 2026-09-07. What has *not*
changed is Remote RESET.

**What this file must never do.** ``0x1005`` Remote Latch Clear and ``0x1006`` Remote
Reset are never sent. RESET is the one command whose success looks exactly like a dropped
connection, and it reboots the CPU.
:func:`test_this_module_cannot_reach_latch_clear_or_reset` asserts it by walking this
module's own AST -- ``plc.remote.latch_clear``, ``plc.remote.reset``, the command classes,
and the two command numbers as integer literals anywhere outside the guard's own
definition. A docstring promising restraint enforces nothing.

**The oracle is D8, not SD203.** SD203 is what the library reads to verify a remote
command, so using it to check the library would be marking its own homework. ``D8`` is
IO_Scan, a free-running counter the PLC's own program increments -- it advances only when
the program executes, so it goes flat in STOP and in PAUSE and moves again in RUN. That
is a fact about the CPU that no amount of client-side wrongness can fake.

**Every test that stops the CPU puts it back.** :func:`back_in_run` is a ``finally`` that
commands RUN and then proves it with the scan counter, and the last test in the file
asserts the CPU is running and scanning before it is done.

**Running this file wipes non-latched device memory, and nothing here can restore it.**
Measured 2026-09-07 from the laptop at 192.168.10.41 over Wi-Fi, entry 5004: ``D100`` and
``D101`` were written 0x1234 / 0x5678 and ``D8`` stood at 693,829; one ``0x1002`` Remote
STOP later, *while still in STOP*, all three read zero. The CPU did that, not this file,
so there is nothing to put back -- but it means ``D8`` is free-running only **within one
RUN**, and every comparison here is a delta taken inside a single window for exactly that
reason. Never hold a ``D8`` value across a transition and expect it to still mean
something. See ``docs/hardware.md`` section 15.

**The bench.** FX5U-32MT/DS firmware 1.065 at 192.168.10.250, in RUN at
:data:`IDLE_SCAN_RATE_HZ` -- **1018 scans/s, 982 us per scan**, the one idle scan rate this
repository publishes, recorded with its conditions in ``docs/hardware.md`` section 17 --
with no physical I/O wired. (This docstring said ~1029 until 2026-09-07. That figure was
one session's own reference, quoted here with nothing to pair it against, which is how a
within-run number becomes a standing claim.) This module uses **TCP entry 5004** so it
cannot contend
with the main hardware suite for an entry; the CPU serves one TCP connection per
configured entry.

Gated on ``ASLMP_TEST_HOST``; marked ``hardware``; never run in CI::

    ASLMP_TEST_HOST=192.168.10.250 .venv/Scripts/python.exe -m pytest \\
        tests/hardware/test_remote_control.py -s
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import os
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from aslmp import (
    CpuStatus,
    Plc,
    SlmpConfigurationError,
    SlmpRemoteStateNotReachedError,
)

HOST = os.environ.get("ASLMP_TEST_HOST")
REMOTE_PORT = int(os.environ.get("ASLMP_TEST_REMOTE_PORT", "5004"))
PROFILE = os.environ.get("ASLMP_TEST_PROFILE", "melsec:iq-f/fx5u")

pytestmark = [
    pytest.mark.hardware,
    pytest.mark.skipif(HOST is None, reason="set ASLMP_TEST_HOST to run against a real PLC"),
]

SCAN = "D8"
"""IO_Scan, a REAL the PLC's own program increments once per scan. The oracle."""

IDLE_SCAN_RATE_HZ = 1018.0
"""The CPU's idle scan rate, in scans per second. **Quoted from one place.**

``docs/hardware.md`` section 17: 20,374 counts in 20.014 s = 1018.0 scans/s from
``argus-bench`` (192.168.10.36) over the wired link, 1018.4 from the laptop
(192.168.10.41) over Wi-Fi, FX5U-32MT/DS fw 1.065 at 192.168.10.250 in RUN with no
physical I/O wired, 2026-09-07. 982 us per scan is ``1e6 / 1018.0``, not a second
measurement. ``tests/unit/test_citations.py`` fails if any copy of this number in the
tree drifts from that section.
"""

SETTLE_WINDOW_S = 0.3
"""How long the scan counter is watched to decide whether the program is executing.

At :data:`IDLE_SCAN_RATE_HZ` this is ~305 counts when running and exactly 0 when not, so
the two cases are three orders of magnitude apart and no threshold has to be invented. It
is also comfortably longer than the 25-33 ms the CPU takes to enter RUN.
"""

CYCLES = 3
"""Stop/run cycles in the asymmetry test. Three is what the original measurement used."""

SETTLE_ZERO_ATTEMPTS = 5
"""How many times :func:`test_settle_zero_reproduces_the_false_negative` tries.

The bug it documents is a race, so one attempt could miss it. Five could in principle all
miss it on a CPU or a link fast enough; that outcome skips with a message rather than
failing, and rather than the test being deleted. Measured 2026-09-07 from the laptop at
192.168.10.41 over **Wi-Fi**, entry 5004: 5 of 5 attempts raised. The original
three-cycle measurement that found the bug raised on 2 RUNs of 3, but the link it was
taken over was never written down -- see ``docs/hardware.md`` section 15 -- so it is not
quoted here as a second link's result.
"""

# --------------------------------------------------------------------------------------
# The two commands this module may never send, and the guard that proves it does not.
#
# 0x1005 Remote Latch Clear and 0x1006 Remote Reset. Kept as ONE definition so the AST
# walk below can allow the literals here and nowhere else in the file.
# --------------------------------------------------------------------------------------

FORBIDDEN_COMMANDS = frozenset({0x1005, 0x1006})
FORBIDDEN_METHODS = frozenset({"latch_clear", "reset"})
FORBIDDEN_CLASSES = frozenset({"RemoteLatchClear", "RemoteReset"})


def measured(label: str, **numbers: object) -> None:
    """Print a measurement so ``-s`` output is the artifact, not just a pass/fail."""
    body = "  ".join(f"{key}={value}" for key, value in numbers.items())
    print(f"\n[MEASURED] {label}: {body}")


@contextlib.asynccontextmanager
async def controller(**kwargs: Any) -> AsyncIterator[Plc]:
    """A connected client on entry 5004 that is *allowed* to stop this machine.

    ``allow_remote_control=True`` appears in this file and in no other test in the suite.
    """
    assert HOST is not None
    plc = Plc(HOST, REMOTE_PORT, profile=PROFILE, allow_remote_control=True, **kwargs)
    async with plc:
        yield plc


async def scan_delta(plc: Plc, seconds: float = SETTLE_WINDOW_S) -> float:
    """How far IO_Scan moved over ``seconds``. Zero means the program is not executing."""
    first = await plc.read_f32(SCAN)
    await asyncio.sleep(seconds)
    return await plc.read_f32(SCAN) - first


async def assert_scanning(plc: Plc, why: str) -> float:
    delta = await scan_delta(plc)
    assert delta > 0, f"{why}: IO_Scan did not move in {SETTLE_WINDOW_S} s, so the CPU is not"
    return delta


async def assert_frozen(plc: Plc, why: str) -> None:
    delta = await scan_delta(plc)
    assert delta == 0, (
        f"{why}: IO_Scan advanced by {delta} in {SETTLE_WINDOW_S} s. SD203 may say what it "
        f"likes; the program is still executing and the CPU did not stop."
    )


@contextlib.asynccontextmanager
async def back_in_run(plc: Plc) -> AsyncIterator[None]:
    """Whatever happens in the body, command RUN on the way out and prove it scanned.

    Not a ``try/except`` that hides a failure: the body's exception propagates. This only
    guarantees that a test which halted the CPU does not hand it to the next test halted,
    and that "we put it back" is asserted rather than assumed.
    """
    try:
        yield
    finally:
        await plc.remote.run()
        await assert_scanning(plc, "the CPU was left stopped by a test in this file")


# ========================================================================================
# The guards. These run first and touch nothing.
# ========================================================================================


def test_this_module_cannot_reach_latch_clear_or_reset() -> None:
    """Walk this module's own AST for ``0x1005`` and ``0x1006`` in any reachable form.

    Remote RESET is the one command in the package for which an absent response is the
    documented success case, and it reboots the CPU. Remote Latch Clear wipes retained
    device memory. Neither has ever been sent to this bench and neither is sent here.

    Three shapes are refused, because a ban that only knows one spelling is decoration:
    the method names on ``plc.remote``, the command classes from ``aslmp.commands.remote``
    by name, and the raw command numbers as integer literals anywhere in the file except
    inside :data:`FORBIDDEN_COMMANDS`'s own definition. ``run``, ``stop`` and ``pause``
    are deliberately NOT in the list -- this module exists to send those.
    """
    source = Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    # The guard set has to contain the very literals it bans, so its own constants are
    # located by node identity and excused. Nothing else in the file may carry them.
    excused: set[int] = set()
    for statement in tree.body:
        targets: list[ast.expr] = []
        if isinstance(statement, ast.AnnAssign):
            targets = [statement.target]
        elif isinstance(statement, ast.Assign):
            targets = list(statement.targets)
        if any(isinstance(t, ast.Name) and t.id == "FORBIDDEN_COMMANDS" for t in targets):
            excused = {
                id(child) for child in ast.walk(statement) if isinstance(child, ast.Constant)
            }
    assert excused, "the guard could not find its own FORBIDDEN_COMMANDS definition"

    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_METHODS:
            offenders.append(f"a .{node.attr} attribute at line {node.lineno}")
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_CLASSES:
            offenders.append(f"{node.id} at line {node.lineno}")
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, int)
            and not isinstance(node.value, bool)
            and node.value in FORBIDDEN_COMMANDS
            and id(node) not in excused
        ):
            offenders.append(f"the literal 0x{node.value:04X} at line {node.lineno}")
    assert not offenders, (
        "this module can reach a forbidden remote command: "
        + ", ".join(offenders)
        + ". 0x1005 (Latch Clear) and 0x1006 (Reset) are never sent to this CPU."
    )


async def test_the_interlock_refuses_every_remote_command_before_the_wire() -> None:
    """Without ``allow_remote_control=True`` nothing reaches the socket. Counters prove it.

    The interlock lives in the command's ``validate``, which runs during encoding, so the
    refusal happens before a byte is framed -- not after a round trip and not at the CPU.
    A client that stopped a machine and *then* apologised would be worthless, so this
    asserts on ``transactions_started`` and ``bytes_sent``, not just on the exception.

    Note which methods are exercised: run, stop and pause only. Latch clear and reset are
    not called even on a client that is guaranteed to refuse them, because
    :func:`test_this_module_cannot_reach_latch_clear_or_reset` forbids naming them at all
    and an interlock is a thing you can have a bug in.
    """
    assert HOST is not None
    plc = Plc(HOST, REMOTE_PORT, profile=PROFILE)
    async with plc:
        assert await plc.read_cpu_status() is CpuStatus.RUN
        started = plc.counters.transactions_started
        sent = plc.counters.bytes_sent
        for what, call in (
            ("run", plc.remote.run),
            ("stop", plc.remote.stop),
            ("pause", plc.remote.pause),
        ):
            with pytest.raises(SlmpConfigurationError, match="allow_remote_control"):
                await call()
            assert plc.counters.transactions_started == started, (
                f"remote.{what}() started a transaction on a client that never opted in"
            )
            assert plc.counters.bytes_sent == sent, f"remote.{what}() put bytes on the wire"
        # The refusals cost nothing and left the connection usable.
        await assert_scanning(plc, "a refused remote command disturbed the CPU")


# ========================================================================================
# The transitions, each checked against the scan counter
# ========================================================================================


async def test_remote_stop_freezes_the_scan_counter_and_run_starts_it_again() -> None:
    """STOP and RUN, proven by the program's own counter rather than by SD203.

    ``read_cpu_status`` reads SD203, and SD203 is what ``verify=True`` already consulted,
    so a client that misdecoded that register would agree with itself perfectly. IO_Scan
    is incremented by the PLC program: it advances at :data:`IDLE_SCAN_RATE_HZ` in RUN and
    by **exactly zero** in STOP. Measured 2026-09-07 from the laptop at 192.168.10.41 over
    Wi-Fi: 324 counts in a 300 ms window running, 0.0 stopped, 326 running again.

    Those counts are the assertion; they are deliberately not divided into a scan rate.
    A 300 ms window bracketed by two ~7 ms round trips is too short to measure one -- the
    round trips alone are 5 % of it -- and dividing anyway is how this repository came to
    carry ~1024, ~1029 and 1018 for the same quantity. The rate is quoted from
    ``docs/hardware.md`` section 17, which took a 20 s window from both hosts.
    """
    async with controller() as plc:
        assert await plc.remote.status() is CpuStatus.RUN
        before = await assert_scanning(plc, "the bench was not running before this test")

        async with back_in_run(plc):
            reached = await plc.remote.stop()
            stop_result = plc.remote.last
            assert reached is CpuStatus.STOP
            assert stop_result is not None
            assert stop_result.verified is True
            assert stop_result.status is CpuStatus.STOP
            assert stop_result.requested == "remote.stop()"
            await assert_frozen(plc, "remote.stop() reported STOP")

            reached = await plc.remote.run()
            run_result = plc.remote.last
            assert reached is CpuStatus.RUN
            assert run_result is not None
            assert run_result.verified is True
            assert run_result.status is CpuStatus.RUN

        after = await assert_scanning(plc, "the CPU did not resume")
        measured(
            "remote stop / run against IO_Scan",
            scans_before=before,
            scans_stopped=0.0,
            scans_after=after,
            stop_polls=stop_result.polls,
            run_polls=run_result.polls,
        )


async def test_remote_pause_freezes_the_scan_counter_and_run_resumes_it() -> None:
    """PAUSE halts program execution too, and ``CpuStatus.PAUSE.running`` says it does not.

    Worth its own test because PAUSE is the state most easily mistaken for RUN: the CPU
    is not stopped, outputs are held, and a status check that asked "is it not STOP?"
    would pass. The counter does not care -- a paused program executes nothing.
    """
    async with controller() as plc:
        assert await plc.remote.status() is CpuStatus.RUN

        async with back_in_run(plc):
            reached = await plc.remote.pause()
            paused = plc.remote.last
            assert reached is CpuStatus.PAUSE
            assert paused is not None
            assert paused.status is CpuStatus.PAUSE
            assert not CpuStatus.PAUSE.running, "PAUSE is not a running state"
            await assert_frozen(plc, "remote.pause() reported PAUSE")

        resumed = plc.remote.last
        assert resumed is not None
        assert resumed.status is CpuStatus.RUN
        measured(
            "remote pause / run against IO_Scan",
            pause_polls=paused.polls,
            resume_polls=resumed.polls,
            scans_after=await assert_scanning(plc, "the CPU did not resume from PAUSE"),
        )


# ========================================================================================
# The asymmetry, pinned as a regression
# ========================================================================================


async def test_entering_run_is_asynchronous_and_leaving_it_is_not() -> None:
    """The bug the settle loop fixed, pinned so it cannot come back as a false negative.

    Measured on FX5U-32MT/DS fw 1.065, 2026-09-07, three cycles polling SD203 every 2 ms:
    after remote STOP, SD203 reported STOP on the **first** poll 3 times of 3 (~18-21 ms
    after the command); after remote RUN it still reported STOP on the first poll in
    **2 cycles of 3** and reached RUN only on the second, 25-33 ms after the command.
    ``_apply`` used to read SD203 exactly once, so ``verify=True`` -- the safety default --
    raised :class:`SlmpRemoteStateNotReachedError` for a RUN the CPU had accepted, roughly
    two thirds of the time.

    Reproduced later the same day by this test from the laptop at 192.168.10.41 over
    **Wi-Fi** on entry 5004: ``stop_polls`` [1, 1, 1] and ``run_polls`` [2, 2, 2], so 3 of
    3 RUNs needed a second observation and 0 of 3 STOPs did, a verified ``run()`` costing
    29.8 / 37.7 / 36.3 ms end to end. Same asymmetry, one cycle more of it.

    What is asserted, and why it is not a timing test:

    * every ``run()`` succeeds. That is the regression itself; before the fix, two of
      three did not.
    * ``polls`` is the loop's real iteration count, not decoration: a verified call costs
      ``1 + polls`` transactions, the command plus that many SD203 reads. If the settle
      loop were removed, ``polls`` could only ever be 1 and this arithmetic would still
      hold -- so the previous point is what catches removal, and this one proves the
      number being reported is the one that happened.
    * entering RUN never needs **fewer** extra polls than leaving it. Stated as an
      inequality so a fast CPU that answers everything on the first poll passes (0 >= 0),
      while the finding is still pinned: if leaving RUN ever starts costing more polls
      than entering it, the asymmetry has reversed and the constant that rests on it
      needs re-deriving.

    The counts themselves are printed rather than asserted, because they are a property
    of this CPU on this link on this day.
    """
    stop_polls: list[int] = []
    run_polls: list[int] = []
    run_ms: list[float] = []
    async with controller() as plc:
        await assert_scanning(plc, "the bench was not running before this test")
        async with back_in_run(plc):
            for _cycle in range(CYCLES):
                await plc.remote.stop()
                stopped = plc.remote.last
                assert stopped is not None
                stop_polls.append(stopped.polls)

                base = plc.counters.transactions_started
                started = time.perf_counter_ns()
                assert await plc.remote.run() is CpuStatus.RUN, "run() must reach RUN"
                run_ms.append((time.perf_counter_ns() - started) / 1e6)
                running = plc.remote.last
                assert running is not None
                run_polls.append(running.polls)
                assert plc.counters.transactions_started - base == 1 + running.polls, (
                    f"a verified run() reporting {running.polls} poll(s) should have cost "
                    f"{1 + running.polls} transactions -- the command plus that many "
                    f"SD203 reads -- and polls is what says how many observations it took"
                )
                await asyncio.sleep(0.05)

    slow_runs = sum(1 for polls in run_polls if polls > 1)
    slow_stops = sum(1 for polls in stop_polls if polls > 1)
    measured(
        "SD203 polls to see a transition, over three cycles",
        stop_polls=stop_polls,
        run_polls=run_polls,
        runs_needing_a_second_poll=f"{slow_runs} of {CYCLES}",
        stops_needing_a_second_poll=f"{slow_stops} of {CYCLES}",
        run_verify_ms=[round(value, 2) for value in run_ms],
    )
    assert all(polls >= 1 for polls in stop_polls + run_polls), "SD203 was never read"
    assert slow_runs >= slow_stops, (
        f"entering RUN needed a second SD203 poll in {slow_runs} of {CYCLES} cycles and "
        f"leaving it needed one in {slow_stops}. The measured asymmetry is the other way "
        f"round -- leaving RUN was synchronous 3 times of 3 and entering it was not -- so "
        f"if this fails, re-derive REMOTE_SETTLE_SECONDS rather than widening this test."
    )


async def test_settle_zero_reproduces_the_false_negative_a_deadline_fixes() -> None:
    """``settle=0.0`` reads SD203 once, which is exactly the bug, and it still raises.

    This is the pre-fix behaviour, reachable on purpose: with a zero deadline the loop
    breaks after its first observation, so ``run(verify=True)`` reports
    :class:`SlmpRemoteStateNotReachedError` for a command the CPU accepted. The proof
    that it is a **false** negative and not a real failure is the assertion right after
    the raise: the CPU is in RUN moments later, having never been sent a second command.

    .. rubric:: This test is a race and can legitimately not reproduce

    It depends on the CPU still being in STOP when the first SD203 read lands. On a link
    or a CPU fast enough that the transition completes first, no attempt raises and
    nothing is wrong. That outcome **skips with an explanation** rather than failing, and
    the test is kept rather than deleted, because what it documents is the reason
    :data:`aslmp.client.REMOTE_SETTLE_SECONDS` exists at all.

    Measured 2026-09-07: 5 attempts of 5 raised from the bench laptop over Wi-Fi;
    2 of 3 raised from the wired host. The transition itself took 25-33 ms both ways.
    """
    raised: list[str] = []
    async with controller() as plc:
        await assert_scanning(plc, "the bench was not running before this test")
        async with back_in_run(plc):
            for attempt in range(SETTLE_ZERO_ATTEMPTS):
                await plc.remote.stop()
                await assert_frozen(plc, "the CPU did not stop before the settle=0.0 attempt")
                try:
                    await plc.remote.run(settle=0.0)
                except SlmpRemoteStateNotReachedError as exc:
                    result = plc.remote.last
                    assert result is not None
                    assert result.polls == 1, "settle=0.0 must observe SD203 exactly once"
                    assert result.status is CpuStatus.STOP, "the false negative reports STOP"
                    raised.append(f"attempt {attempt}: {exc.args[0][:60]}")
                    # The command was accepted and never re-sent. If the CPU reaches RUN
                    # anyway, the exception was a false negative -- which is the finding.
                    await assert_scanning(
                        plc,
                        "settle=0.0 raised AND the CPU really did not enter RUN, so this "
                        "is a genuine failure and not the documented race",
                    )
                else:
                    await assert_scanning(plc, "run(settle=0.0) returned but nothing scanned")

    measured(
        "run(settle=0.0), the pre-fix single read",
        raised=f"{len(raised)} of {SETTLE_ZERO_ATTEMPTS}",
        first=raised[0] if raised else "none",
    )
    if not raised:
        pytest.skip(
            f"run(settle=0.0) reached RUN on its first SD203 read in all "
            f"{SETTLE_ZERO_ATTEMPTS} attempts, so the race this test documents did not "
            f"reproduce on this link today. It reproduced 5 of 5 over Wi-Fi and 2 of 3 "
            f"wired on 2026-09-07. This is a skip and not a pass: the test is kept "
            f"because REMOTE_SETTLE_SECONDS exists for the attempts that do raise."
        )
    assert len(raised) >= 1


# ========================================================================================
# Leave it as we found it
# ========================================================================================


async def test_the_cpu_is_left_running_and_scanning() -> None:
    """The last word, and the only one that matters if anything above went wrong.

    SD203 and IO_Scan must agree that the CPU is executing. Both, because this file spent
    its whole length arguing that either one alone is not evidence.
    """
    async with controller() as plc:
        status = await plc.remote.status()
        assert status is CpuStatus.RUN, (
            f"this file left the CPU in {status}. It must be returned to RUN before "
            f"anyone walks away from the bench."
        )
        delta = await assert_scanning(plc, "the CPU reports RUN but is not executing")
        measured(
            "final state",
            cpu=status.name,
            scans_per_second=round(delta / SETTLE_WINDOW_S),
            window_s=SETTLE_WINDOW_S,
        )
