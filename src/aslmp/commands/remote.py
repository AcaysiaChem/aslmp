"""Remote control: ``1001`` Run, ``1002`` Stop, ``1003`` Pause, ``1005`` Latch Clear,
``1006`` Reset.

Layer 2. These are the five commands in this package that can stop a running machine
over an unauthenticated cleartext socket, and **none of them has ever been sent to our
bench.** The hardware baseline was collected with the explicit constraint that no
CPU-state command was ever issued (FX5U-32MT/DS fw 1.065, 2026-09-06), so every byte
below is manual-derived, the two contested fields are registered ambiguities, and the
first user to try remote control is the experiment.

**Remote RUN reports success when it did nothing.** SH(NA)-080956ENG-M p.131 and
JY997D56001-K p.104, verbatim: *"Remote RUN can be executed when the switch of the
access destination module is in the RUN state. Even if the switch is in the STOP state,
Remote Run (command: 1001) will be completed normally. However, the access destination
does not become the RUN state."* An end code of ``0x0000`` from ``1001`` is therefore
**not** evidence that the CPU is running. This package's answer is graft G15: ``verify``
defaults to ``True`` on run, stop and pause, and the client reads SD203 back and raises
:class:`~aslmp.errors.SlmpRemoteStateNotReachedError` if the requested state was not
reached. The second round trip is the correct price; returning the successful end code
as success would be precisely the silent lie the house rules forbid.

**Remote RESET's expected outcome is no response at all.** SH(NA)-080956ENG-M p.136:
*"If the access destination reset is succeeded, the response request is not be sent back
to the external device."* JY997D56001-K p.108 says the same, and over TCP the connection
goes with it. :attr:`~aslmp.commands.base.Command.response_optional` is ``True`` on
:class:`RemoteReset` alone, and it is what tells the client to surface a
``ResetOutcome`` rather than a timeout. ``pymcprotocol`` handles this by setting a
one-second socket timeout, swallowing the exception and reconnecting; silence here is a
documented outcome and it is modelled as one.

**The ``1002`` / ``1005`` / ``1006`` fixed field is a genuine conflict between two
Mitsubishi documents.** SH(NA)-080956ENG-M pp.133/135/136 print ``01 00``;
JY997D56001-K pp.106/107/108 print ``00 00``. Both readings were confirmed from rendered
page images rather than a text layer, so this is not an extraction artefact. Ambiguity
``A-REMOTE-FIXED`` records it, the family-specific manual wins for its own family
(``profile.remote_fixed_field`` is ``b"\\x00\\x00"`` on iQ-F and ``b"\\x01\\x00"`` on
the SLMP-reference families), and ``fixed_field=`` overrides it per call for whoever
runs the experiment. Nothing here retries in the other value.

**Clear mode is refused rather than guessed on an iQ-F.** JY997D56001-K p.105's
clear-mode table has exactly one row -- "Do not clear the device", ``00H`` -- while the
communication example on the *same page* sets clear mode to "clear all devices including
that in the latch range" and prints ``02H``. The manual contradicts itself, the
consequence of guessing wrong is clearing a running machine's latch range, and so
``profile.allowed_clear_modes`` is ``{NONE}`` on iQ-F and anything else raises before
the request is built (ambiguity ``A-CLEAR-MODE``).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import ClassVar

from aslmp.commands.base import (
    FIXED_FIELD_UNITS,
    Command,
    EncodeContext,
    expect_empty_payload,
)
from aslmp.errors import SlmpConfigurationError
from aslmp.profile import Capability, ClearMode
from aslmp.wire.citations import Ambiguity, Citation, Source

__all__ = [
    "CLEAR_MODE_AMBIGUITY",
    "FIXED_FIELD_AMBIGUITY",
    "FX5_REMOTE",
    "REMOTE_LAYOUT",
    "RUN_LIES_IN_STOP",
    "RemoteLatchClear",
    "RemotePause",
    "RemoteReset",
    "RemoteRun",
    "RemoteStop",
    "RunMode",
]


REMOTE_LAYOUT: Citation = Citation(
    manual="SH(NA)-080956ENG",
    revision="M",
    section="5.6 pp.130-140",
    note=(
        "Remote Run 1001H: mode (2 bytes), clear mode (1 byte), then one fixed 00H "
        "padding byte -- 8 bytes of request data in total including the command and "
        "subcommand, and no response data. Remote Pause 1003H: mode (2 bytes). Remote "
        "Stop 1002H, Remote Latch Clear 1005H and Remote Reset 1006H: a 2-byte fixed "
        "field, printed as 01 00. Mode 0001H is 'forced execution not allowed' and "
        "0003H is 'allowed'. Only one station can be operated remotely per command, and "
        "resetting or power-cycling the module deletes all remote-operation state."
    ),
)
"""The generic layout of all five commands."""

FX5_REMOTE: Citation = Citation(
    manual="JY997D56001",
    revision="K",
    section="4.3 pp.104-111",
    note=(
        "The FX5's copy, which prints 00 00 in the 1002 / 1005 / 1006 fixed field where "
        "SH(NA)-080956ENG-M prints 01 00, and whose clear-mode table for 1001 has a "
        "single row (00H, 'Do not clear the device') while the communication example on "
        "the same page prints 02H. Remote Reset additionally requires the CPU parameter "
        "'Remote Reset Setting' to be Enabled; the GX Works3 default is Disable, which "
        "is the most likely reason a 1006 fails on a fresh FX5U."
    ),
)
"""The FX5 restatement, and both contested fields."""

RUN_LIES_IN_STOP: Citation = Citation(
    manual="SH(NA)-080956ENG",
    revision="M",
    section="5.6 p.131",
    note=(
        "'Remote RUN can be executed when the switch of the access destination module "
        "is in the RUN state. Even if the switch is in the STOP state, Remote Run "
        "(command: 1001) will be completed normally. However, the access destination "
        "does not become the RUN state.' The same paragraph appears for Remote PAUSE. "
        "End code 0x0000 is not evidence of the requested state, which is why verify "
        "defaults to True."
    ),
)
"""The documented silent no-op that graft G15 exists to catch."""

RESET_MAY_NOT_ANSWER: Citation = Citation(
    manual="SH(NA)-080956ENG",
    revision="M",
    section="5.6 p.136",
    note=(
        "'When the subcommand is 0000 - If the access destination reset is succeeded, "
        "the response request is not be sent back to the external device.' JY997D56001-K "
        "p.108 agrees. It is the only command in the catalogue for which an absent "
        "response is the expected outcome, and over TCP the connection is torn down with "
        "the reset. Both manuals recommend UDP for remote operation for that reason."
    ),
)
"""Why :class:`RemoteReset` sets ``response_optional``."""

FIXED_FIELD_AMBIGUITY: Ambiguity = Ambiguity(
    key="A-REMOTE-FIXED",
    question=(
        "What are the two fixed bytes after the subcommand in 1002 Remote Stop, 1005 "
        "Remote Latch Clear and 1006 Remote Reset?"
    ),
    readings=(
        "01 00 -- SH(NA)-080956ENG-M pp.133/135/136, and what pymcprotocol 0.3.0 sends",
        "00 00 -- JY997D56001-K pp.106/107/108",
    ),
    chosen=(
        "profile.remote_fixed_field: 00 00 on iQ-F, 01 00 on the SLMP-reference "
        "families, overridable per call with fixed_field=. Never guessed, never retried "
        "in the other value."
    ),
    reason=(
        "Both readings were confirmed from rendered page images rather than a text "
        "layer, so this is a genuine difference between two Mitsubishi documents. The "
        "family-specific manual wins for its own family. Historically (SH(NA)-080008) "
        "the field is a Mode fixed at 0001H, which is why 01 00 has evidently worked in "
        "the field on Q and L."
    ),
    probe=(
        "FX5U in STOP with a scratch program: send 1002 with 01 00 and record the end "
        "code, then repeat with 00 00. We deliberately never sent any CPU-state command "
        "to the bench, so this is untested by us in both directions."
    ),
)
"""The row ``aslmp ambiguities`` prints for the fixed field, quoted in the docstrings."""

CLEAR_MODE_AMBIGUITY: Ambiguity = Ambiguity(
    key="A-CLEAR-MODE",
    question="Which clear modes does 1001 Remote Run accept on an FX5?",
    readings=(
        "Only 00H -- JY997D56001-K p.105's clear-mode table has exactly one row",
        (
            "00H, 01H and 02H -- the communication example on the SAME page sets clear "
            "mode to 'clear all devices including that in the latch range' and prints 02H"
        ),
    ),
    chosen=(
        "iQ-F profiles allow ClearMode.NONE only; anything else raises before the "
        "request is built."
    ),
    reason=(
        "The FX5 manual contradicts itself on one page, and the consequence of guessing "
        "wrong is clearing a running machine's latch range. Refusing is the only "
        "defensible default when the document disagrees with itself and we have no "
        "measurement."
    ),
    probe=(
        "1001 with clear mode 01H and then 02H against a scratch program on a CPU in "
        "STOP: record the end code, and check whether devices actually cleared."
    ),
)
"""The row behind the clear-mode refusal."""

_CITES: tuple[Source, ...] = (REMOTE_LAYOUT, FX5_REMOTE, RUN_LIES_IN_STOP)


class RunMode(enum.Enum):
    """Whether a remote RUN or PAUSE may override another device's remote STOP/PAUSE.

    ``NOT_FORCED``
        ``0001``. Refused if another external device is holding a remote STOP or PAUSE.
    ``FORCED``
        ``0003``. Proceeds anyway. SH(NA)-080956ENG-M's rationale is that forced
        execution exists for when the device that issued the remote STOP has itself
        failed and can no longer issue the RUN. It is the option that takes a machine
        out of somebody else's hands, so it is never the default.
    """

    NOT_FORCED = 0x0001
    FORCED = 0x0003

    @property
    def wire(self) -> int:
        """The 16-bit mode field: ``01 00`` in binary, ``"0001"`` in ASCII."""
        return int(self.value)


def _fixed_value(command: _RemoteFixedField, ctx: EncodeContext) -> int:
    """The 16-bit value of the two-byte fixed field, from the profile or the override.

    Held as a *value* rather than as bytes so that both codings fall out of one
    expression: ``codec.number(1, bits=16)`` is ``01 00`` in binary and ``"0001"`` in
    ASCII, and the two are not byte transformations of each other.
    """
    raw = command.fixed_field
    if raw is None:
        raw = ctx.profile.remote_fixed_field
    if len(raw) != 2:
        raise SlmpConfigurationError(
            f"the fixed field of 0x{command.CODE:04X} is exactly two bytes; got "
            f"{len(raw)} ({raw!r}). {FIXED_FIELD_AMBIGUITY.key}: "
            f"{FIXED_FIELD_AMBIGUITY.chosen}"
        )
    return int.from_bytes(raw, "little")


class _Remote(Command[None], abstract=True):
    """Shared interlock, capability gate and empty-response rule for remote control."""

    __slots__ = ()

    CAPABILITY: ClassVar[Capability] = Capability.REMOTE_CONTROL

    def subcommand(self, ctx: EncodeContext) -> int:
        del ctx
        return 0x0000

    def _require_interlock(self, ctx: EncodeContext) -> None:
        if not ctx.allow_remote_control:
            raise SlmpConfigurationError(
                f"{self.describe()} needs the client to have been constructed with "
                f"allow_remote_control=True. This library can stop a running machine "
                f"over an unauthenticated cleartext socket, so the interlock is "
                f"explicit and per client, never per call."
            )
        ctx.profile.require(self.CAPABILITY, what=self.describe())

    def validate(self, ctx: EncodeContext) -> None:
        self._require_interlock(ctx)

    def decode(self, payload: bytes, ctx: EncodeContext) -> None:
        del ctx
        expect_empty_payload(payload, what=self.describe())


class _RemoteFixedField(_Remote, abstract=True):
    """A remote command whose payload is only the contested two-byte fixed field."""

    __slots__ = ()

    fixed_field: bytes | None

    def payload_len(self, ctx: EncodeContext) -> int:
        return ctx.codec.number_len(FIXED_FIELD_UNITS)

    def encode(self, ctx: EncodeContext) -> bytes:
        return ctx.codec.number(_fixed_value(self, ctx), bits=FIXED_FIELD_UNITS)


# ----------------------------------------------------------------------------------------
# 1001 Remote Run
# ----------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RemoteRun(_Remote):
    """``1001``: put the access destination into RUN. Eight bytes, no response data.

    Golden vector, both manuals -- mode "not forced", clear mode "clear all devices
    including the latch range", binary::

        request data : 01 00 02 00
        ASCII        : "0001" "02" "00"

    The trailing ``00H`` is a fixed padding byte and is not optional. That example is
    the one JY997D56001-K prints on the same page as a clear-mode table that permits
    only ``00H``; on an iQ-F profile this class refuses to build it (``A-CLEAR-MODE``).

    An end code of ``0x0000`` here does not mean the CPU is running
    (:data:`RUN_LIES_IN_STOP`). The client's ``verify=True`` default reads SD203 back.
    """

    mode: RunMode = RunMode.NOT_FORCED
    clear: ClearMode = ClearMode.NONE

    CODE = 0x1001
    NAME = "Remote Run"
    mutates = True
    CITES = _CITES
    AMBIGUITIES = (CLEAR_MODE_AMBIGUITY,)

    def validate(self, ctx: EncodeContext) -> None:
        self._require_interlock(ctx)
        if self.clear not in ctx.profile.allowed_clear_modes:
            allowed = ", ".join(
                sorted(mode.name for mode in ctx.profile.allowed_clear_modes)
            )
            raise SlmpConfigurationError(
                f"{ctx.profile.key} accepts clear mode {allowed} and not "
                f"ClearMode.{self.clear.name}. {FX5_REMOTE.reference}'s clear-mode table "
                f"has exactly one row while the communication example on the same page "
                f"prints 02H -- the manual contradicts itself, we have no measurement, "
                f"and the consequence of guessing wrong is clearing a running machine's "
                f"latch range. See ambiguity {CLEAR_MODE_AMBIGUITY.key}."
            )

    def payload_len(self, ctx: EncodeContext) -> int:
        return ctx.codec.number_len(16) + 2 * ctx.codec.number_len(8)

    def encode(self, ctx: EncodeContext) -> bytes:
        return (
            ctx.codec.number(self.mode.wire, bits=16)
            + ctx.codec.number(int(self.clear), bits=8)
            + ctx.codec.number(0, bits=8)
        )

    def describe(self) -> str:
        return f"remote.run(mode={self.mode.name}, clear={self.clear.name})"


# ----------------------------------------------------------------------------------------
# 1003 Remote Pause
# ----------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RemotePause(_Remote):
    """``1003``: pause the access destination. Mode field only, no response data.

    Carries the same documented silent no-op as Remote RUN: with the switch in STOP the
    command "will be completed normally. However, the access destination does not change
    to the PAUSE status" (:data:`RUN_LIES_IN_STOP`).
    """

    mode: RunMode = RunMode.NOT_FORCED

    CODE = 0x1003
    NAME = "Remote Pause"
    mutates = True
    CITES = _CITES

    def payload_len(self, ctx: EncodeContext) -> int:
        return ctx.codec.number_len(16)

    def encode(self, ctx: EncodeContext) -> bytes:
        return ctx.codec.number(self.mode.wire, bits=16)

    def describe(self) -> str:
        return f"remote.pause(mode={self.mode.name})"


# ----------------------------------------------------------------------------------------
# 1002 / 1005 / 1006 -- the contested fixed field
# ----------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RemoteStop(_RemoteFixedField):
    """``1002``: stop the access destination. No modes, no clear, no response data.

    ``fixed_field`` defaults to ``profile.remote_fixed_field`` and may be overridden per
    call by whoever runs the ``A-REMOTE-FIXED`` probe. Nothing here tries the other value
    after a failure.
    """

    fixed_field: bytes | None = None

    CODE = 0x1002
    NAME = "Remote Stop"
    mutates = True
    CITES = _CITES
    AMBIGUITIES = (FIXED_FIELD_AMBIGUITY,)

    def describe(self) -> str:
        return "remote.stop()"


@dataclass(frozen=True, slots=True)
class RemoteLatchClear(_RemoteFixedField):
    """``1005``: clear the latch range. The CPU must already be in STOP.

    SH(NA)-080956ENG-M p.135 adds a second precondition this library cannot check:
    *"while the access destination is stopped or paused remotely by the request from the
    other external device, the Remote Latch Clear cannot be executed"*. That failure
    arrives as an end code and is surfaced, not retried.
    """

    fixed_field: bytes | None = None

    CODE = 0x1005
    NAME = "Remote Latch Clear"
    mutates = True
    CITES = _CITES
    AMBIGUITIES = (FIXED_FIELD_AMBIGUITY,)

    def describe(self) -> str:
        return "remote.latch_clear()"


@dataclass(frozen=True, slots=True)
class RemoteReset(_RemoteFixedField):
    """``1006``: reset the access destination. **Silence is the expected outcome.**

    :attr:`~aslmp.commands.base.Command.response_optional` is ``True`` here and nowhere
    else in this package. A successful reset sends no response (:data:`RESET_MAY_NOT_ANSWER`)
    and, over TCP, takes the connection with it; the client turns that into a
    ``ResetOutcome`` recording whether anything answered and whether the connection
    closed, rather than into a timeout.

    Two preconditions the wire cannot tell you about: the CPU must be in STOP, and the
    GX Works3 parameter *Remote Reset Setting* must be Enabled. Its default is Disable,
    which is the most likely reason a ``1006`` fails on a fresh FX5U.
    """

    fixed_field: bytes | None = None

    CODE = 0x1006
    NAME = "Remote Reset"
    mutates = True
    CITES = (*_CITES, RESET_MAY_NOT_ANSWER)
    AMBIGUITIES = (FIXED_FIELD_AMBIGUITY,)
    CAPABILITY: ClassVar[Capability] = Capability.REMOTE_RESET
    response_optional = True

    def describe(self) -> str:
        return "remote.reset()"
