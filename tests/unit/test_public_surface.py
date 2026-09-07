"""What `import aslmp` costs, and the seams no single build unit could check.

Two jobs, and they are the same job seen from two sides.

**Importing the package must do nothing.** DESIGN section 1.1 makes ``aslmp.wire``,
``aslmp.errors``, ``aslmp.profile``, ``aslmp.commands`` and ``aslmp.blocks.layout`` pure
bytes, importable in a process that has no event loop and no socket, and DESIGN section
1.2 says the end-code table has "no import-time parsing". Those are separate claims that
decay separately: a re-export added to ``aslmp/__init__.py`` drags the client in, and a
convenience ``read_table()`` at module scope turns a frozen literal back into a file
read on a plant PC whose wheel was installed read-only. Both are checked here in a fresh
subprocess with an audit hook, because both are properties of *importing*, which no test
running inside an already-imported process can observe.

**The seams between units must actually meet.** Units 1-8 were each implemented against
their own slice and could only assume the other side. Three of those assumptions are
load-bearing and none of them was checkable from inside one unit:

1. ``wire.address.AddressProfile`` is a structural protocol that layer 0 declares and
   ``aslmp.profile.CpuProfile`` (layer 1) is supposed to satisfy. Every address test in
   ``test_address.py`` uses a hand-written ``Stub``, so before this file nothing asserted
   that the profiles the library actually ships parse ``Y20`` as 16.
2. ``aslmp.wire`` cannot import ``aslmp.errors`` (layer 0 to layer 0.5 closes a cycle the
   layering test fails the build for), so it raises its own ``ValueError`` family. Unless
   exactly one table translates those into DESIGN section 3.1's classes, ``Y8`` -- the
   literal an FX5U-32MT/DS on firmware 1.065 was measured to *accept* with end code
   ``0x0000`` -- escapes ``except SlmpUsageError`` entirely.
3. ``L`` has one expression in the package (DESIGN section 4.2). The property tests
   assert the arithmetic; nothing asserted the *uniqueness*, which is the half that stops
   a second, drifting copy from appearing. An understated ``L`` returns ``0xC061`` and the
   connection recovers; an overstated one gets no response at all and looks exactly like a
   dead PLC.

The import *graph* is proved statically in ``tests/unit/test_layering.py``, by an AST walk
whose detectors have their own meta-tests. This file asserts the same rule from the other
end -- what is actually in ``sys.modules`` after a real import of the real package -- and
deliberately does not import that file's helpers. ``tests/`` has no ``__init__.py``, so a
cross-file import there is a module resolvable under two names, which ``mypy`` refuses; and
a static scan and a runtime observation are worth more as two independent witnesses than as
one shared implementation called twice.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from aslmp.commands.base import EncodeContext
from aslmp.commands.batch import ReadWords
from aslmp.errors import (
    SlmpAddressRangeError,
    SlmpAddressSyntaxError,
    SlmpDeviceNotOnCpuError,
    SlmpDeviceRadixError,
    SlmpFrameFormatError,
    SlmpProtocolError,
    SlmpUnknownDeviceError,
    SlmpUsageError,
)
from aslmp.errors.routing import protocol_error_for, usage_error_for
from aslmp.profile import Encoding, Link
from aslmp.profiles import ALL as ALL_PROFILES
from aslmp.profiles import FX5U, IQ_R
from aslmp.wire import raw as wire_raw
from aslmp.wire.address import AddressProfile, parse_address
from aslmp.wire.codec import ASCII, BINARY, Codec, SpecFormat
from aslmp.wire.frames import THREE_E, response_body
from aslmp.wire.reader import ResponseAccumulator
from aslmp.wire.route import Route

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE = REPO_ROOT / "src" / "aslmp"

# Everything a user may reasonably import without owning a socket. Each is imported in
# its own subprocess, because a leak that only happens when two of them are imported
# together is still a leak, and a leak that one of them hides is worse.
PUBLIC_ENTRY_POINTS = (
    "aslmp",
    "aslmp.wire",
    "aslmp.wire.frames",
    "aslmp.errors",
    "aslmp.errors.routing",
    "aslmp.profile",
    "aslmp.profiles",
    "aslmp.commands",
    "aslmp.identity",
    "aslmp.timing",
    "aslmp.observability",
)

FORBIDDEN_MODULES = ("socket", "ssl", "asyncio", "selectors", "threading", "logging")


# ========================================================================================
# importing does nothing
# ========================================================================================


def run_probe(source: str) -> str:
    """Run ``source`` in a fresh interpreter with no site packages and no inherited env."""
    result = subprocess.run(
        [sys.executable, "-I", "-c", source],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    assert result.returncode == 0, f"probe failed:\n{result.stdout}\n{result.stderr}"
    return result.stdout.strip()


@pytest.mark.parametrize("module", PUBLIC_ENTRY_POINTS)
def test_importing_the_public_surface_pulls_in_no_io_machinery(module: str) -> None:
    """No socket, no TLS, no event loop, no selector, no thread, no logging.

    ``aslmp/__init__.py`` is the one that will break this: importing ``aslmp.wire``
    executes the top-level package first, so the moment ``__init__`` eagerly imports
    ``client`` -- and therefore ``transport``, and therefore ``socket`` -- every row here
    fails at once. The fix is a module-level ``__getattr__`` (PEP 562), not a weaker
    assertion: DESIGN section 5.1.2 makes this a Tier 0 test, the tier whose failure
    means nothing else matters.
    """
    leaked = run_probe(
        f"import sys; import {module}; "
        f"print(','.join(n for n in {FORBIDDEN_MODULES!r} if n in sys.modules))"
    )
    assert not leaked, (
        f"importing {module} put [{leaked}] into sys.modules. Everything below layer 3 "
        f"is pure bytes and must stay importable in a process that has no event loop "
        f"and no socket."
    )


@pytest.mark.parametrize("module", PUBLIC_ENTRY_POINTS)
def test_importing_the_public_surface_opens_no_data_file(module: str) -> None:
    """The shipped tables are frozen Python literals, not a file read at import.

    DESIGN section 1.2: ``errors/endcodes.py`` is generated and committed precisely so
    that there is "no import-time parsing, no runtime dependency", and the same holds for
    ``wire/devicetable.py``. A ``read_table()`` at module scope would work on a developer
    machine and fail on a wheel installed somewhere the ``data/`` directory is not
    readable -- at import, before any handler exists to explain it.

    Source and bytecode files are excluded because the import system itself opens those.
    Anything else -- a ``.tsv``, a ``.jsonl``, a config file, a device node -- is a real
    read and fails this test.
    """
    probe = (
        "import sys\n"
        "opened = []\n"
        "def hook(event, args):\n"
        "    if event in ('open', 'socket.socket', 'socket.connect'):\n"
        "        opened.append(event + '=' + str(args[0] if args else ''))\n"
        "sys.addaudithook(hook)\n"
        f"import {module}\n"
        "suffixes = ('.py', '.pyc', '.pyd', '.so', '.dll', '.zip')\n"
        "print(';'.join(e for e in opened if not e.lower().endswith(suffixes)))\n"
    )
    unexpected = run_probe(probe)
    assert not unexpected, (
        f"importing {module} performed I/O at import time: {unexpected}. The device "
        f"table and the end-code table are committed Python literals for exactly this "
        f"reason; nothing in aslmp reads aslmp/data/*.tsv at runtime."
    )


def test_importing_aslmp_does_not_pull_in_the_tsv_reader() -> None:
    """``aslmp.data`` is a tool for the generators and the tests, never a dependency.

    Stronger than the audit-hook test and independent of it: the reader could be
    imported without reading anything today and start reading tomorrow. DESIGN section
    1.14 calls ``data/`` checked-in sources of truth; the *code* that parses them is not
    part of the client's import graph.
    """
    present = run_probe(
        "import sys, aslmp, aslmp.errors, aslmp.profiles, aslmp.commands; "
        "print('yes' if 'aslmp.data' in sys.modules else '')"
    )
    assert not present, (
        "importing the public surface pulled in aslmp.data, the strict TSV reader. The "
        "shipped tables are generated Python; if a runtime module now parses a TSV, the "
        "wheel has grown a filesystem dependency it does not declare."
    )


def test_importing_aslmp_starts_no_thread() -> None:
    """A background loop or a warm-up thread at import is a side effect with a pulse."""
    count = run_probe("import aslmp, threading; print(threading.active_count())")
    assert count == "1", (
        f"importing aslmp left {count} thread(s) running. aslmp.sync owns exactly one "
        f"background loop and it is created by constructing a facade, never by an import."
    )


# ========================================================================================
# the layering, observed rather than read
# ========================================================================================

# Layer 3 and above. Nothing a user imports for its bytes may drag one of these in.
UPPER_LAYERS = (
    "aslmp.transport",
    "aslmp.connection",
    "aslmp.client",
    "aslmp.timed",
    "aslmp.sync",
    "aslmp.health",
    "aslmp.resilience",
    "aslmp.entries",
    "aslmp.loop",
    "aslmp.blocks.plan",
    "aslmp.testing",
    "aslmp.tools",
)


@pytest.mark.parametrize("module", PUBLIC_ENTRY_POINTS)
def test_the_pure_surface_does_not_reach_the_stateful_half(module: str) -> None:
    """The import DAG of DESIGN section 1, observed at runtime instead of parsed.

    ``test_layering.py`` walks the AST and asserts no module imports at or above its own
    layer. That is the complete statement and it has its own meta-tests; this is the
    independent witness, and it catches the two things a static walk cannot: a lazy
    import inside a function that runs at module scope anyway, and a re-export in
    ``aslmp/__init__.py`` that pulls the client in behind every one of these names.

    ``aslmp.testing`` is on this list for DESIGN section 4.11's reason rather than for
    layering: the simulator must not be reachable from the client's import graph, or a
    transport bug becomes invisible to every client-to-server test.
    """
    present = run_probe(
        f"import sys; import {module}; "
        f"print(','.join(n for n in {UPPER_LAYERS!r} if n in sys.modules))"
    )
    assert not present, (
        f"importing {module} pulled in [{present}]. Layers 0-2.5 are the pure half of "
        f"the package and are importable with no socket, no loop and no simulator."
    )


# ========================================================================================
# DESIGN section 4.2 guard 1: L has exactly one expression
# ========================================================================================


def bare_name_assignments(tree: ast.AST) -> list[tuple[str, int]]:
    """Every assignment to a bare local name, with its line. Attributes do not count."""
    found: list[tuple[str, int]] = []

    def record(target: ast.expr) -> None:
        if isinstance(target, ast.Name):
            found.append((target.id, target.lineno))
        elif isinstance(target, ast.Tuple | ast.List):
            for element in target.elts:
                record(element)

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                record(target)
        elif isinstance(
            node, ast.AnnAssign | ast.AugAssign | ast.NamedExpr | ast.For | ast.AsyncFor
        ):
            record(node.target)
    return found


def length_named_assignments() -> list[str]:
    """Assignments named ``L`` or ``length`` in the two places DESIGN section 4.2 names."""
    files = [PACKAGE / "wire" / "frames.py", *sorted((PACKAGE / "commands").glob("*.py"))]
    offenders: list[str] = []
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for name, line in bare_name_assignments(tree):
            if name in {"L", "length"}:
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{line}: assignment to {name}")
    return offenders


def test_l_is_produced_in_exactly_one_place() -> None:
    """One assignment, in ``wire/frames.py``, and nothing named ``L`` in ``commands/``.

    DESIGN section 4.2 gives this arithmetic three guards because the failure is
    asymmetric, and this is the guard the other two cannot supply. ``payload_len ==
    len(encode)`` and ``len(build(...)) == prefix + L`` are both property tests over the
    *one* expression; neither notices a second expression appearing beside it. Understated
    returns ``0xC061`` and the connection recovers. **Overstated hangs with no response at
    all** -- measured on FX5U-32MT/DS fw 1.065 -- and is indistinguishable from a dead PLC.

    If this fails because a helper legitimately needs a local called ``length``, rename
    the local. The name is reserved on purpose, so that ``grep -n 'L = ' wire/frames.py``
    is a complete answer to "where does the data length come from".
    """
    offenders = length_named_assignments()
    assert len(offenders) == 1, (
        "L must be produced in exactly one expression in the package; found "
        f"{len(offenders)}:\n  " + "\n  ".join(offenders)
    )
    assert offenders[0].startswith("src\\aslmp\\wire\\frames.py") or offenders[0].startswith(
        "src/aslmp/wire/frames.py"
    ), f"the one L expression must live in wire/frames.py; found it at {offenders[0]}"


def test_the_l_scan_would_notice_a_second_expression(tmp_path: Path) -> None:
    """The detector fires, rather than passing because it walks nothing."""
    module = tmp_path / "m.py"
    module.write_text("def f(body):\n    L = len(body)\n    return L\n", encoding="utf-8")
    tree = ast.parse(module.read_text(encoding="utf-8"))
    assert ("L", 2) in bare_name_assignments(tree)
    assert ("length", 2) not in bare_name_assignments(tree)


# ========================================================================================
# seam: the shipped profiles satisfy the protocol layer 0 declares
# ========================================================================================


@pytest.mark.parametrize("key", sorted(ALL_PROFILES))
def test_every_shipped_profile_satisfies_the_address_protocol(key: str) -> None:
    """``CpuProfile`` is what ``parse_address`` is handed in production.

    ``wire/address.py`` declares ``AddressProfile`` structurally so that layer 0 never
    imports layer 1, and ``test_address.py`` exercises it through a two-line ``Stub``.
    That is the right shape for those tests and it means the real thing was never on the
    other end of the protocol in any assertion until here. The annotation below is the
    static half (``mypy`` fails if ``CpuProfile`` drifts from the protocol) and the parse
    is the runtime half.
    """
    profile: AddressProfile = ALL_PROFILES[key]
    assert profile.key == key
    assert parse_address("D0", profile).index == 0


@pytest.mark.parametrize(
    ("literal", "index", "why"),
    [
        ("Y0", 0, "1st output"),
        ("Y7", 7, "8th output; the last before the octal carry"),
        ("Y10", 8, "9th output -- sending the digits would reach the 11th"),
        ("Y17", 15, "16th output"),
        ("Y20", 16, "17th output; 0x10 on the wire, measured directly"),
        ("X17", 15, "the same carry on the input side"),
    ],
)
def test_the_measured_xy_vectors_hold_through_the_shipped_fx5u_profile(
    literal: str, index: int, why: str
) -> None:
    """Measured on FX5U-32MT/DS fw 1.065: the wire number is the LINEAR index.

    GX Works3 numbers an FX5's inputs and outputs in octal and the device-number field
    carries the **value** of that literal, never its digits. A client that sends the
    digits puts ``Y10`` on the wire as 10, which is the eleventh output: off by two, end
    code ``0x0000``, no error anywhere, and a physical output moving on a machine. The
    error grows with the address -- ``Y70`` sent as ``0x70`` is off by 56.

    The equivalent assertion in ``test_address.py`` runs against a stub profile. This one
    runs against ``aslmp.profiles.FX5U``, which is what a user gets.
    """
    assert parse_address(literal, FX5U).index == index, why


def test_the_radix_belongs_to_the_profile_and_not_to_the_letter() -> None:
    """``X17`` is 15 on an FX5U and 23 on an iQ-R, and neither is a property of ``X``."""
    assert parse_address("X17", FX5U).index == 15
    assert parse_address("X17", IQ_R).index == 23
    assert parse_address("Y20", FX5U).index == 16
    assert parse_address("Y20", IQ_R).index == 32


# ========================================================================================
# seam: layer-0 refusals reach the caller as DESIGN section 3.1 classes
# ========================================================================================


def fx5u_context(
    *, codec: Codec = BINARY, encoding: Encoding = Encoding.BINARY
) -> EncodeContext:
    """An FX5U context on the bench's own settings: TCP / binary / 3E, short spec."""
    return EncodeContext(
        codec=codec,
        spec=SpecFormat.SHORT,
        profile=FX5U,
        encoding=encoding,
        link=Link.CPU_BUILTIN,
    )


@pytest.mark.parametrize(
    ("literal", "expected", "why"),
    [
        ("Y8", SlmpDeviceRadixError, "not a legal octal address; the FX5U ACCEPTED it"),
        ("X1F", SlmpDeviceRadixError, "'F' is not an octal digit on an iQ-F"),
        ("XFFG", SlmpDeviceRadixError, "'G' is not a digit in any device radix"),
        ("D100x", SlmpDeviceRadixError, "names the offending character and its position"),
        ("QQ1", SlmpUnknownDeviceError, "no device family matches this prefix"),
        ("D", SlmpAddressSyntaxError, "a device with no number is not an address"),
        ("D100.5", SlmpAddressSyntaxError, "not shaped like an address at all"),
        ("V0", SlmpDeviceNotOnCpuError, "iQ-F has no index register V; measured 0xC05C"),
        ("D8000", SlmpAddressRangeError, "D ends at D7999; measured 0xC056"),
    ],
)
def test_every_bad_address_reaches_the_caller_as_a_usage_error(
    literal: str, expected: type[SlmpUsageError], why: str
) -> None:
    """DESIGN section 3.1: validation raises ``SlmpUsageError`` and nothing was sent.

    Half of these are refused by ``profile.check_range`` at layer 1, which may import
    ``aslmp.errors`` and raises the public class directly. The other half are refused by
    ``parse_address`` at layer 0, which may not, and which raises its own ``ValueError``
    family. Before ``errors.routing.usage_error_for`` translated them at the one door in
    ``commands/base.py``, ``V0`` was a ``SlmpUsageError`` and ``Y8`` was not -- and to the
    person who typed them they are the same mistake.

    ``Y8`` is the one that matters most: it is not a legal octal address at all, and an
    FX5U-32MT/DS on firmware 1.065 **accepted** a write at that wire number and answered
    end code ``0x0000``. Refusing it is client-side validation that nothing else performs,
    so it had better be catchable.
    """
    with pytest.raises(expected) as caught:
        ReadWords(literal, 1).validate(fx5u_context())
    assert isinstance(caught.value, SlmpUsageError), why
    assert isinstance(caught.value, ValueError), "section 3.2 decision 1"


def test_a_translated_address_error_keeps_the_layer_0_detail_as_its_cause() -> None:
    """The wrapper never re-parses a message to recover what the parser already knew."""
    with pytest.raises(SlmpDeviceRadixError) as caught:
        ReadWords("Y8", 1).validate(fx5u_context())
    cause = caught.value.__cause__
    assert cause is not None
    assert getattr(cause, "character", None) == "8"
    assert getattr(cause, "device", None) == "Y"
    assert getattr(cause, "text", None) == "Y8"


# ========================================================================================
# seam: a codec refusal inside a frame is a protocol error, not a bare ValueError
# ========================================================================================


def ascii_response(payload: bytes) -> bytes:
    return THREE_E.build_response(
        route=Route.OWN_STATION,
        body=response_body(ASCII, end_code=0, payload=payload),
        codec=ASCII,
    )


@pytest.mark.parametrize(
    ("field", "offset"),
    [("the access route", 7), ("the declared length L", 15), ("the end code", 19)],
)
def test_a_bad_hex_nibble_in_a_frame_field_is_a_frame_format_error(
    field: str, offset: int
) -> None:
    """DESIGN section 3.1 files "non-hex in ASCII" under ``SlmpFrameFormatError``.

    ``ASCII.read_number`` refuses the character rather than reading it as zero, which is
    right (``libslmp2``'s ``wordcodec.c:27`` returns 0 for a bad nibble, so ``"ZZZZ"``
    decodes to a plausible ``0``). But it raises the layer-0 ``SlmpCodecError``, which is
    **not** a ``wire.raw.SlmpFrameError``, so ``errors.routing.protocol_error_for`` --
    whose map is keyed on that family -- refused it with a ``TypeError`` and the original
    escaped the whole ``SlmpError`` tree. The frame parser holds the frame, the codec and
    the offset, so it is the layer that translates, and the public face then falls out of
    the mapping that already existed.

    Both halves are asserted, because either one alone is a promise about the other.
    """
    frame = bytearray(ascii_response(b"1234"))
    frame[offset] = ord("Z")
    accumulator = ResponseAccumulator(THREE_E, ASCII)
    with pytest.raises(wire_raw.SlmpFrameFormatError) as caught:
        accumulator.feed(bytes(frame))
        accumulator.take()
    public = protocol_error_for(caught.value)
    assert isinstance(public, SlmpFrameFormatError)
    assert isinstance(public, SlmpProtocolError)
    assert public.__cause__ is caught.value


def test_a_bad_hex_nibble_in_the_payload_is_a_protocol_error_at_decode() -> None:
    """The frame layer treats the payload as opaque; the command is what decodes it.

    ``Command.checked_decode`` is the door, not ``decode``: the same
    ``SlmpCodecError`` would otherwise escape a caller's ``except SlmpProtocolError``
    around the one operation it exists for.
    """
    ctx = fx5u_context(codec=ASCII, encoding=Encoding.ASCII_XY_HEX)
    command = ReadWords("D0", 1)
    assert command.checked_decode(b"0064", ctx) == (100,)
    with pytest.raises(SlmpFrameFormatError):
        command.checked_decode(b"006Z", ctx)


def test_a_binary_bit_nibble_that_is_neither_zero_nor_one_is_a_protocol_error() -> None:
    """``bool(nibble)`` would be a silent fix-up. The codec raises; the command wraps."""
    from aslmp.commands.batch import ReadBits

    ctx = fx5u_context()
    assert ReadBits("M0", 2).checked_decode(b"\x10", ctx) == (True, False)
    with pytest.raises(SlmpFrameFormatError):
        ReadBits("M0", 2).checked_decode(b"\x21", ctx)


def test_the_usage_translation_refuses_an_exception_it_does_not_recognise() -> None:
    """Symmetric with ``protocol_error_for``: no class is ever guessed.

    A table that fell back to a base class for anything it did not recognise would turn
    a bug in aslmp into a plausible-looking ``SlmpUsageError`` blaming the caller. An
    unrecognised failure must reach the caller as itself.
    """
    with pytest.raises(TypeError, match=r"does not recognise"):
        usage_error_for(ValueError("not an address refusal"))
