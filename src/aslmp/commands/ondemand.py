"""``2101`` Ondemand -- recognised on receive, never sent, never mistaken for a response.

Layer 2. Ondemand is the one message in SLMP that travels the wrong way: the PLC program
pushes it to the external device with no request behind it, carrying up to 1920 bytes
(960 words) of send data (SH(NA)-080956ENG-M section 5.11 p.204). It uses the **request**
subheader -- ``50 00`` on 3E, ``54 00`` on 4E -- and command ``2101H`` with subcommand
``0000H``.

**Why this module exists at all.** A receive path that treats "the next thing that
arrives" as its response is desynchronised by one of these for the life of the
connection: every subsequent read returns the previous transaction's answer, with end
code ``0x0000`` and nothing anywhere to say so. That is exactly the failure class this
library is built to make impossible, so an unsolicited ``2101`` is named, raised and
counted -- and the stream is **not** resynchronised by skipping forward to the next thing
that looks like a subheader, because skipping is how the corruption becomes permanent.

``wire/frames.py`` already refuses a request-shaped subheader on a response path with
:class:`~aslmp.wire.raw.SlmpUnsolicitedFrameError`; this module is what turns a frame
that *has* been parsed as a request into a typed record for a proxy, a simulator or an
``aslmp explain`` dump.

**There is no ``Ondemand`` command class, deliberately.** Every other entry in
``commands/`` is something a client can send. This one is not: SLMP defines no way for
an external device to originate a ``2101``, and a class with an ``encode`` that raised
would be a door with a wall behind it. :data:`ONDEMAND` in the registry records the
command code, its direction and its citation, and carries no class.

Nothing here has been observed on hardware: no ``2101`` ever arrived on the bench,
because the FX5U's scratch program was never asked to send one.
"""

from __future__ import annotations

from dataclasses import dataclass

from aslmp.errors import SlmpUnsolicitedFrameError
from aslmp.wire.citations import Citation
from aslmp.wire.codec import Codec
from aslmp.wire.raw import RawRequest

__all__ = [
    "ONDEMAND_COMMAND",
    "ONDEMAND_LAYOUT",
    "ONDEMAND_MAX_BYTES",
    "ONDEMAND_SUBCOMMAND",
    "OnDemandMessage",
    "is_ondemand",
    "parse_ondemand",
    "refuse_ondemand_as_response",
]


ONDEMAND_LAYOUT: Citation = Citation(
    manual="SH(NA)-080956ENG",
    revision="M",
    section="5.11 p.204",
    note=(
        "Ondemand, command 2101H subcommand 0000H: a message sent from the "
        "SLMP-compatible device to the external device with no request behind it, "
        "carrying up to 1920 bytes (960 words) of send data. It uses the request "
        "subheader, so on a response path it is a request-shaped frame arriving "
        "unbidden."
    ),
)
"""The only description of the wrong-way message."""

ONDEMAND_COMMAND: int = 0x2101
"""The command code of an unsolicited PLC-to-external message."""

ONDEMAND_SUBCOMMAND: int = 0x0000
"""The only subcommand Ondemand is documented with."""

ONDEMAND_MAX_BYTES: int = 1920
"""960 words, the documented maximum of the send-data field."""


@dataclass(frozen=True, slots=True)
class OnDemandMessage:
    """One Ondemand frame, as data.

    ``data`` is the send-data field exactly as it arrived, undecoded. This package has
    no idea what a given PLC program puts in there, and guessing a structure for it would
    be the same class of mistake as guessing a device radix.
    """

    data: bytes
    subcommand: int
    raw: bytes

    def __post_init__(self) -> None:
        object.__setattr__(self, "data", bytes(self.data))
        object.__setattr__(self, "raw", bytes(self.raw))

    def __str__(self) -> str:
        return f"ondemand(0x{ONDEMAND_COMMAND:04X}, {len(self.data)} unit(s))"


def is_ondemand(command: int) -> bool:
    """Whether ``command`` is the Ondemand code."""
    return command == ONDEMAND_COMMAND


def parse_ondemand(request: RawRequest, codec: Codec) -> OnDemandMessage:
    """Turn a parsed request frame carrying ``2101`` into a typed record.

    Raises if ``request`` is not an Ondemand: a caller that reached this function with
    something else has misidentified the frame, and returning an empty message would
    hand them a plausible-looking nothing.
    """
    if not is_ondemand(request.command):
        raise SlmpUnsolicitedFrameError(
            f"parse_ondemand() was given command 0x{request.command:04X}, which is not "
            f"Ondemand (0x{ONDEMAND_COMMAND:04X}). Request-shaped frames arriving on a "
            f"response path are never parsed speculatively "
            f"({ONDEMAND_LAYOUT.reference})."
        )
    if request.subcommand != ONDEMAND_SUBCOMMAND:
        raise SlmpUnsolicitedFrameError(
            f"an Ondemand frame carried subcommand 0x{request.subcommand:04X}; the only "
            f"documented value is 0x{ONDEMAND_SUBCOMMAND:04X} "
            f"({ONDEMAND_LAYOUT.reference}). Please report this with the CPU model and "
            f"firmware rather than treating it as 0x0000."
        )
    limit = ONDEMAND_MAX_BYTES * codec.width
    if len(request.payload) > limit:
        raise SlmpUnsolicitedFrameError(
            f"an Ondemand frame carried {len(request.payload)} wire unit(s) of send "
            f"data; the documented maximum is {ONDEMAND_MAX_BYTES} bytes, which is "
            f"{limit} unit(s) in {codec.name} coding ({ONDEMAND_LAYOUT.reference})."
        )
    return OnDemandMessage(
        data=request.payload, subcommand=request.subcommand, raw=request.raw
    )


def refuse_ondemand_as_response(request: RawRequest) -> SlmpUnsolicitedFrameError:
    """The exception to raise when an Ondemand arrives where a response was expected.

    Returned rather than raised so the caller keeps its own ``raise`` statement and its
    own ``from``. The message says what happened, why the stream is not resynchronised,
    and what the receiver should do instead.
    """
    return SlmpUnsolicitedFrameError(
        f"an Ondemand frame (command 0x{request.command:04X}, "
        f"{len(request.payload)} unit(s) of send data) arrived where a response was "
        f"expected. It is a message the PLC program originated, not an answer to any "
        f"request ({ONDEMAND_LAYOUT.reference}). This transaction has no answer yet, "
        f"and the stream is deliberately NOT resynchronised: treating the next thing "
        f"that arrives as this transaction's response is how one unsolicited frame "
        f"turns every later read into the previous read's data, with end code 0x0000 "
        f"and nothing to say so."
    )
