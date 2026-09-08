"""``aslmp read`` -- read one address, or an array, and print the value.

The point of this command is that it is **typed at the command line**. ``aslmp read
192.168.10.250 D0 --as f32`` says what the two words at D0 and D1 mean; there is no
dtype string threaded through a generic call and no ``.value`` on the result. The kind
you name selects one method on the client and its concrete return type, and the printed
value is that type's ``repr``.

``--as`` is **required**, for the same reason ``--profile`` is: there is no default that
could be right -- required in the parser, so the usage line prints ``--as {...}`` without
the brackets that mean optional, and not merely refused in the body afterwards. It
defaulted to ``u16`` for one revision, and on the bench this library
was built against that made ``aslmp read 192.168.10.250 D8`` print ``54720`` -- ``D8``
holds a ``REAL`` there (``IO_Scan := IO_Scan + 1.0`` in the CPU's own ST), so 54720 is
the low half of a float's bit pattern rendered as a plausible integer, and the CPU
answers ``0x0000`` either way. Measured again from this host over TCP 5002 on
2026-09-07: the same register read ``1625586.0`` as an f32 and ``30984`` as a u16 in the
same second. "What is in D8" is the first question a newcomer asks, and the honest
answer is that the register does not know; asking the caller costs six characters and is
the whole difference between a value and a number.

Every read here goes through the same ``0x0401`` / ``0x0403`` path as the library, with
the same pre-transport refusals, so ``aslmp read ... D8000`` fails with the range error
rather than with the CPU's ``0xC056``, and says which is which.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from aslmp.tools import EXIT_OK
from aslmp.tools._common import (
    VALUE_KINDS,
    KindNotGivenError,
    KindRequiredParser,
    add_connection_arguments,
    add_kind_argument,
    build_client,
    guarded,
    parse_or_exit,
    usage,
)

if TYPE_CHECKING:
    from aslmp.results import Reading

__all__ = ["KINDS", "build_parser", "run"]

KINDS: tuple[str, ...] = VALUE_KINDS
"""What ``--as`` accepts. ``words`` and ``bits`` are the raw batch arrays; everything
else is a decoded value.

The **same** tuple ``aslmp write`` offers, and one object rather than two lists that
happen to agree today: they did not agree, and ``bits`` was the one that was readable
and not writable (:data:`~aslmp.tools._common.VALUE_KINDS`).
"""

_ARRAY_KINDS = frozenset({"words", "bits", "f32"})
"""The kinds ``--count`` is meaningful for. ``--count`` with any other kind is a usage
error rather than a silently ignored flag."""

WHY_AS_IS_REQUIRED = (
    "--as is required: a register carries no type on the wire, so there is no "
    "default that could be right. On the bench this library was built against, "
    "D8 is a REAL -- the CPU's own ST does IO_Scan := IO_Scan + 1.0 -- and this "
    "command's old default of u16 answered `aslmp read ... D8` with 54720 "
    "(measured on FX5U-32MT/DS fw 1.065, 2026-09-07): the low half of that "
    "float's bit pattern, a perfectly plausible integer, end code 0x0000. "
    f"Choose one of: {', '.join(KINDS)}. Check the type in GX Works3 under "
    "Label -> Global Label; `--as words` prints the raw registers if you want "
    "to look at the bytes first."
)
"""Why the caller is being asked. Printed *instead of* argparse's own one-liner.

Said once, and used twice: this is both the reason in the failure message and the
substance of ``--as``'s ``help``. The two used to be separate strings.
"""


_MissingKind = KindNotGivenError
"""``--as`` was not given. Carried out of argparse rather than exiting.

See :class:`_ReadParser` for why this exists rather than an ``exit(2)``. It lives in
``_common`` now rather than here, because ``aslmp write`` needs exactly the same thing
for exactly the same reason and had not been given it
(:class:`~aslmp.tools._common.KindNotGivenError`).
"""


class _ReadParser(KindRequiredParser):
    """An ``ArgumentParser`` whose usage line tells the truth about ``--as``.

    ``--as`` is declared ``required=True``, because it is: without it this command
    exits 2 and reads nothing. Until 2026-09-07 it was declared optional with
    ``default=None`` and refused in the body, so ``aslmp read --help`` printed
    ``[--as {bit,i16,...}]`` -- brackets, meaning optional -- directly above a help
    string beginning "REQUIRED". A usage line that contradicts its own help is the same
    defect as a docstring that contradicts its code, in the command this project had
    just finished fixing for telling users things that were not so.

    Declaring it required moves the refusal into argparse, which would print
    ``the following arguments are required: --as`` and throw away the reason. The reason
    is the whole value of the message -- it is the one place a newcomer is told that a D
    register carries no type -- so that one error is converted back into
    :func:`~aslmp.tools._common.usage`'s return-a-status form and rendered with
    :data:`WHY_AS_IS_REQUIRED`. Every other parse error is argparse's own, unchanged.

    The mechanism is :class:`~aslmp.tools._common.KindRequiredParser` and this class is
    the two lines that say which paragraph to print: ``aslmp write`` needed the identical
    behaviour and was the half still carrying a default, so the machinery is shared and
    only the reason differs.
    """

    WHY = WHY_AS_IS_REQUIRED


def build_parser() -> argparse.ArgumentParser:
    parser = _ReadParser(
        prog="aslmp read",
        description="Read one device address and print the value. Changes nothing.",
        epilog=(
            "Addresses are written the way GX Works3 shows them. On an iQ-F, X and Y "
            "are OCTAL: Y20 is the 17th output (wire number 16, measured 2026-09-07), "
            "and Y8 is refused because it does not exist -- the CPU accepts it and "
            "answers 0x0000, so refusing it is the client's job."
        ),
    )
    add_connection_arguments(parser)
    parser.add_argument("address", help="a device address, e.g. D0, M100, Y20, SD203")
    add_kind_argument(
        parser,
        help=(
            "how to decode what is read. A register carries no type on the wire, so "
            "there is no default that could be right"
        ),
    )
    parser.add_argument(
        "--count",
        type=int,
        default=1,
        metavar="N",
        help="how many points; only for --as words, bits or f32 (default: 1)",
    )
    parser.add_argument(
        "--length",
        type=int,
        default=None,
        metavar="N",
        help="byte length, two per register; required for --as str",
    )
    parser.add_argument(
        "--word-order",
        choices=("low-first", "high-first"),
        default=None,
        help=(
            "how a 32-bit value is assembled from two words. A PLC-PROGRAM convention, "
            "not a protocol fact; the default is the client's low-word-first, which is "
            "what GX Works3 EMOV writes"
        ),
    )
    parser.add_argument(
        "--hex", action="store_true", help="print integers in hexadecimal as well"
    )
    parser.add_argument(
        "--timing",
        action="store_true",
        help="also print the transaction's wire time and chunk count",
    )
    return parser


def _order(name: str | None) -> Any:
    from aslmp.commands import WordOrder

    return None if name is None else WordOrder(name)


def run(argv: Sequence[str]) -> int:
    try:
        args = parse_or_exit(build_parser(), argv)
    except _MissingKind:
        return usage(WHY_AS_IS_REQUIRED)
    if args.count < 1:
        return usage(f"--count must be at least 1; got {args.count}")
    if args.count > 1 and args.kind not in _ARRAY_KINDS:
        return usage(
            f"--count {args.count} is meaningless for --as {args.kind}; "
            f"only {', '.join(sorted(_ARRAY_KINDS))} read arrays"
        )
    if args.kind == "str" and args.length is None:
        return usage("--as str needs --length: a string's word count is part of the request")
    if args.kind != "str" and args.length is not None:
        return usage("--length applies only to --as str")

    async def body() -> int:
        plc = build_client(args)
        order = _order(args.word_order)
        async with plc:
            timed = plc.timed
            # One variable, many concrete value types: the branch picks the method and
            # the method's return type is concrete. ``Any`` is the erased element type
            # of a heterogeneous printer, and it never escapes this function.
            reading: Reading[Any]
            address = args.address
            kind = args.kind
            count = args.count
            if kind == "words" or (kind == "u16" and count > 1):
                reading = await timed.read_words(address, count)
            elif kind == "bits":
                reading = await timed.read_bits(address, count)
            elif kind == "f32" and count > 1:
                reading = await timed.read_f32_array(address, count, word_order=order)
            elif kind == "bit":
                reading = await timed.read_bit(address)
            elif kind == "i16":
                reading = await timed.read_i16(address)
            elif kind == "u16":
                reading = await timed.read_u16(address)
            elif kind == "i32":
                reading = await timed.read_i32(address, word_order=order)
            elif kind == "u32":
                reading = await timed.read_u32(address, word_order=order)
            elif kind == "f32":
                reading = await timed.read_f32(address, word_order=order)
            elif kind == "f64":
                reading = await timed.read_f64(address, word_order=order)
            else:
                reading = await timed.read_str(address, length=args.length)

            print(_render(reading.value, hexadecimal=args.hex))
            if args.timing:
                timing = reading.tx.timing
                print(
                    f"  {timing.wire_ms:.2f} ms wire, "
                    f"{len(timing.chunks)} chunk(s), "
                    f"command 0x{reading.tx.command:04X} "
                    f"sub 0x{reading.tx.subcommand:04X}, "
                    f"{reading.tx.response_bytes} bytes back"
                )
        return EXIT_OK

    return guarded(body)


def _render(value: object, *, hexadecimal: bool) -> str:
    """One value, printed so that it can be pasted back into Python."""
    if isinstance(value, tuple):
        return "\n".join(
            f"[{index}] {_render(item, hexadecimal=hexadecimal)}"
            for index, item in enumerate(value)
        )
    if isinstance(value, bool):
        return "ON" if value else "OFF"
    if isinstance(value, int) and hexadecimal:
        return f"{value} (0x{value & 0xFFFFFFFF:04X})"
    return repr(value) if isinstance(value, str) else str(value)
