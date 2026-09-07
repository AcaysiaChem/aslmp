"""The in-flight gate: the mechanism that makes request coalescing inexpressible.

.. rubric:: The failure this exists for

On **MELSEC iQ-F FX5U-32MT/DS, firmware 1.065**, 2026-09-06, two SLMP requests written
to one TCP connection before the first response was read produced **one** response --
for the *last* request -- with end code ``0x0000``. On 3E there is no serial No., so the
first request's caller receives the second request's data, correctly framed, reported as
a success, with nothing anywhere to catch it. It is the only failure on that bench that
produces plausible wrong data with no error of any kind.

A rule in a docstring does not survive an ``asyncio.gather``. So there is no public
method anywhere in this package that puts bytes on a socket: the only path is a
capability token that is obtainable only from an async context manager, is exclusive per
connection, and burns itself after one exchange. Two writes in a row is not a
discouraged pattern -- it is a thing this API cannot express.

.. rubric:: Why the gate has a capacity at all

The same test over UDP returned **both** responses, correct and in order: datagrams are
framed and the coalescing failure simply does not exist there. The one-in-flight rule is
a TCP rule, not a universal one. So the gate takes a capacity, TCP always passes 1, and
UDP passes its pipeline depth -- which is itself refused above 1 unless the frame format
carries a serial No. to correlate with (see :mod:`aslmp.transport.udp`).

.. rubric:: Why a wait is counted and stamped

Under :attr:`Concurrency.SERIALIZE` a second submission waits instead of raising. That
wait lands in ``queue_ns`` and never in ``wire_ns``: a queue that does not report its own
delay makes every latency number in the library a lie, which is the hidden-reconnect
problem wearing a different hat.
"""

from __future__ import annotations

import asyncio
import enum
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, final

from aslmp.errors import SlmpConcurrentTransactionError, SlmpConfigurationError, SlmpUsageError
from aslmp.timing import Clock, Nanos

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import AsyncIterator, Sequence

    from aslmp.observability import Counters

__all__ = ["Concurrency", "GateSlot", "Holder", "TransactionGate"]

_NS_PER_MS = 1_000_000.0


class Concurrency(enum.Enum):
    """What a second concurrent submission does.

    ``STRICT`` (the default)
        It raises :class:`~aslmp.errors.SlmpConcurrentTransactionError`, naming the
        holder. A naive ``asyncio.gather`` of two reads on one client is therefore an
        error rather than silent corruption, and that is the correct lesson for this
        hardware even though it will be reported as our bug.
    ``SERIALIZE``
        It waits, FIFO, and the wait is stamped into ``queue_ns``. Nothing is reordered
        and nothing is dropped.

    There is no third member that queues without reporting.
    """

    STRICT = "strict"
    SERIALIZE = "serialize"


@final
@dataclass(frozen=True, slots=True)
class Holder:
    """Who is holding a slot, for the refusal message. Age is what makes it useful."""

    sequence: int
    command: int | None
    subcommand: int | None
    acquired_at: Nanos

    def describe(self, *, now: Nanos) -> str:
        what = (
            "an unnamed request"
            if self.command is None
            else f"0x{self.command:04X}"
            + ("" if self.subcommand is None else f" sub 0x{self.subcommand:04X}")
        )
        age = (now - self.acquired_at) / _NS_PER_MS
        return f"sequence {self.sequence} ({what}), in flight for {age:.2f} ms"


@final
class GateSlot:
    """A single-use lease on one in-flight slot. Not constructible by callers.

    :meth:`use` is what makes the capability single-use: the token that wraps this slot
    calls it before it writes anything, and a second call raises. The exchange is
    therefore atomic from the API's point of view -- send and read are one operation and
    then the right to write is gone.
    """

    __slots__ = ("_holder", "_used")

    def __init__(self, holder: Holder) -> None:
        self._holder = holder
        self._used = False

    @property
    def holder(self) -> Holder:
        return self._holder

    @property
    def sequence(self) -> int:
        return self._holder.sequence

    @property
    def used(self) -> bool:
        return self._used

    def use(self) -> None:
        """Burn the capability. The second call raises and nothing is sent."""
        if self._used:
            raise SlmpUsageError(
                f"this transaction token has already been used (sequence "
                f"{self._holder.sequence}). One token is one request/response "
                f"exchange; a second write on the same connection before the first "
                f"response has been read is the measured coalescing corruption -- on "
                f"FX5U-32MT/DS fw 1.065 the PLC answers ONCE, for the LAST request, "
                f"with end code 0x0000. Open a new transaction() instead."
            )
        self._used = True

    def __repr__(self) -> str:
        state = "used" if self._used else "unused"
        return f"GateSlot(sequence={self._holder.sequence}, {state})"


@final
class TransactionGate:
    """Hands out in-flight slots, up to ``capacity``, and refuses or queues past it.

    The gate is owned by :class:`aslmp.connection.Connection` and is not part of the
    public surface. It counts what it does: ``concurrent_rejections`` for every
    ``STRICT`` refusal and ``queue_waits`` for every ``SERIALIZE`` wait, both of which
    are questions a control loop eventually asks about itself.
    """

    __slots__ = ("_capacity", "_clock", "_counters", "_holders", "_mode", "_waiters")

    def __init__(
        self,
        *,
        clock: Clock,
        mode: Concurrency = Concurrency.STRICT,
        capacity: int = 1,
        counters: Counters | None = None,
    ) -> None:
        if capacity < 1:
            raise SlmpConfigurationError(
                f"an in-flight capacity of {capacity} admits no transactions at all."
            )
        self._clock = clock
        self._mode = mode
        self._capacity = capacity
        self._counters = counters
        self._holders: dict[int, Holder] = {}
        self._waiters: deque[asyncio.Future[None]] = deque()

    # -- inspection ----------------------------------------------------------

    @property
    def mode(self) -> Concurrency:
        return self._mode

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def in_flight(self) -> int:
        return len(self._holders)

    @property
    def waiting(self) -> int:
        return len(self._waiters)

    @property
    def holders(self) -> Sequence[Holder]:
        return tuple(self._holders.values())

    # -- the lease -----------------------------------------------------------

    @asynccontextmanager
    async def lease(
        self,
        *,
        sequence: int,
        command: int | None = None,
        subcommand: int | None = None,
    ) -> AsyncIterator[GateSlot]:
        """Hold one in-flight slot for the body of the ``async with``.

        The slot is released on the way out however the body leaves -- return, raise or
        cancellation -- and the next FIFO waiter is woken there and only there.
        """
        await self._acquire(sequence=sequence, command=command, subcommand=subcommand)
        slot = GateSlot(self._holders[sequence])
        try:
            yield slot
        finally:
            self._release(sequence)

    async def _acquire(
        self, *, sequence: int, command: int | None, subcommand: int | None
    ) -> None:
        if sequence in self._holders:
            raise SlmpUsageError(
                f"sequence {sequence} is already in flight on this gate; sequence "
                f"numbers come from one allocator and are never reused."
            )
        if not self._free():
            if self._mode is Concurrency.STRICT:
                self._count("concurrent_rejections")
                raise self._refusal(sequence=sequence)
            self._count("queue_waits")
            await self._wait()
        self._holders[sequence] = Holder(
            sequence=sequence,
            command=command,
            subcommand=subcommand,
            acquired_at=Nanos(self._clock()),
        )

    def _free(self) -> bool:
        """A slot is free only when nobody is already queued for it: FIFO, always."""
        return len(self._holders) < self._capacity and not self._waiters

    async def _wait(self) -> None:
        waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiters.append(waiter)
        try:
            await waiter
        except BaseException:
            # Cancelled while queued: drop out of the line and, if we had already been
            # handed the slot in the same instant, pass it to the next waiter rather
            # than leaking capacity.
            if waiter in self._waiters:
                self._waiters.remove(waiter)
            elif waiter.done() and not waiter.cancelled():
                self._wake_next()
            raise

    def _release(self, sequence: int) -> None:
        self._holders.pop(sequence, None)
        self._wake_next()

    def _wake_next(self) -> None:
        while self._waiters:
            waiter = self._waiters.popleft()
            if not waiter.done():
                waiter.set_result(None)
                return

    def _refusal(self, *, sequence: int) -> SlmpConcurrentTransactionError:
        now = Nanos(self._clock())
        held = "; ".join(holder.describe(now=now) for holder in self._holders.values())
        return SlmpConcurrentTransactionError(
            f"sequence {sequence} was submitted while {len(self._holders)} of "
            f"{self._capacity} in-flight slot(s) are held: {held}. Concurrency.STRICT "
            f"refuses rather than writing a second request onto the same connection: "
            f"measured on FX5U-32MT/DS fw 1.065, two TCP requests written before the "
            f"first response is read return ONE response, for the LAST request, with "
            f"end code 0x0000 -- and 3E carries no serial No. to detect it with. Await "
            f"the first call, or construct the client with "
            f"concurrency=Concurrency.SERIALIZE to queue instead (the wait is reported "
            f"in queue_ns)."
        )

    def _count(self, field: str) -> None:
        counters = self._counters
        if counters is not None:
            setattr(counters, field, getattr(counters, field) + 1)

    def __repr__(self) -> str:
        return (
            f"TransactionGate(mode={self._mode.value}, capacity={self._capacity}, "
            f"in_flight={self.in_flight}, waiting={self.waiting})"
        )
