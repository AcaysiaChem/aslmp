"""``aslmp write`` -- write one address, optionally verifying the read-back.

**Device memory only.** There is no path from this command to Remote RUN, Remote STOP,
Remote PAUSE, Remote LATCH CLEAR or Remote RESET. Those commands can stop a running
machine over an unauthenticated cleartext socket, they are gated in the library behind
``Plc(allow_remote_control=True)``, and a command line is exactly the wrong place to put
that switch: a typo in a shell history is not an interlock.

``--verify`` reads the value back through the same client and raises
:class:`~aslmp.errors.SlmpVerificationError` if it disagrees. It is off by default here
because a read-back doubles the round trips and because the CPU's own ``0x0000`` is
truthful for a device write -- unlike Remote RUN, which Mitsubishi documents as
completing normally while the CPU does not run.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from aslmp.tools import EXIT_OK
from aslmp.tools._common import (
    add_connection_arguments,
    build_client,
    guarded,
    parse_or_exit,
    usage,
)

__all__ = ["KINDS", "build_parser", "run"]

KINDS: tuple[str, ...] = ("bit", "i16", "u16", "i32", "u32", "f32", "f64", "str", "words")
"""What ``--as`` accepts. ``words`` writes several raw words from one comma-separated
argument; every other kind writes exactly one value."""

_TRUE = frozenset({"1", "on", "true", "yes"})
_FALSE = frozenset({"0", "off", "false", "no"})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aslmp write",
        description=(
            "Write one device address. Device memory only -- no remote control command "
            "is reachable from this command line."
        ),
        epilog=(
            "A bit value is one of on/off, true/false, yes/no or 1/0. --as words takes "
            "a comma-separated list and writes it as one 0x1401 batch."
        ),
    )
    add_connection_arguments(parser)
    parser.add_argument("address", help="a device address, e.g. D100, M100")
    parser.add_argument("value", help="the value to write")
    parser.add_argument(
        "--as", dest="kind", choices=KINDS, default="u16", help="how to encode the value"
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
        help="how a 32-bit value is split across two words (default: the client's)",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="read the value back afterwards and raise if it disagrees",
    )
    return parser


def _bit(text: str) -> bool:
    lowered = text.strip().lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise ValueError(
        f"{text!r} is not a bit value; write one of on/off, true/false, yes/no, 1/0"
    )


def run(argv: Sequence[str]) -> int:
    args = parse_or_exit(build_parser(), argv)
    if args.kind == "str" and args.length is None:
        return usage("--as str needs --length: the word count is part of the request")
    if args.kind != "str" and args.length is not None:
        return usage("--length applies only to --as str")

    # Parsed before the socket is opened, into one typed local per kind. A single
    # ``object`` holding whichever value this invocation wants would need a cast at
    # every call below, and ``tests/typing/consumer.py`` exists to assert that no call
    # site of this library needs one.
    kind: str = args.kind
    bit_value = False
    int_value = 0
    float_value = 0.0
    words_value: tuple[int, ...] = ()
    str_value: str = args.value
    try:
        if kind == "bit":
            bit_value = _bit(args.value)
        elif kind in ("i16", "u16", "i32", "u32"):
            int_value = int(args.value, 0)
        elif kind in ("f32", "f64"):
            float_value = float(args.value)
        elif kind == "words":
            words_value = tuple(int(part, 0) for part in args.value.split(",") if part.strip())
    except ValueError as exc:
        return usage(f"cannot read {args.value!r} as {kind}: {exc}")
    if kind == "words" and not words_value:
        return usage("--as words needs at least one value")

    async def body() -> int:
        from aslmp.commands import WordOrder

        order = None if args.word_order is None else WordOrder(args.word_order)
        plc = build_client(args)
        address: str = args.address
        verify: bool = args.verify
        async with plc:
            if kind == "bit":
                await plc.write_bit(address, bit_value, verify=verify)
            elif kind == "i16":
                await plc.write_i16(address, int_value, verify=verify)
            elif kind == "u16":
                await plc.write_u16(address, int_value, verify=verify)
            elif kind == "i32":
                await plc.write_i32(address, int_value, word_order=order, verify=verify)
            elif kind == "u32":
                await plc.write_u32(address, int_value, word_order=order, verify=verify)
            elif kind == "f32":
                await plc.write_f32(address, float_value, word_order=order, verify=verify)
            elif kind == "f64":
                await plc.write_f64(address, float_value, word_order=order, verify=verify)
            elif kind == "words":
                await plc.write_words(address, words_value)
            else:
                await plc.write_str(
                    address, str_value, length=args.length, verify=verify
                )
            suffix = " (verified by read-back)" if verify else ""
            print(f"wrote {args.value} to {address} as {kind}{suffix}")
        return EXIT_OK

    return guarded(body)
