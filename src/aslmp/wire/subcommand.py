"""The subcommand, derived from what the request actually is.

Layer 0. **stdlib only.** Importing this module must not pull ``socket``, ``ssl``,
``asyncio``, ``selectors``, ``threading`` or ``logging`` into ``sys.modules``
(``tests/unit/test_layering.py`` proves it in a subprocess).

The SLMP reference writes subcommands as ``00□X`` and prints them as a list of magic
numbers. The FX5 manual gives the scheme away: JY997D56001-K p.69 ("Device Read (Batch)
- Subcommand") is an explicit three-column table, and it decodes as a bit field.

===========  ==============================================  =========================
Bit          Set                                             Clear
===========  ==============================================  =========================
``0x0001``   bit units, 1 point = 1 bit                      word units, 1 point = 1 word
``0x0002``   long device spec: 2-byte code, 4-byte number    short: 1-byte, 3-byte
``0x0080``   device memory extension specification in use    plain device access
===========  ==============================================  =========================

So the everyday four are ``0000`` word/short, ``0001`` bit/short, ``0002`` word/long,
``0003`` bit/long, and their extension twins ``0080``/``0081``/``0082``/``0083``.

**Derived, never a literal.** A subcommand written as a constant at a call site is a
constant that can disagree with the request beside it, and the failure is not a crash:
``0401`` with subcommand ``0000`` and a bit device returns word-packed data that decodes
into plausible booleans. Every command in this package asks this function, which takes
the three facts that *are* the subcommand and can therefore never disagree with them.
The three arguments have three distinct types, so a call site cannot silently swap them.

**What this function does not decide.** Whether the CPU accepts the answer is the
profile's business:

* SH(NA)-080956ENG-M p.35 says to use ``001□``/``000□`` for Q and L and ``003□``/``002□``
  for iQ-R and iQ-L.
* The FX5 manual's subcommand tables offer only ``0000, 0001, 0080, 0081, 0082, 0083``
  for every device command -- bare ``0002``/``0003`` is never listed -- and an
  FX5U-32MT/DS on firmware 1.065 answered ``0xC059`` to subcommand ``0x0002``
  (measured 2026-09-06). ``SpecFormat.LONG`` is therefore a capability the iQ-F profile
  refuses, not a value this function withholds.
* ``0403`` Read Random has **no** bit-unit variant at all (SH(NA)-080956ENG-M pp.53-56);
  ``commands/random.py`` never passes ``Unit.BIT``.
* The FX5 restricts the extension specification to the 3E frame and to the module access
  device (JY997D56001-K appendix). No block encoder for the ``008□`` layout ships in
  ``wire/devspec.py`` yet, so nothing in this package passes ``extension=True``; the bit
  is implemented because leaving a hole in a bit field is how the hole gets filled with
  a literal later.
"""

from __future__ import annotations

from typing import Final

from aslmp.wire.citations import Citation
from aslmp.wire.codec import SlmpCodecValueError, SpecFormat, Unit

__all__ = [
    "BIT_UNITS",
    "EXTENSION",
    "LONG_SPEC",
    "SUBCOMMAND_BITS",
    "decode_subcommand",
    "subcommand",
]


BIT_UNITS: Final = 0x0001
"""One point is one bit rather than one 16-bit word."""

LONG_SPEC: Final = 0x0002
"""The "4 digit code / 8 digit number" device specification: 2-byte code, 4-byte number."""

EXTENSION: Final = 0x0080
"""A device memory extension specification (``J□\\``, ``U□\\G``, ``U3E0\\G``) is in use."""

SUBCOMMAND_BITS: Final = BIT_UNITS | LONG_SPEC | EXTENSION
"""Every bit this library knows how to set. A response carrying any other is unparsed."""

BIT_FIELD: Final = Citation(
    manual="JY997D56001",
    revision="K",
    section="4.3 p.69",
    note=(
        "'Device Read (Batch), Subcommand' prints the three-column table that decodes "
        "the whole scheme: 0x0001 selects bit units over word units, 0x0002 selects the "
        "4-digit-code / 8-digit-number device specification over the 2-digit / 6-digit "
        "one, and 0x0080 says a device memory extension specification is in use. "
        "SH(NA)-080956ENG-M p.35 lists the same values as opaque constants."
    ),
)
"""The source that turns four magic numbers into three independent facts."""


def subcommand(unit: Unit, spec: SpecFormat, extension: bool) -> int:
    """The subcommand for a request in ``unit`` units with a ``spec`` device block.

    ``subcommand(Unit.WORD, SpecFormat.SHORT, False)`` is ``0x0000``;
    ``subcommand(Unit.BIT, SpecFormat.LONG, True)`` is ``0x0083``. Total over its
    domain: eight inputs, eight documented values, no error path and no default.
    """
    if not isinstance(unit, Unit):
        raise TypeError(f"unit must be a Unit, not {type(unit).__name__}")
    if not isinstance(spec, SpecFormat):
        raise TypeError(f"spec must be a SpecFormat, not {type(spec).__name__}")
    if not isinstance(extension, bool):
        raise TypeError(
            f"extension must be a bool -- a device memory extension specification is "
            f"either in use or it is not -- not {type(extension).__name__}"
        )
    value = BIT_UNITS if unit is Unit.BIT else 0
    if spec is SpecFormat.LONG:
        value |= LONG_SPEC
    if extension:
        value |= EXTENSION
    return value


def decode_subcommand(value: int) -> tuple[Unit, SpecFormat, bool]:
    """The inverse: ``0x0081`` -> ``(Unit.BIT, SpecFormat.SHORT, True)``.

    For the simulator, the proxy and ``aslmp explain``, which read a subcommand off a
    frame somebody else built. An undocumented bit raises rather than being masked away:
    a request whose subcommand we do not fully understand is a request whose response
    layout we do not know, and guessing it is how a reader returns plausible numbers for
    a frame it never parsed.
    """
    if not isinstance(value, int) or isinstance(value, bool):
        raise SlmpCodecValueError(
            f"a subcommand is an int, not {type(value).__name__}"
        )
    if not 0 <= value <= 0xFFFF:
        raise SlmpCodecValueError(
            f"a subcommand is a 16-bit field (0..65535); got {value}"
        )
    unknown = value & ~SUBCOMMAND_BITS
    if unknown:
        raise SlmpCodecValueError(
            f"subcommand 0x{value:04X} sets bits 0x{unknown:04X}, which are not in the "
            f"documented field ({BIT_FIELD.reference}: 0x0001 bit units, 0x0002 long "
            f"device specification, 0x0080 device memory extension). Please report this "
            f"with the CPU model and firmware rather than treating it as 0x"
            f"{value & SUBCOMMAND_BITS:04X}."
        )
    unit = Unit.BIT if value & BIT_UNITS else Unit.WORD
    spec = SpecFormat.LONG if value & LONG_SPEC else SpecFormat.SHORT
    return unit, spec, bool(value & EXTENSION)
