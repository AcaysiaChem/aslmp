"""The access route: the five bytes that say which CPU the request is for.

Layer 0. **stdlib only.** Importing this module must not pull ``socket``, ``ssl``,
``asyncio``, ``selectors``, ``threading`` or ``logging`` into ``sys.modules``
(``tests/unit/test_layering.py`` proves it in a subprocess).

Four fields, five binary bytes, ten ASCII characters, in this order and no other
(SH(NA)-080956ENG-M section 4 pp.19-22; SH(NA)-080008-AB p.48 calls the same five bytes
the "access route" and renames every field, which is worth knowing when reading the two
manuals side by side):

==========================  =====  =====  =========================================
Field                       bin    ASCII  Value for a direct Ethernet connection
==========================  =====  =====  =========================================
request destination network     1      2  ``00``   own station
request destination station     1      2  ``FF``   own station
request destination module      2      4  ``FF 03`` little-endian = 03FFH = own CPU
    I/O number
request destination                1   2  ``00``   not a multidrop target
    multidrop station
==========================  =====  =====  =========================================

SH(NA)-080008-AB p.48 prints the canonical route verbatim as ``00H FFH FFH 03H 00H`` in
binary and ``"00FF03FF00"`` in ASCII. That is :data:`Route.OWN_STATION`, and it is what
every connection in this library uses unless somebody says otherwise.

The module I/O number is the only multi-byte field here, and it is little-endian like
every other binary numeric field in SLMP: ``03FFH`` goes out as ``FF 03``. Writing it
``03 FF`` addresses module I/O ``FF03H``, which is not a documented value and which the
PLC answers with a routing end code rather than silence -- one of the few mistakes in
this layer that does *not* return plausible data.

A response echoes the request's route back, and an abnormal response carries a second
route in its error information block naming the station that actually answered
(SH(NA)-080956ENG-M pp.27-28). :meth:`Route.decode` reads both; comparing them is
``wire/raw.py``'s job, not this module's.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Final

from aslmp.wire.citations import Citation

if TYPE_CHECKING:  # pragma: no cover - typing only
    from aslmp.wire.codec import Codec

__all__ = ["ACCESS_ROUTE", "Route", "SlmpRouteError"]


ACCESS_ROUTE: Final = Citation(
    manual="SH(NA)-080956ENG",
    revision="M",
    section="4.2 pp.19-22",
    note=(
        "Request destination network No. (00H = own station, 01H-EFH = network; 240-255 "
        "are not accessible), station No. (FFH = own station, 01H-78H = station, 7CH-7EH "
        "the special values), module I/O No. (03FFH = own station / control CPU, 2 bytes "
        "little-endian), multidrop station No. (00H-1FH, 00H when not multidrop). "
        "SH(NA)-080008-AB p.48 gives the connected-station route as 00H FFH FFH 03H 00H "
        "in binary and '00FF03FF00' in ASCII."
    ),
)
"""The one source for every value range in this module. Quoted in its refusals."""

_NETWORK_MAX: Final = 0xEF
_STATION_SPECIAL: Final[frozenset[int]] = frozenset({0x7C, 0x7D, 0x7E, 0xFF})
_STATION_MAX: Final = 0x78
_MODULE_IO_MAX: Final = 0xFFFF
_MULTIDROP_MAX: Final = 0x1F


class SlmpRouteError(ValueError):
    """An access route field outside what the manual documents.

    Local to layer 0 because ``aslmp.errors`` imports ``aslmp.wire`` and the reverse edge
    would close a cycle the layering test fails the build for; wrapped as
    ``SlmpConfigurationError`` (DESIGN section 3.1). The message always names
    :data:`ACCESS_ROUTE`, because a route that is legal on somebody's CC-Link IE network
    and refused here is a bug report we want to receive with the page number attached.
    """


@dataclass(frozen=True, slots=True)
class Route:
    """Where a request is going, and what a response says it came from.

    The defaults are the direct-Ethernet route -- the CPU at the other end of the socket
    -- and are identical to :data:`Route.OWN_STATION`. They are declared constants, not
    a probe: nothing in this library discovers a route.

    Frozen and slotted. A route is baked into every prebuilt frame at bind time, and a
    mutable one would let a validated plan address a different station later.
    """

    network: int = 0x00
    station: int = 0xFF
    module_io: int = 0x03FF
    multidrop: int = 0x00

    OWN_STATION: ClassVar[Route]
    """``00 FF FF 03 00`` -- the CPU at the other end of this socket."""

    def __post_init__(self) -> None:
        _check(self.network, "network", 0xFF)
        if not (self.network == 0x00 or 0x01 <= self.network <= _NETWORK_MAX):
            raise SlmpRouteError(
                f"request destination network No. 0x{self.network:02X} is not "
                f"accessible: 00H is the own station and 01H-EFH are network numbers "
                f"1-239. Networks 240-255 cannot be reached ({ACCESS_ROUTE.reference})."
            )
        _check(self.station, "station", 0xFF)
        if not (
            self.station in _STATION_SPECIAL or 0x01 <= self.station <= _STATION_MAX
        ):
            raise SlmpRouteError(
                f"request destination station No. 0x{self.station:02X} is not a "
                f"documented value: FFH is the own station, 01H-78H are stations 1-120, "
                f"7CH means the number is in the extension station field (station "
                f"number extension frame only), 7DH is the assigned control station and "
                f"7EH the present one ({ACCESS_ROUTE.reference})."
            )
        _check(self.module_io, "module_io", _MODULE_IO_MAX)
        _check(self.multidrop, "multidrop", 0xFF)
        if self.multidrop > _MULTIDROP_MAX:
            raise SlmpRouteError(
                f"request destination multidrop station No. 0x{self.multidrop:02X} is "
                f"out of range: 00H-1FH (0-31), and 00H when the target is not a "
                f"multidrop station ({ACCESS_ROUTE.reference})."
            )

    def encode(self, codec: Codec) -> bytes:
        """The five route fields, in wire order, in this coding.

        ``Route.OWN_STATION.encode(BINARY)`` is ``00 FF FF 03 00`` and
        ``Route.OWN_STATION.encode(ASCII)`` is ``b"00FF03FF00"`` -- both printed
        verbatim at SH(NA)-080008-AB p.48. Note that they are **not** byte
        transformations of one another: the module I/O number is little-endian in binary
        and most-significant-digit-first in ASCII, which is exactly the property that
        makes hexlifying a binary frame into an ASCII one wrong.
        """
        return (
            codec.number(self.network, bits=8)
            + codec.number(self.station, bits=8)
            + codec.number(self.module_io, bits=16)
            + codec.number(self.multidrop, bits=8)
        )

    @classmethod
    def decode(cls, buf: bytes, off: int, codec: Codec) -> Route:
        """The route at ``off``. Raises if it is short, malformed or undocumented.

        Used for the route a response echoes and for the second route inside an abnormal
        response's error information block. The value ranges are enforced on the way in
        as well as on the way out: a route we cannot name is a frame we have not
        understood, and believing it is how a reply from the wrong station becomes an
        answer.
        """
        network = codec.read_number(buf, off, bits=8)
        station = codec.read_number(buf, off + codec.number_len(8), bits=8)
        module_io = codec.read_number(buf, off + 2 * codec.number_len(8), bits=16)
        multidrop = codec.read_number(
            buf, off + 2 * codec.number_len(8) + codec.number_len(16), bits=8
        )
        return cls(network, station, module_io, multidrop)

    @staticmethod
    def wire_len(codec: Codec) -> int:
        """5 in binary, 10 in ASCII. Part of the 7-unit prefix that ``L`` excludes."""
        return 3 * codec.number_len(8) + codec.number_len(16)

    def __str__(self) -> str:
        if self == Route.OWN_STATION:
            return "own station (00 FF FF 03 00)"
        return (
            f"network 0x{self.network:02X}, station 0x{self.station:02X}, "
            f"module I/O 0x{self.module_io:04X}, multidrop 0x{self.multidrop:02X}"
        )


def _check(value: int, field: str, maximum: int) -> None:
    """Refuse a route field that is not an int, is negative, or does not fit."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"Route.{field} must be an int, not {type(value).__name__}")
    if not 0 <= value <= maximum:
        raise SlmpRouteError(
            f"Route.{field} must be 0..0x{maximum:02X}; got {value}. Nothing here masks "
            f"or wraps a route field ({ACCESS_ROUTE.reference})."
        )


Route.OWN_STATION = Route()
"""The direct-Ethernet access route: network 00H, station FFH, module I/O 03FFH,
multidrop 00H. SH(NA)-080008-AB p.48 prints it as ``00H FFH FFH 03H 00H``."""
