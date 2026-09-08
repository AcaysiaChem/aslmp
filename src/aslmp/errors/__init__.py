"""The exception hierarchy: hand-written, because these names are the API.

Layer 0.5. May import ``aslmp.wire`` and :mod:`aslmp.errors.endcodes`, nothing else.
Importing this package must not pull ``socket``, ``ssl``, ``asyncio``, ``selectors``,
``threading`` or ``logging`` into ``sys.modules`` (``tests/unit/test_layering.py``
proves it in a subprocess).

DESIGN.md section 3 in full: the tree (3.1), the four decisions inside it (3.2), the
end-code mapping strategy (3.3), the computed timeout causes (3.4) and the one
multi-line ``__str__`` (3.6, graft G17) that every class here renders.

**Why the classes are written out by hand and the ~100-row end-code table is not.**
Class names are what people type in ``except`` clauses and what a Mitsubishi engineer
reads in the source; they must be stable, greppable and good for ``mypy`` and IDE
navigation, and generated names drift. The code-to-description mapping is data: it
changes when a manual revision lands and it must be diffable one row at a time. So
``aslmp/data/end_codes.tsv`` is the source of truth, ``tools/gen_endcodes.py`` emits
:mod:`aslmp.errors.endcodes` as a committed literal, and
:func:`aslmp.errors.routing.end_code_error` joins the two.

**The four decisions of section 3.2, restated where they are implemented.**

1. :class:`SlmpUsageError` also inherits :class:`ValueError` and
   :class:`SlmpTimeoutError` also inherits :class:`TimeoutError`, so an existing
   ``except ValueError`` / ``except TimeoutError`` keeps working. Interoperability beats
   single-inheritance purity: the alternative is people writing ``except Exception``.
2. :class:`SlmpOutcomeUnknownError` is a **sibling of the whole tree**, not a
   :class:`SlmpTransportError`. ``except SlmpTransportError: retry()`` is correct for a
   read and a data-loss bug for a write; nesting it re-arms exactly that bug.
3. Reads never raise :class:`SlmpOutcomeUnknownError`. A failed read has no outcome to
   be unknown about, and the classification is driven by ``Command.mutates`` so a new
   command cannot forget it.
4. An unknown end code still raises a named class -- never a bare integer, never
   ``"slmp_end_code_c059"``, and never ``pymelsec``'s ``'0x49241'``, which looks like
   hex and is the decimal 49241 with ``0x`` glued on the front.

**Structural, not imported.** ``.client``, ``.tx`` and ``.request`` are typed by the
protocols :class:`ClientSummary`, :class:`TransactionSummary` and
:class:`aslmp.wire.raw.RequestSummary`. ``Plc`` is Layer 5, ``Transaction`` is Layer
2.5 and ``Command`` is Layer 2; an exception that named any of them would invert the
layering and drag the whole client into the error path. The dependency runs upward and
the knowledge runs downward, exactly as ``transport/`` takes a structural
``Reassembler`` rather than naming a frame.

**Everything rendered here is ASCII.** An exception is printed on plant PCs whose
console is cp437 or cp1252, and a ``UnicodeEncodeError`` raised while reporting a
``0xC059`` is the worst possible second failure. DESIGN section 3.6 prints an em dash;
this renders ``--``.
"""

from __future__ import annotations

import enum
import textwrap
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Final, Protocol

from aslmp.errors.endcodes import END_CODES, EndCodeInfo
from aslmp.wire.citations import Citation, Measurement, Source
from aslmp.wire.raw import RequestSummary
from aslmp.wire.route import Route

__all__ = [
    "NO_DIAGNOSTICS",
    "UNDOCUMENTED_END_CODE",
    "ClientSummary",
    "Diagnostics",
    "Labelled",
    "OutcomeUnknownReason",
    "SlmpAddressRangeError",
    "SlmpAddressSyntaxError",
    "SlmpAsciiConversionError",
    "SlmpBitPointCountError",
    "SlmpBlockLayoutError",
    "SlmpBusyError",
    "SlmpCapabilityError",
    "SlmpConcurrentTransactionError",
    "SlmpConfigurationError",
    "SlmpConnectionEntryBusyError",
    "SlmpConnectionLostError",
    "SlmpConnectionStateError",
    "SlmpCpuDataTooLargeError",
    "SlmpCpuDeviceSpecError",
    "SlmpCpuError",
    "SlmpCpuFileError",
    "SlmpCpuModuleError",
    "SlmpCpuRemoteDisabledError",
    "SlmpCpuRunningError",
    "SlmpCpuUnsupportedRequestError",
    "SlmpDataLengthError",
    "SlmpDatagramLostError",
    "SlmpDatagramSourceError",
    "SlmpDeviceNotAccessibleError",
    "SlmpDeviceNotAllowedHereError",
    "SlmpDeviceNotOnCpuError",
    "SlmpDeviceRadixError",
    "SlmpDeviceRangeError",
    "SlmpEncodingNotSupportedError",
    "SlmpEndCodeError",
    "SlmpError",
    "SlmpFrameFormatError",
    "SlmpHandshakeError",
    "SlmpMonitorNotRegisteredError",
    "SlmpMonitoringTimerError",
    "SlmpNotConnectedError",
    "SlmpNotSentError",
    "SlmpOutcomeUnknownError",
    "SlmpPayloadShapeError",
    "SlmpPlcFramingError",
    "SlmpPlcTimeoutError",
    "SlmpPointCountError",
    "SlmpPointLimitError",
    "SlmpProfileMismatchError",
    "SlmpProtocolError",
    "SlmpRandomPointCountError",
    "SlmpRemotePasswordError",
    "SlmpRemotePasswordLockedError",
    "SlmpRemotePasswordLockoutError",
    "SlmpRemoteStateNotReachedError",
    "SlmpRequestContentError",
    "SlmpRouteMismatchError",
    "SlmpRoutingError",
    "SlmpSemanticError",
    "SlmpSerialMismatchError",
    "SlmpShortDatagramError",
    "SlmpSinkError",
    "SlmpTargetChangedError",
    "SlmpTimeoutError",
    "SlmpTrailingDataError",
    "SlmpTransportError",
    "SlmpUnknownDeviceError",
    "SlmpUnsolicitedFrameError",
    "SlmpUnsupportedCommandError",
    "SlmpUsageError",
    "SlmpValueRangeError",
    "SlmpVerificationError",
    "SlmpWordPointCountError",
    "SlmpWriteProtectedError",
    "TargetInfo",
    "TimeoutCause",
    "TimingSummary",
    "TransactionSummary",
]


# ========================================================================================
# Structural stand-ins for the things an exception may describe but may not import.
# ========================================================================================


class Labelled(Protocol):
    """Structural stand-in for the connection-entry option enums.

    ``FrameType``, ``Encoding`` and ``TransportKind`` are Layer 0/1 public enums; this
    module is below them and names none of them. Any :class:`enum.Enum` whose members
    carry ``str`` values satisfies this, which is the same shape
    :class:`aslmp.timing.WireOption` takes for the same reason.
    """

    @property
    def name(self) -> str: ...

    @property
    def value(self) -> str: ...


class TimingSummary(Protocol):
    """The stamps the ``timing`` line reads, as raw nanoseconds.

    Deliberately the *stamps* and not the derived durations.
    :class:`aslmp.timing.TransactionTiming` raises ``TimingIncompleteError`` from
    ``wire_ns`` when the transaction never received a response -- which is precisely the
    case an exception is being rendered for. Reading the stamps and subtracting means
    :meth:`SlmpError.__str__` has no path that can raise.
    """

    @property
    def submitted_at(self) -> int: ...

    @property
    def gate_acquired_at(self) -> int: ...

    @property
    def sent_at(self) -> int: ...

    @property
    def first_byte_at(self) -> int | None: ...

    @property
    def received_at(self) -> int | None: ...

    @property
    def chunks(self) -> Sequence[object]: ...


class TransactionSummary(Protocol):
    """What an exception may carry about the transaction record.

    :class:`aslmp.timing.Transaction` satisfies it. ``tests/unit/test_errors.py`` binds
    a real one to this type so that the compatibility is checked by ``mypy`` rather than
    hoped for.
    """

    @property
    def timing(self) -> TimingSummary: ...

    @property
    def sequence(self) -> int: ...

    @property
    def generation(self) -> int: ...

    @property
    def command(self) -> int: ...

    @property
    def subcommand(self) -> int: ...

    @property
    def end_code(self) -> int: ...

    @property
    def serial(self) -> int | None: ...

    @property
    def request_frame(self) -> bytes | None: ...

    @property
    def response_frame(self) -> bytes | None: ...


class ClientSummary(Protocol):
    """What an exception may say about the client it came from: the ``target`` line.

    ``Plc`` is Layer 5. Six read-only members are all the rendering needs, and
    :class:`TargetInfo` is a concrete implementation for tests, the simulator and the
    command-line tool.
    """

    @property
    def peer(self) -> tuple[str, int]: ...

    @property
    def model(self) -> str | None: ...

    @property
    def model_code(self) -> int | None: ...

    @property
    def transport(self) -> Labelled: ...

    @property
    def encoding(self) -> Labelled: ...

    @property
    def frame(self) -> Labelled: ...


@dataclass(frozen=True, slots=True)
class TargetInfo:
    """A concrete :class:`ClientSummary`: who we were talking to, in six fields.

    ``model`` and ``model_code`` are ``None`` before the ``0101`` half of the handshake
    has run. They are never guessed: an unidentified CPU renders as "unidentified CPU",
    not as the profile the caller asked for, because the whole point of the identify
    step is that the two can differ.
    """

    peer: tuple[str, int]
    transport: Labelled
    encoding: Labelled
    frame: Labelled
    model: str | None = None
    model_code: int | None = None


# ========================================================================================
# The diagnostic bundle behind section 3.6
# ========================================================================================


@dataclass(frozen=True, slots=True)
class Diagnostics:
    """Everything the section 3.6 rendering can print. Every field optional.

    Bundled rather than spread across ~70 constructor signatures so that a subclass
    which adds a field of its own -- :class:`SlmpTimeoutError`,
    :class:`SlmpEndCodeError` -- writes one extra keyword argument instead of
    re-declaring ten.

    ``sent_frame`` / ``received_frame`` are named for the *lines they render*. The
    plain name ``sent`` is reserved: :class:`SlmpOutcomeUnknownError` needs
    ``.sent: bool`` for the "did the bytes leave this process" question, and a
    ``bytes | None`` field of the same name is exactly the sort of collision that ends
    with someone testing ``if err.sent:`` on a frame.
    """

    client: ClientSummary | None = None
    request: RequestSummary | None = None
    tx: TransactionSummary | None = None
    sent_frame: bytes | None = None
    received_frame: bytes | None = None
    requested_route: Route | None = None
    responded_route: Route | None = None
    echoed: str = ""
    cause: str = ""
    measurement: Measurement | None = None
    note: str = ""
    action: str = ""
    manual: Citation | None = None


NO_DIAGNOSTICS: Final = Diagnostics()
"""The empty bundle. ``SlmpError`` uses it so ``.diagnostics`` is never ``None``."""


# ========================================================================================
# Rendering. Pure functions; none of them can raise on any input the types allow.
# ========================================================================================

_LABEL_WIDTH: Final = 10
_LEAD: Final = "  "
_WIDTH: Final = 96
_BYTE_LIMIT: Final = 24
_NS_PER_MS: Final = 1_000_000.0


def _hexdump(data: bytes) -> str:
    """``"50 00 00 FF ... (32 bytes)"`` -- the head of a frame plus its true length."""
    head = " ".join(f"{byte:02X}" for byte in data[:_BYTE_LIMIT])
    if len(data) > _BYTE_LIMIT:
        return f"{head} ... ({len(data)} bytes)"
    return f"{head} ({len(data)} bytes)"


def _route(route: Route) -> str:
    """``"00/FF/03FF/00"`` -- network / station / module I/O / multidrop."""
    return (
        f"{route.network:02X}/{route.station:02X}/"
        f"{route.module_io:04X}/{route.multidrop:02X}"
    )


def _ms(nanoseconds: int) -> str:
    return f"{nanoseconds / _NS_PER_MS:.2f} ms"


def _target_line(client: ClientSummary) -> str:
    host, port = client.peer
    if client.model is None:
        who = "unidentified CPU"
    elif client.model_code is None:
        who = client.model
    else:
        who = f"{client.model} (model code 0x{client.model_code:04X})"
    return (
        f"{who} at {host}:{port} -- {client.transport.value} / "
        f"{client.encoding.value} / {client.frame.value}"
    )


def _request_line(request: RequestSummary) -> str:
    return (
        f"{request.describe()}  ->  0x{request.command:04X} "
        f"sub 0x{request.subcommand:04X}, {request.request_bytes} bytes"
    )


def _timing_line(tx: TransactionSummary) -> str:
    timing = tx.timing
    parts = [f"gen {tx.generation}", f"seq {tx.sequence}"]
    count = len(timing.chunks)
    parts.append("1 chunk" if count == 1 else f"{count} chunks")
    parts.append(f"queue {_ms(timing.gate_acquired_at - timing.submitted_at)}")
    if timing.first_byte_at is not None:
        parts.append(f"first byte {_ms(timing.first_byte_at - timing.sent_at)}")
    if tx.serial is not None:
        parts.append(f"serial 0x{tx.serial:04X}")
    head = "no response" if timing.received_at is None else _ms(
        timing.received_at - timing.sent_at
    )
    return f"{head}  ({', '.join(parts)})"


def _labelled(label: str, text: str) -> list[str]:
    """``label`` and ``text``, wrapped to 96 columns, continuations under the value."""
    indent = _LEAD + " " * _LABEL_WIDTH
    wrapped = textwrap.wrap(
        text,
        width=_WIDTH,
        initial_indent=_LEAD + label.ljust(_LABEL_WIDTH),
        subsequent_indent=indent,
        break_long_words=False,
        break_on_hyphens=False,
    )
    if wrapped:
        return wrapped
    return [_LEAD + label.ljust(_LABEL_WIDTH)]


# ========================================================================================
# The root
# ========================================================================================


def _rebuild(
    cls: type[SlmpError], args: tuple[object, ...], state: Mapping[str, Any]
) -> SlmpError:
    """Reconstruct without calling ``__init__``: subclass signatures all differ.

    ``BaseException.__reduce__`` would call ``cls(*self.args)``, which is a
    ``TypeError`` for every class here that takes more than a message --
    :class:`SlmpEndCodeError` needs its :class:`~aslmp.errors.endcodes.EndCodeInfo`.
    An exception that cannot cross a ``multiprocessing`` boundary turns a diagnosable
    ``0xC056`` into an unpickling traceback about the diagnosis.
    """
    instance = cls.__new__(cls)
    BaseException.__init__(instance, *args)
    instance.__dict__.update(state)
    return instance


class SlmpError(Exception):
    """Root of every exception this library raises.

    Carries the section 3.6 diagnostic bundle and renders it. ``str(e)`` never raises:
    every line is built from stored values, with no arithmetic that can fail and no
    property access that can throw. That is the specific defect this rendering is named
    after -- ``pymelsec`` formats ``0xC056`` as the string ``'0x49238'`` (the decimal,
    with ``0x`` glued on the front) and then ``str(e)`` raises ``TypeError``.

    The class name is **not** part of ``__str__``. Python prints
    ``aslmp.errors.SlmpUnsupportedCommandError: <str(e)>`` at the head of a traceback
    already, and DESIGN section 3.6's first line is that traceback line; repeating it
    inside ``__str__`` would print the class twice on every traceback in the world.
    """

    def __init__(self, message: str, *, diagnostics: Diagnostics | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.diagnostics = NO_DIAGNOSTICS if diagnostics is None else diagnostics

    # -- the section 3.1 accessors -------------------------------------------

    @property
    def client(self) -> ClientSummary | None:
        """Who we were talking to, if the raiser knew."""
        return self.diagnostics.client

    @property
    def tx(self) -> TransactionSummary | None:
        """The transaction record -- present for failed transactions too."""
        return self.diagnostics.tx

    @property
    def request(self) -> RequestSummary | None:
        """The request that caused this, as a structural summary."""
        return self.diagnostics.request

    # -- rendering -----------------------------------------------------------

    def headline(self) -> str:
        """The first line: the one-sentence statement of what went wrong."""
        return self.message

    def details(self) -> list[str]:
        """The section 3.6 lines, in order, omitting those with nothing to say."""
        diagnostics = self.diagnostics
        lines: list[str] = []
        if diagnostics.client is not None:
            lines += _labelled("target", _target_line(diagnostics.client))
        if diagnostics.request is not None:
            lines += _labelled("request", _request_line(diagnostics.request))
        sent = diagnostics.sent_frame
        if sent is None and diagnostics.tx is not None:
            sent = diagnostics.tx.request_frame
        if sent:
            lines += _labelled("sent", _hexdump(sent))
        received = diagnostics.received_frame
        if received is None and diagnostics.tx is not None:
            received = diagnostics.tx.response_frame
        if received:
            lines += _labelled("received", _hexdump(received))
        routes: list[str] = []
        if diagnostics.requested_route is not None:
            routes.append(f"requested {_route(diagnostics.requested_route)}")
        if diagnostics.responded_route is not None:
            routes.append(f"responded {_route(diagnostics.responded_route)}")
        if routes:
            lines += _labelled("routes", "   ".join(routes))
        if diagnostics.echoed:
            lines += _labelled("echoed", diagnostics.echoed)
        if diagnostics.tx is not None:
            lines += _labelled("timing", _timing_line(diagnostics.tx))
        if diagnostics.cause:
            lines += _labelled("cause", diagnostics.cause)
        observed = diagnostics.note
        if diagnostics.measurement is not None:
            reference = diagnostics.measurement.reference
            observed = f"{observed} ({reference})" if observed else reference
        if observed:
            lines += _labelled("observed", observed)
        if diagnostics.action:
            lines += _labelled("action", diagnostics.action)
        if diagnostics.manual is not None:
            lines += _labelled("manual", str(diagnostics.manual))
        return lines

    def __str__(self) -> str:
        return "\n".join([self.headline(), *self.details()])

    def __reduce__(self) -> tuple[object, ...]:
        return (_rebuild, (type(self), self.args, dict(self.__dict__)))


# ========================================================================================
# 3.1 -- pre-transport. NOTHING WAS SENT.
# ========================================================================================


class SlmpUsageError(SlmpError, ValueError):
    """The call was wrong and no byte left this process.

    Also a :class:`ValueError` (section 3.2 decision 1). Every subclass is raised by
    validation, a capability check, a range check, a point-limit check or a block
    layout -- all of them before a frame is built, let alone sent. A caller may try a
    different call; retrying *this* one cannot help.
    """


class SlmpAddressSyntaxError(SlmpUsageError):
    """``"D100x"`` -- names the offending character and its position.

    Never a partial parse. ``pymcprotocol`` reads ``D100x`` as ``D100``, and
    ``PySLMPClient`` validates with ``assert 0 < start_num < 0xFFF``, which rejects
    ``D0`` when it runs and validates nothing under ``python -O``.
    """


class SlmpUnknownDeviceError(SlmpUsageError):
    """No device family in the table matches this prefix.

    Raised rather than defaulted. A device code the CPU does not recognise comes back
    as ``0xC05C`` on FX5U-32MT/DS fw 1.065 -- one round trip and a manual lookup later
    than refusing here.
    """


class SlmpDeviceRadixError(SlmpUsageError):
    """The digits are not legal in this device's radix on this profile.

    ``X1F`` and ``X8`` on iQ-F (X and Y are octal there), ``XFFG`` anywhere. The FX5U
    **accepted** ``Y8``, which is not a legal octal address, and answered ``0x0000``:
    client-side validation is the only thing between a typo and a plausible reading.
    """


class SlmpDeviceNotOnCpuError(SlmpUsageError):
    """This CPU has no such device family: ``V``, ``ZR``, ``DX``, ``DY`` on iQ-F.

    Measured on FX5U-32MT/DS fw 1.065: ``V0``, ``ZR0`` and ``DX0`` all return
    ``0xC05C``, not the ``0xC05B`` the doc-derived mapping predicted.
    """


class SlmpAddressRangeError(SlmpUsageError):
    """The address, or the **span** it implies, leaves the device's range.

    ``D8000`` on an FX5U (``D`` ends at ``D7999``; ``D8000`` returns ``0xC056``,
    measured), and equally ``D7999`` read as two points, which is the case a
    start-only check misses.
    """


class SlmpPointLimitError(SlmpUsageError):
    """More points than the command, coding, unit and link permit -- or zero points.

    FX5U-32MT/DS fw 1.065, built-in port, binary: 961 words -> ``0xC052``, 3585 bits ->
    ``0xC051`` (the manual says 7168), 193 random points -> ``0xC054``. A **zero** point
    count returns ``0xC052`` -- a point-count error, not an address error.
    """


class SlmpDeviceNotAllowedHereError(SlmpUsageError):
    """A legal device this command may not touch: ``TS`` in a Read Random.

    Load-bearing rather than pedantic: the FX5U **accepted** an illegal ``TS`` point in
    a Read Random and answered ``0x0000``. Nothing on the wire says the value is
    meaningless.
    """


class SlmpCapabilityError(SlmpUsageError):
    """The target does not have this capability, and we refuse before the wire.

    ``0x0801`` / ``0x0802`` (Monitor Registration / Execute Monitor) return ``0xC059``
    on FX5U-32MT/DS fw 1.065, measured twice through independent code paths, as does
    subcommand ``0x0002`` (the iQ-R long device specification). Refusing here is the
    difference between a typed error naming the measurement and a round trip that ends
    in "command or subcommand specification".
    """


class SlmpEncodingNotSupportedError(SlmpUsageError):
    """This coding cannot be used on this profile: ``ASCII_XY_OCT`` off iQ-F."""


class SlmpMonitoringTimerError(SlmpUsageError):
    """The monitoring timer is not expressible, or contradicts the client deadline.

    The field is in 250 ms units. Nothing here rounds: ``MonitoringTimer.seconds(0.3)``
    raises and names 0.25 and 0.5.
    """


class SlmpBlockLayoutError(SlmpUsageError):
    """A ``@plc_block`` layout cannot be bound: overlap, a bad bit fold, no base.

    Raised by ``bind()``, which is synchronous and does no I/O, so a layout mistake
    surfaces at start-up rather than inside the control loop.
    """


class SlmpValueRangeError(SlmpUsageError):
    """A value does not fit the field it is being written to.

    Never truncated and never masked. ``pymcprotocol`` writes ``0x1FFFF`` into a 16-bit
    register as ``0xFFFF`` and reports success.
    """


class SlmpConfigurationError(SlmpUsageError):
    """The arguments are incoherent, or an interlock is not set.

    Includes every ``RemoteControl`` method on a client constructed without
    ``allow_remote_control=True``: this library can stop a running machine over an
    unauthenticated cleartext socket.
    """


class SlmpConcurrentTransactionError(SlmpUsageError):
    """A second transaction was submitted while one was in flight, in ``STRICT`` mode.

    Structural, not advisory. On FX5U-32MT/DS fw 1.065 two TCP requests written before
    the first response is read produce **one** response, for the *last* request, with
    end code ``0x0000``. On 3E there is no serial, so that corruption is undetectable
    in band and the gate is the only defence.
    """


# ========================================================================================
# 3.1 -- the socket. No end code exists.
# ========================================================================================


class SlmpTransportError(SlmpError):
    """Something below SLMP failed: the socket, the peer, the silence.

    ``except SlmpTransportError: retry()`` is a correct pattern **for a read**. It is a
    data-loss bug for a write, which is why :class:`SlmpOutcomeUnknownError` is not a
    subclass of this.
    """


class SlmpConnectionEntryBusyError(SlmpTransportError):
    """TCP accepted and then the CPU immediately FINed: this socket did not get the entry.

    Measured on FX5U-32MT/DS fw 1.065, 2026-09-06: a second connection to a one-entry
    SLMP configuration completes ``connect()`` and then ``recv()`` returns 0 bytes before
    anything is sent. ``socket.connect()`` demonstrably lies on this hardware, which is
    why the handshake exists and why the non-blocking EOF check runs first.

    **Two causes, and the second one is easy to miss.** A second client really holding
    the entry is the obvious one. The other is *this* client reconnecting into its own
    ``close()``: measured 2026-09-07 on a wired link with a median RTT of 3.64 ms, a
    reconnect straight after a clean ``close()`` succeeded 1/6 at a 0 ms gap, 2/6 at
    1 ms, and 6/6 from 2 ms out to 200 ms. It does not behave like a fixed hold period --
    over Wi-Fi at ~7 ms RTT the same test never failed at any gap, so what has to elapse
    tracks the link rather than the clock, which is what racing the CPU's own FIN
    processing would look like from outside. That mechanism is an inference from the
    timings and not something this library can see; the consequence holds either way, so
    the window is link-dependent, a faster link should widen it, and 2 ms is one CPU on
    one link on one day rather than a spec value. Since this package has no default
    backoff on reconnect, an immediate reconnect is the ordinary way to arrive here --
    which is why an outside reviewer met this error repeatedly with nothing else
    connected to the CPU, and went looking for a second client that did not exist.

    The fix for that second cause is a short explicit settle before reconnecting to an
    entry this client just released, not a search for a phantom second client and not a
    retry loop. The error itself is correct in both cases and stays an error: nothing in
    this package retries a connect. See ``docs/hardware.md`` section 2.1, and
    ``A-ENTRY-RELEASE-RACE`` in ``aslmp/data/ambiguities.tsv``.
    """


class SlmpNotConnectedError(SlmpTransportError):
    """The client is not usable: never connected, closed, or stuck in ``FAILED``.

    ``reason`` says which. The ``FAILED`` state is sticky and the socket is closed;
    there is no automatic reconnection anywhere in this package, and a transaction
    submitted during a supervised reconnect raises immediately rather than waiting.
    """

    def __init__(
        self, message: str, *, reason: str, diagnostics: Diagnostics | None = None
    ) -> None:
        super().__init__(message, diagnostics=diagnostics)
        self.reason = reason


class SlmpConnectionLostError(SlmpTransportError):
    """The peer closed or reset an established connection mid-transaction."""


class SlmpHandshakeError(SlmpTransportError):
    """The ``0x0619`` Self Test echo did not verify byte for byte.

    The handshake is the proof that ``connect()`` did not lie: one measured ~7 ms
    zero-side-effect round trip establishes entry availability, coding, frame format,
    route, protocol and liveness at once. An echo that differs from what was sent means
    at least one of those six is wrong, and none of them is guessed at afterwards.
    """


class SlmpNotSentError(SlmpTransportError):
    """Provably nothing left this process (graft G8).

    The distinction from :class:`SlmpOutcomeUnknownError` is the whole point: "did not
    happen" and "may have happened" have different recoveries, and only one of them is
    safe to retry blindly.
    """


class SlmpDatagramSourceError(SlmpTransportError):
    """A UDP datagram arrived from an address or port we did not send to.

    Dropped and counted, never consumed. A UDP SLMP entry on iQ-F is point-to-point --
    GX Works3 refuses to save one without a destination IP address -- so a datagram
    from anywhere else is somebody else's, or nobody's.
    """

    def __init__(
        self,
        message: str,
        *,
        expected_peer: tuple[str, int],
        actual_peer: tuple[str, int],
        diagnostics: Diagnostics | None = None,
    ) -> None:
        super().__init__(message, diagnostics=diagnostics)
        self.expected_peer = expected_peer
        self.actual_peer = actual_peer


class SlmpDatagramLostError(SlmpTransportError):
    """A UDP request vanished: no response, no end code, no ICMP, nothing.

    **Its own class, never a generic timeout.** Measured on FX5U-32MT/DS fw 1.065 over
    UDP (GX Works3 connection entry No. 2, PLC port 5001), 2026-09-06: bursts of 4E
    reads fired without waiting returned 8/8 and 32/32 with zero loss, and **44/64** at
    depth 64 -- 20 requests dropped by the PLC's receive path with no error anywhere.
    The client finds out only by a serial that never comes back.

    ``serial`` is the 4E serial that never returned and ``in_flight`` is the depth at
    the moment it was sent, because those two numbers *are* the diagnosis: at depth 32
    this does not happen, and at depth 64 it happened to a third of the burst. Reporting
    it as :class:`SlmpTimeoutError` would send the reader looking at the network.

    3E has no serial (``serial is None``), which is why 3E/UDP pipelining is never
    offered: positional matching plus real loss is silently mismatched replies.
    """

    def __init__(
        self,
        message: str,
        *,
        serial: int | None,
        in_flight: int,
        deadline_s: float,
        diagnostics: Diagnostics | None = None,
    ) -> None:
        super().__init__(message, diagnostics=diagnostics)
        self.serial = serial
        self.in_flight = in_flight
        self.deadline_s = deadline_s

    def details(self) -> list[str]:
        lines = super().details()
        which = "3E carries no serial" if self.serial is None else f"0x{self.serial:04X}"
        lines += _labelled(
            "lost",
            f"serial {which}, {self.in_flight} request(s) in flight, "
            f"{self.deadline_s:g} s deadline. Measured on FX5U-32MT/DS fw 1.065: clean "
            f"to depth 32, 20 of 64 dropped at depth 64 with no error of any kind.",
        )
        return lines


class TimeoutCause(enum.Enum):
    """Candidate explanations for silence (DESIGN section 3.4).

    Ordered by :func:`aslmp.errors.routing.timeout_causes`. Two measured failures on
    FX5U-32MT/DS fw 1.065 are both *pure silence* and only one of them is fixed by a
    constructor argument: a coding mismatch answers ``0xC06F`` on some paths and says
    nothing at all on others, while a request whose length field is **overstated**
    makes the CPU block waiting for bytes that never come and answer nothing ever.
    Telling those apart is what this enum is for.
    """

    CODING_MISMATCH = "coding_mismatch"
    FRAME_NOT_ACCEPTED = "frame_not_accepted"
    PROTOCOL_MISMATCH = "protocol_mismatch"
    WRONG_PORT = "wrong_port"
    ENTRY_BUSY = "entry_busy"
    REQUEST_LENGTH_OVERSTATED = "request_length_overstated"
    PLC_STOPPED_OR_RESET = "plc_stopped_or_reset"
    CPU_BUSY = "cpu_busy"
    NETWORK = "network"


class SlmpTimeoutError(SlmpTransportError, TimeoutError):
    """The client deadline expired (graft G3).

    Also a :class:`TimeoutError` (section 3.2 decision 1). ``likely_causes`` is
    **computed from what was observed**, not a fixed list: how many bytes arrived, and
    whether this connection has ever completed a transaction, are enough to separate
    "your coding or frame format is wrong" from "the CPU stopped" from "your length
    field was overstated". See :func:`aslmp.errors.routing.timeout_causes`.
    """

    def __init__(
        self,
        message: str,
        *,
        likely_causes: tuple[TimeoutCause, ...],
        bytes_received: int,
        deadline_s: float,
        diagnostics: Diagnostics | None = None,
    ) -> None:
        super().__init__(message, diagnostics=diagnostics)
        self.likely_causes = likely_causes
        self.bytes_received = bytes_received
        self.deadline_s = deadline_s

    def details(self) -> list[str]:
        lines = super().details()
        causes = ", ".join(cause.value for cause in self.likely_causes)
        lines += _labelled(
            "silence",
            f"{self.bytes_received} byte(s) in {self.deadline_s:g} s; "
            f"ordered causes: {causes or 'none computed'}",
        )
        return lines


# ========================================================================================
# 3.1 -- bytes arrived; they are not a valid response.
# ========================================================================================


class SlmpProtocolError(SlmpError):
    """Bytes arrived and they are not a message this library can name.

    Never repaired and never resynchronised. A parser that skips forward to the next
    thing that looks like a subheader eventually delivers the previous transaction's
    data as this transaction's answer, with end code ``0x0000`` and nothing to catch it.

    ``aslmp.wire`` raises its own layer-0 ``SlmpFrameError`` family, which cannot import
    this module without closing a cycle. The connection layer catches those and re-raises
    the class here with the target, the frames and the timing attached, keeping the wire
    error as the ``__cause__``; :func:`aslmp.errors.routing.protocol_error_for` does the
    mapping so that no call site has to remember it.
    """


class SlmpFrameFormatError(SlmpProtocolError):
    """A field is present but impossible: a foreign subheader, an ``L`` that cannot be,
    a non-hex character where ASCII coding requires ``0``-``9`` / ``A``-``F``."""


class SlmpSerialMismatchError(SlmpProtocolError):
    """A 4E response echoed a serial we did not send.

    The only in-band defence against the measured TCP coalescing corruption. 4E is
    accepted on an FX5U connection entry configured for 3E and the serial is echoed
    correctly (FX5U-32MT/DS fw 1.065, 2026-09-06), contradicting two manuals; that
    observation is why 4E is permitted at all, and it ships labelled non-contractual.
    """


class SlmpRouteMismatchError(SlmpProtocolError):
    """The response's access route is not the one the request was sent on."""


class SlmpShortDatagramError(SlmpProtocolError):
    """Fewer bytes than the frame's own length field says there are.

    On UDP a truncated datagram -- which is a different message, not a short read. A
    960-point read returns 1935 bytes and is IP-fragmented on a 1500-byte MTU, so every
    fragment must arrive or the whole datagram is gone.
    """


class SlmpTrailingDataError(SlmpProtocolError):
    """More bytes than the frame's own length field says there are.

    The surplus is somebody's answer, and guessing whose is how a control loop reads
    the wrong tag.
    """


class SlmpUnsolicitedFrameError(SlmpProtocolError):
    """An Ondemand (``0x2101``) frame arrived where a response was expected.

    Recognised on receive and never parsed as somebody's response: a client that treats
    "the next thing that arrives" as its answer is desynchronised by one of these for
    the life of the connection.
    """


class SlmpPayloadShapeError(SlmpProtocolError):
    """End code ``0x0000``, and the payload does not fit the request's point layout.

    A response one word short is not three good values and a zero. ``pymcprotocol``
    turns ``[111, 222, 333, 444]`` into ``[111, 222, 0, 0]`` here.
    """


# ========================================================================================
# 3.1 -- the PLC answered, in its own words.
# ========================================================================================

UNDOCUMENTED_END_CODE: Final = "undocumented end code"
"""The description carried by a code that is not a row of ``data/end_codes.tsv``."""


class SlmpEndCodeError(SlmpError):
    """A well-formed response carrying a non-zero end code (sections 3.1, 3.3).

    Every field is populated: the code, Mitsubishi's own description, the cause and
    action from the table, the echoed command and subcommand, both access routes, any
    command-defined error trailer, the request that caused it, and the source -- a
    :class:`~aslmp.wire.citations.Measurement` where the hardware and a manual disagree,
    otherwise a :class:`~aslmp.wire.citations.Citation`.

    Build one with :func:`aslmp.errors.routing.end_code_error`, which picks the subclass
    from the table and synthesises a record for a code nobody has written down.
    ``0xC059`` must never surface as "error 49241", and never as ``pymelsec``'s
    ``'0x49241'``, which looks like hex and is not.
    """

    def __init__(
        self,
        info: EndCodeInfo,
        *,
        command: int | None = None,
        subcommand: int | None = None,
        request_route: Route | None = None,
        responding_station: Route | None = None,
        error_data: bytes = b"",
        diagnostics: Diagnostics | None = None,
    ) -> None:
        base = NO_DIAGNOSTICS if diagnostics is None else diagnostics
        if command is None or subcommand is None:
            echoed = base.echoed
        else:
            trailer = f", {_hexdump(error_data)} error data" if error_data else ""
            echoed = base.echoed or f"0x{command:04X} sub 0x{subcommand:04X}{trailer}"
        enriched = replace(
            base,
            echoed=echoed,
            requested_route=(
                request_route if base.requested_route is None else base.requested_route
            ),
            responded_route=(
                responding_station
                if base.responded_route is None
                else base.responded_route
            ),
            cause=base.cause or info.likely_cause,
            measurement=(
                info.measurement if base.measurement is None else base.measurement
            ),
            note=base.note or info.note,
            action=base.action or info.caller_action,
            manual=info.citation if base.manual is None else base.manual,
        )
        super().__init__(
            f'end code 0x{info.code:04X} -- "{info.description}"',
            diagnostics=enriched,
        )
        self.info = info
        self.end_code = info.code
        self.name = info.name
        self.description = info.description
        self.likely_cause = info.likely_cause
        self.caller_action = info.caller_action
        self.command = command
        self.subcommand = subcommand
        self.request_route = request_route
        self.responding_station = responding_station
        self.error_data = error_data

    @property
    def source(self) -> Source | None:
        """The measurement if there is one, else the citation, else ``None``.

        ``None`` only for a code synthesised at run time, which by definition has
        neither. Never raises -- unlike ``EndCodeInfo.source``, which is right to.
        """
        if self.info.measurement is not None:
            return self.info.measurement
        return self.info.citation

    @property
    def documented(self) -> bool:
        """``False`` for a code that is not a row of ``data/end_codes.tsv``."""
        return self.end_code in END_CODES

    def details(self) -> list[str]:
        lines = super().details()
        if not self.documented:
            lines += _labelled(
                "report",
                f"end code 0x{self.end_code:04X} is not in this library's table. Please "
                f"report it, with the CPU model and firmware version, at "
                f"https://github.com/AcaysiaChem/aslmp/issues -- the table is data and a "
                f"new row is a one-line change.",
            )
        return lines


class SlmpUnsupportedCommandError(SlmpEndCodeError):
    """``0xC059`` ``0xC0D9`` ``0xC1A4`` -- command or subcommand the target cannot use.

    Measured four ways on FX5U-32MT/DS fw 1.065: ``0x0801``, ``0x0802``, subcommand
    ``0x0002`` and the nonsense command ``0x9999`` all return ``0xC059``. ``0x0802``
    returns ``0xC059`` rather than ``0xC05D``, so it cannot be mistaken for "you forgot
    to register".
    """


class SlmpDeviceRangeError(SlmpEndCodeError):
    """``0xC056`` ``0xC1A9`` -- outside the device range. ``D8000`` on an FX5U."""


class SlmpDeviceNotAccessibleError(SlmpEndCodeError):
    """``0xC05A`` ``0xC05B`` ``0xC1AA`` -- the device exists, but not for this access."""


class SlmpPointCountError(SlmpEndCodeError):
    """``0xC071`` ``0xC0D8``, and the three specialised counts below."""


class SlmpBitPointCountError(SlmpPointCountError):
    """``0xC051`` -- bit points out of range. 3585 bits on an FX5U built-in port.

    The manual says 7168; FX5U-32MT/DS fw 1.065 refuses at 3585. The hardware wins.
    """


class SlmpWordPointCountError(SlmpPointCountError):
    """``0xC052`` -- word points out of range. 961 words on an FX5U built-in port.

    Also what a **zero** point count returns, measured, where the doc-derived mapping
    predicted an address error.
    """


class SlmpRandomPointCountError(SlmpPointCountError):
    """``0xC053`` ``0xC054`` -- random-access point counts. 193 points -> ``0xC054``."""


class SlmpRequestContentError(SlmpEndCodeError):
    """``0xC05C`` ``0xC060`` ``0xC072`` ``0xC0B5`` -- the request data is wrong.

    ``0xC05C`` is the measured correction: device code ``0x00``, device code ``0xFF``,
    ``V0``, ``ZR0`` and ``DX0`` all return ``0xC05C`` where the documents predicted
    ``0xC05B``.
    """


class SlmpPlcFramingError(SlmpEndCodeError):
    """``0xC057`` ``0xC058`` ``0xC061`` -- the PLC could not parse the frame.

    ``0xC061`` is the measured correction for a request-length mismatch, where the
    documents predicted ``0xC057``. It appears only when the length is **understated**;
    an **overstated** length produces no response at all, which is why
    :class:`TimeoutCause` has ``REQUEST_LENGTH_OVERSTATED``.
    """


class SlmpAsciiConversionError(SlmpEndCodeError):
    """``0xC050`` -- an ASCII-coded request contained a non-hex character."""


class SlmpDataLengthError(SlmpEndCodeError):
    """``0xC020`` ``0xC055`` ``0xC075`` -- the data length is not what the command needs."""


class SlmpPlcTimeoutError(SlmpEndCodeError):
    """``0xC022`` ``0xC035`` ``0xC040`` ``0xC05E`` ``0xC0DE`` -- the PLC itself timed out.

    Distinct from :class:`SlmpTimeoutError`: here a response *arrived*, saying that
    something on the PLC's side of the network gave up. Never auto-retried.
    """


class SlmpBusyError(SlmpEndCodeError):
    """``0xC0B2`` ``0xC0BD`` ``0xC86C`` -- the target is busy.

    **Never retried automatically anywhere in this library.** A retry decision needs to
    know whether the request mutates anything, and only the caller does.
    """


class SlmpMonitorNotRegisteredError(SlmpEndCodeError):
    """``0xC05D`` -- Execute Monitor without a Monitor Registration."""


class SlmpWriteProtectedError(SlmpEndCodeError):
    """``0xC062`` -- writing is prohibited: the remote password, or write protection."""


class SlmpRoutingError(SlmpEndCodeError):
    """``0xC05F`` ``0xC073`` ``0xC074`` ``0xC1A5`` ``0xC1A7`` ``0xC1A8`` -- the request
    cannot be relayed to the station it names."""


class SlmpConnectionStateError(SlmpEndCodeError):
    """``0xC001`` ``0xC012`` ``0xC013`` ``0xC015`` ``0xC018`` ``0xC0B9`` ``0xC0BA``
    ``0xC0BC`` -- the connection is not in a state that can serve this."""


class SlmpRemotePasswordError(SlmpEndCodeError):
    """``0xC200`` ``0xC202``-``0xC205`` ``0xC810`` -- remote password unlock/lock failed."""


class SlmpRemotePasswordLockedError(SlmpRemotePasswordError):
    """``0xC201`` -- the connection is locked; unlock it before accessing devices."""


class SlmpRemotePasswordLockoutError(SlmpRemotePasswordError):
    """``0xC815`` ``0xC816`` -- too many failures; the target is refusing attempts.

    Retrying is the specific thing that makes this worse.
    """


class SlmpCpuError(SlmpEndCodeError):
    """``0x4000``-``0x4FFF`` -- the CPU module said no, not the Ethernet side.

    The split matters for where you look: a ``0xC0xx`` is the SLMP/Ethernet service and
    a ``0x40xx`` is the CPU itself, and the two live in different manuals.
    """


class SlmpCpuUnsupportedRequestError(SlmpCpuError):
    """``0x4001`` ``0x4002`` -- the CPU does not implement this request."""


class SlmpCpuDataTooLargeError(SlmpCpuError):
    """``0x4005`` -- too much data for this request."""


class SlmpCpuRunningError(SlmpCpuError):
    """``0x4010`` ``0x4013`` -- the CPU is running; set it to STOP.

    This is the FX5's write-during-RUN refusal.
    """


class SlmpCpuFileError(SlmpCpuError):
    """``0x4021`` ``0x4022`` ``0x4025`` ``0x4027`` ``0x4029`` ``0x402C`` -- file access."""


class SlmpCpuDeviceSpecError(SlmpCpuError):
    """``0x4030`` ``0x4031`` ``0x4032`` -- the CPU rejected the device specification."""


class SlmpCpuModuleError(SlmpCpuError):
    """``0x4040`` ``0x4041`` ``0x4042`` ``0x4043`` -- intelligent function module access."""


class SlmpCpuRemoteDisabledError(SlmpCpuError):
    """``0x408B`` -- Remote Reset is not enabled in the CPU parameters.

    Our own bench CPU is in this state, and GX Works3 says so explicitly after a
    parameter write: new Ethernet parameters need a physical power cycle.
    """


# ========================================================================================
# 3.1 -- end code 0x0000, and it still is not true.
# ========================================================================================


class SlmpSemanticError(SlmpError):
    """The PLC said ``0x0000`` and the thing nevertheless did not happen.

    This class exists because Mitsubishi documents one of these in so many words:
    Remote RUN with the switch in STOP "will be completed normally. However, the access
    destination does not become the RUN state."
    """


class SlmpRemoteStateNotReachedError(SlmpSemanticError):
    """Remote RUN / STOP / PAUSE returned ``0x0000`` and SD203 says otherwise.

    ``verify=True`` is the **default** (graft G15) precisely because the successful end
    code is not evidence. The second round trip is the correct price.
    """

    def __init__(
        self,
        message: str,
        *,
        requested: str,
        actual: str,
        diagnostics: Diagnostics | None = None,
    ) -> None:
        super().__init__(message, diagnostics=diagnostics)
        self.requested = requested
        self.actual = actual


class SlmpVerificationError(SlmpSemanticError):
    """A ``verify=True`` read-back disagreed with what was written."""

    def __init__(
        self,
        message: str,
        *,
        written: object,
        read_back: object,
        diagnostics: Diagnostics | None = None,
    ) -> None:
        super().__init__(message, diagnostics=diagnostics)
        self.written = written
        self.read_back = read_back


class SlmpProfileMismatchError(SlmpSemanticError):
    """The ``0x0101`` model code is not one this profile claims.

    **There is no generic profile and no fallback.** An unrecognised model code is an
    error, never a radix guess: reading FX5 ``X``/``Y`` as hexadecimal instead of octal
    is silently two points off at ``Y20`` and worse as the address grows.
    ``aslmp identify <host>`` prints the profile string to pass.
    """

    def __init__(
        self,
        message: str,
        *,
        model_code: int,
        model: str,
        profile_key: str,
        diagnostics: Diagnostics | None = None,
    ) -> None:
        super().__init__(message, diagnostics=diagnostics)
        self.model_code = model_code
        self.model = model
        self.profile_key = profile_key


class SlmpTargetChangedError(SlmpSemanticError):
    """The CPU on the other end of the socket is not the one we bound to (graft G6).

    A prebuilt ``0x0403`` frame carried across a reconnect into a different D-memory
    layout returns plausible floats. ``Supervisor`` re-runs the identify half of the
    handshake, and ``plan.read()`` compares ``client.identity.model_code`` against
    ``bound_model_code``; either raises this rather than reading.
    """

    def __init__(
        self,
        message: str,
        *,
        expected_model_code: int | None,
        actual_model_code: int | None,
        bound_generation: int | None = None,
        diagnostics: Diagnostics | None = None,
    ) -> None:
        super().__init__(message, diagnostics=diagnostics)
        self.expected_model_code = expected_model_code
        self.actual_model_code = actual_model_code
        self.bound_generation = bound_generation


class SlmpSinkError(SlmpSemanticError):
    """A user callback raised.

    Surfaced at most once per generation and counted in ``counters.sink_errors``: a
    sink that raises on every transaction must not turn one broken callback into an
    unusable connection, and must not be swallowed either.
    """

    def __init__(
        self, message: str, *, sink: str, diagnostics: Diagnostics | None = None
    ) -> None:
        super().__init__(message, diagnostics=diagnostics)
        self.sink = sink


# ========================================================================================
# 3.1 -- a sibling of the whole tree, deliberately.
# ========================================================================================


class OutcomeUnknownReason(enum.Enum):
    """Why the outcome of a state-changing request cannot be known."""

    TIMEOUT = "timeout"
    SEND_INCOMPLETE = "send_incomplete"
    CONNECTION_LOST = "connection_lost"
    CANCELLED = "cancelled"
    RESPONSE_CORRUPT = "response_corrupt"


class SlmpOutcomeUnknownError(SlmpError):
    """A **state-changing** request failed after its bytes went out (graft G8).

    The write may or may not have happened. Not a :class:`SlmpTransportError`, on
    purpose (section 3.2 decision 2): ``except SlmpTransportError: retry()`` is right
    for a read and a data-loss bug for a write, and nesting this under it re-arms
    exactly that bug. Reads never raise it -- a failed read has no outcome to be
    unknown about -- and the classification is driven by ``Command.mutates`` so a new
    command cannot forget the distinction.

    The underlying failure is always the ``__cause__``.
    """

    def __init__(
        self,
        message: str,
        *,
        reason: OutcomeUnknownReason,
        sent: bool,
        diagnostics: Diagnostics | None = None,
    ) -> None:
        super().__init__(message, diagnostics=diagnostics)
        self.reason = reason
        self.sent = sent

    def details(self) -> list[str]:
        lines = super().details()
        went_out = "the request reached the OS" if self.sent else "nothing was sent"
        lines += _labelled(
            "outcome",
            f"unknown ({self.reason.value}); {went_out}. The write may or may not have "
            f"happened -- read the affected devices back before deciding.",
        )
        return lines
