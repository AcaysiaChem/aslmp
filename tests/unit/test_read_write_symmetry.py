"""The symmetry property, asserted structurally, so the next sibling cannot be left behind.

Three adversarial rounds on this package produced the same criticism three times, and the
third time it landed on a fix made in the round that named it: **a fix here has a sibling
one function away every single time, and the sibling is never found by the person who made
the fix.** ``encoded()`` was added to close ``write_str`` and ``read_str`` was left raising
a bare ``UnicodeDecodeError``. ``aslmp read --as`` was made required because "a register
carries no type on the wire, so there is no default that could be right" and ``aslmp write
--as`` kept its ``u16``. ``RandomWrite.wire_value()`` was taught to read a point's declared
domain and ``u16`` was left declaring none, on the half that changes the plant.

Every one of those is the same shape: **a door that refuses on one side and leaks on the
other.** This file is not a list of those defects. It is the *property* they all violate,
asserted by reflection and by AST over whatever the package contains today, so that a
read added tomorrow without its writer's guard fails here rather than in a plant.

Everything is discovered, nothing is enumerated. Where a constant does appear it is a
**fail-closed exemption**: two parameter names that are legitimately one-directional. Add
a third asymmetric keyword and this file fails until somebody writes down why. That is the
opposite of a hand-kept list, which fails by staying silent.

Nothing here opens a socket. A refusal that reaches the socket is not a refusal these
tests would accept: every value probe runs on a client that has never connected, so a
``SlmpNotConnectedError`` is the *proof* that a good value got as far as the transport and
a ``SlmpValueRangeError`` is the proof that a bad one did not.
"""

from __future__ import annotations

import ast
import asyncio
import importlib
import inspect
import re
from pathlib import Path
from typing import Any, cast

import pytest

from aslmp.blocks.fields import Bounds, check_reading
from aslmp.client import Plc, _check_writing, _string_words
from aslmp.commands.base import decoded, encoded
from aslmp.commands.random import (
    _POINT_DOMAINS,
    AccessWidth,
    PointKind,
    RandomPoint,
    RandomWrite,
)
from aslmp.errors import (
    SlmpConfigurationError,
    SlmpNotConnectedError,
    SlmpPayloadShapeError,
    SlmpUsageError,
    SlmpValueRangeError,
)
from aslmp.sync import Plc as SyncPlc
from aslmp.timed import TimedApi
from aslmp.tools import EXIT_USAGE, SUBCOMMANDS
from aslmp.tools import read as read_tool
from aslmp.tools import write as write_tool

REPO_ROOT = Path(__file__).resolve().parents[2]
CLIENT = REPO_ROOT / "src" / "aslmp" / "client.py"
BENCH = "192.168.10.250"

READ_ONLY_KEYWORDS = frozenset({"allow_split"})
"""Keywords a read may have that its writer legitimately does not. **Fail-closed.**

``allow_split`` asks for several ``0403``s instead of one and returns a different type to
say so. There is no write analogue and there must not be one: splitting a snapshot costs
atomicity a reader can reason about afterwards, while splitting a write puts a plant
through a state nobody asked for. One name, with a reason. A second name appearing on a
read and not on its writer fails :func:`test_a_read_write_pair_takes_the_same_value_domain`
until somebody adds it here deliberately.
"""

WRITE_ONLY_KEYWORDS = frozenset({"verify"})
"""Keywords a write may have that its reader legitimately does not. **Fail-closed.**

``verify`` is a read-back *after* a write. A read has nothing to verify against.
"""

_NUMERIC_KIND = re.compile(r"\A(?P<sign>[iu])(?P<bits>8|16|32|64)\Z|\Af(?P<real>32|64)\Z")
"""What a typed scalar's suffix looks like, and where its domain comes from.

The domain is **computed from the name**, not looked up in a table beside it. A
``read_i64``/``write_i64`` added tomorrow gets its probes for free; a ``read_bcd`` added
tomorrow matches nothing here and :func:`test_every_typed_pair_enforces_the_same_domain`
fails saying so, which is the entire point.
"""


# ========================================================================================
# Discovery -- every pair this package has, found rather than listed
# ========================================================================================


def _public_methods(surface: type) -> dict[str, Any]:
    return {
        name: member
        for name, member in inspect.getmembers(surface, inspect.isfunction)
        if not name.startswith("_")
    }


def _takes_a_target(function: Any) -> bool:
    """Whether this method names something to read from or write to.

    The derived line between "a device access" and "a question about the CPU":
    ``read_type_name()`` and ``read_cpu_status()`` take nothing but ``self`` and have no
    write counterpart in SLMP at all, and no exemption list is needed to say so.
    """
    return len(inspect.signature(function).parameters) > 1


def pair_names(surface: type = Plc) -> list[str]:
    """Every ``read_X`` on ``surface`` that addresses something, by its ``X``."""
    methods = _public_methods(surface)
    return sorted(
        name[len("read_") :]
        for name, member in methods.items()
        if name.startswith("read_") and _takes_a_target(member)
    )


def keyword_parameters(function: Any) -> dict[str, Any]:
    return {
        parameter.name: parameter.default
        for parameter in inspect.signature(function).parameters.values()
        if parameter.kind is inspect.Parameter.KEYWORD_ONLY
    }


SURFACES: tuple[type, ...] = (Plc, TimedApi, SyncPlc)
"""The three public spellings of the same calls. A guard added to one is a guard added to
none until it is on all three, which is what ``plc.timed`` and ``aslmp.sync`` are for."""


# ========================================================================================
# Every read has a writer, and every writer has a read
# ========================================================================================


@pytest.mark.parametrize("surface", SURFACES, ids=lambda cls: cls.__module__)
def test_every_addressed_read_has_a_writer(surface: type) -> None:
    """A device this package can observe is a device it can be asked to change.

    Derived, not listed: a read that takes no target -- ``read_type_name``,
    ``read_cpu_status`` -- is a question about the CPU and is exempt by its own signature.
    """
    methods = _public_methods(surface)
    missing = [name for name in pair_names(surface) if f"write_{name}" not in methods]
    assert not missing, (
        f"{surface.__module__}.{surface.__name__} can read {missing} and cannot write "
        f"them. If that is deliberate, the reason belongs in the method's docstring and "
        f"the read should stop taking an address."
    )


@pytest.mark.parametrize("surface", SURFACES, ids=lambda cls: cls.__module__)
def test_every_write_has_a_read(surface: type) -> None:
    """The other direction, so that ``--verify`` and the read-back always have a door."""
    methods = _public_methods(surface)
    missing = [
        name[len("write_") :]
        for name in methods
        if name.startswith("write_") and f"read_{name[len('write_'):]}" not in methods
    ]
    assert not missing, f"{surface.__name__} can write {missing} and cannot read them."


# ========================================================================================
# The two halves take the same value-domain keywords
# ========================================================================================


PAIR_PARAMS = [
    pytest.param(surface, name, id=f"{surface.__module__}-{name}")
    for surface in SURFACES
    for name in pair_names(surface)
]
"""Every (surface, pair) this package actually has. ``plc.timed`` carries no block plan,
so it contributes no ``block`` row rather than needing an exemption for one."""


@pytest.mark.parametrize("surface,name", PAIR_PARAMS)
def test_a_read_write_pair_takes_the_same_value_domain(surface: type, name: str) -> None:
    """The property the ``minimum``/``maximum`` defect broke, stated once for every pair.

    ``read_f32("D2", minimum=0, maximum=100)`` refused an implausible reading while
    ``write_f32("D2", 1e6)`` had no way to say the same thing -- on the half that changes
    the plant, in a package whose *block* fields have enforced bounds in both directions
    since bounds existed. Same keywords, same defaults, both directions, or a name in one
    of the two fail-closed sets above with a reason beside it.
    """
    reader = keyword_parameters(getattr(surface, f"read_{name}"))
    writer = keyword_parameters(getattr(surface, f"write_{name}"))
    read_only = set(reader) - set(writer) - READ_ONLY_KEYWORDS
    write_only = set(writer) - set(reader) - WRITE_ONLY_KEYWORDS
    assert not read_only, (
        f"read_{name} takes {sorted(read_only)} and write_{name} does not. A value that "
        f"crosses in one direction with a constraint and in the other without it is the "
        f"defect this file exists for."
    )
    assert not write_only, f"write_{name} takes {sorted(write_only)} and read_{name} does not."
    for keyword in set(reader) & set(writer):
        assert reader[keyword] == writer[keyword], (
            f"read_{name}({keyword}=) defaults to {reader[keyword]!r} and "
            f"write_{name}({keyword}=) to {writer[keyword]!r}."
        )


# ========================================================================================
# The two halves enforce the same numeric domain, and the domain comes from the name
# ========================================================================================


def probes(kind: str) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
    """``(inside, outside)`` for one typed scalar, **computed from its own name**.

    No table to keep. ``u16`` is 0..65535 because it says ``u`` and ``16``; ``i32`` is
    two's complement of 32 bits because it says ``i`` and ``32``. An unrecognised suffix
    raises, and the test that calls this reports it as a pair with no declared domain --
    which is exactly what a new kind added without a guard would be.
    """
    match = _NUMERIC_KIND.match(kind)
    if match is None:
        raise LookupError(kind)
    if match["real"] is not None:
        bits = int(match["real"])
        # A finite magnitude past the format's maximum exists as a Python float only for
        # the narrower width; for f64 the only value outside the domain is a non-number.
        too_large: tuple[Any, ...] = (1e39,) if bits == 32 else ()
        return (0.0, 1.5, -3.25), (*too_large, "1.0", None)
    bits = int(match["bits"])
    if match["sign"] == "i":
        top = 1 << (bits - 1)
        return (-top, -1, 0, top - 1), (-top - 1, top, 1.5, "1")
    return (0, (1 << bits) - 1), (-1, 1 << bits, 1.5, "1")


def refusal(call: Any) -> BaseException | None:
    """Run one un-connected call and return what refused it, or ``None`` if nothing did.

    ``SlmpNotConnectedError`` means the value was accepted and the call reached the
    transport, which on a client that has never connected is as far as an accepted value
    can get. Anything else is a refusal, and every refusal this file wants is raised
    before a byte is built.
    """
    try:
        asyncio.run(call())
    except SlmpNotConnectedError:
        return None
    # Every exception is data here: the test compares which door raised, not what.
    except BaseException as exc:
        return exc
    return None


def a_client() -> Plc:
    """A client aimed at the bench, which never connects. Constructing one opens nothing."""
    return Plc(BENCH, 5002, profile="melsec:iq-f/fx5u")


def numeric_pairs() -> list[str]:
    """Every pair whose read returns one number, taken from the **return annotation**.

    Not from the name and not from a list: ``read_f32`` is a typed scalar because it is
    annotated ``-> float``, and ``read_words`` is not because it is annotated
    ``-> tuple[int, ...]``. A ``read_i64 -> int`` added tomorrow is in this set the
    moment it exists, whether or not anybody remembered this file.
    """
    scalars = []
    for name in pair_names():
        annotation = inspect.signature(getattr(Plc, f"read_{name}")).return_annotation
        if str(annotation) in ("int", "float"):
            scalars.append(name)
    return scalars


TYPED_PAIRS: list[str] = numeric_pairs()
"""The pairs whose domain is a number. Discovered, so a new width joins by existing."""


def test_every_typed_pair_has_a_domain_this_file_can_derive() -> None:
    """The fail-closed half of the discovery: no numeric pair may be silently un-probed.

    A ``read_bcd -> int``/``write_bcd`` added tomorrow is a numeric pair by its own
    annotation and matches no rule in :data:`_NUMERIC_KIND`, so it lands here rather than
    slipping past every test below it.
    """
    undeclared = sorted(name for name in numeric_pairs() if not _NUMERIC_KIND.match(name))
    assert not undeclared, (
        f"{undeclared} read as numbers and this file cannot derive a value domain for "
        f"them, so nothing below is checking that their two halves agree. Teach "
        f"_NUMERIC_KIND the new width."
    )


@pytest.mark.parametrize("kind", TYPED_PAIRS)
def test_every_typed_pair_enforces_the_same_domain(kind: str) -> None:
    """A value the reader could never return is a value the writer must never send.

    The writer is the half that is checked here because it is the half that can be
    checked with no PLC: the reader's domain is the width it unpacks, which ``struct``
    enforces by construction. What this asserts is that the *writer* holds every value to
    that same width, before the socket -- never masked, never clamped, never truncated.
    """
    inside, outside = probes(kind)
    writer = getattr(a_client(), f"write_{kind}")
    for value in inside:
        assert refusal(lambda v=value: writer("D100", v)) is None, (
            f"write_{kind} refused {value!r}, which is inside the domain its own name "
            f"declares."
        )
    for value in outside:
        caught = refusal(lambda v=value: writer("D100", v))
        assert isinstance(caught, SlmpValueRangeError), (
            f"write_{kind}({value!r}) raised {caught!r} rather than an "
            f"SlmpValueRangeError. Anything outside the declared width must be refused "
            f"before a byte is built; nothing here masks to fit."
        )


# ========================================================================================
# The declared bounds are enforced in both directions, by two guards that agree
# ========================================================================================


def _guarded_methods(prefix: str, guard: str) -> list[str]:
    """Every ``client.py`` method whose name starts with ``prefix`` and calls ``guard``."""
    tree = ast.parse(CLIENT.read_text(encoding="utf-8"), filename=str(CLIENT))
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
            continue
        if not node.name.startswith(prefix):
            continue
        calls = {
            inner.func.id
            for inner in ast.walk(node)
            if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name)
        }
        if guard in calls:
            found.append(node.name)
    return found


def test_both_halves_of_every_bounded_pair_are_wired_to_a_guard() -> None:
    """AST, not signatures: a keyword that is accepted and never read is worse than none.

    ``write_f32(minimum=...)`` could take the argument and drop it and every signature
    test in this file would pass. This asserts that each half's body actually calls its
    guard -- ``check_reading`` on the way in, ``_check_writing`` on the way out -- and
    that the two sets of methods are the same pairs.
    """
    reads = {name[len("read_") :] for name in _guarded_methods("read_", "check_reading")}
    writes = {name[len("write_") :] for name in _guarded_methods("write_", "_check_writing")}
    assert reads, "no read on Plc calls check_reading; the guard has lost its callers"
    assert reads == writes, (
        f"these are bounded on the way in and not on the way out: "
        f"{sorted(reads - writes)}; and on the way out and not in: {sorted(writes - reads)}"
    )
    assert reads == set(TYPED_PAIRS), (
        f"the bounded pairs {sorted(reads)} are not the typed pairs {sorted(TYPED_PAIRS)}"
    )


@pytest.mark.parametrize("kind", TYPED_PAIRS)
def test_a_declared_bound_stops_a_write_before_the_socket(kind: str) -> None:
    """The write half of the promise, on a client that has never connected."""
    inside, _outside = probes(kind)
    good = inside[-1]
    writer = getattr(a_client(), f"write_{kind}")
    assert refusal(lambda: writer("D100", good, minimum=None, maximum=None)) is None
    caught = refusal(lambda: writer("D100", good, minimum=float(good) + 1))
    assert isinstance(caught, SlmpValueRangeError), (
        f"write_{kind}({good!r}, minimum={float(good) + 1!r}) was not refused; it "
        f"returned {caught!r}. A bound is a statement about what may be in that "
        f"register, and a write is the other way something gets there."
    )


@pytest.mark.parametrize(
    "minimum,maximum",
    [
        (float("nan"), None),
        (None, float("nan")),
        (10.0, 1.0),
    ],
)
def test_both_guards_refuse_the_same_impossible_declaration(
    minimum: float | None, maximum: float | None
) -> None:
    """The two guards judge the same range, so they must reject the same declarations.

    ``check_reading`` open-codes these three refusals and ``_check_writing`` gets them
    from :class:`~aslmp.blocks.fields.Bounds`. Two spellings of one rule is how the two
    directions drift apart, and this is the assertion that says they have not: a NaN
    bound compares false against every value and would silently never fire; an inverted
    range fires on every value.
    """
    with pytest.raises(SlmpUsageError):
        check_reading(
            1.0, minimum, maximum, field="read_f32", address="D0", registers=(0, 0)
        )
    with pytest.raises(SlmpUsageError):
        _check_writing(
            1.0, minimum, maximum, field="write_f32", kind="f32", address="D0"
        )


def test_the_two_guards_agree_on_every_value_they_are_shown() -> None:
    """Same range, same verdict, opposite error class -- and the classes are the point.

    A read that is outside its bounds is *semantic*: the CPU answered ``0x0000`` and the
    registers are the registers it sent. A write that is outside them is *usage*: nothing
    was sent at all. Both are :class:`~aslmp.errors.SlmpError`.
    """
    bounds = Bounds(0.0, 100.0)
    for value in (-1e9, -0.001, 0.0, 50.0, 100.0, 100.001, float("nan")):
        reading_failed = False
        try:
            check_reading(
                value, 0.0, 100.0, field="read_f32", address="D0", registers=(0, 0)
            )
        except Exception:  # the class is asserted by the write half below
            reading_failed = True
        writing_failed = False
        try:
            _check_writing(
                value, 0.0, 100.0, field="write_f32", kind="f32", address="D0"
            )
        except SlmpValueRangeError:
            writing_failed = True
        assert reading_failed == writing_failed == bounds.excludes(value), (
            f"{value!r}: the read guard {'refused' if reading_failed else 'allowed'} it "
            f"and the write guard {'refused' if writing_failed else 'allowed'} it."
        )


# ========================================================================================
# A point's declared kind means the same thing as the method of the same name
# ========================================================================================


def writable_point_kinds() -> list[PointKind]:
    """Every ``0403``/``1402`` point kind a :class:`RandomWrite` will carry.

    Discovered by asking ``RandomWrite`` rather than by listing: ``bits`` is excluded
    because that class refuses it at construction, and it stays excluded for exactly as
    long as that stays true.
    """
    found: list[PointKind] = []
    for kind in _POINT_DOMAINS:
        width = AccessWidth.WORD if kind in ("u16", "i16", "bits") else AccessWidth.DWORD
        try:
            RandomWrite(RandomPoint("D100", width, kind), 0)
        except Exception:  # an unwritable kind is what is being detected
            continue
        found.append(kind)
    return found


@pytest.mark.parametrize("kind", writable_point_kinds())
def test_a_point_kind_and_the_writer_of_that_name_share_one_domain(kind: str) -> None:
    """Two doors onto the same register, both naming ``u16``, disagreeing about ``-1``.

    ``Plc.write_u16(-1)`` refused while ``RandomWrite(word('D101', kind='u16'), -1)``
    masked to ``0xFFFF`` and ``write_random`` sent it: ``D101`` read back 65535 on
    FX5U-32MT/DS fw 1.065 over TCP 5002 from this host, 2026-09-07. A refusal that
    depends on which door you came through is not a refusal, and this asserts the
    property for every kind rather than for the one that was found.
    """
    assert hasattr(Plc, f"write_{kind}"), (
        f"a {kind!r} point can be written by 1402 and Plc has no write_{kind} to agree "
        f"with. Two vocabularies for one set of types is how they drift."
    )
    inside, outside = probes(kind)
    width = AccessWidth.WORD if kind in ("u16", "i16") else AccessWidth.DWORD
    point = RandomPoint("D100", width, cast("PointKind", kind))
    writer = getattr(a_client(), f"write_{kind}")
    for value in (*inside, *outside):
        through_the_point: BaseException | None = None
        try:
            RandomWrite(point, value).wire_value()
        except Exception as exc:  # compared against the other door, not handled
            through_the_point = exc
        through_the_method = refusal(lambda v=value: writer("D100", v))
        assert (through_the_point is None) == (through_the_method is None), (
            f"{value!r} through a {kind} access point: "
            f"{through_the_point!r}; through Plc.write_{kind}: {through_the_method!r}. "
            f"The same named type must have the same domain at every door."
        )


# ========================================================================================
# The string door refuses on both sides, and its length means one thing
# ========================================================================================


def test_the_string_length_is_bytes_on_both_halves_and_says_so() -> None:
    """``length`` was documented "in characters" and was the byte window in every
    implementation, which is what cut a ``shift_jis`` pair in half.

    The first line of a docstring is what a reader and an IDE show, so that is the line
    asserted. If a future edit puts "characters" back on either half, this fails.
    """
    assert _string_words(4) == 2, "four bytes are two registers, at two bytes per register"
    for surface in SURFACES:
        for name in ("read_str", "write_str"):
            first = (getattr(surface, name).__doc__ or "").splitlines()[0]
            assert "bytes" in first, (
                f"{surface.__name__}.{name}'s first line does not say what ``length`` "
                f"counts: {first!r}"
            )
            assert "characters" not in first, (
                f"{surface.__name__}.{name}'s first line calls the byte window a "
                f"character count: {first!r}"
            )


def test_an_unknown_codec_is_refused_on_both_halves_before_the_socket() -> None:
    """``encoded()`` closed the write half and ``read_str`` kept raising ``LookupError``.

    Both are now :class:`~aslmp.errors.SlmpConfigurationError`, and both are raised
    before anything is sent -- the ``SlmpNotConnectedError`` on the valid codec is what
    proves the invalid one never reached the transport.
    """
    plc = a_client()
    for call in (
        lambda: plc.read_str("D110", length=4, encoding="utf-9"),
        lambda: plc.write_str("D110", "ok", length=4, encoding="utf-9"),
        lambda: plc.timed.read_str("D110", length=4, encoding="utf-9"),
        lambda: plc.timed.write_str("D110", "ok", length=4, encoding="utf-9"),
    ):
        assert isinstance(refusal(call), SlmpConfigurationError)
    assert refusal(lambda: plc.read_str("D110", length=4, encoding="shift_jis")) is None


def test_a_split_multi_byte_character_is_refused_and_not_a_bare_python_error() -> None:
    """The measured case: ``D110``/``D111`` held ``0xA082 0xA282`` on the bench and
    ``read_str(length=3, encoding='shift_jis')`` raised a bare ``UnicodeDecodeError``
    (FX5U-32MT/DS fw 1.065 over TCP 5002 from this host, 2026-09-07).

    Asserted on the shared decoder rather than over a socket, because the decoder is
    where both the client and the block path now go.
    """
    # Built rather than typed, so this file stays ASCII: two Japanese characters that
    # shift_jis encodes as two bytes each -- the pair D110/D111 held on the bench.
    text = chr(0x3042) + chr(0x3044)
    whole = text.encode("shift_jis")
    assert whole == bytes((0x82, 0xA0, 0x82, 0xA2))
    assert decoded(whole, encoding="shift_jis", what="x") == text
    with pytest.raises(SlmpPayloadShapeError) as caught:
        decoded(whole[:3], encoding="shift_jis", what="read_str(D110, length=3)")
    assert "read_str(D110, length=3)" in str(caught.value)
    assert "BYTES" in str(caught.value)


def test_the_two_codec_doors_accept_and_refuse_the_same_names() -> None:
    """One table for "that is not a codec", read from both directions."""
    for name in ("ascii", "utf-8", "shift_jis", "latin-1"):
        assert encoded("ok", encoding=name, what="w") is not None
        assert decoded(b"ok", encoding=name, what="r") == "ok"
    for bad in ("utf-9", "not-a-codec"):
        with pytest.raises(SlmpConfigurationError):
            encoded("ok", encoding=bad, what="w")
        with pytest.raises(SlmpConfigurationError):
            decoded(b"ok", encoding=bad, what="r")


# ========================================================================================
# The command line: no subcommand may guess a type, on either side
# ========================================================================================


def kind_actions() -> list[tuple[str, Any]]:
    """Every ``--as`` option any subcommand declares, found by walking the table.

    Reflection over :data:`~aslmp.tools.SUBCOMMANDS`, so a twelfth subcommand with a
    ``--as`` is checked the day it is added and without anybody remembering to add it.
    """
    found: list[tuple[str, Any]] = []
    for row in SUBCOMMANDS.values():
        if row.needs_simulator:
            continue
        module = importlib.import_module(row.module)
        builder = getattr(module, "build_parser", None)
        if builder is None:  # pragma: no cover - every subcommand has one today
            continue
        for action in builder()._actions:
            if "--as" in action.option_strings:
                found.append((row.name, action))
    return found


def test_at_least_one_subcommand_declares_as() -> None:
    """Guards the discovery itself: a walk that finds nothing asserts nothing."""
    assert {name for name, _ in kind_actions()} >= {"read", "write"}


@pytest.mark.parametrize(
    "name,action", [pytest.param(n, a, id=n) for n, a in kind_actions()]
)
def test_no_subcommand_gives_as_a_default(name: str, action: Any) -> None:
    """``aslmp read --as`` was required and ``aslmp write --as`` defaulted to ``u16``.

    A register carries no type on the wire, so a default is a type this library invented
    -- and the half that invented one was the half that changes the machine. ``aslmp
    write ... D100 60`` exited 0 having chosen ``u16``, and those registers read back as
    ``8.407790785948902e-44`` under ``--as f32`` (FX5U-32MT/DS fw 1.065 over TCP 5002
    from this host, 2026-09-07).
    """
    assert action.required is True, f"aslmp {name} --as is not declared required"
    assert action.default is None, (
        f"aslmp {name} --as defaults to {action.default!r}. There is no default that "
        f"could be right."
    )


@pytest.mark.parametrize("name", ["read", "write"])
def test_omitting_as_is_a_usage_error_that_says_why(
    name: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Argparse's "the following arguments are required" throws the reason away."""
    module = importlib.import_module(f"aslmp.tools.{name}")
    argv = [BENCH, "--profile", "melsec:iq-f/fx5u", "D100"]
    assert module.run([*argv, "60"] if name == "write" else argv) == EXIT_USAGE
    complaint = capsys.readouterr().err
    assert "no default that could be right" in complaint
    assert "FX5U-32MT/DS" in complaint


def test_read_and_write_offer_exactly_the_same_kinds() -> None:
    """``bits`` was readable and not writable, although ``write_bits`` has always existed."""
    assert read_tool.KINDS == write_tool.KINDS


@pytest.mark.parametrize("module", [read_tool, write_tool], ids=lambda m: m.__name__)
def test_every_offered_kind_reaches_a_branch(module: Any) -> None:
    """A kind in ``--as``'s choices with no branch behind it is a promise the parser makes
    and the body does not keep. AST over ``run``'s own string literals, so adding a kind
    to the shared tuple without wiring it fails here."""
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    body = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "run"
    )
    literals = {
        node.value
        for node in ast.walk(body)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    missing = [kind for kind in module.KINDS if kind not in literals]
    assert not missing, f"{module.__name__}.run has no branch for {missing}"
