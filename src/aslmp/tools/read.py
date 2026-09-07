"""``aslmp read`` -- read one address, or an array, and print the value.

The point of this command is that it is **typed at the command line**. ``aslmp read
192.168.10.250 D0 --as f32`` says what the two words at D0 and D1 mean; there is no
dtype string threaded through a generic call and no ``.value`` on the result. The kind
you name selects one method on the client and its concrete return type, and the printed
value is that type's ``repr``.

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
    add_connection_arguments,
    build_client,
    guarded,
    parse_or_exit,
    usage,
)

if TYPE_CHECKING:
    from aslmp.results import Reading

__all__ = ["KINDS", "build_parser", "run"]

KINDS: tuple[str, ...] = (
    "bit",
    "i16",
    "u16",
    "i32",
    "u32",
    "f32",
    "f64",
    "str",
    "words",
    "bits",
)
"""What ``--as`` accepts. ``words`` and ``bits`` are the raw batch arrays; everything
else is a decoded value."""

_ARRAY_KINDS = frozenset({"words", "bits", "f32"})
"""The kinds ``--count`` is meaningful for. ``--count`` with any other kind is a usage
error rather than a silently ignored flag."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
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
    parser.add_argument(
        "--as",
        dest="kind",
        choices=KINDS,
        default="u16",
        help="how to decode what is read (default: u16, one raw word)",
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
        help="character length; required for --as str",
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
    args = parse_or_exit(build_parser(), argv)
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
