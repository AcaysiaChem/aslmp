"""``0101`` Read Type Name, ``0619`` Self Test and ``1617`` Clear Error.

Layer 2. The three commands that touch no device memory, and the two that the rest of
this library leans on hardest.

**``0619`` Self Test is the health-check and connect primitive.** It reads no device and
does not depend on CPU state, and it costs the same as a two-word batch read: 7.34 ms at
p50 against 6.92 ms, n=300 each (FX5U-32MT/DS fw 1.065, 2026-09-06, from the laptop at
192.168.10.41 over Wi-Fi at ~7 ms median RTT; ``docs/hardware.md`` section 3). So
essentially the whole ~7 ms is transport plus the SLMP module's service processing, and
a loopback is a
truthful liveness probe with zero side effects. One round trip of it proves, at once,
that the connection entry is free, that the coding is right, that the frame type is
accepted, that the route bytes are right and that the PLC is answering *now* -- which is
why ``connect()`` sends one and compares the echo byte for byte, and why ``socket
.connect()`` succeeding proves none of those things.

The exact request and echo from the bench::

    request : 50 00 00 FF FF 03 00 0C 00 10 00 19 06 00 00 04 00 41 42 43 44
    response: ... 04 00 41 42 43 44

**Both manuals restrict the loopback data to ``'0'``-``'9'`` and ``'A'``-``'F'``.**
Whether a CPU enforces it is unknown -- ambiguity ``A-LOOPBACK-CHARSET`` -- so this
package enforces it client-side, against its own defaults included:
``Codec.loopback_payload_ok`` gates every payload, and the default ``b"ABCD"`` is the
exact byte sequence ``41 42 43 44`` proven on that bench rather than a plausible-looking
guess. Refusing in the safe direction costs nothing; sending a payload that violates a
documented constraint and discovering the constraint is real costs a bring-up afternoon.

**``0101`` Read Type Name is how ``connect()`` learns which silicon it is talking to.**
The model name is 16 characters of ASCII **even in binary coding**, space padded, and
the model code follows as a 16-bit field. Measured on the bench: ``"FX5U-32MT/DS    "``
and ``0x4A49``, matching JY997D56001-K p.110 exactly. The code is what
``aslmp.identity.resolve_profile`` turns into a profile, and an unrecognised one is an
error rather than a radix guess.

**``1617`` Clear Error's subcommand is contested inside one manual.** JY997D56001-K's
section 4.1 command list gives ``0001``; its own section 4.4 detail page prints
``17H 16H 00H 00H``, and SH(NA)-080956ENG-M agrees with the detail page. This package
sends ``0000`` and validates it (ambiguity ``A-1617-SUBCOMMAND``); if a CPU answers
``0xC059`` the other reading is worth trying by hand, and nothing here tries it
automatically.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from aslmp.commands.base import (
    Command,
    EncodeContext,
    expect_empty_payload,
    expect_payload_len,
)
from aslmp.errors import SlmpConfigurationError, SlmpPayloadShapeError
from aslmp.profile import Capability
from aslmp.wire.citations import Ambiguity, Citation, Measurement, Source
from aslmp.wire.codec import LOOPBACK_MAX_BYTES, LOOPBACK_MIN_BYTES

__all__ = [
    "CLEAR_ERROR",
    "CLEAR_ERROR_SUBCOMMAND_AMBIGUITY",
    "DEFAULT_LOOPBACK",
    "LOOPBACK_CHARSET_AMBIGUITY",
    "MODEL_NAME_UNITS",
    "READ_TYPE_NAME",
    "SELF_TEST",
    "SELF_TEST_ON_FX5U",
    "ClearError",
    "ReadTypeName",
    "SelfTest",
    "TypeName",
]


READ_TYPE_NAME: Citation = Citation(
    manual="SH(NA)-080956ENG",
    revision="M",
    section="5.7 pp.137-140",
    note=(
        "Read Type Name 0101H, subcommand 0000H, no request data. Response: the CPU "
        "model name as 16 ASCII characters, space padded -- ASCII characters even when "
        "the communication data code is binary -- followed by the 16-bit model code. "
        "The model-code table on pp.138-139 lists the iQ-R, Q and L codes; JY997D56001-K "
        "p.110 lists the FX5 ones."
    ),
)
"""The layout, and the fact that the name is text in both codings."""

TYPE_NAME_ON_FX5U: Measurement = Measurement(
    cpu="FX5U-32MT/DS",
    firmware="1.065",
    date="2026-09-06",
    note=(
        "0101 returned 'FX5U-32MT/DS    ' (16 characters, space padded) and model code "
        "0x4A49, matching JY997D56001-K p.110's FX5U-32MT/DS = 4A49H exactly. The name "
        "came back as ASCII bytes on a binary connection, as documented."
    ),
)
"""The one model code this package has actually seen on a wire."""

SELF_TEST: Citation = Citation(
    manual="SH(NA)-080956ENG",
    revision="M",
    section="5.4 pp.126-127",
    note=(
        "Self Test 0619H, subcommand 0000H. Request: the number of loopback bytes (2 "
        "bytes low byte first, 1 to 960) then the loopback data. Response: an exact "
        "echo of both. The data is restricted to '0'-'9' and 'A'-'F'. It works only "
        "against the Ethernet-equipped module the client is directly connected to, not "
        "to another station across a network."
    ),
)
"""The loopback layout and its documented character restriction."""

SELF_TEST_ON_FX5U: Measurement = Measurement(
    cpu="FX5U-32MT/DS",
    firmware="1.065",
    date="2026-09-06",
    host="192.168.10.41 (laptop)",
    medium="Wi-Fi, ~7 ms median RTT",
    samples=300,
    note=(
        "0619 with 4 bytes of loopback data 41 42 43 44 ('ABCD') echoed 04 00 41 42 43 "
        "44 in 7.34 ms at p50, against 6.92 ms for an 0401 two-word read over the same "
        "n=300 on that host and link (docs/hardware.md section 3). A loopback touches no "
        "device and does not involve device memory, so "
        "essentially the whole round trip is transport plus SLMP service processing. "
        "This is the zero-side-effect liveness and latency probe."
    ),
)
"""Why ``connect()``, ``ping()`` and the health monitor all use this command."""

CLEAR_ERROR: Citation = Citation(
    manual="SH(NA)-080956ENG",
    revision="M",
    section="5.9 p.146",
    note=(
        "Clear Error 1617H, subcommand 0000H, no request data and no response data. It "
        "initialises the own-station error code and turns off the error LED, on the "
        "connected station only."
    ),
)
"""The layout, and the reading the FX5 detail page agrees with."""

LOOPBACK_CHARSET_AMBIGUITY: Ambiguity = Ambiguity(
    key="A-LOOPBACK-CHARSET",
    question=(
        "Does 0619 Self Test really reject loopback data outside '0'-'9' and 'A'-'F'?"
    ),
    readings=(
        "Enforced -- both manuals restrict loopback data to those characters",
        "Not enforced -- the CPU echoes whatever it is given",
    ),
    chosen=(
        "Codec.loopback_payload_ok refuses non-hex payloads client-side, including this "
        "package's own defaults. self_test()'s default is b'ABCD', the exact bytes "
        "proven on the bench."
    ),
    reason=(
        "b'ABCD' (41 42 43 44) is a measured value rather than a guess, and shipping a "
        "default that violates a documented constraint would be indefensible whichever "
        "way the constraint turns out."
    ),
    probe="Send 0619 with b'ZZZZ' and record whether it echoes, errors, or is silent.",
)
"""The open question behind the client-side character check."""

CLEAR_ERROR_SUBCOMMAND_AMBIGUITY: Ambiguity = Ambiguity(
    key="A-1617-SUBCOMMAND",
    question="Is the subcommand for 1617 Clear Error 0000 or 0001?",
    readings=(
        "0000 -- the FX5 detail section, and SH(NA)-080956ENG-M",
        "0001 -- the FX5 command-list table in the same manual",
    ),
    chosen="0000, validated client-side; anything else raises.",
    reason=(
        "The FX5 manual's command-list table and its own detail section disagree. 0000 "
        "matches every other non-device command in the catalogue."
    ),
    probe=(
        "Send 1617 with subcommand 0000 against a CPU with a clearable error and record "
        "the end code; repeat with 0001. Untested by us."
    ),
)
"""The open question behind the fixed ``0000``."""

MODEL_NAME_UNITS: int = 16
"""Characters in the ``0101`` model-name field, in **both** codings.

The name is text, not a number, so ASCII coding does not double it: the model code that
follows is a numeric field and does (SH(NA)-080956ENG-M p.137).
"""

DEFAULT_LOOPBACK: bytes = b"ABCD"
"""The default ``0619`` payload: ``41 42 43 44``, the exact bytes proven on the bench."""


@dataclass(frozen=True, slots=True)
class TypeName:
    """What ``0101`` returned: the model name as printed, and its numeric code.

    ``raw`` is the undecoded 16-character field, kept because the space padding is the
    only evidence that the field was read at the right offset and because a name this
    package cannot decode is worth showing to a person verbatim.
    """

    model: str
    model_code: int
    raw: bytes

    def __str__(self) -> str:
        return f"{self.model} (model code 0x{self.model_code:04X})"


@dataclass(frozen=True, slots=True)
class ReadTypeName(Command[TypeName]):
    """``0101``: which CPU is on the other end. No request data, zero side effects.

    Golden vector, measured on FX5U-32MT/DS fw 1.065 (2026-09-06) and matching
    JY997D56001-K p.111's worked example in shape::

        request data : (none)
        response data: 46 58 35 55 2D 33 32 4D 54 2F 44 53 20 20 20 20  49 4A
                       'FX5U-32MT/DS    '                               0x4A49
    """

    CODE = 0x0101
    NAME = "Read Type Name"
    mutates = False
    CITES: ClassVar[tuple[Source, ...]] = (READ_TYPE_NAME, TYPE_NAME_ON_FX5U)

    def subcommand(self, ctx: EncodeContext) -> int:
        del ctx
        return 0x0000

    def validate(self, ctx: EncodeContext) -> None:
        ctx.profile.require(Capability.READ_TYPE_NAME, what=self.describe())

    def payload_len(self, ctx: EncodeContext) -> int:
        del ctx
        return 0

    def encode(self, ctx: EncodeContext) -> bytes:
        del ctx
        return b""

    def decode(self, payload: bytes, ctx: EncodeContext) -> TypeName:
        expected = MODEL_NAME_UNITS + ctx.codec.number_len(16)
        expect_payload_len(payload, expected, what=self.describe())
        raw = bytes(payload[:MODEL_NAME_UNITS])
        try:
            model = raw.decode("ascii")
        except UnicodeDecodeError as exc:
            raise SlmpPayloadShapeError(
                f"the 0101 model name field is 16 ASCII characters in both codings "
                f"({READ_TYPE_NAME.reference}), but this response carried "
                f"{raw.hex(' ').upper()}, which is not ASCII. Refusing to guess an "
                f"encoding: the model name is what chooses the profile, and the profile "
                f"is what decides whether Y20 is output 16 or output 32."
            ) from exc
        code = ctx.codec.read_number(payload, MODEL_NAME_UNITS, bits=16)
        return TypeName(model=model.rstrip(), model_code=code, raw=raw)

    def describe(self) -> str:
        return "read_type_name()"


@dataclass(frozen=True, slots=True)
class SelfTest(Command[bytes]):
    """``0619``: ask the module to echo a payload back. The liveness probe.

    The response echoes the count and the data, and this decoder checks both: the count
    field must agree with the number of bytes that followed it, and the echo must be
    exactly what was sent. An echo that differs is not a warning -- it means the response
    on this socket is not the answer to this request, which on 3E is the only way the
    measured TCP request-coalescing corruption can ever be seen.
    """

    payload: bytes = DEFAULT_LOOPBACK

    CODE = 0x0619
    NAME = "Self Test"
    mutates = False
    CITES: ClassVar[tuple[Source, ...]] = (SELF_TEST, SELF_TEST_ON_FX5U)
    AMBIGUITIES = (LOOPBACK_CHARSET_AMBIGUITY,)

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", bytes(self.payload))

    def subcommand(self, ctx: EncodeContext) -> int:
        del ctx
        return 0x0000

    def validate(self, ctx: EncodeContext) -> None:
        ctx.profile.require(Capability.SELF_TEST, what=self.describe())
        if ctx.codec.loopback_payload_ok(self.payload):
            return
        raise SlmpConfigurationError(
            f"{self.describe()}: a 0619 loopback payload is {LOOPBACK_MIN_BYTES} to "
            f"{LOOPBACK_MAX_BYTES} bytes of '0'-'9' and 'A'-'F' "
            f"({SELF_TEST.reference}); got {len(self.payload)} byte(s): "
            f"{self.payload!r}. Whether a CPU enforces the character set is untested "
            f"(ambiguity {LOOPBACK_CHARSET_AMBIGUITY.key}), so this package refuses in "
            f"the safe direction, against its own defaults included."
        )

    def payload_len(self, ctx: EncodeContext) -> int:
        return ctx.codec.number_len(16) + len(self.payload)

    def encode(self, ctx: EncodeContext) -> bytes:
        return ctx.codec.number(len(self.payload), bits=16) + self.payload

    def decode(self, payload: bytes, ctx: EncodeContext) -> bytes:
        expect_payload_len(payload, self.payload_len(ctx), what=self.describe())
        count = ctx.codec.read_number(payload, 0, bits=16)
        echo = bytes(payload[ctx.codec.number_len(16) :])
        if count != len(echo):
            raise SlmpPayloadShapeError(
                f"{self.describe()}: the response declared {count} loopback byte(s) and "
                f"carried {len(echo)}. A 0619 response is an exact echo of the count and "
                f"the data ({SELF_TEST.reference})."
            )
        if echo != self.payload:
            raise SlmpPayloadShapeError(
                f"{self.describe()}: sent {self.payload!r} and got {echo!r} back. A "
                f"0619 response is an exact echo, so this is not the answer to this "
                f"request. On FX5U-32MT/DS fw 1.065 two requests written before the "
                f"first response is read produce ONE response, for the LAST request, "
                f"with end code 0x0000 (measured 2026-09-06); on a 3E frame a differing "
                f"echo is the only way to see it."
            )
        return echo

    def describe(self) -> str:
        return f"self_test({self.payload!r})"


@dataclass(frozen=True, slots=True)
class ClearError(Command[None]):
    """``1617``: clear the own-station error code and the error LED. Connected station only.

    Binary request data is nothing at all: ``17 16 00 00`` is the command and the
    subcommand. ``mutates`` is True -- it changes module state, and a mid-flight failure
    leaves it genuinely unknown whether the error history was cleared.
    """

    CODE = 0x1617
    NAME = "Clear Error"
    mutates = True
    CITES: ClassVar[tuple[Source, ...]] = (CLEAR_ERROR,)
    AMBIGUITIES = (CLEAR_ERROR_SUBCOMMAND_AMBIGUITY,)

    def subcommand(self, ctx: EncodeContext) -> int:
        del ctx
        return 0x0000

    def validate(self, ctx: EncodeContext) -> None:
        ctx.profile.require(Capability.CLEAR_ERROR, what=self.describe())

    def payload_len(self, ctx: EncodeContext) -> int:
        del ctx
        return 0

    def encode(self, ctx: EncodeContext) -> bytes:
        del ctx
        return b""

    def decode(self, payload: bytes, ctx: EncodeContext) -> None:
        del ctx
        expect_empty_payload(payload, what=self.describe())

    def describe(self) -> str:
        return "clear_error()"
