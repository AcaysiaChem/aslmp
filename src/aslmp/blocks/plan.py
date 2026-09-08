"""Layer 5 -- binding a block layout to one connected CPU, and reading it in one frame.

This is the feature the original brief asked for in its own words: declare a block of
typed fields once, read it in a single random read, get a typed object back.

**``bind()`` is synchronous, and the absence of ``async`` is load-bearing.** It does no
I/O at all. The layout is already computed (:mod:`aslmp.blocks.layout`, pure, at class
definition); binding is where a *profile* joins it -- radix resolution, device existence,
whole-span range validation including every folded bit window, the point budget against
``LimitKey(0x0403, encoding, WORD, link)``, the subcommand family, the prebuilt frame and
one compiled :class:`struct.Struct`. Every one of those is a start-up failure, with a
paragraph, rather than a surprise inside a control loop at 3am.

**There is no path that takes an unbound class** (graft G16). ``plan.read()`` is the only
call shape, so the unvalidated, revalidate-every-cycle path is not merely discouraged --
it does not exist, and cannot become the shortest thing to type in a hot loop.

**The hot path.** ``read()`` sends bytes that were built once: on 3E the frame is a
constant, on 4E the serial number is patched in place through a two-unit slice of a
buffer that is otherwise a constant, and nothing is rebuilt. The response is unpacked by
one compiled ``Struct`` over the whole payload -- legal because a ``0403`` response is
bare data with no framing between access points (SH(NA)-080956ENG-M section 6.4
pp.53-56) -- and turned into one frozen instance. The work per cycle is therefore the
same for a four-field block and a hundred-field one, which
``tests/unit/test_blocks_plan.py`` asserts rather than claims.

**A plan does not resume into different silicon** (graft G6). It records the model code
the CPU reported at bind, and ``read()`` refuses with
:class:`~aslmp.errors.SlmpTargetChangedError` if the client is now talking to a different
one. A prebuilt ``0403`` carried across a reconnect into a different D-memory layout
returns plausible floats and end code ``0x0000``; nothing else in the protocol would ever
tell you.

**How this module reaches the client.** It is a peer of :mod:`aslmp.client` at layer 5 and
uses four of its internals -- the frozen :class:`~aslmp.commands.base.EncodeContext`, the
:class:`~aslmp.connection.Connection`, the transaction-record builder and the observation
sink -- because a block read must land in the same counters, the same histogram and the
same ``on_transaction`` stream as every other transaction. ``Plc`` publishes no accessor
for them today; DESIGN.md section 2.7 gives ``Plc.bind`` / ``Plc.read_block`` to
``client.py``, and when those land they should delegate straight to :func:`bind` here.
"""

from __future__ import annotations

import dataclasses
import struct
import textwrap
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Generic, Literal, TypeVar, final, overload

from aslmp.blocks.fields import (
    STRING_WORDS,
    WORD_ORDER_PROOF,
    BitSpec,
    Bounds,
    NumberSpec,
    StringSpec,
    implausible,
    outside,
    refuse_write,
)
from aslmp.blocks.layout import (
    BitFold,
    BlockLayout,
    FieldPlan,
    layout_of,
    lower_anchor,
    plan_folds,
)
from aslmp.blocks.layout import (
    cites as layout_cites,
)
from aslmp.commands.base import (
    CommandSummary,
    EncodeContext,
    WordOrder,
    encoded,
    expect_payload_len,
    real,
    unsigned,
)
from aslmp.commands.random import (
    AccessWidth,
    BitWrite,
    RandomPoint,
    RandomWrite,
    ReadRandom,
    WriteRandom,
    WriteRandomBits,
)
from aslmp.connection import Txn
from aslmp.errors import (
    Diagnostics,
    SlmpBlockLayoutError,
    SlmpError,
    SlmpPayloadShapeError,
    SlmpPointLimitError,
    SlmpTargetChangedError,
    SlmpUsageError,
)
from aslmp.errors.routing import end_code_error
from aslmp.profile import Encoding
from aslmp.timing import Transaction
from aslmp.wire.address import DeviceAddress
from aslmp.wire.citations import Source
from aslmp.wire.codec import Codec, Unit
from aslmp.wire.frames import request_body
from aslmp.wire.raw import RawResponse

if TYPE_CHECKING:  # the peer at layer 5; every use is behind a deferred annotation
    from aslmp.client import Plc, PlcClockSource

__all__ = [
    "BlockPlan",
    "Split",
    "SplitBlockPlan",
    "bind",
]

B = TypeVar("B")

_READ_RANDOM: Final = 0x0403
_WRITE_RANDOM: Final = 0x1402
_TX_FIELD: Final = "tx"


# ========================================================================================
# What a split read gives back
# ========================================================================================


@final
@dataclass(frozen=True, slots=True)
class Split(Generic[B]):
    """A block whose fields were **not** sampled together. Deliberately not a ``B``.

    A ``0403`` over the CPU's point ceiling has to become several requests, and that
    destroys the single-snapshot atomicity which is the entire reason to send one: on our
    bench, three separate reads sampled the plant up to 27 ms apart (FX5U-32MT/DS fw
    1.065, 2026-09-06). The loss is in the *type* rather than in a docstring, so a caller
    who did not opt in cannot be handed one by accident.

    ``snapshot_span_ns`` is the distance from the first request leaving to the last
    response arriving: how far apart, in time, the fields inside ``block`` really are.
    """

    block: B
    transactions: tuple[Transaction, ...]
    snapshot_span_ns: int

    def __str__(self) -> str:
        return (
            f"{type(self.block).__name__} across {len(self.transactions)} transaction(s)"
            f" spanning {self.snapshot_span_ns / 1e6:.2f} ms"
        )


# ========================================================================================
# One request's worth of a block
# ========================================================================================


@dataclass(frozen=True, slots=True)
class _Group:
    """One field's access points, one folded bit window's point, or the PLC clock's.

    A group is the unit the payload is laid out in: its points are consecutive on the
    wire, so its bytes are consecutive in the response and one ``struct`` format
    character covers the whole of it.
    """

    points: tuple[RandomPoint, ...]
    struct_code: str
    field: FieldPlan | None = None
    fold: BitFold | None = None
    clock: PlcClockSource | None = None
    """The caller's clock declaration, when this group is the PLC clock's point.

    The declaration itself rather than a ``bool``, because the width, the ``struct`` code
    and the decode of this group all come from it. A ``bool`` here was the whole of the
    defect: it said *that* there was a clock and left *what it is* to a constant.
    """

    @property
    def dword(self) -> bool:
        """Whether these are double-word access points, which the wire puts last."""
        return self.points[0].width is AccessWidth.DWORD

    @property
    def label(self) -> str:
        """What ``describe()`` and a budget refusal call this group."""
        if self.fold is not None:
            return f"bit window {self.fold.anchor_text}"
        if self.clock is not None:
            return self.clock.label
        return "?" if self.field is None else self.field.name


class _Reader:
    """One prebuilt ``0403`` and the decoder for exactly its own points.

    Everything here is set once, at bind. The only bytes that ever change afterwards are
    the two units of the 4E serial number, patched in place rather than rebuilding a
    frame that is otherwise identical every cycle.
    """

    __slots__ = (
        "_ascii",
        "_bits",
        "_bounds",
        "_buffer",
        "_codec",
        "_expected",
        "_image",
        "_plain",
        "_serial_span",
        "_strings",
        "_struct",
        "_wire",
        "clock_float",
        "clock_index",
        "dword_points",
        "groups",
        "request_frame",
        "subcommand",
        "summary",
        "word_points",
    )

    def __init__(self, groups: Sequence[_Group], *, plc: Plc, text: str) -> None:
        self.groups: tuple[_Group, ...] = (
            *(group for group in groups if not group.dword),
            *(group for group in groups if group.dword),
        )
        self.word_points: tuple[RandomPoint, ...] = tuple(
            point for group in self.groups if not group.dword for point in group.points
        )
        self.dword_points: tuple[RandomPoint, ...] = tuple(
            point for group in self.groups if group.dword for point in group.points
        )
        points = (*self.word_points, *self.dword_points)

        ctx = plc._ctx
        conn = plc._conn
        codec: Codec = ctx.codec
        self._codec = codec
        command = ReadRandom(points)
        command.validate(ctx)
        payload = command.checked_encode(ctx)
        self.subcommand: int = command.subcommand(ctx)
        body = request_body(
            codec,
            monitoring_timer=plc._monitoring_timer.units,
            command=_READ_RANDOM,
            subcommand=self.subcommand,
            payload=payload,
        )
        serial: int | None = 0 if conn.frame.carries_serial else None
        frame = conn.frame.build(
            route=conn.route,
            body=body,
            codec=codec,
            serial=serial,
            expect_body_len=command.body_len(ctx),
        )
        self.request_frame: bytes = frame
        self._buffer: bytearray | None = None if serial is None else bytearray(frame)
        self._wire: bytes | memoryview = (
            frame if self._buffer is None else memoryview(self._buffer)
        )
        width = codec.number_len(16)
        self._serial_span: tuple[int, int] = (width, 2 * width)
        self.summary: CommandSummary = CommandSummary(
            command=_READ_RANDOM,
            subcommand=self.subcommand,
            request_bytes=len(frame),
            text=text,
        )
        self._compile(points)

    # -- the decoder, compiled once -----------------------------------------

    def _compile(self, points: tuple[RandomPoint, ...]) -> None:
        codec = self._codec
        self._struct = struct.Struct(
            "<" + "".join(group.struct_code for group in self.groups)
        )
        widths: tuple[Literal[16, 32], ...] = tuple(
            point.width.bits for point in points
        )
        self._image = struct.Struct(
            "<" + "".join("H" if bits == 16 else "I" for bits in widths)
        )
        self._expected = sum(codec.number_len(bits) for bits in widths)
        offsets: list[tuple[int, Literal[16, 32]]] = []
        cursor = 0
        for bits in widths:
            offsets.append((cursor, bits))
            cursor += codec.number_len(bits)
        self._ascii: tuple[tuple[int, Literal[16, 32]], ...] = tuple(offsets)

        plain: list[tuple[int, str]] = []
        strings: list[tuple[int, str, str]] = []
        bits_at: list[tuple[int, str, int]] = []
        bounded: list[tuple[int, str, float | None, float | None, Bounds, str, str]] = []
        clock: int | None = None
        clock_float = False
        for index, group in enumerate(self.groups):
            if group.clock is not None:
                clock = index
                spec = group.clock.spec
                clock_float = spec.python == "float"
                if spec.bounds is not None:
                    bounded.append(
                        (
                            index,
                            group.clock.label,
                            spec.bounds.minimum,
                            spec.bounds.maximum,
                            spec.bounds,
                            str(_address(group.points[0])),
                            spec.struct_code,
                        )
                    )
            elif group.fold is not None:
                bits_at.extend((index, name, bit) for name, bit in group.fold.bits)
            elif group.field is None:  # pragma: no cover - a group is one of the three
                raise SlmpBlockLayoutError("a block group describes nothing")
            elif isinstance(group.field.spec, StringSpec):
                strings.append((index, group.field.name, group.field.spec.encoding))
            else:
                plain.append((index, group.field.name))
                limits = group.field.bounds
                if limits is not None:
                    bounded.append(
                        (
                            index,
                            group.field.name,
                            limits.minimum,
                            limits.maximum,
                            limits,
                            str(_address(group.points[0])),
                            group.struct_code,
                        )
                    )
        self._plain: tuple[tuple[int, str], ...] = tuple(plain)
        self._strings: tuple[tuple[int, str, str], ...] = tuple(strings)
        self._bits: tuple[tuple[int, str, int], ...] = tuple(bits_at)
        self._bounds: tuple[
            tuple[int, str, float | None, float | None, Bounds, str, str], ...
        ] = tuple(bounded)
        self.clock_index: int | None = clock
        self.clock_float: bool = clock_float
        wire_words = sum(point.words for point in points)
        if self._struct.size != 2 * wire_words:  # pragma: no cover - an invariant
            raise SlmpBlockLayoutError(
                f"the compiled decoder for {self.summary.text} covers "
                f"{self._struct.size} byte(s) and its access points carry "
                f"{2 * wire_words}. Refusing to decode a response against a layout that "
                f"does not describe it."
            )

    # -- the wire ------------------------------------------------------------

    @property
    def points(self) -> int:
        """Access points this request costs against the CPU's ceiling."""
        return len(self.word_points) + len(self.dword_points)

    def request(self, serial: int | None) -> bytes | memoryview:
        """The prebuilt frame, with the 4E serial patched into it in place.

        Nothing is rebuilt: on 3E this returns the same ``bytes`` object every cycle, and
        on 4E it writes two units into a buffer that is otherwise a constant. That is the
        whole of ``encode_ns`` for a bound plan, which is why the transaction record says
        ``prebuilt=True`` -- the number must never be compared with an assembled frame's.
        """
        buffer = self._buffer
        if serial is None or buffer is None:
            return self._wire
        start, end = self._serial_span
        buffer[start:end] = self._codec.number(serial, bits=16)
        return self._wire

    # -- the decoder ---------------------------------------------------------

    def unpack(self, payload: bytes, *, binary: bool) -> tuple[Any, ...]:
        """The response's values, one per group, in wire order.

        ASCII is read point by point and repacked into the byte image binary would have
        sent, then run through the same compiled ``Struct``. There is exactly one decoder
        for a block, so the two codings cannot drift; the extra pack costs a few
        microseconds on a coding whose own round trip is longer in every measurement we
        have.
        """
        expect_payload_len(payload, self._expected, what=self.summary.text)
        if binary:
            return self._struct.unpack_from(payload, 0)
        codec = self._codec
        return self._struct.unpack(
            self._image.pack(
                *(codec.read_number(payload, at, bits=bits) for at, bits in self._ascii)
            )
        )

    def fill(self, values: tuple[Any, ...], into: dict[str, Any]) -> None:
        """Turn one unpacked response into field values, by name.

        The bounds pass at the end is the only part of the hot path that can refuse a
        response the PLC answered ``0x0000`` to. It **allocates nothing**: the table it
        walks was built at bind, the two ends are stored beside the
        :class:`~aslmp.blocks.fields.Bounds` rather than read off it, and
        :func:`~aslmp.blocks.fields.outside` is a module-level function so that no bound
        method is created per field per cycle. A block that declares no bounds -- which
        is most blocks -- walks an empty tuple and pays a loop that does not run.
        """
        for index, name in self._plain:
            into[name] = values[index]
        for index, name, encoding in self._strings:
            into[name] = _text(values[index], name, encoding, self.summary.text)
        for index, name, bit in self._bits:
            into[name] = bool(values[index] >> bit & 1)
        for index, name, minimum, maximum, limits, where, code in self._bounds:
            value = values[index]
            if outside(value, minimum, maximum):
                raise implausible(
                    field=name,
                    bounds=limits,
                    value=value,
                    address=where,
                    registers=_registers(value, code),
                )

    def clock_count(self, value: float) -> int:
        """One decoded PLC-clock point as the integer count ``Transaction.plc_clock`` is.

        The value arrived decoded **as the caller declared it** -- an ``f32`` clock is a
        float here, not a bit pattern -- and a free-running counter is a count, so a float
        one is truncated toward zero. That truncation is documented rather than silent: a
        REAL scan counter incremented by ``1.0`` is integral at every value it takes until
        it passes 2**24 and stops incrementing at all, which is what a declared
        ``maximum`` is for.

        A non-finite clock has no count and is refused rather than turned into one.
        ``int(nan)`` raises ``ValueError`` and ``int(inf)`` raises ``OverflowError``,
        neither of which is in the DESIGN section 3.1 tree, so both become
        :class:`~aslmp.errors.SlmpPayloadShapeError`: the PLC answered 0x0000 and the
        registers are the registers it sent, but they are not a clock.
        """
        try:
            return int(value)
        except (ValueError, OverflowError) as exc:
            raise SlmpPayloadShapeError(
                f"{self.summary.text}: the PLC clock decoded to {value!r}, which is not "
                f"a count. The end code was 0x0000 and nothing was retried. A "
                f"non-finite value from a register pair usually means the declared type "
                f"is not the type the PLC program writes there; check it in GX Works3 "
                f"under Label -> Global Label and pass it as PlcClockSource(kind=...)."
            ) from exc


def _registers(value: float, struct_code: str) -> tuple[int, ...]:
    """The registers a decoded value came from, re-derived for the refusal that names them.

    The cold path only: a bound has already fired and the frame is about to be described
    to a person. Re-packing is exact rather than approximate -- the decoder is one
    ``struct`` over the whole response and this is the same format character for the same
    field, so these are the words the PLC actually sent, low word first
    (:data:`~aslmp.blocks.fields.WORD_ORDER_PROOF`).
    """
    raw = struct.pack(f"<{struct_code}", value)
    return tuple(
        int.from_bytes(raw[at : at + 2], "little") for at in range(0, len(raw), 2)
    )


def _text(value: object, name: str, encoding: str, what: str) -> str:
    """One string field's registers as characters, cut at the NUL word.

    Nothing here replaces an undecodable byte: a register that does not hold text in the
    declared encoding is a block declaration that does not match the PLC program, and
    ``errors="replace"`` would put question marks into a recipe name and report success.
    """
    if not isinstance(value, bytes):  # pragma: no cover - "Ns" always unpacks bytes
        raise SlmpPayloadShapeError(f"{what}: field {name} did not decode as registers.")
    try:
        return value.split(b"\x00", 1)[0].decode(encoding)
    except UnicodeDecodeError as exc:
        raise SlmpPayloadShapeError(
            f"{what}: field {name} holds {value.hex(' ')}, which is not {encoding} "
            f"text. The end code was 0x0000, so this is a block declaration that does "
            f"not match what the PLC program stores there, not a failure the CPU "
            f"reported. Nothing here substitutes replacement characters."
        ) from exc


# ========================================================================================
# The bound plans
# ========================================================================================


class _Bound(Generic[B]):
    """What a single-request plan and a split plan have in common."""

    __slots__ = (
        "_plc",
        "block",
        "bound_generation",
        "bound_model_code",
        "citations",
        "folds",
        "layout",
        "word_order",
    )

    def __init__(
        self,
        block: type[B],
        layout: BlockLayout,
        *,
        plc: Plc,
        folds: tuple[BitFold, ...],
        word_order: WordOrder,
        citations: tuple[Source, ...],
    ) -> None:
        self.block = block
        self.layout = layout
        self.folds = folds
        self.word_order = word_order
        self.citations = citations
        self._plc = plc
        self.bound_generation = plc.generation
        self.bound_model_code = plc.model_code

    @property
    def plc(self) -> Plc:
        """The client this plan is bound to. A plan is not portable between clients."""
        return self._plc

    # -- the guards ----------------------------------------------------------

    def _check_target(self) -> None:
        """Refuse to read into a CPU that is not the one we validated against (G6).

        A plan bound to a client whose handshake never identified the CPU carries no
        model code and therefore no guard; that is what ``Handshake.NONE`` costs, and it
        is said here rather than pretended away.
        """
        expected = self.bound_model_code
        if expected is None:
            return
        actual = self._plc.model_code
        if actual == expected:
            return
        now = "no identified CPU" if actual is None else f"0x{actual:04X}"
        raise SlmpTargetChangedError(
            f"{self.layout.block_name} was bound to model code 0x{expected:04X} at "
            f"generation {self.bound_generation} and this client now reports {now}. A "
            f"prebuilt 0403 carried into a different D-memory layout returns plausible "
            f"floats and end code 0x0000, and nothing in the protocol would ever tell "
            f"you. Bind the block again against the connection you have now.",
            expected_model_code=expected,
            actual_model_code=actual,
            bound_generation=self.bound_generation,
            diagnostics=Diagnostics(client=self._plc),
        )

    # -- the hot path --------------------------------------------------------

    async def _one(self, reader: _Reader, into: dict[str, Any]) -> Transaction:
        """One prebuilt transaction, decoded into ``into``."""
        plc = self._plc
        conn = plc._conn
        binary = plc.encoding is Encoding.BINARY
        async with conn.transaction(
            command=_READ_RANDOM, subcommand=reader.subcommand
        ) as txn:
            request = reader.request(txn.serial)
            record = bytes(request) if plc._capture_frames else reader.request_frame
            try:
                raw, _timing = await txn.exchange(
                    request,
                    conn.accumulator(txn.serial),
                    mutates=False,
                    prebuilt=True,
                )
            except SlmpError as exc:
                plc._enrich(exc, reader.summary, record)
                plc._record_failure(txn, reader.summary, reader.subcommand, record)
                raise
            if raw.end_code != 0:
                raise self._end_code(raw, reader, txn, record)
            values = reader.unpack(raw.payload, binary=binary)
            tx = plc._transaction(
                txn.decoded(),
                txn,
                reader.summary,
                reader.subcommand,
                raw=raw,
                request=record,
            )
            reader.fill(values, into)
            if reader.clock_index is not None:
                tx = dataclasses.replace(
                    tx, plc_clock=reader.clock_count(values[reader.clock_index])
                )
        plc._observe(tx)
        return tx

    def _end_code(
        self, raw: RawResponse, reader: _Reader, txn: Txn, request: bytes
    ) -> SlmpError:
        """The PLC answered, in its own words. Never a falsy value, always a raise."""
        plc = self._plc
        failed = plc._transaction(
            txn.timing.build(),
            txn,
            reader.summary,
            reader.subcommand,
            raw=raw,
            request=request,
        )
        return end_code_error(
            raw,
            request=reader.summary,
            tx=failed,
            client=plc,
            sent_frame=request if plc._capture_frames else None,
        )

    def _instance(self, values: dict[str, Any], tx: Transaction) -> B:
        values[_TX_FIELD] = tx
        # type[B] is a @plc_block class: frozen, slots, keyword-only, tx included.
        return self.block(**values)

    # -- reporting -----------------------------------------------------------

    def _header(self) -> list[str]:
        plc = self._plc
        model = "unidentified CPU" if plc.model is None else plc.model
        return [
            f"block {self.layout.block_name} bound to {plc.name} "
            f"({model}, {plc.profile.key})",
            f"  transport {plc.transport.value}  frame {plc.frame.value}  "
            f"coding {plc.encoding.value}  generation {self.bound_generation}",
        ]

    def _field_table(self) -> list[str]:
        """The field table, with each field's declared bounds beside it.

        The bounds column is printed because this report is the artifact a Mitsubishi
        engineer reads against GX Works3's ``Label -> Global Label`` view (graft G9). A
        range that disagrees with the process is the same question as a type that
        disagrees with the label, and both are answered from this one table. Fields that
        promise nothing say ``--`` rather than nothing, so an empty column is never
        mistaken for a bound of zero.
        """
        rows = [
            (
                name,
                label,
                where,
                words if words == "folded" else f"{words} word(s)",
                "--" if plan.bounds is None else str(plan.bounds),
            )
            for (name, label, where, words), plan in zip(
                self.layout.table(), self.layout.fields, strict=True
            )
        ]
        widths = [max(len(row[column]) for row in rows) for column in range(4)]
        out = ["  fields:"]
        out.extend(
            f"    {name:<{widths[0]}}  {label:<{widths[1]}}  "
            f"{where:<{widths[2]}}  {words:<{widths[3]}}  {bounds}"
            for name, label, where, words, bounds in rows
        )
        return out

    def _fold_lines(self) -> list[str]:
        if not self.folds:
            return ["  folds: none (this block declares no bit fields)"]
        return ["  folds:", *(f"    {fold}" for fold in self.folds)]

    def _budget_lines(self, used: int, word: int, dword: int) -> list[str]:
        plc = self._plc
        limit = plc.profile.limit(_READ_RANDOM, plc.encoding, Unit.WORD, plc._ctx.link)
        return [
            f"  points: {word} word + {dword} double-word = {used}",
            f"  budget: {limit.rule.describe()} "
            f"(exceeded -> 0x{limit.end_code_if_exceeded:04X}; "
            f"{limit.evidence.provenance.value}, {limit.evidence.source})",
        ]

    def _citation_lines(self) -> list[str]:
        """Every source behind this plan, with the sentence that made it matter.

        The reference alone is not the artifact: two of these are measurements on the
        same CPU and firmware, and printed bare they would be the same line twice.
        """
        out = ["  citations:"]
        for source in self.citations:
            out.append(f"    {source.reference}")
            if source.note:
                out.extend(
                    f"      {line}"
                    for line in textwrap.wrap(source.note, width=82)
                )
        return out


@final
class BlockPlan(_Bound[B]):
    """One block, one prebuilt ``0403``, one typed instance per cycle.

    Built by :func:`bind` and by nothing else: there is no constructor a caller can reach
    that skips the validation, because that is exactly what would make the unchecked path
    the convenient one.
    """

    __slots__ = ("_reader", "_writer")

    def __init__(
        self,
        block: type[B],
        layout: BlockLayout,
        *,
        plc: Plc,
        folds: tuple[BitFold, ...],
        word_order: WordOrder,
        citations: tuple[Source, ...],
        reader: _Reader,
        writer: _Writer,
    ) -> None:
        super().__init__(
            block,
            layout,
            plc=plc,
            folds=folds,
            word_order=word_order,
            citations=citations,
        )
        self._reader = reader
        self._writer = writer

    # -- what a plan is ------------------------------------------------------

    @property
    def request_frame(self) -> bytes:
        """The exact ``0403`` frame, built once at bind. On 4E the serial is patched."""
        return self._reader.request_frame

    @property
    def write_template(self) -> bytes | None:
        """The exact ``1402`` skeleton this block writes with, or ``None`` if it cannot.

        Prebuilt at bind with every value zero, which is what proves *at start-up* that
        the block is writable at all: the device gate, the whole-span range check and the
        weighted ``1402`` budget (``word x 12 + dword x 14 <= 1920``, measured on
        FX5U-32MT/DS fw 1.065) are all exercised building it. The values of an actual
        write are encoded through the client's one control flow rather than patched into
        this buffer, because a ``1402`` payload interleaves specifications and values and
        so has no contiguous value block to patch -- and a write is not the cycle-time
        path a control loop lives on.
        """
        return self._writer.template

    @property
    def write_refusal(self) -> str:
        """Why this block cannot be written in one frame, or ``""`` if it can."""
        return self._writer.refusal

    @property
    def subcommand(self) -> int:
        """The ``0403`` subcommand this plan sends: ``0000``, or ``0002`` under LONG."""
        return self._reader.subcommand

    @property
    def word_points(self) -> tuple[DeviceAddress, ...]:
        """The word access points, in wire order. A folded window appears as its anchor."""
        return tuple(_address(point) for point in self._reader.word_points)

    @property
    def dword_points(self) -> tuple[DeviceAddress, ...]:
        """The double-word access points, in wire order."""
        return tuple(_address(point) for point in self._reader.dword_points)

    @property
    def points(self) -> int:
        """Access points against the CPU's ``0403`` ceiling. Folded bits cost nothing."""
        return self._reader.points

    # -- the calls -----------------------------------------------------------

    async def read(self) -> B:
        """One ``0403``, one instance. The call shape (graft G16).

        Raises :class:`~aslmp.errors.SlmpTargetChangedError` before touching the socket
        if the CPU on the other end is no longer the one this plan was validated against.
        """
        self._check_target()
        values: dict[str, Any] = {}
        tx = await self._one(self._reader, values)
        return self._instance(values, tx)

    async def write(self, **fields: Any) -> None:
        """Write the named fields in one ``1402``. Runtime-validated, not statically.

        The one ``Any`` on this library's public surface, named as such in DESIGN.md
        section 7: every keyword is checked against the layout and every value against
        the field's declared width, but no type checker sees any of it.
        :meth:`write_block` is the typed alternative.
        """
        await self._writer.write(self, fields)

    async def write_block(self, value: B, /) -> None:
        """Write every field of ``value`` in one ``1402``. The fully typed alternative."""
        await self._writer.write(self, self._writer.explode(value))

    def describe(self) -> str:
        """The artifact you hand a Mitsubishi engineer (graft G9).

        The field table, the folds and the anchors they chose, the point budget with the
        evidence behind it, the prebuilt frame in hex, and the manual sections and
        measurements every one of those decisions rests on. Computed at runtime from the
        bound plan itself, so it cannot drift from what goes on the wire.
        """
        reader = self._reader
        lines = [
            *self._header(),
            *self._field_table(),
            *self._fold_lines(),
            *self._budget_lines(
                reader.points, len(reader.word_points), len(reader.dword_points)
            ),
            f"  subcommand: 0x{reader.subcommand:04X}",
            f"  request frame ({len(reader.request_frame)} bytes):",
            f"    {reader.request_frame.hex(' ')}",
        ]
        template = self._writer.template
        if template is None:
            lines.append(f"  write: no single template -- {self._writer.refusal}")
        else:
            lines.append(f"  write template ({len(template)} bytes):")
            lines.append(f"    {template.hex(' ')}")
        lines.extend(self._citation_lines())
        return "\n".join(lines)

    def __repr__(self) -> str:
        return (
            f"<BlockPlan {self.layout.block_name} {self.points} point(s) "
            f"on {self._plc.name}>"
        )


@final
class SplitBlockPlan(_Bound[B]):
    """A block too big for one ``0403``, read as several. Returns :class:`Split`.

    Only ``bind(..., allow_split=True)`` produces one, and it returns a **different
    type** so that the loss of atomicity is visible at the call site rather than
    described in a docstring.
    """

    __slots__ = ("_readers",)

    def __init__(
        self,
        block: type[B],
        layout: BlockLayout,
        *,
        plc: Plc,
        folds: tuple[BitFold, ...],
        word_order: WordOrder,
        citations: tuple[Source, ...],
        readers: tuple[_Reader, ...],
    ) -> None:
        super().__init__(
            block,
            layout,
            plc=plc,
            folds=folds,
            word_order=word_order,
            citations=citations,
        )
        self._readers = readers

    @property
    def request_frames(self) -> tuple[bytes, ...]:
        """One prebuilt frame per transaction, in the order they are sent."""
        return tuple(reader.request_frame for reader in self._readers)

    @property
    def points(self) -> int:
        """Access points across every request."""
        return sum(reader.points for reader in self._readers)

    @property
    def transactions(self) -> int:
        """How many ``0403``s one :meth:`read` costs."""
        return len(self._readers)

    async def read(self) -> Split[B]:
        """Several ``0403``s, one instance, and the span they were sampled over."""
        self._check_target()
        values: dict[str, Any] = {}
        records: list[Transaction] = []
        for reader in self._readers:
            records.append(await self._one(reader, values))
        first = records[0].timing.sent_at
        last = records[-1].timing.received_at
        span = 0 if last is None else last - first
        return Split(self._instance(values, records[-1]), tuple(records), span)

    def describe(self) -> str:
        """The same report as :meth:`BlockPlan.describe`, once per transaction."""
        lines = [
            *self._header(),
            f"  SPLIT across {len(self._readers)} transactions: the fields of one "
            f"instance are NOT one snapshot.",
            *self._field_table(),
            *self._fold_lines(),
        ]
        for number, reader in enumerate(self._readers, start=1):
            lines.append(
                f"  request {number}: {len(reader.word_points)} word + "
                f"{len(reader.dword_points)} double-word = {reader.points} point(s)"
            )
            lines.append(f"    {reader.request_frame.hex(' ')}")
        lines.extend(self._citation_lines())
        return "\n".join(lines)

    def __repr__(self) -> str:
        return (
            f"<SplitBlockPlan {self.layout.block_name} {self.points} point(s) in "
            f"{len(self._readers)} transactions on {self._plc.name}>"
        )


# ========================================================================================
# Writing
# ========================================================================================


class _Writer:
    """How a bound block turns field values into one ``1402``, or refuses to.

    A block whose fields are a mix of bit devices and registers cannot be written in one
    transaction: a single bit is written by ``1402`` in **bit** units and a register by
    ``1402`` in word units, and those are two subcommands. Sending both would silently
    give up the atomicity that declaring the fields in one block asked for, so this
    refuses and names the two calls to make instead.
    """

    __slots__ = ("bits", "fields", "refusal", "template")

    def __init__(
        self,
        fields: Mapping[str, tuple[DeviceAddress, FieldPlan]],
        *,
        bits: Mapping[str, DeviceAddress],
        template: bytes | None,
        refusal: str,
    ) -> None:
        self.fields = fields
        self.bits = bits
        self.template = template
        self.refusal = refusal

    def explode(self, value: object) -> dict[str, Any]:
        """Every declared field of ``value``, as the keyword arguments of a write."""
        return {name: getattr(value, name) for name in (*self.fields, *self.bits)}

    async def write(self, plan: BlockPlan[Any], fields: Mapping[str, Any]) -> None:
        """One ``1402`` for the named fields, or a refusal that names the alternative."""
        name_list = ", ".join((*self.fields, *self.bits))
        if not fields:
            raise SlmpBlockLayoutError(
                f"{plan.layout.block_name}.write() was given no fields. A 1402 with "
                f"nothing in it is answered 0xC052 (measured, FX5U-32MT/DS fw 1.065); "
                f"nothing here sends an empty write and reports success."
            )
        unknown = [n for n in fields if n not in self.fields and n not in self.bits]
        if unknown:
            raise SlmpBlockLayoutError(
                f"{plan.layout.block_name} has no field(s) {', '.join(unknown)}. Its "
                f"fields are: {name_list}."
            )
        named_bits = [n for n in fields if n in self.bits]
        named_words = [n for n in fields if n in self.fields]
        if named_bits and named_words:
            raise SlmpBlockLayoutError(
                f"{plan.layout.block_name}.write() was asked to write bit fields "
                f"({', '.join(named_bits)}) and register fields "
                f"({', '.join(named_words)}) in one call. A bit is written by 0x1402 in "
                f"bit units and a register by 0x1402 in word units: two subcommands, "
                f"therefore two transactions. Splitting them here would quietly give up "
                f"the atomicity that declaring them in one block asked for, so write "
                f"them in two explicit calls."
            )
        plc = plan.plc
        if named_bits:
            await plc._run(
                WriteRandomBits(
                    tuple(
                        BitWrite(self.bits[n], _as_bool(fields[n], n))
                        for n in named_bits
                    )
                ),
                mutates=True,
            )
            return
        writes: list[RandomWrite] = []
        for name in named_words:
            address, field = self.fields[name]
            _refuse_out_of_range(field, address, fields[name])
            writes.extend(_write_points(address, field, fields[name]))
        await plc._run(WriteRandom(tuple(writes)), mutates=True)


def _refuse_out_of_range(field: FieldPlan, address: DeviceAddress, value: object) -> None:
    """A value outside a field's declared bounds never reaches the PLC.

    Checked on the way out as well as on the way in, because a bound is a statement about
    what may be in that register and a write is the other way something gets there.
    Deliberately **not** inside :func:`_write_points`: that function also builds the
    all-zero write template at bind, and a field declared ``minimum=1.0`` would turn its
    own plausibility bound into "this block has no write template" -- a silent loss of
    the prebuilt path, reported as a refusal about something else entirely.

    Type refusals and width refusals are left to :func:`_in_width`, which says the right
    thing about them; this only judges numbers against a range the caller *declared*.
    """
    limits = field.bounds
    if limits is None or isinstance(value, bool) or not isinstance(value, int | float):
        return
    if outside(value, limits.minimum, limits.maximum):
        raise refuse_write(
            field=field.name,
            label=field.label,
            bounds=limits,
            value=value,
            address=str(address),
        )


def _as_bool(value: object, name: str) -> bool:
    if isinstance(value, bool):
        return value
    raise SlmpBlockLayoutError(
        f"field {name} is a Bit and takes True or False, not {type(value).__name__}. "
        f"Nothing here treats a non-empty value as on."
    )


def _write_points(
    address: DeviceAddress, field: FieldPlan, value: object
) -> list[RandomWrite]:
    """One field's value as the ``1402`` access points that carry it."""
    spec = field.spec
    if isinstance(spec, StringSpec):
        raw = _string_bytes(value, field.name, spec)
        return [
            RandomWrite(
                RandomPoint(_offset(address, index), AccessWidth.WORD, "u16"),
                int.from_bytes(raw[2 * index : 2 * index + 2], "little"),
            )
            for index in range(spec.words)
        ]
    if isinstance(spec, NumberSpec) and spec.words == 4:
        packed = struct.pack("<d", _in_width(spec, value, field.name))
        return [
            RandomWrite(
                RandomPoint(_offset(address, 2 * half), AccessWidth.DWORD, "u32"),
                int.from_bytes(packed[4 * half : 4 * half + 4], "little"),
            )
            for half in range(2)
        ]
    if isinstance(spec, NumberSpec):
        width = AccessWidth.DWORD if spec.dword else AccessWidth.WORD
        point = RandomPoint(address, width, spec.kind)
        return [RandomWrite(point, _in_width(spec, value, field.name))]
    raise SlmpBlockLayoutError(  # pragma: no cover - a BitSpec never reaches here
        f"field {field.name} is not a register field and cannot be written in word units"
    )


def _in_width(spec: NumberSpec, value: object, name: str) -> int | float:
    """One field's value, held to **its own declared type** before anything is built.

    The declaration is the contract in both directions. A field declared ``I16`` and
    given ``40000`` used to reach ``RandomWrite``, where the shared 16-bit helper
    accepted the union of the signed and unsigned ranges and masked -- so ``40000``
    became ``0x9C40`` and read back as ``-25536``, with end code ``0x0000`` at every
    step. A field declared ``F32`` and given ``1e39`` reached ``struct.pack`` and raised
    a bare ``OverflowError``, which is not in the DESIGN section 3.1 tree at all.

    Both are now :class:`~aslmp.errors.SlmpValueRangeError` before a byte is built, from
    the same two helpers every other write in this package uses. Declared
    :class:`~aslmp.blocks.fields.Bounds` are a *narrower* promise checked separately by
    :func:`_refuse_out_of_range`; this is the width the field cannot physically exceed,
    and it is checked even when the caller declared no bounds at all.
    """
    what = f"field {name} declared {spec.label}"
    if spec.python == "float":
        return real(_as_float(value, name), bits=_REAL_BITS[spec.struct_code], what=what)
    integer = _as_int(value, name)
    # For the range only: the *checked* value is the caller's own, so that the refusal a
    # bound plan prints and the RandomWrite a report shows both say -1 rather than 65535.
    unsigned(
        integer,
        bits=16 * min(spec.words, 2),
        what=what,
        signed_field=spec.struct_code in ("h", "i"),
    )
    return integer


_REAL_BITS: Final[Mapping[str, Literal[32, 64]]] = {"f": 32, "d": 64}
"""Which IEEE-754 width each float field's ``struct`` code is, for :func:`_in_width`."""


def _as_int(value: object, name: str) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    raise SlmpBlockLayoutError(f"field {name} takes an int, not {type(value).__name__}.")


def _as_float(value: object, name: str) -> float:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    raise SlmpBlockLayoutError(
        f"field {name} takes a real number, not {type(value).__name__}."
    )


def _string_bytes(value: object, name: str, spec: StringSpec) -> bytes:
    """A string field's registers, NUL-padded, refusing anything that does not fit.

    The non-``str`` case is refused here rather than by
    :func:`~aslmp.commands.base.encoded` only so that the message can name the field and
    its declared ``Str(...)`` width; the encode itself goes through that helper, because
    ``value.encode(spec.encoding)`` raises ``UnicodeEncodeError`` for a character the
    declared codec cannot carry and ``LookupError`` for a codec name that does not
    exist, and neither is in the DESIGN section 3.1 tree a caller catches.
    """
    if not isinstance(value, str):
        raise SlmpBlockLayoutError(
            f"field {name} is a Str({spec.length}) and takes a str, not "
            f"{type(value).__name__}."
        )
    raw = encoded(
        value, encoding=spec.encoding, what=f"field {name} declared Str({spec.length})"
    )
    if len(raw) > 2 * spec.words - 1:
        raise SlmpBlockLayoutError(
            f"field {name} holds {spec.length} characters plus a NUL terminator "
            f"({spec.words} register(s)), and {value!r} encodes to {len(raw)} byte(s) "
            f"in {spec.encoding}. Nothing here truncates a string to fit: a recipe name "
            f"silently cut in half is a different recipe written to the plant."
        )
    return raw.ljust(2 * spec.words, b"\x00")


def _offset(address: DeviceAddress, words: int) -> DeviceAddress:
    """``address`` moved ``words`` registers along, keeping the profile's radix."""
    return DeviceAddress.of(address.type, address.index + words, radix=address.radix)


def _address(point: RandomPoint) -> DeviceAddress:
    """A resolved point's address. Every point in a bound plan is resolved."""
    if isinstance(point.address, DeviceAddress):
        return point.address
    raise SlmpBlockLayoutError(  # pragma: no cover - bind resolves every point
        f"the access point {point} was never resolved against a profile"
    )


# ========================================================================================
# bind()
# ========================================================================================


@overload
def bind(
    plc: Plc,
    block: type[B],
    /,
    *,
    base: str | DeviceAddress | None = ...,
    allow_split: Literal[False] = ...,
) -> BlockPlan[B]: ...


@overload
def bind(
    plc: Plc,
    block: type[B],
    /,
    *,
    base: str | DeviceAddress | None = ...,
    allow_split: Literal[True],
) -> BlockPlan[B] | SplitBlockPlan[B]: ...


def bind(
    plc: Plc,
    block: type[B],
    /,
    *,
    base: str | DeviceAddress | None = None,
    allow_split: bool = False,
) -> BlockPlan[B] | SplitBlockPlan[B]:
    """Bind a ``@plc_block`` class to a connected client. **Synchronous: no I/O.**

    Resolves every address against the client's profile, validates every span including
    the folded bit windows, checks the point budget, chooses the subcommand family,
    prebuilds the ``0403`` frame and compiles one decoder. A failure here is a start-up
    failure with a paragraph attached, which is the whole reason this step is separate
    from :meth:`BlockPlan.read`.

    Over the ceiling with ``allow_split=False`` -- the default -- this raises
    :class:`~aslmp.errors.SlmpPointLimitError` naming the count, the limit, the evidence
    behind it and the fields that push it over. ``allow_split=True`` returns a different
    type instead (:class:`SplitBlockPlan`), because several requests are not one
    snapshot.
    """
    layout = layout_of(block)
    ctx = plc._ctx
    order = _word_order(layout, plc)
    resolved = _resolve(layout, ctx, base if base is not None else layout.base)
    folds = _fold(layout, ctx, resolved)
    groups = _groups(layout, ctx, resolved, folds, plc)
    citations = _citations(layout)
    text = f"{layout.block_name}.read()"
    try:
        reader = _Reader(groups, plc=plc, text=text)
    except SlmpPointLimitError as exc:
        if not allow_split:
            raise _over_budget(layout, groups, plc, exc) from exc
        readers = tuple(
            _Reader(chunk, plc=plc, text=f"{text} [{number}]")
            for number, chunk in enumerate(_chunks(groups, plc), start=1)
        )
        return SplitBlockPlan(
            block,
            layout,
            plc=plc,
            folds=folds,
            word_order=order,
            citations=citations,
            readers=readers,
        )
    return BlockPlan(
        block,
        layout,
        plc=plc,
        folds=folds,
        word_order=order,
        citations=citations,
        reader=reader,
        writer=_writer_for(layout, resolved, plc),
    )


def _citations(layout: BlockLayout) -> tuple[Source, ...]:
    """The provenance behind this particular block's decisions, for ``describe()``."""
    out: list[Source] = [*layout_cites()]
    if any(field.dword for field in layout.fields):
        out.append(WORD_ORDER_PROOF)
    if any(isinstance(field.spec, StringSpec) for field in layout.fields):
        out.append(STRING_WORDS)
    return tuple(out)


def _word_order(layout: BlockLayout, plc: Plc) -> WordOrder:
    """The block's word order, and a refusal rather than a knob that does nothing.

    Every multi-register numeric field in a block is a double-word access point, which is
    one native low-word-first value on this hardware
    (:data:`~aslmp.blocks.fields.WORD_ORDER_PROOF`). There is nothing for ``HIGH_FIRST``
    to reverse, so accepting it would mean accepting a parameter that silently does not
    apply.
    """
    order = layout.word_order if layout.word_order is not None else plc._ctx.word_order
    if order is WordOrder.LOW_FIRST:
        return order
    wide = [field.name for field in layout.fields if field.dword]
    if not wide:
        return order
    raise SlmpBlockLayoutError(
        f"{layout.block_name} declares {', '.join(wide)} as double-word access "
        f"point(s) and this block's word order is HIGH_FIRST. A 0403 double-word access "
        f"point is two consecutive registers low word first, natively, and its "
        f"binary-versus-ASCII field order is a property of the codec rather than of a "
        f"PLC program's convention -- so there is nothing here for HIGH_FIRST to "
        f"reverse and accepting it would be a parameter that does not apply. If the "
        f"program really stores those registers high word first, declare them as two "
        f"U16 fields and assemble the value where the convention is written down."
    )


def _resolve(
    layout: BlockLayout, ctx: EncodeContext, base: str | DeviceAddress | None
) -> dict[str, DeviceAddress]:
    """Every field's address, against this CPU's radix. The first thing bind does."""
    out: dict[str, DeviceAddress] = {}
    head = None if base is None else ctx.address(base)
    for field in layout.fields:
        if field.address is not None:
            out[field.name] = ctx.address(field.address)
            continue
        if head is None:  # pragma: no cover - compile_layout refuses this first
            raise SlmpBlockLayoutError(
                f"{layout.block_name}.{field.name} is addressed from a base and none "
                f"was given."
            )
        if head.type.unit is not Unit.WORD:
            raise SlmpBlockLayoutError(
                f"{layout.block_name} has fields addressed from base {head}, which is a "
                f"bit device. A base cursor counts registers, so it has to start on a "
                f"word device; address those fields individually with at(...)."
            )
        out[field.name] = _offset(head, field.word_offset or 0)
    _refuse_overlap(layout, out)
    return out


def _refuse_overlap(layout: BlockLayout, resolved: Mapping[str, DeviceAddress]) -> None:
    """Two fields may not cover one register. Bits are exempt: they share by design."""
    seen: dict[tuple[str, int], str] = {}
    for field in layout.fields:
        if isinstance(field.spec, BitSpec):
            continue
        address = resolved[field.name]
        for word in range(field.words):
            key = (address.type.name, address.index + word)
            other = seen.get(key)
            if other is not None:
                raise SlmpBlockLayoutError(
                    f"{layout.block_name}.{field.name} and {layout.block_name}.{other} "
                    f"both cover {key[0]}{key[1]}. Two fields over one register are read "
                    f"twice and written twice, and a write of one would silently change "
                    f"the other."
                )
            seen[key] = field.name


def _fold(
    layout: BlockLayout, ctx: EncodeContext, resolved: Mapping[str, DeviceAddress]
) -> tuple[BitFold, ...]:
    """Group the declared bits into 16-point windows, one device family at a time.

    The whole window is validated, not just the named bits: a word access point on a bit
    device reads all 16 (:data:`~aslmp.blocks.layout.BIT_WINDOW`), so a window whose tail
    runs off the end of the family is a request this CPU answers ``0xC056``.
    """
    families: dict[str, list[tuple[str, int]]] = {}
    for field in layout.fields:
        if not isinstance(field.spec, BitSpec):
            continue
        address = resolved[field.name]
        if address.type.unit is not Unit.BIT:
            raise SlmpBlockLayoutError(
                f"{layout.block_name}.{field.name} is a Bit field at {address}, which "
                f"is a word device. One word access point there is one register and not "
                f"16 bits; declare it U16 and select the bit from the value."
            )
        families.setdefault(address.type.name, []).append((field.name, address.index))
    folds: list[BitFold] = []
    for family, items in families.items():
        example = resolved[items[0][0]]
        last = ctx.profile.range_for(example.type).last
        for window in plan_folds(items):
            fitted = lower_anchor(window, device=family, last=last)
            anchor = DeviceAddress.of(
                example.type, fitted.anchor, radix=example.radix
            )
            folds.append(
                BitFold(
                    device=family,
                    anchor_index=fitted.anchor,
                    anchor_text=str(anchor),
                    bits=fitted.bits,
                    lowered=fitted.anchor != window.anchor,
                )
            )
    return tuple(folds)


def _groups(
    layout: BlockLayout,
    ctx: EncodeContext,
    resolved: Mapping[str, DeviceAddress],
    folds: tuple[BitFold, ...],
    plc: Plc,
) -> tuple[_Group, ...]:
    """The access points of every field, in declaration order.

    A folded window takes the position of the first bit field inside it, so the report,
    the wire and the class body a person wrote all agree.
    """
    by_name: dict[str, BitFold] = {
        name: fold for fold in folds for name in fold.names
    }
    emitted: set[int] = set()
    out: list[_Group] = []
    for field in layout.fields:
        if isinstance(field.spec, BitSpec):
            fold = by_name[field.name]
            if id(fold) in emitted:
                continue
            emitted.add(id(fold))
            example = resolved[field.name]
            anchor = DeviceAddress.of(
                example.type, fold.anchor_index, radix=example.radix
            )
            out.append(
                _Group(
                    points=(RandomPoint(anchor, AccessWidth.WORD, "bits"),),
                    struct_code="H",
                    fold=fold,
                )
            )
            continue
        address = resolved[field.name]
        spec = field.spec
        points: tuple[RandomPoint, ...]
        if isinstance(spec, StringSpec):
            points = tuple(
                RandomPoint(_offset(address, index), AccessWidth.WORD, "u16")
                for index in range(spec.words)
            )
        elif spec.words == 4:
            points = tuple(
                RandomPoint(_offset(address, 2 * half), AccessWidth.DWORD, "u32")
                for half in range(2)
            )
        else:
            width = AccessWidth.DWORD if spec.dword else AccessWidth.WORD
            points = (RandomPoint(address, width, spec.kind),)
        out.append(_Group(points=points, struct_code=spec.struct_code, field=field))
    clock = plc.plc_clock
    if clock is not None:
        # The width, the point kind and the struct code all come from the caller's own
        # declaration. They used to be the constants DWORD/"u32"/"I", which on a bench
        # whose D8 is a REAL published the float's bit pattern as a monotonic, plausible
        # and wrong counter with end code 0x0000 -- see PlcClockSource.
        spec = clock.spec
        out.append(
            _Group(
                points=(
                    RandomPoint(
                        ctx.address(clock.address),
                        AccessWidth.DWORD if spec.dword else AccessWidth.WORD,
                        spec.kind,
                    ),
                ),
                struct_code=spec.struct_code,
                clock=clock,
            )
        )
    return tuple(out)


def _chunks(groups: tuple[_Group, ...], plc: Plc) -> Iterator[tuple[_Group, ...]]:
    """Split the groups into requests that each fit the CPU's ``0403`` ceiling."""
    ceiling = plc._random_ceiling()
    current: list[_Group] = []
    used = 0
    for group in groups:
        cost = len(group.points)
        if cost > ceiling:
            raise SlmpPointLimitError(
                f"field {group.label} alone needs {cost} access point(s) and this CPU "
                f"allows {ceiling} per 0403. Splitting cannot help: one field read in "
                f"two halves is not one value."
            )
        if used + cost > ceiling:
            yield tuple(current)
            current = []
            used = 0
        current.append(group)
        used += cost
    if current:
        yield tuple(current)


def _over_budget(
    layout: BlockLayout, groups: tuple[_Group, ...], plc: Plc, exc: SlmpPointLimitError
) -> SlmpPointLimitError:
    """The over-the-ceiling refusal, naming the fields that push it over."""
    limit = plc.profile.limit(_READ_RANDOM, plc.encoding, Unit.WORD, plc._ctx.link)
    ceiling = getattr(limit.rule, "maximum", None)
    total = sum(len(group.points) for group in groups)
    over: list[str] = []
    if isinstance(ceiling, int):
        used = 0
        for group in groups:
            used += len(group.points)
            if used > ceiling:
                over.append(group.label)
    tail = f" The fields past the ceiling are: {', '.join(over)}." if over else ""
    return SlmpPointLimitError(
        f"{layout.block_name} needs {total} access point(s) and {plc.profile.key} "
        f"allows {limit.rule.describe()} for 0x0403 in {plc.encoding.value} "
        f"({limit.evidence.provenance.value}: {limit.evidence.source}); exceeding it is "
        f"answered 0x{limit.end_code_if_exceeded:04X}.{tail} Nothing here splits the "
        f"request on your behalf, because several requests are several snapshots: pass "
        f"allow_split=True for a SplitBlockPlan, whose read() returns a Split[B] rather "
        f"than a B, or declare a smaller block. ({exc})"
    )


def _writer_for(
    layout: BlockLayout, resolved: Mapping[str, DeviceAddress], plc: Plc
) -> _Writer:
    """The write half, prebuilt and proved at bind -- or the reason there is none."""
    fields: dict[str, tuple[DeviceAddress, FieldPlan]] = {}
    bits: dict[str, DeviceAddress] = {}
    for field in layout.fields:
        if isinstance(field.spec, BitSpec):
            bits[field.name] = resolved[field.name]
        else:
            fields[field.name] = (resolved[field.name], field)
    if fields and bits:
        return _Writer(
            fields,
            bits=bits,
            template=None,
            refusal=(
                f"{layout.block_name} declares both bit fields ({', '.join(bits)}) and "
                f"register fields ({', '.join(fields)}), and one 1402 is either in bit "
                f"units or in word units. write() takes one group or the other; "
                f"write_block() would have to send two transactions and will not."
            ),
        )
    try:
        template = _template(fields, bits, plc)
    except SlmpUsageError as exc:
        return _Writer(fields, bits=bits, template=None, refusal=exc.headline())
    return _Writer(fields, bits=bits, template=template, refusal="")


def _template(
    fields: Mapping[str, tuple[DeviceAddress, FieldPlan]],
    bits: Mapping[str, DeviceAddress],
    plc: Plc,
) -> bytes:
    """A ``1402`` frame for this block with every value zero.

    Building it *is* the validation: the device gate, the whole-span range check and the
    weighted budget all run here, at bind, so a block that cannot be written says so at
    start-up rather than the first time a setpoint changes.
    """
    ctx = plc._ctx
    command: WriteRandom | WriteRandomBits
    if bits:
        command = WriteRandomBits(
            tuple(BitWrite(address, False) for address in bits.values())
        )
    else:
        writes: list[RandomWrite] = []
        for address, field in fields.values():
            zero: object = "" if isinstance(field.spec, StringSpec) else 0
            writes.extend(_write_points(address, field, zero))
        command = WriteRandom(tuple(writes))
    command.validate(ctx)
    payload = command.checked_encode(ctx)
    conn = plc._conn
    body = request_body(
        conn.codec,
        monitoring_timer=plc._monitoring_timer.units,
        command=_WRITE_RANDOM,
        subcommand=command.subcommand(ctx),
        payload=payload,
    )
    return conn.frame.build(
        route=conn.route,
        body=body,
        codec=conn.codec,
        serial=0 if conn.frame.carries_serial else None,
        expect_body_len=command.body_len(ctx),
    )
