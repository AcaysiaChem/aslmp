"""``1630`` Remote Password Unlock and ``1631`` Remote Password Lock.

Layer 2. Two commands, one layout: a 16-bit length and then the password's **literal
characters** -- not a hash, not hexadecimal, not any encoding of them
(SH(NA)-080956ENG-M section 5.10 pp.141-145). The manual's own worked example sends the
26-character ``abcdefghijklmnopqrstuvwxyz`` with a length field of ``1A 00``.

That is worth stating plainly rather than burying: **SLMP has no encryption and no
authentication**, so a remote password crosses the wire in clear text and is readable in
any packet capture between the client and the PLC. It gates access; it does not protect
the password. Anyone deploying this on a plant network should read the threat model in
the README first.

**The characters are literal in both codings.** In binary the field is the password's
bytes; in ASCII it is the same bytes, because ASCII coding renders *numeric* fields as
hexadecimal and this field is text. Only the length in front of it doubles. A client
that hex-encodes the password in ASCII coding sends a different password and is told
``0xC200``.

This package refuses a password that is not printable ASCII, because what a CPU does
with a byte outside that range is undocumented and the failure mode -- ``0xC810`` /
``0xC815`` / ``0xC816``, the authentication-failure lockout -- is one you do not want to
provoke by accident. Neither of these commands has been sent to our bench: no CPU-state
command ever was.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from aslmp.commands.base import Command, EncodeContext, expect_empty_payload
from aslmp.errors import SlmpConfigurationError
from aslmp.profile import Capability
from aslmp.wire.citations import Citation, Source

__all__ = [
    "MAX_PASSWORD_CHARS",
    "REMOTE_PASSWORD",
    "LockPassword",
    "UnlockPassword",
]


REMOTE_PASSWORD: Citation = Citation(
    manual="SH(NA)-080956ENG",
    revision="M",
    section="5.10 pp.141-145",
    note=(
        "Remote Password Unlock 1630H and Lock 1631H, subcommand 0000H. Request: the "
        "password character count (2 bytes low byte first in binary, 4 characters in "
        "ASCII) then the password's literal characters, as-is, in both codings. The "
        "worked example sends the 26-character password abcdefghijklmnopqrstuvwxyz with "
        "a length of 1AH 00H. No response data. Connected station only. Failures: "
        "C200H wrong password, C201H the port is locked, C204H a different device "
        "requested the unlock, C810H / C815H / C816H the authentication-failure lockout."
    ),
)
"""The layout, and the failure codes worth recognising before provoking them."""

MAX_PASSWORD_CHARS: int = 32
"""The longest password this package will send.

SH(NA)-080956ENG-M does not print a maximum for the field, whose count is 16 bits, and
the CPU-side limit is a GX Works3 parameter. 32 is a deliberate client-side ceiling
rather than a documented one: a length that a CPU truncates would authenticate against
a password nobody typed, and the failure is an authentication lockout.
"""

_CITES: tuple[Source, ...] = (REMOTE_PASSWORD,)


class _Password(Command[None], abstract=True):
    """Shared validation and emission for ``1630`` / ``1631``."""

    __slots__ = ()

    password: str

    def subcommand(self, ctx: EncodeContext) -> int:
        del ctx
        return 0x0000

    def _characters(self) -> bytes:
        return self.password.encode("ascii")

    def validate(self, ctx: EncodeContext) -> None:
        what = self.describe()
        ctx.profile.require(Capability.REMOTE_PASSWORD, what=what)
        if not isinstance(self.password, str):
            raise TypeError(
                f"a remote password is a str of literal characters, not "
                f"{type(self.password).__name__}"
            )
        if not self.password:
            raise SlmpConfigurationError(
                f"{what}: an empty remote password has nothing to send. The length field "
                f"would be 0 and the CPU would compare against nothing "
                f"({REMOTE_PASSWORD.reference})."
            )
        if len(self.password) > MAX_PASSWORD_CHARS:
            raise SlmpConfigurationError(
                f"{what}: this package sends at most {MAX_PASSWORD_CHARS} password "
                f"characters and this one is {len(self.password)}. The wire field is "
                f"16 bits wide and no manual prints a maximum, so the ceiling is "
                f"client-side and deliberate: a password a CPU truncates authenticates "
                f"against something nobody typed, and the failure is the C810H "
                f"authentication lockout."
            )
        for index, character in enumerate(self.password):
            if not 0x21 <= ord(character) <= 0x7E:
                raise SlmpConfigurationError(
                    f"{what}: character {index} of the password is {character!r} "
                    f"(U+{ord(character):04X}), which is not a printable non-space "
                    f"ASCII character. The password crosses the wire as literal bytes "
                    f"in both codings ({REMOTE_PASSWORD.reference}) and what a CPU does "
                    f"with anything else is undocumented; provoking the C810H "
                    f"authentication lockout to find out is not a good trade."
                )

    def payload_len(self, ctx: EncodeContext) -> int:
        return ctx.codec.number_len(16) + len(self.password)

    def encode(self, ctx: EncodeContext) -> bytes:
        characters = self._characters()
        return ctx.codec.number(len(characters), bits=16) + characters

    def decode(self, payload: bytes, ctx: EncodeContext) -> None:
        del ctx
        expect_empty_payload(payload, what=self.describe())


@dataclass(frozen=True, slots=True)
class UnlockPassword(_Password):
    """``1630``: unlock the remote password on the connected station.

    ``describe()`` prints the password's **length**, never its characters: a
    :class:`~aslmp.errors.SlmpError` renders its request line into whatever a user
    pastes into an issue.
    """

    password: str

    CODE = 0x1630
    NAME = "Remote Password Unlock"
    mutates = True
    CITES: ClassVar[tuple[Source, ...]] = _CITES

    def describe(self) -> str:
        return f"remote.unlock(<{len(self.password)} characters>)"


@dataclass(frozen=True, slots=True)
class LockPassword(_Password):
    """``1631``: lock the remote password again on the connected station."""

    password: str

    CODE = 0x1631
    NAME = "Remote Password Lock"
    mutates = True
    CITES: ClassVar[tuple[Source, ...]] = _CITES

    def describe(self) -> str:
        return f"remote.lock(<{len(self.password)} characters>)"
