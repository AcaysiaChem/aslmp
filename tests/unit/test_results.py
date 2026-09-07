"""What a call gives back: positional, typed, and never quietly widened.

Three properties, and each is a defect somebody else shipped.

``Reading`` is not iterable, because ``__iter__ -> Iterator[Any]`` on a generic result
throws away the type the class exists to carry. ``RandomReading`` is positional rather
than dict-keyed, because ``0403`` can legally read ``D0`` as a word and ``D0``/``D1`` as a
float in the same snapshot and a dict keyed by device string cannot say that.
``SplitReading`` is a different class from ``RandomReading``, because splitting destroys
the single-snapshot atomicity that is the entire reason to send a ``0403`` -- and a loss
that lives in a docstring is a loss nobody notices.

The typed accessors get their own tests for a reason that no runtime assertion can state:
they exist so that ``reading.f32(0)`` is a ``float`` to ``mypy``. That half is in
``tests/typing/consumer.py``. What is asserted here is the other half -- that asking for
the wrong kind raises instead of reinterpreting sixteen bits as a float.
"""

from __future__ import annotations

import pytest

from aslmp.commands.random import bit_point, dword, word
from aslmp.identity import CpuStatus
from aslmp.profile import Encoding
from aslmp.results import (
    BlockReading,
    RandomReading,
    Reading,
    RemoteResult,
    ResetOutcome,
    SplitReading,
    WriteAck,
)
from aslmp.timing import Chunk, Nanos, Transaction, TransactionTiming
from aslmp.transport.base import TransportKind
from aslmp.wire.frames import FrameType

MS = 1_000_000


def a_timing(*, wire_ns: int = 7 * MS) -> TransactionTiming:
    """A complete timing record whose ``wire_ns`` is exactly what was asked for."""
    arrived = Nanos(3 + wire_ns)
    return TransactionTiming(
        submitted_at=Nanos(0),
        gate_acquired_at=Nanos(1),
        encoded_at=Nanos(2),
        sent_at=Nanos(3),
        first_byte_at=arrived,
        received_at=arrived,
        chunks=(Chunk(21, arrived),),
        decoded_at=Nanos(4 + wire_ns),
    )


def a_transaction(*, command: int = 0x0403, sequence: int = 1) -> Transaction:
    return Transaction(
        timing=a_timing(),
        sequence=sequence,
        connection_id="conn-test",
        generation=0,
        after_reconnect=False,
        command=command,
        subcommand=0x0000,
        frame=FrameType.THREE_E,
        encoding=Encoding.BINARY,
        transport=TransportKind.TCP,
        serial=None,
        request_bytes=25,
        response_bytes=21,
        end_code=0,
        prebuilt=False,
    )


# ========================================================================================
# Reading and WriteAck
# ========================================================================================


def test_a_reading_carries_the_value_and_its_record() -> None:
    tx = a_transaction()
    reading = Reading(60.0, tx)
    assert reading.value == 60.0
    assert reading.tx is tx
    assert reading.tx.timing.wire_ms == pytest.approx(7.0)


def test_a_reading_is_not_iterable() -> None:
    """An ``__iter__`` returning ``Iterator[Any]`` throws away the point of the class.

    ``value, tx = await plc.timed.read_f32("D0")`` would read exactly like a tuple until
    the day somebody writes ``for x in reading`` and gets a float and a transaction.
    """
    reading = Reading(60.0, a_transaction())
    assert not hasattr(reading, "__iter__")
    with pytest.raises(TypeError):
        list(reading)  # type: ignore[call-overload]  # that is the assertion


def test_a_reading_is_frozen() -> None:
    reading = Reading(60.0, a_transaction())
    with pytest.raises(AttributeError):
        reading.value = 61.0  # type: ignore[misc]  # frozen dataclass, on purpose


def test_a_write_ack_says_how_many_points_moved() -> None:
    ack = WriteAck(2, a_transaction(command=0x1401))
    assert ack.points == 2
    assert "2 point(s)" in str(ack)


def test_a_write_ack_is_not_generic() -> None:
    """A ``WriteAck[float]`` would parameterise on the argument the caller just passed."""
    with pytest.raises(TypeError):
        WriteAck[float]  # type: ignore[misc]  # that is the assertion


# ========================================================================================
# RandomReading -- positional, typed, and refusing to reinterpret
# ========================================================================================


def a_random_reading() -> RandomReading:
    points = (dword("D0", kind="f32"), word("D8"), bit_point("M100"), dword("D4", kind="i32"))
    values = (60.0, 7, (True, False) + (False,) * 14, -3)
    return RandomReading(points, values, a_transaction())


def test_random_results_are_positional_and_in_the_callers_own_order() -> None:
    """The wire groups words before double words; the caller's order is restored.

    A dict keyed by device string could not express this request at all: reading ``D0``
    as one float and ``D8`` as one word in the same snapshot is ordinary, and so is
    reading ``D0`` twice at two widths.
    """
    reading = a_random_reading()
    assert len(reading) == 4
    assert reading[0] == 60.0
    assert reading[1] == 7
    assert reading[3] == -3
    assert list(reading[0:2]) == [60.0, 7]


def test_the_typed_accessors_return_the_declared_kind() -> None:
    reading = a_random_reading()
    assert reading.f32(0) == 60.0
    assert reading.u16(1) == 7
    assert reading.bits(2)[:2] == (True, False)
    assert reading.i32(3) == -3


@pytest.mark.parametrize(
    ("accessor", "index"),
    [("f32", 1), ("u16", 0), ("bits", 0), ("i32", 1), ("u32", 2), ("i16", 3)],
)
def test_asking_for_the_wrong_kind_raises_instead_of_reinterpreting(
    accessor: str, index: int
) -> None:
    """The kind is fixed when the point is built, because the wire carries no type tag.

    Sixteen bits read as a float is a plausible number. The request is the only thing
    that knows what its own words mean, so a mismatch is a refusal and never a cast.
    """
    reading = a_random_reading()
    with pytest.raises(TypeError, match="kind="):
        getattr(reading, accessor)(index)


def test_a_reading_with_more_points_than_values_is_refused() -> None:
    """A response one word short is not good values and a zero.

    ``pymcprotocol`` turns ``[111, 222, 333, 444]`` into ``[111, 222, 0, 0]`` at exactly
    this seam.
    """
    with pytest.raises(ValueError, match="one value per point"):
        RandomReading((word("D0"), word("D1")), (1,), a_transaction())


def test_a_random_reading_is_a_sequence() -> None:
    reading = a_random_reading()
    assert 7 in reading
    assert reading.index(7) == 1
    assert next(iter(reading)) == 60.0


def test_a_random_reading_carries_its_own_transaction() -> None:
    """A control loop wants the record with no ceremony (DESIGN.md section 2.5)."""
    reading = a_random_reading()
    assert reading.tx.command == 0x0403
    assert reading.tx.timing.wire_ms == pytest.approx(7.0)


# ========================================================================================
# SplitReading -- the loss is in the type
# ========================================================================================


def test_a_split_reading_is_not_a_random_reading() -> None:
    """Separate reads sample a moving plant milliseconds apart. That is not a snapshot."""
    split = SplitReading(
        (word("D0"), word("D1")),
        (1, 2),
        (a_transaction(sequence=1), a_transaction(sequence=2)),
        27 * MS,
    )
    assert RandomReading not in type(split).__mro__
    assert split.snapshot_span_ms == pytest.approx(27.0)
    assert len(split) == 2
    assert split[1] == 2
    assert "27.00 ms apart" in str(split)


def test_a_split_reading_needs_at_least_two_transactions() -> None:
    """One transaction is a RandomReading; reporting a span for it would invent one."""
    with pytest.raises(ValueError, match="at least two transactions"):
        SplitReading((word("D0"),), (1,), (a_transaction(),), 0)


def test_a_split_reading_refuses_a_value_count_that_does_not_match() -> None:
    with pytest.raises(ValueError, match="one value per point"):
        SplitReading(
            (word("D0"), word("D1")),
            (1,),
            (a_transaction(sequence=1), a_transaction(sequence=2)),
            0,
        )


# ========================================================================================
# Blocks and remote control
# ========================================================================================


def test_a_block_reading_carries_the_instance_and_the_record() -> None:
    tx = a_transaction()
    reading = BlockReading("a block instance", tx)
    assert reading.block == "a block instance"
    assert reading.tx is tx


def test_an_unanswered_reset_is_the_successful_shape() -> None:
    """SH(NA)-080956ENG-M p.136: on success the response is not sent back at all."""
    outcome = ResetOutcome(responded=False, connection_closed=True)
    assert outcome.tx is None
    assert "did not answer (expected)" in str(outcome)
    assert "connection closed" in str(outcome)


def test_a_verified_remote_result_names_what_sd203_said() -> None:
    result = RemoteResult(
        "remote.run()", CpuStatus.RUN, True, a_transaction(command=0x1001), a_transaction()
    )
    assert "SD203 reports RUN" in str(result)


def test_an_unverified_remote_result_says_so_in_capitals() -> None:
    """``verify=False`` is a choice to trust an end code Mitsubishi documents as a lie."""
    result = RemoteResult("remote.run()", None, False, a_transaction(command=0x1001))
    assert "UNVERIFIED" in str(result)
    assert result.verify_tx is None
