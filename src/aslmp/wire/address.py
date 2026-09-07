"""Device addresses: the text a person writes, and the number that goes on the wire.

Layer 0. **stdlib only.** Importing this module must not pull ``socket``, ``ssl``,
``asyncio``, ``selectors``, ``threading`` or ``logging`` into ``sys.modules``
(``tests/unit/test_layering.py`` proves it in a subprocess).

**Why this module is the most important 300 lines in the library.**

GX Works3 numbers an FX5's inputs and outputs in **octal**, and the device-number field
on the wire carries the **value** of that literal, never its digits. Measured on
FX5U-32MT/DS firmware 1.065 on 2026-09-07: a bit-unit write at wire device number 16
(``0x10``) lit the seventeenth output, which GX Works3 calls ``Y20``.

======  =================  ==================
Written  what it means      wire device number
======  =================  ==================
``Y0``   1st output         0
``Y7``   8th output         7
``Y10``  9th output         **8**
``Y17``  16th output        15
``Y20``  17th output        **16**
======  =================  ==================

A client that sends the digits as written puts ``Y10`` on the wire as 10, which is the
**eleventh** output: off by two, end code ``0x0000``, and no error anywhere. The error
grows with the address -- ``Y70`` sent as 0x70 is off by 56 -- and it moves a physical
output on a machine. Every X/Y address in this library therefore round-trips through an
anchored octal parse, and :func:`parse_address` is the only door.

The same bench also showed that **the PLC will not catch a bad literal for you**: a
write at wire number 8 for ``Y`` -- which is what ``Y8`` would have to mean, and ``Y8``
is not a legal octal address at all -- was accepted and answered ``0x0000``. Rejecting
``Y8`` and ``Y9`` is the client's job and nothing else will do it. (This is the second
measured case of that CPU accepting a documented-illegal address; the first was a ``TS``
point in a Read Random.)

**The radix belongs to the PROFILE, not to the letter.** X and Y are octal on iQ-F and
hexadecimal on iQ-R (SH(NA)-080956ENG-M pp.35-36 gives the generic table; JY997D56001-K
p.12 gives the iQ-F override). ``X1F`` is a legal iQ-R address for 31 and a parse error
on an iQ-F; ``X17`` is 23 on an iQ-R and 15 on an FX5U. Nothing here reads a radix off
the device letter, and there is no default profile: DESIGN section 0.3 deletes the
generic profile precisely so that an unrecognised CPU can never silently become
hexadecimal.

**Two questions, never conflated** (DESIGN section 4.3):

1. *Parsing* -- this module. Longest-prefix device match, then a **fully anchored** parse
   of the remaining characters in the radix ``profile.radix_for(dt)`` gives. Never
   truncates, never guesses, never returns a partial address.
2. *Wire notation* -- ``wire/devspec.py``. The emit radix of an ASCII device number is a
   different question with a different answer: on an iQ-F under ``ASCII_XY_OCT`` the
   already-parsed ``Y45`` (index 37) is rendered back into octal digits as ``"000045"``,
   while under ``ASCII_XY_HEX`` the same point is ``"000025"``. In binary it is
   ``25 00 00`` either way.

**Errors.** ``aslmp.errors`` is layer 0.5 and imports ``aslmp.wire``, so this module
cannot import it without closing a cycle that ``tests/unit/test_layering.py`` fails the
build for. The refusals here are therefore local, typed and ``ValueError``-derived, and
the public hierarchy of DESIGN section 3 wraps them one layer up:
:class:`SlmpAddressTextError` becomes ``SlmpAddressSyntaxError``,
:class:`SlmpUnknownPrefixError` becomes ``SlmpUnknownDeviceError``,
:class:`SlmpRadixDigitError` becomes ``SlmpDeviceRadixError`` and
:class:`SlmpDeviceIndexError` becomes ``SlmpAddressRangeError``. Every one of them
carries the offending text, and the radix error additionally carries the character, its
position, the radix and the device, so the wrapper does not have to re-parse a message.

**Case.** A device literal is a human-facing string, not a wire field: ``d0`` and ``x1f``
are accepted and mean ``D0`` and ``X1F``. That is deliberately the opposite of
``Codec.read_number``, which refuses lower case in an ASCII *wire* field because
SH(NA)-080956ENG-M pp.39-41 say to use capitalised code and no manual says what a PLC
does with anything else. Nothing about accepting ``d0`` changes a byte we emit; the byte
is the index, and :attr:`DeviceAddress.text` renders the canonical upper-case form.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final, Protocol

from aslmp.wire.citations import Citation, Measurement
from aslmp.wire.codec import BINARY, SpecFormat
from aslmp.wire.devicetable import DEVICE_TABLE, PREFIXES_LONGEST_FIRST, DeviceType, Radix

__all__ = [
    "MAX_DEVICE_INDEX",
    "XY_IS_THE_LINEAR_INDEX",
    "XY_OCTAL_IS_NOT_VALIDATED_BY_THE_PLC",
    "AddressProfile",
    "DeviceAddress",
    "SlmpAddressError",
    "SlmpAddressTextError",
    "SlmpDeviceIndexError",
    "SlmpRadixDigitError",
    "SlmpUnknownPrefixError",
    "format_address",
    "parse_address",
]


# ----------------------------------------------------------------------------------------
# Provenance
# ----------------------------------------------------------------------------------------

XY_IS_THE_LINEAR_INDEX: Final = Measurement(
    cpu="FX5U-32MT/DS",
    firmware="1.065",
    date="2026-09-07",
    note=(
        "Y0..Y63 cleared, then one bit set at a chosen WIRE device number and the whole "
        "block read back: wire 0 lit linear output 0, wire 8 lit linear output 8, wire "
        "16 (0x10) lit linear output 16 -- which GX Works3 shows as Y20. The device "
        "number field carries the VALUE of the octal literal, not its digits. Binary, "
        "3E, TCP, device code 0x9D."
    ),
)
"""X/Y wire numbering, settled on the bench. See the module docstring."""

XY_OCTAL_IS_NOT_VALIDATED_BY_THE_PLC: Final = Measurement(
    cpu="FX5U-32MT/DS",
    firmware="1.065",
    date="2026-09-07",
    note=(
        "A bit-unit write at wire device number 8 for Y -- the number the illegal "
        "literal Y8 would have to mean -- was ACCEPTED and answered end code 0x0000. "
        "The CPU does not enforce octal legality, so refusing the digits 8 and 9 in an "
        "iQ-F X/Y literal is client-side validation that nothing else performs."
    ),
)
"""Why :func:`parse_address` refuses ``Y8``: because the PLC does not."""

_XY_RADIX_IS_A_PROFILE_FACT: Final = Citation(
    manual="JY997D56001",
    revision="K",
    section="4.1 p.12",
    note=(
        "'ASCII code (X, Y OCT): octal; ASCII code (X, Y HEX): hexadecimal'. The iQ-F "
        "numbers X and Y in octal where SH(NA)-080956ENG-M pp.35-36 gives hexadecimal "
        "for the generic device table, which is why the radix is resolved through the "
        "profile and never read off the device letter."
    ),
)

_DEVICE_NUMBER_FIELD_WIDTHS: Final = Citation(
    manual="SH(NA)-080956ENG",
    revision="M",
    section="5.2 pp.35-38",
    note=(
        "The device number field is 3 bytes with the short device specification and 4 "
        "with the long one, so no device number wider than 32 bits can be expressed in "
        "any frame this library builds."
    ),
)


# ----------------------------------------------------------------------------------------
# Limits
# ----------------------------------------------------------------------------------------

MAX_DEVICE_INDEX: Final[int] = (1 << (8 * BINARY.device_number_len(SpecFormat.LONG))) - 1
"""The widest device number any SLMP frame can carry: ``0xFFFFFFFF``.

Derived from the codec rather than typed as a constant, so the two cannot drift apart.
An index above this is refused here; whether a given CPU actually *has* that device
number is ``profile.check_range``, which validates the whole span and not just the
start (DESIGN section 4.3). See ``_DEVICE_NUMBER_FIELD_WIDTHS``.
"""

_MAX_DIGITS: Final = 12
"""Longest digit run accepted before the radix conversion is even attempted.

``0xFFFFFFFF`` is 11 octal digits, so 12 is generous. The guard exists so that a
pathological literal -- a megabyte of digits pasted into a config file -- becomes a
typed refusal naming the text instead of an arbitrarily large integer conversion.
"""

_DIGITS: Final[dict[Radix, str]] = {
    Radix.OCTAL: "01234567",
    Radix.DECIMAL: "0123456789",
    Radix.HEXADECIMAL: "0123456789ABCDEF",
}

_CONVERSION: Final[dict[Radix, str]] = {
    Radix.OCTAL: "o",
    Radix.DECIMAL: "d",
    Radix.HEXADECIMAL: "X",
}

_RADIX_NAME: Final[dict[Radix, str]] = {
    Radix.OCTAL: "octal",
    Radix.DECIMAL: "decimal",
    Radix.HEXADECIMAL: "hexadecimal",
}

_AN: Final[dict[Radix, str]] = {
    Radix.OCTAL: "an octal",
    Radix.DECIMAL: "a decimal",
    Radix.HEXADECIMAL: "a hexadecimal",
}

_OCTAL_ON_IQ_F: Final[frozenset[str]] = frozenset({"X", "Y"})
"""The two devices whose radix an iQ-F profile overrides, named only to write a better
error message. The override itself lives in the profile (JY997D56001-K p.12), never
here: this module reads ``profile.radix_for`` and does not know what family it is on."""


# ----------------------------------------------------------------------------------------
# Errors
# ----------------------------------------------------------------------------------------


class SlmpAddressError(ValueError):
    """Base for every literal this module refuses to turn into an address.

    Also a ``ValueError`` for the reason ``SlmpUsageError`` is (DESIGN section 3.2): an
    existing ``except ValueError`` keeps working, and the alternative is people writing
    ``except Exception``. ``aslmp.errors`` wraps these into the public hierarchy; see the
    module docstring for the mapping.
    """

    def __init__(self, message: str, *, text: str) -> None:
        super().__init__(message)
        self.text = text
        """The literal exactly as it was handed to :func:`parse_address`."""


class SlmpAddressTextError(SlmpAddressError):
    """The literal is not shaped like an address at all.

    Empty, all whitespace, a character that is neither a letter nor a digit (``D100.5``,
    ``D-1``, ``D 100``), or a device with no number after it (``D``). Wrapped as
    ``SlmpAddressSyntaxError``.
    """


class SlmpUnknownPrefixError(SlmpAddressError):
    """No device family matches the leading letters. Wrapped as ``SlmpUnknownDeviceError``.

    Matching is longest-prefix, so this is not "the first letter is unknown": ``SB0`` is
    link special relay 0 and not step relay ``B0``, and a parser that matched the
    shortest prefix would reach a different register with end code ``0x0000``.
    """


class SlmpRadixDigitError(SlmpAddressError):
    """A character in the device number is not a digit in this device's radix.

    ``Y8`` and ``X1F`` on an iQ-F, ``XFFG`` anywhere, ``D100x`` -- every one of them
    names the character, its position in the literal, the radix and the device, so that
    the wrapper in ``aslmp.errors`` never has to re-parse a message. Wrapped as
    ``SlmpDeviceRadixError``.
    """

    def __init__(
        self,
        message: str,
        *,
        text: str,
        device: str,
        character: str,
        position: int,
        radix: Radix,
    ) -> None:
        super().__init__(message, text=text)
        self.device = device
        """The device family name the prefix matched, e.g. ``"Y"``."""
        self.character = character
        """The offending character, upper-cased as it was compared."""
        self.position = position
        """Its 0-based index in the stripped literal."""
        self.radix = radix
        """The radix the profile gives this device on this CPU."""


class SlmpDeviceIndexError(SlmpAddressError):
    """The device number is negative, or wider than any device number field.

    Not a range check: whether *this* CPU has ``D8000`` is ``profile.check_range``, which
    takes the span and not the start. This is the arithmetic floor and ceiling of the
    wire field itself. Wrapped as ``SlmpAddressRangeError``.
    """


# ----------------------------------------------------------------------------------------
# The profile, structurally
# ----------------------------------------------------------------------------------------


class AddressProfile(Protocol):
    """The two things address parsing needs from a CPU profile, and nothing else.

    ``aslmp.profile.CpuProfile`` (layer 1, build unit U6) satisfies this structurally --
    it has a ``key`` field and a ``radix_for`` method with exactly this signature -- and
    a structural protocol is what keeps layer 0 from importing layer 1. Nothing here
    imports a profile module, and no profile has to know this protocol exists.

    Deliberately minimal. Device *existence* on a CPU (``V``, ``ZR``, ``DX``, ``DY`` are
    absent on an iQ-F and answer ``0xC05C``) and device *ranges* (``D7999`` is the last
    data register) are the profile's ``check_range``, not this module's: DESIGN section
    4.3 separates the two questions, and duplicating the second one here would give two
    places to disagree about it.
    """

    @property
    def key(self) -> str:
        """The profile identity, e.g. ``"melsec:iq-f/fx5u"``. Quoted in every refusal."""
        ...

    def radix_for(self, dt: DeviceType) -> Radix:
        """The radix a device number is WRITTEN in on this CPU.

        The generic table's ``dt.radix`` for every device except X and Y on an iQ-F,
        where JY997D56001-K p.12 makes it octal.
        """
        ...


# ----------------------------------------------------------------------------------------
# The address
# ----------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DeviceAddress:
    """One device point: a family, a linear index, and how it was written.

    ``index`` is the number that goes on the wire, always, in every coding and every
    frame -- ``Y20`` on an iQ-F is ``index=16``. ``radix`` is how that index is written
    for a human on this CPU, which is what makes the rendering reversible: without it,
    ``DeviceAddress(Y, 16)`` would print as ``Y16`` and re-parse as 14.

    ``source`` keeps the caller's own literal so that an exception quotes what they
    typed rather than a normalisation of it. The constructor **verifies** that the source
    really denotes this index in this radix, so an address cannot lie about where it came
    from; build one with :meth:`of` rather than hand-writing the pair.

    Frozen and slotted: an address is passed into a prebuilt plan and held for the life
    of a control loop, and a mutable one would let a bind-time validation be undone at
    run time.

    **Equality and hashing are wire identity: the family and the index.** ``source`` and
    ``radix`` are excluded, because ``D0`` and ``D00`` are one data register and
    ``Y20``-as-octal and ``Y10``-as-hexadecimal are one output. A block plan checking for
    duplicate or overlapping points is asking "does this reach the same register", and if
    two spellings compared unequal it would answer no. Rendering is still exact --
    :attr:`text` and :meth:`__str__` differ where the spellings differ -- it is only
    ``==`` that is about the bytes.
    """

    type: DeviceType
    index: int
    source: str = field(compare=False)
    radix: Radix = field(compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.type, DeviceType):
            raise TypeError(
                f"DeviceAddress.type must be a DeviceType from DEVICE_TABLE, not "
                f"{type(self.type).__name__}"
            )
        if not isinstance(self.radix, Radix):
            raise TypeError(
                f"DeviceAddress.radix must be a Radix, not {type(self.radix).__name__}"
            )
        if not isinstance(self.index, int) or isinstance(self.index, bool):
            raise TypeError(
                f"DeviceAddress.index must be an int, not {type(self.index).__name__}"
            )
        if not isinstance(self.source, str):
            raise TypeError(
                f"DeviceAddress.source must be a str, not {type(self.source).__name__}"
            )
        _check_index(self.index, self.source)
        _verify_source(self.type, self.index, self.source, self.radix)

    @classmethod
    def of(cls, dt: DeviceType, index: int, *, radix: Radix) -> DeviceAddress:
        """Build an address from an index, rendering ``source`` in ``radix``.

        The programmatic door, for span arithmetic and generated plans. ``radix`` is
        required and has no default: it comes from ``profile.radix_for(dt)``, and a
        default of ``dt.radix`` would render an iQ-F ``Y`` point in hexadecimal, which
        prints wrong in every diagnostic and re-parses as a different output.
        """
        if not isinstance(dt, DeviceType):
            raise TypeError(f"dt must be a DeviceType, not {type(dt).__name__}")
        if not isinstance(radix, Radix):
            raise TypeError(f"radix must be a Radix, not {type(radix).__name__}")
        if not isinstance(index, int) or isinstance(index, bool):
            raise TypeError(f"index must be an int, not {type(index).__name__}")
        _check_index(index, f"{dt.name}<{index}>")
        return cls(dt, index, _render(dt, index, radix), radix)

    def offset(self, delta: int, /) -> DeviceAddress:
        """The point ``delta`` device numbers further on, same family and radix.

        The second word of a double-word access point, the next element of a batch, the
        anchor of a folded bit window. Raises rather than wrapping or clamping if the
        result leaves the field: ``slmp-rs`` computes a point count as ``as u8`` and
        wraps 256 to 0, which is the same class of bug one field over.
        """
        if not isinstance(delta, int) or isinstance(delta, bool):
            raise TypeError(f"delta must be an int, not {type(delta).__name__}")
        moved = self.index + delta
        if moved < 0:
            raise SlmpDeviceIndexError(
                f"{self} offset by {delta} is device number {moved}, which is before "
                f"the first {self.type.name} device. Nothing here clamps to 0.",
                text=self.source,
            )
        return DeviceAddress.of(self.type, moved, radix=self.radix)

    @property
    def text(self) -> str:
        """The canonical rendering: the family name and the index in ``radix``.

        ``DeviceAddress.of(Y, 16, radix=Radix.OCTAL).text`` is ``"Y20"`` -- what GX
        Works3 shows -- while ``.index`` stays 16, which is what the wire carries.
        """
        return _render(self.type, self.index, self.radix)

    def __str__(self) -> str:
        return self.source


def format_address(address: DeviceAddress, /) -> str:
    """``address`` as a person would write it on this CPU: ``"Y20"``, not ``"Y16"``.

    The inverse of :func:`parse_address` under the same profile, which is asserted as a
    round-trip property test over every device family and radix.
    """
    if not isinstance(address, DeviceAddress):
        raise TypeError(f"expected a DeviceAddress, not {type(address).__name__}")
    return address.text


# ----------------------------------------------------------------------------------------
# Parsing
# ----------------------------------------------------------------------------------------


def parse_address(text: str, profile: AddressProfile) -> DeviceAddress:
    """``"Y20"`` on an iQ-F -> ``DeviceAddress(Y, index=16)``. The only door.

    Longest-prefix device match, then a **fully anchored** parse of every remaining
    character in ``profile.radix_for(dt)``. There is no partial success: either the whole
    literal is an address on this CPU, or a typed refusal names the character that is
    not. ``int(digits, base)`` is reached only after each character has been checked
    individually, because ``int`` accepts ``"1_0"``, ``" 10 "`` and ``"+10"`` and this
    function accepts none of them.

    Examples that the design turns on::

        parse_address("Y20",  FX5U).index == 16     # octal literal, linear wire number
        parse_address("X1F",  IQ_R).index == 31     # hexadecimal on iQ-R
        parse_address("X1F",  FX5U)                 # SlmpRadixDigitError
        parse_address("Y8",   FX5U)                 # SlmpRadixDigitError -- the PLC accepts it
        parse_address("XFFF", IQ_R).index == 4095   # and X0FFF is the same address
        parse_address("D100x", FX5U)                # SlmpRadixDigitError naming 'X' at 4
        parse_address("D0",   FX5U).index == 0      # D0, M0 and X0 are ordinary addresses

    That last line is a regression test with a name: ``PySLMPClient``'s
    ``assert 0 < start_num < 0xFFF`` makes ``D0``, ``M0`` and ``X0`` unreadable when it
    runs and validates nothing when Python is run with ``-O``.
    """
    if not isinstance(text, str):
        raise TypeError(
            f"a device address is a str such as 'D100' or 'Y20', not "
            f"{type(text).__name__}"
        )
    literal = text.strip()
    if not literal:
        raise SlmpAddressTextError(
            f"a device address must not be blank; got {text!r}. Write the device family "
            f"and its number, e.g. 'D100', 'M0' or 'Y20'.",
            text=text,
        )
    upper = literal.upper()
    _check_characters(upper, literal)
    dt = _match_prefix(upper, literal)
    digits = upper[len(dt.name) :]
    if not digits:
        raise SlmpAddressTextError(
            f"{literal!r} names the device family {dt.name} ({dt.long_name}) with no "
            f"device number after it. The first one is {dt.name}0.",
            text=text,
        )
    radix = _radix_for(profile, dt)
    index = _parse_digits(digits, dt, radix, literal, profile)
    return DeviceAddress(dt, index, literal, radix)


def _check_characters(upper: str, literal: str) -> None:
    """Every character must be an ASCII letter or digit. Names the first that is not."""
    for position, character in enumerate(upper):
        if not ("0" <= character <= "9" or "A" <= character <= "Z"):
            shown = character if character.isprintable() else f"\\x{ord(character):02x}"
            raise SlmpAddressTextError(
                f"{literal!r} contains {shown!r} at position {position}. A device "
                f"address is a device family followed by its number and nothing else: "
                f"no separators, no sign, no bit suffix, no whitespace inside.",
                text=literal,
            )


def _match_prefix(upper: str, literal: str) -> DeviceType:
    """The longest device family name that starts ``upper``, or raise.

    Longest first is load-bearing: ``SB``, ``SW``, ``SD``, ``SM``, ``STS``, ``LCN`` and
    their relatives all begin with a letter that is itself a device, so a parser that
    took the shortest match would read ``SB0`` as step relay ``B0`` -- a different
    register, answered ``0x0000``.
    """
    for prefix in PREFIXES_LONGEST_FIRST:
        if upper.startswith(prefix):
            return DEVICE_TABLE[prefix]
    letters = "".join(c for c in upper if not c.isdigit())
    raise SlmpUnknownPrefixError(
        f"{literal!r} does not start with a device family this library knows. Its "
        f"leading letters are {letters!r}; the families are "
        f"{', '.join(sorted(DEVICE_TABLE))}. Names are matched longest first, so SB0 is "
        f"link special relay 0 and never step relay B0.",
        text=literal,
    )


def _radix_for(profile: AddressProfile, dt: DeviceType) -> Radix:
    """``profile.radix_for(dt)``, refusing anything that is not a :class:`Radix`.

    The profile is a structural protocol, so it may be a stub, a test double or a
    ``replace``d profile from another version of this package. A radix that is not one
    of the three is the one thing that would silently address the wrong register, so it
    is checked here rather than trusted.
    """
    radix = profile.radix_for(dt)
    if not isinstance(radix, Radix):
        raise TypeError(
            f"{type(profile).__name__}.radix_for({dt.name}) returned "
            f"{radix!r}, which is not a Radix. The radix decides which register a "
            f"literal reaches and is never inferred from the device letter."
        )
    return radix


def _parse_digits(
    digits: str,
    dt: DeviceType,
    radix: Radix,
    literal: str,
    profile: AddressProfile,
) -> int:
    """The anchored radix parse. Every character, or a named refusal."""
    if len(digits) > _MAX_DIGITS:
        raise SlmpDeviceIndexError(
            f"{literal!r} has {len(digits)} digits in its device number. No SLMP device "
            f"number field is wider than 32 bits (0..{MAX_DEVICE_INDEX}), which is at "
            f"most 11 digits in any radix.",
            text=literal,
        )
    allowed = _DIGITS[radix]
    offset = len(dt.name)
    for position, character in enumerate(digits):
        if character not in allowed:
            raise SlmpRadixDigitError(
                _radix_message(character, offset + position, dt, radix, literal, profile),
                text=literal,
                device=dt.name,
                character=character,
                position=offset + position,
                radix=radix,
            )
    index = int(digits, radix.value)
    _check_index(index, literal)
    return index


def _radix_message(
    character: str,
    position: int,
    dt: DeviceType,
    radix: Radix,
    literal: str,
    profile: AddressProfile,
) -> str:
    """The sentence a person needs to fix their address, with the evidence for it."""
    named = _RADIX_NAME[radix]
    head = (
        f"{literal!r}: {character!r} at position {position} is not "
        f"{_AN[radix]} digit, and {dt.name} is written in {named} on profile "
        f"{profile.key!r}."
    )
    if radix is Radix.OCTAL and character in "89":
        measured = XY_OCTAL_IS_NOT_VALIDATED_BY_THE_PLC
        return (
            f"{head} GX Works3 numbers X and Y in octal, so there is no {dt.name}8 and "
            f"no {dt.name}9: after {dt.name}7 comes {dt.name}10 (wire number 8) and "
            f"after {dt.name}17 comes {dt.name}20 (wire number 16). This has to be "
            f"refused here, because the CPU does not refuse it -- "
            f"{measured.reference}: {measured.note}"
        )
    if radix is not Radix.HEXADECIMAL and character in "ABCDEF":
        where = (
            "an iQ-R, where X and Y are hexadecimal"
            if dt.name in _OCTAL_ON_IQ_F
            else f"a CPU whose {dt.name} device is hexadecimal"
        )
        return (
            f"{head} {literal!r} would be a legal address on {where}. The radix belongs "
            f"to the profile and not to the device letter "
            f"({_XY_RADIX_IS_A_PROFILE_FACT.reference}), so the same text means "
            f"different registers on different families and is never guessed."
        )
    return f"{head} The digits of {_AN[radix]} device number are {_DIGITS[radix]!r}."


def _check_index(index: int, text: str) -> None:
    """The arithmetic floor and ceiling of the device number field itself."""
    if index < 0:
        raise SlmpDeviceIndexError(
            f"{text!r} is device number {index}. A device number is not signed; there "
            f"is no device before the first one.",
            text=text,
        )
    if index > MAX_DEVICE_INDEX:
        raise SlmpDeviceIndexError(
            f"{text!r} is device number {index}, which does not fit any SLMP device "
            f"number field (0..{MAX_DEVICE_INDEX}, {_DEVICE_NUMBER_FIELD_WIDTHS}).",
            text=text,
        )


def _render(dt: DeviceType, index: int, radix: Radix) -> str:
    """``(Y, 16, OCTAL)`` -> ``"Y20"``. No padding: GX Works3 shows Y20, not Y0020."""
    return f"{dt.name}{index:{_CONVERSION[radix]}}"


def _verify_source(dt: DeviceType, index: int, source: str, radix: Radix) -> None:
    """Refuse a :class:`DeviceAddress` whose text does not denote its own index.

    Cheap, and it makes the type unforgeable: every diagnostic in the library prints
    ``source``, every frame carries ``index``, and an address that could hold ``"Y20"``
    beside ``index=20`` would make the one bug this module exists to prevent invisible
    in exactly the place a person would go looking for it.
    """
    upper = source.strip().upper()
    if not upper.startswith(dt.name):
        raise SlmpAddressTextError(
            f"DeviceAddress source {source!r} does not start with its own device "
            f"family {dt.name}. Use DeviceAddress.of() or parse_address().",
            text=source,
        )
    digits = upper[len(dt.name) :]
    allowed = _DIGITS[radix]
    if not digits or any(character not in allowed for character in digits):
        raise SlmpAddressTextError(
            f"DeviceAddress source {source!r} is not a {_RADIX_NAME[radix]} "
            f"{dt.name} address. Use DeviceAddress.of() or parse_address().",
            text=source,
        )
    if int(digits, radix.value) != index:
        raise SlmpAddressTextError(
            f"DeviceAddress source {source!r} is device number "
            f"{int(digits, radix.value)} in {_RADIX_NAME[radix]}, but the address "
            f"carries index={index}. An address may not disagree with its own text: "
            f"that is the {dt.name}20-is-16 bug wearing a disguise.",
            text=source,
        )
