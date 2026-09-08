"""The command line, and the package facade it must not drag a socket into.

Three things are proved here and only here.

**Importing ``aslmp`` does nothing.** DESIGN section 5.1.2 makes it a Tier 0 property
that ``aslmp.wire``, ``aslmp.errors``, ``aslmp.profile``, ``aslmp.commands`` and
``aslmp.blocks.layout`` stay importable in a process with no event loop and no socket --
and importing any of them executes ``aslmp/__init__.py`` first. Until this build unit
that file exported one string, so the property held by having nothing to break it. Now
it publishes 189 names across every layer, through a module-level ``__getattr__``
(PEP 562), and the property has to be defended rather than assumed. ``test_public_surface``
checks the same thing from the other side; this file checks the table itself.

**``aslmp --help`` costs one module.** The dispatcher hand-rolls its top-level help
instead of using ``argparse`` subparsers, because a subparser tree has to import every
subcommand to ask it for its arguments -- and one of them imports the client, the
transport and ``socket``. That is an easy thing to undo by accident and an invisible one
once undone, so it is asserted in a subprocess.

**The raw-socket bench control is byte-correct.** ``aslmp bench`` refuses to print a
table without a same-session control that shares no code with the library, which is only
worth anything if the control's hand-written frame is actually right. It is checked here
against the library's own frame builder: two independent expressions of the same 24-byte
request, and if they ever disagree, one of them is wrong and the bench is meaningless.

Everything else is the ordinary surface: every subcommand parses ``--help``, refuses
what it should refuse, and returns a status rather than raising.
"""

from __future__ import annotations

import ast
import importlib
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

import aslmp
from aslmp.tools import EXIT_FAILURE, EXIT_OK, EXIT_USAGE, SUBCOMMANDS
from aslmp.tools import bench as bench_tool
from aslmp.tools import proxy as proxy_tool
from aslmp.tools.__main__ import main
from aslmp.tools._common import columns, hexdump
from aslmp.wire.codec import BINARY
from aslmp.wire.frames import THREE_E, request_body
from aslmp.wire.route import Route

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE = REPO_ROOT / "src" / "aslmp"

FORBIDDEN_MODULES = ("socket", "ssl", "asyncio", "selectors", "threading", "logging")


def probe(source: str) -> str:
    """Run ``source`` in a fresh isolated interpreter and return its stdout."""
    result = subprocess.run(
        [sys.executable, "-I", "-c", source],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    assert result.returncode == 0, f"probe failed:\n{result.stdout}\n{result.stderr}"
    return result.stdout.strip()


# ========================================================================================
# aslmp/__init__.py -- the lazy re-export table
# ========================================================================================


def test_importing_aslmp_has_no_side_effects() -> None:
    """No socket, no loop, no thread, no file read, no work at all.

    The single most load-bearing assertion in this file. ``aslmp/__init__.py`` names
    every public class in the library, including ``Plc`` at layer 5 and ``TcpTransport``'s
    neighbours at layer 3. If any of those imports moves out of ``__getattr__`` and up
    to module scope, ``import aslmp.wire`` starts pulling ``socket`` into a process that
    is only trying to parse bytes, and DESIGN section 1.1's hard rule is gone.
    """
    report = probe(
        "import sys\n"
        "opened = []\n"
        "def hook(event, args):\n"
        "    if event in ('open', 'socket.socket', 'socket.connect'):\n"
        "        opened.append(event + '=' + str(args[0] if args else ''))\n"
        "sys.addaudithook(hook)\n"
        "import aslmp\n"
        "suffixes = ('.py', '.pyc', '.pyd', '.so', '.dll', '.zip')\n"
        "leaked = [n for n in "
        f"{FORBIDDEN_MODULES!r}"
        " if n in sys.modules]\n"
        "io = [e for e in opened if not e.lower().endswith(suffixes)]\n"
        # threading is imported only AFTER the leak check, so the probe's own
        # thread count cannot be the leak it is looking for.
        "import threading\n"
        "print(repr((leaked, io, threading.active_count())))\n"
    )
    leaked, io_events, threads = ast.literal_eval(report)
    assert leaked == [], (
        f"importing aslmp put {leaked} into sys.modules. The re-export table in "
        f"aslmp/__init__.py must resolve through __getattr__ (PEP 562); an eager import "
        f"there breaks every pure-layer guarantee in DESIGN section 1.1 at once."
    )
    assert io_events == [], f"importing aslmp performed I/O: {io_events}"
    assert threads == 1, f"importing aslmp left {threads} threads running"


def test_every_public_name_resolves() -> None:
    """Every name in ``__all__`` is reachable and comes from the module it claims."""
    from aslmp import _EXPORTS

    missing = []
    for name, module_name in _EXPORTS.items():
        try:
            value = getattr(aslmp, name)
        except AttributeError as exc:  # pragma: no cover -- the failure this test exists for
            missing.append(f"{name}: {exc}")
            continue
        source = importlib.import_module(module_name)
        assert getattr(source, name) is value, (
            f"aslmp.{name} is not the {module_name}.{name} the table claims"
        )
    assert not missing, "unreachable public names:\n  " + "\n  ".join(missing)


def test_all_is_the_table_plus_the_version() -> None:
    """``__all__`` and the lazy table name exactly the same set. No quiet extras."""
    from aslmp import _EXPORTS

    assert set(aslmp.__all__) == {"__version__", *_EXPORTS}
    assert aslmp.__all__ == sorted(
        aslmp.__all__, key=lambda name: (name != "__version__", name)
    )


# The table is hand-maintained, so the direction that actually rots is the one where a
# unit adds a public name and nobody adds the row. These two lock the sub-surfaces whose
# whole point is that a caller reaches them from the package root: you cannot inherit a
# base class you cannot import, and you cannot catch an exception you cannot name.

REEXPORTED_FACADES: tuple[str, ...] = ("aslmp.blocks",)
"""Facades whose ``__all__`` must be a SUBSET of the root's.

``aslmp.blocks`` only. The other exporting modules deliberately publish support types
that are not top-level (``Labelled``, ``Diagnostics``, ``TimingBuilder``, every
individual profile constant), so a bare set comparison against them would be a list
edited to silence a test rather than a rule.
"""


def test_the_blocks_facade_is_reachable_from_the_package_root() -> None:
    """A block class inherits ``PlcBlock`` and is bound with ``bind``; both are public.

    Regression: ``PlcBlock`` was in ``aslmp.blocks.__all__`` and absent from the root
    table, so the documented ``class LoopState(PlcBlock)`` failed on ``from aslmp import
    PlcBlock`` while every other name in the same example worked.
    """
    root = set(aslmp.__all__)
    for facade_name in REEXPORTED_FACADES:
        facade = importlib.import_module(facade_name)
        gap = sorted(name for name in facade.__all__ if name not in root)
        assert not gap, (
            f"{facade_name}.__all__ publishes {gap}, which aslmp.__all__ does not. Add "
            f"the row to _EXPORTS and the ``X as X`` line to the TYPE_CHECKING block."
        )


def test_every_exception_a_public_api_raises_is_catchable_from_the_root() -> None:
    """Regression: ``SlmpCadenceOverrunError`` lived only in ``aslmp.loop``.

    ``Cadence`` was exported and the error it raises was not, so the documented
    ``except SlmpCadenceOverrunError`` needed a second, deeper import to compile.

    ``aslmp.blocks.fields`` is on the list for the same reason and it rotted the same
    way: ``SlmpImplausibleValueError`` is what a bounded field raises and what the
    README tells a caller to catch, and it was reachable only as
    ``aslmp.blocks.fields.SlmpImplausibleValueError``. The two exceptions that live
    outside ``aslmp/errors`` are exactly the ones this test has to name explicitly, so
    it names them.
    """
    root = set(aslmp.__all__)
    gap: list[str] = []
    for module_name in (
        "aslmp.errors",
        "aslmp.loop",
        "aslmp.health",
        "aslmp.resilience",
        "aslmp.blocks.fields",
    ):
        module = importlib.import_module(module_name)
        gap += [
            f"{module_name}.{name}"
            for name in getattr(module, "__all__", ())
            if name.startswith("Slmp") and name.endswith("Error") and name not in root
        ]
    assert not gap, "public exception classes missing from aslmp.__all__: " + ", ".join(gap)


def test_dir_lists_the_lazy_names() -> None:
    """``dir(aslmp)`` includes the lazy names, so REPL completion finds them."""
    listed = dir(aslmp)
    assert "Plc" in listed
    assert "SlmpTimeoutError" in listed
    assert listed == sorted(listed)


def test_an_unknown_attribute_raises_attribute_error() -> None:
    """A typo is an ``AttributeError`` naming the module, not a silent ``None``."""
    with pytest.raises(AttributeError, match="has no attribute 'Plcc'"):
        getattr(aslmp, "Plcc")  # noqa: B009 -- the typo IS the test


def test_a_resolved_name_is_cached_in_globals() -> None:
    """The second lookup is an ordinary attribute access, not another import call.

    ``__getattr__`` writes the resolved object back into this module's globals, so the
    lazy table costs a dictionary lookup and an ``importlib`` call exactly once per
    name. A control loop that reads ``aslmp.Route`` is not paying for the indirection
    every cycle.
    """
    vars(aslmp).pop("Route", None)
    first = aslmp.Route
    assert vars(aslmp)["Route"] is first
    assert aslmp.Route is first


def test_the_error_hierarchy_is_reachable_from_the_top() -> None:
    """``except aslmp.SlmpTimeoutError`` works, which is how people will write it."""
    assert issubclass(aslmp.SlmpTimeoutError, aslmp.SlmpTransportError)
    assert issubclass(aslmp.SlmpTimeoutError, TimeoutError)
    assert issubclass(aslmp.SlmpUsageError, ValueError)
    assert not issubclass(aslmp.SlmpOutcomeUnknownError, aslmp.SlmpTransportError)


# ========================================================================================
# the dispatcher
# ========================================================================================


def test_help_imports_neither_asyncio_nor_socket() -> None:
    """``aslmp --help`` must not pay for the transport to print eleven lines.

    This is why ``tools/__main__.py`` hand-rolls its usage text. An ``argparse``
    subparser tree would import every subcommand module -- and ``aslmp.tools.read``
    imports ``aslmp.client``, which imports ``socket``.
    """
    leaked = probe(
        "import sys\n"
        "from aslmp.tools.__main__ import main\n"
        "import io, contextlib\n"
        "with contextlib.redirect_stdout(io.StringIO()):\n"
        "    main(['--help'])\n"
        f"print(','.join(n for n in {FORBIDDEN_MODULES!r} if n in sys.modules))\n"
    )
    assert not leaked, (
        f"`aslmp --help` imported [{leaked}]. Subcommands are imported lazily, one per "
        f"invocation, and the top-level help is a table of strings for this reason."
    )


def test_python_dash_m_aslmp_help_is_the_same_and_costs_the_same() -> None:
    """``python -m aslmp --help`` is the spelling reached for when the script is not on PATH.

    A strictly stronger claim than the test above, and the one ``aslmp/__main__.py``'s
    docstring makes: ``runpy`` imports the ``aslmp`` package **before** it runs
    ``aslmp/__main__.py``, so this exercises the lazy re-export table in
    ``aslmp/__init__.py`` as well as the subcommand table. Calling
    ``aslmp.tools.__main__.main`` directly skips the package facade entirely, so an
    eager import added there would pass that test and fail this one.
    """
    report = probe(
        "import runpy, sys, io, contextlib\n"
        "sys.argv = ['aslmp', '--help']\n"
        "out = io.StringIO()\n"
        "try:\n"
        "    with contextlib.redirect_stdout(out):\n"
        "        runpy.run_module('aslmp', run_name='__main__')\n"
        "except SystemExit as exc:\n"
        "    code = exc.code\n"
        "else:\n"
        "    code = 0\n"
        f"leaked = ','.join(n for n in {FORBIDDEN_MODULES!r} if n in sys.modules)\n"
        "print(repr((code, leaked, 'capabilities' in out.getvalue())))\n"
    )
    code, leaked, listed_a_subcommand = ast.literal_eval(report)
    assert code in (0, None), f"`python -m aslmp --help` exited {code!r}"
    assert listed_a_subcommand, "`python -m aslmp --help` printed no subcommand table"
    assert not leaked, (
        f"`python -m aslmp --help` imported [{leaked}]. aslmp/__main__.py delegates and "
        f"does nothing else; the cost came from aslmp/__init__.py or the subcommand table."
    )


@pytest.mark.parametrize("flag", ["--help", "-h", "help", ""])
def test_help_lists_every_subcommand(flag: str, capsys: pytest.CaptureFixture[str]) -> None:
    """Four spellings of "I do not know what to type", one answer."""
    assert main([flag] if flag else []) == EXIT_OK
    printed = capsys.readouterr().out
    for name in SUBCOMMANDS:
        assert name in printed


def test_version_prints_the_package_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--version"]) == EXIT_OK
    assert aslmp.__version__ in capsys.readouterr().out


def test_an_unknown_subcommand_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    """Named, listed, and never guessed at.

    No prefix matching and no spelling correction: ``aslmp w`` is unambiguous today and
    ambiguous the moment a twelfth subcommand starts with a w, and a shell script that
    changes meaning when a command is added is a trap.
    """
    assert main(["wrote"]) == EXIT_USAGE
    err = capsys.readouterr().err
    assert "unknown command 'wrote'" in err
    assert "write" in err


@pytest.mark.parametrize("name", sorted(SUBCOMMANDS))
def test_each_subcommand_module_has_the_expected_shape(name: str) -> None:
    """``run(argv) -> int`` and ``build_parser()``, and a ``--help`` that renders."""
    module = importlib.import_module(SUBCOMMANDS[name].module)
    assert callable(module.run)
    parser = module.build_parser()
    assert parser.prog == f"aslmp {name}"
    assert parser.format_help()


@pytest.mark.parametrize("name", sorted(SUBCOMMANDS))
def test_each_subcommand_help_exits_zero(name: str) -> None:
    """``aslmp <cmd> --help`` is argparse's own exit 0, not an error."""
    module = importlib.import_module(SUBCOMMANDS[name].module)
    with pytest.raises(SystemExit) as raised:
        module.run(["--help"])
    assert raised.value.code == 0


def test_the_subcommand_table_matches_the_files_on_disk() -> None:
    """Every row names a module that exists, and every module has a row.

    ``_common`` is the exception: it is shared machinery, not a subcommand, and a
    subcommand row for it would put it on the user's command line.
    """
    on_disk = {
        path.stem
        for path in (PACKAGE / "tools").glob("*.py")
        if not path.stem.startswith("_")
    }
    from_table = {row.module.rsplit(".", 1)[1] for row in SUBCOMMANDS.values()}
    assert on_disk == from_table


# ========================================================================================
# the subcommands that need no socket
# ========================================================================================


def test_cite_prints_a_command_with_its_sources(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["cite", "0x0403"]) == EXIT_OK
    printed = capsys.readouterr().out
    assert "0x0403" in printed
    assert "Device Read Random" in printed
    assert "SH(NA)-080956ENG" in printed


def test_cite_accepts_a_bare_hex_code(capsys: pytest.CaptureFixture[str]) -> None:
    """``403`` means 0x0403. Every Mitsubishi document prints these in hexadecimal and
    none of them in decimal, so there is no ambiguity to resolve."""
    assert main(["cite", "403"]) == EXIT_OK
    assert "Device Read Random" in capsys.readouterr().out


def test_cite_explains_an_end_code_with_its_measurement(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """0xC059 is the code the FX5U returns for monitor registration, measured."""
    assert main(["cite", "--end-code", "0xC059"]) == EXIT_OK
    printed = capsys.readouterr().out
    assert "0xC059" in printed
    assert "SlmpUnsupportedCommandError" in printed


def test_cite_refuses_an_unimplemented_command(capsys: pytest.CaptureFixture[str]) -> None:
    """No nearest match. Answering with a neighbouring row prints the wrong manual page."""
    assert main(["cite", "0x9999"]) == EXIT_FAILURE
    assert "raw_command" in capsys.readouterr().err


def test_cite_rejects_a_non_hex_code(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["cite", "04zz"]) == EXIT_USAGE
    assert "hexadecimal" in capsys.readouterr().err


def test_ambiguities_prints_the_probe_not_just_the_choice(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An ambiguity with no stated experiment is an opinion."""
    assert main(["ambiguities"]) == EXIT_OK
    printed = capsys.readouterr().out
    assert "aslmp does:" in printed
    assert "probe:" in printed
    assert "readings:" in printed


def test_ambiguities_keys_are_not_empty(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["ambiguities", "--keys-only"]) == EXIT_OK
    keys = capsys.readouterr().out.split()
    assert keys, "the shipped profiles and commands carry no ambiguity records at all"
    assert all(key.startswith("A-") for key in keys)


def test_ambiguities_names_an_unknown_key(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["ambiguities", "--key", "A-NOPE"]) == EXIT_FAILURE
    assert "--keys-only" in capsys.readouterr().err


def shipped_ambiguity_keys() -> tuple[str, ...]:
    """The keys in ``aslmp/data/ambiguities.tsv``, parsed here and not by aslmp.

    Deliberately a second, dumb reader. ``aslmp.data.read_table`` is what the command
    uses; a test that called it too would agree with the command about a row the reader
    dropped, which is the failure mode this whole section exists for.
    """
    table = PACKAGE / "data" / "ambiguities.tsv"
    keys: list[str] = []
    for line in table.read_text(encoding="ascii").splitlines():
        if not line or line.startswith("#"):
            continue
        key = line.split("\t")[0]
        if key.startswith("A-"):
            keys.append(key)
    return tuple(keys)


def test_ambiguities_surfaces_exactly_the_rows_the_shipped_table_holds(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Regression: 30 rows in the file, 14 lines out of the CLI, 12 distinct keys.

    The command walked the ``CpuProfile`` and ``CommandSpec`` objects, which carry an
    ``Ambiguity`` only where a profile or a command needed to name one -- and de-duplicated
    by VALUE, so ``A-CLEAR-MODE`` and ``A-REMOTE-FIXED`` each printed twice because two
    objects held the same key with different wording. Eighteen shipped records were
    unreachable from the command line that exists to print them.
    """
    expected = shipped_ambiguity_keys()
    assert len(expected) == len(set(expected)), "duplicate key in ambiguities.tsv"
    assert main(["ambiguities", "--keys-only"]) == EXIT_OK
    printed = capsys.readouterr().out.split()
    assert printed == list(expected), (
        f"the CLI surfaces {len(printed)} row(s) and the shipped table holds "
        f"{len(expected)}. Missing: {sorted(set(expected) - set(printed))}; "
        f"invented: {sorted(set(printed) - set(expected))}"
    )


def test_ambiguities_can_print_the_retraction_we_published(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``A-UDP-TAIL-LATENCY`` is where this library says it published a wrong number.

    It answered "no such ambiguity", which is a worse thing to say than the wrong number
    was: the retraction ships in the wheel and the command that exists to print it denied
    it existed.
    """
    assert main(["ambiguities", "--key", "A-UDP-TAIL-LATENCY"]) == EXIT_OK
    printed = capsys.readouterr().out
    assert "WITHDRAWN" in printed
    assert "resolved_by_measurement" in printed
    assert "FX5U-32MT/DS" in printed


def test_ambiguities_prints_the_status_of_every_row(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An open question and a settled one are different things and must read differently."""
    assert main(["ambiguities"]) == EXIT_OK
    printed = capsys.readouterr().out
    assert printed.count("  status:") == len(shipped_ambiguity_keys())
    assert "open" in printed
    assert "resolved_by_measurement" in printed


def test_ambiguities_status_filter_selects_a_subset(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["ambiguities", "--status", "open", "--keys-only"]) == EXIT_OK
    open_keys = capsys.readouterr().out.split()
    assert 0 < len(open_keys) < len(shipped_ambiguity_keys())
    assert main(["ambiguities", "--status", "settled", "--keys-only"]) == EXIT_FAILURE
    assert "resolved_by_measurement" in capsys.readouterr().err


@pytest.mark.parametrize("key", sorted(set(shipped_ambiguity_keys())))
def test_every_shipped_ambiguity_is_reachable_by_key(
    key: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """One row, one lookup. This is the assertion the old collector could not pass."""
    assert main(["ambiguities", "--key", key]) == EXIT_OK
    assert key in capsys.readouterr().out


def test_a_profile_filter_is_a_subset_of_the_table(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--profile`` narrows; it never reaches a row the shipped table does not hold."""
    assert main(["ambiguities", "--profile", "melsec:iq-f/fx5u", "--keys-only"]) == EXIT_OK
    keys = capsys.readouterr().out.split()
    assert keys, "the FX5U profile carries no ambiguities at all"
    assert set(keys) < set(shipped_ambiguity_keys())
    assert len(keys) == len(set(keys)), f"a key printed twice: {keys}"


def test_capabilities_does_not_deny_the_remote_control_we_sent(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The note told every user something that stopped being true on 2026-09-07.

    ``0x1001`` RUN, ``0x1002`` STOP and ``0x1003`` PAUSE were driven against
    FX5U-32MT/DS fw 1.065 from the laptop at 192.168.10.41 over Wi-Fi, TCP entries 5003
    and 5004, and checked against the scan counter the PLC program keeps in D8. The note
    still said "we deliberately never sent a remote-control command" -- an
    under-claim, which is a lie in the same way an over-claim is, and the more corrosive
    one in a file whose whole subject is what we did and did not verify.
    """
    assert main(["capabilities", "melsec:iq-f/fx5u"]) == EXIT_OK
    printed = capsys.readouterr().out
    assert "never sent a remote-control command" not in printed
    assert "2026-09-07" in printed
    assert "0x1005" in printed and "0x1006" in printed
    assert "never been sent" in printed
    assert "not verified" in printed


def test_capabilities_labels_the_iq_f_monitor_refusal(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """DESIGN section 5.11 requires the label to appear here, not only in a docstring."""
    assert main(["capabilities", "melsec:iq-f/fx5u"]) == EXIT_OK
    printed = capsys.readouterr().out
    assert "REFUSED" in printed
    assert "0xC059" in printed
    assert "not verified" in printed


def test_capabilities_unverified_only_hides_the_measured_rows(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["capabilities", "melsec:iq-f/fx5u", "--unverified-only"]) == EXIT_OK
    printed = capsys.readouterr().out
    assert "batch-access" not in printed, "batch access is measured, not manual"
    assert "block-access" in printed, "block access was never exercised on the bench"


def test_capabilities_without_a_profile_is_a_usage_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """There is no default profile anywhere in this library, including here."""
    assert main(["capabilities"]) == EXIT_USAGE
    assert "melsec:iq-f/fx5u" in capsys.readouterr().err


# ========================================================================================
# argument refusals that happen before any socket is opened
# ========================================================================================


def test_read_requires_an_explicit_type(capsys: pytest.CaptureFixture[str]) -> None:
    """``--as`` defaulted to ``u16``, which is a guess dressed as a default.

    On the bench this library was built against, ``D8`` holds a ``REAL`` -- the CPU's own
    ST does ``IO_Scan := IO_Scan + 1.0`` -- so ``aslmp read ... D8`` printed 54720: the
    low half of that float's bit pattern, a perfectly plausible integer, with the CPU
    answering 0x0000 either way. "What is in D8" is the first question a newcomer asks
    and the register does not know the answer, so the caller is asked instead, exactly as
    ``--profile`` asks rather than defaulting.
    """
    from aslmp.tools import read as read_tool

    status = read_tool.run(["1.2.3.4", "D8", "--profile", "melsec:iq-f/fx5u"])
    assert status == EXIT_USAGE
    message = capsys.readouterr().err
    assert "--as is required" in message
    assert "54720" in message
    assert "f32" in message and "words" in message  # it lists what to choose from


def test_read_still_accepts_every_kind_it_offers() -> None:
    """Requiring --as must not have narrowed what may be asked for. No socket here: the
    parser is asked directly, so this stays a statement about the arguments."""
    from aslmp.tools import read as read_tool

    parser = read_tool.build_parser()
    for kind in read_tool.KINDS:
        argv = ["1.2.3.4", "D0", "--profile", "melsec:iq-f/fx5u", "--as", kind]
        assert parser.parse_args(argv).kind == kind
    assert parser.parse_args(["1.2.3.4", "D0", "--profile", "x"]).kind is None


def test_read_refuses_a_count_for_a_scalar_kind(capsys: pytest.CaptureFixture[str]) -> None:
    """``--count 4 --as i32`` is a request that cannot be honoured, so it is refused
    rather than silently read as one value."""
    from aslmp.tools import read as read_tool

    status = read_tool.run(
        ["1.2.3.4", "D0", "--profile", "melsec:iq-f/fx5u", "--as", "i32", "--count", "4"]
    )
    assert status == EXIT_USAGE
    assert "meaningless" in capsys.readouterr().err


def test_read_requires_a_length_for_a_string(capsys: pytest.CaptureFixture[str]) -> None:
    """A string's word count is part of the request; it cannot be inferred from the
    response, which is exactly the mistake that makes string reads disagree across
    libraries."""
    from aslmp.tools import read as read_tool

    assert read_tool.run(["1.2.3.4", "D0", "--profile", "melsec:iq-f/fx5u", "--as", "str"]) == (
        EXIT_USAGE
    )
    assert "--length" in capsys.readouterr().err


def test_write_refuses_a_value_it_cannot_parse(capsys: pytest.CaptureFixture[str]) -> None:
    """Refused before the socket opens. Nothing was sent."""
    from aslmp.tools import write as write_tool

    status = write_tool.run(
        ["1.2.3.4", "M100", "maybe", "--profile", "melsec:iq-f/fx5u", "--as", "bit"]
    )
    assert status == EXIT_USAGE
    assert "on/off" in capsys.readouterr().err


def test_no_subcommand_can_reach_a_remote_control_command() -> None:
    """The command line has no path to Remote RUN, STOP, PAUSE, LATCH CLEAR or RESET.

    They can stop a running machine over an unauthenticated cleartext socket. The
    library gates them behind ``Plc(allow_remote_control=True)``; a shell history is not
    an interlock, so the CLI does not offer the switch at all.
    """
    forbidden_names = {
        "RemoteRun",
        "RemoteStop",
        "RemotePause",
        "RemoteLatchClear",
        "RemoteReset",
        "RemoteControl",
    }
    offenders = []
    for path in sorted((PACKAGE / "tools").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            # Read from the syntax tree, not from the text: several of these modules
            # explain in prose exactly why they do not do this, and a grep would flag
            # the explanation.
            if isinstance(node, ast.Call):
                for keyword in node.keywords:
                    if keyword.arg == "allow_remote_control":
                        offenders.append(f"{path.name}:{node.lineno}: allow_remote_control=")
            if isinstance(node, ast.Attribute) and node.attr == "remote":
                offenders.append(f"{path.name}:{node.lineno}: .remote")
            if isinstance(node, ast.Name) and node.id in forbidden_names:
                offenders.append(f"{path.name}:{node.lineno}: {node.id}")
    assert not offenders, (
        "a tools/ module reaches the remote-control surface:\n  " + "\n  ".join(offenders)
    )


def test_bench_refuses_to_run_without_a_control(capsys: pytest.CaptureFixture[str]) -> None:
    """``--no-control`` exists only so that the refusal can name it.

    The same rig gave p50 7.1 / p99 18.8 ms one day and p50 10.3 / p99 95.2 ms the next.
    A latency table with no same-session control is not a measurement.
    """
    status = bench_tool.run(
        ["1.2.3.4", "--profile", "melsec:iq-f/fx5u", "--no-control"]
    )
    assert status == EXIT_USAGE
    assert "not a measurement" in capsys.readouterr().err


def test_bench_refuses_an_encoding_its_control_cannot_speak(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The control is hand-written 3E binary. Benching ASCII would leave the library's
    rows with nothing to be compared against, so it is refused rather than printed
    alone."""
    status = bench_tool.run(
        ["1.2.3.4", "--profile", "melsec:iq-f/fx5u", "--encoding", "ascii-xy-hex"]
    )
    assert status == EXIT_USAGE
    assert "control" in capsys.readouterr().err


# ========================================================================================
# the bench control frame, checked against the library it is a control FOR
# ========================================================================================


def test_the_raw_control_frame_equals_the_library_frame() -> None:
    """Two independent expressions of one 3E binary ``0x0401`` read of D0, 2 words.

    The bench control is hand-written from literal bytes and ``struct`` precisely so
    that it shares no code with the library -- which makes it worth exactly nothing
    unless somebody checks that it is right. This is that check, and it is the strongest
    test in this file: a disagreement means either the control is measuring the wrong
    request, or the library is building one.

    It is also, incidentally, a golden vector: the expected bytes are written out.
    """
    control = bench_tool._control_request(0, 2)
    body = request_body(
        BINARY, monitoring_timer=0x0000, command=0x0401, subcommand=0x0000, payload=(
            b"\x00\x00\x00" + b"\xa8" + b"\x02\x00"
        )
    )
    library = THREE_E.build(route=Route.OWN_STATION, body=body, codec=BINARY)
    assert control == library
    expected = (
        "5000"          # subheader 5000 (request, 3E)
        "00"            # network No.
        "ff"            # station No.
        "ff03"          # module I/O No. 03FF
        "00"            # multidrop station No.
        "0c00"          # L = 12 wire units, timer through end of request data
        "0000"          # monitoring timer: wait indefinitely
        "0104"          # command 0x0401
        "0000"          # subcommand 0x0000
        "000000"        # head device number 0, three bytes little-endian
        "a8"            # device code: D
        "0200"          # 2 points
    )
    assert control.hex() == expected


def test_the_raw_control_frame_declares_the_length_it_carries() -> None:
    """``L`` counts from the monitoring timer to the end of the request data.

    Understated returns 0xC061 and the connection recovers; **overstated gets no
    response at all** and looks exactly like a dead PLC. A control that overstated its
    length would hang rather than measure, once, on the first sample.
    """
    frame = bench_tool._control_request(0, 960)
    declared = int.from_bytes(frame[7:9], "little")
    assert declared == len(frame) - 9
    assert declared == 12


def test_nearest_rank_never_invents_a_sample() -> None:
    """No interpolation: a published p99 must be a latency that actually happened."""
    samples = [1.0, 2.0, 3.0, 4.0, 100.0]
    assert bench_tool.nearest_rank(samples, 50) == 3.0
    assert bench_tool.nearest_rank(samples, 99) == 100.0
    assert bench_tool.nearest_rank(samples, 100) == 100.0
    assert bench_tool.nearest_rank([7.0], 50) == 7.0


def test_a_distribution_of_no_samples_reports_dashes_not_zeros() -> None:
    """A refused suite has no latency. Printing 0.00 would be a measurement it is not."""
    row = bench_tool.Distribution("block", (), "refused pre-transport").row()
    assert row[1] == "0"
    assert set(row[2:8]) == {"--"}
    assert "refused" in row[-1]


def test_percentiles_of_nothing_raise() -> None:
    with pytest.raises(ValueError, match="not a number"):
        bench_tool.nearest_rank([], 50)


# ========================================================================================
# the proxy's frame splitter -- TCP segmentation, without a socket
# ========================================================================================


def _sample_response() -> bytes:
    from aslmp.wire.frames import response_body

    return THREE_E.build_response(
        route=Route.OWN_STATION,
        body=response_body(BINARY, end_code=0x0000, payload=b"\x2a\x00\x2b\x00"),
        codec=BINARY,
    )


def test_the_proxy_splitter_reassembles_a_frame_one_byte_at_a_time() -> None:
    """Segmentation is real: 1 of 3 identical 1931-byte reads split at the 1460-byte
    MSS on FX5U-32MT/DS fw 1.065. A proxy that assumed one read is one frame would
    mis-attribute every large response."""
    frame = _sample_response()
    splitter = proxy_tool._Splitter(THREE_E, BINARY, response=True)
    produced: list[bytes] = []
    for index in range(len(frame)):
        produced.extend(splitter.feed(frame[index : index + 1]))
    assert produced == [frame]
    assert splitter.broken is None


def test_the_proxy_splitter_separates_two_coalesced_frames() -> None:
    """Two frames in one segment come out as two frames, in order.

    This is the shape of the measured TCP coalescing corruption seen from outside: the
    proxy must show both requests even though the CPU answered only the last.
    """
    frame = _sample_response()
    splitter = proxy_tool._Splitter(THREE_E, BINARY, response=True)
    assert splitter.feed(frame + frame) == [frame, frame]


def test_the_proxy_splitter_stops_annotating_rather_than_resynchronising() -> None:
    """A stream whose framing is lost cannot be recovered without guessing, and guessing
    invents transactions that never happened. Forwarding is unaffected -- it already
    happened before this code ran."""
    splitter = proxy_tool._Splitter(THREE_E, BINARY, response=True)
    assert splitter.feed(b"\x00" * 32) == []
    assert splitter.broken is not None
    assert splitter.feed(_sample_response()) == []


def test_the_proxy_describes_a_request_by_its_command_name() -> None:
    body = request_body(
        BINARY, monitoring_timer=0, command=0x0403, subcommand=0x0000, payload=b"\x00\x00"
    )
    frame = THREE_E.build(route=Route.OWN_STATION, body=body, codec=BINARY)
    described = proxy_tool._describe(THREE_E, BINARY, frame, response=False)
    assert "0x0403" in described
    assert "Device Read Random" in described


def test_the_proxy_says_undecodable_rather_than_guessing() -> None:
    described = proxy_tool._describe(THREE_E, BINARY, b"\xff" * 24, response=True)
    assert described.startswith("undecodable")


# ========================================================================================
# small shared helpers
# ========================================================================================


def test_columns_aligns_and_does_not_pad_the_last_cell() -> None:
    rendered = columns([["a", "one"], ["bbbb", "two"]])
    assert rendered == "a     one\nbbbb  two"


def test_columns_of_nothing_is_empty() -> None:
    assert columns([]) == ""


def test_hexdump_is_the_form_the_manuals_print() -> None:
    assert hexdump(b"\x50\x00\x00\xff") == "50 00 00 FF"


def test_hexdump_truncates_with_a_count() -> None:
    """A 1935-byte UDP response must not fill a terminal, and must say it was cut."""
    rendered = hexdump(bytes(1935), limit=4)
    assert rendered.endswith("(1935 bytes)")


# ========================================================================================
# the long-running subcommands -- output has to arrive before the command blocks forever
# ========================================================================================

LONG_RUNNING = ("serve", "proxy")
"""Subcommands that print and then block, instead of printing and then exiting.

Everything else in ``SUBCOMMANDS`` returns, and returning flushes. These two do not:
``serve`` waits on an ``asyncio.Event`` that is never set and ``proxy`` sits in
``serve_forever``, so every line either arrives while the command is running or is
never read at all. Python block-buffers stdout when it is not a terminal, which is
exactly the case a harness creates by redirecting the command to a file.
"""


@pytest.mark.parametrize("name", LONG_RUNNING)
def test_a_long_running_subcommand_flushes_every_line_it_prints(name: str) -> None:
    """The rule, not the instance.

    ``aslmp serve`` was found holding its whole banner -- including the ephemeral port
    from its default ``--port 0``, which is written down nowhere else -- in an 8 KB
    buffer for as long as the simulator was up. ``aslmp proxy`` had it too, and its
    entire purpose is a running log: measured on this Windows 11 host on 2026-09-07, a
    proxied ``aslmp read`` succeeded against 192.168.10.250:5003 while the redirected
    proxy log stayed 0 bytes. An unflushed ``print`` in either of these is the same
    defect wearing a different name, so it is a property of the module rather than a
    test of one line.
    """
    module = PACKAGE / "tools" / f"{name}.py"
    tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
    unflushed = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "print"
        and not any(
            keyword.arg == "flush"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is True
            for keyword in node.keywords
        )
    ]
    assert not unflushed, (
        f"aslmp/tools/{name}.py prints without flush=True at line(s) {unflushed}. This "
        f"command blocks after printing, so an unflushed line is a line the operator "
        f"never sees."
    )


def test_the_flush_scan_would_notice_an_unflushed_print() -> None:
    """A meta-test, because a scan that matches nothing passes every file.

    It also pins the comparison itself. ``ast`` nodes do not implement ``__eq__``, so
    ``keyword.value == ast.Constant(True)`` is False for every node in every file and
    the scan above would have passed on a module with no flush anywhere in it. This
    test caught exactly that.
    """
    tree = ast.parse("print('a')\nprint('b', flush=True)\nprint('c', flush=x)\n")
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    flushed = [
        any(
            keyword.arg == "flush"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is True
            for keyword in call.keywords
        )
        for call in calls
    ]
    assert flushed == [False, True, False]


def test_serve_flushes_its_banner_to_a_redirected_stdout(tmp_path: Path) -> None:
    """Backgrounding the simulator with stdout on a file is how a harness uses it.

    ``aslmp serve`` binds, prints the address of every entry, and then waits on an
    ``asyncio.Event`` that is never set. Python block-buffers stdout when it is not a
    terminal, so the banner sat in an 8 KB buffer for as long as the process was useful:
    the log was empty, and with the default ``--port 0`` the ephemeral port the harness
    needed was written down nowhere else.

    The process is killed rather than asked to stop, deliberately. A kill discards the
    buffer, so anything in the file was flushed while the simulator was running -- which
    is the property, and the only way to assert it from outside.
    """
    log = tmp_path / "serve.log"
    with log.open("w", encoding="utf-8") as sink:
        process = subprocess.Popen(
            [sys.executable, "-m", "aslmp.tools", "serve", "--host", "127.0.0.1"],
            stdout=sink,
            stderr=subprocess.STDOUT,
            cwd=REPO_ROOT,
        )
        try:
            deadline = time.monotonic() + 30.0
            banner = ""
            while time.monotonic() < deadline:
                banner = log.read_text(encoding="utf-8", errors="replace")
                if "Ctrl-C to stop." in banner:
                    break
                assert process.poll() is None, f"aslmp serve exited early:\n{banner}"
                time.sleep(0.05)
        finally:
            process.kill()
            process.wait(timeout=30)

    assert "Ctrl-C to stop." in banner, (
        f"aslmp serve did not flush its banner while it was running. What arrived in "
        f"{log.name} before it was killed:\n{banner}"
    )
    ports = re.findall(r"127\.0\.0\.1:(\d+)", banner)
    assert ports, f"the banner named no bound address:\n{banner}"
    assert all(int(port) > 0 for port in ports), (
        "an ephemeral entry reported port 0, so the banner is printed before the socket "
        "is bound and a harness still cannot find it"
    )
