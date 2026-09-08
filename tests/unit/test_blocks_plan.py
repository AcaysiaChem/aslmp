"""``bind()`` and the plans it returns, with no socket anywhere in this file.

Constructing a :class:`~aslmp.client.Plc` opens nothing, and ``bind()`` is synchronous
and does no I/O, so every validation, the prebuilt frame, the compiled decoder and the
whole ``describe()`` report can be -- and here are -- asserted in an ordinary unit test.
That is the property DESIGN.md section 4.9 is buying by splitting layout from bind: a
block mistake is a start-up failure, and a start-up failure is a test you can write.

The two tests worth reading first:

* ``test_the_prebuilt_frame_is_this_exact_vector`` pins the bytes. Everything else in the
  file could pass while the library talked to the wrong registers; this cannot.
* ``test_decoding_allocates_the_same_whatever_the_block_size`` is the allocation budget
  of DESIGN.md section 5.9, written as the assertion that actually catches regressions:
  not an absolute number, but the *invariance* -- a hundred-field block must retain no
  more per cycle than a four-field one.
"""

from __future__ import annotations

import gc
import struct
import sys
from typing import Any

import pytest

from aslmp.blocks.fields import (
    F32,
    F64,
    I16,
    U16,
    U32,
    Bit,
    BlockTransaction,
    Bounds,
    SlmpImplausibleValueError,
    Str,
    Word,
    at,
)
from aslmp.blocks.layout import plc_block
from aslmp.blocks.plan import BlockPlan, Split, SplitBlockPlan, bind
from aslmp.client import Handshake, Plc, PlcClockSource
from aslmp.commands.base import WordOrder
from aslmp.errors import (
    SlmpAddressRangeError,
    SlmpBlockLayoutError,
    SlmpDeviceNotOnCpuError,
    SlmpNotConnectedError,
    SlmpPayloadShapeError,
    SlmpPointLimitError,
    SlmpTargetChangedError,
)
from aslmp.identity import CpuIdentity
from aslmp.profile import Encoding, Family
from aslmp.timing import Chunk, Nanos, Transaction, TransactionTiming
from aslmp.transport.base import TransportKind
from aslmp.wire.frames import FrameType

BENCH = "192.168.10.250"
FX5U = "melsec:iq-f/fx5u"


def a_client(**kwargs: Any) -> Plc:
    """A client aimed at the bench's own settings. Constructing one opens no socket."""
    return Plc(BENCH, 5002, profile=FX5U, **kwargs)


def identified(model_code: int = 0x4A49, **kwargs: Any) -> Plc:
    """A client that believes it has completed the ``0101`` half of the handshake.

    ``_identity`` is what ``connect()`` sets, and a plan's target guard is written
    against it; setting it here is how a unit test reaches the guard without a CPU.
    """
    plc = a_client(**kwargs)
    plc._identity = CpuIdentity(
        model="FX5U-32MT/DS",
        model_code=model_code,
        family=Family.IQ_F,
        raw=b"FX5U-32MT/DS    ",
    )
    return plc


@plc_block(base="D0")
class Bench:
    """The bench's own register map, plus one folded bit and one scattered register."""

    setpoint: F32
    scan: U32
    mode: U16 = at("D400")
    fault: Bit = at("M100")


@plc_block(base="D100")
class Registers:
    """Registers only: the shape that gets a prebuilt write template."""

    value: F32
    count: U32
    small: I16
    plain: Word


def numeric_block(count: int, *, name: str = "Big", base: str = "D0") -> type[Any]:
    """A block of ``count`` single-register fields, built at run time.

    A hundred-field block is not something to write out by hand, and the point-ceiling
    and allocation tests need one of an arbitrary size.
    """
    made: type[Any] = type(
        name, (), {"__annotations__": {f"f{i}": U16 for i in range(count)}}
    )
    return plc_block(base=base)(made)


# ========================================================================================
# The bytes
# ========================================================================================


def test_the_prebuilt_frame_is_this_exact_vector() -> None:
    """3E, binary, own station, monitoring timer 0x0000, ``0403`` subcommand ``0000``.

    Two word access points (``D400``, the folded ``M100`` window) then two double-word
    ones (``D0``, ``D2``), because the wire demands every word specification before every
    double-word one and carries no framing between the groups
    (SH(NA)-080956ENG-M section 6.4 pp.53-56). ``L`` is 0x0018: the two-unit timer,
    command and subcommand plus the 18-unit payload.
    """
    plan = bind(a_client(), Bench)
    assert plan.request_frame.hex(" ") == (
        "50 00 00 ff ff 03 00 18 00 00 00 03 04 00 00 "
        "02 02 90 01 00 a8 64 00 00 90 00 00 00 a8 02 00 00 a8"
    )


def test_the_points_are_partitioned_word_first_in_declaration_order() -> None:
    plan = bind(a_client(), Bench)
    assert [str(a) for a in plan.word_points] == ["D400", "M100"]
    assert [str(a) for a in plan.dword_points] == ["D0", "D2"]
    assert plan.points == 4
    assert plan.subcommand == 0x0000


def test_the_frame_is_built_once_and_never_rebuilt() -> None:
    """The hot path sends the same object every cycle; that is what ``prebuilt`` means."""
    plan = bind(a_client(), Bench)
    reader = plan._reader
    assert reader.request(None) is reader.request(None)
    assert reader.request(None) is plan.request_frame


def test_a_4e_plan_patches_the_serial_in_place_and_rebuilds_nothing() -> None:
    """Two units change; the buffer is the same object and the rest is identical."""
    plan = bind(a_client(frame=FrameType.FOUR_E), Bench)
    reader = plan._reader
    first = bytes(reader.request(0x1234))
    second_view = reader.request(0x5678)
    second = bytes(second_view)
    assert reader.request(0x5678) is second_view  # the same buffer, patched
    assert first[2:4] == b"\x34\x12"
    assert second[2:4] == b"\x78\x56"
    assert first[4:] == second[4:]
    assert first[:2] == second[:2] == b"\x54\x00"


# ========================================================================================
# Folding
# ========================================================================================


def test_sixteen_declared_bits_cost_one_access_point() -> None:
    """The whole reason folding exists, against the measured 192-point ceiling."""
    annotations = {f"b{i}": Bit for i in range(16)}
    made: type[Any] = type("Bits", (), {"__annotations__": annotations})
    for i in range(16):
        setattr(made, f"b{i}", at(f"M{100 + i}"))
    block = plc_block()(made)
    plan = bind(a_client(), block)
    assert plan.points == 1
    assert [str(a) for a in plan.word_points] == ["M100"]
    assert plan.folds[0].bits == tuple((f"b{i}", i) for i in range(16))


def test_the_fold_is_named_in_the_report_and_carries_its_anchor() -> None:
    plan = bind(a_client(), Bench)
    (fold,) = plan.folds
    assert (fold.device, fold.anchor_index, fold.anchor_text) == ("M", 100, "M100")
    assert fold.bits == (("fault", 0),)
    assert "M100 +16 points: fault=bit 0" in plan.describe()


def test_a_window_at_the_end_of_the_family_is_lowered_and_the_report_says_so() -> None:
    """M ends at M32767 on an FX5U, and a word access point reads all 16 points."""

    @plc_block()
    class Edge:
        last: Bit = at("M32767")

    plan = bind(a_client(), Edge)
    (fold,) = plan.folds
    assert (fold.anchor_index, fold.lowered) == (32752, True)
    assert fold.bits == (("last", 15),)
    assert "anchor lowered" in plan.describe()


def test_a_bit_declared_past_the_end_of_the_family_is_refused_by_the_fold() -> None:
    @plc_block()
    class Past:
        gone: Bit = at("M32768")

    with pytest.raises(SlmpBlockLayoutError, match="16-point window"):
        bind(a_client(), Past)


def test_a_bit_field_on_a_word_device_is_refused_with_the_alternative() -> None:
    @plc_block()
    class Wrong:
        flag: Bit = at("D100")

    with pytest.raises(SlmpBlockLayoutError, match="word device"):
        bind(a_client(), Wrong)


# ========================================================================================
# The validations bind exists for
# ========================================================================================


def test_two_fields_over_one_register_are_refused() -> None:
    @plc_block(base="D0")
    class Overlap:
        first: F32
        second: U16 = at("D1")

    with pytest.raises(SlmpBlockLayoutError, match="both cover D1"):
        bind(a_client(), Overlap)


def test_a_span_that_leaves_the_device_is_refused_at_bind() -> None:
    """``D7999`` alone is legal and ``D7999`` read as two registers is not: measured."""

    @plc_block(base="D7999")
    class OverTheEdge:
        value: F32

    with pytest.raises(SlmpAddressRangeError):
        bind(a_client(), OverTheEdge)


def test_a_device_this_cpu_does_not_have_is_refused_at_bind() -> None:
    @plc_block(base="ZR0")
    class NotHere:
        value: U16

    with pytest.raises(SlmpDeviceNotOnCpuError, match="ZR"):
        bind(a_client(), NotHere)


def test_a_base_on_a_bit_device_is_refused_for_auto_addressed_fields() -> None:
    @plc_block(base="M0")
    class FromBits:
        value: U16

    with pytest.raises(SlmpBlockLayoutError, match="bit device"):
        bind(a_client(), FromBits)


def test_bind_refuses_a_class_that_was_never_decorated() -> None:
    class Plain:
        value: U16

    with pytest.raises(SlmpBlockLayoutError, match="not a @plc_block class"):
        bind(a_client(), Plain)


def test_high_first_word_order_is_refused_rather_than_quietly_ignored() -> None:
    """A double-word access point is one native low-word-first value: measured."""

    @plc_block(base="D0", word_order=WordOrder.HIGH_FIRST)
    class Reversed:
        value: F32

    with pytest.raises(SlmpBlockLayoutError, match="HIGH_FIRST"):
        bind(a_client(), Reversed)


def test_high_first_is_accepted_where_there_is_nothing_for_it_to_reverse() -> None:
    @plc_block(base="D0", word_order=WordOrder.HIGH_FIRST)
    class Narrow:
        value: U16

    plan = bind(a_client(), Narrow)
    assert plan.word_order is WordOrder.HIGH_FIRST


def test_the_client_word_order_is_the_default_and_is_also_refused() -> None:
    with pytest.raises(SlmpBlockLayoutError, match="HIGH_FIRST"):
        bind(a_client(word_order=WordOrder.HIGH_FIRST), Bench)


# ========================================================================================
# The point ceiling
# ========================================================================================


def test_over_the_ceiling_refuses_and_names_the_limit_the_evidence_and_the_fields() -> None:
    """193 random points returned ``0xC054`` on FX5U-32MT/DS fw 1.065."""
    block = numeric_block(200, name="TooBig")
    with pytest.raises(SlmpPointLimitError) as caught:
        bind(a_client(), block)
    message = str(caught.value)
    assert "200 access point(s)" in message
    assert "192" in message
    assert "0xC054" in message
    assert "FX5U-32MT/DS fw 1.065" in message
    assert "f192" in message  # the first field past the ceiling
    assert "allow_split=True" in message


def test_allow_split_returns_a_different_type_and_several_prebuilt_frames() -> None:
    """Several requests are several snapshots, and the type says so."""
    block = numeric_block(200, name="Split200")
    plan = bind(a_client(), block, allow_split=True)
    assert isinstance(plan, SplitBlockPlan)
    assert plan.transactions == 2
    assert plan.points == 200
    assert len(plan.request_frames) == 2
    assert "NOT one snapshot" in plan.describe()


def test_a_block_that_fits_is_never_split_even_when_splitting_is_allowed() -> None:
    plan = bind(a_client(), Bench, allow_split=True)
    assert isinstance(plan, BlockPlan)


# ========================================================================================
# Decoding, which is pure and needs no socket at all
# ========================================================================================


def decode(plan: BlockPlan[Any], payload: bytes) -> dict[str, Any]:
    """Run the compiled decoder over one response payload, as ``read()`` would."""
    values: dict[str, Any] = {}
    plan._reader.fill(plan._reader.unpack(payload, binary=True), values)
    return values


def cycle(plan: BlockPlan[Any], payload: bytes) -> object:
    """Everything ``read()`` does with a response once the bytes are in hand."""
    values: dict[str, Any] = {}
    plan._reader.fill(plan._reader.unpack(payload, binary=True), values)
    return plan._instance(values, None)  # type: ignore[arg-type]  # tx is optional


def test_the_compiled_decoder_turns_a_payload_into_the_declared_fields() -> None:
    """Word section then double-word section, in the order the frame asked for them."""
    plan = bind(a_client(), Bench)
    payload = struct.pack("<HHfI", 7, 0b1000_0001, 12.5, 999)
    assert decode(plan, payload) == {
        "mode": 7,
        "fault": True,
        "setpoint": 12.5,
        "scan": 999,
    }


def test_a_bit_that_is_clear_decodes_to_false_rather_than_being_absent() -> None:
    plan = bind(a_client(), Bench)
    payload = struct.pack("<HHfI", 0, 0b1111_1110, 0.0, 0)
    assert decode(plan, payload)["fault"] is False


def test_a_response_of_the_wrong_length_is_refused_rather_than_zero_filled() -> None:
    plan = bind(a_client(), Bench)
    with pytest.raises(SlmpPayloadShapeError):
        decode(plan, struct.pack("<HHfI", 7, 0, 12.5, 999)[:-2])


def test_a_string_field_stops_at_the_nul_word() -> None:
    @plc_block(base="D500")
    class Recipe:
        name: str = Str(length=8)

    plan = bind(a_client(), Recipe)
    assert plan.points == 5
    assert decode(plan, b"ABCD\x00\x00\x00\x00\x00\x00") == {"name": "ABCD"}
    assert decode(plan, b"ABCDEFGH\x00\x00") == {"name": "ABCDEFGH"}


def test_string_bytes_that_are_not_the_declared_encoding_are_refused() -> None:
    """Never ``errors='replace'``: question marks in a recipe name reported as success."""

    @plc_block(base="D500")
    class Recipe:
        name: str = Str(length=2)

    plan = bind(a_client(), Recipe)
    with pytest.raises(SlmpPayloadShapeError, match="not ascii text"):
        decode(plan, b"\xff\xfe\x00\x00")


def test_an_f64_spans_two_double_word_points_low_word_first() -> None:
    @plc_block(base="D0")
    class Wide:
        big: F64

    plan = bind(a_client(), Wide)
    assert plan.points == 2
    assert [str(a) for a in plan.dword_points] == ["D0", "D2"]
    assert decode(plan, struct.pack("<d", -1234.5)) == {"big": -1234.5}


def test_signed_and_unsigned_registers_decode_as_declared() -> None:
    @plc_block(base="D0")
    class Signs:
        small: I16
        plain: Word
        wide: U32

    plan = bind(a_client(), Signs)
    assert decode(plan, struct.pack("<HHI", 0xFFFF, 0xFFFF, 0xFFFFFFFF)) == {
        "small": -1,
        "plain": 0xFFFF,
        "wide": 0xFFFFFFFF,
    }


# ========================================================================================
# The allocation budget (DESIGN section 5.9)
# ========================================================================================


def retained(work: Any, rounds: int) -> int:
    """Blocks still allocated after ``rounds`` cycles whose results were dropped.

    The published claim is that the hot path *retains* nothing: each cycle's values are
    freed as the next one replaces them, whatever the block size. Counting live blocks
    rather than total allocations is the honest form of it -- decoding a hundred floats
    necessarily allocates a hundred floats, and the question is whether any of them are
    still there next cycle.
    """
    for _ in range(5):
        work()
    gc.collect()
    before = sys.getallocatedblocks()
    for _ in range(rounds):
        work()
    gc.collect()
    return sys.getallocatedblocks() - before


@pytest.mark.slow
def test_decoding_allocates_the_same_whatever_the_block_size() -> None:
    """Invariant to point count, which is the assertion that catches a regression."""
    small = bind(a_client(), numeric_block(4, name="Small4"))
    large = bind(a_client(), numeric_block(100, name="Large100"))
    small_payload = struct.pack("<4H", *range(4))
    large_payload = struct.pack("<100H", *range(100))

    def read_small() -> None:
        cycle(small, small_payload)

    def read_large() -> None:
        cycle(large, large_payload)

    # The published constant: one block still live after 200 cycles, whatever the size.
    # It is not zero because the interpreter keeps a little of its own between the two
    # sampling points, and it is deliberately an equality rather than a bound -- a
    # decoder that grew with the block would move the second number and not the first.
    # Re-baseline per Python version; this is a CPython implementation detail.
    small_delta = retained(read_small, 200)
    large_delta = retained(read_large, 200)
    assert small_delta == large_delta
    assert large_delta <= 2


def test_a_bound_plan_holds_no_growing_container() -> None:
    """The structural half: there is nothing here that *can* grow between cycles."""
    plan = bind(a_client(), Bench)
    reader = plan._reader
    assert not hasattr(reader, "__dict__")
    assert not hasattr(plan, "__dict__")
    payload = struct.pack("<HHfI", 7, 1, 12.5, 999)
    sizes = (len(reader.groups), len(reader.word_points), len(reader.dword_points))
    for _ in range(100):
        decode(plan, payload)
    assert (
        len(reader.groups),
        len(reader.word_points),
        len(reader.dword_points),
    ) == sizes


# ========================================================================================
# The target guard (graft G6)
# ========================================================================================


async def test_read_refuses_a_changed_model_code_before_touching_the_socket() -> None:
    """A prebuilt 0403 into different silicon returns plausible floats and 0x0000.

    The client here was never connected, so a plan that failed to check would raise
    ``SlmpNotConnectedError`` instead -- which is what makes this an ordering test as
    well as a refusal test.
    """
    plc = identified(0x4A49)
    plan = bind(plc, Bench)
    assert plan.bound_model_code == 0x4A49
    plc._identity = CpuIdentity(
        model="R04CPU", model_code=0x4806, family=Family.IQ_R, raw=b"R04CPU          "
    )
    with pytest.raises(SlmpTargetChangedError) as caught:
        await plan.read()
    assert caught.value.expected_model_code == 0x4A49
    assert caught.value.actual_model_code == 0x4806
    assert caught.value.bound_generation == plan.bound_generation


async def test_an_identity_lost_across_a_reconnect_is_also_a_changed_target() -> None:
    plc = identified(0x4A49)
    plan = bind(plc, Bench)
    plc._identity = None
    with pytest.raises(SlmpTargetChangedError, match="no identified CPU"):
        await plan.read()


async def test_a_plan_bound_without_the_identify_handshake_carries_no_guard() -> None:
    """``Handshake.NONE`` costs you the guard, and that is said rather than pretended."""
    plc = a_client(handshake=Handshake.NONE)
    plan = bind(plc, Bench)
    assert plan.bound_model_code is None
    with pytest.raises(SlmpNotConnectedError):
        await plan.read()


async def test_a_split_plan_checks_the_target_too() -> None:
    plc = identified(0x4A49)
    plan = bind(plc, numeric_block(200, name="SplitGuard"), allow_split=True)
    plc._identity = None
    with pytest.raises(SlmpTargetChangedError):
        await plan.read()


# ========================================================================================
# describe() -- the artifact (graft G9)
# ========================================================================================


def test_describe_prints_the_table_the_folds_the_budget_and_the_frame() -> None:
    report = bind(a_client(), Bench).describe()
    assert "block Bench bound to 192.168.10.250:5002" in report
    assert "melsec:iq-f/fx5u" in report
    assert "setpoint" in report and "F32" in report and "D0+0" in report
    assert "M100 +16 points" in report
    assert "points: 2 word + 2 double-word = 4" in report
    assert "0xC054" in report
    assert "subcommand: 0x0000" in report
    assert "50 00 00 ff ff 03 00 18 00" in report
    assert "SH(NA)-080956ENG-M section 6.4 p.54" in report
    assert "FX5U-32MT/DS fw 1.065" in report


def test_describe_prints_the_write_template_when_there_is_one() -> None:
    report = bind(a_client(), Registers).describe()
    assert "write template" in report
    assert "02 14" in report  # the 1402 command code, low byte first


def test_describe_says_why_a_block_has_no_single_write() -> None:
    report = bind(a_client(), Bench).describe()
    assert "no single template" in report
    assert "bit units" in report


def test_repr_names_the_block_the_points_and_the_client() -> None:
    assert repr(bind(a_client(), Bench)) == (
        "<BlockPlan Bench 4 point(s) on 192.168.10.250:5002>"
    )


# ========================================================================================
# Writing
# ========================================================================================


def test_a_register_only_block_gets_a_prebuilt_write_template() -> None:
    """Building it at bind is what proves the block is writable at all."""
    plan = bind(a_client(), Registers)
    template = plan.write_template
    assert template is not None
    assert template[11:13] == b"\x02\x14"  # 1402, low byte first
    assert plan.write_refusal == ""


def test_a_block_of_bits_and_registers_has_no_single_write() -> None:
    plan = bind(a_client(), Bench)
    assert plan.write_template is None
    assert "two transactions" in plan.write_refusal


def test_a_block_that_reads_but_cannot_be_written_binds_and_says_why() -> None:
    """The 1402 budget is weighted and the 0403 one is flat, so they disagree.

    160 double-word points fit the flat ``0403`` ceiling of 192 and do not fit
    ``word x 12 + dword x 14 <= 1920``: 2240. A block like that is perfectly readable, so
    bind must not fail -- it records the refusal and produces it when a write is tried.
    """
    annotations = {f"f{index}": F32 for index in range(160)}
    made: type[Any] = type("Wide160", (), {"__annotations__": annotations})
    block = plc_block(base="D0")(made)
    plan = bind(a_client(), block)
    assert plan.points == 160
    assert plan.write_template is None
    assert "1920" in plan.write_refusal


async def test_writing_bits_and_registers_together_is_refused() -> None:
    plan = bind(a_client(), Bench)
    with pytest.raises(SlmpBlockLayoutError, match="two subcommands"):
        await plan.write(mode=1, fault=True)


async def test_writing_a_field_the_block_does_not_have_is_refused() -> None:
    plan = bind(a_client(), Bench)
    with pytest.raises(SlmpBlockLayoutError, match="setpiont"):
        await plan.write(setpiont=1.0)


async def test_writing_nothing_is_refused() -> None:
    """A 1402 with no access points is answered 0xC052, measured."""
    plan = bind(a_client(), Bench)
    with pytest.raises(SlmpBlockLayoutError, match="no fields"):
        await plan.write()


async def test_a_value_of_the_wrong_python_type_is_refused_before_the_wire() -> None:
    plan = bind(a_client(), Registers)
    with pytest.raises(SlmpBlockLayoutError, match="takes an int"):
        await plan.write(count="12")


async def test_a_string_too_long_for_its_field_is_refused_rather_than_truncated() -> None:
    @plc_block(base="D500")
    class Recipe:
        name: str = Str(length=4)

    plan = bind(a_client(), Recipe)
    with pytest.raises(SlmpBlockLayoutError, match="truncates"):
        await plan.write(name="far too long")


async def test_write_block_refuses_a_mixed_block_by_the_same_sentence() -> None:
    plan = bind(a_client(), Bench)
    value = Bench(setpoint=1.0, scan=2, mode=3, fault=True)
    with pytest.raises(SlmpBlockLayoutError, match="two subcommands"):
        await plan.write_block(value)


# ========================================================================================
# The PLC's own clock
# ========================================================================================


def test_a_configured_plc_clock_adds_one_double_word_point() -> None:
    """So a cycle carries the CPU's own notion of time inside the same snapshot."""
    plc = a_client(plc_clock=PlcClockSource("D8"))
    plan = bind(plc, Bench)
    assert plan.points == 5
    assert [str(a) for a in plan.dword_points] == ["D0", "D2", "D8"]
    assert plan._reader.clock_index is not None
    assert "plc_clock" not in {f.name for f in plan.layout.fields}


def test_the_clock_point_does_not_become_a_block_field() -> None:
    plc = a_client(plc_clock=PlcClockSource("D8"))
    plan = bind(plc, Bench)
    payload = struct.pack("<HHfII", 7, 1, 12.5, 999, 4242)
    assert set(decode(plan, payload)) == {"mode", "fault", "setpoint", "scan"}


def test_a_real_clock_is_decoded_as_a_real_and_not_as_its_bit_pattern() -> None:
    """The regression for the U32-against-a-REAL misread, in the timing feature itself.

    ``D8``/``D9`` on the bench hold ``0xFEA0 0x4970``. Read as the f32 the CPU's own ST
    writes (``IO_Scan := IO_Scan + 1.0``) that is 987114; read as the unsigned double word
    this library used to hard-code it is 1232141984 -- the float's bit pattern, monotonic,
    plausible, and wrong by a factor that halves at every power of two. Measured on
    FX5U-32MT/DS fw 1.065 from this host over TCP 5002, 2026-09-07.
    """
    registers = struct.pack("<HH", 0xFEA0, 0x4970)
    misread, = struct.unpack("<I", registers)
    assert misread == 1232141984

    plc = a_client(plc_clock=PlcClockSource("D8", kind="f32"))
    plan = bind(plc, Bench)
    point = plan._reader.dword_points[-1]
    assert str(point) == "D8:f32"
    assert point.kind == "f32"
    assert point.width.bits == 32

    payload = struct.pack("<HHf", 7, 1, 12.5) + struct.pack("<I", 999) + registers
    values = plan._reader.unpack(payload, binary=True)
    at = plan._reader.clock_index
    assert at is not None
    assert plan._reader.clock_count(values[at]) == 987114


def test_a_clock_declared_u32_reads_the_bit_pattern_because_that_is_the_declaration() -> None:
    """Nothing here sniffs the bytes. The default is a default, not a guess."""
    plc = a_client(plc_clock=PlcClockSource("D8"))
    plan = bind(plc, Bench)
    payload = struct.pack("<HHfI", 7, 1, 12.5, 999) + struct.pack("<HH", 0xFEA0, 0x4970)
    values = plan._reader.unpack(payload, binary=True)
    at = plan._reader.clock_index
    assert at is not None
    assert plan._reader.clock_count(values[at]) == 1232141984


def test_a_word_wide_clock_costs_a_word_point_and_not_a_double_word_one() -> None:
    """The access width follows the declared type, which is the whole of the fix."""
    plc = a_client(plc_clock=PlcClockSource("D8", kind="u16"))
    plan = bind(plc, Bench)
    assert [str(a) for a in plan.word_points] == ["D400", "M100", "D8"]
    assert [str(a) for a in plan.dword_points] == ["D0", "D2"]
    payload = struct.pack("<HHHfI", 7, 1, 4242, 12.5, 999)
    values = plan._reader.unpack(payload, binary=True)
    at = plan._reader.clock_index
    assert at is not None
    assert plan._reader.clock_count(values[at]) == 4242


def test_a_clock_outside_its_declared_bounds_is_refused_rather_than_published() -> None:
    """The same promise a bounded block field makes, for the one point that is not one."""
    plc = a_client(plc_clock=PlcClockSource("D8", kind="f32", bounds=Bounds(0.0, 1.0e7)))
    plan = bind(plc, Bench)
    ok = struct.pack("<HHfI", 7, 1, 12.5, 999) + struct.pack("<f", 987114.0)
    assert decode(plan, ok)["setpoint"] == 12.5
    bad = struct.pack("<HHfI", 7, 1, 12.5, 999) + struct.pack("<f", 2.0e7)
    with pytest.raises(SlmpImplausibleValueError) as caught:
        decode(plan, bad)
    assert caught.value.field == "plc_clock"
    assert caught.value.address == "D8"
    assert caught.value.registers == (0x9680, 0x4B98)


def test_a_clock_label_names_the_point_in_the_report_and_in_a_refusal() -> None:
    """``label`` was declared and never read. It is read now."""
    source = PlcClockSource("D8", kind="f32", bounds=Bounds(maximum=1.0), label="scan")
    plan = bind(a_client(plc_clock=source), Bench)
    payload = struct.pack("<HHfI", 7, 1, 12.5, 999) + struct.pack("<f", 5.0)
    with pytest.raises(SlmpImplausibleValueError) as caught:
        decode(plan, payload)
    assert caught.value.field == "scan"


def test_a_non_finite_clock_is_refused_rather_than_turned_into_a_count() -> None:
    """``int(nan)`` is a ValueError, which is not in the DESIGN section 3.1 tree."""
    plan = bind(a_client(plc_clock=PlcClockSource("D8", kind="f32")), Bench)
    payload = struct.pack("<HHfI", 7, 1, 12.5, 999) + struct.pack("<f", float("nan"))
    values = plan._reader.unpack(payload, binary=True)
    at = plan._reader.clock_index
    assert at is not None
    with pytest.raises(SlmpPayloadShapeError, match="not a count"):
        plan._reader.clock_count(values[at])


# ========================================================================================
# The structural stand-in for a transaction
# ========================================================================================


def test_a_real_transaction_satisfies_the_block_transaction_protocol() -> None:
    """Checked by ``mypy`` through the annotation, not by the assertion below it.

    ``aslmp.blocks.fields`` is Layer 1 and ``aslmp.timing`` is Layer 2.5, so a block
    field's ``tx`` is declared structurally. Binding a genuine record to that type is
    what makes the compatibility a build failure instead of a hope.
    """
    timing = TransactionTiming(
        submitted_at=Nanos(0),
        gate_acquired_at=Nanos(1),
        encoded_at=Nanos(2),
        sent_at=Nanos(3),
        first_byte_at=Nanos(4),
        received_at=Nanos(5),
        decoded_at=Nanos(6),
        chunks=(Chunk(nbytes=11, at=Nanos(4)), Chunk(nbytes=4, at=Nanos(5))),
    )
    record: BlockTransaction = Transaction(
        timing=timing,
        sequence=1,
        connection_id="conn-1",
        generation=0,
        after_reconnect=False,
        command=0x0403,
        subcommand=0x0000,
        frame=FrameType.THREE_E,
        encoding=Encoding.BINARY,
        transport=TransportKind.TCP,
        serial=None,
        request_bytes=33,
        response_bytes=15,
        end_code=0,
        prebuilt=True,
    )
    assert record.timing.wire_ms >= 0
    assert record.prebuilt is True
    assert record.plc_clock is None


# ========================================================================================
# Split
# ========================================================================================


def test_a_split_result_reports_the_span_it_was_sampled_over() -> None:
    """The loss of atomicity is in the type and in the number, not in a docstring."""
    value = Split(block=object(), transactions=(), snapshot_span_ns=27_000_000)
    assert "27.00 ms" in str(value)
    assert value.snapshot_span_ns == 27_000_000
