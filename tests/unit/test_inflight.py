"""The in-flight gate: the mechanism that makes the measured coalescing bug unsayable.

The failure being defended against, on **FX5U-32MT/DS fw 1.065**: two TCP requests
written before the first response is read produce ONE response, for the LAST request,
with end code ``0x0000``. On 3E there is no serial No., so the first caller silently
receives the second caller's data. These tests assert the three properties that make
that inexpressible rather than discouraged -- a token is single-use, a second holder is
refused or queued, and a queue reports its own wait.
"""

from __future__ import annotations

import asyncio

import pytest

from aslmp.errors import SlmpConcurrentTransactionError, SlmpConfigurationError, SlmpUsageError
from aslmp.observability import Counters
from aslmp.transport.inflight import Concurrency, TransactionGate

NS_PER_S = 1_000_000_000


class FakeClock:
    def __init__(self) -> None:
        self.now = 0

    def __call__(self) -> int:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += int(seconds * NS_PER_S)


def a_gate(
    *,
    mode: Concurrency = Concurrency.STRICT,
    capacity: int = 1,
    counters: Counters | None = None,
    clock: FakeClock | None = None,
) -> TransactionGate:
    return TransactionGate(
        clock=clock or FakeClock(),
        mode=mode,
        capacity=capacity,
        counters=counters,
    )


# --------------------------------------------------------------------------------------
# The single-use token
# --------------------------------------------------------------------------------------


async def test_a_slot_is_single_use() -> None:
    """One token, one exchange. A second write is the coalescing corruption."""
    gate = a_gate()
    async with gate.lease(sequence=1, command=0x0401) as slot:
        states = [slot.used]
        slot.use()
        states.append(slot.used)
        assert states == [False, True]
        with pytest.raises(SlmpUsageError) as caught:
            slot.use()
    assert "0x0000" in str(caught.value)
    assert "LAST request" in str(caught.value)


async def test_the_slot_is_released_even_when_the_body_raises() -> None:
    gate = a_gate()
    with pytest.raises(ZeroDivisionError):
        async with gate.lease(sequence=1):
            raise ZeroDivisionError
    assert gate.in_flight == 0
    async with gate.lease(sequence=2):
        assert gate.in_flight == 1


# --------------------------------------------------------------------------------------
# STRICT
# --------------------------------------------------------------------------------------


async def test_strict_refuses_a_second_holder_and_names_the_first() -> None:
    clock = FakeClock()
    counters = Counters()
    gate = a_gate(counters=counters, clock=clock)
    async with gate.lease(sequence=11, command=0x0403, subcommand=0x0000):
        clock.advance(0.004)
        with pytest.raises(SlmpConcurrentTransactionError) as caught:
            async with gate.lease(sequence=12, command=0x0401):
                pass  # pragma: no cover - the lease above raises
    message = str(caught.value)
    assert "sequence 11" in message
    assert "0x0403" in message
    assert "4.00 ms" in message
    assert "SERIALIZE" in message
    assert counters.concurrent_rejections == 1
    assert counters.queue_waits == 0


async def test_strict_refuses_a_real_gather_of_two_reads() -> None:
    """``asyncio.gather`` of two reads on one connection is the shape of the bug."""
    gate = a_gate()
    started = asyncio.Event()

    async def slow() -> str:
        async with gate.lease(sequence=1, command=0x0401):
            started.set()
            await asyncio.sleep(0.02)
            return "first"

    async def second() -> str:
        await started.wait()
        async with gate.lease(sequence=2, command=0x0401):
            return "second"  # pragma: no cover - never reached

    results = await asyncio.gather(slow(), second(), return_exceptions=True)
    assert results[0] == "first"
    assert isinstance(results[1], SlmpConcurrentTransactionError)


# --------------------------------------------------------------------------------------
# SERIALIZE
# --------------------------------------------------------------------------------------


async def test_serialize_waits_and_counts_the_wait() -> None:
    counters = Counters()
    gate = a_gate(mode=Concurrency.SERIALIZE, counters=counters)
    order: list[str] = []
    holding = asyncio.Event()

    async def first() -> None:
        async with gate.lease(sequence=1):
            holding.set()
            await asyncio.sleep(0.02)
            order.append("first")

    async def second() -> None:
        await holding.wait()
        async with gate.lease(sequence=2):
            order.append("second")

    await asyncio.gather(first(), second())
    assert order == ["first", "second"]
    assert counters.queue_waits == 1
    assert counters.concurrent_rejections == 0
    assert gate.in_flight == 0


async def test_serialize_is_fifo() -> None:
    """Nothing is reordered: a queue that reorders is a queue that starves somebody."""
    gate = a_gate(mode=Concurrency.SERIALIZE)
    order: list[int] = []
    holding = asyncio.Event()

    async def hold() -> None:
        async with gate.lease(sequence=0):
            holding.set()
            await asyncio.sleep(0.03)

    async def queued(n: int) -> None:
        await holding.wait()
        await asyncio.sleep(0.001 * n)
        async with gate.lease(sequence=n):
            order.append(n)

    await asyncio.gather(hold(), queued(1), queued(2), queued(3))
    assert order == [1, 2, 3]


async def test_a_cancelled_waiter_does_not_leak_capacity() -> None:
    gate = a_gate(mode=Concurrency.SERIALIZE)
    holding = asyncio.Event()

    async def hold() -> None:
        async with gate.lease(sequence=1):
            holding.set()
            await asyncio.sleep(0.05)

    async def queued() -> None:
        await holding.wait()
        async with gate.lease(sequence=2):
            pass  # pragma: no cover - cancelled while queued

    holder = asyncio.create_task(hold())
    waiter = asyncio.create_task(queued())
    await holding.wait()
    await asyncio.sleep(0.005)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    await holder
    assert gate.in_flight == 0
    assert gate.waiting == 0
    async with gate.lease(sequence=3):
        assert gate.in_flight == 1


# --------------------------------------------------------------------------------------
# Capacity, holders, refusals
# --------------------------------------------------------------------------------------


async def test_capacity_above_one_admits_a_pipelined_burst() -> None:
    """The one-in-flight rule is a TCP rule: UDP answered both of two unread requests."""
    gate = a_gate(capacity=4)
    async with gate.lease(sequence=1), gate.lease(sequence=2), gate.lease(sequence=3):
        assert gate.in_flight == 3
        assert {holder.sequence for holder in gate.holders} == {1, 2, 3}
    assert gate.in_flight == 0


async def test_capacity_is_still_a_ceiling() -> None:
    gate = a_gate(capacity=2)
    async with gate.lease(sequence=1), gate.lease(sequence=2):
        with pytest.raises(SlmpConcurrentTransactionError) as caught:
            async with gate.lease(sequence=3):
                pass  # pragma: no cover
    assert "2 of 2 in-flight slot(s)" in str(caught.value)


def test_a_gate_with_no_capacity_is_refused() -> None:
    with pytest.raises(SlmpConfigurationError):
        a_gate(capacity=0)


async def test_a_reused_sequence_number_is_refused() -> None:
    gate = a_gate(capacity=2)
    async with gate.lease(sequence=5):
        with pytest.raises(SlmpUsageError):
            async with gate.lease(sequence=5):
                pass  # pragma: no cover


def test_repr_says_what_the_gate_is_doing() -> None:
    gate = a_gate(mode=Concurrency.SERIALIZE, capacity=3)
    assert "serialize" in repr(gate)
    assert "capacity=3" in repr(gate)
