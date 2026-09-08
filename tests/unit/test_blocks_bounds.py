"""Per-field plausibility bounds: the last silent-wrong-data path, closed.

**The finding this file exists for.** An outside reviewer ran the library against the
bench FX5U and declared ``scan: U32`` copying the README, while the PLC program's own ST
says ``IO_Scan := IO_Scan + 1.0``. The library returned ``1226168560`` -- a plausible
integer that is really a float's bit pattern -- with end code ``0x0000`` and no error
anywhere. That is not a protocol defect: a D register carries no type on the wire, and
sixteen bits are sixteen bits. It was nevertheless the one place this library handed back
a number it could not stand behind.

It is worse than it looks, and ``test_a_counter_declared_u32_still_counts_up`` is the
test that says why: IEEE-754 bit patterns rise monotonically for positive floats, so a
counter read at the wrong type still *increases* every cycle and the obvious sanity check
-- "is it advancing?" -- passes. Ours did. The only visible symptom was its rate, and that
symptom is itself unstable: ``+1.0`` in the REAL moves the ``U32`` reading by one ulp-step,
16 at the 613775.0 we measured and halving each time the counter crosses a power of two.

**What is asserted here, and what is deliberately not.** Bounds are a promise the caller
makes and this library keeps. Nothing here infers a register's type, nothing sniffs
whether a value "looks like" a float, and no test in this file asserts that it does --
that would be a feature the wire cannot support, and having it half-work would be worse
than not having it. What is asserted is that a declared range is enforced on the way in
and on the way out, that the refusal names the cause a person can act on, that an
undeclared field is untouched, and that the check costs no allocation in the cycle.
"""

from __future__ import annotations

import gc
import itertools
import struct
import tracemalloc
from collections.abc import Callable, Sequence
from typing import Annotated, Any

import pytest

from aslmp.blocks import fields as fields_module
from aslmp.blocks import plan as plan_module
from aslmp.blocks.fields import (
    F32,
    I16,
    U16,
    U32,
    Bit,
    Bounds,
    SlmpImplausibleValueError,
    at,
    outside,
)
from aslmp.blocks.layout import layout_of, plc_block
from aslmp.blocks.plan import BlockPlan, bind
from aslmp.client import Plc
from aslmp.errors import (
    SlmpBlockLayoutError,
    SlmpConfigurationError,
    SlmpSemanticError,
    SlmpValueRangeError,
)
from aslmp.profile import Encoding
from aslmp.timing import Chunk, Nanos, Transaction, TransactionTiming
from aslmp.transport.base import TransportKind
from aslmp.wire.frames import FrameType

BENCH = "192.168.10.250"
FX5U = "melsec:iq-f/fx5u"

# The reviewer's own number. IO_Scan on the bench is a REAL that the PLC program raises
# by 1.0 a cycle (``IO_Scan := IO_Scan + 1.0``, from the CPU's own ST source); at 613775
# cycles the library, told the field was a U32, returned 1226168560 -- which is that
# float's bit pattern and a perfectly ordinary-looking counter.
SCAN_FLOAT = 613775.0
SCAN_AS_U32 = int(struct.unpack("<I", struct.pack("<f", SCAN_FLOAT))[0])


def a_client(**kwargs: Any) -> Plc:
    """A client aimed at the bench's own settings. Constructing one opens no socket."""
    return Plc(BENCH, 5002, profile=FX5U, **kwargs)


def declare(alias: Any, **bounds: Any) -> Any:
    """``F32(minimum=..., maximum=...)`` from a file ``mypy`` checks as an expression.

    Real code writes the call in the metadata position of an ``Annotated``, which a type
    checker does not analyse; written bare in an expression it is analysed as
    ``float(minimum=...)``. Same object, same call. Every block class below uses the real
    annotation form, so the documented spelling is exercised as written.
    """
    return alias(**bounds)


BOUNDED_F32 = declare(F32, minimum=0.0, maximum=1.0)
"""A bounded field written the wrong way round, for the refusal that says so.

At module scope because ``from __future__ import annotations`` makes every annotation a
string that ``get_type_hints`` resolves against module globals -- exactly as it is in the
real code this refusal is written for.
"""


def decode(plan: BlockPlan[Any], payload: bytes) -> dict[str, Any]:
    """Run the compiled decoder over one response payload, as ``read()`` would."""
    values: dict[str, Any] = {}
    plan._reader.fill(plan._reader.unpack(payload, binary=True), values)
    return values


# ========================================================================================
# The reviewer's scenario, reproduced
# ========================================================================================


@plc_block(base="D0")
class ReviewerBlock:
    """What the reviewer wrote: the README's example with ``scan`` declared ``U32``.

    The bound is the only thing added, and it is the whole fix: a scan counter on a rig
    that has been up for weeks is not ten million, so ``maximum`` is a promise the
    process can keep and a float's bit pattern cannot.
    """

    setpoint: F32
    scan: Annotated[int, U32(minimum=0, maximum=1_000_000)]


@plc_block(base="D0")
class CorrectedBlock:
    """The same registers, declared the way GX Works3's Data Type column says."""

    setpoint: F32
    scan: Annotated[float, F32(minimum=0.0, maximum=1.0e7)]


def reviewer_payload(scan: float = SCAN_FLOAT) -> bytes:
    """The bench's own response bytes: two f32s, low word first, in wire order."""
    return struct.pack("<ff", 60.0, scan)


def test_a_float_bit_pattern_read_as_u32_is_caught_by_a_bound() -> None:
    """The finding, end to end. Same bytes, same end code, and now it refuses.

    Everything about this response is correct: the PLC answered ``0x0000``, the frame
    parsed, the length matched, and the two registers really do hold what the CPU sent.
    The declaration is what is wrong, and the bound is the only thing in the system that
    could have known.
    """
    plan = bind(a_client(), ReviewerBlock)
    with pytest.raises(SlmpImplausibleValueError) as caught:
        decode(plan, reviewer_payload())
    assert caught.value.value == SCAN_AS_U32 == 1226168560


def test_the_refusal_names_the_global_label_as_the_cause_to_check() -> None:
    """The message has to be actionable at 3 a.m., by somebody who has GX Works3 open.

    "Out of range" is not actionable. The cause is almost never the plant; it is a
    declared type that disagrees with the PLC program's own global label, and the
    message says so, says where to look, and says what the wrong reading looks like.
    """
    plan = bind(a_client(), ReviewerBlock)
    with pytest.raises(SlmpImplausibleValueError) as caught:
        decode(plan, reviewer_payload())
    message = str(caught.value)
    assert "Global Label" in message
    assert "Data Type" in message
    assert "GX Works3" in message
    assert "carries no type on the wire" in message
    assert "does not guess" in message or "nothing here guesses" in message
    assert "0x0000" in message, "the end code was success, and that is the point"


def test_the_refusal_carries_the_field_the_bounds_the_value_and_the_registers() -> None:
    """Structured, not only prose: a supervisor logs these fields rather than a string."""
    plan = bind(a_client(), ReviewerBlock)
    with pytest.raises(SlmpImplausibleValueError) as caught:
        decode(plan, reviewer_payload())
    error = caught.value
    assert error.field == "scan"
    assert error.bounds == Bounds(0, 1_000_000)
    assert (error.minimum, error.maximum) == (0, 1_000_000)
    assert error.value == SCAN_AS_U32
    assert error.address == "D2"
    low, high = struct.unpack("<HH", struct.pack("<f", SCAN_FLOAT))
    assert error.registers == (low, high), "low word first, as the wire carried them"
    assert f"0x{low:04X}" in str(error) and f"0x{high:04X}" in str(error)


def test_the_same_registers_declared_f32_read_back_as_the_float_they_are() -> None:
    """The other half of the fix: with the right declaration the bound never fires."""
    plan = bind(a_client(), CorrectedBlock)
    assert decode(plan, reviewer_payload()) == {"setpoint": 60.0, "scan": SCAN_FLOAT}


def test_a_counter_declared_u32_still_counts_up() -> None:
    """Why "is it advancing?" did not catch this, stated as an assertion.

    IEEE-754 orders positive floats the same way their bit patterns order as unsigned
    integers, so a REAL counter read as ``U32`` is still monotonically increasing. The
    naive liveness check passes forever; only a *range* can tell.
    """
    floats = [SCAN_FLOAT + step for step in range(50)]
    patterns = [struct.unpack("<I", struct.pack("<f", value))[0] for value in floats]
    assert patterns == sorted(patterns), "the wrong reading rises exactly like the right one"
    assert all(b > a for a, b in itertools.pairwise(patterns))
    assert all(value > 1_000_000 for value in patterns), "and every one breaks the bound"

    # And the rate it rises at is not a fixed multiple, which is why the docs may not
    # quote one. +1.0 in the REAL moves the U32 reading by one ulp-step: 16 at the value
    # actually measured, halving every time the counter crosses a power of two.
    def step_at(value: float) -> int:
        def pattern(single: float) -> int:
            return int(struct.unpack("<I", struct.pack("<f", single))[0])

        return pattern(value + 1.0) - pattern(value)

    assert step_at(SCAN_FLOAT) == 16, "the factor at the measured 613775.0"
    assert [step_at(float(2**exponent)) for exponent in (20, 21, 22)] == [8, 4, 2], (
        "the apparent rate error halves at every power of two, so a single number "
        "describing it is wrong everywhere except the magnitude it was taken at"
    )


# ========================================================================================
# Reading: what a bound does, and what it leaves alone
# ========================================================================================


@plc_block(base="D0")
class Mixed:
    """Two bounded fields, two unbounded ones, and a bit. One block, one 0403."""

    level: Annotated[float, F32(minimum=0.0, maximum=100.0)]
    raw: F32
    count: Annotated[int, U16(minimum=1, maximum=999)]
    plain: U16
    fault: Bit = at("M100")


def mixed_payload(*, count: int = 5, plain: int = 7, level: float, raw: float) -> bytes:
    """Word points first, then double-word ones, which is the order the wire demands."""
    return struct.pack("<HHHff", count, plain, 0b0000_0001, level, raw)


def test_a_value_inside_its_declared_range_is_returned_untouched() -> None:
    plan = bind(a_client(), Mixed)
    values = decode(plan, mixed_payload(level=42.5, raw=-1.0e9))
    assert values == {
        "count": 5,
        "plain": 7,
        "fault": True,
        "level": 42.5,
        "raw": -1.0e9,
    }


@pytest.mark.parametrize("level", [-0.001, 100.001, 1e30, float("nan")])
def test_a_value_outside_its_declared_range_raises_rather_than_returning(
    level: float,
) -> None:
    """Including NaN: a register pair that decodes to a NaN is inside no range meant."""
    plan = bind(a_client(), Mixed)
    with pytest.raises(SlmpImplausibleValueError, match="level"):
        decode(plan, mixed_payload(level=level, raw=0.0))


def test_an_unbounded_field_beside_a_bounded_one_is_never_judged() -> None:
    """``raw`` is deliberately absurd. Unbounded stays the default, and default is silent."""
    plan = bind(a_client(), Mixed)
    values = decode(plan, mixed_payload(level=0.0, raw=float("inf")))
    assert values["raw"] == float("inf")


def test_each_bounded_field_is_judged_against_its_own_range() -> None:
    plan = bind(a_client(), Mixed)
    with pytest.raises(SlmpImplausibleValueError) as caught:
        decode(plan, mixed_payload(count=1000, level=1.0, raw=0.0))
    assert caught.value.field == "count"
    assert caught.value.registers == (1000,), "one register, one word"


def test_the_refusal_is_a_semantic_error_because_the_plc_said_0x0000() -> None:
    """Section 3.1 of DESIGN: this is the family for "it succeeded and is still wrong".

    Not a ``SlmpUsageError``: the call was fine and bytes did go out. Not a
    ``SlmpProtocolError``: the frame was perfect. The transaction succeeded and the
    number is still not one you can use, which is exactly what ``SlmpSemanticError``
    means.
    """
    plan = bind(a_client(), Mixed)
    with pytest.raises(SlmpSemanticError):
        decode(plan, mixed_payload(level=-1.0, raw=0.0))
    assert issubclass(SlmpImplausibleValueError, SlmpSemanticError)


# ========================================================================================
# Writing: the same promise, enforced before anything is sent
# ========================================================================================


@plc_block(base="D100")
class Setpoints:
    """Registers only, so this block has a prebuilt write template to protect."""

    target: Annotated[float, F32(minimum=10.0, maximum=80.0)]
    trim: Annotated[int, I16(minimum=-100, maximum=100)]
    spare: U16


def refuse_all_io(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Replace the client's one I/O path with a recorder. Nothing may reach it.

    Patched on the class rather than the instance because :class:`~aslmp.client.Plc` is
    slotted and ``final``: there is no per-instance attribute to shadow a method with,
    which is the same property that stops a caller inventing one at run time.
    """
    sent: list[object] = []

    async def _run(self: object, cmd: object, *, mutates: bool) -> tuple[object, object]:
        del self, mutates
        sent.append(cmd)
        raise ReachedTheWireError("a write outside the declared bounds reached the PLC")

    monkeypatch.setattr(Plc, "_run", _run)
    return sent


class ReachedTheWireError(Exception):
    """Raised by the recorder above when a call really did get as far as the wire."""


async def test_a_write_outside_the_declared_range_never_reaches_the_plc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller bug, and therefore a ``SlmpUsageError``: nothing left this process."""
    plan = bind(a_client(), Setpoints)
    sent = refuse_all_io(monkeypatch)
    with pytest.raises(SlmpValueRangeError, match="target"):
        await plan.write(target=95.0)
    assert sent == []


async def test_the_write_refusal_says_it_did_not_clamp_and_did_not_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = bind(a_client(), Setpoints)
    refuse_all_io(monkeypatch)
    with pytest.raises(SlmpValueRangeError) as caught:
        await plan.write(trim=-500)
    message = str(caught.value)
    assert "Nothing here clamps to fit" in message
    assert "Nothing was sent" in message
    assert "D102" in message, "the register the bound is about"
    assert "[-100 .. 100]" in message


async def test_write_block_is_judged_the_same_way_as_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The typed door and the keyword door are one check, not two."""
    plan = bind(a_client(), Setpoints)
    refuse_all_io(monkeypatch)
    block = Setpoints(target=1000.0, trim=0, spare=0)
    with pytest.raises(SlmpValueRangeError, match="target"):
        await plan.write_block(block)


async def test_an_unbounded_field_may_be_written_anything_its_width_holds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``spare`` promises nothing, so nothing judges it; the width still does."""
    plan = bind(a_client(), Setpoints)
    sent = refuse_all_io(monkeypatch)
    with pytest.raises(ReachedTheWireError):
        await plan.write(spare=65535)
    assert len(sent) == 1


def test_a_bound_that_excludes_zero_does_not_cost_the_block_its_write_template() -> None:
    """The regression this file exists to prevent a second time.

    The template is built at bind with every value zero, and building it *is* the
    validation that the block is writable at all. Checking bounds inside the point
    builder would make ``minimum=10.0`` turn its own plausibility bound into "this block
    has no write template" -- a silent loss of the prebuilt path, reported as a refusal
    about something else entirely.
    """
    plan = bind(a_client(), Setpoints)
    assert plan.write_refusal == ""
    template = plan.write_template
    assert template is not None and b"\x02\x14" in template


# ========================================================================================
# Declarations that cannot mean anything, refused at class definition
# ========================================================================================


def test_the_bare_type_must_agree_with_the_alias_beside_it() -> None:
    """``Annotated[int, F32(...)]`` would hand a float back through an ``int`` annotation."""
    with pytest.raises(SlmpBlockLayoutError, match="reads back as a float"):

        @plc_block(base="D0")
        class Wrong:
            value: Annotated[int, F32(minimum=0.0)]


def test_a_call_used_as_the_whole_annotation_is_refused_with_the_right_form() -> None:
    """The mistake a reader of the README will make once, answered with the fix."""
    with pytest.raises(SlmpBlockLayoutError) as caught:

        @plc_block(base="D0")
        class Wrong:
            value: BOUNDED_F32  # type: ignore[valid-type]  # the refusal is the point

    assert "Annotated[float, F32(minimum=..., maximum=...)]" in str(caught.value)


def test_a_bound_on_a_field_is_visible_on_the_layout() -> None:
    """``__layout__`` is public, and a field's promise is part of what it declares."""
    layout = layout_of(Mixed)
    assert layout.field("level").bounds == Bounds(0.0, 100.0)
    assert layout.field("raw").bounds is None
    assert layout.field("fault").bounds is None, "a bool has no implausible value"


# ========================================================================================
# describe(): the artifact a Mitsubishi engineer reads (graft G9)
# ========================================================================================


def test_describe_prints_the_bounds_beside_every_field() -> None:
    """Because the question "does this range match the process?" is asked of this table.

    It is the same table, read at the same moment, as "does this type match the global
    label?" -- so both answers have to be on it.
    """
    report = bind(a_client(), Mixed).describe()
    assert "[0.0 .. 100.0]" in report
    assert "[1 .. 999]" in report
    lines = [line for line in report.splitlines() if line.strip().startswith("raw")]
    assert lines and lines[0].endswith("--"), "an unbounded field says so"


def test_describe_still_prints_a_block_that_declares_no_bounds_at_all() -> None:
    @plc_block(base="D0")
    class Plain:
        value: F32

    report = bind(a_client(), Plain).describe()
    assert "value" in report and "F32" in report and "--" in report


# ========================================================================================
# The hot path. Bounds checking must not allocate per read.
# ========================================================================================


def traced_delta(work: Callable[[], None], rounds: int) -> tuple[int, int]:
    """Bytes and blocks still live after ``rounds`` passes, attributed to our modules.

    The same instrument ``tests/unit/test_observability.py`` points at
    ``LatencyRecorder``: snapshot, work, snapshot, and keep only the traces whose frames
    are in the two files that do the checking.
    """
    gc.collect()
    tracemalloc.start()
    try:
        before = tracemalloc.take_snapshot()
        for _ in range(rounds):
            work()
        after = tracemalloc.take_snapshot()
    finally:
        tracemalloc.stop()
    keep = [
        tracemalloc.Filter(True, fields_module.__file__),
        tracemalloc.Filter(True, plan_module.__file__),
    ]
    diff = after.filter_traces(keep).compare_to(before.filter_traces(keep), "lineno")
    return sum(d.size_diff for d in diff), sum(d.count_diff for d in diff)


def test_the_bounds_table_is_built_once_at_bind_and_never_grows() -> None:
    """The structural half: there is nothing here that *can* allocate in the cycle.

    The table is a tuple of tuples of numbers and strings, computed at bind, holding
    both ends of every range unpacked beside the :class:`Bounds` they came from. The
    cycle reads it; nothing writes it, and there is no container in the reader that
    could grow.
    """
    reader = bind(a_client(), Mixed)._reader
    table = reader._bounds
    assert isinstance(table, tuple)
    assert [row[1] for row in table] == ["count", "level"], "wire order: words, then dwords"
    assert not hasattr(reader, "__dict__"), "__slots__, so no attribute can appear later"
    payload = mixed_payload(level=42.5, raw=1.0)
    values: dict[str, Any] = {}
    for _ in range(100):
        reader.fill(reader.unpack(payload, binary=True), values)
    assert reader._bounds is table

    plain = bind(a_client(), Setpoints)._reader
    assert [row[1] for row in plain._bounds] == ["trim", "target"]


def test_a_bounded_block_retains_no_more_than_the_same_block_unbounded() -> None:
    """The invariance, which is the assertion that actually catches a regression."""

    @plc_block(base="D0")
    class Unbounded:
        level: F32
        raw: F32
        count: U16
        plain: U16
        fault: Bit = at("M100")

    payload = mixed_payload(level=42.5, raw=1.0)
    pairs = []
    for block in (Unbounded, Mixed):
        plan = bind(a_client(), block)
        reader = plan._reader
        values: dict[str, Any] = {}

        def cycle(reader: Any = reader, values: dict[str, Any] = values) -> None:
            reader.fill(reader.unpack(payload, binary=True), values)

        for _ in range(50):
            cycle()
        pairs.append(traced_delta(cycle, rounds=2000))
    assert pairs[1][0] <= pairs[0][0] + 0, pairs


def test_outside_allocates_nothing_for_either_answer() -> None:
    """The primitive itself, measured directly, in and out of range."""

    def call() -> None:
        outside(42.5, 0.0, 100.0)
        outside(-1.0, 0.0, 100.0)
        outside(1.0, None, None)

    for _ in range(50):
        call()
    size, blocks = traced_delta(call, rounds=5000)
    assert (size, blocks) <= (0, 0)


# ========================================================================================
# The per-call form: plc.read_f32("D0", minimum=..., maximum=...)
# ========================================================================================


def a_transaction() -> Transaction:
    """A record shaped like the one a real read would carry. No socket, no clock."""
    timing = TransactionTiming(
        submitted_at=Nanos(1),
        gate_acquired_at=Nanos(2),
        encoded_at=Nanos(3),
        sent_at=Nanos(4),
        first_byte_at=Nanos(5),
        received_at=Nanos(6),
        decoded_at=Nanos(7),
        chunks=(Chunk(nbytes=15, at=Nanos(5)), Chunk(nbytes=4, at=Nanos(6))),
    )
    return Transaction(
        timing=timing,
        sequence=1,
        connection_id="conn-1",
        generation=0,
        after_reconnect=False,
        command=0x0401,
        subcommand=0x0000,
        frame=FrameType.THREE_E,
        encoding=Encoding.BINARY,
        transport=TransportKind.TCP,
        serial=None,
        request_bytes=21,
        response_bytes=15,
        end_code=0,
        prebuilt=False,
    )


def answering(monkeypatch: pytest.MonkeyPatch, words: Sequence[int]) -> Plc:
    """A client whose one I/O path returns these registers and touches no socket."""

    async def _run(
        self: object, cmd: object, *, mutates: bool
    ) -> tuple[tuple[int, ...], Transaction]:
        del self, cmd, mutates
        return tuple(words), a_transaction()

    monkeypatch.setattr(Plc, "_run", _run)
    return a_client()


def as_words(value: float, code: str) -> tuple[int, ...]:
    raw = struct.pack(f"<{code}", value)
    return struct.unpack(f"<{len(raw) // 2}H", raw)


async def test_a_scalar_read_with_no_bounds_behaves_exactly_as_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole feature is opt-in; the default path is untouched."""
    plc = answering(monkeypatch, as_words(SCAN_FLOAT, "f"))
    assert await plc.read_f32("D8") == SCAN_FLOAT
    assert await plc.read_u32("D8") == SCAN_AS_U32, "still wrong, still returned"


async def test_a_scalar_read_holds_its_value_to_the_bounds_it_was_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reviewer's scenario again, without a block: one call, one promise."""
    plc = answering(monkeypatch, as_words(SCAN_FLOAT, "f"))
    with pytest.raises(SlmpImplausibleValueError) as caught:
        await plc.read_u32("D8", minimum=0, maximum=1_000_000)
    assert caught.value.field == "read_u32"
    assert caught.value.address == "D8"
    assert caught.value.registers == as_words(SCAN_FLOAT, "f")
    assert "Global Label" in str(caught.value)


async def test_a_scalar_read_inside_its_bounds_returns_the_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plc = answering(monkeypatch, as_words(42.5, "f"))
    assert await plc.read_f32("D0", minimum=0.0, maximum=100.0) == 42.5


@pytest.mark.parametrize(
    ("method", "code", "value", "bounds"),
    [
        ("read_i16", "h", -300, {"minimum": -100}),
        ("read_u16", "H", 4000, {"maximum": 1000}),
        ("read_i32", "i", -80000, {"minimum": -1000}),
        ("read_u32", "I", 80000, {"maximum": 1000}),
        ("read_f32", "f", 5.5, {"maximum": 1.0}),
        ("read_f64", "d", 5.5, {"maximum": 1.0}),
    ],
)
async def test_every_typed_scalar_read_takes_the_same_two_keywords(
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    code: str,
    value: float,
    bounds: dict[str, float],
) -> None:
    """One width having bounds and another not would be the worst of both."""
    plc = answering(monkeypatch, as_words(value, code))
    with pytest.raises(SlmpImplausibleValueError):
        await getattr(plc, method)("D0", **bounds)
    with pytest.raises(SlmpImplausibleValueError):
        await getattr(plc.timed, method)("D0", **bounds)


async def test_the_timed_surface_carries_the_bounds_through_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``plc.timed`` is generated from the client, so this is a seam, not a copy."""
    plc = answering(monkeypatch, as_words(42.5, "f"))
    reading = await plc.timed.read_f32("D0", minimum=0.0, maximum=100.0)
    assert reading.value == 42.5
    assert reading.tx.end_code == 0


@pytest.mark.parametrize(
    "bounds",
    [
        {"minimum": 10.0, "maximum": 1.0},
        {"minimum": float("nan")},
        {"maximum": float("nan")},
    ],
)
async def test_a_nonsensical_per_call_bound_is_refused_as_a_configuration_error(
    monkeypatch: pytest.MonkeyPatch, bounds: dict[str, float]
) -> None:
    """A range nothing can be inside, and a NaN end that would never fire.

    A ``SlmpConfigurationError`` rather than the implausible-value error: the arguments
    are incoherent, which is a different thing from a value that disagrees with them.
    """
    plc = answering(monkeypatch, as_words(42.5, "f"))
    read: Any = plc.read_f32
    with pytest.raises(SlmpConfigurationError):
        await read("D0", **bounds)


async def test_the_sync_facade_takes_the_bounds_too() -> None:
    """``tests/unit/test_sync.py`` proves the signatures match; this proves they carry."""
    import inspect

    from aslmp.sync import Plc as SyncPlc

    for name in ("read_i16", "read_u16", "read_i32", "read_u32", "read_f32", "read_f64"):
        parameters = inspect.signature(getattr(SyncPlc, name)).parameters
        assert "minimum" in parameters and "maximum" in parameters, name
