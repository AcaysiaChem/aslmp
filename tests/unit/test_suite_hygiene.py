"""Rules the test suite holds ITSELF to, enforced structurally rather than by review.

There is exactly one rule here so far, and it was bought at the cost of a CI outage.

A test that polls with ``while <condition>: await asyncio.sleep(...)`` and no upper bound
does not fail when the condition never becomes true. It hangs, forever, and takes the
whole pytest process with it. On 2026-09-24 four cells of the nine-cell matrix
(macos/3.12, macos/3.13, ubuntu/3.12, windows/3.12) sat inside the pytest step for 842
seconds against a 109 s honest run and were still going when an unrelated push happened
to cancel them; GitHub's default job timeout would have let each of them run for six
hours. The offender was a single line in ``test_health.py`` waiting for a counter that a
sticky ``FAILED`` state had frozen for good.

The suite already had the right pattern -- ``eventually()`` in
``tests/integration/test_client_against_simulator.py`` takes a deadline and raises
``AssertionError`` -- so this was one place not using what was already there. That is
precisely the kind of thing a reviewer does not catch twice and a checker catches always.

A bounded poll turns a hung suite into a failed test with a reason attached, which is the
same trade this library makes everywhere else: withhold and say why, never wait forever.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS = REPO_ROOT / "tests"


def _is_async_sleep(node: ast.AST) -> bool:
    """``await asyncio.sleep(...)`` or ``await sleep(...)``, however it was imported."""
    if not isinstance(node, ast.Await):
        return False
    call = node.value
    if not isinstance(call, ast.Call):
        return False
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr == "sleep"
    return isinstance(func, ast.Name) and func.id == "sleep"


def _awaits_outside_nested_functions(node: ast.While) -> list[ast.Await]:
    """Every ``await`` the loop itself performs, ignoring ones inside nested defs."""
    found: list[ast.Await] = []
    stack: list[ast.AST] = list(node.body)
    while stack:
        current = stack.pop()
        if isinstance(current, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            continue  # a callback defined in the loop is not work the loop awaits
        if isinstance(current, ast.Await):
            found.append(current)
        stack.extend(ast.iter_child_nodes(current))
    return found


def unbounded_poll_offenders(paths: list[Path]) -> list[str]:
    """Every ``while`` loop that spins on ``sleep`` alone and cannot give up.

    The discriminator is what took two attempts to get right, so it is worth stating.
    "Contains a sleep" is much too crude: the fake servers in this suite have read loops
    that sleep to reproduce a measured service delay, and they are not polling anything.
    What makes a loop a *poll* is that sleeping is the ONLY thing it awaits. A loop that
    awaits ``reader.read(...)`` is parked on real I/O and the peer will wake it; a loop
    whose sole await is ``sleep`` is asking the same question over and over and will ask
    it forever unless something stops it.

    A ``raise`` anywhere in the loop is accepted as that something, which is exactly what
    a deadline check looks like. ``for _ in range(n)`` is not a ``while`` and never
    matches. A ``break`` is deliberately NOT accepted: ``while True`` that breaks on a
    condition is still unbounded when the condition never arrives, which is the bug.
    """
    offenders: list[str] = []
    for path in sorted(paths):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        # Tolerant, because the meta-tests below feed this checker files from tmp_path,
        # which is not under the repository at all.
        try:
            where = path.relative_to(REPO_ROOT).as_posix()
        except ValueError:
            where = path.as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.While):
                continue
            awaits = _awaits_outside_nested_functions(node)
            if not awaits or not all(_is_async_sleep(a) for a in awaits):
                continue
            if any(
                isinstance(inner, ast.Raise)
                for stmt in node.body
                for inner in ast.walk(stmt)
            ):
                continue
            offenders.append(
                f"{where}:{node.lineno}: spins on sleep alone and can never give up -- "
                f"if the condition never comes true this hangs pytest"
            )
    return offenders


def test_no_test_polls_without_a_bound() -> None:
    offenders = unbounded_poll_offenders(sorted(TESTS.rglob("*.py")))
    assert not offenders, (
        "unbounded polling in the suite:\n  " + "\n  ".join(offenders) + "\n"
        "Give the loop a deadline and raise AssertionError when it passes, the way "
        "eventually() in tests/integration/test_client_against_simulator.py does. A "
        "poll that cannot give up does not fail the test, it hangs the whole run."
    )


# ======================================================================================
# The detector is only worth having if it fires. Below it is fed the exact shape that
# hung CI, and the shapes it must NOT flag.
# ======================================================================================


def _synthetic(tmp_path: Path, source: str) -> list[Path]:
    path = tmp_path / "sample.py"
    path.write_text(source, encoding="utf-8")
    return [path]


def test_the_poll_check_catches_the_loop_that_hung_ci(tmp_path: Path) -> None:
    """Verbatim the line that cost four cells fourteen minutes each."""
    offenders = unbounded_poll_offenders(
        _synthetic(
            tmp_path,
            "async def f(plc):\n"
            "    while plc.counters.probes_sent < 2:\n"
            "        await asyncio.sleep(0.005)\n",
        )
    )
    assert len(offenders) == 1, offenders
    assert "can never give up" in offenders[0]


def test_the_poll_check_allows_a_deadline_that_raises(tmp_path: Path) -> None:
    """The ``eventually()`` shape: the same poll, with a way out."""
    offenders = unbounded_poll_offenders(
        _synthetic(
            tmp_path,
            "async def f(condition, deadline, loop):\n"
            "    while not condition():\n"
            "        if loop.time() > deadline:\n"
            "            raise AssertionError('never became true')\n"
            "        await asyncio.sleep(0.005)\n",
        )
    )
    assert offenders == []


def test_the_poll_check_allows_a_server_read_loop(tmp_path: Path) -> None:
    """``while True`` over a socket is not polling and must not be flagged."""
    offenders = unbounded_poll_offenders(
        _synthetic(
            tmp_path,
            "async def f(reader, received):\n"
            "    while True:\n"
            "        data = await reader.read(65536)\n"
            "        if not data:\n"
            "            return\n"
            "        received.append(data)\n",
        )
    )
    assert offenders == []


def test_the_poll_check_still_flags_a_loop_that_only_breaks(tmp_path: Path) -> None:
    """A ``break`` is not a bound, and the docstring says so, so it is asserted.

    ``while True`` that breaks on a condition hangs exactly as long as the condition
    stays false. Accepting ``break`` would have let the bug that hung CI through under a
    different spelling.
    """
    offenders = unbounded_poll_offenders(
        _synthetic(
            tmp_path,
            "async def f(flag):\n"
            "    while True:\n"
            "        if flag():\n"
            "            break\n"
            "        await asyncio.sleep(0.01)\n",
        )
    )
    assert len(offenders) == 1, offenders


def test_the_poll_check_allows_a_loop_that_sleeps_between_real_awaits(
    tmp_path: Path,
) -> None:
    """The fake servers sleep to reproduce a measured service delay. Not a poll.

    This is the shape that made the first version of this checker wrong: it flagged the
    read loops in ``test_connection.py`` and ``test_transport_tcp.py``, which are parked
    on a peer's bytes and not spinning on anything.
    """
    offenders = unbounded_poll_offenders(
        _synthetic(
            tmp_path,
            "async def f(reader, delay):\n"
            "    while True:\n"
            "        data = await reader.read(65536)\n"
            "        if not data:\n"
            "            return\n"
            "        await asyncio.sleep(delay)\n",
        )
    )
    assert offenders == []


def test_the_poll_check_reads_a_bare_sleep_import(tmp_path: Path) -> None:
    """``from asyncio import sleep`` must not be a way around the rule."""
    offenders = unbounded_poll_offenders(
        _synthetic(
            tmp_path,
            "from asyncio import sleep\n"
            "async def f(flag):\n"
            "    while not flag():\n"
            "        await sleep(0.01)\n",
        )
    )
    assert len(offenders) == 1, offenders


# ======================================================================================
# Rule 2: no unbounded wait_closed(). Learned the same day as rule 1, from a worse bug.
# ======================================================================================

SRC = REPO_ROOT / "src"


def unbounded_close_offenders(paths: list[Path]) -> list[str]:
    """Every ``await x.wait_closed()`` that is not wrapped in a timeout.

    ``Server.wait_closed()`` waits for every ACCEPTED connection to detach, and
    ``StreamWriter.wait_closed()`` waits on the peer. Both return immediately before
    CPython 3.12.1 and genuinely wait from 3.12.1 on, so code written and run on 3.11 can
    carry this defect invisibly and then hang on a newer interpreter -- which is exactly
    what happened here on 2026-09-24: every 3.12 cell of the matrix hung inside pytest,
    no 3.11 cell did, and the cause was two fake servers whose handlers returned without
    closing their writer, so the transport never detached.

    The rule is about the WAIT and not about the close, because bounding the wait is what
    turns "the suite stopped" into a named, diagnosable failure. Wrapping it in
    ``asyncio.wait_for`` satisfies this: the ``wait_closed()`` call is then an argument
    rather than the thing being awaited, so it no longer matches.
    """
    offenders: list[str] = []
    for path in sorted(paths):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        try:
            where = path.relative_to(REPO_ROOT).as_posix()
        except ValueError:
            where = path.as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Await):
                continue
            call = node.value
            if not isinstance(call, ast.Call):
                continue
            func = call.func
            if isinstance(func, ast.Attribute) and func.attr == "wait_closed":
                offenders.append(
                    f"{where}:{node.lineno}: awaits wait_closed() with no timeout -- "
                    f"from CPython 3.12.1 this waits for every accepted connection to "
                    f"detach, and one that nothing closes waits forever"
                )
    return offenders


def test_no_unbounded_wait_closed() -> None:
    paths = sorted(SRC.rglob("*.py")) + sorted(TESTS.rglob("*.py"))
    offenders = unbounded_close_offenders(paths)
    assert not offenders, (
        "unbounded wait_closed():\n  " + "\n  ".join(offenders) + "\n"
        "Wrap it: await asyncio.wait_for(thing.wait_closed(), SOME_TIMEOUT). A teardown "
        "that cannot give up hangs the whole run with no test named and no stack."
    )


def test_the_close_check_catches_a_bare_await(tmp_path: Path) -> None:
    offenders = unbounded_close_offenders(
        _synthetic(
            tmp_path,
            "async def f(server):\n"
            "    server.close()\n"
            "    await server.wait_closed()\n",
        )
    )
    assert len(offenders) == 1, offenders


def test_the_close_check_allows_a_wrapped_wait(tmp_path: Path) -> None:
    offenders = unbounded_close_offenders(
        _synthetic(
            tmp_path,
            "async def f(server):\n"
            "    server.close()\n"
            "    await asyncio.wait_for(server.wait_closed(), 5.0)\n",
        )
    )
    assert offenders == []
