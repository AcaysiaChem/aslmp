"""``aslmp write`` -- write one address, optionally verifying the read-back.

**Device memory only.** There is no path from this command to Remote RUN, Remote STOP,
Remote PAUSE, Remote LATCH CLEAR or Remote RESET. Those commands can stop a running
machine over an unauthenticated cleartext socket, they are gated in the library behind
``Plc(allow_remote_control=True)``, and a command line is exactly the wrong place to put
that switch: a typo in a shell history is not an interlock.

``--as`` is **required**, exactly as it is on ``aslmp read``, and for a stronger version
of the same reason. It defaulted to ``u16`` here for three revisions -- *after* the read
side had had its default taken away with the sentence "a register carries no type on the
wire, so there is no default that could be right" -- so the mutating half of the pair
kept the guess. ``aslmp write 192.168.10.250 D100 60`` exited 0 having chosen an
interpretation on the caller's behalf; the two registers it wrote read back as
``8.407790785948902e-44`` under ``--as f32`` (measured on FX5U-32MT/DS fw 1.065 from this
host over TCP 5002, 2026-09-07). A wrong guess on a read prints a wrong number. A wrong
guess here puts one into the machine.

``--verify`` reads the value back through the same client and raises
:class:`~aslmp.errors.SlmpVerificationError` if it disagrees. It is off by default here
because a read-back doubles the round trips and because the CPU's own ``0x0000`` is
truthful for a device write -- unlike Remote RUN, which Mitsubishi documents as
completing normally while the CPU does not run. It is refused, rather than ignored, for
the array kinds: :meth:`~aslmp.client.Plc.write_words`,
:meth:`~aslmp.client.Plc.write_bits` and :meth:`~aslmp.client.Plc.write_f32_array` take
no ``verify``, and this command used to print "(verified by read-back)" after an
``--as words --verify`` that had verified nothing.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

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

__all__ = ["ARRAY_KINDS", "KINDS", "build_parser", "run"]

KINDS: tuple[str, ...] = VALUE_KINDS
"""What ``--as`` accepts. The **same** tuple ``aslmp read`` offers.

It was not: ``bits`` was readable and not writable, so the command line could observe a
run of bit devices and not write one, although :meth:`~aslmp.client.Plc.write_bits` has
existed since the batch commands did.
"""

ARRAY_KINDS: frozenset[str] = frozenset({"words", "bits", "f32"})
"""The kinds a comma-separated list may be written through, mirroring ``read --count``.

The same three ``aslmp read`` accepts ``--count`` for, and for the same reason: they are
the three the library has an array call for. ``--as f32`` with one value is
:meth:`~aslmp.client.Plc.write_f32`; with several it is
:meth:`~aslmp.client.Plc.write_f32_array`, the way ``read --as f32 --count 3`` is
:meth:`~aslmp.client.Plc.read_f32_array`.
"""

WHY_AS_IS_REQUIRED = (
    "--as is required: a register carries no type on the wire, so there is no default "
    "that could be right -- and this is the half that changes the machine. It defaulted "
    "to u16 here after `aslmp read` had already had its default taken away for exactly "
    "this reason, so `aslmp write ... D100 60` exited 0 having chosen an interpretation "
    "on your behalf; those registers read back as 8.407790785948902e-44 under --as f32 "
    "(measured on FX5U-32MT/DS fw 1.065 over TCP 5002, 2026-09-07). "
    f"Choose one of: {', '.join(KINDS)}. Check the type in GX Works3 under "
    "Label -> Global Label, or read the address first with `aslmp read ... --as words` "
    "to look at the bytes that are there now."
)
"""Why the caller is being asked. Printed *instead of* argparse's own one-liner.

Said once and used twice, as ``aslmp read`` says its own: this is both the substance of
``--as``'s ``help`` and the whole of the failure message.
"""

_TRUE = frozenset({"1", "on", "true", "yes"})
_FALSE = frozenset({"0", "off", "false", "no"})


class _WriteParser(KindRequiredParser):
    """``aslmp write``'s parser: ``--as`` required, with the reason kept.

    See :class:`~aslmp.tools._common.KindRequiredParser`. Declaring ``--as`` required is
    what makes ``aslmp write --help`` print it without brackets; catching argparse's own
    refusal is what keeps :data:`WHY_AS_IS_REQUIRED` in front of the person who forgot it.
    """

    WHY = WHY_AS_IS_REQUIRED


def build_parser() -> argparse.ArgumentParser:
    parser = _WriteParser(
        prog="aslmp write",
        description=(
            "Write one device address. Device memory only -- no remote control command "
            "is reachable from this command line."
        ),
        epilog=(
            "A bit value is one of on/off, true/false, yes/no or 1/0. --as words, bits "
            "and f32 take a comma-separated list and write it as one batch."
        ),
    )
    add_connection_arguments(parser)
    parser.add_argument("address", help="a device address, e.g. D100, M100")
    parser.add_argument("value", help="the value to write, or a comma-separated list")
    add_kind_argument(
        parser,
        help=(
            "how to encode the value. A register carries no type on the wire, so there "
            "is no default that could be right -- least of all on a write"
        ),
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


def _parts(text: str) -> list[str]:
    """A comma-separated list, with the empty tail of ``"1,2,"`` dropped."""
    return [part for part in text.split(",") if part.strip()]


def run(argv: Sequence[str]) -> int:
    try:
        args = parse_or_exit(build_parser(), argv)
    except KindNotGivenError as missing:
        return usage(str(missing))
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
    bits_value: tuple[bool, ...] = ()
    floats_value: tuple[float, ...] = ()
    str_value: str = args.value
    try:
        if kind == "bit":
            bit_value = _bit(args.value)
        elif kind in ("i16", "u16", "i32", "u32"):
            int_value = int(args.value, 0)
        elif kind == "f64":
            float_value = float(args.value)
        elif kind == "f32":
            floats_value = tuple(float(part) for part in _parts(args.value))
            float_value = floats_value[0] if floats_value else 0.0
        elif kind == "words":
            words_value = tuple(int(part, 0) for part in _parts(args.value))
        elif kind == "bits":
            bits_value = tuple(_bit(part) for part in _parts(args.value))
    except ValueError as exc:
        return usage(f"cannot read {args.value!r} as {kind}: {exc}")
    if kind == "words" and not words_value:
        return usage("--as words needs at least one value")
    if kind == "bits" and not bits_value:
        return usage("--as bits needs at least one value")
    if kind == "f32" and not floats_value:
        return usage("--as f32 needs at least one value")

    # An array call takes no ``verify``, and this command used to print "(verified by
    # read-back)" after one anyway. Refused rather than ignored: a flag that is silently
    # dropped is a claim the output then makes on its behalf.
    array = kind in ("words", "bits") or (kind == "f32" and len(floats_value) > 1)
    if array and args.verify:
        return usage(
            f"--verify does not apply to a list of values (--as {kind}): write_words, "
            f"write_bits and write_f32_array take no read-back, and this command "
            f"printed '(verified by read-back)' after one anyway. Write the points one "
            f"at a time to verify each."
        )

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
            elif kind == "f32" and len(floats_value) > 1:
                await plc.write_f32_array(address, floats_value, word_order=order)
            elif kind == "f32":
                await plc.write_f32(address, float_value, word_order=order, verify=verify)
            elif kind == "f64":
                await plc.write_f64(address, float_value, word_order=order, verify=verify)
            elif kind == "words":
                await plc.write_words(address, words_value)
            elif kind == "bits":
                await plc.write_bits(address, bits_value)
            else:
                await plc.write_str(
                    address, str_value, length=args.length, verify=verify
                )
            suffix = " (verified by read-back)" if verify else ""
            print(f"wrote {args.value} to {address} as {kind}{suffix}")
        return EXIT_OK

    return guarded(body)
