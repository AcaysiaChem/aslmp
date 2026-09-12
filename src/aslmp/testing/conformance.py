"""The conformance suite: the same questions, asked of a simulator or of real iron.

Layer 2.5 (``aslmp.testing``). Imports L0-L2 only.

Every case is a request this library can build and an expectation drawn from the
**target** -- so running the suite against :data:`~aslmp.testing.targets.PEDANTIC` and
against :data:`~aslmp.testing.targets.FX5U_32MT_DS` and diffing the two reports is the
empirical form of the same document :func:`~aslmp.testing.targets.diff_targets` renders
declaratively. A test asserts the two agree.

**The suite is its own client.** It builds frames from :mod:`aslmp.wire` and reads
responses length-first with about ten lines of its own socket handling, rather than
through ``aslmp.transport``. That is not duplication for its own sake: a suite that used
the transport under test could not fail when the transport was broken, and the whole
reason ``aslmp.testing`` may not import layer 3 is that this suite has to be able to.

**Safe on real iron by construction.** A case that changes anything carries
``mutates=True`` and is skipped unless ``allow_writes=True`` is passed explicitly; every
address a mutating case touches comes from the scratch range the caller names, and the
default is the bench's own ``D100``/``M100``. Nothing here ever emits ``0x1001``,
``0x1002``, ``0x1003``, ``0x1005`` or ``0x1006``: remote control is not a conformance
question you ask of a machine that is running.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, Literal, Protocol

from aslmp._clock import DEFAULT_CLOCK
from aslmp.commands.base import EncodeContext
from aslmp.commands.batch import ReadWords, WriteBits, WriteWords
from aslmp.commands.info import ReadTypeName, SelfTest
from aslmp.commands.random import ReadRandom, dword, word
from aslmp.profile import Encoding, Link
from aslmp.testing.server import codec_for
from aslmp.wire.codec import SpecFormat
from aslmp.wire.devicetable import DEVICE_TABLE
from aslmp.wire.frames import FRAMES, FrameType, request_body
from aslmp.wire.raw import RawResponse, SlmpFrameError
from aslmp.wire.route import Route

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable, Sequence

    from aslmp.testing.targets import SimulatorTarget
    from aslmp.wire.citations import Source
    from aslmp.wire.codec import Codec
    from aslmp.wire.frames import FrameFormat

__all__ = [
    "STANDARD_TAGS",
    "Abnormal",
    "CaseResult",
    "ConformanceCase",
    "ConformanceReport",
    "Exchange",
    "Expectation",
    "NoResponse",
    "Normal",
    "TcpExchange",
    "UdpExchange",
    "context_for",
    "run_conformance",
    "standard_cases",
]


RequestParts = tuple[int, int, bytes]
"""``(command, subcommand, request data)`` -- what a case puts on the wire."""


# ----------------------------------------------------------------------------------------
# Expectations
# ----------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Normal:
    """End code ``0x0000``, and optionally an exact response payload."""

    payload: bytes | None = None
    note: str = ""

    def __str__(self) -> str:
        return "end code 0x0000" + (" with an exact payload" if self.payload else "")


@dataclass(frozen=True, slots=True)
class Abnormal:
    """One of a set of end codes.

    A **set** because the honest expectation is sometimes plural: the manuals and the
    silicon disagree in three places, and a suite run against an unfamiliar CPU should
    record which of the two it answered rather than fail for having met the other one.
    Against a declared target the set is a singleton, taken from that target's policy.
    """

    codes: frozenset[int]
    note: str = ""

    def __post_init__(self) -> None:
        codes = frozenset(self.codes)
        if not codes:
            raise ValueError("an Abnormal expectation needs at least one end code")
        if 0 in codes:
            raise ValueError("0x0000 is normal completion; use Normal for it")
        object.__setattr__(self, "codes", codes)

    def __str__(self) -> str:
        return "end code " + " or ".join(f"0x{code:04X}" for code in sorted(self.codes))


@dataclass(frozen=True, slots=True)
class NoResponse:
    """No response at all, within the deadline. A measured outcome, not a failure.

    Three provocations produce it on an FX5U-32MT/DS: an ASCII request on a binary
    entry, a frame type the entry is not configured for, and an overstated data length.
    A fourth is documented rather than measured: a successful Remote Reset.
    """

    note: str = ""

    def __str__(self) -> str:
        return "no response"


Expectation = Normal | Abnormal | NoResponse


STANDARD_TAGS: Final = (
    "identity",
    "self-test",
    "batch",
    "random",
    "limits",
    "ranges",
    "framing",
    "devices",
)
"""Every tag the shipped suite uses, so a caller can select a subset by name."""


@dataclass(frozen=True, slots=True)
class ConformanceCase:
    """One question, its expected answer, and where the expectation comes from."""

    key: str
    title: str
    build: Callable[[EncodeContext], RequestParts]
    expect: Expectation
    source: Source
    tags: frozenset[str] = frozenset()
    mutates: bool = False
    declared_delta: int = 0
    """Corrupt the declared data length by this many wire units before sending.

    The one thing a case cannot express through a command object, and the one whose two
    directions are asymmetric: understating ``L`` by two returned ``0xC061`` and the
    connection recovered, while **overstating** it by two produced no response at all --
    the CPU blocked waiting for bytes that never came (FX5U-32MT/DS fw 1.065, measured
    2026-09-06).
    """

    def __str__(self) -> str:
        return f"{self.key}: {self.title}"


@dataclass(frozen=True, slots=True)
class CaseResult:
    """What happened when one case was asked."""

    case: ConformanceCase
    status: Literal["pass", "fail", "skip", "error"]
    detail: str
    end_code: int | None = None
    elapsed_ns: int = 0
    request: bytes = b""
    response: bytes = b""

    @property
    def ok(self) -> bool:
        """Whether this case did not fail. A skip is not a pass and is counted apart."""
        return self.status in ("pass", "skip")

    def __str__(self) -> str:
        return f"[{self.status.upper()}] {self.case.key}: {self.detail}"


@dataclass(frozen=True, slots=True)
class ConformanceReport:
    """Every result, and the two ways to consume them: a table and an assertion."""

    target_label: str
    verified: bool
    results: tuple[CaseResult, ...] = field(default_factory=tuple)

    def of(self, status: str) -> tuple[CaseResult, ...]:
        """Just the results with one status."""
        return tuple(result for result in self.results if result.status == status)

    @property
    def failures(self) -> tuple[CaseResult, ...]:
        """Everything that failed or errored."""
        return tuple(result for result in self.results if not result.ok)

    @property
    def end_codes(self) -> dict[str, int | None]:
        """The end code each case produced, keyed by case. This is what a diff compares."""
        return {result.case.key: result.end_code for result in self.results}

    def assert_clean(self) -> None:
        """Raise unless every case passed or was skipped."""
        failures = self.failures
        if not failures:
            return
        lines = "\n  ".join(str(result) for result in failures)
        raise AssertionError(
            f"{len(failures)} conformance case(s) failed against {self.target_label}:\n"
            f"  {lines}"
        )

    def to_markdown(self) -> str:
        """The table a reviewer reads."""
        head = (
            f"# Conformance: {self.target_label}\n\n"
            f"{len(self.of('pass'))} passed, {len(self.of('fail'))} failed, "
            f"{len(self.of('skip'))} skipped, {len(self.of('error'))} errored.\n\n"
        )
        if not self.verified:
            head += (
                "> This target is UNVERIFIED: every expectation in it is a reading of a "
                "manual, not an observation.\n\n"
            )
        head += "| case | status | end code | detail |\n| --- | --- | --- | --- |\n"
        rows = "".join(
            f"| `{result.case.key}` | {result.status} | "
            f"{'--' if result.end_code is None else f'0x{result.end_code:04X}'} | "
            f"{result.detail} |\n"
            for result in self.results
        )
        return head + rows

    def __str__(self) -> str:
        return self.to_markdown()


# ----------------------------------------------------------------------------------------
# The transport a suite is handed
# ----------------------------------------------------------------------------------------


class Exchange(Protocol):
    """Send one request frame, return one response frame, or ``None`` for silence.

    Structural on purpose: a caller can point the suite at a live PLC with twelve lines
    of their own socket code and no ``aslmp`` transport anywhere near it.
    """

    async def __call__(self, request: bytes, /) -> bytes | None:
        """One transaction. ``None`` means the deadline passed with no response."""
        ...


class _StreamExchange:
    """Shared response reading: fixed prefix, then exactly ``L`` more units."""

    __slots__ = ("_codec", "_frame", "_timeout")

    def __init__(self, frame: FrameFormat, codec: Codec, timeout: float) -> None:
        self._frame = frame
        self._codec = codec
        self._timeout = timeout

    def _wanted(self, prefix: bytes) -> int:
        declared = self._codec.read_number(
            prefix,
            self._frame.subheader_units(self._codec) + Route.wire_len(self._codec),
            bits=16,
        )
        return int(declared)


class TcpExchange(_StreamExchange):
    """One TCP connection, one transaction at a time. The safe shape, by construction.

    There is no way to write two requests before reading the first response with this
    object, because there is no ``send()``: that is the same structural argument the
    client makes, arrived at for the same measured reason. A test that wants the
    coalescing corruption has to reach for a raw socket and mean it.
    """

    __slots__ = ("_reader", "_writer")

    def __init__(self, frame: FrameFormat, codec: Codec, *, timeout: float = 3.0) -> None:
        super().__init__(frame, codec, timeout)
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    @classmethod
    async def connect(
        cls,
        host: str,
        port: int,
        *,
        frame: FrameFormat,
        codec: Codec,
        timeout: float = 3.0,
    ) -> TcpExchange:
        """Open a connection and return an exchange bound to it."""
        exchange = cls(frame, codec, timeout=timeout)
        exchange._reader, exchange._writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout
        )
        return exchange

    async def __call__(self, request: bytes, /) -> bytes | None:
        if self._reader is None or self._writer is None:
            raise RuntimeError("this exchange is not connected; use TcpExchange.connect()")
        self._writer.write(request)
        await self._writer.drain()
        try:
            prefix = await asyncio.wait_for(
                self._reader.readexactly(self._frame.prefix_units(self._codec)), self._timeout
            )
            body = await asyncio.wait_for(
                self._reader.readexactly(self._wanted(prefix)), self._timeout
            )
        except (TimeoutError, asyncio.IncompleteReadError):
            return None
        return prefix + body

    async def aclose(self) -> None:
        """Close the connection and wait for the peer to see the FIN.

        Waiting matters against a one-connection entry: the slot frees when the CPU
        notices the close, and a test that reconnects immediately otherwise races the
        accept-then-FIN it was not asking for.
        """
        writer = self._writer
        self._writer = None
        self._reader = None
        if writer is not None:
            writer.close()
            with contextlib.suppress(ConnectionError, OSError):
                await writer.wait_closed()


class UdpExchange(_StreamExchange):
    """One datagram out, one datagram in. A datagram is exactly one SLMP message.

    UDP does **not** suffer the TCP coalescing corruption -- the same two-requests-with-
    no-read-between test returns both responses correctly (measured on FX5U-32MT/DS fw
    1.065, 2026-09-06) -- but this object still asks one question at a time, because a
    conformance case whose answer might belong to the previous case is not a test.
    """

    __slots__ = ("_protocol", "_transport")

    def __init__(self, frame: FrameFormat, codec: Codec, *, timeout: float = 3.0) -> None:
        super().__init__(frame, codec, timeout)
        self._transport: asyncio.DatagramTransport | None = None
        self._protocol: _DatagramClient | None = None

    @classmethod
    async def connect(
        cls,
        host: str,
        port: int,
        *,
        frame: FrameFormat,
        codec: Codec,
        timeout: float = 3.0,
    ) -> UdpExchange:
        """Bind a socket connected to ``(host, port)`` and return an exchange."""
        exchange = cls(frame, codec, timeout=timeout)
        loop = asyncio.get_running_loop()
        exchange._transport, exchange._protocol = await loop.create_datagram_endpoint(
            _DatagramClient, remote_addr=(host, port)
        )
        return exchange

    async def __call__(self, request: bytes, /) -> bytes | None:
        if self._transport is None or self._protocol is None:
            raise RuntimeError("this exchange is not connected; use UdpExchange.connect()")
        self._transport.sendto(request)
        try:
            return await asyncio.wait_for(self._protocol.queue.get(), self._timeout)
        except TimeoutError:
            return None

    async def aclose(self) -> None:
        """Close the socket."""
        if self._transport is not None:
            self._transport.close()
            self._transport = None
            self._protocol = None


class _DatagramClient(asyncio.DatagramProtocol):
    """Every datagram, in arrival order. Never reordered and never merged."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[bytes] = asyncio.Queue()

    def datagram_received(self, data: bytes, addr: object) -> None:
        del addr
        self.queue.put_nowait(data)


# ----------------------------------------------------------------------------------------
# Building and running
# ----------------------------------------------------------------------------------------


def context_for(
    target: SimulatorTarget,
    *,
    encoding: Encoding = Encoding.BINARY,
    spec: SpecFormat = SpecFormat.SHORT,
    link: Link = Link.CPU_BUILTIN,
) -> EncodeContext:
    """The encoding context the suite builds its frames in.

    It uses the target's **profile**, because that is what turns ``"Y20"`` into a wire
    index, and the whole point of pointing the suite at a real CPU is to find out whether
    the profile is right about it.
    """
    return EncodeContext(
        codec=codec_for(encoding),
        spec=spec,
        profile=target.profile,
        encoding=encoding,
        link=link,
    )


def _frame_bytes(
    ctx: EncodeContext,
    frame: FrameFormat,
    parts: RequestParts,
    *,
    route: Route,
    serial: int | None,
    declared_delta: int,
) -> bytes:
    command, subcommand, payload = parts
    body = request_body(
        ctx.codec,
        monitoring_timer=0,
        command=command,
        subcommand=subcommand,
        payload=payload,
    )
    raw = frame.build(route=route, body=body, codec=ctx.codec, serial=serial)
    if not declared_delta:
        return raw
    at = frame.subheader_units(ctx.codec) + Route.wire_len(ctx.codec)
    width = ctx.codec.number_len(16)
    corrupted = ctx.codec.number(len(body) + declared_delta, bits=16)
    return raw[:at] + corrupted + raw[at + width :]


async def run_conformance(
    exchange: Exchange,
    target: SimulatorTarget,
    *,
    cases: Sequence[ConformanceCase] | None = None,
    encoding: Encoding = Encoding.BINARY,
    frame: FrameType = FrameType.THREE_E,
    route: Route = Route.OWN_STATION,
    allow_writes: bool = False,
    tags: frozenset[str] | None = None,
) -> ConformanceReport:
    """Ask every case and report what came back.

    ``allow_writes`` gates every case that changes anything. It has no default of
    ``True`` anywhere and never will: this function is meant to be pointed at a machine
    that is running.
    """
    ctx = context_for(target, encoding=encoding)
    frame_format = FRAMES[frame]
    selected = standard_cases(target) if cases is None else tuple(cases)
    results: list[CaseResult] = []
    serial = 0
    for case in selected:
        if tags is not None and not (case.tags & tags):
            results.append(
                CaseResult(case, "skip", f"not in the selected tags {sorted(tags)}")
            )
            continue
        if case.mutates and not allow_writes:
            results.append(
                CaseResult(
                    case,
                    "skip",
                    "this case writes; pass allow_writes=True and point it at scratch "
                    "registers you are willing to lose",
                )
            )
            continue
        serial = (serial + 1) & 0xFFFF
        results.append(
            await _run_case(
                exchange,
                case,
                ctx,
                frame_format,
                route=route,
                serial=serial if frame_format.carries_serial else None,
            )
        )
    return ConformanceReport(
        target_label=target.label, verified=target.verified, results=tuple(results)
    )


async def _run_case(
    exchange: Exchange,
    case: ConformanceCase,
    ctx: EncodeContext,
    frame: FrameFormat,
    *,
    route: Route,
    serial: int | None,
) -> CaseResult:
    try:
        parts = case.build(ctx)
        request = _frame_bytes(
            ctx,
            frame,
            parts,
            route=route,
            serial=serial,
            declared_delta=case.declared_delta,
        )
    except Exception as exc:  # a case that cannot even be built is a case failure
        return CaseResult(case, "error", f"the request could not be built: {exc!r}")
    started = DEFAULT_CLOCK()
    raw = await exchange(request)
    elapsed = DEFAULT_CLOCK() - started
    if raw is None:
        if isinstance(case.expect, NoResponse):
            return CaseResult(case, "pass", "no response, as expected", None, elapsed, request)
        return CaseResult(
            case,
            "fail",
            f"expected {case.expect}, got no response at all. On this hardware silence "
            f"means a coding mismatch, a frame-type mismatch or an overstated data "
            f"length -- all three are measured and all three look identical from here.",
            None,
            elapsed,
            request,
        )
    if isinstance(case.expect, NoResponse):
        return CaseResult(
            case,
            "fail",
            f"expected no response and {len(raw)} byte(s) arrived",
            None,
            elapsed,
            request,
            raw,
        )
    try:
        response: RawResponse = frame.parse(raw, ctx.codec, expect_serial=serial)
    except SlmpFrameError as exc:
        return CaseResult(
            case,
            "fail",
            f"the response does not parse: {exc}",
            None,
            elapsed,
            request,
            raw,
        )
    return _judge(case, response, elapsed, request, raw)


def _judge(
    case: ConformanceCase,
    response: RawResponse,
    elapsed: int,
    request: bytes,
    raw: bytes,
) -> CaseResult:
    expect = case.expect
    if isinstance(expect, Abnormal):
        if response.end_code in expect.codes:
            return CaseResult(
                case,
                "pass",
                f"0x{response.end_code:04X}",
                response.end_code,
                elapsed,
                request,
                raw,
            )
        return CaseResult(
            case,
            "fail",
            f"expected {expect}, got 0x{response.end_code:04X}",
            response.end_code,
            elapsed,
            request,
            raw,
        )
    if not isinstance(expect, Normal):  # pragma: no cover - NoResponse is handled above
        return CaseResult(case, "error", f"unreachable expectation {expect}", None, elapsed)
    if response.end_code != 0:
        return CaseResult(
            case,
            "fail",
            f"expected {expect}, got 0x{response.end_code:04X}",
            response.end_code,
            elapsed,
            request,
            raw,
        )
    if expect.payload is not None and response.payload != expect.payload:
        return CaseResult(
            case,
            "fail",
            f"payload {response.payload.hex(' ').upper()} is not the expected "
            f"{expect.payload.hex(' ').upper()}",
            0,
            elapsed,
            request,
            raw,
        )
    return CaseResult(case, "pass", "0x0000", 0, elapsed, request, raw)


# ----------------------------------------------------------------------------------------
# The shipped cases
# ----------------------------------------------------------------------------------------


def _parts(command: int, subcommand: int, payload: bytes) -> RequestParts:
    return command, subcommand, payload


def standard_cases(
    target: SimulatorTarget,
    *,
    scratch_word: str = "D100",
    scratch_bit: str = "M100",
) -> tuple[ConformanceCase, ...]:
    """The shipped suite, with every expectation taken from ``target``.

    ``scratch_word`` and ``scratch_bit`` are the registers a mutating case is allowed to
    touch, and they default to the bench's own scratch range. Nothing outside them is
    written by any case here.
    """
    codes = target.end_codes
    limits = target.limits
    head = _head_index(scratch_word)
    data_range = target.profile.range_for(DEVICE_TABLE["D"])
    past_end = (data_range.last or 0) + 1

    def read_words(address: str, count: int) -> Callable[[EncodeContext], RequestParts]:
        def build(ctx: EncodeContext) -> RequestParts:
            command = ReadWords(address=address, count=count)
            return _parts(command.CODE, command.subcommand(ctx), command.encode(ctx))

        return build

    def self_test(ctx: EncodeContext) -> RequestParts:
        command = SelfTest(b"ABCD")
        return _parts(command.CODE, command.subcommand(ctx), command.encode(ctx))

    def read_type_name(ctx: EncodeContext) -> RequestParts:
        command = ReadTypeName()
        return _parts(command.CODE, command.subcommand(ctx), command.encode(ctx))

    def read_random(ctx: EncodeContext) -> RequestParts:
        command = ReadRandom((word(scratch_word), dword(f"D{head + 2}", kind="f32")))
        return _parts(command.CODE, command.subcommand(ctx), command.encode(ctx))

    def zero_points(ctx: EncodeContext) -> RequestParts:
        return _parts(
            0x0401,
            0x0000,
            ctx.device_spec(ctx.address(scratch_word)) + ctx.codec.number(0, bits=16),
        )

    def over_word_ceiling(ctx: EncodeContext) -> RequestParts:
        return _parts(
            0x0401,
            0x0000,
            ctx.device_spec(ctx.address("D0"))
            + ctx.codec.number(limits.batch_word_for(ctx.encoding) + 1, bits=16),
        )

    def past_range(ctx: EncodeContext) -> RequestParts:
        return _parts(
            0x0401,
            0x0000,
            ctx.device_spec(ctx.address(f"D{past_end - 1}")) + ctx.codec.number(2, bits=16),
        )

    def unknown_command(ctx: EncodeContext) -> RequestParts:
        del ctx
        return _parts(0x9999, 0x0000, b"")

    def write_scratch(ctx: EncodeContext) -> RequestParts:
        command = WriteWords(address=scratch_word, values=(0x1234,))
        return _parts(command.CODE, command.subcommand(ctx), command.encode(ctx))

    def write_scratch_bit(ctx: EncodeContext) -> RequestParts:
        command = WriteBits(address=scratch_bit, values=(True,))
        return _parts(command.CODE, command.subcommand(ctx), command.encode(ctx))

    def bad_device_code(ctx: EncodeContext) -> RequestParts:
        if ctx.codec.name != "binary":
            return _parts(
                0x0401,
                0x0000,
                b"ZZ" + b"000000" + ctx.codec.number(1, bits=16),
            )
        return _parts(
            0x0401,
            0x0000,
            b"\x00\x00\x00\xff" + ctx.codec.number(1, bits=16),
        )

    cases = (
        ConformanceCase(
            key="self_test_echo",
            title="0619 Self Test echoes the payload byte for byte",
            build=self_test,
            expect=Normal(note="the echo is compared exactly by the client"),
            source=target.evidence,
            tags=frozenset({"self-test"}),
        ),
        ConformanceCase(
            key="read_type_name",
            title="0101 Read Type Name returns 16 characters and a model code",
            build=read_type_name,
            expect=Normal(),
            source=target.evidence,
            tags=frozenset({"identity"}),
        ),
        ConformanceCase(
            key="batch_read_two_words",
            title=f"0401 reads two words from {scratch_word}",
            build=read_words(scratch_word, 2),
            expect=Normal(),
            source=target.evidence,
            tags=frozenset({"batch"}),
        ),
        ConformanceCase(
            key="batch_read_zero_points",
            title="0401 with a zero point count is a point-count error, not an address error",
            build=zero_points,
            expect=Abnormal(
                frozenset({codes.zero_point_count}),
                note=(
                    "0xC052 measured on FX5U-32MT/DS fw 1.065; the generic documentation "
                    "predicts an address error"
                ),
            ),
            source=target.evidence,
            tags=frozenset({"limits"}),
        ),
        ConformanceCase(
            key="batch_read_over_ceiling",
            title="0401 one point past this CPU's word ceiling",
            build=over_word_ceiling,
            expect=Abnormal(frozenset({codes.batch_word_limit})),
            source=target.evidence,
            tags=frozenset({"limits"}),
        ),
        ConformanceCase(
            key="batch_read_straddles_range_end",
            title="0401 for two words at the last valid D: the SPAN is validated",
            build=past_range,
            expect=Abnormal(frozenset({codes.address_out_of_range})),
            source=target.evidence,
            tags=frozenset({"ranges"}),
        ),
        ConformanceCase(
            key="unknown_command",
            title="an unimplemented command is refused with the command echoed",
            build=unknown_command,
            expect=Abnormal(frozenset({codes.unsupported_command})),
            source=target.evidence,
            tags=frozenset({"framing"}),
        ),
        ConformanceCase(
            key="bad_device_code",
            title="a device code in no table is refused",
            build=bad_device_code,
            expect=Abnormal(frozenset({codes.unknown_device_code})),
            source=target.evidence,
            tags=frozenset({"devices"}),
        ),
        ConformanceCase(
            key="read_random_word_and_dword",
            title="0403 carries a word point and a double-word point in one snapshot",
            build=read_random,
            expect=Normal(),
            source=target.evidence,
            tags=frozenset({"random"}),
        ),
        ConformanceCase(
            key="understated_length",
            title="a data length understated by two is answered and the connection recovers",
            build=read_words(scratch_word, 2),
            expect=Abnormal(frozenset({codes.request_length_mismatch})),
            source=target.evidence,
            tags=frozenset({"framing"}),
            declared_delta=-2,
        ),
        ConformanceCase(
            key="write_scratch_word",
            title=f"1401 writes one word to {scratch_word} and reads it back",
            build=write_scratch,
            expect=Normal(),
            source=target.evidence,
            tags=frozenset({"batch"}),
            mutates=True,
        ),
        ConformanceCase(
            key="write_scratch_bit",
            title=f"1401 in bit units drives {scratch_bit}",
            build=write_scratch_bit,
            expect=Normal(),
            source=target.evidence,
            tags=frozenset({"batch"}),
            mutates=True,
        ),
    )
    return cases


def _head_index(address: str) -> int:
    """The numeric part of a ``D``-family scratch literal, for deriving neighbours."""
    digits = address.lstrip("DdRr")
    if not digits.isdigit():
        raise ValueError(
            f"the scratch word address must be a decimal-radix register such as 'D100'; "
            f"got {address!r}"
        )
    return int(digits)
