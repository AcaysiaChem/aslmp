"""The two SLMP wire codings, as one protocol with two singletons.

Layer 0. **stdlib only.** Importing this module must not pull ``socket``, ``ssl``,
``asyncio``, ``selectors``, ``threading`` or ``logging`` into ``sys.modules``
(``tests/unit/test_layering.py`` proves it in a subprocess).

Every numeric field on an SLMP wire is the same idea twice. In binary coding a field is
its little-endian bytes; in ASCII coding it is its uppercase hexadecimal rendering,
most-significant digit first, zero-padded to exactly twice the binary width
(SH(NA)-080956ENG-M pp.19, 21, 23, 24, 27, 28, each carrying the pair of headings *"When
communicating data in ASCII code - Send the data in order from the upper byte to the
lower byte"* / *"...in binary code - Send the data in order from the lower byte to the
upper byte"*).

**The 32-bit number field is the decisive one.** ``BINARY.number(v, bits=32)`` is
``struct.pack("<I", v)`` and ``ASCII.number(v, bits=32)`` is ``f"{v:08X}".encode()``.
Those are the same value under each coding's own rule, and they put the low word first
in binary and the high word first in ASCII **without anybody writing a word-order
branch**. Every 32-bit thing in the library -- ``f32``, ``i32``, ``u32``, a ``0403``
double-word access point -- is then one expression over this one field, and the measured
low-word-first fact (FX5U-32MT/DS fw 1.065, 2026-09-06, proved four ways) is free rather
than repeated::

    struct.unpack("<f", struct.pack("<I", v))[0]

**Lengths are computed per codec, never by doubling.** The 2x rule holds for every fixed
field and for word data, and breaks for bit-unit data with an odd point count:
SH(NA)-080956ENG-M p.40 gives one ASCII character per point and one binary *nibble* per
point, high nibble first, with the trailing low nibble padded to 0. So
``ascii_len(n) == n`` and ``binary_len(n) == ceil(n / 2)``, and for odd ``n``
``ascii_len(n) == 2 * binary_len(n) - 1``. A library that computes an ASCII length as
twice a binary length overstates the request data length of every odd-count bit write --
and an overstated length is the one failure on this hardware that produces **no response
at all** and looks exactly like a dead PLC (FX5U-32MT/DS fw 1.065, 2026-09-06).

**ASCII device codes are mnemonics, never hex.** ``D`` goes out as ``b"D*"`` /
``b"D***"``, never as ``b"A8"``. Padding is ``*`` (0x2A) on emit; SH(NA)-080956ENG-M p.38
footnote 1 also permits a space, which :func:`parse_ascii_device_code` accepts on the way
in (SH(NA)-080956ENG-M pp.35-38).

**An ASCII device NUMBER is not hexadecimal.** It is the linear index written in the
device's own radix, so ``M1111`` goes out as ``"001111"`` while its binary field is
``57 04 00`` (0457H). The two are not transformations of one another
(SH(NA)-080956ENG-M p.55, which prints the same Read Random request in both codings).
:class:`Notation` says which base, :meth:`Codec.device_number` takes it as a required
keyword, and there is no default -- getting it wrong reaches a different register and
answers ``0x0000``.

**Case is refused, not normalised.** The manuals say *"Use capitalized code for
alphabetical character"* (SH(NA)-080956ENG-M pp.39-41) and never say what a PLC does with
lower case. We emit upper case and :meth:`Codec.read_number` raises on anything outside
``[0-9A-F]`` rather than returning 0 for a bad nibble or quietly upper-casing it. That is
ambiguity ``A-ASCII-CASE``, resolved by choice.

What this module deliberately does **not** know: which field comes first. The binary
``[number][code]`` versus ASCII ``[code][number]`` swap of SH(NA)-080956ENG-M p.45 is
``wire/devspec.py``'s single job, and :class:`Notation` is declared here only so that the
profile and ``devspec`` share one spelling of it.
"""

from __future__ import annotations

import enum
from typing import TYPE_CHECKING, Final, Literal, Protocol

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    # devicetable imports THIS module for SpecFormat and Unit, so the runtime edge only
    # runs one way. DeviceType is needed for a signature, not for behaviour.
    from aslmp.wire.devicetable import DeviceType

__all__ = [
    "ASCII",
    "BINARY",
    "CODECS",
    "LOOPBACK_MAX_BYTES",
    "LOOPBACK_MIN_BYTES",
    "AsciiCodec",
    "BinaryCodec",
    "Codec",
    "Notation",
    "SlmpCodecError",
    "SlmpCodecValueError",
    "SlmpHexDigitError",
    "SlmpShortBufferError",
    "SpecFormat",
    "Unit",
    "parse_ascii_device_code",
]


# ----------------------------------------------------------------------------------------
# Enums
# ----------------------------------------------------------------------------------------


class SpecFormat(enum.Enum):
    """Which device specification a request uses.

    ``SHORT``
        Subcommand ``0000``/``0001`` (and their ``0080``/``0081`` extension forms): a
        1-byte device code and a 3-byte device number in binary, a 2-character mnemonic
        and 6 digits in ASCII.
    ``LONG``
        Subcommand ``0002``/``0003``: a 2-byte code and a 4-byte number in binary, a
        4-character mnemonic and 8 digits in ASCII.

    SH(NA)-080956ENG-M pp.35-38. An FX5U-32MT/DS on firmware 1.065 refuses ``LONG``
    outright with end code ``0xC059`` (measured 2026-09-06), which is why the profile
    carries it as a capability rather than the codec carrying it as an option.
    """

    SHORT = "short"
    LONG = "long"


class Unit(enum.Enum):
    """Whether a command counts bit points or word points.

    The two are not interchangeable: SH(NA)-080956ENG-M p.40 gives bit data one ASCII
    character or one binary nibble per point, and word data four characters or two bytes.
    """

    BIT = "bit"
    WORD = "word"


class Notation(enum.Enum):
    """Which base the ASCII device-number digits are written in.

    A binary device number is the linear index as little-endian bytes and has no
    notation at all. An ASCII device number is the same index written **in the device's
    own radix**, which is the part that surprises people:

    ======  ===========  ===================  ==============
    Device  Linear index  Binary field         ASCII field
    ======  ===========  ===================  ==============
    M1111   1111          ``57 04 00``         ``"001111"``
    D1500   1500          ``DC 05 00``         ``"001500"``
    B1234   4660          ``34 12 00``         ``"001234"``
    X20     32            ``20 00 00``         ``"000020"``
    ======  ===========  ===================  ==============

    SH(NA)-080956ENG-M p.55 prints exactly that Read Random request in both codings, and
    p.38 gives the widths. The two fields are **not** transformations of one another,
    and a client that renders the ASCII field as hexadecimal addresses ``M1111`` as
    ``M4369`` with end code ``0x0000`` and nothing anywhere to say so. (``pymcprotocol``
    0.3.0 ``type3e.py:339`` has the mirror-image bug: it renders a hexadecimal-radix
    device number in decimal, so ``X0A0`` reaches a different register depending on
    which coding the connection uses.)

    ``VALUE``
        Write the digits in the device's radix as the generic device table gives it --
        decimal for D, M, T, C, S, Z, R, SM, SD; hexadecimal for X, Y, B, W, SB, SW, DX,
        DY, ZR. Every device, every family, every coding but one.
    ``OCTAL_DIGITS``
        Write them in base 8, so GX Works3's ``Y45`` (linear index 37) goes out as
        ``"000045"`` rather than ``"000025"``. This is X and Y on an iQ-F under
        ``Encoding.ASCII_XY_OCT`` and nothing else (JY997D56001-K p.12: *"ASCII code
        (X, Y OCT): octal; ASCII code (X, Y HEX): hexadecimal"*). It is a GX Works3
        connection parameter that cannot be read off the wire.

        Note that this is the **emit** radix, which is not the parse radix: on an iQ-F
        the literal ``Y45`` is octal in *both* ASCII modes -- the profile's radix
        override -- and only the rendering changes. Under ``ASCII_XY_OCT`` the same
        point is ``"000045"``; under ``ASCII_XY_HEX`` it is ``"000025"``. In binary it
        is ``25 00 00``: the linear index, measured (FX5U-32MT/DS fw 1.065, 2026-09-06 --
        GX Works3 ``Y20``, the 17th output, goes out as 16). See ambiguity ``A-IQF-XY``.

    Declared here so the profile, ``devspec`` and this module share one spelling.
    Choosing between them is ``profile.notation_for(dt, encoding)``; turning the choice
    into a base and calling :meth:`Codec.device_number` is ``wire/devspec.py``'s job.
    """

    VALUE = "value"
    OCTAL_DIGITS = "octal-digits"

    @property
    def fixed_base(self) -> int | None:
        """The base this notation forces, or ``None`` when the device's radix decides."""
        return 8 if self is Notation.OCTAL_DIGITS else None


# ----------------------------------------------------------------------------------------
# Errors
#
# These live here rather than in ``aslmp.errors`` because ``aslmp.errors`` is layer 0.5
# and imports ``aslmp.wire``: a codec that imported the exception hierarchy would close
# the cycle, and ``tests/unit/test_layering.py`` fails the build for it. DESIGN section
# 4.1 asks for ``SlmpFrameFormatError`` here; the honest split is that a codec has no
# frame to name, so it raises these low-level, typed refusals and the frame layer
# translates them with the context (frame, offset, transaction) that makes the message
# worth reading. See the concerns recorded with build unit U2.
# ----------------------------------------------------------------------------------------


class SlmpCodecError(ValueError):
    """Base for everything a codec refuses to encode or decode.

    Also a ``ValueError`` for the same reason ``SlmpUsageError`` is (DESIGN section
    3.2): an existing ``except ValueError`` keeps working, and the alternative is people
    writing ``except Exception``.
    """


class SlmpHexDigitError(SlmpCodecError):
    """An ASCII field contains a character outside ``[0-9A-F]``.

    Never a silent 0. SH(NA)-080956ENG-M pp.39-41 require capitalised codes; whether a
    PLC accepts lower case is undocumented, so lower case raises here too rather than
    being normalised (``A-ASCII-CASE``).
    """


class SlmpShortBufferError(SlmpCodecError):
    """A field runs past the end of the buffer it was to be read from.

    The reader is length-driven, so reaching this means the response did not have the
    shape the request implies -- never that we should read what is there and hope.
    """


class SlmpCodecValueError(SlmpCodecError):
    """A value this codec cannot render, or a wire value no manual documents.

    Encoding: a number wider than its field, a negative point count, a device whose
    specification format has no code. Decoding: a binary bit nibble that is neither 0
    nor 1, or an ASCII bit character that is neither ``'0'`` nor ``'1'``.
    """


# ----------------------------------------------------------------------------------------
# Shared constants and checks
# ----------------------------------------------------------------------------------------

_HEX_DIGITS: Final[frozenset[int]] = frozenset(b"0123456789ABCDEF")
"""The 16 byte values SLMP permits in an ASCII numeric field. Upper case only."""

LOOPBACK_MIN_BYTES: Final = 1
LOOPBACK_MAX_BYTES: Final = 960
"""``0619`` Self Test loopback data range, SH(NA)-080956ENG-M and JY997D56001-K.

Both manuals additionally restrict the payload to ``'0'``-``'9'`` and ``'A'``-``'F'``.
Whether a CPU enforces it is unknown (``A-LOOPBACK-CHARSET``); we enforce it client-side,
against our own defaults included.
"""

_ASCII_ZERO: Final = 0x30
_ASCII_ONE: Final = 0x31
_ASCII_STAR: Final = 0x2A
_ASCII_SPACE: Final = 0x20

_BYTES_FOR_BITS: Final[dict[int, int]] = {8: 1, 16: 2, 32: 4}

_BASE_FORMAT: Final[dict[int, str]] = {8: "o", 10: "d", 16: "X"}
"""The three radices a device number can be written in. ``Radix`` is an ``IntEnum``
whose value IS the base, so ``base=dt.radix`` passes straight through."""


def _binary_width(bits: int) -> int:
    """The binary byte width of an ``bits``-wide numeric field, or raise.

    ``bits`` is statically ``Literal[8, 16, 32]``; this is the runtime half, because a
    caller reached through ``Codec`` from untyped code can still pass 24.
    """
    width = _BYTES_FOR_BITS.get(bits)
    if width is None:
        raise SlmpCodecValueError(
            f"SLMP numeric fields are 8, 16 or 32 bits wide; got bits={bits!r}. "
            f"A 24-bit field exists only as the SHORT device number, which is "
            f"Codec.device_number()."
        )
    return width


def _check_unsigned(value: int, bits: int, what: str) -> None:
    """Refuse a value that does not fit its field, rather than truncating it."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise SlmpCodecValueError(
            f"{what} must be an int, not {type(value).__name__}"
        )
    if not 0 <= value < (1 << bits):
        raise SlmpCodecValueError(
            f"{what} must fit an unsigned {bits}-bit field (0..{(1 << bits) - 1}); "
            f"got {value}. Signed and float conversion happens above the codec: "
            f"nothing here masks, clamps or wraps."
        )


def _check_count(count: int, what: str) -> None:
    """Refuse a negative count. Zero is arithmetic here and a usage error above."""
    if not isinstance(count, int) or isinstance(count, bool):
        raise SlmpCodecValueError(f"{what} must be an int, not {type(count).__name__}")
    if count < 0:
        raise SlmpCodecValueError(f"{what} must not be negative; got {count}")


def _span(buf: bytes, off: int, length: int, codec: str, what: str) -> bytes:
    """``buf[off:off + length]``, or raise naming what did not fit.

    Python slicing silently shortens; this is the one place that is turned back into an
    error, because a short slice becomes a plausible wrong number a few lines later.
    """
    if not isinstance(off, int) or isinstance(off, bool):
        raise SlmpCodecValueError(f"offset must be an int, not {type(off).__name__}")
    if off < 0:
        raise SlmpCodecValueError(f"offset must not be negative; got {off}")
    end = off + length
    if end > len(buf):
        raise SlmpShortBufferError(
            f"{codec}: reading {what} needs {length} byte(s) at offset {off}, but the "
            f"buffer holds {len(buf)}. The response is shorter than the request's "
            f"point layout implies."
        )
    return bytes(buf[off:end])


def _check_loopback(data: bytes) -> bool:
    """``0619`` payload rule, identical in both codings."""
    if not isinstance(data, bytes | bytearray | memoryview):
        raise SlmpCodecValueError(
            f"loopback payload must be bytes, not {type(data).__name__}"
        )
    raw = bytes(data)
    if not LOOPBACK_MIN_BYTES <= len(raw) <= LOOPBACK_MAX_BYTES:
        return False
    return all(byte in _HEX_DIGITS for byte in raw)


def _check_hex(raw: bytes, off: int, what: str) -> None:
    """Every byte of an ASCII numeric field must be ``0-9`` or ``A-F``."""
    for index, byte in enumerate(raw):
        if byte not in _HEX_DIGITS:
            position = off + index
            shown = chr(byte) if 0x20 <= byte <= 0x7E else f"\\x{byte:02x}"
            hint = (
                " Lower case is refused rather than normalised: SH(NA)-080956ENG-M "
                "pp.39-41 say to use capitalised code and do not say what a PLC does "
                "with anything else (A-ASCII-CASE)."
                if 0x61 <= byte <= 0x7A
                else ""
            )
            raise SlmpHexDigitError(
                f"ascii: {what} contains {shown!r} (0x{byte:02X}) at buffer offset "
                f"{position}, which is not one of 0-9 or A-F.{hint}"
            )


def _check_base(base: int) -> str:
    """The ``format`` conversion for a device-number radix, or raise."""
    # 10.0 hashes equal to 10, so a plain dict lookup would accept a float base.
    conversion = _BASE_FORMAT.get(base) if isinstance(base, int) else None
    if conversion is None:
        raise SlmpCodecValueError(
            f"a device number is written in base 8, 10 or 16; got base={base!r}. "
            f"The base is the device's own radix (Radix is an IntEnum whose value is "
            f"the base), overridden to 8 for iQ-F X/Y under ASCII code (X, Y OCT)."
        )
    return conversion


def _mnemonic(dt: DeviceType, spec: SpecFormat) -> str:
    """The ASCII mnemonic for ``dt`` at ``spec``, or raise naming the device."""
    text = dt.ascii2 if spec is SpecFormat.SHORT else dt.ascii4
    expected = 2 if spec is SpecFormat.SHORT else 4
    if not text:
        raise SlmpCodecValueError(
            f"ascii: device {dt.name} has no {expected}-character mnemonic, so it "
            f"cannot be addressed with the {spec.value} device specification. It "
            f"requires subcommand 0002/0003."
        )
    if len(text) != expected:
        raise SlmpCodecValueError(
            f"ascii: device {dt.name} has the {expected}-character mnemonic {text!r}, "
            f"which is {len(text)} characters. The device table is wrong; regenerate "
            f"it from data/devices.tsv."
        )
    return text


def parse_ascii_device_code(field: bytes) -> str:
    """The device mnemonic in an ASCII device-code field, padding removed.

    Emit is always ``*`` (0x2A), because that is what every Mitsubishi example prints.
    On the way in, SH(NA)-080956ENG-M p.38 footnote 1 also permits a space (0x20)
    *"instead of the second character and the following characters '*'"*, so both are
    accepted here -- and nothing else is. ``b"D*"`` and ``b"D "`` both give ``"D"``;
    ``b"D0"`` gives ``"D0"``, which is a device name and not padding.

    Only ``aslmp.testing`` and the proxy parse requests; a client never does. It lives
    here so that the padding rule is stated once, next to the emit rule it mirrors.
    Trailing ``*`` and ``' '`` are stripped together, so ``b"D* "`` -- which no
    Mitsubishi example prints but which the footnote's wording permits -- resolves
    rather than tripping the character check below.
    """
    if not isinstance(field, bytes | bytearray | memoryview):
        raise SlmpCodecValueError(
            f"device code field must be bytes, not {type(field).__name__}"
        )
    raw = bytes(field)
    if len(raw) not in (2, 4):
        raise SlmpCodecValueError(
            f"ascii: a device code field is 2 characters (short specification) or 4 "
            f"(long); got {len(raw)}: {raw!r}"
        )
    body = raw.rstrip(bytes((_ASCII_STAR, _ASCII_SPACE)))
    if not body:
        raise SlmpCodecValueError(f"ascii: device code field is all padding: {raw!r}")
    for index, byte in enumerate(body):
        if not (0x30 <= byte <= 0x39 or 0x41 <= byte <= 0x5A):
            shown = chr(byte) if 0x20 <= byte <= 0x7E else f"\\x{byte:02x}"
            raise SlmpCodecValueError(
                f"ascii: device code {raw!r} contains {shown!r} at position {index}; "
                f"a mnemonic is upper-case letters and digits, padded with '*' or ' '."
            )
    return body.decode("ascii")


# ----------------------------------------------------------------------------------------
# The protocol
# ----------------------------------------------------------------------------------------


class Codec(Protocol):
    """One SLMP communication data code: how a value becomes wire bytes.

    Two implementations ship, as the singletons :data:`BINARY` and :data:`ASCII`. Which
    one a connection uses is a per-connection GX Works3 parameter (Ethernet Port -> SLMP
    connection -> Communication Data Code). It is **not** negotiable and **not**
    discoverable from the wire: sending a well-formed ASCII request into an entry
    configured for binary produced nothing at all on an FX5U-32MT/DS fw 1.065 -- 4000 ms,
    zero bytes, connection still open, no FIN (measured 2026-09-06; end code ``0xC06F``
    is registered in the PLC's error history and never sent). That is why the client
    takes the coding as a declared constant and proves it with a ``0619`` handshake.

    Every length method returns **wire** bytes for this codec, so callers never double a
    binary length to get an ASCII one.
    """

    @property
    def name(self) -> Literal["binary", "ascii"]:
        """``"binary"`` or ``"ascii"``. Appears verbatim in exceptions and records."""
        ...

    @property
    def width(self) -> int:
        """Wire bytes per binary byte: 1 for binary, 2 for ASCII."""
        ...

    # -- numeric fields ------------------------------------------------------------------

    def number(self, value: int, *, bits: Literal[8, 16, 32]) -> bytes:
        """``value`` as a ``bits``-wide field. The whole word-order story lives here."""
        ...

    def read_number(self, buf: bytes, off: int, *, bits: Literal[8, 16, 32]) -> int:
        """The ``bits``-wide field at ``off``. Raises rather than guessing."""
        ...

    def number_len(self, bits: Literal[8, 16, 32]) -> int:
        """Wire length of a ``bits``-wide numeric field: ``(bits // 8) * width``."""
        ...

    # -- device specification fields -----------------------------------------------------

    def device_code(self, dt: DeviceType, spec: SpecFormat) -> bytes:
        """The device code field: a numeric code in binary, a mnemonic in ASCII."""
        ...

    def device_code_len(self, spec: SpecFormat) -> int:
        """Wire length of a device code field. 1/2 binary, 2/4 ASCII."""
        ...

    def device_number(self, wire_value: int, spec: SpecFormat, *, base: int) -> bytes:
        """The device number field. 3/4 bytes binary, 6/8 digits ASCII.

        ``wire_value`` is always the LINEAR index. ``base`` is the radix its digits are
        written in and is required, keyword-only and never defaulted, because a wrong
        default here is a wrong register with end code ``0x0000``: see :class:`Notation`.
        """
        ...

    def device_number_len(self, spec: SpecFormat) -> int:
        """Wire length of a device number field. 3/4 binary, 6/8 ASCII."""
        ...

    # -- bulk data -----------------------------------------------------------------------

    def words(self, values: Sequence[int]) -> bytes:
        """Word-unit data: one 16-bit field per point."""
        ...

    def read_words(self, buf: bytes, off: int, count: int) -> tuple[int, ...]:
        """``count`` word-unit points at ``off``, as unsigned 16-bit values."""
        ...

    def word_data_len(self, count: int) -> int:
        """Wire length of ``count`` word points: ``2 * count`` / ``4 * count``."""
        ...

    def bits(self, values: Sequence[bool]) -> bytes:
        """Bit-unit data. One character per point in ASCII, one nibble in binary."""
        ...

    def read_bits(self, buf: bytes, off: int, count: int) -> tuple[bool, ...]:
        """``count`` bit-unit points at ``off``."""
        ...

    def bit_data_len(self, count: int) -> int:
        """``count`` for ASCII, ``ceil(count / 2)`` for binary. Never ``2 x`` the other."""
        ...

    # -- command-specific rules ----------------------------------------------------------

    def loopback_payload_ok(self, data: bytes) -> bool:
        """Is ``data`` a legal ``0619`` Self Test payload? 1-960 bytes of ``[0-9A-F]``."""
        ...


# ----------------------------------------------------------------------------------------
# Binary
# ----------------------------------------------------------------------------------------


class BinaryCodec:
    """Communication data code = Binary. Little-endian, one byte per byte.

    The FX5U-32MT/DS bench ran binary throughout, so every measured frame in
    ``data/vectors_hardware.jsonl`` is a check on this class.
    """

    __slots__ = ()

    @property
    def name(self) -> Literal["binary", "ascii"]:
        return "binary"

    @property
    def width(self) -> int:
        return 1

    def __repr__(self) -> str:
        return "BINARY"

    # -- numeric fields ------------------------------------------------------------------

    def number(self, value: int, *, bits: Literal[8, 16, 32]) -> bytes:
        """``value`` little-endian in ``bits // 8`` bytes.

        ``number(v, bits=32) == struct.pack("<I", v)``, so a 32-bit value is low word
        first with no word-order branch anywhere. Measured four ways on FX5U-32MT/DS fw
        1.065, 2026-09-06: writing 1234.5 as one ``1402`` double-word point put
        ``00 50 9A 44`` on the wire and read back ``D104=0x5000, D105=0x449A``.
        """
        octets = _binary_width(bits)
        _check_unsigned(value, bits, f"a {bits}-bit field")
        return value.to_bytes(octets, "little")

    def read_number(self, buf: bytes, off: int, *, bits: Literal[8, 16, 32]) -> int:
        octets = _binary_width(bits)
        raw = _span(buf, off, octets, "binary", f"a {bits}-bit field")
        return int.from_bytes(raw, "little")

    def number_len(self, bits: Literal[8, 16, 32]) -> int:
        return _binary_width(bits)

    # -- device specification fields -----------------------------------------------------

    def device_code(self, dt: DeviceType, spec: SpecFormat) -> bytes:
        """1 byte (``A8`` for D) short, 2 bytes little-endian (``A8 00``) long.

        SH(NA)-080956ENG-M pp.35-38. The two-byte order is ambiguity
        ``A-LONG-SPEC-BYTE-ORDER``; we send little-endian like every other binary field,
        and an iQ-F cannot reach it anyway (``0xC059``, measured).
        """
        if spec is SpecFormat.SHORT:
            if dt.code_short is None:
                raise SlmpCodecValueError(
                    f"binary: device {dt.name} has no 1-byte device code, so it cannot "
                    f"be addressed with the short device specification. It requires "
                    f"subcommand 0002/0003."
                )
            return bytes((dt.code_short,))
        return dt.code_long.to_bytes(2, "little")

    def device_code_len(self, spec: SpecFormat) -> int:
        return 1 if spec is SpecFormat.SHORT else 2

    def device_number(self, wire_value: int, spec: SpecFormat, *, base: int) -> bytes:
        """3 bytes little-endian short, 4 bytes long. SH(NA)-080956ENG-M pp.35-38.

        The binary field carries the linear index and nothing else: ``M1111`` is
        ``57 04 00`` (0457H = 1111) and ``D1500`` is ``DC 05 00`` (05DCH), from the
        manual's own Read Random example on p.55. ``base`` therefore does not change a
        single byte here -- it is still required so that the two codecs share one
        signature and no caller can reach the ASCII path without having decided.
        """
        _check_base(base)
        octets = self.device_number_len(spec)
        _check_unsigned(wire_value, octets * 8, "a device number")
        return wire_value.to_bytes(octets, "little")

    def device_number_len(self, spec: SpecFormat) -> int:
        return 3 if spec is SpecFormat.SHORT else 4

    # -- bulk data -----------------------------------------------------------------------

    def words(self, values: Sequence[int]) -> bytes:
        """Two little-endian bytes per point. ``T100=0x1234`` goes out as ``34 12``."""
        out = bytearray(2 * len(values))
        for index, value in enumerate(values):
            _check_unsigned(value, 16, f"word point {index}")
            out[2 * index] = value & 0xFF
            out[2 * index + 1] = value >> 8
        return bytes(out)

    def read_words(self, buf: bytes, off: int, count: int) -> tuple[int, ...]:
        _check_count(count, "word point count")
        raw = _span(buf, off, 2 * count, "binary", f"{count} word point(s)")
        return tuple(
            raw[2 * index] | (raw[2 * index + 1] << 8) for index in range(count)
        )

    def word_data_len(self, count: int) -> int:
        _check_count(count, "word point count")
        return 2 * count

    def bits(self, values: Sequence[bool]) -> bytes:
        """One nibble per point, **first point in the high nibble**, odd count padded 0.

        SH(NA)-080956ENG-M p.40 and its own worked example on p.47: M100-M107 read back
        as ``00 01 00 11``, i.e. nibbles ``0,0 / 0,1 / 0,0 / 1,1``.
        """
        out = bytearray((len(values) + 1) // 2)
        for index, value in enumerate(values):
            if value:
                out[index // 2] |= 0x10 if index % 2 == 0 else 0x01
        return bytes(out)

    def read_bits(self, buf: bytes, off: int, count: int) -> tuple[bool, ...]:
        """Unpack ``count`` nibbles, high nibble first, and refuse anything but 0 or 1.

        The padding nibble of an odd count is documented as 0 (SH(NA)-080956ENG-M p.40:
        *"0 is shown when the number of points is an odd number"*), so a non-zero pad is
        a response we do not understand and is raised rather than ignored.
        """
        _check_count(count, "bit point count")
        length = self.bit_data_len(count)
        raw = _span(buf, off, length, "binary", f"{count} bit point(s)")
        out: list[bool] = []
        for index in range(count):
            byte = raw[index // 2]
            nibble = (byte >> 4) if index % 2 == 0 else (byte & 0x0F)
            if nibble > 1:
                raise SlmpCodecValueError(
                    f"binary: bit point {index} of {count} decoded to nibble "
                    f"0x{nibble:X} in byte 0x{byte:02X} at buffer offset "
                    f"{off + index // 2}. SH(NA)-080956ENG-M p.40 documents only 0 (OFF) "
                    f"and 1 (ON). Please report this with the CPU model and firmware."
                )
            out.append(nibble == 1)
        if count % 2 and (raw[-1] & 0x0F):
            raise SlmpCodecValueError(
                f"binary: {count} bit points is an odd count, so the final low nibble "
                f"is padding and SH(NA)-080956ENG-M p.40 documents it as 0; got "
                f"0x{raw[-1] & 0x0F:X} in byte 0x{raw[-1]:02X}. Please report this with "
                f"the CPU model and firmware."
            )
        return tuple(out)

    def bit_data_len(self, count: int) -> int:
        """``ceil(count / 2)``. Two points per byte, and the odd one still costs a byte."""
        _check_count(count, "bit point count")
        return (count + 1) // 2

    # -- command-specific rules ----------------------------------------------------------

    def loopback_payload_ok(self, data: bytes) -> bool:
        return _check_loopback(data)


# ----------------------------------------------------------------------------------------
# ASCII
# ----------------------------------------------------------------------------------------


class AsciiCodec:
    """Communication data code = ASCII. Uppercase hex, most-significant digit first.

    Support both codings, default to binary: SH(NA)-080956ENG-M p.10 and JY997D56001-K
    p.12 note that binary roughly halves the byte count and therefore the transfer time,
    and ASCII's only real advantage is a readable packet capture.
    """

    __slots__ = ()

    @property
    def name(self) -> Literal["binary", "ascii"]:
        return "ascii"

    @property
    def width(self) -> int:
        return 2

    def __repr__(self) -> str:
        return "ASCII"

    # -- numeric fields ------------------------------------------------------------------

    def number(self, value: int, *, bits: Literal[8, 16, 32]) -> bytes:
        """``value`` as ``bits // 4`` uppercase hex digits, most significant first.

        ``number(v, bits=32) == f"{v:08X}".encode()``, which puts the HIGH word first --
        the exact mirror of the binary field, from the same value and the same call. The
        binary/ASCII dword flip is this one line and nothing else.
        """
        digits = 2 * _binary_width(bits)
        _check_unsigned(value, bits, f"a {bits}-bit field")
        return f"{value:0{digits}X}".encode("ascii")

    def read_number(self, buf: bytes, off: int, *, bits: Literal[8, 16, 32]) -> int:
        """Raises :class:`SlmpHexDigitError` on any character outside ``[0-9A-F]``.

        It never returns 0 for a bad nibble. ``int(b"00G8", 16)`` would raise a bare
        ``ValueError`` naming nothing; this names the character, its byte value and its
        offset in the buffer.
        """
        digits = 2 * _binary_width(bits)
        raw = _span(buf, off, digits, "ascii", f"a {bits}-bit field")
        _check_hex(raw, off, f"a {bits}-bit field")
        return int(raw, 16)

    def number_len(self, bits: Literal[8, 16, 32]) -> int:
        return 2 * _binary_width(bits)

    # -- device specification fields -----------------------------------------------------

    def device_code(self, dt: DeviceType, spec: SpecFormat) -> bytes:
        """The MNEMONIC, padded with ``*``: ``b"D*"``, ``b"TN"``, ``b"SS"``, ``b"X***"``.

        Never the hex of the binary code. ``D`` is ``b"D*"``, not ``b"A8"``
        (SH(NA)-080956ENG-M pp.35-38, and the worked write on JY997D56001-K p.75:
        ``"1401" "0000" "D*" "000100" "0003"``). The retentive timer's two-character
        mnemonics are ``SS``/``SC``/``SN``, not ``ST*`` (SH(NA)-080008-AB p.68).
        """
        return _mnemonic(dt, spec).encode("ascii")

    def device_code_len(self, spec: SpecFormat) -> int:
        return 2 if spec is SpecFormat.SHORT else 4

    def device_number(self, wire_value: int, spec: SpecFormat, *, base: int) -> bytes:
        """6 digits short, 8 long, written in ``base`` -- the DEVICE'S radix, not hex.

        ``M1111`` is ``"001111"`` and ``B1234`` is ``"001234"``, from the same
        SH(NA)-080956ENG-M p.55 Read Random request whose binary form is ``57 04 00``
        and ``34 12 00``. Rendering this field as the hexadecimal of the linear index
        reaches ``M4369`` instead of ``M1111``, and rendering a hexadecimal-radix device
        in decimal reaches ``X352`` instead of ``X0A0``; both answer ``0x0000``.

        ``base`` comes from :class:`Notation` and the device's radix, via
        ``wire/devspec.py``. It is required: there is no safe default.
        """
        conversion = _check_base(base)
        digits = self.device_number_len(spec)
        _check_unsigned(wire_value, 4 * digits, "a device number")
        if wire_value >= base**digits:
            raise SlmpCodecValueError(
                f"ascii: device number {wire_value} needs more than {digits} base-{base} "
                f"digits, so it does not fit the {spec.value} device specification "
                f"field (0..{base**digits - 1}). The binary field is wider than the "
                f"ASCII one for every radix below 16: switch to subcommand 0002/0003 "
                f"or to binary coding."
            )
        return f"{wire_value:0{digits}{conversion}}".encode("ascii")

    def device_number_len(self, spec: SpecFormat) -> int:
        return 6 if spec is SpecFormat.SHORT else 8

    # -- bulk data -----------------------------------------------------------------------

    def words(self, values: Sequence[int]) -> bytes:
        """Four uppercase hex digits per point. ``T100=0x1234`` goes out as ``"1234"``."""
        out: list[str] = []
        for index, value in enumerate(values):
            _check_unsigned(value, 16, f"word point {index}")
            out.append(f"{value:04X}")
        return "".join(out).encode("ascii")

    def read_words(self, buf: bytes, off: int, count: int) -> tuple[int, ...]:
        _check_count(count, "word point count")
        raw = _span(buf, off, 4 * count, "ascii", f"{count} word point(s)")
        _check_hex(raw, off, f"{count} word point(s)")
        return tuple(int(raw[4 * i : 4 * i + 4], 16) for i in range(count))

    def word_data_len(self, count: int) -> int:
        _check_count(count, "word point count")
        return 4 * count

    def bits(self, values: Sequence[bool]) -> bytes:
        """One character per point: ``31H`` ON, ``30H`` OFF, first point first.

        SH(NA)-080956ENG-M p.40: 5 points from M10 are ``"10101"``, five characters --
        against three bytes in binary. This is the odd-count case that breaks the 2x
        length rule.
        """
        return bytes(_ASCII_ONE if value else _ASCII_ZERO for value in values)

    def read_bits(self, buf: bytes, off: int, count: int) -> tuple[bool, ...]:
        _check_count(count, "bit point count")
        raw = _span(buf, off, count, "ascii", f"{count} bit point(s)")
        out: list[bool] = []
        for index, byte in enumerate(raw):
            if byte not in (_ASCII_ZERO, _ASCII_ONE):
                shown = chr(byte) if 0x20 <= byte <= 0x7E else f"\\x{byte:02x}"
                raise SlmpCodecValueError(
                    f"ascii: bit point {index} of {count} at buffer offset {off + index} "
                    f"is {shown!r} (0x{byte:02X}). SH(NA)-080956ENG-M p.40 documents "
                    f"only '0' (OFF) and '1' (ON). Please report this with the CPU model "
                    f"and firmware."
                )
            out.append(byte == _ASCII_ONE)
        return tuple(out)

    def bit_data_len(self, count: int) -> int:
        """``count``. One character per point -- NOT twice the binary length.

        For odd ``count`` this is ``2 * BINARY.bit_data_len(count) - 1``, and computing
        it the other way overstates the request data length. An overstated length is the
        failure that produced no response at all and no error of any kind on
        FX5U-32MT/DS fw 1.065 (measured 2026-09-06).
        """
        _check_count(count, "bit point count")
        return count

    # -- command-specific rules ----------------------------------------------------------

    def loopback_payload_ok(self, data: bytes) -> bool:
        return _check_loopback(data)


# ----------------------------------------------------------------------------------------
# The singletons
# ----------------------------------------------------------------------------------------

BINARY: Final[BinaryCodec] = BinaryCodec()
"""The binary coding. GX Works3's factory default and this library's default."""

ASCII: Final[AsciiCodec] = AsciiCodec()
"""The ASCII coding, both X/Y notations. The notation itself belongs to the profile."""

CODECS: Final[tuple[Codec, ...]] = (BINARY, ASCII)
"""Both codings, for tests and tools that must sweep every one of them."""
