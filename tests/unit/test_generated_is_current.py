"""The generated-and-committed modules must match what the generators produce now.

Two constant tables in this library are data with a Python face: the ~40-row device
table and the ~100-row end-code table. Keeping the TSV as the source of truth and the
Python as a committed artifact buys a per-row diff when a manual revision lands, and
costs nothing at run time -- ``aslmp.wire`` and ``aslmp.errors`` never parse a file.

The cost is that the two can drift, and drift in this direction is invisible: a device
code fixed in the TSV and not regenerated is a wrong byte on the wire with a correct
looking source file behind it. So regeneration is asserted to be a byte-for-byte no-op,
and the generators are asserted to be deterministic and to refuse a row that cannot be
checked against a source.

While a target module has not been written yet the comparison skips, naming the build
unit that owns it. Everything else here runs regardless.
"""

from __future__ import annotations

import ast
import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS = REPO_ROOT / "tools"
MAX_LINE = 96

# (generator, the file it owns, the build unit that commits that file)
GENERATORS: tuple[tuple[str, str, str], ...] = (
    ("gen_devicetable.py", "src/aslmp/wire/devicetable.py", "U2"),
    ("gen_endcodes.py", "src/aslmp/errors/endcodes.py", "U5"),
)


def run_generator(tool: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(TOOLS / tool), *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )


def load_generator(tool: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"_aslmp_gen_{tool[:-3]}", TOOLS / tool)
    if spec is None or spec.loader is None:  # pragma: no cover - packaging accident
        pytest.fail(f"cannot import {tool}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(("tool", "target", "unit"), GENERATORS)
def test_generator_runs_and_emits_parseable_python(tool: str, target: str, unit: str) -> None:
    del target, unit
    result = run_generator(tool, "--stdout")
    assert result.returncode == 0, f"{tool} --stdout failed:\n{result.stderr}"
    ast.parse(result.stdout, filename=f"<{tool} output>")


@pytest.mark.parametrize(("tool", "target", "unit"), GENERATORS)
def test_generator_is_deterministic(tool: str, target: str, unit: str) -> None:
    """Two runs must agree byte for byte, or the no-op check below means nothing."""
    del target, unit
    first = run_generator(tool, "--stdout")
    second = run_generator(tool, "--stdout")
    assert first.returncode == second.returncode == 0
    assert first.stdout == second.stdout, f"{tool} is not deterministic"


@pytest.mark.parametrize(("tool", "target", "unit"), GENERATORS)
def test_generated_output_would_pass_the_linter(tool: str, target: str, unit: str) -> None:
    """The generated file is checked by ruff like any other, so it must fit in 96 columns."""
    del target, unit
    result = run_generator(tool, "--stdout")
    long_lines = [
        f"line {number}: {len(line)} columns"
        for number, line in enumerate(result.stdout.splitlines(), start=1)
        if len(line) > MAX_LINE
    ]
    assert not long_lines, f"{tool} emits over-long lines:\n  " + "\n  ".join(long_lines)


@pytest.mark.parametrize(("tool", "target", "unit"), GENERATORS)
def test_generated_output_is_marked_as_generated(tool: str, target: str, unit: str) -> None:
    """Nobody should hand-edit these files, and the file itself has to say so."""
    del target, unit
    head = run_generator(tool, "--stdout").stdout[:2000]
    assert "@generated" in head
    assert tool in head, "the header must name the generator that produced it"
    assert "Do not edit" in head


@pytest.mark.parametrize(("tool", "target", "unit"), GENERATORS)
def test_committed_file_matches_the_generator(tool: str, target: str, unit: str) -> None:
    """The CI no-op check: regenerating must change nothing.

    A device code or an end-code description edited in the TSV and not regenerated is a
    wrong constant with a plausible-looking source file in front of it.
    """
    path = REPO_ROOT / target
    if not path.exists():
        pytest.skip(
            f"{target} has not been generated yet; build unit {unit} owns it. "
            f"Produce it with: python tools/{tool} --write"
        )
    result = run_generator(tool)
    assert result.returncode == 0, (
        f"{target} is stale or does not match tools/{tool}:\n"
        f"{result.stdout}\n{result.stderr}\n"
        f"Regenerate it with: python tools/{tool} --write"
    )


def test_endcode_generator_refuses_a_measurement_with_no_silicon() -> None:
    """A row that says LIVE and does not name the CPU is a rumour, and must not build."""
    module = load_generator("gen_endcodes.py")
    row = {
        "code": "0xC052",
        "name": "x",
        "exception_class": "SlmpWordPointCountError",
        "description": "d",
        "likely_cause": "",
        "caller_action": "",
        "provenance": "live",
        "cpu": "",
        "firmware": "1.065",
        "measured": "2026-09-06",
        "manual": "",
        "revision": "",
        "section": "",
        "note": "",
    }
    with pytest.raises(SystemExit, match="cpu"):
        module.check_row(row)


def test_endcode_generator_refuses_a_manual_with_no_revision() -> None:
    """Two revisions of JY997D56001 disagree. A citation without one cannot be checked."""
    module = load_generator("gen_endcodes.py")
    row = {
        "code": "0xC059",
        "name": "x",
        "exception_class": "",
        "description": "d",
        "likely_cause": "",
        "caller_action": "",
        "provenance": "manual",
        "cpu": "",
        "firmware": "",
        "measured": "",
        "manual": "JY997D56001",
        "revision": "",
        "section": "p.69",
        "note": "",
    }
    with pytest.raises(SystemExit, match="revision"):
        module.check_row(row)


def test_endcode_generator_refuses_a_row_with_no_source_at_all() -> None:
    module = load_generator("gen_endcodes.py")
    row = {
        "code": "0xC059",
        "name": "x",
        "exception_class": "",
        "description": "d",
        "likely_cause": "",
        "caller_action": "",
        "provenance": "manual",
        "cpu": "",
        "firmware": "",
        "measured": "",
        "manual": "",
        "revision": "",
        "section": "",
        "note": "",
    }
    with pytest.raises(SystemExit, match="no manual and no measurement"):
        module.check_row(row)


def test_devicetable_generator_refuses_a_short_device_with_no_short_code() -> None:
    module = load_generator("gen_devicetable.py")
    row = {
        "name": "D",
        "long_name": "Data register",
        "unit": "word",
        "words_per_point": "1",
        "ascii2": "D*",
        "ascii4": "D***",
        "code_short": "",
        "code_long": "0x00A8",
        "radix": "decimal",
        "min_spec": "short",
        "batch_ok": "yes",
        "random_ok": "yes",
        "monitor_ok": "yes",
        "block_ok": "yes",
        "provenance": "manual",
        "cpu": "",
        "firmware": "",
        "measured": "",
        "manual": "SH(NA)-080956ENG",
        "revision": "M",
        "section": "5.2 p.35-36",
        "note": "",
    }
    with pytest.raises(SystemExit, match="code_short"):
        module.check_row(row)


def test_devicetable_output_contains_the_prefix_order_that_parsing_depends_on() -> None:
    """Longest prefix first, or ``SB0`` parses as step relay ``B0``."""
    module = load_generator("gen_devicetable.py")
    source: str = module.render()
    namespace: dict[str, object] = {}
    tree = ast.parse(source)
    for node in tree.body:
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "PREFIXES_LONGEST_FIRST"
            and node.value is not None
        ):
            namespace["prefixes"] = ast.literal_eval(node.value)
    prefixes = namespace.get("prefixes")
    assert isinstance(prefixes, tuple), "PREFIXES_LONGEST_FIRST is not a literal tuple"
    lengths = [len(name) for name in prefixes]
    assert lengths == sorted(lengths, reverse=True), (
        "PREFIXES_LONGEST_FIRST must be ordered longest first: SB, SW, STS, LCN and "
        "their relatives all begin with a letter that is itself a device"
    )
    assert "SB" in prefixes and "S" in prefixes
    assert prefixes.index("SB") < prefixes.index("S")
