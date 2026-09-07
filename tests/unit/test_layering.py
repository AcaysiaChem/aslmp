"""Tier 0 -- the layering itself. If these fail, nothing else in the suite matters.

Four of this library's load-bearing claims are structural rather than documentary, and
a comment cannot hold any of them:

* ``aslmp.wire`` is pure bytes. Importing it must not drag ``socket``, ``asyncio`` or
  ``threading`` into the process -- proved in a fresh subprocess, not by inspection.
* the import graph of DESIGN section 1 is exactly the graph, and a module may import
  only from strictly lower layers. An extra edge fails the build.
* ``aslmp.testing`` is built on L0-L2 only, so a bug in the transport, the in-flight
  gate or the client's control flow IS visible to every client-against-server test. If
  the simulator shared the client's transport, a transport bug would be invisible to
  the entire simulator suite.
* nothing in the library recovers silently, and nothing validates with ``assert``
  (``PySLMPClient``'s ``assert 0 < start_num < 0xFFF`` makes ``D0``, ``M0`` and ``X0``
  unreadable *and* evaporates under ``-O``).

These tests pass trivially while the package is a skeleton. They are written for the
day they do not: every message names the rule, the offending edge and the fix.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"
PKG = SRC / "aslmp"

# --------------------------------------------------------------------------------------
# The layer map of DESIGN section 1. A module may import only from a STRICTLY lower
# layer, plus siblings inside its own package where that package is listed as uniform.
#
# UNIFORM_PACKAGES: every module in the package sits at one layer, so siblings may
# import each other freely (wire/codec.py imports wire/citations.py, and so on).
# EXACT_MODULES: individually placed modules. aslmp/blocks/ deliberately spans three
# layers -- fields is pure L1, layout is pure L2, plan needs a client at L5 -- so it is
# NOT uniform and its members do not get a sibling exemption.
# --------------------------------------------------------------------------------------

UNIFORM_PACKAGES: dict[str, float] = {
    "aslmp.wire": 0.0,
    "aslmp.data": 0.0,
    "aslmp.errors": 0.5,
    "aslmp.profiles": 1.0,
    "aslmp.commands": 2.0,
    "aslmp.transport": 3.0,
    "aslmp.tools": 9.0,
}

EXACT_MODULES: dict[str, float] = {
    "aslmp": 9.0,
    "aslmp._version": 0.0,
    "aslmp.profile": 1.0,
    "aslmp.blocks": 5.0,
    "aslmp.blocks.fields": 1.0,
    "aslmp.blocks.layout": 2.0,
    "aslmp.blocks.plan": 5.0,
    "aslmp.identity": 2.0,
    "aslmp.timing": 2.5,
    "aslmp.observability": 2.5,
    "aslmp.connection": 4.0,
    "aslmp.results": 4.0,
    "aslmp.client": 5.0,
    "aslmp.timed": 5.0,
    "aslmp.health": 6.0,
    "aslmp.resilience": 6.0,
    "aslmp.entries": 6.0,
    "aslmp.loop": 6.0,
    "aslmp.sync": 7.0,
}

# Modules that share a layer and are allowed to import each other even though they are
# not in one package: profiles/ needs profile.py, identity.py needs commands/, timed.py
# is generated from client.py. Every group is a SINGLE layer; this relaxes the "strictly
# lower" rule sideways, never upwards.
PEER_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({"aslmp.profile", "aslmp.profiles", "aslmp.blocks.fields"}),
    frozenset({"aslmp.commands", "aslmp.identity", "aslmp.blocks.layout"}),
    frozenset({"aslmp.timing", "aslmp.observability"}),
    frozenset({"aslmp.connection", "aslmp.results"}),
    frozenset({"aslmp.client", "aslmp.timed", "aslmp.blocks.plan"}),
    frozenset({"aslmp.health", "aslmp.resilience", "aslmp.entries", "aslmp.loop"}),
)

TESTING_PACKAGE = "aslmp.testing"
TESTING_LAYER = 2.5
TESTING_MAX_IMPORT_LAYER = 2.0

# transport/ takes a structural Reassembler protocol and therefore cannot name a
# command, a device or an end code. The dependency runs upward; the knowledge runs
# downward. This edge is banned outright rather than by layer arithmetic, because wire
# is BELOW transport and the layer rule alone would permit it.
FORBIDDEN_EDGES: tuple[tuple[str, str, str], ...] = (
    (
        "aslmp.transport",
        "aslmp.wire",
        "transport/ must not import wire/. It receives a structural Reassembler "
        "protocol (bytes_needed / feed) which wire.reader.ResponseAccumulator happens "
        "to satisfy. A transport that can name a frame will eventually parse one.",
    ),
)

BANNED_AT_IMPORT = ("socket", "ssl", "asyncio", "selectors", "threading", "logging")

# Modules that must be importable without any of BANNED_AT_IMPORT appearing in
# sys.modules. Ones whose file does not exist yet are skipped, naming the build unit
# that owns them, so this test switches itself on as the package fills in.
PURE_MODULES: tuple[tuple[str, str, str], ...] = (
    ("aslmp.wire", "src/aslmp/wire/__init__.py", "U2"),
    ("aslmp.wire.citations", "src/aslmp/wire/citations.py", "U1"),
    ("aslmp.errors", "src/aslmp/errors/__init__.py", "U5"),
    ("aslmp.profile", "src/aslmp/profile.py", "U6"),
    ("aslmp.commands", "src/aslmp/commands/__init__.py", "U7"),
    ("aslmp.blocks.layout", "src/aslmp/blocks/layout.py", "U12"),
)


def shown(path: Path) -> str:
    """A repo-relative path when possible, so a failure points at a file you can open."""
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def module_name(path: Path) -> str:
    """``src/aslmp/wire/codec.py`` -> ``aslmp.wire.codec``; ``__init__`` -> its package."""
    parts = list(path.relative_to(SRC).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def all_modules() -> dict[str, Path]:
    return {module_name(p): p for p in sorted(PKG.rglob("*.py"))}


def layer_of(module: str) -> float | None:
    """The declared layer of ``module``, or ``None`` if it is not in the map."""
    if module in EXACT_MODULES:
        return EXACT_MODULES[module]
    if module == TESTING_PACKAGE or module.startswith(TESTING_PACKAGE + "."):
        return TESTING_LAYER
    for package, layer in UNIFORM_PACKAGES.items():
        if module == package or module.startswith(package + "."):
            return layer
    return None


def peer_group(module: str) -> frozenset[str] | None:
    """The declared same-layer group ``module`` belongs to, if any."""
    for group in PEER_GROUPS:
        for member in group:
            if module == member or module.startswith(member + "."):
                return group
    return None


def owning_package(module: str) -> str | None:
    """The uniform package ``module`` belongs to, if any."""
    if module == TESTING_PACKAGE or module.startswith(TESTING_PACKAGE + "."):
        return TESTING_PACKAGE
    for package in UNIFORM_PACKAGES:
        if module == package or module.startswith(package + "."):
            return package
    return None


def imported_aslmp_modules(
    path: Path, module: str, known: set[str]
) -> Iterator[tuple[str, int]]:
    """Yield ``(target_module, lineno)`` for every ``aslmp.*`` import in ``path``.

    ``from aslmp.wire import codec`` resolves to ``aslmp.wire.codec`` when that module
    exists, and to ``aslmp.wire`` when ``codec`` is merely a name defined there.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    base_package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "aslmp" or alias.name.startswith("aslmp."):
                    yield alias.name, node.lineno
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = base_package.split(".")
                if node.level > 1:
                    parts = parts[: -(node.level - 1)]
                base = ".".join([*parts, node.module] if node.module else parts)
            elif node.module and (node.module == "aslmp" or node.module.startswith("aslmp.")):
                base = node.module
            else:
                continue
            for alias in node.names:
                candidate = f"{base}.{alias.name}"
                yield (candidate if candidate in known else base), node.lineno


def test_every_module_declares_a_layer() -> None:
    """A new module must be placed in the layer map before it may exist.

    This is deliberately the annoying kind of test. Adding a module without saying what
    may import it is how a dependency graph stops being a graph.
    """
    unplaced = sorted(name for name in all_modules() if layer_of(name) is None)
    assert not unplaced, (
        "these modules are not in the layer map of tests/unit/test_layering.py: "
        f"{unplaced}. Add them to EXACT_MODULES or to a UNIFORM_PACKAGES entry, with "
        "the layer DESIGN section 1 gives them."
    )


def dag_violations(modules: Mapping[str, Path]) -> list[str]:
    """Every import that reaches at or above its own layer, as a printable line."""
    known = set(modules)
    violations: list[str] = []
    for name, path in sorted(modules.items()):
        source_layer = layer_of(name)
        if source_layer is None:
            continue  # reported by test_every_module_declares_a_layer
        source_package = owning_package(name)
        is_package_init = path.name == "__init__.py"
        for target, lineno in imported_aslmp_modules(path, name, known):
            if target == name:
                continue
            target_layer = layer_of(target)
            if target_layer is None:
                violations.append(
                    f"{shown(path)}:{lineno}: imports {target}, which "
                    f"has no declared layer"
                )
                continue
            if is_package_init and (target == name or target.startswith(name + ".")):
                continue  # a package facade may re-export its own contents
            if target_layer < source_layer:
                continue
            if source_package is not None and source_package == owning_package(target):
                continue
            group = peer_group(name)
            if group is not None and group is peer_group(target):
                continue
            violations.append(
                f"{shown(path)}:{lineno}: {name} (L{source_layer:g}) "
                f"imports {target} (L{target_layer:g}). A module may import only from "
                f"strictly lower layers, plus siblings inside its own package."
            )
    return violations


def test_import_dag_matches_the_declared_layers() -> None:
    """No module imports at or above its own layer, outside its own uniform package."""
    violations = dag_violations(all_modules())
    assert not violations, "layering violations:\n  " + "\n  ".join(violations)


def forbidden_edge_violations(modules: Mapping[str, Path]) -> list[str]:
    """Edges the layer arithmetic would permit but the design forbids."""
    known = set(modules)
    violations: list[str] = []
    for name, path in sorted(modules.items()):
        for from_pkg, to_pkg, why in FORBIDDEN_EDGES:
            if not (name == from_pkg or name.startswith(from_pkg + ".")):
                continue
            for target, lineno in imported_aslmp_modules(path, name, known):
                if target == to_pkg or target.startswith(to_pkg + "."):
                    violations.append(
                        f"{shown(path)}:{lineno}: {name} imports "
                        f"{target}. {why}"
                    )
    return violations


def test_no_forbidden_edges() -> None:
    """Edges that the layer arithmetic would permit but the design forbids."""
    violations = forbidden_edge_violations(all_modules())
    assert not violations, "forbidden import edges:\n  " + "\n  ".join(violations)


def simulator_violations(modules: Mapping[str, Path]) -> list[str]:
    """Every import by aslmp.testing that reaches above layer 2."""
    known = set(modules)
    violations: list[str] = []
    for name, path in sorted(modules.items()):
        if not (name == TESTING_PACKAGE or name.startswith(TESTING_PACKAGE + ".")):
            continue
        for target, lineno in imported_aslmp_modules(path, name, known):
            if target == TESTING_PACKAGE or target.startswith(TESTING_PACKAGE + "."):
                continue
            target_layer = layer_of(target)
            if target_layer is None or target_layer > TESTING_MAX_IMPORT_LAYER:
                violations.append(
                    f"{shown(path)}:{lineno}: aslmp.testing imports "
                    f"{target}. The simulator may import L0-L2 only (wire, errors, "
                    f"profile, commands) -- never transport, connection or client."
                )
    return violations


def test_testing_package_imports_only_l0_to_l2() -> None:
    """The simulator must not be built on the client's transport.

    If it were, a bug in the transport, the in-flight gate, the reconnection logic or
    the client's control flow would be invisible to every client-against-server test in
    the suite, because both sides would share it.
    """
    violations = simulator_violations(all_modules())
    assert not violations, "simulator layering violations:\n  " + "\n  ".join(violations)


@pytest.mark.parametrize(("module", "relpath", "unit"), PURE_MODULES)
def test_import_pulls_no_io_machinery(module: str, relpath: str, unit: str) -> None:
    """A fresh subprocess imports the module; no I/O machinery may appear.

    The failure this guards is not hypothetical hygiene. Everything below L3 is
    supposed to be a pure function of bytes, which is what makes the whole wire layer
    testable one byte at a time with no socket and no event loop. The moment one of
    these modules can reach a socket, that stops being true and nothing tells you.

    Note for whoever writes ``aslmp/__init__.py``: importing ``aslmp.wire`` executes
    the top-level package first. If that package eagerly imports ``client`` (and so
    ``transport``, and so ``socket``), THIS TEST WILL FAIL. Re-export lazily with a
    module-level ``__getattr__``.
    """
    if not (REPO_ROOT / relpath).exists():
        pytest.skip(f"{relpath} does not exist yet; it belongs to build unit {unit}")
    code = (
        "import sys\n"
        f"sys.path.insert(0, {str(SRC)!r})\n"
        f"import {module}\n"
        f"banned = [m for m in {BANNED_AT_IMPORT!r} if m in sys.modules]\n"
        "print(','.join(banned))\n"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", code],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    assert result.returncode == 0, f"importing {module} failed:\n{result.stderr}"
    leaked = [name for name in result.stdout.strip().split(",") if name]
    assert not leaked, (
        f"importing {module} put {leaked} into sys.modules. Everything below layer 3 "
        f"is pure bytes and must stay importable in a process that has no event loop "
        f"and no socket. Check for a transitive import through aslmp/__init__.py."
    )


def library_sources() -> Iterator[tuple[str, Path]]:
    """Every module in the shipped library, excluding the optional simulator."""
    for name, path in sorted(all_modules().items()):
        if name == TESTING_PACKAGE or name.startswith(TESTING_PACKAGE + "."):
            continue
        yield name, path


def assert_offenders(sources: Iterable[tuple[str, Path]]) -> list[str]:
    """Every ``assert`` statement in the given modules."""
    offenders: list[str] = []
    for _name, path in sources:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Assert):
                offenders.append(f"{shown(path)}:{node.lineno}")
    return offenders


def test_no_assert_outside_tests() -> None:
    """Validation never uses ``assert``: it evaporates under ``-O``.

    Named after ``PySLMPClient``'s ``assert 0 < start_num < 0xFFF``, which makes ``D0``,
    ``M0`` and ``X0`` unreadable when it runs and validates nothing when it does not.
    """
    offenders = assert_offenders(library_sources())
    assert not offenders, (
        "assert used for validation in the library:\n  " + "\n  ".join(offenders) + "\n"
        "Raise a typed SlmpError instead: assert statements vanish under python -O."
    )


LOGGING_ALLOWED = frozenset({"aslmp.observability"})


def logging_offenders(sources: Iterable[tuple[str, Path]]) -> list[str]:
    """Every import of ``logging`` outside the one module allowed to name it."""
    offenders: list[str] = []
    for name, path in sources:
        if name in LOGGING_ALLOWED:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "logging" or alias.name.startswith("logging."):
                        offenders.append(f"{shown(path)}:{node.lineno}")
            elif isinstance(node, ast.ImportFrom) and node.module == "logging":
                offenders.append(f"{shown(path)}:{node.lineno}")
    return offenders


def test_logging_is_named_in_exactly_one_place() -> None:
    """A log call inside the transport lands in the latency number it describes."""
    offenders = logging_offenders(library_sources())
    assert not offenders, (
        "logging is imported outside aslmp/observability.py:\n  "
        + "\n  ".join(offenders)
        + "\nObservability in this library is typed events and transaction records. "
        "aslmp.observability.attach_logging() is the single bridge to the logging module."
    )


def _contains_raise(node: ast.AST) -> bool:
    return any(isinstance(child, ast.Raise) for child in ast.walk(node))


def _functions_that_raise(tree: ast.AST) -> set[int]:
    """Line numbers of every ``except`` handler inside a function that raises somewhere.

    ``observability.fanout`` is the pattern this exists for: it collects each sink's
    exception and re-raises the lot as an ``ExceptionGroup`` after the loop. That is not
    silent recovery -- nothing is dropped -- but the ``raise`` is not lexically inside
    the handler. Swallowing is still caught, because a handler that returns a falsy
    value or does nothing at all is flagged separately.
    """
    allowed: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if not _contains_raise(node):
            continue
        for child in ast.walk(node):
            if isinstance(child, ast.ExceptHandler):
                allowed.add(child.lineno)
    return allowed


def _falsy_return(node: ast.Return) -> bool:
    value = node.value
    if value is None:
        return True
    if isinstance(value, ast.Constant):
        return not value.value
    if isinstance(value, ast.List | ast.Tuple | ast.Dict | ast.Set):
        return not getattr(value, "elts", None) and not getattr(value, "keys", None)
    return False


def silent_recovery_offenders(sources: Iterable[tuple[str, Path]]) -> list[str]:
    """Every bare except, swallowed ``Exception`` and falsy return from an error path."""
    offenders: list[str] = []
    for _name, path in sources:
        where = shown(path)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        raising = _functions_that_raise(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            if node.type is None:
                offenders.append(f"{where}:{node.lineno}: bare 'except:'")
                continue
            body = node.body
            if len(body) == 1 and isinstance(body[0], ast.Pass):
                offenders.append(f"{where}:{node.lineno}: 'except ...: pass'")
                continue
            names = {
                n.id for n in ast.walk(node.type) if isinstance(n, ast.Name)
            }
            swallows = not _contains_raise(node) and node.lineno not in raising
            if {"Exception", "BaseException"} & names and swallows:
                offenders.append(
                    f"{where}:{node.lineno}: catches Exception without re-raising or "
                    f"wrapping"
                )
            for inner in ast.walk(node):
                if isinstance(inner, ast.Return) and _falsy_return(inner):
                    offenders.append(
                        f"{where}:{inner.lineno}: returns a falsy value from an except "
                        f"body -- raise instead"
                    )
    return offenders


def test_no_silent_recovery() -> None:
    """No bare except, no swallowed Exception, no falsy return from an error path.

    Every one of these patterns appears in a library we surveyed, and every one of them
    turns a failed read into a plausible-looking value: an empty list where the caller
    expected registers, a zero-filled tail where the response was truncated, a stale
    reading where the socket had already closed.
    """
    offenders = silent_recovery_offenders(library_sources())
    assert not offenders, (
        "silent recovery in the library:\n  " + "\n  ".join(offenders) + "\n"
        "Nothing in aslmp retries, clamps, substitutes a default or returns a stale "
        "value. If you cannot do the thing, raise."
    )


# ======================================================================================
# The detectors above are only worth having if they fire. Everything below feeds them a
# synthetic violation of each rule and asserts that they catch it -- otherwise this file
# is a suite of tests that pass because nothing has been checked.
# ======================================================================================


def synthetic(tmp_path: Path, module: str, source: str) -> dict[str, Path]:
    """Write ``source`` to a temp file and return a one-entry module map for it."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / (module.replace(".", "_") + ".py")
    path.write_text(source, encoding="utf-8")
    return {module: path}


def test_the_dag_check_catches_an_upward_import(tmp_path: Path) -> None:
    modules = synthetic(tmp_path, "aslmp.wire.codec", "from aslmp.client import Plc\n")
    violations = dag_violations(modules)
    assert len(violations) == 1
    assert "aslmp.wire.codec (L0) imports aslmp.client (L5)" in violations[0]


def test_the_dag_check_catches_a_sideways_import_across_packages(tmp_path: Path) -> None:
    """L2.5 to L2.5 is fine inside a peer group; commands to identity is not."""
    modules = synthetic(tmp_path, "aslmp.commands.batch", "import aslmp.transport.tcp\n")
    assert dag_violations(modules), "an L2 module importing L3 must be reported"


def test_the_dag_check_allows_a_strictly_lower_import(tmp_path: Path) -> None:
    modules = synthetic(
        tmp_path, "aslmp.commands.batch", "from aslmp.profile import CpuProfile\n"
    )
    assert dag_violations(modules) == []


def test_the_dag_check_allows_a_sibling_inside_one_package(tmp_path: Path) -> None:
    modules = synthetic(tmp_path, "aslmp.wire.address", "from aslmp.wire.codec import BINARY\n")
    assert dag_violations(modules) == []


def test_the_dag_check_allows_a_declared_peer_group(tmp_path: Path) -> None:
    """profiles/ needs profile.py, and both are L1."""
    modules = synthetic(
        tmp_path, "aslmp.profiles.iq_f", "from aslmp.profile import CpuProfile\n"
    )
    assert dag_violations(modules) == []


def test_the_dag_check_catches_an_import_of_an_unregistered_module(tmp_path: Path) -> None:
    modules = synthetic(tmp_path, "aslmp.client", "from aslmp.mystery import thing\n")
    violations = dag_violations(modules)
    assert violations and "no declared layer" in violations[0]


def test_the_dag_check_resolves_relative_imports(tmp_path: Path) -> None:
    modules = synthetic(tmp_path, "aslmp.wire.codec", "from ..client import Plc\n")
    violations = dag_violations(modules)
    assert violations and "aslmp.client" in violations[0]


def test_the_forbidden_edge_check_catches_transport_importing_wire(tmp_path: Path) -> None:
    modules = synthetic(
        tmp_path, "aslmp.transport.tcp", "from aslmp.wire.reader import ResponseAccumulator\n"
    )
    violations = forbidden_edge_violations(modules)
    assert violations and "must not import wire" in violations[0]


def test_the_simulator_check_catches_an_import_of_the_client(tmp_path: Path) -> None:
    modules = synthetic(
        tmp_path, "aslmp.testing.server", "from aslmp.transport.tcp import Tcp\n"
    )
    violations = simulator_violations(modules)
    assert violations and "L0-L2 only" in violations[0]


def test_the_simulator_check_allows_commands(tmp_path: Path) -> None:
    modules = synthetic(
        tmp_path, "aslmp.testing.server", "from aslmp.commands import COMMANDS\n"
    )
    assert simulator_violations(modules) == []


def test_the_assert_scan_fires(tmp_path: Path) -> None:
    modules = synthetic(tmp_path, "aslmp.wire.address", "def f(n):\n    assert n > 0\n")
    assert assert_offenders(modules.items())


def test_the_logging_scan_fires(tmp_path: Path) -> None:
    modules = synthetic(tmp_path, "aslmp.transport.tcp", "import logging\n")
    assert logging_offenders(modules.items())
    allowed = synthetic(tmp_path / "ok", "aslmp.observability", "import logging\n")
    assert logging_offenders(allowed.items()) == []


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("try:\n    f()\nexcept:\n    pass\n", "bare 'except:'"),
        ("try:\n    f()\nexcept ValueError:\n    pass\n", "'except ...: pass'"),
        (
            "def g():\n    try:\n        return f()\n"
            "    except Exception:\n        return []\n",
            "catches Exception without re-raising",
        ),
        (
            "def g():\n    try:\n        return f()\n"
            "    except ValueError:\n        return []\n",
            "returns a falsy value",
        ),
        (
            "def g():\n    try:\n        return f()\n"
            "    except KeyError:\n        return None\n",
            "returns a falsy value",
        ),
    ],
)
def test_the_silent_recovery_scan_fires(tmp_path: Path, source: str, expected: str) -> None:
    modules = synthetic(tmp_path, "aslmp.client", source)
    offenders = silent_recovery_offenders(modules.items())
    assert any(expected in line for line in offenders), offenders


def test_the_silent_recovery_scan_permits_collect_then_raise(tmp_path: Path) -> None:
    """``observability.fanout`` collects every sink's failure and raises the group.

    Nothing is dropped, so this is not silent recovery -- but the ``raise`` is not
    lexically inside the handler, and a naive scan would reject it.
    """
    source = (
        "def g(sinks, event):\n"
        "    failures = []\n"
        "    for sink in sinks:\n"
        "        try:\n"
        "            sink(event)\n"
        "        except Exception as exc:\n"
        "            failures.append(exc)\n"
        "    if failures:\n"
        "        raise ExceptionGroup('sinks raised', failures)\n"
    )
    modules = synthetic(tmp_path, "aslmp.observability", source)
    assert silent_recovery_offenders(modules.items()) == []
