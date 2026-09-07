"""The one function in the package that emits a device specification block.

Layer 0. **stdlib only.** Importing this module must not pull ``socket``, ``ssl``,
``asyncio``, ``selectors``, ``threading`` or ``logging`` into ``sys.modules``
(``tests/unit/test_layering.py`` proves it in a subprocess).

A device specification block appears in ``0401``, ``1401``, ``0403``, ``1402``, ``0406``,
``1406``, ``0801`` and ``0802`` -- eight commands, three of which repeat it once per
access point. It is four bytes long and it has **two independent ways to be silently
wrong**, so it is written once, here, and every command calls it.

**One. The field order flips between codings.**

===========  ====================================================
Binary       ``[device number 3 or 4 bytes LE][device code 1 or 2]``
ASCII        ``[device code 2 or 4 chars][device number 6 or 8 chars]``
===========  ====================================================

SH(NA)-080956ENG-M pp.45-49 and the FX5 worked example on JY997D56001-K p.75
(``"1401" "0000" "D*" "000100" "0003"``). The command catalogue calls this "the single
most common place to get it wrong", and it is invisible: a swapped ASCII block is still
eight legal characters and still reaches *a* register.

**Two. An ASCII device number is not hexadecimal.**

It is the linear index written **in the device's own radix**, so ``M1111`` goes out as
``"001111"`` while its binary field is ``57 04 00`` (0457H). The two are not
transformations of one another (SH(NA)-080956ENG-M p.55 prints the same Read Random
request in both codings). ``pymcprotocol`` 0.3.0 ``type3e.py:339`` has the mirror-image
defect -- it renders a hexadecimal-radix device number in decimal, so ``X0A0`` reaches a
different register depending on which coding the connection happens to use.

**The emit radix is not the parse radix.** This is the subtle one, and it is why
:class:`~aslmp.wire.codec.Notation` exists as a separate idea from
``profile.radix_for``. On an iQ-F the literal ``Y45`` is **octal in both ASCII modes** --
that is the profile's parse radix, and it makes the linear index 37 either way. Only the
rendering changes, and it changes with a GX Works3 connection parameter that cannot be
read off the wire (JY997D56001-K p.12: *"ASCII code (X, Y OCT): octal; ASCII code
(X, Y HEX): hexadecimal"*):

==================  ============  =========================================
``Y45`` on an iQ-F  wire          why
==================  ============  =========================================
``BINARY``          ``25 00 00``  the linear index, 37 = 0x25 (measured)
``ASCII_XY_HEX``    ``"000025"``  37 in the generic table's radix for Y (hex)
``ASCII_XY_OCT``    ``"000045"``  37 in base 8 -- the digits as GX Works3 shows
==================  ============  =========================================

So :func:`encode_device_number` takes a :class:`~aslmp.wire.codec.Notation` and derives
the base from it and from ``DeviceType.radix`` -- **never** from ``DeviceAddress.radix``,
which is the parse radix and would render every iQ-F X/Y ASCII field in octal regardless
of the connection's setting. The distinction has one test per row of that table.

``Notation`` comes from ``profile.notation_for(dt, encoding)``. It is a required
argument here with no default, for the same reason ``Codec.device_number`` requires its
``base``: a wrong value reaches a different register and the PLC answers ``0x0000``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aslmp.wire.citations import Citation
from aslmp.wire.codec import Codec, Notation, SlmpCodecValueError, SpecFormat
from aslmp.wire.devicetable import DeviceType

if TYPE_CHECKING:  # pragma: no cover - typing only
    from aslmp.wire.address import DeviceAddress

__all__ = [
    "SlmpDeviceSpecError",
    "SlmpNotationError",
    "SlmpSpecFormatError",
    "devspec_len",
    "emit_base",
    "encode_device_number",
    "encode_device_spec",
]


FIELD_ORDER: Citation = Citation(
    manual="SH(NA)-080956ENG",
    revision="M",
    section="6.1 pp.45-49",
    note=(
        "Binary sends the head device number (3 bytes, low byte first) and then the "
        "device code (1 byte); ASCII sends the device code (2 characters) and then the "
        "device number (6 characters). The order is reversed between the two codings. "
        "Confirmed for the FX5 by the worked 1401 write on JY997D56001-K p.75: "
        "'1401' '0000' 'D*' '000100' '0003'."
    ),
)
"""Why this module exists. Cited in :func:`encode_device_spec`'s refusals."""

NUMBER_IS_NOT_HEXADECIMAL: Citation = Citation(
    manual="SH(NA)-080956ENG",
    revision="M",
    section="6.4 p.55",
    note=(
        "The same Read Random request printed in both codings: M1111 is 57 04 00 in "
        "binary and '001111' in ASCII, B1234 is 34 12 00 and '001234'. The ASCII field "
        "is the linear index written in the device's own radix, not the hexadecimal of "
        "the binary field."
    ),
)

XY_NOTATION_IS_A_CONNECTION_PARAMETER: Citation = Citation(
    manual="JY997D56001",
    revision="K",
    section="4.1 p.12",
    note=(
        "'ASCII code (X, Y OCT)' renders X and Y device numbers in octal; 'ASCII code "
        "(X, Y HEX)' renders them in hexadecimal. Both parse the literal as octal; only "
        "the emitted digits differ. It is a GX Works3 own-node parameter and cannot be "
        "discovered from the wire."
    ),
)

_OCTAL_NOTATION_DEVICES: frozenset[str] = frozenset({"X", "Y"})
"""The only devices ``Notation.OCTAL_DIGITS`` may ever be applied to.

A guard, not a policy: ``profile.notation_for`` decides, and it returns ``OCTAL_DIGITS``
only for X and Y on an iQ-F under ``Encoding.ASCII_XY_OCT``. Rendering any other device
in base 8 would send ``D100`` as ``"000144"``, which is data register 100 asking for
data register 144 and being answered ``0x0000``.
"""


class SlmpDeviceSpecError(ValueError):
    """Base for a device specification block this module refuses to build.

    Local to layer 0 for the same reason ``address.SlmpAddressError`` is: ``aslmp.errors``
    imports ``aslmp.wire``, so importing it here would close a cycle the layering test
    fails the build for. ``SlmpSpecFormatError`` is wrapped as ``SlmpCapabilityError``
    and ``SlmpNotationError`` as ``SlmpEncodingNotSupportedError`` (DESIGN section 3.1).
    """


class SlmpSpecFormatError(SlmpDeviceSpecError):
    """This device cannot be addressed with the requested device specification.

    ``LTN``, ``LSTN``, ``LCN``, ``LZ`` and ``RD`` have no 1-byte device code and no
    2-character mnemonic: they exist only under subcommand ``0002``/``0003``. An
    FX5U-32MT/DS on firmware 1.065 refuses that subcommand outright with end code
    ``0xC059`` (measured 2026-09-06), so on an iQ-F these devices are unreachable and
    saying so before the frame is built is the whole point.
    """


class SlmpNotationError(SlmpDeviceSpecError):
    """The requested notation cannot be rendered in this coding, or for this device.

    ``Notation.OCTAL_DIGITS`` is a property of the ASCII *digits*; a binary device number
    is little-endian bytes and has no notation at all. DESIGN section 4.3 requires this
    combination to raise rather than be silently coerced to ``VALUE``, because a caller
    who asked for octal digits and got the binary field has a connection configured for
    something other than what they think.
    """


def encode_device_spec(
    address: DeviceAddress,
    *,
    codec: Codec,
    spec: SpecFormat,
    notation: Notation,
) -> bytes:
    """The device specification block for ``address``, in this coding.

    Binary is ``[number][code]``; ASCII is ``[code][number]``. That swap is this
    function's reason to exist, so no command anywhere else in the package concatenates
    a device code with a device number.

    Golden vectors, each from a printed page:

    =================  ======  =========================================
    Address (short)    Coding  Block
    =================  ======  =========================================
    ``M100``           binary  ``64 00 00 90``  (SH(NA)-080956ENG-M p.47)
    ``T100``           binary  ``64 00 00 C2``  (p.49)
    ``M1111``          binary  ``57 04 00 90``  (p.55)
    ``M1111``          ASCII   ``"M*001111"``   (p.55)
    ``D100``           ASCII   ``"D*000100"``   (JY997D56001-K p.75)
    ``Y45`` on iQ-F    binary  ``25 00 00 9D``  (measured: index 37)
    =================  ======  =========================================
    """
    number = encode_device_number(address, codec=codec, spec=spec, notation=notation)
    code = _device_code(address.type, codec=codec, spec=spec)
    if codec.name == "binary":
        return number + code
    return code + number


def encode_device_number(
    address: DeviceAddress,
    *,
    codec: Codec,
    spec: SpecFormat,
    notation: Notation,
) -> bytes:
    """Just the device number field: 3 or 4 bytes binary, 6 or 8 characters ASCII.

    Exposed separately because the ``008x`` device-memory-extension block interleaves the
    number and the code with two extension fields (SH(NA)-080956ENG-M appendix 1
    pp.207-216), and because a diagnostic wants to print one field at a time. The base is
    derived here and nowhere else.
    """
    base = emit_base(address.type, notation, codec=codec)
    return codec.device_number(address.index, spec, base=base)


def emit_base(dt: DeviceType, notation: Notation, *, codec: Codec) -> int:
    """The base an ASCII device number's digits are written in, or raise.

    ``Notation.VALUE`` means the **device table's own** radix -- decimal for D, M, T, C,
    S, Z, R, SM, SD; hexadecimal for X, Y, B, W, SB, SW, DX, DY, ZR. Not
    ``DeviceAddress.radix``, which is the radix the literal was *parsed* in: on an iQ-F
    those differ for X and Y, deliberately, and conflating them renders every X/Y ASCII
    field in octal on a connection configured for ``ASCII_XY_HEX``.
    """
    if not isinstance(notation, Notation):
        raise TypeError(
            f"notation must be a Notation from profile.notation_for(), not "
            f"{type(notation).__name__}"
        )
    if notation is Notation.VALUE:
        return int(dt.radix)
    if codec.name != "ascii":
        raise SlmpNotationError(
            f"Notation.OCTAL_DIGITS was requested for {dt.name} in {codec.name} coding. "
            f"A binary device number is the linear index as little-endian bytes and has "
            f"no notation: {dt.name}45 on an iQ-F is 25 00 00 whatever the ASCII X/Y "
            f"setting is. Octal digits exist only under Encoding.ASCII_XY_OCT "
            f"({XY_NOTATION_IS_A_CONNECTION_PARAMETER.reference}). Nothing here coerces "
            f"this to Notation.VALUE."
        )
    if dt.name not in _OCTAL_NOTATION_DEVICES:
        raise SlmpNotationError(
            f"Notation.OCTAL_DIGITS was requested for {dt.name} ({dt.long_name}). Only "
            f"X and Y are rendered in octal, and only on an iQ-F under Encoding."
            f"ASCII_XY_OCT ({XY_NOTATION_IS_A_CONNECTION_PARAMETER.reference}); every "
            f"other device keeps the radix of the generic device table, which is "
            f"{dt.radix.name.lower()} for {dt.name}. Rendering {dt.name}100 in base 8 "
            f"would send '000144' and reach {dt.name}144 with end code 0x0000."
        )
    fixed = notation.fixed_base
    if fixed is None:  # pragma: no cover - Notation has exactly two members
        raise SlmpNotationError(
            f"Notation.{notation.name} declares no fixed base; wire/codec.py and "
            f"wire/devspec.py disagree about how many notations exist."
        )
    return fixed


def devspec_len(codec: Codec, spec: SpecFormat) -> int:
    """Wire length of one device specification block. 4/6 binary, 8/12 ASCII.

    Order-independent, which is why the length can be shared by both codings while the
    bytes cannot. ``payload_len`` uses this so that ``L`` is filled without building a
    throwaway buffer -- and an overstated ``L`` is the one failure on this hardware that
    produces no response at all (FX5U-32MT/DS fw 1.065, measured 2026-09-06).
    """
    return codec.device_code_len(spec) + codec.device_number_len(spec)


def _device_code(dt: DeviceType, *, codec: Codec, spec: SpecFormat) -> bytes:
    """The device code field, refusing a device the specification cannot express.

    ``Codec.device_code`` already raises for these, but it raises about a missing table
    entry. This says which subcommand would reach the device and, on an iQ-F, that no
    subcommand will.
    """
    if spec is SpecFormat.SHORT and dt.min_spec is SpecFormat.LONG:
        raise SlmpSpecFormatError(
            f"{dt.name} ({dt.long_name}) has no 1-byte device code and no 2-character "
            f"mnemonic, so it cannot be addressed with the short device specification. "
            f"It needs subcommand 0002/0003 (SpecFormat.LONG), which an FX5U-32MT/DS on "
            f"firmware 1.065 refuses with end code 0xC059 (measured 2026-09-06): on an "
            f"iQ-F this device is unreachable, not merely differently addressed."
        )
    try:
        return codec.device_code(dt, spec)
    except SlmpCodecValueError as exc:
        raise SlmpSpecFormatError(
            f"{dt.name} ({dt.long_name}) has no {spec.value} device code in "
            f"{codec.name} coding: {exc}"
        ) from exc
