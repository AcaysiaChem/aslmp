"""Layer 4 -- what a call gives back, and what it gives back beside the value.

Graft G14 of the locked architecture (DESIGN.md section 2.5): **the primary surface
returns bare values** and :class:`Reading` lives on ``plc.timed.*``. ``.value`` on every
line, forever, on a library whose main job is reading one float, is the wrong tax; but a
control loop that cannot see ``wire_ms`` is worse. Both surfaces exist, one is generated
from the other (``tools/gen_timed.py``), and neither is a wrapper the common path pays
for.

Three things in here are deliberate and each closes a hole a judge found:

**Nothing is iterable that cannot say what it yields.** :class:`Reading` has no
``__iter__``. An ``__iter__`` returning ``Iterator[Any]`` on a generic result throws away
the one type the class exists to preserve, and
``value, tx = await plc.timed.read_f32(...)`` reads like a tuple until the day somebody
writes ``for x in reading``.

**A random read is POSITIONAL, never dict-keyed.** Reading ``D0`` as ``u16`` and ``D0``
paired with ``D1`` as ``f32`` in one ``0403`` is legal SLMP, and a dict keyed by device
string cannot express it -- which is why ``plc-comm-slmp`` bans the harmless aliasing
``['D0:U', 'D0:F']`` while allowing ``['D0:F', 'D1:U']``, the genuinely confusing
physical overlap. :class:`RandomReading` is a sequence in the caller's own order.

**A split read is a different type, not a flag.** :class:`SplitReading` is not a
:class:`RandomReading`. Splitting destroys the single-snapshot atomicity that is the
entire reason to send a ``0403``, and ``snapshot_span_ns`` is how far apart the halves
were actually sampled. The loss is in the type, not in a docstring.

``Any`` appears nowhere here. DESIGN.md section 2.5 sketches ``RandomReading`` as
``Sequence[Any]``; :mod:`aslmp.commands.random` already defines ``RandomValue`` as
``int | float | tuple[bool, ...]`` and calls it "a union, deliberately, and never
``Any``", so this module uses that union and the typed accessors narrow it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Generic, TypeVar, final, overload

from aslmp.commands.random import RandomPoint, RandomValue
from aslmp.connection import ConnectionInfo
from aslmp.identity import CpuStatus
from aslmp.timing import Transaction

__all__ = [
    "BlockReading",
    "ConnectionInfo",
    "RandomReading",
    "Reading",
    "RemoteResult",
    "ResetOutcome",
    "SplitReading",
    "WriteAck",
]

T = TypeVar("T")
B = TypeVar("B")


# ========================================================================================
# The scalar result
# ========================================================================================


@final
@dataclass(frozen=True, slots=True)
class Reading(Generic[T]):
    """One value and the transaction that produced it.

    Returned only by ``plc.timed.*``. ``plc.read_f32("D0")`` is a ``float`` and
    ``(await plc.timed.read_f32("D0")).tx.timing.wire_ms`` is the round trip that
    produced it, from the same code path -- ``timed.py`` is generated from
    ``client.py``, so the two cannot drift.

    Deliberately **not iterable**: see the module docstring.
    """

    value: T
    tx: Transaction

    def __str__(self) -> str:
        return f"{self.value!r} in {self.tx.timing.wire_ms:.2f} ms"


@final
@dataclass(frozen=True, slots=True)
class WriteAck:
    """What a write gives back on ``plc.timed.*``: how many points, and the record.

    A write has no response data at all -- ``1401``, ``1402`` and every remote-control
    command answer with the 11-byte minimum frame, ``L = 0x0002``, end code and nothing
    after it (measured on FX5U-32MT/DS fw 1.065, 2026-09-06). So there is no value to
    return, and ``points`` is what is worth saying instead: it is what the request
    asserted, carried back so that a caller reading ``ack.points`` knows how much of the
    device memory this transaction moved.

    Not generic, deliberately. A ``WriteAck[float]`` whose only float is the argument the
    caller just passed in would be a type parameter that carries no information the call
    site does not already have.
    """

    points: int
    tx: Transaction

    def __str__(self) -> str:
        return f"wrote {self.points} point(s) in {self.tx.timing.wire_ms:.2f} ms"


# ========================================================================================
# Random access -- the control-loop primitive
# ========================================================================================


def _kind_error(point: RandomPoint, index: int, wanted: str) -> TypeError:
    return TypeError(
        f"point {index} is {point} with kind={point.kind!r}; .{wanted}({index}) would "
        f"reinterpret it. The kind is fixed when the point is built -- word(), dword() "
        f"and bit_point() -- because the wire carries no type tag at all and the request "
        f"is the only thing that knows what its own words mean."
    )


def _decoder_bug(point: RandomPoint, index: int, value: RandomValue) -> TypeError:
    return TypeError(
        f"point {index} is {point} with kind={point.kind!r} and its decoded value is a "
        f"{type(value).__name__}. That is a bug in aslmp, not in your call: the command "
        f"that built the request is the same object that decoded the response. Please "
        f"report it with the point list and the coding."
    )


@final
@dataclass(frozen=True, slots=True)
class RandomReading(Sequence[RandomValue]):
    """One ``0403`` Device Read Random, in the caller's own point order.

    The wire demands every word specification before every double-word specification
    (SH(NA)-080956ENG-M section 6.4 pp.53-56) and carries no framing at all between the
    two groups. The command sorts the points into those groups to encode, decodes the
    response against the same partition, and restores the caller's order before this
    object is built -- so ``reading[2]`` is the third point *as written*, never the third
    word on the wire.

    The typed accessors are the point of the class: ``reading.f32(1)`` is a ``float`` to
    ``mypy``, with no ``cast`` and no ``isinstance`` at the call site
    (``tests/typing/consumer.py`` makes that a build failure rather than a claim).
    """

    points: tuple[RandomPoint, ...]
    values: tuple[RandomValue, ...]
    tx: Transaction

    def __post_init__(self) -> None:
        if len(self.points) != len(self.values):
            raise ValueError(
                f"a random reading has one value per point; got {len(self.points)} "
                f"point(s) and {len(self.values)} value(s). Nothing here pads the "
                f"difference: a response one word short is not good values and a zero."
            )

    @overload
    def __getitem__(self, index: int) -> RandomValue: ...

    @overload
    def __getitem__(self, index: slice) -> tuple[RandomValue, ...]: ...

    def __getitem__(self, index: int | slice) -> RandomValue | tuple[RandomValue, ...]:
        return self.values[index]

    def __len__(self) -> int:
        return len(self.values)

    # -- typed accessors -----------------------------------------------------

    def f32(self, index: int) -> float:
        """The float at ``index``. Raises unless that point was built as ``f32``."""
        point = self.points[index]
        if point.kind != "f32":
            raise _kind_error(point, index, "f32")
        value = self.values[index]
        if not isinstance(value, float):
            raise _decoder_bug(point, index, value)
        return value

    def i32(self, index: int) -> int:
        """The signed double word at ``index``."""
        return self._int(index, "i32")

    def u32(self, index: int) -> int:
        """The unsigned double word at ``index``."""
        return self._int(index, "u32")

    def i16(self, index: int) -> int:
        """The signed word at ``index``."""
        return self._int(index, "i16")

    def u16(self, index: int) -> int:
        """The unsigned word at ``index``."""
        return self._int(index, "u16")

    def bits(self, index: int) -> tuple[bool, ...]:
        """The 16 bits of the word-access point at ``index``, LSB first.

        The named device is the least significant bit of the window
        (SH(NA)-080956ENG-M p.54), which is why a bit point is a *word* access point and
        why ``bit_point("M100")`` and ``bit_point("M107")`` in one request are one point,
        not two.
        """
        point = self.points[index]
        if point.kind != "bits":
            raise _kind_error(point, index, "bits")
        value = self.values[index]
        if not isinstance(value, tuple):
            raise _decoder_bug(point, index, value)
        return value

    def _int(self, index: int, wanted: str) -> int:
        point = self.points[index]
        if point.kind != wanted:
            raise _kind_error(point, index, wanted)
        value = self.values[index]
        if not isinstance(value, int) or isinstance(value, bool):
            raise _decoder_bug(point, index, value)
        return value

    def __str__(self) -> str:
        return f"{len(self.values)} point(s) in {self.tx.timing.wire_ms:.2f} ms"


@final
@dataclass(frozen=True, slots=True)
class SplitReading:
    """Several transactions' worth of points. **Not** a :class:`RandomReading`.

    Splitting a ``0403`` over the point ceiling destroys the single-snapshot atomicity
    that is the entire reason to send one: the halves are sampled ``snapshot_span_ns``
    apart, which is milliseconds of a plant that is moving. The type is different so that
    a caller cannot receive one where the atomic answer was assumed, and
    ``allow_split=True`` is the only way to ask for it -- ``@overload`` on
    ``Literal[True]`` makes the union appear only for callers who opted in.
    """

    points: tuple[RandomPoint, ...]
    values: tuple[RandomValue, ...]
    transactions: tuple[Transaction, ...]
    snapshot_span_ns: int

    def __post_init__(self) -> None:
        if len(self.points) != len(self.values):
            raise ValueError(
                f"a split reading has one value per point; got {len(self.points)} "
                f"point(s) and {len(self.values)} value(s)."
            )
        if len(self.transactions) < 2:
            raise ValueError(
                "a SplitReading records at least two transactions; one transaction is a "
                "RandomReading, and returning this type for it would report a snapshot "
                "span that never happened."
            )

    @overload
    def __getitem__(self, index: int) -> RandomValue: ...

    @overload
    def __getitem__(self, index: slice) -> tuple[RandomValue, ...]: ...

    def __getitem__(self, index: int | slice) -> RandomValue | tuple[RandomValue, ...]:
        return self.values[index]

    def __len__(self) -> int:
        return len(self.values)

    @property
    def snapshot_span_ms(self) -> float:
        """How far apart the first and last transactions of this reading were sampled."""
        return self.snapshot_span_ns / 1_000_000.0

    def __str__(self) -> str:
        return (
            f"{len(self.values)} point(s) over {len(self.transactions)} transactions, "
            f"sampled {self.snapshot_span_ms:.2f} ms apart"
        )


# ========================================================================================
# Blocks
# ========================================================================================


@final
@dataclass(frozen=True, slots=True)
class BlockReading(Generic[B]):
    """One decoded ``@plc_block`` instance and the transaction that produced it.

    A block instance carries its own ``tx`` field (DESIGN.md section 2.7), so a control
    loop reads ``state.tx.timing.wire_ms`` with no ceremony at all. This exists for the
    generated timed surface, where every method returns a two-field record whatever it
    read, and for a caller who wants the record without naming the block type.
    """

    block: B
    tx: Transaction


# ========================================================================================
# Remote control
# ========================================================================================


@final
@dataclass(frozen=True, slots=True)
class ResetOutcome:
    """What ``0x1006`` Remote Reset did. An ABSENT response is the expected outcome.

    SH(NA)-080956ENG-M p.136: when the reset succeeds "the response request is not be
    sent back to the external device", and over TCP the connection goes with it. So
    ``responded=False, connection_closed=True`` is the *success* shape, and this is the
    one command in the package for which silence is not a failure. ``tx`` is ``None``
    exactly when nothing answered.
    """

    responded: bool
    connection_closed: bool
    tx: Transaction | None = None

    def __str__(self) -> str:
        answered = "answered" if self.responded else "did not answer (expected)"
        closed = "connection closed" if self.connection_closed else "connection still open"
        return f"remote reset {answered}, {closed}"


@final
@dataclass(frozen=True, slots=True)
class RemoteResult:
    """One remote-control action, the transaction that carried it, and what SD203 said.

    ``verify=True`` is the default on run / stop / pause (graft G15) because Mitsubishi
    documents Remote RUN with the switch in STOP as completing normally while "the access
    destination does not become the RUN state" (SH(NA)-080956ENG-M p.131). A successful
    end code is therefore not evidence, and ``status`` is the second round trip that makes
    the answer true. ``verified`` says whether that round trip happened at all.
    """

    requested: str
    status: CpuStatus | None
    verified: bool
    tx: Transaction
    verify_tx: Transaction | None = None
    polls: int = 0
    """How many times SD203 was read before it agreed, or the deadline expired.

    Repeated *observation*, never a repeated command. Entering RUN takes the CPU an extra
    scan or two -- measured on FX5U-32MT/DS fw 1.065, SD203 still said STOP on the first
    poll in two cycles of three -- so a value above 1 here is normal for ``run()`` and
    would be unusual for ``stop()``.
    """

    def __str__(self) -> str:
        if not self.verified:
            return f"{self.requested}: end code 0x0000, UNVERIFIED"
        return f"{self.requested}: end code 0x0000, SD203 reports {self.status}"
