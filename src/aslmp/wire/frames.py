"""3E and 4E frames, and the one expression in this package that produces ``L``.

Layer 0. **stdlib only.** Importing this module must not pull ``socket``, ``ssl``,
``asyncio``, ``selectors``, ``threading`` or ``logging`` into ``sys.modules``
(``tests/unit/test_layering.py`` proves it in a subprocess).

A 3E frame is a subheader, a five-field access route, a 16-bit length, and a body. A 4E
frame is the same thing with four more subheader units carrying a serial number. Nothing
else moves: every other field keeps its width, order and endianness
(SH(NA)-080956ENG-M section 4.2 pp.17-28; SH(NA)-080008-AB pp.42-48; JY997D56001-K
pp.19-22 prints the FX5's own copy of the 3E prefix and it is byte-identical).

There is **no footer, terminator or checksum** on 3E/4E over Ethernet. The "footer" box
in SH(NA)-080956ENG-M p.24 is the Ethernet/IP trailer -- *"Normally it is added
automatically"* -- and a grep for "sum check" over that manual returns nothing. Serial MC
protocol frames 1C/2C/3C/4C do have ETX and a sum check; do not port that assumption.

Subheaders are **literal byte sequences**, never ``struct.pack("<H", 0x5000)``. ``5000H``
goes on the wire as ``50 00``, which is the one place in binary coding that is not
little-endian. ProtoForge's ``mc/server.py:56`` compares ``0x0054`` against a
little-endian unpack of ``50 00`` and therefore rejects every correct client; DESIGN
section 5.6 makes that a day-one regression test.

.. rubric:: Why ``L`` gets three guards

``L`` counts wire units -- **bytes in binary coding, characters in ASCII coding** -- from
the monitoring timer (request) or the end code (response) through the end of the data,
counting neither itself nor anything before it. Because the codec has already emitted
characters by the time :func:`request_body` returns, ``len(body)`` is already the
character count in ASCII and the odd-bit-count problem (``ascii_len(n) == 2*binary_len(n)
- (n % 2)``, ``codec.py``) cannot recur here.

The failure is asymmetric, and both halves are measured on FX5U-32MT/DS fw 1.065,
2026-09-06:

* ``L`` **understated** by 2 -> end code ``0xC061``, and the connection recovers.
* ``L`` **overstated** by 2 -> **no response at all.** The CPU blocks waiting for bytes
  that never come, and it looks exactly like a dead PLC.
* ``L = 0x0000`` with a 12-byte body -> ``0xC061``, the error frame echoing
  ``cmd 0x0000 sub 0x0000`` because the CPU read the command out of the timer field.

So the arithmetic lives in exactly one expression, in one function, in this module:

1. ``tests/unit/test_frames.py`` walks this file's AST and asserts that ``L`` is assigned
   exactly once and that nothing here is named ``length``;
2. :meth:`FrameFormat.build` takes an optional ``expect_body_len`` and refuses a body
   whose length disagrees, which is where a command's ``payload_len`` two-pass helper
   turns into a runtime guard rather than a property test;
3. a property test asserts ``len(build(...)) == prefix_units(codec) + len(body)`` over
   generated inputs, in both codecs and both frame formats.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from aslmp.wire.citations import Citation, Measurement, Source
from aslmp.wire.codec import SlmpCodecError
from aslmp.wire.raw import (
    ABNORMAL_RESPONSE,
    ErrorInfo,
    RawRequest,
    RawResponse,
    SlmpErrorInfoError,
    SlmpFrameFormatError,
    SlmpSerialMismatchError,
    SlmpShortFrameError,
    SlmpTrailingDataError,
    SlmpUnsolicitedFrameError,
)
from aslmp.wire.route import Route

if TYPE_CHECKING:  # pragma: no cover - typing only
    from aslmp.wire.codec import Codec

__all__ = [
    "FOUR_E",
    "FRAMES",
    "FRAME_LAYOUT",
    "ONDEMAND_COMMAND",
    "THREE_E",
    "FrameFormat",
    "FramePrefix",
    "FrameType",
    "Subheader",
    "request_body",
    "response_body",
]


FRAME_LAYOUT: Final = Citation(
    manual="SH(NA)-080956ENG",
    revision="M",
    section="4.2 pp.17-28",
    note=(
        "3E request subheader 5000H, response D000H; 4E request 5400H, response D400H, "
        "each followed by a 2-unit serial No. and a 2-unit fixed 0000H. Then the access "
        "route (5 binary bytes / 10 ASCII characters), then the 2-unit data length L, "
        "then the body. p.23: L counts from the monitoring timer through the end of the "
        "request data. p.27: in a response it counts from the end code. p.18: 'When "
        "sending the message in binary code, the serial No. is stored from the lower "
        "byte to the upper byte' -- and from the upper byte in ASCII, so the two "
        "codings are not byte transformations of one another."
    ),
)
"""The layout every builder and parser in this module cites."""

FX5_PREFIX: Final = Citation(
    manual="JY997D56001",
    revision="K",
    section="pp.19-22",
    note=(
        "The FX5's own copy of the 3E prefix, printed as 50H 00H 00H FFH FFH 03H 00H "
        "0CH 00H 00H 00H -- byte-identical to SH(NA)-080956ENG-M. p.22 prints the "
        "11-byte minimum normal response D0H 00H 00H FFH FFH 03H 00H 02H 00H 00H 00H, "
        "and p.21 the 20-byte abnormal frame for C051H with L = 000BH."
    ),
)
"""The FX5-specific restatement, quoted where a reader may only have the FX5 manual."""

FOUR_E_ON_IQF: Final = Measurement(
    cpu="FX5U-32MT/DS",
    firmware="1.065",
    date="2026-09-06",
    note=(
        "A 4E frame sent to a connection entry configured for 3E was answered normally: "
        "request 54 00 34 12 00 00 ... -> response D4 00 34 12 00 00 ..., serial 0x1234 "
        "echoed little-endian, end code 0x0000. This contradicts JY997D56001-K section "
        "2.1 ('Applicable communication frames: 3E frame, 1E frame') and "
        "JY997D56201-B p.25. The CPU appears to dispatch on the subheader rather than on "
        "the entry's frame parameter. Observed on one unit, one firmware, one afternoon: "
        "aslmp defaults to 3E, permits 4E, and labels this non-contractual (A-4E-IQF)."
    ),
)
"""Why ``FOUR_E`` is not refused on an iQ-F, and why it is not the default either."""

SUBHEADER_TAIL_ZEROED: Final = Measurement(
    cpu="FX5U-32MT/DS",
    firmware="1.065",
    date="2026-09-06",
    note=(
        "A 4E request sent with AA 55 in the two subheader units after the serial came "
        "back with 00 00 there: not echoed, not rejected. SH(NA)-080956ENG-M p.18 calls "
        "them '(Fixed value)'; SH(NA)-080008-AB p.42 labels the same two bytes '(Free)' "
        "in its worked example while its own prose says a 2-byte zero is inserted. The "
        "bench settles it in favour of SH(NA)-080956ENG-M. aslmp always sends 0000H, "
        "never validates them on receive, and builds nothing on them (A-3)."
    ),
)
"""The 4E subheader tail: sent as zero, exposed on parse, never validated."""

ONDEMAND: Final = Citation(
    manual="SH(NA)-080956ENG",
    revision="M",
    section="5.11 p.204",
    note=(
        "Ondemand, command 2101H subcommand 0000H: a message sent from the "
        "SLMP-compatible device to the external device with no request, carrying up to "
        "1920 bytes (960 words). A receive path that treats the next thing that arrives "
        "as its response is desynchronised by one of these."
    ),
)
"""Why a request-shaped subheader arriving on a response path is named, not skipped."""

ONDEMAND_COMMAND: Final = 0x2101
"""The command code of an unsolicited PLC-to-external message. Recognised, never parsed
as somebody's response."""

_SERIAL_BLOCK_BINARY_UNITS: Final = 4
"""The 4E subheader's serial No. (2) plus the fixed ``0000H`` after it (2), in binary
bytes. SH(NA)-080956ENG-M p.18: the 4E subheader "is 6 bytes in total"."""

_ROUTE_UNITS: Final = 5
"""Access-route bytes in binary coding: network, station, module I/O (2), multidrop."""

_LENGTH_FIELD_UNITS: Final = 2
"""Binary bytes in the data-length field itself, which ``L`` does not count."""

_PREFIX_BINARY_UNITS: Final = _ROUTE_UNITS + _LENGTH_FIELD_UNITS
"""The 7 binary bytes that follow every subheader: route + data length."""

_ABNORMAL_MIN_BINARY_UNITS: Final = 11
"""``L = 000BH``: end code (2) + error responding station (5) + command and subcommand
(4). SH(NA)-080956ENG-M p.28 and JY997D56001-K p.21; measured on every one of the 16
abnormal frames the bench provoked."""

_REQUEST_MIN_BINARY_UNITS: Final = 6
"""``L = 0006H``: monitoring timer (2) + command (2) + subcommand (2), with no request
data. The bench's ``0x9999`` unknown-command probe is exactly this frame."""


class FrameType(enum.Enum):
    """Which frame format a connection speaks. A GX Works3 connection-entry fact.

    ``THREE_E``
        The default everywhere in this library. No serial number, therefore no in-band
        correlation, therefore the one-in-flight gate is structural rather than advisory.
    ``FOUR_E``
        3E plus a serial number the responder echoes. The only in-band defence against
        the measured TCP coalescing corruption. Accepted by our FX5U on a connection
        configured for 3E (:data:`FOUR_E_ON_IQF`), which two Mitsubishi manuals say is
        impossible -- so it is permitted, labelled observed, and not made the default.

    The station number extension frame (``6800H``/``E800H``, binary only, CC-Link IE TSN)
    is deliberately absent: it is out of scope for v1, and its abnormal response puts the
    error-responding-station block *after* the command and subcommand rather than before
    them (SH(NA)-080956ENG-M p.25), so it cannot share this parser.
    """

    THREE_E = "3E"
    FOUR_E = "4E"


@dataclass(frozen=True, slots=True)
class Subheader:
    """One subheader in both codings, as literal bytes.

    The two are not transformations of one another. ``5000H`` is ``50 00`` in binary --
    big-endian, uniquely in the whole protocol -- and ``"5000"`` in ASCII, which is four
    characters. Keeping both as literals is what stops anybody writing
    ``struct.pack("<H", 0x5000)`` and shipping ``00 50``.
    """

    binary: bytes
    ascii: bytes

    def for_codec(self, codec: Codec) -> bytes:
        """This subheader in ``codec``'s coding."""
        return self.binary if codec.name == "binary" else self.ascii


@dataclass(frozen=True, slots=True)
class FramePrefix:
    """Everything readable from the fixed part of a frame, before the body.

    The reader needs exactly this and nothing more: once the prefix is in, ``declared``
    says how many further units to read, and the read is length-driven from there.
    """

    frame: FrameFormat
    serial: int | None
    subheader_tail: bytes
    route: Route
    declared: int
    """``L``, as the frame declares it, in wire units."""


@dataclass(frozen=True, slots=True, repr=False)
class FrameFormat:
    """A frame format: its two subheaders, and the arithmetic that follows from them.

    Frozen and slotted. A format is baked into every prebuilt frame at bind time, and a
    mutable one would let a validated plan change frame type underneath a live socket.
    """

    frame_type: FrameType
    request: Subheader
    response: Subheader
    carries_serial: bool
    cites: tuple[Source, ...]

    # -- arithmetic ----------------------------------------------------------------------

    @property
    def subheader_binary_len(self) -> int:
        """2 for 3E, 6 for 4E. The ASCII width is twice this, per codec.

        :class:`Subheader` holds only the literal head (``50 00`` / ``54 00``); on 4E the
        serial No. and the fixed ``0000H`` that follow it are numeric fields the codec
        renders, and they are part of the subheader for every width calculation.
        """
        return len(self.request.binary) + (
            _SERIAL_BLOCK_BINARY_UNITS if self.carries_serial else 0
        )

    def subheader_units(self, codec: Codec) -> int:
        """Subheader width in wire units: 2/4 for 3E, 6/12 for 4E."""
        return self.subheader_binary_len * codec.width

    def prefix_units(self, codec: Codec) -> int:
        """Units before the body: 9/18 for 3E, 13/26 for 4E.

        ``(subheader_binary_len + 7) * codec.width`` -- the 7 being the five access-route
        bytes and the two-byte data-length field, neither of which ``L`` counts.
        """
        return (self.subheader_binary_len + _PREFIX_BINARY_UNITS) * codec.width

    def data_offset(self, codec: Codec) -> int:
        """Offset of the response data / of the monitoring timer: 11/22/15/30.

        The prefix plus the 2-unit field that follows it -- the end code in a response,
        the monitoring timer in a request.
        """
        return self.prefix_units(codec) + codec.number_len(16)

    def _serial_offset(self, codec: Codec) -> int:
        """Where the serial No. sits inside the subheader: 2 binary, 4 ASCII."""
        return codec.number_len(16)

    # -- building ------------------------------------------------------------------------

    def subheader(self, codec: Codec, *, serial: int | None, response: bool) -> bytes:
        """The subheader for one message, serial included on 4E.

        The trailing fixed field is ``codec.number(0, bits=16)``, which is ``00 00`` in
        binary and ``"0000"`` in ASCII -- the one part of the subheader that *is* a
        number rather than a literal, and the only rendering both manuals agree on.
        """
        head = (self.response if response else self.request).for_codec(codec)
        if not self.carries_serial:
            if serial is not None:
                raise SlmpFrameFormatError(
                    f"a {self.frame_type.value} frame has no serial No. field; got "
                    f"serial=0x{serial:04X}. Pass serial=None, or use FOUR_E "
                    f"({FRAME_LAYOUT.reference})."
                )
            return head
        if serial is None:
            raise SlmpFrameFormatError(
                f"a {self.frame_type.value} frame requires a serial No.: it is the only "
                f"in-band correlation SLMP has, and the responder echoes it "
                f"({FRAME_LAYOUT.reference})."
            )
        return head + codec.number(serial, bits=16) + codec.number(0, bits=16)

    def build(
        self,
        *,
        route: Route,
        body: bytes,
        codec: Codec,
        serial: int | None = None,
        expect_body_len: int | None = None,
    ) -> bytes:
        """A complete request frame: subheader, route, ``L``, body.

        ``body`` is ``codec.number(timer, bits=16) + codec.number(command, bits=16) +
        codec.number(subcommand, bits=16) + payload`` -- see :func:`request_body`.

        ``expect_body_len`` is the third of the three ``L`` guards. A command computes
        its ``payload_len`` without building a throwaway buffer, so that the length can
        be checked against the bytes it actually emitted; pass it here and a disagreement
        raises **before** the frame reaches a socket. An overstated ``L`` produces no
        response at all on this hardware, so the cheap check is worth having on the hot
        path (see the module docstring).
        """
        if expect_body_len is not None and expect_body_len != len(body):
            raise SlmpFrameFormatError(
                f"the request body is {len(body)} wire unit(s) but was declared as "
                f"{expect_body_len}. Refusing to send: an understated data length "
                f"returns end code 0xC061 and an OVERSTATED one gets no response at all "
                f"-- the PLC blocks waiting for bytes that never come, which is "
                f"indistinguishable from a dead PLC ({FOUR_E_ON_IQF.cpu} fw "
                f"{FOUR_E_ON_IQF.firmware}, {FOUR_E_ON_IQF.date})."
            )
        return self._assemble(
            self.subheader(codec, serial=serial, response=False), route, body, codec
        )

    def build_response(
        self,
        *,
        route: Route,
        body: bytes,
        codec: Codec,
        serial: int | None = None,
    ) -> bytes:
        """A complete response frame. Used by the conformance simulator and the vectors.

        The client never builds one, but ``encode(decode(bytes)) == bytes`` over the
        golden corpus is what proves the *parser* right rather than merely
        self-consistent, and the simulator has to emit these for the client-against-
        server suite to mean anything.

        ``body`` is ``codec.number(end_code, bits=16)`` followed by the response data, or
        by the error information block for an abnormal response.
        """
        return self._assemble(
            self.subheader(codec, serial=serial, response=True), route, body, codec
        )

    @staticmethod
    def _assemble(head: bytes, route: Route, body: bytes, codec: Codec) -> bytes:
        """Subheader + route + data length + body. The length arithmetic lives here.

        ``codec.number(..., bits=16)`` refuses a value that does not fit its field, so a
        body longer than 65535 wire units raises rather than wrapping to a small ``L``
        that would truncate the request into a valid-looking shorter one.
        """
        # `L` is Mitsubishi's own name for this field (SH(NA)-080956ENG-M p.23) and the
        # AST guard in tests/unit/test_frames.py asserts it is assigned exactly once in
        # this file. Spelling it `n` to satisfy N806 would break the grep this design
        # rests on, so the naming rule is waived here and nowhere else.
        L = len(body)  # noqa: N806 - THE ONLY EXPRESSION IN THE PACKAGE PRODUCING L
        return head + route.encode(codec) + codec.number(L, bits=16) + body

    # -- parsing -------------------------------------------------------------------------

    def read_prefix(self, buf: bytes, codec: Codec, *, response: bool) -> FramePrefix:
        """Decode the fixed part of a frame. Raises rather than guessing, always.

        ``buf`` must hold at least :meth:`prefix_units` units; anything beyond them is
        ignored, so the reader can call this the moment the prefix has arrived and learn
        how much more to read.
        """
        prefix = self.prefix_units(codec)
        if len(buf) < prefix:
            raise SlmpShortFrameError(
                f"a {self.frame_type.value} {codec.name} frame prefix is {prefix} wire "
                f"unit(s); got {len(buf)}. Read the fixed prefix, take L from it, then "
                f"read exactly L more ({FRAME_LAYOUT.reference})."
            )
        self._check_subheader(buf, codec, response=response)
        try:
            serial: int | None = None
            tail = b""
            if self.carries_serial:
                at = self._serial_offset(codec)
                serial = codec.read_number(buf, at, bits=16)
                tail = bytes(buf[at + codec.number_len(16) : self.subheader_units(codec)])
            route = Route.decode(buf, self.subheader_units(codec), codec)
            declared = codec.read_number(
                buf, self.subheader_units(codec) + Route.wire_len(codec), bits=16
            )
        except SlmpCodecError as codec_error:
            raise _as_frame_format_error(
                codec_error, self, codec, where="the frame prefix"
            ) from codec_error
        self._check_declared(declared, codec, response=response)
        return FramePrefix(
            frame=self,
            serial=serial,
            subheader_tail=tail,
            route=route,
            declared=declared,
        )

    def _check_declared(self, declared: int, codec: Codec, *, response: bool) -> None:
        """``L`` must at least cover the fields the format guarantees are there."""
        if response:
            minimum = codec.number_len(16)
            covers = "the end code"
        else:
            minimum = _REQUEST_MIN_BINARY_UNITS * codec.width
            covers = "the monitoring timer, the command and the subcommand"
        if declared >= minimum:
            return
        raise SlmpFrameFormatError(
            f"declared data length L = 0x{declared:04X} ({declared} wire unit(s)) is "
            f"impossible for a {self.frame_type.value} {codec.name} "
            f"{'response' if response else 'request'}: the minimum is {minimum}, which "
            f"is {covers} alone. A request whose L was 0x0000 was answered 0xC061 with "
            f"the command echoed as 0x0000 on FX5U-32MT/DS fw 1.065, 2026-09-06 -- the "
            f"CPU read the command out of the monitoring timer field "
            f"({FRAME_LAYOUT.reference})."
        )

    def _check_subheader(self, buf: bytes, codec: Codec, *, response: bool) -> None:
        """Refuse anything that is not this format's subheader for this direction."""
        expected = (self.response if response else self.request).for_codec(codec)
        got = bytes(buf[: len(expected)])
        if got == expected:
            return
        opposite = (self.request if response else self.response).for_codec(codec)
        if response and got == opposite:
            raise SlmpUnsolicitedFrameError(
                f"a request-shaped subheader {got.hex(' ').upper()} arrived where a "
                f"{self.frame_type.value} response ({expected.hex(' ').upper()}) was "
                f"expected. The documented sender-initiated message is command "
                f"0x{ONDEMAND_COMMAND:04X} Ondemand ({ONDEMAND.reference}). It is never "
                f"parsed as somebody's response and the stream is not resynchronised: "
                f"skipping forward to the next thing that looks like a subheader is how "
                f"the previous transaction's data becomes this transaction's answer."
            )
        raise SlmpFrameFormatError(
            f"subheader {got.hex(' ').upper()} is not the {self.frame_type.value} "
            f"{codec.name} {'response' if response else 'request'} subheader "
            f"{expected.hex(' ').upper()}. Subheaders are literal byte sequences: 5000H "
            f"goes on the wire as 50 00, which is the one binary field that is not "
            f"little-endian ({FRAME_LAYOUT.reference}). Check the connection entry's "
            f"frame type and communication data code."
        )

    def parse(
        self, data: bytes, codec: Codec, *, expect_serial: int | None = None
    ) -> RawResponse:
        """A complete response frame in, a :class:`RawResponse` out, or a raised error.

        ``data`` must be exactly ``prefix_units + L`` units: fewer raises
        :class:`~aslmp.wire.raw.SlmpShortFrameError`, more raises
        :class:`~aslmp.wire.raw.SlmpTrailingDataError`. Neither is repaired.
        :class:`aslmp.wire.reader.ResponseAccumulator` is how a TCP caller arrives here
        with exactly the right bytes.

        ``expect_serial`` is compared byte-for-byte on 4E. A mismatch is the only in-band
        evidence of the measured coalescing corruption and raises
        :class:`~aslmp.wire.raw.SlmpSerialMismatchError`.
        """
        prefix = self._parse_span(data, codec, response=True)
        if expect_serial is not None:
            self._check_serial_echo(prefix, expect_serial)
        try:
            end_code = codec.read_number(data, self.prefix_units(codec), bits=16)
        except SlmpCodecError as codec_error:
            raise _as_frame_format_error(
                codec_error, self, codec, where="the end code"
            ) from codec_error
        body = data[self.data_offset(codec) :]
        if end_code == 0:
            return RawResponse(
                frame=self,
                serial=prefix.serial,
                route=prefix.route,
                length=prefix.declared,
                end_code=end_code,
                payload=bytes(body),
                error_info=None,
                extra_error_data=b"",
                raw=bytes(data),
                subheader_tail=prefix.subheader_tail,
            )
        return self._parse_abnormal(data, codec, prefix, end_code)

    def _parse_abnormal(
        self, data: bytes, codec: Codec, prefix: FramePrefix, end_code: int
    ) -> RawResponse:
        """The abnormal branch: validate the error block before believing any of it."""
        minimum = _ABNORMAL_MIN_BINARY_UNITS * codec.width
        if prefix.declared < minimum:
            raise SlmpErrorInfoError(
                f"end code 0x{end_code:04X} is abnormal, so L must be at least "
                f"0x{minimum:04X} ({minimum} wire unit(s)): the end code, the error "
                f"responding station and the echoed command and subcommand. This frame "
                f"declared L = 0x{prefix.declared:04X}. Refusing to report an end code "
                f"whose error information block is not there -- a block read out of "
                f"whatever bytes followed names a station nobody addressed and looks "
                f"exactly like a true one ({ABNORMAL_RESPONSE.reference})."
            )
        try:
            error_info = ErrorInfo.decode(data, self.data_offset(codec), codec)
        except SlmpCodecError as codec_error:
            raise _as_frame_format_error(
                codec_error, self, codec, where="the error information block"
            ) from codec_error
        after = self.data_offset(codec) + ErrorInfo.wire_len(codec)
        return RawResponse(
            frame=self,
            serial=prefix.serial,
            route=prefix.route,
            length=prefix.declared,
            end_code=end_code,
            payload=b"",
            error_info=error_info,
            extra_error_data=bytes(data[after:]),
            raw=bytes(data),
            subheader_tail=prefix.subheader_tail,
        )

    def parse_request(self, data: bytes, codec: Codec) -> RawRequest:
        """A complete request frame in, a :class:`RawRequest` out, or a raised error.

        The client builds requests and never parses them. This exists for the
        conformance simulator -- which must decode exactly what a PLC decodes, including
        the malformed frames the pathology switches reproduce -- and for the golden
        corpus, where ``parse(build(x)) == x`` is what stops the two directions drifting
        together into a shared mistake.
        """
        prefix = self._parse_span(data, codec, response=False)
        offset = self.prefix_units(codec)
        step = codec.number_len(16)
        try:
            timer = codec.read_number(data, offset, bits=16)
            command = codec.read_number(data, offset + step, bits=16)
            subcommand = codec.read_number(data, offset + 2 * step, bits=16)
        except SlmpCodecError as codec_error:
            raise _as_frame_format_error(
                codec_error, self, codec, where="the monitoring timer, command or subcommand"
            ) from codec_error
        return RawRequest(
            frame=self,
            serial=prefix.serial,
            route=prefix.route,
            length=prefix.declared,
            monitoring_timer=timer,
            command=command,
            subcommand=subcommand,
            payload=bytes(data[offset + 3 * step :]),
            raw=bytes(data),
            subheader_tail=prefix.subheader_tail,
        )

    def _parse_span(self, data: bytes, codec: Codec, *, response: bool) -> FramePrefix:
        """Read the prefix and assert the buffer is exactly ``prefix + L`` units."""
        prefix = self.read_prefix(data, codec, response=response)
        total = self.prefix_units(codec) + prefix.declared
        if len(data) < total:
            raise SlmpShortFrameError(
                f"the frame declares L = 0x{prefix.declared:04X}, so it is {total} wire "
                f"unit(s) long, but only {len(data)} arrived. Take action to receive the "
                f"remaining data (SH(NA)-080956ENG-M chapter 6 p.205 lists exactly this "
                f"as a troubleshooting item); this library's ResponseAccumulator drives "
                f"the read off L so a short frame is never parsed."
            )
        if len(data) > total:
            raise SlmpTrailingDataError(
                f"the frame declares L = 0x{prefix.declared:04X}, so it is {total} wire "
                f"unit(s) long, but {len(data)} arrived. The surplus {len(data) - total} "
                f"unit(s) are somebody's message and this library will not guess whose. "
                f"On TCP this is the measured request-coalescing corruption; on UDP a "
                f"datagram must be exactly one SLMP message."
            )
        return prefix

    def _check_serial_echo(self, prefix: FramePrefix, expect_serial: int) -> None:
        """The 4E serial echo, compared byte for byte."""
        if not self.carries_serial:
            raise SlmpFrameFormatError(
                f"expect_serial=0x{expect_serial:04X} was supplied for a "
                f"{self.frame_type.value} frame, which has no serial No. field. 3E "
                f"offers no in-band correlation at all: that is a property of the frame "
                f"format, not something a caller can ask for."
            )
        if prefix.serial != expect_serial:
            echoed = "none" if prefix.serial is None else f"0x{prefix.serial:04X}"
            raise SlmpSerialMismatchError(
                f"the response echoed serial No. {echoed}, but this "
                f"transaction sent 0x{expect_serial:04X}. This is the only in-band "
                f"evidence of the measured coalescing corruption: two requests written "
                f"before the first response is read produce ONE response, for the LAST "
                f"request, with end code 0x0000 ({FOUR_E_ON_IQF.cpu} fw "
                f"{FOUR_E_ON_IQF.firmware}, {FOUR_E_ON_IQF.date}). The value in this "
                f"frame is not this transaction's answer."
            )

    def __str__(self) -> str:
        return self.frame_type.value

    def __repr__(self) -> str:
        """``THREE_E`` / ``FOUR_E``.

        The generated dataclass repr would print both subheaders and every citation --
        several hundred characters -- inside the repr of every ``RawResponse``, which is
        exactly the object a person prints when a frame has gone wrong.
        """
        return "THREE_E" if self.frame_type is FrameType.THREE_E else "FOUR_E"


def _as_frame_format_error(
    exc: SlmpCodecError,
    frame: FrameFormat,
    codec: Codec,
    *,
    where: str,
) -> SlmpFrameFormatError:
    """A codec refusal inside a frame field is a malformed frame, and must say so.

    ``ASCII.read_number`` refuses a character outside ``[0-9A-F]`` rather than reading
    it as zero (DESIGN section 4.1 invariant 6; ``libslmp2``'s ``wordcodec.c:27``
    returns ``0`` for a bad nibble, so ``"ZZZZ"`` decodes to a plausible ``0``). It
    raises its own :class:`~aslmp.wire.codec.SlmpCodecError`, because a codec has no
    frame, no offset-in-frame and no transaction to name.

    A caller of :meth:`FrameFormat.parse` does have all three, and DESIGN section 3.1
    files "non-hex in ASCII" under ``SlmpFrameFormatError``. Translating here is what
    makes ``except SlmpFrameError`` -- and therefore
    :func:`aslmp.errors.routing.protocol_error_for`, whose map is keyed on that family
    -- catch a corrupt response instead of letting a bare ``ValueError`` escape the
    whole ``SlmpError`` tree. The codec error stays as ``__cause__`` with its own
    offset and character.
    """
    return SlmpFrameFormatError(
        f"{where} of this {frame.frame_type.value} {codec.name} frame does not decode: "
        f"{exc}"
    )


def request_body(
    codec: Codec,
    *,
    monitoring_timer: int,
    command: int,
    subcommand: int,
    payload: bytes = b"",
) -> bytes:
    """The three fixed request fields plus the command payload, in wire order.

    This is the ``body`` that :meth:`FrameFormat.build` measures to produce ``L``.
    Keeping it a function rather than three concatenations at each call site is what
    makes ``len(body)`` a meaningful single expression: there is one place where the
    monitoring timer, the command and the subcommand are put in front of a payload.

    ``monitoring_timer`` is in 250 ms units, and ``0x0000`` means *wait indefinitely* --
    not "no timeout" and not "zero milliseconds" (SH(NA)-080956ENG-M p.24;
    SH(NA)-080008-AB p.43). JY997D56001-K p.27 requires ``0000H`` for the FX5 CPU
    module: a non-zero timer is "supported only for Ethernet modules". Our bench swept
    0x0000, 0x0010, 0x0028 and 0x00F0 and every one was answered normally in ~7 ms, so
    the field is inert there rather than refused (FX5U-32MT/DS fw 1.065, 2026-09-06).
    """
    return (
        codec.number(monitoring_timer, bits=16)
        + codec.number(command, bits=16)
        + codec.number(subcommand, bits=16)
        + payload
    )


def response_body(
    codec: Codec,
    *,
    end_code: int,
    payload: bytes = b"",
    error_info: ErrorInfo | None = None,
    extra_error_data: bytes = b"",
) -> bytes:
    """The end code plus whatever follows it, in wire order.

    The counterpart of :func:`request_body`, and the ``body`` that
    :meth:`FrameFormat.build_response` measures to produce ``L``. Only the conformance
    simulator and the golden corpus call it; the client parses responses and never
    builds one.

    A normal response is the end code and the response data. An abnormal one is the end
    code, the error responding station, the echoed command and subcommand, and then the
    command-defined error data if that command defines any -- SH(NA)-080956ENG-M p.28,
    "response data when failed (when defined by command)", which none of the 16 abnormal
    frames captured on FX5U-32MT/DS fw 1.065 carried. The two shapes are mutually
    exclusive and this refuses to mix them.
    """
    if end_code == 0:
        if error_info is not None or extra_error_data:
            raise SlmpFrameFormatError(
                "end code 0x0000 is a normal response: it carries response data, not an "
                f"error information block ({ABNORMAL_RESPONSE.reference})."
            )
        return codec.number(end_code, bits=16) + payload
    if error_info is None:
        raise SlmpErrorInfoError(
            f"end code 0x{end_code:04X} is abnormal, so the frame must carry an error "
            f"information block naming the responding station and echoing the command "
            f"({ABNORMAL_RESPONSE.reference})."
        )
    if payload:
        raise SlmpFrameFormatError(
            f"end code 0x{end_code:04X} is abnormal, so there is no response data; a "
            f"command-defined trailer goes in extra_error_data "
            f"({ABNORMAL_RESPONSE.reference})."
        )
    return (
        codec.number(end_code, bits=16) + error_info.encode(codec) + extra_error_data
    )


THREE_E: Final = FrameFormat(
    frame_type=FrameType.THREE_E,
    request=Subheader(binary=b"\x50\x00", ascii=b"5000"),
    response=Subheader(binary=b"\xd0\x00", ascii=b"D000"),
    carries_serial=False,
    cites=(FRAME_LAYOUT, FX5_PREFIX),
)
"""The default frame format. ``50 00`` out, ``D0 00`` back; no serial number."""

FOUR_E: Final = FrameFormat(
    frame_type=FrameType.FOUR_E,
    request=Subheader(binary=b"\x54\x00", ascii=b"5400"),
    response=Subheader(binary=b"\xd4\x00", ascii=b"D400"),
    carries_serial=True,
    cites=(FRAME_LAYOUT, FOUR_E_ON_IQF, SUBHEADER_TAIL_ZEROED),
)
"""3E plus an echoed serial number. Accepted by our FX5U; not the default."""

FRAMES: Final[dict[FrameType, FrameFormat]] = {
    FrameType.THREE_E: THREE_E,
    FrameType.FOUR_E: FOUR_E,
}
"""Every frame format this library speaks, by its public enum member."""
