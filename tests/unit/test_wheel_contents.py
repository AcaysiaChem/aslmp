"""What the built wheel actually contains, asserted against the wheel and not the intent.

``pyproject.toml`` says ``packages = ["src/aslmp"]`` and everything else is hatchling's
default behaviour. Defaults change, ``.gitignore`` entries leak into build exclusions,
and a data file that is committed and imported and tested is still absent from the
artifact somebody installs. The only way to know is to build one and open it, which is
what this file does.

**The headline assertion is the TSVs.** ``aslmp/data/*.tsv`` are the diffable sources of
truth behind the device table, the end-code table, the limits and the ambiguity records
(DESIGN section 1.14 ships them in the wheel). Nothing imports them at run time -- the
shipped tables are committed Python literals, and ``test_public_surface`` proves no
import reads a file -- so a wheel that silently dropped them would pass every other test
in this suite, and ``aslmp cite --manuals`` would fail on a user's machine with a
``FileNotFoundError`` pointing inside site-packages. They are also what a Mitsubishi
engineer diffs when a manual revision lands.

---

**The decision this file records: ``aslmp/testing/`` SHIPS in the default wheel.**

DESIGN section 5.1.7 asks for the opposite -- "the default wheel ships the client,
``py.typed``, ``data/``, and exactly one console script. No simulator, no bench" -- and
section 1.15 puts the simulator behind an ``aslmp[testing]`` extra. Implemented as
specified, that gate achieves nothing, and the reasoning is worth writing down rather
than rediscovering:

1. **The extra has no dependencies.** ``[project.optional-dependencies] testing = []``.
   ``pip install aslmp[testing]`` and ``pip install aslmp`` would resolve to the same
   bytes on disk, so the "extra" would be a label, not a gate. A Python extra selects
   *dependencies*; it cannot select *modules* of the package it belongs to. Excluding
   ``aslmp/testing`` from the wheel would make ``pip install aslmp[testing]`` install a
   package without the simulator -- an extra that promises something it cannot deliver.
2. **The simulator is dependency-free and useful at run time.** It is 9 modules of pure
   stdlib. Somebody integrating against a PLC they cannot reach -- most of the time,
   for most people -- wants ``aslmp serve`` from the installed wheel, not from a git
   checkout. That is what ``aslmp serve`` is for, and it is on the single console
   script the design already committed to.
3. **The layering rule, which is the part that actually matters, is unaffected.**
   DESIGN section 4.11's real requirement is that ``aslmp.testing`` must not import
   ``aslmp.transport``, ``aslmp.connection`` or ``aslmp.client``, so that a transport
   bug cannot be invisible to every client-to-server test. That is enforced by
   ``tests/unit/test_layering.py`` (a layer cap of 2.0 on everything ``aslmp.testing``
   imports) and by ``test_public_surface``'s subprocess check that no public entry
   point pulls the simulator in. Shipping the files does not weaken either one.

What section 5.1.7 was really protecting -- a client wheel that is dependency-free,
import-light, and installs exactly one thing on ``PATH`` -- is asserted below and holds.
``bench/`` genuinely stays out: it lives at the repository root, is not part of the
package, and its scripts need a PLC.
"""

from __future__ import annotations

import configparser
import tempfile
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE = REPO_ROOT / "src" / "aslmp"

hatchling = pytest.importorskip(
    "hatchling",
    reason=(
        "building a wheel needs hatchling, the build backend pyproject.toml already "
        "declares in [build-system].requires. It is not in the dev extra; install it "
        "with `pip install hatchling` (or add it to [project.optional-dependencies].dev) "
        "to run the wheel-contents checks."
    ),
)


@pytest.fixture(scope="module")
def wheel_names() -> tuple[str, ...]:
    """Every path inside a freshly built wheel.

    Built once per module into a temporary directory and thrown away: this test asserts
    what the build produces *now*, from the tree as it stands, not what some earlier
    ``dist/`` happens to hold.
    """
    from hatchling.builders.wheel import WheelBuilder

    with tempfile.TemporaryDirectory() as tmp:
        builder = WheelBuilder(str(REPO_ROOT))
        built = list(builder.build(directory=tmp, versions=["standard"]))
        assert len(built) == 1, f"expected one wheel, got {built}"
        with zipfile.ZipFile(built[0]) as archive:
            return tuple(sorted(archive.namelist()))


@pytest.fixture(scope="module")
def wheel_entry_points() -> configparser.ConfigParser:
    """``entry_points.txt`` from the built wheel, parsed."""
    from hatchling.builders.wheel import WheelBuilder

    with tempfile.TemporaryDirectory() as tmp:
        builder = WheelBuilder(str(REPO_ROOT))
        built = list(builder.build(directory=tmp, versions=["standard"]))
        with zipfile.ZipFile(built[0]) as archive:
            name = next(n for n in archive.namelist() if n.endswith("entry_points.txt"))
            text = archive.read(name).decode("utf-8")
    parser = configparser.ConfigParser()
    parser.read_string(text)
    return parser


# ========================================================================================
# data/ -- the assertion this file exists for
# ========================================================================================


def test_every_committed_tsv_lands_in_the_wheel(wheel_names: tuple[str, ...]) -> None:
    """The diffable sources of truth ship with the package.

    Nothing reads these at import (the device and end-code tables are generated Python
    literals precisely so that a wheel installed read-only still works), which is what
    makes their absence invisible to every other test. ``aslmp cite --manuals`` and the
    regeneration check are what would break, on a user's machine, long after the build.
    """
    committed = sorted(path.name for path in PACKAGE.glob("data/*.tsv"))
    assert committed, "src/aslmp/data holds no .tsv files at all"
    shipped = {name.rsplit("/", 1)[1] for name in wheel_names if name.endswith(".tsv")}
    missing = [name for name in committed if name not in shipped]
    assert not missing, (
        f"the wheel is missing {missing}. DESIGN section 1.14 ships aslmp/data/ in the "
        f"wheel: they are the tables a Mitsubishi engineer diffs when a manual revision "
        f"lands, and the input to the generators whose output CI asserts is a no-op."
    )


def test_the_shipped_tsvs_are_under_aslmp_data(wheel_names: tuple[str, ...]) -> None:
    """Inside the package, not beside it: an installed wheel has no repository root."""
    for name in wheel_names:
        if name.endswith(".tsv"):
            assert name.startswith("aslmp/data/"), f"{name} is not under aslmp/data/"


def test_a_shipped_tsv_is_readable_and_not_empty(wheel_names: tuple[str, ...]) -> None:
    """A zero-byte placeholder would satisfy a name check and nothing else."""
    from hatchling.builders.wheel import WheelBuilder

    with tempfile.TemporaryDirectory() as tmp:
        builder = WheelBuilder(str(REPO_ROOT))
        built = list(builder.build(directory=tmp, versions=["standard"]))
        with zipfile.ZipFile(built[0]) as archive:
            content = archive.read("aslmp/data/end_codes.tsv").decode("ascii")
    lines = [line for line in content.splitlines() if line and not line.startswith("#")]
    assert lines[0].split("\t")[0] == "code"
    assert len(lines) > 50, f"end_codes.tsv shipped with only {len(lines)} rows"


def test_py_typed_ships(wheel_names: tuple[str, ...]) -> None:
    """Without it, every downstream ``mypy --strict`` treats this package as untyped,
    and DESIGN section 5.8's "zero cast() at any call site" claim is unverifiable for
    anybody but us."""
    assert "aslmp/py.typed" in wheel_names


# ========================================================================================
# exactly one console script
# ========================================================================================


def test_exactly_one_console_script(wheel_entry_points: configparser.ConfigParser) -> None:
    """One entry point on the user's PATH, not fourteen (DESIGN section 1.16)."""
    scripts = dict(wheel_entry_points["console_scripts"])
    assert scripts == {"aslmp": "aslmp.tools.__main__:main"}


def test_the_console_script_target_exists(
    wheel_entry_points: configparser.ConfigParser,
) -> None:
    """The declared target is importable and callable, not a name that used to be."""
    import importlib

    module_name, _, attribute = wheel_entry_points["console_scripts"]["aslmp"].partition(":")
    module = importlib.import_module(module_name)
    assert callable(getattr(module, attribute))


# ========================================================================================
# what is in, what is out
# ========================================================================================


def test_the_wheel_ships_the_client(wheel_names: tuple[str, ...]) -> None:
    for expected in (
        "aslmp/__init__.py",
        "aslmp/client.py",
        "aslmp/wire/frames.py",
        "aslmp/errors/endcodes.py",
        "aslmp/profiles/fx5u.py",
        "aslmp/transport/tcp.py",
        "aslmp/tools/__main__.py",
    ):
        assert expected in wheel_names


def test_the_simulator_ships_and_this_is_deliberate(wheel_names: tuple[str, ...]) -> None:
    """``aslmp/testing/`` is in the default wheel. See this module's docstring.

    Short version: the ``testing`` extra declares no dependencies, so gating on it would
    make ``pip install aslmp[testing]`` and ``pip install aslmp`` produce identical
    installs -- the gate cannot work. The simulator is nine modules of stdlib, it is what
    ``aslmp serve`` runs, and the property DESIGN section 4.11 actually needs (the
    simulator must not import the client's transport) is enforced by the layering test
    rather than by the packaging.
    """
    assert "aslmp/testing/server.py" in wheel_names
    assert "aslmp/testing/conformance.py" in wheel_names


def test_the_simulator_is_not_reachable_from_the_client_import_graph() -> None:
    """The reason shipping it is safe, asserted rather than asserted-about.

    If the simulator were built on the client's transport, a transport bug would be
    invisible to every client-to-server test. That is a property of the import graph and
    it does not change when the files are in the wheel.
    """
    import ast

    forbidden = ("aslmp.transport", "aslmp.connection", "aslmp.client")
    for path in sorted((PACKAGE / "testing").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            else:
                continue
            for name in names:
                assert not name.startswith(forbidden), (
                    f"{path.name}:{node.lineno} imports {name}. The simulator must not "
                    f"share the client's transport, or a transport bug becomes invisible "
                    f"to every client-to-server test (DESIGN section 4.11)."
                )


def test_the_bench_scripts_stay_out_of_the_wheel(wheel_names: tuple[str, ...]) -> None:
    """``bench/`` lives at the repository root and needs a PLC. It is not a module
    anybody installs, and its scripts write nothing but numbers."""
    assert not [name for name in wheel_names if name.startswith("bench/")]
    assert (REPO_ROOT / "bench").is_dir(), "bench/ should exist in the repository"


def test_the_repo_generators_stay_out_of_the_wheel(wheel_names: tuple[str, ...]) -> None:
    """``tools/gen_*.py`` at the repository root are developer scripts that regenerate
    committed sources; ``aslmp/tools/`` is the shipped command line. Two directories,
    one name, and only one of them belongs in the artifact."""
    assert not [name for name in wheel_names if name.startswith("tools/")]
    assert not [name for name in wheel_names if "gen_endcodes" in name]


def test_the_tests_stay_out_of_the_wheel(wheel_names: tuple[str, ...]) -> None:
    assert not [name for name in wheel_names if name.startswith("tests/")]


def test_no_bytecode_or_caches_ship(wheel_names: tuple[str, ...]) -> None:
    """A ``__pycache__`` from a developer's Python version in somebody else's wheel."""
    junk = [
        name
        for name in wheel_names
        if "__pycache__" in name or name.endswith((".pyc", ".pyo", ".orig", ".rej"))
    ]
    assert not junk, f"the wheel carries build junk: {junk}"


def test_the_licence_and_notice_ship(wheel_names: tuple[str, ...]) -> None:
    """Apache-2.0 requires the licence with the distribution, and NOTICE carries the
    attribution for the byte vectors seeded from Apache PLC4X."""
    assert any(name.endswith("licenses/LICENSE") for name in wheel_names)
    assert any(name.endswith("licenses/NOTICE") for name in wheel_names)


def test_the_wheel_declares_no_runtime_dependencies() -> None:
    """Zero runtime dependencies, load-bearing: this wheel must install on a Jetson's
    aarch64 and on a locked-down plant PC with no compiler."""
    import tomllib

    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert config["project"]["dependencies"] == []
    assert config["project"]["optional-dependencies"]["testing"] == []
