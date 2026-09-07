"""What a decoded SLMP message *is*, before anything interprets it.

Layer 0. **stdlib only.** Importing this module must not pull ``socket``, ``ssl``,
``asyncio``, ``selectors``, ``threading`` or ``logging`` into ``sys.modules``
(``tests/unit/test_layering.py`` proves it in a subprocess).

Three value types and one protocol:

:class:`RawRequest`
    A parsed request frame. The client builds requests rather than parsing them, so this
    exists for the conformance simulator, for ``plan.describe()`` and for the golden
    round trip ``parse(build(x)) == x`` that keeps the two directions honest.
:class:`RawResponse`
    A parsed response frame: the route it came back on, the 4E serial if there was one,
    the declared length, the end code, and **either** a payload **or** an
    :class:`ErrorInfo` -- never both, never neither.
:class:`ErrorInfo`
    The "information on error responding station" block plus the echoed command and
    subcommand that an abnormal response carries. **Validated before it is believed.**
:class:`RequestSummary`
    The structural minimum an exception may carry about the request that failed, so that
    ``aslmp.errors`` never has to import ``aslmp.commands``.

**Why ``ErrorInfo`` is the dangerous one.** Every abnormal response on the bench was
exactly 20 bytes with ``L = 0x000B`` (FX5U-32MT/DS fw 1.065, 2026-09-06, 16 out of 16
provocations), and the block after the end code is a *second* access route -- the station
that actually produced the error -- which SH(NA)-080956ENG-M p.28 warns "may differ from
the request message". A parser that reads that block out of a frame too short to contain
it produces a plausible-looking error object naming a station nobody addressed, attached
to a real end code, and the user debugs the wrong PLC. So a short or malformed abnormal
frame raises here; it never yields a half-filled :class:`ErrorInfo`.

The 20 bytes are a consequence, not a rule: ``L`` is 11 because no command in the bench
set defined an error trailer, and SH(NA)-080956ENG-M p.28 documents "response data when
failed (when defined by command)". Everything here is driven off ``L``; nothing anywhere
compares against 20.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol

from aslmp.wire.citations import Citation, Measurement
from aslmp.wire.route import Route

if TYPE_CHECKING:  # pragma: no cover - typing only
    # frames.py imports THIS module at runtime, so the runtime edge only runs one way.
    # FrameFormat is needed for a field's type and for two attribute reads, not for an
    # import-time dependency.
    from aslmp.wire.codec import Codec
    from aslmp.wire.frames import FrameFormat

__all__ = [
    "ABNORMAL_RESPONSE",
    "ABNORMAL_RESPONSE_MEASURED",
    "ErrorInfo",
    "RawRequest",
    "RawResponse",
    "RequestSummary",
    "SlmpErrorInfoError",
    "SlmpFrameError",
    "SlmpFrameFormatError",
    "SlmpIncompleteFrameError",
    "SlmpSerialMismatchError",
    "SlmpShortFrameError",
    "SlmpTrailingDataError",
    "SlmpUnsolicitedFrameError",
]


ABNORMAL_RESPONSE: Final = Citation(
    manual="SH(NA)-080956ENG",
    revision="M",
    section="4.2 pp.27-28",
    note=(
        "An abnormal response carries, after the end code: information on the error "
        "responding station (network No. 1, station No. 1, module I/O No. 2 LE, "
        "multidrop station No. 1), then the request's command and subcommand echoed "
        "(2 + 2, LE), then 'response data when failed (when defined by command)'. "
        "p.28: 'The information may differ from the request message due to the error "
        "responding station.' JY997D56001-K p.21 prints the same frame for C051H with "
        "L = 000BH."
    ),
)
"""The abnormal-response layout. Quoted by every refusal in this module."""

ABNORMAL_RESPONSE_MEASURED: Final = Measurement(
    cpu="FX5U-32MT/DS",
    firmware="1.065",
    date="2026-09-06",
    note=(
        "All 16 abnormal frames provoked on the bench were exactly 20 bytes with "
        "L = 0x000B and no command-defined trailer, and the error-responding-station "
        "block was always identical to the request route on this direct connection. "
        "Neither fact is assumed anywhere: the read is driven off L, and the two routes "
        "are separate fields."
    ),
)
"""The bench's word on abnormal frames. 20 is an observation, never a constant."""

_UINT16_MAX: Final = 0xFFFF


# ----------------------------------------------------------------------------------------
# Errors
#
# These live in layer 0 rather than in ``aslmp.errors`` for the reason ``codec.py``
# records: ``aslmp.errors`` is layer 0.5 and imports ``aslmp.wire``, so a wire module
# that imported the exception hierarchy would close a cycle and
# ``tests/unit/test_layering.py`` fails the build for it. DESIGN section 3.1's
# ``SlmpFrameFormatError``, ``SlmpSerialMismatchError``, ``SlmpShortDatagramError``,
# ``SlmpTrailingDataError`` and ``SlmpUnsolicitedFrameError`` are the layer-0.5 faces of
# these; U5 wraps them with the target, the frame and the timing that make the message
# worth reading. See the concerns recorded with build unit U4.
# ----------------------------------------------------------------------------------------


class SlmpFrameError(ValueError):
    """Bytes arrived and they are not a message this library can name.

    Also a ``ValueError`` for the reason DESIGN section 3.2 gives: an existing
    ``except ValueError`` keeps working, and the alternative is people writing
    ``except Exception``.
    """


class SlmpFrameFormatError(SlmpFrameError):
    """A field is present but impossible: a foreign subheader, an ``L`` that cannot be.

    Never repaired, never resynchronised. A parser that skips forward to the next thing
    that looks like a subheader will eventually deliver the previous transaction's data
    as this transaction's answer, with end code ``0x0000`` and nothing to catch it.
    """


class SlmpShortFrameError(SlmpFrameFormatError):
    """Fewer bytes than the frame's own length field says there are.

    DESIGN section 3.1's ``SlmpShortDatagramError``. On TCP this is a segmentation bug
    in the caller -- :class:`aslmp.wire.reader.ResponseAccumulator` exists so it cannot
    happen -- and on UDP it is a truncated datagram, which is not a short read but a
    different message.
    """


class SlmpTrailingDataError(SlmpFrameFormatError):
    """More bytes than the frame's own length field says there are.

    On UDP, a datagram longer than ``prefix + L``. On TCP, the measured coalescing
    corruption arriving as a second response in the same buffer. Both are refused: the
    surplus is somebody's answer, and guessing whose is how a control loop reads the
    wrong tag.
    """


class SlmpIncompleteFrameError(SlmpFrameError):
    """A whole frame was asked for before one had arrived.

    Deliberately distinct from :class:`SlmpShortFrameError`: nothing is wrong with the
    bytes, there are simply not enough of them yet. An accumulator that answered this
    with a zero-filled response would reproduce ``pymcprotocol``'s truncation bug, which
    turns ``[111, 222, 333, 444]`` into ``[111, 222, 0, 0]``.
    """


class SlmpSerialMismatchError(SlmpFrameError):
    """A 4E response echoed a serial number we did not send.

    The only in-band defence against the measured TCP coalescing corruption: two requests
    written before the first response is read produce **one** response, for the *last*
    request, with end code ``0x0000`` (FX5U-32MT/DS fw 1.065, 2026-09-06). On 3E there is
    no serial and the corruption is undetectable, which is why the in-flight gate is
    structural rather than advisory.
    """


class SlmpUnsolicitedFrameError(SlmpFrameError):
    """A request-shaped frame arrived where a response was expected.

    SH(NA)-080956ENG-M section 5.11 p.204 documents **Ondemand** (command ``2101``): a
    message the SLMP-compatible device sends to the external device with no request,
    carrying up to 1920 bytes. A client that treats "the next thing that arrives" as its
    response is desynchronised by one of these for the life of the connection.
    """


class SlmpErrorInfoError(SlmpFrameFormatError):
    """An abnormal response too short or too malformed to carry its own error block.

    The end code is real; the station block behind it is not there. Raising is the only
    honest answer: an :class:`ErrorInfo` assembled out of whatever bytes happened to
    follow names a station nobody addressed, and it looks exactly like a true one.
    """


# ----------------------------------------------------------------------------------------
# Value types
# ----------------------------------------------------------------------------------------


def _check_field(value: object, owner: str, name: str) -> int:
    """Return ``value`` as a 16-bit unsigned int, or raise naming the field."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{owner}.{name} must be an int, not {type(value).__name__}")
    if not 0 <= value <= _UINT16_MAX:
        raise SlmpFrameFormatError(
            f"{owner}.{name} must fit an unsigned 16-bit field (0..65535); got {value}. "
            f"Nothing here masks or wraps a wire field."
        )
    return value


def _check_bytes(value: object, owner: str, name: str) -> bytes:
    """Return ``value`` as ``bytes``, or raise naming the field."""
    if not isinstance(value, bytes):
        raise TypeError(
            f"{owner}.{name} must be bytes, not {type(value).__name__}. A memoryview or "
            f"bytearray is copied at the parse boundary so a frame cannot change after "
            f"it has been decoded."
        )
    return value


@dataclass(frozen=True, slots=True)
class ErrorInfo:
    """The error block of an abnormal response: who answered, and what they were asked.

    ``responding`` is a **second** access route. On a direct Ethernet connection it is
    always the request's own route (measured, 16/16), but SH(NA)-080956ENG-M p.28 says it
    may differ, and a relayed request that fails at the far end is exactly when you need
    to know which station said no. The two routes are therefore separate fields
    everywhere in this library, and an exception carries both.

    ``command`` and ``subcommand`` are the request's own, echoed. They are the reason a
    request whose length field was zero is diagnosable at all: the FX5U answered
    ``0xC061`` echoing ``cmd 0x0000 sub 0x0000``, because it read the command out of the
    two bytes that a correctly framed request would have used for the monitoring timer.
    """

    responding: Route
    command: int
    subcommand: int

    def __post_init__(self) -> None:
        if not isinstance(self.responding, Route):
            raise TypeError(
                f"ErrorInfo.responding must be a Route, not "
                f"{type(self.responding).__name__}"
            )
        _check_field(self.command, "ErrorInfo", "command")
        _check_field(self.subcommand, "ErrorInfo", "subcommand")

    @staticmethod
    def wire_len(codec: Codec) -> int:
        """9 units binary, 18 ASCII: a route plus the two echoed 16-bit fields."""
        return Route.wire_len(codec) + 2 * codec.number_len(16)

    def encode(self, codec: Codec) -> bytes:
        """The block as it goes on the wire, for the simulator and the golden corpus.

        The client never emits one. It exists so that ``encode(decode(bytes)) == bytes``
        can be asserted over every captured abnormal frame -- which is what proves the
        parser right rather than merely self-consistent.
        """
        return (
            self.responding.encode(codec)
            + codec.number(self.command, bits=16)
            + codec.number(self.subcommand, bits=16)
        )

    @classmethod
    def decode(cls, buf: bytes, off: int, codec: Codec) -> ErrorInfo:
        """The error block at ``off``. Raises if it is short, malformed or undocumented.

        Every field is validated on the way in -- the route by :meth:`Route.decode`, the
        two 16-bit fields by the codec, which refuses a non-hex ASCII nibble rather than
        reading it as zero.
        """
        responding = Route.decode(buf, off, codec)
        after = off + Route.wire_len(codec)
        return cls(
            responding=responding,
            command=codec.read_number(buf, after, bits=16),
            subcommand=codec.read_number(buf, after + codec.number_len(16), bits=16),
        )

    def __str__(self) -> str:
        return (
            f"0x{self.command:04X} sub 0x{self.subcommand:04X} from {self.responding}"
        )


@dataclass(frozen=True, slots=True)
class RawRequest:
    """A parsed request frame. The client builds these; the simulator parses them.

    ``length`` is the frame's own ``L`` in wire units -- bytes in binary coding,
    characters in ASCII coding -- counting from the monitoring timer through the end of
    the request data, and counting neither itself nor anything before it
    (SH(NA)-080956ENG-M p.23). It is stored as it was read, so a round trip through
    :meth:`aslmp.wire.frames.FrameFormat.parse_request` and
    :meth:`aslmp.wire.frames.FrameFormat.build` is byte-exact.

    Also satisfies :class:`RequestSummary`, so ``Plc.raw_command`` can hand one straight
    to an exception without ``aslmp.errors`` learning what a command is.
    """

    frame: FrameFormat
    serial: int | None
    route: Route
    length: int
    monitoring_timer: int
    command: int
    subcommand: int
    payload: bytes
    raw: bytes
    subheader_tail: bytes

    def __post_init__(self) -> None:
        _check_serial(self.frame, self.serial, "RawRequest")
        _check_field(self.length, "RawRequest", "length")
        _check_field(self.monitoring_timer, "RawRequest", "monitoring_timer")
        _check_field(self.command, "RawRequest", "command")
        _check_field(self.subcommand, "RawRequest", "subcommand")
        _check_bytes(self.payload, "RawRequest", "payload")
        _check_bytes(self.raw, "RawRequest", "raw")
        _check_bytes(self.subheader_tail, "RawRequest", "subheader_tail")

    @property
    def request_bytes(self) -> int:
        """Wire units in the whole frame, subheader included."""
        return len(self.raw)

    def describe(self) -> str:
        """``RequestSummary`` rendering for an exception's ``request`` line."""
        return (
            f"raw_command(0x{self.command:04X}, 0x{self.subcommand:04X}, "
            f"{len(self.payload)} payload unit(s))"
        )


@dataclass(frozen=True, slots=True)
class RawResponse:
    """A parsed response frame: exactly one of a payload or an error block.

    ``end_code == 0`` means ``payload`` is the response data and ``error_info`` is
    ``None``; any other end code means ``error_info`` is present and ``payload`` is
    empty. The two states are enforced in ``__post_init__`` rather than documented,
    because "an empty payload and end code 0x0000" is precisely what a parser that gave
    up returns, and DESIGN section 5.5 asks for a test that an empty buffer never grades
    as success.

    ``length`` is ``L`` in wire units, counting from the end code through the end of the
    response data (SH(NA)-080956ENG-M p.27). ``raw`` is the whole frame including the
    subheader, so an exception can print exactly what came back.

    ``subheader_tail`` is the 4E subheader's trailing two units, and is **never
    validated**. SH(NA)-080956ENG-M p.18 calls them "(Fixed value)" while
    SH(NA)-080008-AB p.42 labels the same two bytes "(Free)" in its own worked example
    and contradicts itself in its prose. The bench settled it: a request sent with
    ``AA 55`` there came back ``00 00`` -- echoed nowhere, rejected nowhere
    (FX5U-32MT/DS fw 1.065, 2026-09-06). They are exposed and nothing is built on them.
    """

    frame: FrameFormat
    serial: int | None
    route: Route
    length: int
    end_code: int
    payload: bytes
    error_info: ErrorInfo | None
    extra_error_data: bytes
    raw: bytes
    subheader_tail: bytes

    def __post_init__(self) -> None:
        _check_serial(self.frame, self.serial, "RawResponse")
        _check_field(self.length, "RawResponse", "length")
        _check_field(self.end_code, "RawResponse", "end_code")
        _check_bytes(self.payload, "RawResponse", "payload")
        _check_bytes(self.extra_error_data, "RawResponse", "extra_error_data")
        _check_bytes(self.raw, "RawResponse", "raw")
        _check_bytes(self.subheader_tail, "RawResponse", "subheader_tail")
        if self.end_code == 0:
            if self.error_info is not None:
                raise SlmpFrameFormatError(
                    "a normal response (end code 0x0000) carries no error information "
                    f"block, but one was supplied: {self.error_info}. "
                    f"({ABNORMAL_RESPONSE.reference})"
                )
            if self.extra_error_data:
                raise SlmpFrameFormatError(
                    "a normal response (end code 0x0000) carries no command-defined "
                    f"error data, but {len(self.extra_error_data)} unit(s) were "
                    f"supplied. ({ABNORMAL_RESPONSE.reference})"
                )
        else:
            if self.error_info is None:
                raise SlmpErrorInfoError(
                    f"end code 0x{self.end_code:04X} is abnormal, so the frame must "
                    f"carry an error information block naming the responding station "
                    f"and echoing the command. Refusing to report an end code whose "
                    f"provenance was not read. ({ABNORMAL_RESPONSE.reference})"
                )
            if self.payload:
                raise SlmpFrameFormatError(
                    f"end code 0x{self.end_code:04X} is abnormal, so there is no "
                    f"response data: the bytes after the end code are the error "
                    f"information block, and any trailer belongs in extra_error_data. "
                    f"({ABNORMAL_RESPONSE.reference})"
                )

    @property
    def ok(self) -> bool:
        """``True`` only for end code ``0x0000``. Not a substitute for raising."""
        return self.end_code == 0

    @property
    def response_bytes(self) -> int:
        """Wire units in the whole frame, subheader included."""
        return len(self.raw)

    def __str__(self) -> str:
        if self.error_info is None:
            return (
                f"{self.frame.frame_type.value} response, end code 0x0000, "
                f"{len(self.payload)} payload unit(s)"
            )
        return (
            f"{self.frame.frame_type.value} response, end code "
            f"0x{self.end_code:04X}, {self.error_info}"
        )


def _check_serial(frame: FrameFormat, serial: int | None, owner: str) -> None:
    """A serial exists on exactly the frame formats that have the field."""
    if serial is None:
        if frame.carries_serial:
            raise SlmpFrameFormatError(
                f"{owner}.serial is required on a {frame.frame_type.value} frame: the "
                f"serial No. is a field of the subheader and is echoed by the "
                f"responder ({frame.cites[0]})."
            )
        return
    if not frame.carries_serial:
        raise SlmpFrameFormatError(
            f"{owner}.serial must be None on a {frame.frame_type.value} frame, which "
            f"has no serial No. field. 3E gives no in-band correlation at all, which is "
            f"why the one-in-flight gate is structural."
        )
    _check_field(serial, owner, "serial")


class RequestSummary(Protocol):
    """What an exception may carry about the request that failed.

    Structural on purpose. ``aslmp.errors`` is layer 0.5 and ``aslmp.commands`` is layer
    2, so an exception that named a concrete command class would invert the layering and
    drag the whole command registry into the error path. Anything with these four
    members will do: :class:`RawRequest` satisfies it, and so does whatever
    ``Command.summary()`` returns.
    """

    @property
    def command(self) -> int:
        """The command code, e.g. ``0x0403``."""
        ...

    @property
    def subcommand(self) -> int:
        """The subcommand, e.g. ``0x0000``."""
        ...

    @property
    def request_bytes(self) -> int:
        """Wire units in the request frame that was built."""
        ...

    def describe(self) -> str:
        """The call, as a user would recognise it: ``read_random(['D0','D4'])``."""
        ...
