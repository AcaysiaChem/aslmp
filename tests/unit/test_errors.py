"""U5 -- the exception hierarchy, the end-code table and the routing between them.

The properties this file exists to hold, in the order DESIGN section 3 states them:

* **Every class in the tree is accounted for.** Either a row of ``data/end_codes.tsv``
  reaches it, or it is named in :data:`NOT_FROM_END_CODES` below. A class that quietly
  stops being reachable is a class nobody can ever catch.
* **``str(e)`` never raises, for every row in the table.** That test exists because
  ``pymelsec`` renders ``0xC056`` as the string ``'0x49238'`` -- the decimal with ``0x``
  glued on the front -- and then ``str(e)`` raises ``TypeError``.
* **No end code is ever rendered in decimal.** ``0xC059`` must not surface as
  "error 49241" and must not surface as ``'0x49241'``.
* **The timeout causes are computed**, and the three measured observations produce
  three different orderings.
* **A lost UDP request is its own error**, carrying the serial and the in-flight depth,
  and is not a :class:`TimeoutError`.

``Transaction`` and ``TransactionTiming`` are imported from :mod:`aslmp.timing` and
bound to :class:`aslmp.errors.TransactionSummary` / ``TimingSummary`` in
:func:`test_a_real_transaction_satisfies_the_structural_protocol`, so that the
structural compatibility between Layer 2.5 and Layer 0.5 is checked by ``mypy`` rather
than hoped for. ``aslmp.errors`` cannot import ``aslmp.timing``; the test can.
"""

from __future__ import annotations

import enum
import pickle
import re

import pytest

from aslmp import errors
from aslmp.errors import (
    Diagnostics,
    OutcomeUnknownReason,
    SlmpConfigurationError,
    SlmpCpuError,
    SlmpDatagramLostError,
    SlmpEndCodeError,
    SlmpError,
    SlmpOutcomeUnknownError,
    SlmpPlcFramingError,
    SlmpProtocolError,
    SlmpRequestContentError,
    SlmpTimeoutError,
    SlmpTransportError,
    SlmpUnsupportedCommandError,
    SlmpUsageError,
    SlmpWordPointCountError,
    TargetInfo,
    TimeoutCause,
    TimingSummary,
    TransactionSummary,
)
from aslmp.errors.endcodes import END_CODES, EndCodeInfo
from aslmp.errors.routing import (
    END_CODE_CLASSES,
    end_code_error,
    end_code_info,
    protocol_error_for,
    timeout_causes,
)
from aslmp.timing import Chunk, Nanos, Transaction, TransactionTiming
from aslmp.wire import raw as wire_raw
from aslmp.wire.frames import THREE_E
from aslmp.wire.raw import ErrorInfo, RawResponse
from aslmp.wire.route import Route

# ------------------------------------------------------------------------------------
# Fixtures that stand in for layers this one may not import
# ------------------------------------------------------------------------------------


class Option(enum.Enum):
    """A stand-in for ``TransportKind`` / ``Encoding`` / ``FrameType`` (Layer 0/1)."""

    TCP = "tcp"
    BINARY = "binary"
    THREE_E = "3E"


class FakeRequest:
    """A stand-in for whatever ``Command.summary()`` returns (Layer 2)."""

    command = 0x0801
    subcommand = 0x0000
    request_bytes = 20

    def describe(self) -> str:
        return "monitor_register(['D0', 'D4', 'D8'])"


TARGET = TargetInfo(
    peer=("192.168.10.250", 5000),
    transport=Option.TCP,
    encoding=Option.BINARY,
    frame=Option.THREE_E,
    model="FX5U-32MT/DS",
    model_code=0x4A49,
)

# The 20-byte abnormal response measured on FX5U-32MT/DS fw 1.065 (L = 0x000B, 16 of 16
# provocations): subheader D0 00, route, L, end code 59 C0, error block, echoed 01 08.
ABNORMAL_C059 = bytes.fromhex("D00000FFFF03000B0059C000FFFF030001080000")


def abnormal(end_code: int, *, command: int = 0x0801) -> RawResponse:
    return RawResponse(
        frame=THREE_E,
        serial=None,
        route=Route.OWN_STATION,
        length=0x000B,
        end_code=end_code,
        payload=b"",
        error_info=ErrorInfo(
            responding=Route.OWN_STATION, command=command, subcommand=0x0000
        ),
        extra_error_data=b"",
        raw=ABNORMAL_C059,
        subheader_tail=b"",
    )


def timing(*, answered: bool = True) -> TransactionTiming:
    if not answered:
        return TransactionTiming(
            submitted_at=Nanos(0),
            gate_acquired_at=Nanos(1_000),
            encoded_at=Nanos(2_000),
            sent_at=Nanos(3_000),
        )
    return TransactionTiming(
        submitted_at=Nanos(0),
        gate_acquired_at=Nanos(1_000),
        encoded_at=Nanos(2_000),
        sent_at=Nanos(3_000),
        first_byte_at=Nanos(7_100_000),
        received_at=Nanos(7_310_000),
        decoded_at=Nanos(7_320_000),
        chunks=(Chunk(nbytes=20, at=Nanos(7_100_000)), Chunk(nbytes=4, at=Nanos(7_310_000))),
    )


def transaction(*, answered: bool = True) -> Transaction:
    return Transaction(
        timing=timing(answered=answered),
        sequence=12,
        connection_id="c-1",
        generation=0,
        after_reconnect=False,
        command=0x0801,
        subcommand=0x0000,
        frame=Option.THREE_E,
        encoding=Option.BINARY,
        transport=Option.TCP,
        serial=None,
        request_bytes=20,
        response_bytes=20,
        end_code=0xC059,
        prebuilt=False,
        request_frame=bytes(range(24)),
        response_frame=ABNORMAL_C059,
    )


FULL = Diagnostics(
    client=TARGET,
    request=FakeRequest(),
    tx=transaction(),
    sent_frame=bytes(range(30)),
    requested_route=Route.OWN_STATION,
    responded_route=Route(network=0x01, station=0x02, module_io=0x03FF, multidrop=0x00),
)


# ------------------------------------------------------------------------------------
# Structural protocols: proved by mypy, exercised at run time
# ------------------------------------------------------------------------------------


def test_a_real_transaction_satisfies_the_structural_protocol() -> None:
    """``aslmp.timing.Transaction`` is a ``TransactionSummary`` -- checked by mypy.

    ``aslmp.errors`` is Layer 0.5 and ``aslmp.timing`` is Layer 2.5, so the exception
    tree types ``.tx`` structurally. If that structure ever drifts, this assignment is
    a type error rather than an ``AttributeError`` inside ``__str__``.
    """
    tx: TransactionSummary = transaction()
    stamps: TimingSummary = tx.timing
    assert tx.sequence == 12
    assert stamps.received_at is not None


def test_target_info_satisfies_client_summary() -> None:
    client: errors.ClientSummary = TARGET
    assert client.peer == ("192.168.10.250", 5000)


# ------------------------------------------------------------------------------------
# The tree is complete and accounted for
# ------------------------------------------------------------------------------------

# Every exported class that is deliberately NOT reachable from an end-code row. These
# are the pre-transport, transport, protocol, semantic and outcome-unknown branches:
# there is no end code for them because either nothing was sent, or nothing well-formed
# came back, or the PLC said 0x0000 and it still was not true.
NOT_FROM_END_CODES = frozenset(
    {
        "SlmpError",
        "SlmpUsageError",
        "SlmpAddressSyntaxError",
        "SlmpUnknownDeviceError",
        "SlmpDeviceRadixError",
        "SlmpDeviceNotOnCpuError",
        "SlmpAddressRangeError",
        "SlmpPointLimitError",
        "SlmpDeviceNotAllowedHereError",
        "SlmpCapabilityError",
        "SlmpEncodingNotSupportedError",
        "SlmpMonitoringTimerError",
        "SlmpBlockLayoutError",
        "SlmpValueRangeError",
        "SlmpConfigurationError",
        "SlmpConcurrentTransactionError",
        "SlmpTransportError",
        "SlmpConnectionEntryBusyError",
        "SlmpNotConnectedError",
        "SlmpConnectionLostError",
        "SlmpHandshakeError",
        "SlmpNotSentError",
        "SlmpDatagramSourceError",
        "SlmpDatagramLostError",
        "SlmpTimeoutError",
        "SlmpProtocolError",
        "SlmpFrameFormatError",
        "SlmpSerialMismatchError",
        "SlmpRouteMismatchError",
        "SlmpShortDatagramError",
        "SlmpTrailingDataError",
        "SlmpUnsolicitedFrameError",
        "SlmpPayloadShapeError",
        "SlmpSemanticError",
        "SlmpRemoteStateNotReachedError",
        "SlmpVerificationError",
        "SlmpProfileMismatchError",
        "SlmpTargetChangedError",
        "SlmpSinkError",
        "SlmpOutcomeUnknownError",
        # SlmpEndCodeError itself is the base and is reached by every row whose
        # exception_class column is empty; it is listed here so the two sets partition.
        "SlmpEndCodeError",
    }
)


def exported_error_classes() -> dict[str, type[SlmpError]]:
    found: dict[str, type[SlmpError]] = {}
    for name in errors.__all__:
        obj = getattr(errors, name)
        if isinstance(obj, type) and issubclass(obj, SlmpError):
            found[name] = obj
    return found


def test_every_row_names_a_real_exported_class() -> None:
    """The ``exception_class`` column is a class name, and the class must exist."""
    exported = exported_error_classes()
    missing = sorted(
        {
            info.exception_class
            for info in END_CODES.values()
            if info.exception_class and info.exception_class not in exported
        }
    )
    assert not missing, (
        f"data/end_codes.tsv names classes that aslmp.errors does not export: {missing}"
    )


def test_every_row_class_is_an_end_code_class() -> None:
    for info in END_CODES.values():
        if not info.exception_class:
            continue
        cls = END_CODE_CLASSES[info.exception_class]
        assert issubclass(cls, SlmpEndCodeError), (
            f"0x{info.code:04X} names {info.exception_class}, which is not a "
            f"SlmpEndCodeError subclass"
        )


def test_every_class_is_reachable_from_a_row_or_explicitly_listed() -> None:
    """No orphan classes, and no stale entries in the explicit list either."""
    exported = exported_error_classes()
    reachable = {
        info.exception_class for info in END_CODES.values() if info.exception_class
    }
    unaccounted = sorted(set(exported) - reachable - NOT_FROM_END_CODES)
    assert not unaccounted, (
        f"these exported classes are neither reachable from a row of end_codes.tsv nor "
        f"listed as transport-only in NOT_FROM_END_CODES: {unaccounted}"
    )
    stale = sorted(NOT_FROM_END_CODES - set(exported))
    assert not stale, f"NOT_FROM_END_CODES names classes that no longer exist: {stale}"
    overlap = sorted(reachable & NOT_FROM_END_CODES - {"SlmpEndCodeError"})
    assert not overlap, (
        f"these classes are listed as transport-only but a row reaches them: {overlap}"
    )


def test_the_end_code_class_table_covers_every_end_code_subclass() -> None:
    """``routing.END_CODE_CLASSES`` is written by hand; it must not fall behind."""
    exported = exported_error_classes()
    subclasses = {
        name
        for name, cls in exported.items()
        if issubclass(cls, SlmpEndCodeError)
    }
    assert subclasses == set(END_CODE_CLASSES), (
        "routing._CANDIDATES and the exported SlmpEndCodeError subclasses disagree: "
        f"only in errors: {sorted(subclasses - set(END_CODE_CLASSES))}; "
        f"only in routing: {sorted(set(END_CODE_CLASSES) - subclasses)}"
    )


def test_the_documented_tree_shape_holds() -> None:
    """The four decisions of DESIGN section 3.2, as assertions."""
    assert issubclass(SlmpUsageError, ValueError)
    assert issubclass(SlmpTimeoutError, TimeoutError)
    assert issubclass(SlmpTimeoutError, SlmpTransportError)
    # Decision 2: a sibling, so `except SlmpTransportError: retry()` cannot swallow it.
    assert not issubclass(SlmpOutcomeUnknownError, SlmpTransportError)
    assert issubclass(SlmpOutcomeUnknownError, SlmpError)
    # A lost datagram is never a generic timeout.
    assert not issubclass(SlmpDatagramLostError, TimeoutError)
    assert not issubclass(SlmpDatagramLostError, SlmpTimeoutError)


# ------------------------------------------------------------------------------------
# __str__ over every row: never raises, never decimal, always ASCII
# ------------------------------------------------------------------------------------

CODES = sorted(END_CODES)
IDS = [f"0x{code:04X}" for code in CODES]


def flat(text: str) -> str:
    """The rendering with its 96-column wrapping undone, for substring assertions.

    ``details()`` wraps every line, so a phrase this file cares about may legitimately
    straddle a line break. Collapsing whitespace tests the content without freezing the
    column at which it happens to break.
    """
    return " ".join(text.split())


def decimal_leak(text: str, code: int) -> str | None:
    """The first bare decimal rendering of ``code`` in ``text``, if there is one.

    ``pymelsec`` produces ``'0x49238'`` for ``0xC056``: the decimal with an ``0x``
    prefix, which reads as hexadecimal and is not. The lookbehind excludes hex digits
    so that a legitimate ``0xC056`` never matches, but deliberately does **not**
    exclude ``x`` -- catching ``0x49238`` is the point.
    """
    match = re.search(rf"(?<![0-9A-Fa-f]){code}(?![0-9])", text)
    return None if match is None else match.group(0)


@pytest.mark.parametrize("code", CODES, ids=IDS)
def test_str_never_raises_and_never_renders_a_decimal_end_code(code: int) -> None:
    info = END_CODES[code]
    error = SlmpEndCodeError(info, diagnostics=FULL)
    text = str(error)
    assert f"0x{code:04X}" in text
    if code != 0:
        # 0x0000 renders the digit 0 inside "0x0000"; every other code is >= 4 digits
        # in decimal and cannot appear by accident.
        assert decimal_leak(text, code) is None, (
            f"0x{code:04X} leaked its decimal rendering {code} into str(e):\n{text}"
        )
    if code != 0:
        # "0x0" is a legitimate substring of "0x0000"; every other code's decimal is
        # four or five digits and cannot be a prefix of its own hexadecimal rendering.
        assert f"0x{code}" not in text, "the pymelsec '0x49238' shape must never appear"


@pytest.mark.parametrize("code", CODES, ids=IDS)
def test_str_is_ascii_and_fits_the_page(code: int) -> None:
    """Rendered on plant consoles that are cp437; and wrapped to 96 columns."""
    text = str(SlmpEndCodeError(END_CODES[code], diagnostics=FULL))
    assert text.isascii(), (
        "a UnicodeEncodeError raised while reporting an end code is the worst possible "
        "second failure; plant consoles are cp437 and cp1252"
    )
    # The headline is exempt: Python does not wrap exception messages and a traceback's
    # last line is the headline, so wrapping it would put a stray indent in every
    # traceback. Only the detail block is laid out by this library.
    for line in text.splitlines()[1:]:
        if len(line) <= 96:
            continue
        longest = max(len(word) for word in line.split())
        assert longest > 96 - 12, f"line not wrapped: {line!r}"


@pytest.mark.parametrize("code", CODES, ids=IDS)
def test_str_never_raises_with_no_diagnostics_at_all(code: int) -> None:
    """The headline alone must render: an exception raised before we knew anything."""
    error = SlmpEndCodeError(END_CODES[code])
    assert str(error).startswith(f'end code 0x{code:04X} -- "')


def test_str_renders_a_transaction_that_never_got_a_response() -> None:
    """``TransactionTiming.wire_ns`` raises when nothing arrived; ``__str__`` may not.

    This is the exact shape of the ``pymelsec`` defect: a property that is right to
    raise, read from inside the code that is explaining a failure.
    """
    error = SlmpTimeoutError(
        "no response within 3.0 s",
        likely_causes=timeout_causes(bytes_received=0, completed_transactions=0),
        bytes_received=0,
        deadline_s=3.0,
        diagnostics=Diagnostics(client=TARGET, tx=transaction(answered=False)),
    )
    text = flat(str(error))
    assert "no response" in text
    assert "coding_mismatch" in text


def test_the_rendering_carries_the_section_3_6_lines() -> None:
    error = end_code_error(
        abnormal(0xC059),
        request=FakeRequest(),
        tx=transaction(),
        client=TARGET,
        sent_frame=bytes(range(30)),
    )
    text = str(error)
    labels = [line[:12].strip() for line in text.splitlines()[1:] if line[2] != " "]
    for label in ("target", "request", "sent", "received", "routes", "timing"):
        assert label in labels, f"the {label!r} line is missing from:\n{text}"
    assert "FX5U-32MT/DS (model code 0x4A49) at 192.168.10.250:5000" in text
    assert "tcp / binary / 3E" in text
    assert "00/FF/03FF/00" in text
    assert "gen 0, seq 12" in flat(text)
    assert "0x0801 sub 0x0000" in text


def test_the_class_name_is_not_repeated_inside_str() -> None:
    """Python prints ``module.Class: <str(e)>``; printing it again doubles it."""
    error = SlmpEndCodeError(END_CODES[0xC059])
    assert "SlmpEndCodeError" not in str(error)


# ------------------------------------------------------------------------------------
# routing.end_code_error
# ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (0xC059, SlmpUnsupportedCommandError),
        (0xC05C, SlmpRequestContentError),
        (0xC052, SlmpWordPointCountError),
        (0xC061, SlmpPlcFramingError),
        (0x4010, errors.SlmpCpuRunningError),
        (0xC201, errors.SlmpRemotePasswordLockedError),
    ],
)
def test_end_code_error_picks_the_class(code: int, expected: type[SlmpError]) -> None:
    error = end_code_error(abnormal(code))
    assert type(error) is expected
    assert error.end_code == code
    assert error.documented


def test_the_three_measured_corrections_carry_their_measurement() -> None:
    """0xC05C, 0xC052 and 0xC061 contradict the documents; the row must say so.

    Doc-derived mapping said 0xC05B for a bad device code, an address error for a zero
    point count, and 0xC057 for a length mismatch. FX5U-32MT/DS fw 1.065 said otherwise.
    """
    for code in (0xC05C, 0xC052, 0xC061):
        info = END_CODES[code]
        assert info.measurement is not None, f"0x{code:04X} must name the silicon"
        assert info.measurement.cpu == "FX5U-32MT/DS"
        assert info.measurement.firmware == "1.065"
        text = flat(str(end_code_error(abnormal(code))))
        assert "FX5U-32MT/DS fw 1.065" in text


def test_end_code_error_populates_every_field() -> None:
    raw = abnormal(0xC059, command=0x0801)
    error = end_code_error(raw, request=FakeRequest(), tx=transaction(), client=TARGET)
    info = END_CODES[0xC059]
    assert error.end_code == 0xC059
    assert error.name == info.name
    assert error.description == info.description
    assert error.likely_cause == info.likely_cause
    assert error.caller_action == info.caller_action
    assert error.command == 0x0801
    assert error.subcommand == 0x0000
    assert error.request_route == Route.OWN_STATION
    assert error.responding_station == Route.OWN_STATION
    assert error.error_data == b""
    assert error.source is info.measurement
    assert error.request is not None
    assert error.tx is not None
    assert error.client is TARGET


def test_end_code_error_refuses_a_normal_completion() -> None:
    """Manufacturing an error for a reading that worked is the bug, not the recovery."""
    normal = RawResponse(
        frame=THREE_E,
        serial=None,
        route=Route.OWN_STATION,
        length=0x0004,
        end_code=0x0000,
        payload=b"\x01\x02",
        error_info=None,
        extra_error_data=b"",
        raw=b"\xd0\x00",
        subheader_tail=b"",
    )
    with pytest.raises(SlmpConfigurationError, match="0x0000"):
        end_code_error(normal)


def test_an_undocumented_code_still_raises_a_named_class() -> None:
    error = end_code_error(abnormal(0xC099))
    assert type(error) is SlmpEndCodeError
    assert error.documented is False
    assert error.description == errors.UNDOCUMENTED_END_CODE
    assert error.source is None
    text = flat(str(error))
    assert "0xC099" in text
    assert "49305" not in text
    assert "report" in text
    assert "CPU model and firmware" in text


def test_an_undocumented_cpu_range_code_is_a_cpu_error() -> None:
    """0x4000-0x4FFF is the CPU module, not the Ethernet side. Different manual."""
    error = end_code_error(abnormal(0x4CCC))
    assert type(error) is SlmpCpuError
    assert error.documented is False
    assert "CPU" in error.likely_cause


@pytest.mark.parametrize("code", [-1, 0x1_0000])
def test_end_code_info_refuses_a_code_that_is_not_a_wire_field(code: int) -> None:
    with pytest.raises(SlmpConfigurationError, match="16-bit"):
        end_code_info(code)


def test_end_code_info_refuses_a_bool() -> None:
    with pytest.raises(TypeError):
        end_code_info(True)  # a bool is not an end code, and True is not 0x0001


def test_end_code_info_returns_the_shipped_row_identically() -> None:
    info = end_code_info(0xC056)
    assert info is END_CODES[0xC056]
    assert isinstance(info, EndCodeInfo)


# ------------------------------------------------------------------------------------
# routing.timeout_causes -- graft G3
# ------------------------------------------------------------------------------------


def test_silence_on_an_unproven_connection_blames_the_connection_entry_facts() -> None:
    causes = timeout_causes(bytes_received=0, completed_transactions=0)
    assert causes == (
        TimeoutCause.CODING_MISMATCH,
        TimeoutCause.FRAME_NOT_ACCEPTED,
        TimeoutCause.PROTOCOL_MISMATCH,
        TimeoutCause.WRONG_PORT,
        TimeoutCause.ENTRY_BUSY,
    )


def test_silence_on_a_working_connection_cannot_blame_the_coding() -> None:
    """The coding cannot have changed under a live socket."""
    causes = timeout_causes(bytes_received=0, completed_transactions=1)
    assert causes == (
        TimeoutCause.PLC_STOPPED_OR_RESET,
        TimeoutCause.CPU_BUSY,
        TimeoutCause.NETWORK,
    )
    assert TimeoutCause.CODING_MISMATCH not in causes


def test_partial_bytes_lead_with_the_overstated_length() -> None:
    """An overstated length blocks the CPU for bytes that never come -- measured."""
    for completed in (0, 5):
        causes = timeout_causes(bytes_received=9, completed_transactions=completed)
        assert causes[0] is TimeoutCause.REQUEST_LENGTH_OVERSTATED
        assert causes == (
            TimeoutCause.REQUEST_LENGTH_OVERSTATED,
            TimeoutCause.NETWORK,
            TimeoutCause.CPU_BUSY,
        )


def test_the_three_orderings_are_all_different() -> None:
    orderings = {
        timeout_causes(bytes_received=0, completed_transactions=0),
        timeout_causes(bytes_received=0, completed_transactions=1),
        timeout_causes(bytes_received=1, completed_transactions=1),
    }
    assert len(orderings) == 3, "the causes are enumerated, not computed"


@pytest.mark.parametrize(
    ("received", "completed"), [(-1, 0), (0, -1)]
)
def test_timeout_causes_refuses_impossible_counts(received: int, completed: int) -> None:
    with pytest.raises(SlmpConfigurationError):
        timeout_causes(bytes_received=received, completed_transactions=completed)


def test_the_timeout_error_prints_its_causes() -> None:
    error = SlmpTimeoutError(
        "no response within 3.0 s",
        likely_causes=timeout_causes(bytes_received=9, completed_transactions=3),
        bytes_received=9,
        deadline_s=3.0,
    )
    text = flat(str(error))
    assert "9 byte(s) in 3 s" in text
    assert "request_length_overstated" in text


# ------------------------------------------------------------------------------------
# The UDP lost-datagram error the hardware findings require
# ------------------------------------------------------------------------------------


def test_a_lost_datagram_names_the_serial_and_the_depth() -> None:
    """Measured: 8/8 and 32/32 clean, 44/64 at depth 64, with no error of any kind.

    Those two numbers are the diagnosis, so they are fields rather than prose.
    """
    error = SlmpDatagramLostError(
        "the 4E response for serial 0x0029 never arrived",
        serial=0x0029,
        in_flight=64,
        deadline_s=3.0,
        diagnostics=Diagnostics(client=TARGET),
    )
    assert error.serial == 0x0029
    assert error.in_flight == 64
    assert isinstance(error, SlmpTransportError)
    assert not isinstance(error, TimeoutError)
    text = flat(str(error))
    assert "0x0029" in text
    assert "64 request(s) in flight" in text
    assert "FX5U-32MT/DS fw 1.065" in text


def test_a_lost_datagram_on_3e_says_there_is_no_serial() -> None:
    error = SlmpDatagramLostError(
        "the response never arrived", serial=None, in_flight=1, deadline_s=1.0
    )
    assert "3E carries no serial" in str(error)


# ------------------------------------------------------------------------------------
# routing.protocol_error_for
# ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("wire_error", "expected"),
    [
        (wire_raw.SlmpShortFrameError("short"), errors.SlmpShortDatagramError),
        (wire_raw.SlmpTrailingDataError("long"), errors.SlmpTrailingDataError),
        (wire_raw.SlmpSerialMismatchError("serial"), errors.SlmpSerialMismatchError),
        (
            wire_raw.SlmpUnsolicitedFrameError("2101"),
            errors.SlmpUnsolicitedFrameError,
        ),
        (wire_raw.SlmpErrorInfoError("short error block"), errors.SlmpFrameFormatError),
        (wire_raw.SlmpFrameFormatError("subheader"), errors.SlmpFrameFormatError),
        (wire_raw.SlmpIncompleteFrameError("not yet"), errors.SlmpFrameFormatError),
        (wire_raw.SlmpFrameError("something"), errors.SlmpFrameFormatError),
    ],
)
def test_protocol_error_for_maps_the_wire_family(
    wire_error: wire_raw.SlmpFrameError, expected: type[SlmpProtocolError]
) -> None:
    """The most derived wire class wins: short and trailing are both format errors."""
    public = protocol_error_for(wire_error, diagnostics=Diagnostics(client=TARGET))
    assert type(public) is expected
    assert public.__cause__ is wire_error
    assert str(wire_error) in str(public)
    assert "192.168.10.250" in str(public)


def test_protocol_error_for_refuses_a_foreign_exception() -> None:
    """An unrecognised failure must reach the caller as itself, not as a guess."""
    with pytest.raises(TypeError, match=r"aslmp\.wire frame error"):
        protocol_error_for(ValueError("not a frame error"))  # type: ignore[arg-type]


# ------------------------------------------------------------------------------------
# The base class itself
# ------------------------------------------------------------------------------------


def test_diagnostics_default_to_empty_and_accessors_are_none() -> None:
    error = SlmpError("something went wrong")
    assert error.diagnostics is errors.NO_DIAGNOSTICS
    assert error.client is None
    assert error.tx is None
    assert error.request is None
    assert str(error) == "something went wrong"
    assert error.args == ("something went wrong",)


def test_a_frame_is_taken_from_the_transaction_when_not_given_explicitly() -> None:
    error = SlmpError("bad", diagnostics=Diagnostics(tx=transaction()))
    text = str(error)
    assert "sent" in text
    assert "received" in text
    assert "D0 00 00 FF FF 03" in text


def test_a_long_frame_is_truncated_and_says_how_long_it_really_was() -> None:
    error = SlmpError("bad", diagnostics=Diagnostics(sent_frame=bytes(200)))
    assert "... (200 bytes)" in flat(str(error))


def test_outcome_unknown_says_whether_the_bytes_went_out() -> None:
    sent = SlmpOutcomeUnknownError(
        "the write may or may not have happened",
        reason=OutcomeUnknownReason.TIMEOUT,
        sent=True,
    )
    assert sent.sent is True
    assert sent.reason is OutcomeUnknownReason.TIMEOUT
    assert "the request reached the OS" in str(sent)
    not_sent = SlmpOutcomeUnknownError(
        "cancelled", reason=OutcomeUnknownReason.CANCELLED, sent=False
    )
    assert "nothing was sent" in str(not_sent)


def test_the_subclasses_that_add_fields_keep_them() -> None:
    not_connected = errors.SlmpNotConnectedError("closed", reason="aclose() was called")
    assert not_connected.reason == "aclose() was called"
    changed = errors.SlmpTargetChangedError(
        "the CPU changed", expected_model_code=0x4A49, actual_model_code=0x4806
    )
    assert changed.expected_model_code == 0x4A49
    assert changed.actual_model_code == 0x4806
    mismatch = errors.SlmpProfileMismatchError(
        "not this profile", model_code=0x4806, model="R04CPU", profile_key="melsec:iq-f/fx5u"
    )
    assert mismatch.profile_key == "melsec:iq-f/fx5u"
    state = errors.SlmpRemoteStateNotReachedError(
        "still stopped", requested="RUN", actual="STOP"
    )
    assert (state.requested, state.actual) == ("RUN", "STOP")
    source = errors.SlmpDatagramSourceError(
        "wrong peer", expected_peer=("10.0.0.1", 5000), actual_peer=("10.0.0.9", 5000)
    )
    assert source.actual_peer == ("10.0.0.9", 5000)


@pytest.mark.parametrize(
    "error",
    [
        SlmpError("plain"),
        SlmpEndCodeError(END_CODES[0xC056]),
        SlmpTimeoutError(
            "silence",
            likely_causes=(TimeoutCause.NETWORK,),
            bytes_received=0,
            deadline_s=1.5,
        ),
        SlmpDatagramLostError("lost", serial=1, in_flight=32, deadline_s=1.0),
        SlmpOutcomeUnknownError(
            "unknown", reason=OutcomeUnknownReason.CONNECTION_LOST, sent=True
        ),
    ],
    ids=["base", "endcode", "timeout", "datagram", "outcome"],
)
def test_exceptions_survive_a_pickle_round_trip(error: SlmpError) -> None:
    """Subclass ``__init__`` signatures all differ, so the default reduce cannot work.

    An exception that raises ``TypeError`` on unpickling turns a diagnosable ``0xC056``
    into a traceback about the diagnosis.
    """
    restored = pickle.loads(pickle.dumps(error))
    assert type(restored) is type(error)
    assert str(restored) == str(error)
    assert restored.args == error.args
