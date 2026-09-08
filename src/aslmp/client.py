"""Layer 5 -- the only module that knows all of it.

Everything below this file is a piece: bytes, a profile, a command, a socket, a gate.
This is where they meet, and DESIGN.md section 4.10 gives it exactly one shape.

**One control flow.** :meth:`Plc._run` is validate, encode, transaction, raise-or-return,
and every command in the package goes through it. There is no second path, no retry, no
reconnect, no fallback encoding, no clamping and no ``except: return []``. Its one
sibling, :meth:`Plc._run_silent`, exists for the single command SLMP documents as
answering nothing -- ``0x1006`` Remote Reset -- and shares this method's prologue rather
than restating it.

**Three declared constants, never a probe.** ``transport``, ``frame`` and ``encoding``
are per-connection GX Works3 facts. They carry the factory defaults as *declared
constants* a caller overrides; they are never auto-detected and never retried in another
value. Getting one wrong costs one handshake round trip and produces the paragraph in
DESIGN.md section 3.4, because a wrong coding, a wrong frame type and a wrong transport
all fail by **silence** on this hardware (FX5U-32MT/DS fw 1.065, 2026-09-06).

**``connect()`` proves something.** ``socket.connect()`` demonstrably lies here: a second
connection to a one-entry SLMP configuration completes its TCP handshake and is then
FINed by the CPU. So :meth:`Plc.connect` runs a ``0x0619`` Self Test and compares the
echo **byte for byte**, then ``0x0101`` Read Type Name and checks the model code against
the declared profile. One measured ~7 ms zero-side-effect round trip proves entry
availability, coding, frame format, route, protocol and liveness at once, and its
:class:`~aslmp.timing.Transaction` becomes the connection's latency baseline (graft G1).

**The third ``L`` guard is wired here.** ``Command.body_len(ctx)`` is threaded into
``FrameFormat.build(expect_body_len=...)`` on every single request. The failure it
catches is asymmetric and both halves are measured: an **understated** data length is
answered ``0xC061`` and the connection recovers, while an **overstated** one gets no
response at all -- the CPU blocks waiting for bytes that never come, and it is
indistinguishable from a dead PLC.

**Bare values here; ``Reading[T]`` on ``plc.timed``** (graft G14). Methods marked
:func:`mirrored` are the generated surface: ``tools/gen_timed.py`` reads this module's AST
and writes ``aslmp/timed.py``, and a test asserts that regenerating is a byte-for-byte
no-op. The surface is written once and there are no hand-maintained twins.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import math
import struct
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as _dataclass_field
from types import TracebackType
from typing import TYPE_CHECKING, ClassVar, Final, Literal, Self, TypeVar, final, overload

from aslmp.blocks.fields import (
    Bounds,
    NumberSpec,
    PointKind,
    check_reading,
    number_spec,
    refuse_write,
)
from aslmp.commands.base import (
    AddressLike,
    Command,
    CommandSummary,
    EncodeContext,
    WordOrder,
    boolean,
    codec_name,
    decoded,
    encoded,
    real,
    signed,
    unsigned,
)
from aslmp.commands.batch import ReadBits, ReadWords, WriteBits, WriteWords
from aslmp.commands.block import BlockSpec, BlockWrite, ReadBlocks, WriteBlocks
from aslmp.commands.info import DEFAULT_LOOPBACK, ClearError, ReadTypeName, SelfTest
from aslmp.commands.monitor import ExecuteMonitor, MonitorRegistration, RegisterMonitor
from aslmp.commands.password import LockPassword, UnlockPassword
from aslmp.commands.random import (
    RandomPoint,
    RandomValue,
    RandomWrite,
    ReadRandom,
    WriteRandom,
)
from aslmp.commands.remote import (
    RemoteLatchClear,
    RemotePause,
    RemoteReset,
    RemoteRun,
    RemoteStop,
    RunMode,
)
from aslmp.connection import Connection, ConnectionInfo, ConnectionState, Txn
from aslmp.errors import (
    NO_DIAGNOSTICS,
    Diagnostics,
    SlmpConfigurationError,
    SlmpError,
    SlmpHandshakeError,
    SlmpMonitoringTimerError,
    SlmpProtocolError,
    SlmpRemoteStateNotReachedError,
    SlmpSinkError,
    SlmpTransportError,
    SlmpVerificationError,
)
from aslmp.errors.routing import end_code_error
from aslmp.identity import CpuIdentity, CpuStatus, cpu_status_command, decode_cpu_status
from aslmp.observability import (
    ConnectionEvent,
    Counters,
    EventSink,
    HandshakeFailed,
    LatencyRecorder,
    MetricsSnapshot,
)
from aslmp.profile import Capability, ClearMode, CpuProfile, Encoding, Evidence, Link
from aslmp.profiles import by_key
from aslmp.results import (
    RandomReading,
    RemoteResult,
    ResetOutcome,
    SplitReading,
)
from aslmp.timing import Clock, Nanos, Transaction, TransactionSink, TransactionTiming
from aslmp.transport.base import TransportKind
from aslmp.transport.inflight import Concurrency
from aslmp.transport.tcp import TcpTransport
from aslmp.transport.udp import UdpTransport
from aslmp.wire.citations import Measurement, Provenance
from aslmp.wire.codec import ASCII, BINARY, Codec, SpecFormat, Unit
from aslmp.wire.frames import FOUR_E, THREE_E, FrameType, request_body
from aslmp.wire.raw import RawResponse
from aslmp.wire.route import Route

if TYPE_CHECKING:  # pragma: no cover - typing only
    from aslmp.blocks.plan import BlockPlan, SplitBlockPlan
    from aslmp.timed import TimedApi

__all__ = [
    "Handshake",
    "MonitoringTimer",
    "Plc",
    "PlcClockSource",
    "RemoteControl",
    "mirrored",
]

R = TypeVar("R")
B = TypeVar("B")
F = TypeVar("F", bound=Callable[..., object])

_TIMER_UNIT_S: Final = 0.25
"""One monitoring-timer unit, in seconds (SH(NA)-080956ENG-M p.24)."""

HANDSHAKE_LATENCY: Final = Measurement(
    cpu="FX5U-32MT/DS",
    firmware="1.065",
    date="2026-09-06",
    note=(
        "A 0x0619 Self Test round trip costs about 7 ms on the built-in Ethernet port, "
        "has no side effect of any kind, and its latency is indistinguishable from a "
        "real read. socket.connect() proves nothing here -- a second connection to a "
        "one-entry SLMP configuration completes and is then FINed by the CPU -- so the "
        "handshake is what makes connect() truthful, and the price is one round trip."
    ),
)
"""Why :meth:`Plc.connect` sends a request before it reports success (graft G1)."""

TRANSPORT_CHOICE: Final = Measurement(
    cpu="FX5U-32MT/DS",
    firmware="1.065",
    date="2026-09-07",
    host="192.168.10.36 (argus-bench)",
    medium="wired, 3.64 ms median RTT",
    samples=300,
    note=(
        "TCP is the default for CONFIGURABILITY, not for speed. On this host and link "
        "UDP is faster at every percentile (p50 2.42 against 3.63 ms, p90 3.40 against "
        "4.05, p99 3.56 against 4.69, stdev 0.40 against 0.36, n=300 each, control "
        "drift 0.01 ms; docs/hardware.md section 5.2). An earlier measurement from the "
        "laptop at 192.168.10.41 over Wi-Fi showed TCP winning the tail and that was a "
        "property of the radio, not of the protocol: it did not survive a wired retest. "
        "TCP is still the default because a UDP SLMP connection entry on iQ-F is "
        "POINT-TO-POINT -- GX Works3 refuses to save one without a destination IP -- so "
        "UDP only works if somebody configured an entry for your host, out of a maximum "
        "of eight. A TCP entry serves any peer. Loss is also silent on UDP. Where an "
        "entry exists and the link is wired, prefer UDP explicitly and knowingly."
    ),
)
"""Why ``transport`` defaults to TCP. Not latency -- see the note, which was corrected
after the Wi-Fi measurement it originally rested on failed to reproduce on wire."""

REMOTE_SETTLE_SECONDS: Final = 0.25
"""How long :meth:`RemoteControl.run` and friends will keep OBSERVING SD203.

Not a retry budget: the remote-control command is sent exactly once and is never re-sent.
What repeats is the reading, because the CPU changes state asynchronously.

Measured on FX5U-32MT/DS fw 1.065, 2026-09-07, three stop/run cycles. Leaving RUN is
effectively synchronous -- SD203 reported STOP on the first poll 3 times out of 3, about
18-21 ms after the command. Entering RUN is not: SD203 still reported STOP on the first
poll in 2 cycles out of 3 and only reached RUN on the second, 25-33 ms after the command.
A single immediate read therefore reports a false negative for RUN roughly two thirds of
the time, which would make ``verify=True`` -- the safety default -- unusable.

Reproduced the same day from the laptop at 192.168.10.41 over **Wi-Fi**, TCP entry 5004,
by ``tests/hardware/test_remote_control.py``: 3 RUNs of 3 needed a second observation and
0 STOPs of 3 did. The link of the original three cycles was not recorded; see
``docs/hardware.md`` section 15. Both runs agree on the asymmetry and on its order of
magnitude, and it is the magnitude this constant rests on: 250 ms is an order of magnitude
above the worst transition seen on either. It is a ceiling, not a wait -- the loop returns
the moment the state matches, and ``RemoteResult.polls`` says how many reads that took.
"""

REMOTE_POLL_INTERVAL_SECONDS: Final = 0.005
"""Gap between SD203 observations while a remote state change settles."""


def mirrored(function: F) -> F:
    """Mark a :class:`Plc` method as mirrored onto the generated ``plc.timed`` surface.

    It is the **identity function** at run time and a machine-readable marker at build
    time: ``tools/gen_timed.py`` walks this module's AST, takes every method wearing this
    decorator, rewrites its return annotation to ``Reading[...]`` (or
    :class:`~aslmp.results.WriteAck`) and its ``_done`` / ``_ack`` call to the matching
    constructor, and writes ``aslmp/timed.py``. A test asserts that regenerating that
    file changes nothing, so the two surfaces cannot drift.

    A decorator rather than a list inside the generator, because a list in another file
    is a thing you can forget to update; a marker sits next to the method it describes.
    """
    return function


# ========================================================================================
# Value types this layer owns
# ========================================================================================


@final
@dataclass(frozen=True, slots=True)
class MonitoringTimer:
    """The SLMP monitoring timer, in 250 ms units. **Never the client-side deadline.**

    Two independent mechanisms: this one makes the *PLC* give up and answer with a
    decodable end code, while ``Plc(timeout=...)`` makes this process give up and raise.
    A client deadline shorter than the timer turns every PLC-side timeout into a bare
    socket timeout, so :class:`Plc` checks the relationship at construction.

    ``INDEFINITE`` is ``0x0000`` and means *wait indefinitely* -- not "no timeout" and not
    "zero milliseconds" (SH(NA)-080956ENG-M p.24). JY997D56001-K p.27 mandates it for the
    FX5 CPU module's own port: a non-zero timer is "supported only for Ethernet modules".
    Our bench swept 0x0000, 0x0010, 0x0028 and 0x00F0 and every one was answered normally
    in ~7 ms, so the field is inert there rather than refused (FX5U-32MT/DS fw 1.065,
    2026-09-06).

    :meth:`seconds` refuses to round: a value that is not a whole number of 250 ms units
    raises and names the two it falls between.
    """

    units: int

    INDEFINITE: ClassVar[MonitoringTimer]

    def __post_init__(self) -> None:
        if not isinstance(self.units, int) or isinstance(self.units, bool):
            raise SlmpMonitoringTimerError(
                f"MonitoringTimer.units is a count of 250 ms units, not "
                f"{type(self.units).__name__}. Use MonitoringTimer.seconds(...)."
            )
        if not 0 <= self.units <= 0xFFFF:
            raise SlmpMonitoringTimerError(
                f"the monitoring timer is a 16-bit field in 250 ms units; {self.units} "
                f"is outside 0..65535."
            )

    @classmethod
    def seconds(cls, value: float) -> MonitoringTimer:
        """``MonitoringTimer.seconds(0.5)``. Refuses anything that is not a whole unit."""
        units = value / _TIMER_UNIT_S
        rounded = round(units)
        if not math.isclose(units, rounded, rel_tol=0.0, abs_tol=1e-9):
            low = math.floor(units) * _TIMER_UNIT_S
            high = math.ceil(units) * _TIMER_UNIT_S
            raise SlmpMonitoringTimerError(
                f"the SLMP monitoring timer is a count of 250 ms units, and {value} s is "
                f"not one: it falls between {low} s and {high} s. Nothing here rounds -- "
                f"a timer silently shortened is a PLC that gives up before you asked it "
                f"to, and a timer silently lengthened is a hang nobody asked for. Pass "
                f"one of the two."
            )
        return cls(rounded)

    @property
    def seconds_value(self) -> float:
        """This timer in seconds. ``0.0`` for :attr:`INDEFINITE`, which never expires."""
        return self.units * _TIMER_UNIT_S

    def __str__(self) -> str:
        if self.units == 0:
            return "indefinite (0x0000)"
        return f"{self.seconds_value:g} s ({self.units} x 250 ms)"


MonitoringTimer.INDEFINITE = MonitoringTimer(0)


class Handshake(enum.Enum):
    """What :meth:`Plc.connect` does before it reports success.

    ``SELF_TEST_AND_IDENTIFY`` is the default and the only value that makes a declared
    ``profile=`` safe: the ``0x0101`` model code must be one the profile claims, or
    connect raises rather than reading ``Y20`` as a different output forever.
    ``SELF_TEST`` proves the connection without identifying the CPU. ``NONE`` exists for
    someone who has measured that they cannot afford ~7 ms at startup, and it trades a
    truthful ``connect()`` for it.
    """

    SELF_TEST_AND_IDENTIFY = "0619+0101"
    SELF_TEST = "0619"
    NONE = "none"


@final
@dataclass(frozen=True, slots=True)
class PlcClockSource:
    """Where the PLC's own free-running counter lives, if the caller has one, **and what
    type the PLC program writes it as**.

    Our bench keeps a scan counter at ``D8``/``D9``. A block plan bound with a clock source
    appends that one extra access point to its ``0403``, so every cycle carries the PLC's
    own notion of time inside the same snapshot as the data -- which is the only way to
    tell "the network was slow" from "the CPU did not scan".

    .. rubric:: ``kind`` is required, and it used to be a constant

    This class once carried an address and nothing else, and
    :mod:`aslmp.blocks.plan` read it as a hard-coded unsigned double word. On the bench
    that wrote it, ``D8`` is a ``REAL``: the CPU's own ST is ``IO_Scan := IO_Scan + 1.0``.
    So ``tx.plc_clock`` published the float's **bit pattern**, which rises monotonically
    for positive floats and is therefore a plausible, useless counter -- 1018 real
    counts/s (``docs/hardware.md`` section 17, the one place that rate is measured) were
    reported as 16274, and that 16x is not even constant, because the
    ulp-step of a REAL halves at every power of two
    (:data:`~aslmp.blocks.fields.IMPLAUSIBLE_VALUE_FINDING`; measured again on
    FX5U-32MT/DS fw 1.065 from this host over TCP 5002, 2026-09-07:
    ``D8/D9 = 0xFEA0 0x4970`` is the f32 987114.0 and the u32 1232141984).

    A register carries no type on the wire, so nothing here can detect that and nothing
    here guesses. ``kind`` is how you say it, in exactly the vocabulary a block field's
    :class:`~aslmp.blocks.fields.NumberSpec` uses::

        PlcClockSource("D8", kind="f32", bounds=Bounds(0.0, 1.0e7))

    ``bounds`` is the same optional promise a bounded block field makes
    (:class:`~aslmp.blocks.fields.Bounds`) and is checked on every cycle: a clock outside
    the declared range raises
    :class:`~aslmp.blocks.fields.SlmpImplausibleValueError` rather than being published.
    Declaring one is the only defence against the *next* mis-declaration, since the wrong
    type still answers ``0x0000``.

    ``kind`` has **no default**, and that is the second half of the same fix. It spent
    one revision defaulting to ``"u32"`` "for compatibility", which is the identical
    shape to the defect it replaced: a default standing in for a fact only the caller
    knows, invisible on the wire, and wrong on the very bench that motivated the class.
    ``PlcClockSource("D8")`` would still have published 16274 counts/s for 1018 real
    ones, and no test, no end code and no readback could have told anyone. This package
    is ``0.1.0.dev0`` with no released users, so the guess is gone rather than carried
    forward: check the type in GX Works3 under ``Label -> Global Label`` and say it.

    The client stores this and exposes it; :mod:`aslmp.blocks.plan` is what folds it into a
    request. Nothing here silently adds a point to a caller's own ``read_random``.
    """

    address: AddressLike
    kind: PointKind
    bounds: Bounds | None = None
    label: str = "plc_clock"

    spec: NumberSpec = _dataclass_field(init=False, repr=False, compare=False)
    """The field spec this declaration resolves to: width, ``struct`` code and kind.

    Exactly what a block field of the same type carries, which is the point -- there is
    one description of "two registers holding an IEEE-754 float" in this library, and both
    the annotation ``F32`` and ``kind="f32"`` resolve to it.
    """

    def __post_init__(self) -> None:
        if not isinstance(self.label, str) or not self.label.strip():
            raise SlmpConfigurationError(
                f"PlcClockSource(label=...) names the point in a block report and in a "
                f"range refusal; it cannot be {self.label!r}."
            )
        # Built here rather than at bind so that a kind that is not a single access
        # point, or a bound the declared width could never reach, is a start-up failure
        # in the file that declares it and not a surprise on the first cycle.
        object.__setattr__(
            self, "spec", number_spec(self.kind, self.bounds, what=self.describe())
        )

    def describe(self) -> str:
        """``PlcClockSource('D8', kind='f32')`` -- the declaration, for a refusal."""
        return f"PlcClockSource({str(self.address)!r}, kind={self.kind!r})"


# ========================================================================================
# Value packing -- one implementation per width, and no byte-swapping helper anywhere
# ========================================================================================


def _order_words(words: tuple[int, ...], order: WordOrder) -> tuple[int, ...]:
    """The words of one multi-word value in wire order.

    ``LOW_FIRST`` is the identity: ``struct.pack("<f", v)`` is already low word first,
    which is what an FX5U-32MT/DS on firmware 1.065 was measured to hold -- 1234.5
    written as one double-word point put ``00 50 9A 44`` on the wire and read back
    ``D104 = 0x5000``, ``D105 = 0x449A`` (2026-09-06). ``HIGH_FIRST`` reverses the
    sequence of words and never the bytes inside one: byte order within a register is a
    property of the codec, not of a PLC program's convention.
    """
    return words if order is WordOrder.LOW_FIRST else tuple(reversed(words))


def _to_words(raw: bytes, order: WordOrder) -> tuple[int, ...]:
    """Little-endian bytes to registers, in this convention's order."""
    count = len(raw) // 2
    return _order_words(struct.unpack(f"<{count}H", raw), order)


def _from_words(words: Sequence[int], order: WordOrder) -> bytes:
    """Registers back to little-endian bytes. The exact inverse of :func:`_to_words`."""
    ordered = _order_words(tuple(words), order)
    return struct.pack(f"<{len(ordered)}H", *ordered)


# ========================================================================================
# The escape hatch's command
# ========================================================================================


@dataclass(frozen=True, slots=True)
class _RawCommand(Command[RawResponse]):
    """``raw_command()``: whatever bytes the caller asked for, and nothing else.

    It bypasses **command validation only**. The frame, the length arithmetic, the
    in-flight gate, the end-code raise and the transaction record all still apply,
    because those are the parts that make a wrong answer visible.

    ``CODE`` is ``0x0000`` because a class-level command code is a property of a *known*
    command and this class is the absence of one. The code that goes on the wire is the
    instance's :attr:`command`, reported by :meth:`summary` -- which is the only thing
    :meth:`Plc._run` reads. Nothing in the client reads ``cmd.CODE``.
    """

    command: int
    subcommand_code: int
    data: bytes = b""
    mutates_flag: bool = True

    CODE = 0x0000
    NAME = "Raw command"
    mutates = True
    CITES = (
        Measurement(
            cpu="FX5U-32MT/DS",
            firmware="1.065",
            date="2026-09-06",
            note=(
                "The escape hatch exists because this CPU accepted requests its own "
                "manual forbids -- a TS0 word point in a 0403, and a write at Y8, an "
                "address octal notation cannot express -- and answered 0x0000 to both. A "
                "library that can only send what it approves of cannot be used to find "
                "out what a CPU really does."
            ),
        ),
    )

    def __post_init__(self) -> None:
        for name, value in (
            ("command", self.command),
            ("subcommand", self.subcommand_code),
        ):
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError(
                    f"raw_command() {name} is an int, not {type(value).__name__}"
                )
            if not 0 <= value <= 0xFFFF:
                raise SlmpConfigurationError(
                    f"raw_command() {name}=0x{value:X} is not a 16-bit field."
                )
        object.__setattr__(self, "data", bytes(self.data))

    def subcommand(self, ctx: EncodeContext) -> int:
        del ctx
        return self.subcommand_code

    def validate(self, ctx: EncodeContext) -> None:
        """Nothing at all. That is the entire purpose of this class."""
        del ctx

    def payload_len(self, ctx: EncodeContext) -> int:
        del ctx
        return len(self.data)

    def encode(self, ctx: EncodeContext) -> bytes:
        del ctx
        return self.data

    def decode(self, payload: bytes, ctx: EncodeContext) -> RawResponse:
        """Never called: :meth:`Plc._decode` hands a raw command its parsed frame."""
        del payload, ctx
        raise SlmpConfigurationError(
            "a raw command has no payload decoder; raw_command() returns the parsed "
            "RawResponse itself, which is the whole point of an escape hatch."
        )

    def describe(self) -> str:
        return (
            f"raw_command(0x{self.command:04X}, 0x{self.subcommand_code:04X}, "
            f"{len(self.data)} byte(s))"
        )

    def summary(self, ctx: EncodeContext, *, request_bytes: int = 0) -> CommandSummary:
        del ctx
        return CommandSummary(
            command=self.command,
            subcommand=self.subcommand_code,
            request_bytes=request_bytes,
            text=self.describe(),
        )


@final
@dataclass(frozen=True, slots=True)
class _SilentRawCommand(_RawCommand):
    """A raw command whose caller has declared that nothing will answer it.

    ``response_optional`` is otherwise true for ``0x1006`` Remote Reset alone. A caller
    who sets ``expect_response=False`` on a command that *does* answer leaves its
    response on the socket, which is how the next transaction reads this one's bytes as
    fresh data -- so the connection closes the socket when the token is burnt without a
    read, rather than reusing it.

    That sentence used to be false. ``response_optional = True`` here defeats the guard
    in :meth:`Plc._run_silent`, ``TcpTransport.exchange`` returned early without reading
    or closing, and the close in ``Connection.transaction``'s ``finally`` never fired
    because ``exchange_without_response`` had already marked the transaction finished --
    so the connection stayed ``READY`` and the next transaction decoded the previous
    request's response as its own, with end code ``0x0000``. It is true now:
    :meth:`aslmp.connection.Connection.retire_after_silent_exchange` closes the socket
    and goes sticky ``FAILED``, and ``TcpTransport`` closes its own. ``0x1006`` -- the one
    command that legitimately gets no answer -- is unaffected, because the CPU is
    resetting and :meth:`RemoteControl.reset` closes the client immediately afterwards.
    """

    response_optional = True

    def describe(self) -> str:
        return (
            f"raw_command(0x{self.command:04X}, 0x{self.subcommand_code:04X}, "
            f"{len(self.data)} byte(s), expect_response=False)"
        )


# ========================================================================================
# The client
# ========================================================================================


REMOTE_CONTROL_COMMANDS: Final[Mapping[int, str]] = {
    RemoteRun.CODE: RemoteRun.NAME,
    RemoteStop.CODE: RemoteStop.NAME,
    RemotePause.CODE: RemotePause.NAME,
    RemoteLatchClear.CODE: RemoteLatchClear.NAME,
    RemoteReset.CODE: RemoteReset.NAME,
}
"""Every command code that can stop, start or clear a running machine, by name.

Built from the command classes rather than written out, so a sixth remote-control command
cannot be added to :mod:`aslmp.commands.remote` and quietly stay outside the interlock.
It exists here, on the client, because ``allow_remote_control`` is a property of the
client: a command class can refuse itself in ``validate()``, and the raw escape hatch is
precisely the path where no command class gets to.
"""


_CODECS: Final[Mapping[str, Codec]] = {"binary": BINARY, "ascii": ASCII}


@final
class Plc:
    """One SLMP client, bound to one configured connection entry.

    **Two required arguments: ``host`` and ``profile``.** There is no generic profile and
    no fallback: ``X`` and ``Y`` are octal on an iQ-F and hexadecimal everywhere else, so
    ``Y20`` is output 16 on an FX5U and output 32 on an iQ-R, and **both CPUs answer end
    code 0x0000**. Nothing on the wire tells them apart. ``aslmp identify <host>`` prints
    the profile string to pass, and :meth:`identify` is the programmatic form.

    Everything else carries a documented default matching the GX Works3 factory settings
    and our bench. A default is a constant the caller overrides; it is not the
    auto-detection the hardware findings forbid.
    """

    __slots__ = (
        "_capture_frames",
        "_clock",
        "_conn",
        "_connect_timeout",
        "_counters",
        "_ctx",
        "_encoding",
        "_frame",
        "_handshake_mode",
        "_identity",
        "_last_generation",
        "_listeners",
        "_monitoring_timer",
        "_name",
        "_on_transaction",
        "_peer",
        "_plc_clock",
        "_profile",
        "_recorder",
        "_remote",
        "_sink_failed_generation",
        "_timed",
        "_transport_kind",
    )

    def __init__(
        self,
        host: str,
        port: int = 5000,
        *,
        profile: CpuProfile | str,
        transport: TransportKind = TransportKind.TCP,
        frame: FrameType = FrameType.THREE_E,
        encoding: Encoding = Encoding.BINARY,
        link: Link = Link.CPU_BUILTIN,
        route: Route = Route.OWN_STATION,
        spec: SpecFormat | None = None,
        timeout: float = 3.0,
        connect_timeout: float = 3.0,
        monitoring_timer: MonitoringTimer = MonitoringTimer.INDEFINITE,
        handshake: Handshake = Handshake.SELF_TEST_AND_IDENTIFY,
        concurrency: Concurrency = Concurrency.STRICT,
        validate_ranges: bool = True,
        word_order: WordOrder = WordOrder.LOW_FIRST,
        tcp_nodelay: bool = True,
        udp_pipeline_depth: int = 1,
        allow_remote_control: bool = False,
        capability_overrides: Mapping[Capability, str] | None = None,
        plc_clock: PlcClockSource | None = None,
        capture_frames: bool = False,
        clock: Clock = time.monotonic_ns,
        on_transaction: TransactionSink | None = None,
        on_event: EventSink | None = None,
        name: str | None = None,
    ) -> None:
        resolved = by_key(profile) if isinstance(profile, str) else profile
        if capability_overrides:
            resolved = _override_capabilities(resolved, capability_overrides)
        self._profile = resolved
        self._encoding = encoding
        self._frame = frame
        self._transport_kind = transport
        self._peer = (host, port)
        self._name = name if name is not None else f"{host}:{port}"
        self._monitoring_timer = _checked_timer(monitoring_timer, timeout)
        self._connect_timeout = connect_timeout
        self._handshake_mode = handshake
        self._plc_clock = plc_clock
        self._capture_frames = capture_frames
        self._clock = clock
        self._on_transaction = on_transaction
        self._listeners: list[EventSink] = [] if on_event is None else [on_event]
        self._identity: CpuIdentity | None = None
        self._counters = Counters()
        self._recorder = LatencyRecorder()
        self._last_generation = -1
        self._sink_failed_generation = -1
        self._timed: TimedApi | None = None

        self._ctx = EncodeContext(
            codec=_CODECS[encoding.coding],
            spec=spec if spec is not None else resolved.default_spec,
            profile=resolved,
            encoding=encoding,
            link=link,
            word_order=word_order,
            allow_remote_control=allow_remote_control,
            validate_ranges=validate_ranges,
        )
        frame_format = FOUR_E if frame is FrameType.FOUR_E else THREE_E
        if frame is FrameType.FOUR_E:
            resolved.require(Capability.FOUR_E_FRAME, what=f"Plc(frame={frame.name})")
        self._conn = Connection(
            _build_transport(
                host,
                port,
                kind=transport,
                nodelay=tcp_nodelay,
                carries_serial=frame_format.carries_serial,
                udp_pipeline_depth=udp_pipeline_depth,
            ),
            frame=frame_format,
            codec=self._ctx.codec,
            route=route,
            timeout=timeout,
            concurrency=concurrency,
            clock=clock,
            counters=self._counters,
            events=self._emit_event,
            capture_frames=capture_frames,
        )
        self._remote = RemoteControl(self)

    # -- what an exception may say about us (``errors.ClientSummary``) --------

    @property
    def peer(self) -> tuple[str, int]:
        """``(host, port)`` as configured."""
        return self._peer

    @property
    def model(self) -> str | None:
        """The model name the CPU gave, or ``None`` before the identify handshake."""
        return None if self._identity is None else self._identity.model

    @property
    def model_code(self) -> int | None:
        """The ``0x0101`` model code, or ``None``. Never guessed from the profile."""
        return None if self._identity is None else self._identity.model_code

    @property
    def transport(self) -> TransportKind:
        """The configured transport. A GX Works3 connection-entry fact."""
        return self._transport_kind

    @property
    def encoding(self) -> Encoding:
        """The configured Communication Data Code. An own-node parameter."""
        return self._encoding

    @property
    def frame(self) -> FrameType:
        """The configured frame format. A GX Works3 connection-entry fact."""
        return self._frame

    # -- inspection ----------------------------------------------------------

    @property
    def name(self) -> str:
        """A label for this client, for a thread name and for a diagnostic."""
        return self._name

    @property
    def profile(self) -> CpuProfile:
        """The declared profile. Never switched under the caller."""
        return self._profile

    @property
    def state(self) -> ConnectionState:
        """Where the connection is. ``FAILED`` is sticky and never left implicitly."""
        return self._conn.state

    @property
    def generation(self) -> int:
        """Bumps on every reconnect and every UDP rebind. The anti-lie field."""
        return self._conn.generation

    @property
    def identity(self) -> CpuIdentity | None:
        """Which CPU answered ``0x0101``, or ``None`` if it was never asked."""
        return self._identity

    @property
    def info(self) -> ConnectionInfo | None:
        """The record of the established connection, with the handshake's own timing."""
        return self._conn.info

    @property
    def counters(self) -> Counters:
        """The live tallies. Never reset by this library."""
        return self._counters

    @property
    def plc_clock(self) -> PlcClockSource | None:
        """The configured PLC-side counter, for a bound block plan to fold in."""
        return self._plc_clock

    @property
    def remote(self) -> RemoteControl:
        """Remote RUN / STOP / PAUSE / reset / password. Interlocked (graft G11)."""
        return self._remote

    @property
    def timed(self) -> TimedApi:
        """The same calls, returning :class:`~aslmp.results.Reading` (graft G14).

        ``aslmp/timed.py`` is generated from this module's AST and committed, and a test
        asserts regeneration is a byte-for-byte no-op -- so the two surfaces are written
        once and cannot drift. The import is deferred because the two modules are peers
        at Layer 5 and ``timed`` imports this one.
        """
        facade = self._timed
        if facade is None:
            from aslmp.timed import TimedApi as _TimedApi

            facade = _TimedApi(self)
            self._timed = facade
        return facade

    def metrics(self) -> MetricsSnapshot:
        """One frozen view of the counters and the latency window."""
        return self._recorder.snapshot(
            at=Nanos(self._clock()),
            connection_id=self._conn.connection_id,
            generation=self._conn.generation,
            counters=self._counters,
        )

    def add_event_listener(self, listener: EventSink) -> None:
        """Add a connection-event callback."""
        self._listeners.append(listener)

    def events(self) -> AsyncIterator[ConnectionEvent]:
        """Every connection event, from the moment this iterator is **created**.

        Not an ``async def``, deliberately. An async generator does not run a line of its
        body until the first ``anext()``, so the listener would be registered only after
        the caller's next await -- and ``connect()`` would have emitted ``Connecting`` and
        ``Connected`` into nothing. Registering eagerly and returning the generator is the
        difference between an event stream and an event stream with a hole at the start.

        The listener is removed when the iterator is closed, so breaking out of an
        ``async for`` does not leave a queue growing behind a loop that stopped reading.
        """
        queue: asyncio.Queue[ConnectionEvent] = asyncio.Queue()
        listener: EventSink = queue.put_nowait
        self._listeners.append(listener)

        async def stream() -> AsyncIterator[ConnectionEvent]:
            try:
                while True:
                    yield await queue.get()
            finally:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return stream()

    def __repr__(self) -> str:
        return (
            f"Plc({self._peer[0]}:{self._peer[1]}, profile={self._profile.key}, "
            f"{self._transport_kind.value}/{self._encoding.value}/{self._frame.value}, "
            f"state={self._conn.state.value})"
        )

    # -- lifecycle -----------------------------------------------------------

    async def connect(self) -> ConnectionInfo:
        """Open the socket and **prove** it (DESIGN.md section 4.5).

        Steps 1 and 2 are the socket open and the transport's non-blocking EOF check,
        which classifies a FIN that has already arrived and costs nothing (graft G2).
        Step 3 is the proof: ``0x0619`` Self Test with a per-generation nonce, echo
        compared byte for byte. Step 4 is ``0x0101``, whose model code must be one the
        declared profile claims. Only then does the connection report ``READY``.
        """
        await self._conn.open(timeout=self._connect_timeout)
        return await self._prove()

    async def reconnect(self, *, reason: str) -> ConnectionInfo:
        """Rebuild the socket and re-prove it. **Never implicit** (graft G12).

        ``generation`` bumps, so nothing sampled through the old socket can be confused
        with what comes after it. Automatic reconnection lives in
        :class:`aslmp.resilience.Supervisor`, whose backoff policy has no default.
        """
        await self._conn.reopen(reason=reason, timeout=self._connect_timeout)
        return await self._prove()

    async def aclose(self) -> None:
        """Close on purpose. Idempotent."""
        await self._conn.aclose()

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        await self.aclose()

    @classmethod
    async def identify(
        cls,
        host: str,
        port: int = 5000,
        *,
        encoding: Encoding = Encoding.BINARY,
        frame: FrameType = FrameType.THREE_E,
        transport: TransportKind = TransportKind.TCP,
        timeout: float = 3.0,
    ) -> CpuIdentity:
        """Ask a CPU what it is, without declaring a profile first.

        The programmatic form of ``aslmp identify <host>``. ``0x0619`` and ``0x0101``
        carry no device address at all, so the profile used to send them cannot change a
        byte; the answer is resolved against every shipped profile, and an unrecognised
        model code raises :class:`~aslmp.errors.SlmpProfileMismatchError` rather than
        being read as a family guessed from the code's high byte.
        """
        probe = cls(
            host,
            port,
            profile=by_key("melsec:iq-f/fx5u"),
            encoding=encoding,
            frame=frame,
            transport=transport,
            timeout=timeout,
            connect_timeout=timeout,
            handshake=Handshake.SELF_TEST,
        )
        try:
            await probe.connect()
            return await probe.read_type_name()
        finally:
            await probe.aclose()

    async def _prove(self) -> ConnectionInfo:
        """Steps 3 to 5 of the connect handshake, shared by connect and reconnect."""
        mode = self._handshake_mode
        if mode is Handshake.NONE:
            return self._conn.confirm()
        nonce = self._conn.generation & 0xFFFF
        payload = b"0619" + f"{nonce:04X}".encode("ascii")
        try:
            echo, handshake_tx = await self._run(SelfTest(payload), mutates=False)
        except SlmpProtocolError as exc:
            self._report_handshake_failure(exc)
            raise SlmpHandshakeError(
                f"the 0x0619 Self Test echo did not verify byte for byte. That echo is "
                f"the proof that this connection entry is free, that the coding, the "
                f"frame format, the route and the protocol are the ones the CPU is "
                f"configured for, and that the CPU is answering now: {exc.headline()}",
                diagnostics=exc.diagnostics,
            ) from exc
        except SlmpError as exc:
            self._report_handshake_failure(exc)
            raise
        if echo != payload:  # pragma: no cover - SelfTest.decode already refuses this
            raise SlmpHandshakeError(
                f"the 0x0619 echo was {echo!r} and {payload!r} was sent."
            )
        if mode is Handshake.SELF_TEST:
            return self._conn.confirm(handshake=handshake_tx)
        try:
            identity = await self.read_type_name()
            identity.check(self._profile)
        except SlmpError as exc:
            self._report_handshake_failure(exc)
            raise
        self._identity = identity
        return self._conn.confirm(handshake=handshake_tx, identity=identity)

    def _report_handshake_failure(self, exc: SlmpError) -> None:
        self._counters.handshake_failures += 1
        with contextlib.suppress(SlmpSinkError):
            self._emit_event(
                HandshakeFailed(
                    connection_id=self._conn.connection_id,
                    generation=self._conn.generation,
                    at=Nanos(self._clock()),
                    peer=self._peer,
                    reason=exc.headline().splitlines()[0],
                    error_type=type(exc).__name__,
                )
            )

    # ====================================================================================
    # THE control flow (DESIGN.md section 4.10)
    # ====================================================================================

    async def _run(self, cmd: Command[R], *, mutates: bool) -> tuple[R, Transaction]:
        """Validate, encode, one transaction, raise-or-return. The only path.

        Note what is absent: no retry, no reconnect, no fallback encoding, no clamping,
        no ``except: return []``. A failure after the bytes went out on a state-changing
        command is wrapped as :class:`~aslmp.errors.SlmpOutcomeUnknownError` by
        :class:`aslmp.connection.Txn`, which is the only layer that knows whether the
        request reached the socket; this method does not repeat that wrapping.
        """
        ctx = self._ctx
        cmd.validate(ctx)  # 1. raise before a byte is BUILT
        payload = cmd.checked_encode(ctx)  # 2. pure; payload_len is checked against it
        subcommand = cmd.subcommand(ctx)
        conn = self._conn
        summary = cmd.summary(ctx)
        async with conn.transaction(  # 3. one in flight, structurally
            command=summary.command, subcommand=subcommand
        ) as txn:
            request = self._build(cmd, payload, summary, subcommand, txn.serial)
            summary = cmd.summary(ctx, request_bytes=len(request))
            try:
                raw, timing = await txn.exchange(
                    request, conn.accumulator(txn.serial), mutates=mutates
                )
            except SlmpError as exc:
                self._enrich(exc, summary, request)
                self._record_failure(txn, summary, subcommand, request)
                raise
            if raw.end_code != 0:  # 4. raise, always. Never a falsy value.
                failed = self._transaction(
                    timing, txn, summary, subcommand, raw=raw, request=request
                )
                with contextlib.suppress(SlmpSinkError):
                    self._observe(failed)
                raise end_code_error(
                    raw,
                    request=summary,
                    tx=failed,
                    client=self,
                    sent_frame=request if self._capture_frames else None,
                )
            value = self._decode(cmd, raw, ctx)  # 5. typed value
            tx = self._transaction(  # 6. latency as data
                txn.decoded(), txn, summary, subcommand, raw=raw, request=request
            )
        self._observe(tx)  # 7. sink, AFTER decoded_at is stamped
        return value, tx

    async def _run_silent(self, cmd: Command[object], *, mutates: bool) -> Transaction:
        """The same policy, for the one command whose *absence* of a reply is success.

        ``0x1006`` Remote Reset: SH(NA)-080956ENG-M p.136 says the response "is not be
        sent back to the external device", and over TCP the connection is torn down with
        it. It burns the capability token like any other exchange, so this is not a
        general ``send``; there is still no public method anywhere in this package that
        puts bytes on a socket.
        """
        if not cmd.response_optional:
            raise SlmpConfigurationError(
                f"{cmd.describe()} defines a response, so it must be read. A request "
                f"whose answer is left on the socket is how the NEXT transaction reads "
                f"this one's bytes as fresh data, and on 3E there is no serial No. that "
                f"would ever reveal it."
            )
        ctx = self._ctx
        cmd.validate(ctx)
        payload = cmd.checked_encode(ctx)
        subcommand = cmd.subcommand(ctx)
        summary = cmd.summary(ctx)
        async with self._conn.transaction(
            command=summary.command, subcommand=subcommand
        ) as txn:
            request = self._build(cmd, payload, summary, subcommand, txn.serial)
            summary = cmd.summary(ctx, request_bytes=len(request))
            try:
                timing = await txn.exchange_without_response(request, mutates=mutates)
            except SlmpError as exc:
                self._enrich(exc, summary, request)
                raise
            tx = self._transaction(
                timing, txn, summary, subcommand, raw=None, request=request
            )
        self._observe(tx)
        return tx

    def _decode(self, cmd: Command[R], raw: RawResponse, ctx: EncodeContext) -> R:
        """Turn the response into the command's own typed value.

        ``checked_decode`` is the door rather than ``decode``: a codec refusal -- a
        character outside ``[0-9A-F]`` in ASCII, a binary bit nibble that is neither 0
        nor 1 -- is a plain ``ValueError`` at layer 0, and letting it escape untranslated
        means a caller's ``except SlmpProtocolError`` around a read does not catch the one
        failure it exists for.

        The raw escape hatch is the single exception, because its "value" *is* the parsed
        frame: it has no payload decoder and its ``decode`` says so.
        """
        if isinstance(cmd, _RawCommand):
            # _RawCommand is a Command[RawResponse], so R is RawResponse in this branch;
            # mypy cannot narrow a TypeVar through isinstance on the parameter's class.
            return raw  # type: ignore[return-value]
        return cmd.checked_decode(raw.payload, ctx)

    def _build(
        self,
        cmd: Command[object],
        payload: bytes,
        summary: CommandSummary,
        subcommand: int,
        serial: int | None,
    ) -> bytes:
        """The request frame, with the third ``L`` guard armed.

        ``expect_body_len=cmd.body_len(ctx)`` is computed from ``payload_len`` and never
        from ``len(encode(...))``, so a command whose two length statements disagree
        raises here instead of reaching a socket. The failure is asymmetric and both
        halves are measured on FX5U-32MT/DS fw 1.065: an understated ``L`` is answered
        ``0xC061`` and the connection recovers; an **overstated** one gets no response at
        all and is indistinguishable from a dead PLC.
        """
        ctx = self._ctx
        body = request_body(
            ctx.codec,
            monitoring_timer=self._monitoring_timer.units,
            command=summary.command,
            subcommand=subcommand,
            payload=payload,
        )
        return self._conn.frame.build(
            route=self._conn.route,
            body=body,
            codec=ctx.codec,
            serial=serial,
            expect_body_len=cmd.body_len(ctx),
        )

    def _transaction(
        self,
        timing: TransactionTiming,
        txn: Txn,
        summary: CommandSummary,
        subcommand: int,
        *,
        raw: RawResponse | None,
        request: bytes,
    ) -> Transaction:
        """One record. ``generation`` and ``after_reconnect`` are the anti-lie fields."""
        generation = self._conn.generation
        after_reconnect = generation > 0 and generation != self._last_generation
        self._last_generation = generation
        return Transaction(
            timing=timing,
            sequence=txn.sequence,
            connection_id=self._conn.connection_id,
            generation=generation,
            after_reconnect=after_reconnect,
            command=summary.command,
            subcommand=subcommand,
            frame=self._frame,
            encoding=self._encoding,
            transport=self._transport_kind,
            serial=txn.serial,
            request_bytes=len(request),
            response_bytes=0 if raw is None else raw.response_bytes,
            end_code=0 if raw is None else raw.end_code,
            prebuilt=txn.prebuilt,
            request_frame=request if self._capture_frames else None,
            response_frame=(
                raw.raw if (self._capture_frames and raw is not None) else None
            ),
        )

    def _record_failure(
        self, txn: Txn, summary: CommandSummary, subcommand: int, request: bytes
    ) -> None:
        """Emit a record for a transaction that failed on the wire, where there is one.

        ``on_transaction`` receives every record, including failed ones -- that is what
        makes a latency histogram honest about the transactions that did not finish. A
        transaction that never reached the socket has no timing to report and
        :class:`~aslmp.timing.TimingBuilder` refuses to build one, which is right: a
        fabricated ``sent_at`` would put a sample into the published histogram for a
        request that never left. A sink that raises here must not replace the PLC failure
        already on its way to the caller, so its error is counted and suppressed.
        """
        if txn.timing.sent_at is None:
            return
        tx = self._transaction(
            txn.timing.build(), txn, summary, subcommand, raw=None, request=request
        )
        with contextlib.suppress(SlmpSinkError):
            self._observe(tx)

    def _enrich(self, exc: SlmpError, summary: CommandSummary, request: bytes) -> None:
        """Fill in the diagnostic lines only this layer knows: the target and the call.

        The transport raised with what it had -- a deadline, a byte count, a route. It
        cannot name the CPU or print the call the user wrote, because it is forbidden to
        know either. Nothing already set is overwritten.
        """
        old = exc.diagnostics
        exc.diagnostics = Diagnostics(
            client=old.client if old.client is not None else self,
            request=old.request if old.request is not None else summary,
            tx=old.tx,
            sent_frame=(
                old.sent_frame
                if old.sent_frame is not None
                else (request if self._capture_frames else None)
            ),
            received_frame=old.received_frame,
            requested_route=(
                old.requested_route
                if old.requested_route is not None
                else self._conn.route
            ),
            responded_route=old.responded_route,
            echoed=old.echoed,
            cause=old.cause,
            measurement=old.measurement,
            note=old.note,
            action=old.action,
            manual=old.manual,
        )

    def _observe(self, tx: Transaction) -> None:
        """Fold one record into the counters and the histogram, then hand it to the sink.

        The sink is called **after** ``decoded_at`` is stamped, so a slow sink cannot
        contaminate the measurement it is handed. A sink that raises is counted in
        ``counters.sink_errors`` and reported at most once per generation: a control loop
        whose sink is broken should learn that once and then keep controlling the plant,
        and swallowing it entirely would be the silent recovery this library forbids.
        """
        self._counters.observe(tx)
        self._recorder.observe(tx)
        sink = self._on_transaction
        if sink is None:
            return
        failure: Exception | None = None
        try:
            sink(tx)
        except Exception as raised:
            failure = raised
        if failure is None:
            return
        self._counters.sink_errors += 1
        reported_before = self._sink_failed_generation == tx.generation
        self._sink_failed_generation = tx.generation
        if reported_before:
            # Counted, not swallowed: counters.sink_errors keeps rising and the tally is
            # in every MetricsSnapshot. Raising on every transaction instead would let
            # one broken callback take a plant off the network.
            return
        raise SlmpSinkError(
            f"the transaction sink raised: {type(failure).__name__}: {failure}. Counted "
            f"in counters.sink_errors and reported once per generation -- a broken "
            f"callback must not take a plant off the network, and must not be swallowed "
            f"either.",
            sink="on_transaction",
            diagnostics=NO_DIAGNOSTICS,
        ) from failure

    def _emit_event(self, event: ConnectionEvent) -> None:
        for listener in tuple(self._listeners):
            listener(event)

    # -- small shared helpers ------------------------------------------------

    def _order(self, word_order: WordOrder | None) -> WordOrder:
        """``None`` means "use the client's", which is a PLC-program convention."""
        return self._ctx.word_order if word_order is None else word_order

    def _check(self, read_back: object, written: object, *, what: str) -> None:
        """The ``verify=True`` comparison. Raises rather than reporting the write anyway."""
        if read_back == written:
            return
        raise SlmpVerificationError(
            f"{what}: the write was answered end code 0x0000 and the read-back returned "
            f"{read_back!r}, not {written!r}. Nothing here retries, and nothing reports "
            f"the write as successful anyway.",
            written=written,
            read_back=read_back,
            diagnostics=Diagnostics(client=self),
        )

    def _done(self, value: R, tx: Transaction) -> R:
        """Return the value and drop the record; ``plc.timed`` returns ``Reading``.

        ``tools/gen_timed.py`` rewrites every call to this into ``Reading(value, tx)``.
        It is the seam between the two surfaces, and it is one function so that the seam
        is one thing rather than thirty.
        """
        del tx
        return value

    def _ack(self, points: int, tx: Transaction) -> None:
        """Return nothing from a write; ``plc.timed`` returns ``WriteAck(points, tx)``."""
        del points, tx

    def _random_ceiling(self) -> int:
        """The ``0403`` point ceiling to split against, or a refusal naming the rule."""
        limit = self._profile.limit(0x0403, self._encoding, Unit.WORD, self._ctx.link)
        maximum = getattr(limit.rule, "maximum", None)
        if not isinstance(maximum, int):
            raise SlmpConfigurationError(
                f"allow_split needs a flat point ceiling to split against and "
                f"{self._profile.key}'s 0x0403 budget is {limit.rule.describe()}. Split "
                f"the request yourself, so that the choice of where the snapshot breaks "
                f"is yours rather than ours."
            )
        return maximum

    # ====================================================================================
    # Typed scalars -- one named method per width, no magic dtype strings
    # ====================================================================================

    @mirrored
    async def read_bit(self, address: AddressLike, /) -> bool:
        """One bit device, as a ``bool``. ``0x0401`` in bit units."""
        values, tx = await self._run(ReadBits(address, 1), mutates=False)
        return self._done(values[0], tx)

    @mirrored
    async def read_i16(
        self,
        address: AddressLike,
        /,
        *,
        minimum: float | None = None,
        maximum: float | None = None,
    ) -> int:
        """One register as a signed 16-bit integer.

        ``minimum``/``maximum`` are the per-call form of a block field's declared bounds:
        a promise **you** make about what may be in that register, enforced on the value
        that comes back. See :meth:`read_f32`, which is where the promise earns its keep.
        """
        words, tx = await self._run(ReadWords(address, 1), mutates=False)
        value = signed(words[0], bits=16)
        check_reading(
            value,
            minimum,
            maximum,
            field="read_i16",
            address=address,
            registers=words,
            client=self,
        )
        return self._done(value, tx)

    @mirrored
    async def read_u16(
        self,
        address: AddressLike,
        /,
        *,
        minimum: float | None = None,
        maximum: float | None = None,
    ) -> int:
        """One register as an unsigned 16-bit integer."""
        words, tx = await self._run(ReadWords(address, 1), mutates=False)
        check_reading(
            words[0],
            minimum,
            maximum,
            field="read_u16",
            address=address,
            registers=words,
            client=self,
        )
        return self._done(words[0], tx)

    @mirrored
    async def read_i32(
        self,
        address: AddressLike,
        /,
        *,
        word_order: WordOrder | None = None,
        minimum: float | None = None,
        maximum: float | None = None,
    ) -> int:
        """Two consecutive registers as a signed 32-bit integer."""
        words, tx = await self._run(ReadWords(address, 2), mutates=False)
        raw = _from_words(words, self._order(word_order))
        value = int(struct.unpack("<i", raw)[0])
        check_reading(
            value,
            minimum,
            maximum,
            field="read_i32",
            address=address,
            registers=words,
            client=self,
        )
        return self._done(value, tx)

    @mirrored
    async def read_u32(
        self,
        address: AddressLike,
        /,
        *,
        word_order: WordOrder | None = None,
        minimum: float | None = None,
        maximum: float | None = None,
    ) -> int:
        """Two consecutive registers as an unsigned 32-bit integer."""
        words, tx = await self._run(ReadWords(address, 2), mutates=False)
        raw = _from_words(words, self._order(word_order))
        value = int(struct.unpack("<I", raw)[0])
        check_reading(
            value,
            minimum,
            maximum,
            field="read_u32",
            address=address,
            registers=words,
            client=self,
        )
        return self._done(value, tx)

    @mirrored
    async def read_f32(
        self,
        address: AddressLike,
        /,
        *,
        word_order: WordOrder | None = None,
        minimum: float | None = None,
        maximum: float | None = None,
    ) -> float:
        """Two consecutive registers as one IEEE-754 single.

        Low word first, measured four ways on FX5U-32MT/DS fw 1.065: 1234.5 written as
        one double-word point put ``00 50 9A 44`` on the wire and read back
        ``D104 = 0x5000``, ``D105 = 0x449A`` (2026-09-06).

        ``minimum`` and ``maximum`` are optional plausibility bounds. A D register
        carries no type on the wire, so the *width* you ask for is never wrong and the
        *meaning* can be: two registers a PLC program writes as a ``REAL``, read here as
        a ``u32``, are a large plausible integer with end code ``0x0000``. This library
        will not guess which of the two it is looking at, and it will not sniff the
        bytes; it will hold the value to a range you declare and raise
        :class:`~aslmp.blocks.fields.SlmpImplausibleValueError` if it is outside.
        """
        words, tx = await self._run(ReadWords(address, 2), mutates=False)
        raw = _from_words(words, self._order(word_order))
        value = float(struct.unpack("<f", raw)[0])
        check_reading(
            value,
            minimum,
            maximum,
            field="read_f32",
            address=address,
            registers=words,
            client=self,
        )
        return self._done(value, tx)

    @mirrored
    async def read_f64(
        self,
        address: AddressLike,
        /,
        *,
        word_order: WordOrder | None = None,
        minimum: float | None = None,
        maximum: float | None = None,
    ) -> float:
        """Four consecutive registers as one IEEE-754 double."""
        words, tx = await self._run(ReadWords(address, 4), mutates=False)
        raw = _from_words(words, self._order(word_order))
        value = float(struct.unpack("<d", raw)[0])
        check_reading(
            value,
            minimum,
            maximum,
            field="read_f64",
            address=address,
            registers=words,
            client=self,
        )
        return self._done(value, tx)

    @mirrored
    async def read_str(
        self, address: AddressLike, /, *, length: int, encoding: str = "ascii"
    ) -> str:
        """``length`` **bytes** packed two per register, trimmed at the first NUL.

        ``length`` is required and is a count of BYTES, not of characters. It said
        "characters" here and in :meth:`write_str` for three revisions and was the byte
        window in every implementation, which is exactly how a ``shift_jis`` pair got cut
        in half: ``read_str('D110', length=3, encoding='shift_jis')`` over two registers
        holding ``0xA082 0xA282`` raised a bare ``UnicodeDecodeError`` (measured on
        FX5U-32MT/DS fw 1.065 from this host over TCP 5002, 2026-09-07). For ``ascii``
        and every other single-byte codec the two counts are the same number, which is
        why the wrong word survived. The window is now named for what it is and the cut
        is refused by :func:`~aslmp.commands.base.decoded` rather than escaping as a
        Python exception.

        ``length`` is required for the same reason ``--as`` is. A string region has no
        in-band length, so the alternatives to naming it are reading a fixed maximum --
        which returns the next field's bytes -- or scanning for a NUL, which is a second
        round trip whose answer can change between the two.

        The codec name is checked **before** the request is built, so a typo costs no
        round trip -- the same point in the call the write half has always checked it at.
        """
        count = _string_words(length)
        what = f"read_str({address}, length={length})"
        codec_name(encoding, what=what)
        words, tx = await self._run(ReadWords(address, count), mutates=False)
        raw = struct.pack(f"<{len(words)}H", *words)[:length]
        text = decoded(raw.split(b"\x00", 1)[0], encoding=encoding, what=what)
        return self._done(text, tx)

    @mirrored
    async def write_bit(
        self, address: AddressLike, value: bool, /, *, verify: bool = False
    ) -> None:
        """One bit device. ``0x1401`` in bit units. ``value`` is ``True`` or ``False``."""
        checked = boolean(value, what=f"write_bit({address})")
        _written, tx = await self._run(WriteBits(address, (checked,)), mutates=True)
        if verify:
            back = await self.read_bit(address)
            self._check(back, value, what=f"write_bit({address})")
        return self._ack(1, tx)

    @mirrored
    async def write_i16(
        self,
        address: AddressLike,
        value: int,
        /,
        *,
        minimum: float | None = None,
        maximum: float | None = None,
        verify: bool = False,
    ) -> None:
        """One register from a signed 16-bit integer. Never masked, never clamped.

        ``minimum``/``maximum`` are the same declared range :meth:`read_i16` holds a
        reading to, enforced here **before** anything is sent. See :meth:`write_f32`.
        """
        word = unsigned(value, bits=16, what=f"write_i16({address})", signed_field=True)
        _check_writing(
            value, minimum, maximum, field="write_i16", kind="i16", address=address
        )
        _written, tx = await self._run(WriteWords(address, (word,)), mutates=True)
        if verify:
            back = await self.read_i16(address)
            self._check(back, value, what=f"write_i16({address})")
        return self._ack(1, tx)

    @mirrored
    async def write_u16(
        self,
        address: AddressLike,
        value: int,
        /,
        *,
        minimum: float | None = None,
        maximum: float | None = None,
        verify: bool = False,
    ) -> None:
        """One register from an unsigned 16-bit integer.

        ``minimum``/``maximum`` are the same declared range :meth:`read_u16` holds a
        reading to, enforced here **before** anything is sent. See :meth:`write_f32`.
        """
        word = unsigned(value, bits=16, what=f"write_u16({address})", signed_field=False)
        _check_writing(
            value, minimum, maximum, field="write_u16", kind="u16", address=address
        )
        _written, tx = await self._run(WriteWords(address, (word,)), mutates=True)
        if verify:
            back = await self.read_u16(address)
            self._check(back, word, what=f"write_u16({address})")
        return self._ack(1, tx)

    @mirrored
    async def write_i32(
        self,
        address: AddressLike,
        value: int,
        /,
        *,
        word_order: WordOrder | None = None,
        minimum: float | None = None,
        maximum: float | None = None,
        verify: bool = False,
    ) -> None:
        """Two registers from a signed 32-bit integer.

        ``minimum``/``maximum`` are the same declared range :meth:`read_i32` holds a
        reading to, enforced here **before** anything is sent. See :meth:`write_f32`.
        """
        order = self._order(word_order)
        words = _to_words(struct.pack("<I", _fits(value, signed_field=True, address=address)),
                          order)
        _check_writing(
            value, minimum, maximum, field="write_i32", kind="i32", address=address
        )
        _written, tx = await self._run(WriteWords(address, words), mutates=True)
        if verify:
            read_back = await self.read_i32(address, word_order=order)
            self._check(read_back, value, what=f"write_i32({address})")
        return self._ack(2, tx)

    @mirrored
    async def write_u32(
        self,
        address: AddressLike,
        value: int,
        /,
        *,
        word_order: WordOrder | None = None,
        minimum: float | None = None,
        maximum: float | None = None,
        verify: bool = False,
    ) -> None:
        """Two registers from an unsigned 32-bit integer.

        ``minimum``/``maximum`` are the same declared range :meth:`read_u32` holds a
        reading to, enforced here **before** anything is sent. See :meth:`write_f32`.
        """
        order = self._order(word_order)
        words = _to_words(struct.pack("<I", _fits(value, signed_field=False, address=address)),
                          order)
        _check_writing(
            value, minimum, maximum, field="write_u32", kind="u32", address=address
        )
        _written, tx = await self._run(WriteWords(address, words), mutates=True)
        if verify:
            read_back = await self.read_u32(address, word_order=order)
            self._check(read_back, value, what=f"write_u32({address})")
        return self._ack(2, tx)

    @mirrored
    async def write_f32(
        self,
        address: AddressLike,
        value: float,
        /,
        *,
        word_order: WordOrder | None = None,
        minimum: float | None = None,
        maximum: float | None = None,
        verify: bool = False,
    ) -> None:
        """Two registers from one IEEE-754 single, low word first.

        ``minimum`` and ``maximum`` are the write-side half of :meth:`read_f32`'s
        plausibility bounds: the same declared range, judged **before** anything is sent,
        raising :class:`~aslmp.errors.SlmpValueRangeError` rather than the read's
        :class:`~aslmp.blocks.fields.SlmpImplausibleValueError` because here there is
        nothing to answer. A bound is a statement about what may be in that register and
        a write is the other way something gets there -- which is why a *block* field has
        had both directions since bounds existed, and why this per-call form having only
        the read half was the same defect one door along. Nothing is clamped to fit: a
        setpoint quietly pulled back to the top of its range is a different setpoint
        written to the plant.
        """
        order = self._order(word_order)
        packed = struct.pack("<f", real(value, bits=32, what=f"write_f32({address})"))
        _check_writing(
            value, minimum, maximum, field="write_f32", kind="f32", address=address
        )
        _written, tx = await self._run(
            WriteWords(address, _to_words(packed, order)), mutates=True
        )
        if verify:
            read_back = await self.read_f32(address, word_order=order)
            rounded = float(struct.unpack("<f", packed)[0])
            self._check(read_back, rounded, what=f"write_f32({address})")
        return self._ack(2, tx)

    @mirrored
    async def write_f64(
        self,
        address: AddressLike,
        value: float,
        /,
        *,
        word_order: WordOrder | None = None,
        minimum: float | None = None,
        maximum: float | None = None,
        verify: bool = False,
    ) -> None:
        """Four registers from one IEEE-754 double.

        ``minimum``/``maximum`` are the same declared range :meth:`read_f64` holds a
        reading to, enforced here **before** anything is sent. See :meth:`write_f32`.
        """
        order = self._order(word_order)
        packed = struct.pack("<d", real(value, bits=64, what=f"write_f64({address})"))
        words = _to_words(packed, order)
        _check_writing(
            value, minimum, maximum, field="write_f64", kind="f64", address=address
        )
        _written, tx = await self._run(WriteWords(address, words), mutates=True)
        if verify:
            read_back = await self.read_f64(address, word_order=order)
            self._check(read_back, float(value), what=f"write_f64({address})")
        return self._ack(4, tx)

    @mirrored
    async def write_str(
        self,
        address: AddressLike,
        value: str,
        /,
        *,
        length: int,
        encoding: str = "ascii",
        verify: bool = False,
    ) -> None:
        """``length`` **bytes**, NUL padded, two per register.

        ``length`` is a count of BYTES, not of characters, and always was: the comparison
        below is against ``len(raw)``, the encoded length. It is named for that now, on
        both halves of the pair -- see :meth:`read_str`, where the same wrong word cut a
        ``shift_jis`` character in half on the way back.

        A string longer than ``length`` raises rather than being truncated to fit: a
        silently shortened part number is a wrong part number.

        ``value`` must be a ``str`` that ``encoding`` can actually carry. Passing
        ``b"x"``, a character ASCII has no room for, or a codec name that does not exist
        raises inside the DESIGN section 3.1 tree
        (:func:`~aslmp.commands.base.encoded`) rather than as the bare
        ``AttributeError``, ``UnicodeEncodeError`` or ``LookupError`` that
        ``value.encode(encoding)`` used to let out of a write path.
        :meth:`read_str` now refuses the mirror-image failures through
        :func:`~aslmp.commands.base.decoded`; it did not when this paragraph was written,
        which is the whole reason the enforcement test in
        ``tests/unit/test_read_write_symmetry.py`` exists.
        """
        raw = encoded(value, encoding=encoding, what=f"write_str({address})")
        if len(raw) > length:
            raise SlmpConfigurationError(
                f"write_str({address}, length={length}) was given {len(raw)} byte(s) of "
                f"{encoding}. Nothing here truncates to fit."
            )
        padded = raw.ljust(_string_words(length) * 2, b"\x00")
        words = struct.unpack(f"<{len(padded) // 2}H", padded)
        _written, tx = await self._run(WriteWords(address, words), mutates=True)
        if verify:
            read_back = await self.read_str(address, length=length, encoding=encoding)
            self._check(read_back, value, what=f"write_str({address})")
        return self._ack(len(words), tx)

    # ====================================================================================
    # Batch arrays (0401 / 1401)
    # ====================================================================================

    @mirrored
    async def read_words(self, address: AddressLike, /, count: int) -> tuple[int, ...]:
        """``count`` consecutive registers, as unsigned words. Batch ceiling 960 words."""
        words, tx = await self._run(ReadWords(address, count), mutates=False)
        return self._done(words, tx)

    @mirrored
    async def read_bits(self, address: AddressLike, /, count: int) -> tuple[bool, ...]:
        """``count`` consecutive bit devices. Batch ceiling 3584 bits on an FX5U."""
        values, tx = await self._run(ReadBits(address, count), mutates=False)
        return self._done(values, tx)

    @mirrored
    async def read_f32_array(
        self, address: AddressLike, /, count: int, *, word_order: WordOrder | None = None
    ) -> tuple[float, ...]:
        """``count`` IEEE-754 singles from ``2 * count`` consecutive registers."""
        order = self._order(word_order)
        words, tx = await self._run(ReadWords(address, 2 * count), mutates=False)
        values = tuple(
            float(struct.unpack("<f", _from_words(words[index : index + 2], order))[0])
            for index in range(0, len(words), 2)
        )
        return self._done(values, tx)

    @mirrored
    async def write_words(self, address: AddressLike, values: Sequence[int], /) -> None:
        """``len(values)`` consecutive registers.

        A raw register names no type, so ``-1`` and ``65535`` are the same sixteen bits
        and both are accepted; ``70000`` and ``1.5`` are not, and both raise
        :class:`~aslmp.errors.SlmpValueRangeError` before anything is built.
        """
        checked = tuple(
            # signed_field=None: this is the raw-register door, and it is the only
            # kind of write that has no declared type to enforce.
            unsigned(
                value,
                bits=16,
                what=f"write_words({address}) value {index}",
                signed_field=None,
            )
            for index, value in enumerate(values)
        )
        _written, tx = await self._run(WriteWords(address, checked), mutates=True)
        return self._ack(len(checked), tx)

    @mirrored
    async def write_bits(self, address: AddressLike, values: Sequence[bool], /) -> None:
        """``len(values)`` consecutive bit devices. Each is ``True`` or ``False``, exactly.

        ``[2]``, ``[-1]`` and ``["yes"]`` raise rather than all writing a 1: Python's
        truthiness is not the PLC's, and a caller who passed the wrong element of a list
        would otherwise never find out.
        """
        checked = tuple(
            boolean(value, what=f"write_bits({address}) value {index}")
            for index, value in enumerate(values)
        )
        _written, tx = await self._run(WriteBits(address, checked), mutates=True)
        return self._ack(len(checked), tx)

    @mirrored
    async def write_f32_array(
        self,
        address: AddressLike,
        values: Sequence[float],
        /,
        *,
        word_order: WordOrder | None = None,
    ) -> None:
        """``len(values)`` IEEE-754 singles into ``2 * len(values)`` registers."""
        order = self._order(word_order)
        words: list[int] = []
        for index, value in enumerate(values):
            checked = real(value, bits=32, what=f"write_f32_array({address}) value {index}")
            words.extend(_to_words(struct.pack("<f", checked), order))
        _written, tx = await self._run(WriteWords(address, tuple(words)), mutates=True)
        return self._ack(len(words), tx)

    # ====================================================================================
    # Random access (0403 / 1402) -- the control-loop primitive
    # ====================================================================================

    @overload
    async def read_random(
        self, points: Sequence[RandomPoint], /, *, allow_split: Literal[False] = False
    ) -> RandomReading: ...

    @overload
    async def read_random(
        self, points: Sequence[RandomPoint], /, *, allow_split: Literal[True]
    ) -> RandomReading | SplitReading: ...

    @mirrored
    async def read_random(
        self, points: Sequence[RandomPoint], /, *, allow_split: bool = False
    ) -> RandomReading | SplitReading:
        """Scattered word and double-word points in **one** snapshot. Results POSITIONAL.

        The wire demands every word specification before every double-word one and
        carries no framing between the groups; the command sorts, encodes, decodes and
        restores the caller's order, so ``reading[2]`` is the third point as written.

        ``allow_split=True`` returns a **different type** when the request does not fit
        the CPU's point ceiling, because splitting destroys the single-snapshot atomicity
        that is the entire reason to send a ``0403``. With the default
        ``allow_split=False`` an over-budget request raises
        :class:`~aslmp.errors.SlmpPointLimitError` and nothing is sent.
        """
        requested = tuple(points)
        if allow_split and len(requested) > self._random_ceiling():
            split = await self._read_random_split(requested)
            return self._done(split, split.transactions[-1])
        resolved = tuple(point.resolve(self._ctx) for point in requested)
        values, tx = await self._run(ReadRandom(requested), mutates=False)
        return self._done(RandomReading(resolved, values, tx), tx)

    @mirrored
    async def write_random(self, writes: Sequence[RandomWrite], /) -> None:
        """Scattered word and double-word writes in one ``1402``.

        The budget is weighted, not flat: ``word x 12 + dword x 14 <= 1920`` on an FX5U
        (measured). A flat count is wrong in both directions -- 160 word points fit and
        138 double-word points do not.

        Each value is held to the type **its own point names**: an ``i16`` point given
        40000 raises rather than putting ``0x9C40`` on the wire for the PLC to read back
        as ``-25536``, exactly as :meth:`write_i16` does. A ``u16`` point given ``-1``
        raises too, exactly as :meth:`write_u16` does -- it did not until the pair was
        looked at together. ``RandomWrite(word('D101', kind='u16'), -1)`` masked to
        ``0xFFFF`` and this method sent it, and ``D101`` read back 65535 on the bench
        (FX5U-32MT/DS fw 1.065 from this host over TCP 5002, 2026-09-07) from a call the
        typed door refuses outright. The raw-register door, where ``-1`` and ``65535``
        are the same sixteen bits, is :meth:`write_words`, and it is the only one.
        """
        checked = tuple(writes)
        for index, item in enumerate(checked):
            _check_point_value(item, index)
        _written, tx = await self._run(WriteRandom(checked), mutates=True)
        return self._ack(len(checked), tx)

    async def _read_random_split(self, points: tuple[RandomPoint, ...]) -> SplitReading:
        """Several ``0403``s. The caller opted in and gets a type that says so."""
        ceiling = self._random_ceiling()
        resolved = tuple(point.resolve(self._ctx) for point in points)
        values: list[RandomValue] = []
        transactions: list[Transaction] = []
        for start in range(0, len(points), ceiling):
            chunk_values, tx = await self._run(
                ReadRandom(points[start : start + ceiling]), mutates=False
            )
            values.extend(chunk_values)
            transactions.append(tx)
        first = transactions[0].timing.sent_at
        last = transactions[-1].timing.received_at
        span = 0 if last is None else last - first
        return SplitReading(resolved, tuple(values), tuple(transactions), span)

    # ====================================================================================
    # Block access (0406 / 1406)
    # ====================================================================================

    @mirrored
    async def read_blocks(
        self, blocks: Sequence[BlockSpec], /
    ) -> tuple[tuple[int, ...], ...]:
        """Several contiguous runs in one frame; one tuple of words per block, in order."""
        values, tx = await self._run(ReadBlocks(tuple(blocks)), mutates=False)
        return self._done(values, tx)

    @mirrored
    async def write_blocks(self, blocks: Sequence[BlockWrite], /) -> None:
        """Several contiguous runs written in one frame."""
        _written, tx = await self._run(WriteBlocks(tuple(blocks)), mutates=True)
        return self._ack(sum(block.points for block in blocks), tx)

    # ====================================================================================
    # Blocks (DESIGN.md section 2.7) -- thin delegates onto aslmp.blocks.plan
    # ====================================================================================

    @overload
    def bind(
        self,
        block: type[B],
        /,
        *,
        base: AddressLike | None = ...,
        allow_split: Literal[False] = ...,
    ) -> BlockPlan[B]: ...

    @overload
    def bind(
        self,
        block: type[B],
        /,
        *,
        base: AddressLike | None = ...,
        allow_split: Literal[True],
    ) -> BlockPlan[B] | SplitBlockPlan[B]: ...

    def bind(
        self,
        block: type[B],
        /,
        *,
        base: AddressLike | None = None,
        allow_split: bool = False,
    ) -> BlockPlan[B] | SplitBlockPlan[B]:
        """Bind a ``@plc_block`` class to this client. **Synchronous: no I/O.**

        Exactly :func:`aslmp.blocks.plan.bind` with this client already supplied. The
        import is deferred because ``blocks.plan`` imports this module -- they are
        declared peers at Layer 5, so the edge is legal, but only one of them may name
        the other at module scope.

        ``allow_split`` is overloaded rather than widened, so a caller who did not ask
        for a split never has to narrow a union: a
        :class:`~aslmp.blocks.plan.SplitBlockPlan` returns a
        :class:`~aslmp.blocks.plan.Split`, which is deliberately not a ``B``, because
        its fields were not one snapshot.
        """
        from aslmp.blocks.plan import bind as _bind

        if allow_split:
            return _bind(self, block, base=base, allow_split=True)
        return _bind(self, block, base=base)

    def _own_plan(self, plan: BlockPlan[B], what: str) -> BlockPlan[B]:
        """Refuse a plan that is bound to a different client. Nothing is sent.

        **Why refused rather than retargeted.** A bound plan is not a description of a
        block; it is a prebuilt ``0403`` frame plus a compiled decoder, and both were
        built from *one* client's profile, codec, spec format, frame type, route,
        monitoring timer and identified model code. Re-deriving all of that against
        ``self`` on every call is exactly the work the prebuilt plan exists to do once,
        and sending it as it stands down another client's socket is worse: an FX5U and an
        iQ-R both answer ``0x0000`` to the same bytes and mean different devices by them.
        So there is no honest way to "use the receiver", and the mismatch is the bug.

        Until this guard existed, ``self`` was unused: ``plc.read_block(plan)`` and
        ``plc.write_block(plan, value)`` transacted on the client the plan was bound to,
        whichever client they were called on. Verified on FX5U-32MT/DS fw 1.065 from this
        host over TCP 5002 (2026-09-07): a ``Plc`` that had never been connected, pointed
        at a host that does not exist, returned a populated block and reported a
        successful write, while the bound client's counters moved and ``D100``/``D101``
        on the real CPU changed to the values passed to the ghost. Writing to the wrong
        PLC and returning success is the worst outcome this package has.
        """
        if plan.plc is self:
            return plan
        raise SlmpConfigurationError(
            f"{what} was called on {self.name} with a plan bound to "
            f"{plan.plc.name}. A bound plan is a prebuilt frame and a compiled decoder "
            f"built from one client's profile, coding, frame type, route and identified "
            f"CPU; it is not portable, and nothing here rebinds it on your behalf -- "
            f"that would send bytes built for one CPU's device numbering to another, "
            f"which both answer 0x0000 to. Nothing was sent. Call plan.read() on the "
            f"plan itself, or bind the block against this client: "
            f"{self.name}.bind({plan.layout.block_name})."
        )

    def _own_registration(
        self, registration: MonitorRegistration, what: str
    ) -> MonitorRegistration:
        """Refuse a monitor registration this client did not make. Nothing is sent.

        :meth:`_own_plan`'s weaker cousin, and the same defect one command along.
        ``ExecuteMonitor.validate`` checks the registration's **profile key**, which
        answers "is this the same kind of CPU" and not "is this the same CPU": two
        clients on ``melsec:iq-f/fx5u`` pointed at two different FX5Us share that key
        exactly. A ``0802`` carries no device specification at all -- the whole request
        is the command and the subcommand -- so the registration IS the decoder, and one
        made against line 1 executed on line 2 returns line 2's registers labelled with
        line 1's addresses, end code ``0x0000``, nothing anywhere to say so.

        A registration with no owner is refused too, and deliberately: the only way to
        get one is to build it by hand, which means it never described a ``0801`` this
        CPU acknowledged. Registering again costs one round trip and is the honest fix.
        """
        if registration.owner is self:
            return registration
        made_by = (
            "no client -- it was constructed directly"
            if registration.owner is None
            else getattr(registration.owner, "name", repr(registration.owner))
        )
        raise SlmpConfigurationError(
            f"{what} was called on {self.name} with a registration made by {made_by}. A "
            f"0802 request carries no device specification, so the registration is the "
            f"only thing that can parse the response; executed against a different CPU "
            f"it decodes that CPU's registers under this list's addresses and answers "
            f"0x0000. The shared profile key {registration.profile_key} does not make "
            f"two CPUs the same CPU. Nothing was sent. Call "
            f"{self.name}.monitor_register(...) and use what it returns."
        )

    async def read_block(self, plan: BlockPlan[B], /) -> B:
        """One ``0403`` from an already-bound plan: :meth:`BlockPlan.read`.

        Takes a :class:`~aslmp.blocks.plan.BlockPlan` and never a class, because binding
        on every cycle would rebuild and revalidate the frame every cycle, which is the
        whole cost the prebuilt plan exists to pay once.

        The plan must be bound to **this** client; one bound to another raises
        :class:`~aslmp.errors.SlmpConfigurationError` and sends nothing (:meth:`_own_plan`).
        """
        return await self._own_plan(plan, "read_block()").read()

    async def write_block(self, plan: BlockPlan[B], value: B, /) -> None:
        """One ``1402`` writing every field of ``value``: :meth:`BlockPlan.write_block`.

        The plan must be bound to **this** client; one bound to another raises
        :class:`~aslmp.errors.SlmpConfigurationError` and sends nothing (:meth:`_own_plan`).
        """
        await self._own_plan(plan, "write_block()").write_block(value)

    # ====================================================================================
    # Monitor (0801 / 0802) -- capability gated, NEVER emulated
    # ====================================================================================

    @mirrored
    async def monitor_register(
        self, points: Sequence[RandomPoint], /
    ) -> MonitorRegistration:
        """``0801``: register a point list for later ``0802`` reads.

        Refused pre-transport on an iQ-F, which answers ``0xC059`` -- not the ``0xC05D``
        "monitor not registered" a reader of the generic reference would expect. It is
        never emulated with a ``0403``: substituting a different command that returns
        similar-looking data is exactly the silent recovery this library forbids.

        The returned registration is stamped with **this** client, and
        :meth:`monitor_read` will execute it on no other (:meth:`_own_registration`).
        """
        registration, tx = await self._run(RegisterMonitor(tuple(points)), mutates=True)
        return self._done(registration.owned_by(self), tx)

    @mirrored
    async def monitor_read(
        self, registration: MonitorRegistration, /
    ) -> RandomReading:
        """``0802``: read the registered list. Positional, exactly like ``read_random``.

        The registration must have been made by **this** client; one made by another
        raises :class:`~aslmp.errors.SlmpConfigurationError` and sends nothing
        (:meth:`_own_registration`).
        """
        owned = self._own_registration(registration, "monitor_read()")
        values, tx = await self._run(ExecuteMonitor(owned), mutates=False)
        resolved = tuple(point.resolve(self._ctx) for point in owned.points)
        return self._done(RandomReading(resolved, values, tx), tx)

    # ====================================================================================
    # Diagnostics
    # ====================================================================================

    @mirrored
    async def self_test(self, payload: bytes = DEFAULT_LOOPBACK, /) -> bytes:
        """``0619``: ask the module to echo ``payload``. Zero side effects.

        The default is ``b"ABCD"`` -- ``41 42 43 44``, the exact bytes proven on the
        bench. Both manuals restrict loopback data to ``'0'``-``'9'`` and ``'A'``-``'F'``
        and ``Codec.loopback_payload_ok`` enforces it, against our own defaults included.
        """
        echo, tx = await self._run(SelfTest(payload), mutates=False)
        return self._done(echo, tx)

    @mirrored
    async def ping(self) -> float:
        """One ``0619`` round trip, in milliseconds. The liveness probe.

        Zero side effects, and its latency equals a real read (~7 ms on FX5U-32MT/DS fw
        1.065), which is what makes it a usable baseline rather than a lower bound.
        """
        nonce = self._counters.probes_sent & 0xFFFF
        self._counters.probes_sent += 1
        _echo, tx = await self._run(
            SelfTest(b"0619" + f"{nonce:04X}".encode("ascii")), mutates=False
        )
        return self._done(tx.timing.wire_ms, tx)

    @mirrored
    async def read_type_name(self) -> CpuIdentity:
        """``0101``: which CPU is on the other end, with its family resolved.

        The family comes from the profile that claims the model code, never from parsing
        the name: the name is a marketing string whose shape has changed between
        families, and the model code is what decides whether ``Y20`` is output 16 or 32.
        """
        type_name, tx = await self._run(ReadTypeName(), mutates=False)
        return self._done(CpuIdentity.of(type_name), tx)

    @mirrored
    async def read_cpu_status(self) -> CpuStatus:
        """The CPU operating status, from SD203 read as an ordinary word.

        SLMP has no "what state are you in" command. An undocumented value raises rather
        than being rounded to the nearest state: "the CPU is stopped" is not something to
        say on no evidence.
        """
        words, tx = await self._run(cpu_status_command(), mutates=False)
        return self._done(decode_cpu_status(words), tx)

    @mirrored
    async def clear_error(self) -> None:
        """``1617``: clear the own-station error code and the error LED."""
        _nothing, tx = await self._run(ClearError(), mutates=True)
        return self._ack(0, tx)

    # ====================================================================================
    # The escape hatch
    # ====================================================================================

    @overload
    async def raw_command(
        self,
        command: int,
        subcommand: int,
        payload: bytes = b"",
        /,
        *,
        mutates: bool = True,
        expect_response: Literal[True] = True,
    ) -> RawResponse: ...

    @overload
    async def raw_command(
        self,
        command: int,
        subcommand: int,
        payload: bytes = b"",
        /,
        *,
        mutates: bool = True,
        expect_response: Literal[False],
    ) -> None: ...

    @mirrored
    async def raw_command(
        self,
        command: int,
        subcommand: int,
        payload: bytes = b"",
        /,
        *,
        mutates: bool = True,
        expect_response: bool = True,
    ) -> RawResponse | None:
        """Send arbitrary command bytes. **Bypasses command validation and nothing else.**

        The frame, the ``L`` guard, the one-in-flight gate, the end-code raise and the
        transaction record all still apply. It exists because this CPU accepted requests
        its own manual forbids, and a library that can only send what it approves of
        cannot be used to find out what a CPU really does.

        ``expect_response=False`` is overloaded to return ``None`` rather than widening
        the normal return type, for the same reason ``allow_split`` is: a caller who did
        not ask for silence never has to narrow a union. It also **retires the connection**
        -- see :meth:`Connection.retire_after_silent_exchange`.

        **The one thing it does not bypass is the remote-control interlock.** ``0x1001``
        through ``0x1006`` are refused here unless the client was built with
        ``allow_remote_control=True``, exactly as :attr:`remote` refuses them. Validation
        is a property of the *command* and the interlock is a property of the *client*, so
        ``_RawCommand.validate()`` being a deliberate no-op cannot be the whole story:
        before this check, ``plc.remote.run()`` raised and ``plc.raw_command(0x1001, 0)``
        halted or started the same machine on the same client (verified on FX5U-32MT/DS
        fw 1.065, 2026-09-07). An escape hatch for finding out what a CPU really does is
        not an escape hatch from "this library can stop a running plant".
        """
        self._require_interlock_for(command)
        if not expect_response:
            silent = _SilentRawCommand(command, subcommand, payload, mutates)
            return self._done(None, await self._run_silent(silent, mutates=mutates))
        response, tx = await self._run(
            _RawCommand(command, subcommand, payload, mutates), mutates=mutates
        )
        return self._done(response, tx)

    def _require_interlock_for(self, command: int) -> None:
        """Refuse a remote-control command code on a client that was not unlocked for one."""
        if command not in REMOTE_CONTROL_COMMANDS or self._ctx.allow_remote_control:
            return
        raise SlmpConfigurationError(
            f"raw_command(0x{command:04X}, ...) is "
            f"{REMOTE_CONTROL_COMMANDS[command]}, and this client was not constructed "
            f"with allow_remote_control=True. The interlock is a property of the client "
            f"and not of the command, so bypassing command validation does not bypass "
            f"it: this library can halt a plant over an unauthenticated cleartext "
            f"socket. Nothing was sent. Use plc.remote, which also verifies the CPU "
            f"actually reached the state it was asked for -- Mitsubishi documents "
            f"Remote RUN with the switch in STOP as completing normally while the CPU "
            f"does not enter RUN (SH(NA)-080956ENG-M p.131)."
        )


# ========================================================================================
# Remote control -- interlocked, verified by default
# ========================================================================================


@final
class RemoteControl:
    """``plc.remote``: the commands that can stop a running machine.

    Every method raises :class:`~aslmp.errors.SlmpConfigurationError` unless the client
    was constructed with ``allow_remote_control=True`` (graft G11). This library can halt
    a plant over an unauthenticated cleartext socket; the interlock is explicit and per
    client, never per call.

    ``verify=True`` is the **default** on run, stop and pause (graft G15). Mitsubishi
    documents Remote RUN with the switch in STOP as completing normally while "the access
    destination does not become the RUN state" (SH(NA)-080956ENG-M p.131) -- a successful
    end code on a lie. The second round trip that reads SD203 back is the correct price.

    The whole surface carries ``Provenance.MANUAL``: these codes were deliberately never
    sent to the bench.
    """

    __slots__ = ("_last", "_plc")

    def __init__(self, plc: Plc) -> None:
        self._plc = plc
        self._last: RemoteResult | None = None

    @property
    def last(self) -> RemoteResult | None:
        """The most recent remote action, its transaction, and what SD203 then said.

        Remote control is a separate object and is not part of the generated
        ``plc.timed`` surface, so this is where its transaction record lives.
        """
        return self._last

    async def status(self) -> CpuStatus:
        """SD203, read as an ordinary word. One round trip, no side effects."""
        return await self._plc.read_cpu_status()

    async def run(
        self,
        *,
        mode: RunMode = RunMode.NOT_FORCED,
        clear: ClearMode = ClearMode.NONE,
        verify: bool = True,
        settle: float | None = None,
    ) -> CpuStatus:
        """``1001`` Remote RUN, then SD203 until it agrees, unless ``verify=False``.

        Entering RUN is not instantaneous: measured on FX5U-32MT/DS fw 1.065, SD203 still
        reported STOP on the first poll in two cycles out of three. ``settle`` bounds how
        long the status is observed (default :data:`REMOTE_SETTLE_SECONDS`); the command
        itself is sent once and never re-sent.
        """
        return await self._apply(
            RemoteRun(mode=mode, clear=clear), CpuStatus.RUN, verify, "remote.run()", settle
        )

    async def stop(self, *, verify: bool = True, settle: float | None = None) -> CpuStatus:
        """``1002`` Remote STOP, then SD203 unless ``verify=False``.

        Leaving RUN was synchronous in every cycle measured, so this rarely polls twice.
        """
        return await self._apply(
            RemoteStop(), CpuStatus.STOP, verify, "remote.stop()", settle
        )

    async def pause(
        self,
        *,
        mode: RunMode = RunMode.NOT_FORCED,
        verify: bool = True,
        settle: float | None = None,
    ) -> CpuStatus:
        """``1003`` Remote PAUSE, then SD203 unless ``verify=False``."""
        return await self._apply(
            RemotePause(mode=mode), CpuStatus.PAUSE, verify, "remote.pause()", settle
        )

    async def latch_clear(self) -> None:
        """``1005`` Remote Latch Clear. Legal only from STOP; the CPU enforces it."""
        _nothing, tx = await self._plc._run(RemoteLatchClear(), mutates=True)
        self._last = RemoteResult("remote.latch_clear()", None, False, tx)

    async def reset(self) -> ResetOutcome:
        """``1006`` Remote Reset. **An absent response is the expected outcome.**

        SH(NA)-080956ENG-M p.136: on success "the response request is not be sent back to
        the external device", and over TCP the connection goes with it. This never raises
        on silence, and it is the one command in the package for which that is true.

        Two preconditions the wire cannot tell you about: the CPU must be in STOP, and the
        GX Works3 *Remote Reset Setting* must be Enabled. Its default is Disable, which is
        the most likely reason a ``1006`` fails on a fresh FX5U.
        """
        plc = self._plc
        try:
            tx = await plc._run_silent(RemoteReset(), mutates=True)
        except SlmpTransportError:
            await plc.aclose()
            return ResetOutcome(responded=False, connection_closed=True)
        await plc.aclose()
        return ResetOutcome(responded=False, connection_closed=True, tx=tx)

    async def unlock(self, password: str, /) -> None:
        """``1630``: unlock the remote password. Literal characters; no encryption."""
        await self._plc._run(UnlockPassword(password), mutates=True)

    async def lock(self, password: str, /) -> None:
        """``1631``: lock this connection again."""
        await self._plc._run(LockPassword(password), mutates=True)

    async def _apply(
        self,
        command: Command[None],
        wanted: CpuStatus,
        verify: bool,
        what: str,
        settle: float | None = None,
    ) -> CpuStatus:
        plc = self._plc
        _nothing, tx = await plc._run(command, mutates=True)
        if not verify:
            self._last = RemoteResult(what, None, False, tx)
            return wanted

        # Poll SD203 to a bounded deadline rather than reading it once.
        #
        # This is NOT a retry and NOT silent recovery: the command is sent exactly once and
        # is never re-sent. What is repeated is the *observation*, because the CPU changes
        # state asynchronously and a single immediate read reports a state that has not
        # settled yet. Concluding "it did not work" from that read would be as wrong as
        # trusting the end code, in the opposite direction.
        #
        # Measured on FX5U-32MT/DS fw 1.065, 2026-09-07, three cycles: STOP is reported
        # correctly on the FIRST poll every time (~18-21 ms after the command), but RUN
        # still reported STOP on the first poll in 2 of 3 cycles and only reached RUN on
        # the second, 25-33 ms after the command. The asymmetry is real: entering RUN takes
        # the CPU an extra scan or two, leaving it does not.
        deadline_s = REMOTE_SETTLE_SECONDS if settle is None else settle
        deadline = time.monotonic() + deadline_s
        polls = 0
        while True:
            words, verify_tx = await plc._run(cpu_status_command(), mutates=False)
            actual = decode_cpu_status(words)
            polls += 1
            if actual is wanted or time.monotonic() >= deadline:
                break
            await asyncio.sleep(REMOTE_POLL_INTERVAL_SECONDS)

        self._last = RemoteResult(what, actual, True, tx, verify_tx, polls=polls)
        if actual is not wanted:
            raise SlmpRemoteStateNotReachedError(
                f"{what} was answered end code 0x0000, but SD203 still reported {actual} "
                f"and not {wanted} after {polls} poll(s) over {deadline_s * 1000:.0f} ms. "
                f"Mitsubishi documents Remote RUN with the switch in STOP as completing "
                f"normally while the access destination does not enter the RUN state "
                f"(SH(NA)-080956ENG-M p.131), so the end code is not evidence and this "
                f"library does not report it as one. Check the RUN/STOP switch position "
                f"first. If the CPU does reach {wanted} slightly later, this deadline is "
                f"too short for it: pass settle= to widen it.",
                requested=str(wanted),
                actual=str(actual),
                diagnostics=Diagnostics(client=plc, tx=verify_tx),
            )
        return actual


# ========================================================================================
# Module-private helpers
# ========================================================================================


def _check_writing(
    value: float,
    minimum: float | None,
    maximum: float | None,
    *,
    field: str,
    kind: str,
    address: AddressLike,
) -> None:
    """Hold one value about to be sent to the bounds a caller passed. Sends nothing.

    The write-side twin of :func:`~aslmp.blocks.fields.check_reading`, and it exists
    because that function had no twin: ``read_f32("D2", minimum=0, maximum=100)`` refused
    an implausible reading while ``write_f32("D2", 1e6)`` had no way to say the same
    thing, on the half that changes the plant. A block field has had both directions
    since bounds were introduced (:func:`aslmp.blocks.plan._refuse_out_of_range`); the
    per-call form had only one, which is the asymmetry
    ``tests/unit/test_read_write_symmetry.py`` now enforces for every typed pair.

    Same range, opposite error class, deliberately.
    :class:`~aslmp.errors.SlmpValueRangeError` here, because nothing was sent and the
    caller's own value is what is wrong;
    :class:`~aslmp.blocks.fields.SlmpImplausibleValueError` on the read, because the CPU
    answered ``0x0000`` and the disagreement is between the registers and the
    declaration. Both are what the block path already raises in the two directions.

    Returns immediately when neither bound was given, which is what this costs every
    caller who does not use the feature: one call and two ``is None`` tests.
    """
    if minimum is None and maximum is None:
        return
    # Bounds.__post_init__ is the one table for "that declaration cannot fire": a NaN
    # bound, a non-number, minimum above maximum. The read path open-codes the same
    # three refusals inside check_reading(); test_read_write_symmetry.py asserts that
    # both doors refuse the same malformed declarations, so the two cannot drift.
    limits = Bounds(minimum, maximum)
    if limits.excludes(value):
        raise refuse_write(
            field=field, label=kind, bounds=limits, value=value, address=str(address)
        )


def _check_point_value(write: RandomWrite, index: int) -> None:
    """Hold one ``1402`` value to the type its own access point names. Sends nothing.

    This function once carried its own kind-to-domain table, which is one table too
    many: :meth:`~aslmp.commands.random.RandomWrite.wire_value` enforces the same rule
    at encode time and was reading no table at all, so the two disagreed and the public
    door was the only one that refused. The table now lives beside the kinds it
    describes, as :attr:`~aslmp.commands.random.RandomPoint.signed_field`, and both
    read it. What survives here is the *message*: refused at the call, naming the
    caller's own value index, before a frame exists.
    """
    what = f"write_random() value {index} at {write.point}"
    if write.point.kind == "f32":
        real(write.value, bits=32, what=what)
        return
    unsigned(
        write.value,
        bits=write.point.width.bits,
        what=what,
        signed_field=write.point.signed_field,
    )


def _fits(value: object, *, signed_field: bool, address: AddressLike) -> int:
    """``value`` as the unsigned 32-bit field a 32-bit write puts on the wire.

    Exactly :func:`~aslmp.commands.base.unsigned` at 32 bits, named here because the
    address makes a better message than the point does. Returning the *wire* value rather
    than the caller's is what lets both ``write_i32`` and ``write_u32`` pack ``"<I"``: the
    two's complement of a negative signed value and its unsigned rendering are the same
    four bytes, and one packing format is one fewer place the two can disagree.

    Never masked and never truncated: ``pymcprotocol`` writes ``0x1FFFF`` into a 16-bit
    register as ``0xFFFF`` and reports success. ``write_i32(3_000_000_000)`` raises here
    rather than reading back ``-1294967296``.
    """
    return unsigned(
        value,
        bits=32,
        what=f"writing to {address} as a 32-bit field",
        signed_field=signed_field,
    )


def _string_words(length: int) -> int:
    """Registers needed for ``length`` **bytes**, two per register.

    Bytes, not characters, on both halves of the pair: this is the arithmetic
    :meth:`Plc.read_str` slices its response with and :meth:`Plc.write_str` measures its
    encoded value against, and it was documented as characters in both while being
    neither.
    """
    if not isinstance(length, int) or isinstance(length, bool) or length < 1:
        raise SlmpConfigurationError(
            f"a string length is a positive count of bytes (two per register), not "
            f"{length!r}."
        )
    return (length + 1) // 2


def _checked_timer(timer: MonitoringTimer, timeout: float) -> MonitoringTimer:
    """DESIGN.md section 4.6: the client deadline must outlast the PLC's own timer.

    Otherwise every PLC-side timeout arrives as a bare socket timeout instead of the
    decodable end code the CPU was about to send, and the one mechanism that can tell "the
    CPU gave up" from "the network ate it" is thrown away.
    """
    if timer.units == 0 or timeout > timer.seconds_value:
        return timer
    raise SlmpMonitoringTimerError(
        f"the client deadline (timeout={timeout} s) is not longer than the SLMP "
        f"monitoring timer ({timer}). They are two independent mechanisms: the monitoring "
        f"timer makes the PLC give up and answer with a decodable end code, and the "
        f"deadline makes this process give up and raise. A deadline that expires first "
        f"converts every PLC-side timeout into a bare socket timeout."
    )


def _override_capabilities(
    profile: CpuProfile, overrides: Mapping[Capability, str]
) -> CpuProfile:
    """Turn named refusals into inferred capabilities, keeping the reason on the record.

    The escape hatch for a firmware that gained something our table says it lacks. The
    override is :attr:`~aslmp.wire.citations.Provenance.INFERRED` and carries the caller's
    own reason, so ``aslmp cite`` prints who claimed it and why -- rather than the
    capability quietly appearing as though somebody had measured it.
    """
    merged = dict(profile.capabilities)
    for capability, reason in overrides.items():
        if not isinstance(capability, Capability):
            raise SlmpConfigurationError(
                f"capability_overrides is keyed by Capability, not "
                f"{type(capability).__name__}."
            )
        if not reason.strip():
            raise SlmpConfigurationError(
                f"capability_overrides[{capability.name}] must say why. An override with "
                f"no reason is indistinguishable from a measurement six months later."
            )
        merged[capability] = Evidence(
            provenance=Provenance.INFERRED,
            source=f"capability_overrides on {profile.key}",
            note=reason,
        )
    return profile.replace(capabilities=merged)


def _build_transport(
    host: str,
    port: int,
    *,
    kind: TransportKind,
    nodelay: bool,
    carries_serial: bool,
    udp_pipeline_depth: int = 1,
) -> TcpTransport | UdpTransport:
    """One transport per configured entry. TCP is one connection, always.

    A second TCP connection to a one-entry SLMP configuration completes and is then FINed,
    so connection pooling against one entry cannot work and the transport's
    ``max_in_flight`` is 1 and not configurable. UDP has no such limit, but pipelining
    needs a serial No. to correlate by and is refused on 3E at construction.

    ``udp_pipeline_depth`` is refused on TCP rather than ignored: silently accepting a
    number that cannot take effect is exactly the "wrong data reported as success" shape
    this library exists to refuse. On UDP it becomes the gate capacity. Measured on
    FX5U-32MT/DS fw 1.065: 4E/UDP bursts are clean to depth 32 and lose above it with no
    end code and no ICMP -- ~31% at 64 over Wi-Fi (2026-09-06), and everything past the
    32nd request when re-measured wired (2026-09-07) -- which is why the ceiling is
    :data:`~aslmp.transport.base.MAX_UDP_PIPELINE_DEPTH` and the default is 1.
    """
    if kind is TransportKind.TCP:
        if udp_pipeline_depth != 1:
            raise SlmpConfigurationError(
                f"udp_pipeline_depth={udp_pipeline_depth} was given with "
                f"transport=TCP, where it cannot take effect. On FX5U-32MT/DS fw 1.065 "
                f"two requests written before the first response is read return ONE "
                f"response, for the LAST request, with end code 0x0000, so TCP is "
                f"one-in-flight structurally. Use transport=UDP with frame=FOUR_E to "
                f"pipeline."
            )
        return TcpTransport(host, port, nodelay=nodelay)
    return UdpTransport(
        host, port, carries_serial=carries_serial, pipeline_depth=udp_pipeline_depth
    )
