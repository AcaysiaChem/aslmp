"""Joining the generated table to the hand-written classes, and nothing else.

Layer 0.5. Imports :mod:`aslmp.errors`, :mod:`aslmp.errors.endcodes` and ``aslmp.wire``.

Four pure functions, each of which exists because getting it wrong is a failure *in
the error path*, where a second exception costs the afternoon:

:func:`end_code_error`
    A :class:`~aslmp.wire.raw.RawResponse` with a non-zero end code becomes the right
    :class:`~aslmp.errors.SlmpEndCodeError` subclass with every field populated -- the
    code, Mitsubishi's own description, the cause and action from the table, both access
    routes, the echoed command, any command-defined trailer, and the request that caused
    it. A code nobody has written down gets a **synthesised** record rather than a
    ``KeyError``: ``0xC059`` must never surface as "error 49241", and never as
    ``pymelsec``'s ``'0x49241'``, which looks like hexadecimal and is the decimal with
    ``0x`` glued on the front.
:func:`timeout_causes`
    DESIGN section 3.4 (graft G3): silence is classified from what was observed rather
    than from a fixed list. It lives here, below ``transport/``, so that the rule is a
    pure function of two integers and is unit-testable with no socket; ``transport/``
    calls it at the one site that knows both numbers.
:func:`protocol_error_for`
    ``aslmp.wire`` raises its own layer-0 ``SlmpFrameError`` family -- it cannot import
    :mod:`aslmp.errors` without closing a cycle -- so exactly one table translates those
    into the public :class:`~aslmp.errors.SlmpProtocolError` tree instead of every call
    site remembering the mapping.
:func:`usage_error_for`
    The same thing for the pre-transport half. ``parse_address("Y8", FX5U)`` raises
    ``wire.address.SlmpRadixDigitError``, which is a ``ValueError`` but is **not** a
    :class:`~aslmp.errors.SlmpUsageError`; without one table translating it, DESIGN
    section 3.1's promise holds for a device the CPU does not have and quietly fails
    for a mistyped literal.

The class-name column of ``data/end_codes.tsv`` is validated **at import**, not at
call time. A row naming a class that does not exist is a build failure; discovering it
while rendering a ``0xC056`` is not an option.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from aslmp.errors import (
    UNDOCUMENTED_END_CODE,
    ClientSummary,
    Diagnostics,
    SlmpAddressRangeError,
    SlmpAddressSyntaxError,
    SlmpAsciiConversionError,
    SlmpBitPointCountError,
    SlmpBusyError,
    SlmpCapabilityError,
    SlmpConfigurationError,
    SlmpConnectionStateError,
    SlmpCpuDataTooLargeError,
    SlmpCpuDeviceSpecError,
    SlmpCpuError,
    SlmpCpuFileError,
    SlmpCpuModuleError,
    SlmpCpuRemoteDisabledError,
    SlmpCpuRunningError,
    SlmpCpuUnsupportedRequestError,
    SlmpDataLengthError,
    SlmpDeviceNotAccessibleError,
    SlmpDeviceRadixError,
    SlmpDeviceRangeError,
    SlmpEncodingNotSupportedError,
    SlmpEndCodeError,
    SlmpFrameFormatError,
    SlmpMonitorNotRegisteredError,
    SlmpPlcFramingError,
    SlmpPlcTimeoutError,
    SlmpPointCountError,
    SlmpProtocolError,
    SlmpRandomPointCountError,
    SlmpRemotePasswordError,
    SlmpRemotePasswordLockedError,
    SlmpRemotePasswordLockoutError,
    SlmpRequestContentError,
    SlmpRoutingError,
    SlmpSerialMismatchError,
    SlmpShortDatagramError,
    SlmpTrailingDataError,
    SlmpUnknownDeviceError,
    SlmpUnsolicitedFrameError,
    SlmpUnsupportedCommandError,
    SlmpUsageError,
    SlmpWordPointCountError,
    SlmpWriteProtectedError,
    TimeoutCause,
    TransactionSummary,
)
from aslmp.errors.endcodes import END_CODES, EndCodeInfo
from aslmp.wire import address as wire_address
from aslmp.wire import devspec as wire_devspec
from aslmp.wire import raw as wire_raw
from aslmp.wire import route as wire_route
from aslmp.wire.citations import Provenance
from aslmp.wire.raw import RawResponse, RequestSummary

__all__ = [
    "END_CODE_CLASSES",
    "end_code_error",
    "end_code_info",
    "protocol_error_for",
    "timeout_causes",
    "usage_error_for",
]


# ========================================================================================
# code -> class
# ========================================================================================

_CANDIDATES: Final[tuple[type[SlmpEndCodeError], ...]] = (
    SlmpAsciiConversionError,
    SlmpBitPointCountError,
    SlmpBusyError,
    SlmpConnectionStateError,
    SlmpCpuDataTooLargeError,
    SlmpCpuDeviceSpecError,
    SlmpCpuError,
    SlmpCpuFileError,
    SlmpCpuModuleError,
    SlmpCpuRemoteDisabledError,
    SlmpCpuRunningError,
    SlmpCpuUnsupportedRequestError,
    SlmpDataLengthError,
    SlmpDeviceNotAccessibleError,
    SlmpDeviceRangeError,
    SlmpEndCodeError,
    SlmpMonitorNotRegisteredError,
    SlmpPlcFramingError,
    SlmpPlcTimeoutError,
    SlmpPointCountError,
    SlmpRandomPointCountError,
    SlmpRemotePasswordError,
    SlmpRemotePasswordLockedError,
    SlmpRemotePasswordLockoutError,
    SlmpRequestContentError,
    SlmpRoutingError,
    SlmpUnsupportedCommandError,
    SlmpWordPointCountError,
    SlmpWriteProtectedError,
)
"""Every hand-written end-code class, as an explicit list.

Explicit rather than a ``__subclasses__()`` walk: a class that stops being reachable
from a row should be a test failure a human reads, not a table that silently reshapes
itself when somebody adds a subclass in another module.
"""

END_CODE_CLASSES: Final[Mapping[str, type[SlmpEndCodeError]]] = {
    cls.__name__: cls for cls in _CANDIDATES
}
"""Class name (as written in ``data/end_codes.tsv``) -> the class itself."""


def _validate_table() -> None:
    """Refuse to import if a row names a class that does not exist.

    A stale class name is a data-integrity failure in the one place this library keeps
    its facts, and the moment to discover it is the moment the package is imported --
    not the moment a plant PC is trying to explain a ``0xC056``.
    """
    unknown = sorted(
        {
            info.exception_class
            for info in END_CODES.values()
            if info.exception_class and info.exception_class not in END_CODE_CLASSES
        }
    )
    if unknown:
        raise RuntimeError(
            f"aslmp/data/end_codes.tsv names exception classes that aslmp.errors does "
            f"not define: {unknown}. The class names are hand-written public API; add "
            f"the class to aslmp/errors/__init__.py and to _CANDIDATES here, or fix the "
            f"exception_class column."
        )


_validate_table()


# ========================================================================================
# end codes
# ========================================================================================

_CPU_RANGE_FIRST: Final = 0x4000
_CPU_RANGE_LAST: Final = 0x4FFF
_UINT16_MAX: Final = 0xFFFF

_UNDOCUMENTED_ACTION: Final = (
    "Look the code up in the error-code list for this CPU, and please report it so the "
    "table gains a row: aslmp/data/end_codes.tsv is data and a new code is a one-line "
    "change."
)


def end_code_info(code: int) -> EndCodeInfo:
    """The table row for ``code``, or a synthesised record for one we have never seen.

    Never raises for an unrecognised code and never returns ``None`` -- DESIGN section
    3.2 decision 4: an unknown end code still produces a named class, the code still
    formats as ``0x%04X``, and the caller is asked to report it with the CPU model and
    firmware. Codes in ``0x4000``-``0x4FFF`` synthesise as
    :class:`~aslmp.errors.SlmpCpuError` because that whole range is documented as CPU
    module errors rather than SLMP/Ethernet service errors, which is a real fork in
    where the reader should look.
    """
    if not isinstance(code, int) or isinstance(code, bool):
        raise TypeError(f"end code must be an int, not {type(code).__name__}")
    if not 0 <= code <= _UINT16_MAX:
        raise SlmpConfigurationError(
            f"end code {code} does not fit the unsigned 16-bit end-code field "
            f"(0x0000-0xFFFF). Nothing here masks or wraps a wire field."
        )
    known = END_CODES.get(code)
    if known is not None:
        return known
    cpu = _CPU_RANGE_FIRST <= code <= _CPU_RANGE_LAST
    return EndCodeInfo(
        code=code,
        name="undocumented_end_code",
        exception_class=SlmpCpuError.__name__ if cpu else "",
        description=UNDOCUMENTED_END_CODE,
        likely_cause=(
            "A CPU module error code (0x4000-0x4FFF): the CPU said no, not the "
            "Ethernet side."
            if cpu
            else "Not a code this library has a row for."
        ),
        caller_action=_UNDOCUMENTED_ACTION,
        provenance=Provenance.INFERRED,
        citation=None,
        measurement=None,
        note="",
    )


def end_code_error(
    raw: RawResponse,
    *,
    request: RequestSummary | None = None,
    tx: TransactionSummary | None = None,
    client: ClientSummary | None = None,
    sent_frame: bytes | None = None,
) -> SlmpEndCodeError:
    """Turn an abnormal response into the exception for it, fully populated.

    ``raw`` must be abnormal: an end code of ``0x0000`` here would mean the caller
    decided a successful response was a failure, which is the one bug this whole
    library is written against, so it raises rather than manufacturing an error object
    for a reading that worked.

    The echoed command, the responding station and any command-defined trailer come
    from ``raw.error_info`` and ``raw.extra_error_data``, which
    :class:`~aslmp.wire.raw.RawResponse` validated before believing --
    SH(NA)-080956ENG-M p.28 warns the error responding station "may differ from the
    request message", and a half-read block names a station nobody addressed.
    """
    if raw.end_code == 0:
        raise SlmpConfigurationError(
            "end_code_error() was called for a response whose end code is 0x0000. That "
            "is a normal completion; there is no error to describe. Check the caller's "
            "success test rather than manufacturing an exception for a reading that "
            "worked."
        )
    info = end_code_info(raw.end_code)
    cls = END_CODE_CLASSES.get(info.exception_class, SlmpEndCodeError)
    error_info = raw.error_info
    return cls(
        info,
        command=None if error_info is None else error_info.command,
        subcommand=None if error_info is None else error_info.subcommand,
        request_route=raw.route,
        responding_station=None if error_info is None else error_info.responding,
        error_data=raw.extra_error_data,
        diagnostics=Diagnostics(
            client=client,
            request=request,
            tx=tx,
            sent_frame=sent_frame,
            received_frame=raw.raw,
        ),
    )


# ========================================================================================
# silence
# ========================================================================================


def timeout_causes(
    *, bytes_received: int, completed_transactions: int
) -> tuple[TimeoutCause, ...]:
    """Order the explanations for a deadline that expired (DESIGN section 3.4).

    Three observations, three orderings, all measured on FX5U-32MT/DS fw 1.065:

    * **Partial bytes.** Fewer bytes than ``prefix + L`` means the responder started an
      answer and stopped, or -- the case that costs an afternoon -- the *request's* own
      length field was overstated, so the CPU is still blocked waiting for bytes that
      will never come. That looks exactly like a dead PLC, and it is first in the list.
    * **No bytes at all, on a connection that has never completed a transaction.** The
      per-connection facts are still unproven: coding, frame format, protocol, port, and
      whether the single SLMP entry was already in use. A coding mismatch is silent by
      design on this path.
    * **No bytes at all, after this connection has worked.** The coding cannot have
      changed under a live socket, so the candidates are the CPU and the network.

    ``completed_transactions`` is the count for the current **generation**: a reconnect
    or a UDP rebind resets it, because the new socket has proven nothing.
    """
    if bytes_received < 0:
        raise SlmpConfigurationError(
            f"bytes_received must not be negative; got {bytes_received}."
        )
    if completed_transactions < 0:
        raise SlmpConfigurationError(
            f"completed_transactions must not be negative; got {completed_transactions}."
        )
    if bytes_received > 0:
        return (
            TimeoutCause.REQUEST_LENGTH_OVERSTATED,
            TimeoutCause.NETWORK,
            TimeoutCause.CPU_BUSY,
        )
    if completed_transactions == 0:
        return (
            TimeoutCause.CODING_MISMATCH,
            TimeoutCause.FRAME_NOT_ACCEPTED,
            TimeoutCause.PROTOCOL_MISMATCH,
            TimeoutCause.WRONG_PORT,
            TimeoutCause.ENTRY_BUSY,
        )
    return (
        TimeoutCause.PLC_STOPPED_OR_RESET,
        TimeoutCause.CPU_BUSY,
        TimeoutCause.NETWORK,
    )


# ========================================================================================
# wire frame errors -> the public protocol tree
# ========================================================================================

# Most derived first: wire's SlmpShortFrameError and SlmpTrailingDataError are both
# subclasses of wire's SlmpFrameFormatError, so a walk in declaration order would
# collapse them all into one class.
_PROTOCOL_MAP: Final[tuple[tuple[type[Exception], type[SlmpProtocolError]], ...]] = (
    (wire_raw.SlmpShortFrameError, SlmpShortDatagramError),
    (wire_raw.SlmpTrailingDataError, SlmpTrailingDataError),
    (wire_raw.SlmpErrorInfoError, SlmpFrameFormatError),
    (wire_raw.SlmpSerialMismatchError, SlmpSerialMismatchError),
    (wire_raw.SlmpUnsolicitedFrameError, SlmpUnsolicitedFrameError),
    (wire_raw.SlmpIncompleteFrameError, SlmpFrameFormatError),
    (wire_raw.SlmpFrameFormatError, SlmpFrameFormatError),
    (wire_raw.SlmpFrameError, SlmpFrameFormatError),
)


def protocol_error_for(
    exc: wire_raw.SlmpFrameError,
    *,
    diagnostics: Diagnostics | None = None,
) -> SlmpProtocolError:
    """Re-raise a layer-0 frame error as its public :class:`SlmpProtocolError` face.

    ``aslmp.wire`` is layer 0 and :mod:`aslmp.errors` is layer 0.5, so the parser cannot
    raise the public class without closing an import cycle. This is the one table that
    translates, and it exists so that the connection layer -- which is the only place
    that knows the target, the frames and the timing -- attaches them in one call:

    .. code-block:: python

        try:
            response = accumulator.take()
        except wire_raw.SlmpFrameError as frame_error:
            raise protocol_error_for(frame_error, diagnostics=diag) from frame_error

    The wire error is set as ``__cause__`` here as well, so a caller who forgets the
    ``from`` clause still keeps the original traceback.

    :class:`~aslmp.errors.SlmpRouteMismatchError` and
    :class:`~aslmp.errors.SlmpPayloadShapeError` are deliberately absent from the
    mapping: neither is decidable from a frame alone. A route mismatch needs the route
    the request went out on, and a payload-shape failure needs the request's point
    layout; both are raised by the layer that holds those, not translated from here.
    """
    for wire_class, public_class in _PROTOCOL_MAP:
        if isinstance(exc, wire_class):
            error = public_class(str(exc), diagnostics=diagnostics)
            error.__cause__ = exc
            return error
    raise TypeError(
        f"protocol_error_for() takes an aslmp.wire frame error, not "
        f"{type(exc).__name__}. Nothing here guesses a class for an exception it does "
        f"not recognise: an unrecognised failure must reach the caller as itself."
    )


# ========================================================================================
# layer-0 usage refusals -> the public SlmpUsageError faces
# ========================================================================================

# Most derived first. The four address classes are siblings, but the base
# ``SlmpAddressError`` and ``SlmpDeviceSpecError`` rows must come last or a walk in
# declaration order would collapse every subclass into the base's face.
_USAGE_MAP: Final[tuple[tuple[type[Exception], type[SlmpUsageError]], ...]] = (
    (wire_address.SlmpAddressTextError, SlmpAddressSyntaxError),
    (wire_address.SlmpUnknownPrefixError, SlmpUnknownDeviceError),
    (wire_address.SlmpRadixDigitError, SlmpDeviceRadixError),
    (wire_address.SlmpDeviceIndexError, SlmpAddressRangeError),
    (wire_address.SlmpAddressError, SlmpAddressSyntaxError),
    (wire_devspec.SlmpSpecFormatError, SlmpCapabilityError),
    (wire_devspec.SlmpNotationError, SlmpEncodingNotSupportedError),
    (wire_devspec.SlmpDeviceSpecError, SlmpConfigurationError),
    (wire_route.SlmpRouteError, SlmpConfigurationError),
)


def usage_error_for(
    exc: ValueError,
    *,
    diagnostics: Diagnostics | None = None,
) -> SlmpUsageError:
    """Re-raise a layer-0 parse or encode refusal as its public :class:`SlmpUsageError`.

    The counterpart of :func:`protocol_error_for`, for the pre-transport half.
    ``aslmp.wire`` is layer 0 and :mod:`aslmp.errors` is layer 0.5, so ``parse_address``
    cannot raise ``SlmpDeviceRadixError`` without closing an import cycle that
    ``tests/unit/test_layering.py`` fails the build for. It raises
    ``wire.address.SlmpRadixDigitError`` instead, and **this is the one table that
    translates**, so that ``Y8`` and ``D8000`` do not reach a caller as two unrelated
    kinds of exception:

    .. code-block:: python

        try:
            return parse_address(literal, profile)
        except wire_address.SlmpAddressError as parse_error:
            raise usage_error_for(parse_error) from parse_error

    Without it, DESIGN section 3.1's promise -- validation raises a ``SlmpUsageError``
    subclass and nothing was sent -- holds for a device this CPU does not have
    (``V0``, refused by the profile at layer 1, which *can* import this package) and
    silently fails for a mistyped literal (``Y8``, refused at layer 0). ``Y8`` is the
    likelier mistake of the two and the one an FX5U-32MT/DS on firmware 1.065 was
    measured to *accept* with end code ``0x0000``, so it is exactly the refusal a
    caller most needs to be able to name in an ``except`` clause.

    The layer-0 error becomes ``__cause__``, which is where the machine-readable detail
    stays: ``SlmpRadixDigitError`` carries ``.character``, ``.position``, ``.radix``,
    ``.device`` and ``.text``, and nothing here has to re-parse a message to get them.
    """
    for wire_class, public_class in _USAGE_MAP:
        if isinstance(exc, wire_class):
            error = public_class(str(exc), diagnostics=diagnostics)
            error.__cause__ = exc
            return error
    raise TypeError(
        f"usage_error_for() takes an aslmp.wire address, device-specification or "
        f"route refusal, not {type(exc).__name__}. Nothing here guesses a class for "
        f"an exception it does not recognise: an unrecognised failure must reach the "
        f"caller as itself."
    )
