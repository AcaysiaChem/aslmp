"""Zero ``cast()`` at any call site, asserted by ``mypy`` rather than claimed.

Graft G13 of the locked architecture. This module is never executed and nothing here is
a runtime test: it is a **compile-time** one. ``mypy`` checks ``tests/`` as well as
``src/``, ``typing.assert_type`` fails the build when an inferred type is not exactly the
type written, and ``warn_unused_ignores`` under ``--strict`` fails it when a ``type:
ignore`` becomes unnecessary. So "the caller never has to narrow, cast or ``isinstance``"
is a thing CI proves.

Why it needs its own file. Every one of these lines would type-check trivially if the
methods returned ``Any``: the entire failure mode this guards against is a return
annotation quietly widening -- a ``tuple[int, ...]`` becoming ``tuple[Any, ...]``, a
``Reading[float]`` becoming ``Reading[Any]`` -- which no runtime test can see and which
makes every downstream call site silently unchecked.

The three shapes it pins:

1. **The primary surface returns bare values.** ``plc.read_f32("D0")`` is a ``float``.
   Not ``Reading[float]``, not ``float | None``.
2. **``plc.timed`` returns the record beside the value**, with the same parameter list,
   from a generated module -- so a drift between the two surfaces is a type error here
   before it is a bug anywhere else.
3. **The overloaded returns stay narrow.** ``read_random(points)`` is a
   ``RandomReading``; only ``allow_split=True`` produces the union, and only the caller
   who asked for it has to narrow anything.
"""

from __future__ import annotations

from typing import assert_type

from aslmp.blocks.fields import F32, U32, Bit, PlcBlock, Str, at
from aslmp.blocks.layout import plc_block
from aslmp.blocks.plan import BlockPlan, Split, SplitBlockPlan
from aslmp.client import Plc
from aslmp.commands.monitor import MonitorRegistration
from aslmp.commands.random import RandomValue, bit_point, dword, word
from aslmp.identity import CpuIdentity, CpuStatus
from aslmp.results import RandomReading, Reading, SplitReading, WriteAck
from aslmp.wire.raw import RawResponse

PLC = Plc("192.168.10.250", 5002, profile="melsec:iq-f/fx5u")


# ----------------------------------------------------------------------------------------
# 1. the primary surface is bare values
# ----------------------------------------------------------------------------------------


async def scalars(plc: Plc) -> None:
    assert_type(await plc.read_bit("M100"), bool)
    assert_type(await plc.read_i16("D0"), int)
    assert_type(await plc.read_u16("D0"), int)
    assert_type(await plc.read_i32("D0"), int)
    assert_type(await plc.read_u32("D0"), int)
    assert_type(await plc.read_f32("D0"), float)
    assert_type(await plc.read_f64("D0"), float)
    assert_type(await plc.read_str("D200", length=8), str)


async def arrays(plc: Plc) -> None:
    assert_type(await plc.read_words("D0", 10), tuple[int, ...])
    assert_type(await plc.read_bits("M0", 10), tuple[bool, ...])
    assert_type(await plc.read_f32_array("D0", 4), tuple[float, ...])
    assert_type(await plc.read_blocks([]), tuple[tuple[int, ...], ...])


async def writes(plc: Plc) -> None:
    """A write on the primary surface returns nothing at all."""
    assert_type(await plc.write_f32("D100", 1234.5), None)
    assert_type(await plc.write_bit("M100", True, verify=True), None)
    assert_type(await plc.write_words("D100", [1, 2, 3]), None)


async def diagnostics(plc: Plc) -> None:
    assert_type(await plc.self_test(), bytes)
    assert_type(await plc.ping(), float)
    assert_type(await plc.read_type_name(), CpuIdentity)
    assert_type(await plc.read_cpu_status(), CpuStatus)
    assert_type(await plc.clear_error(), None)


# ----------------------------------------------------------------------------------------
# 2. plc.timed carries the transaction, and only there
# ----------------------------------------------------------------------------------------


async def timed(plc: Plc) -> None:
    assert_type(await plc.timed.read_f32("D0"), Reading[float])
    assert_type(await plc.timed.read_bit("M100"), Reading[bool])
    assert_type(await plc.timed.read_words("D0", 10), Reading[tuple[int, ...]])
    assert_type(await plc.timed.read_type_name(), Reading[CpuIdentity])
    assert_type(await plc.timed.write_f32("D100", 1234.5), WriteAck)
    assert_type(await plc.timed.write_words("D100", [1, 2]), WriteAck)
    assert_type(await plc.timed.clear_error(), WriteAck)


async def timed_value_is_the_bare_value(plc: Plc) -> None:
    """``.value`` is the type the primary surface returns, with nothing lost."""
    reading = await plc.timed.read_f32("D0")
    assert_type(reading.value, float)
    assert_type(reading.tx.timing.wire_ms, float)
    assert_type(reading.tx.generation, int)


# ----------------------------------------------------------------------------------------
# 3. the overloads keep the control-loop primitive narrow
# ----------------------------------------------------------------------------------------


async def random_access(plc: Plc) -> None:
    """No ``isinstance`` narrowing on the one call a control loop makes every cycle."""
    points = [dword("D0", kind="f32"), word("D8"), bit_point("M100")]
    reading = await plc.read_random(points)
    assert_type(reading, RandomReading)
    assert_type(reading.f32(0), float)
    assert_type(reading.u16(1), int)
    assert_type(reading.i32(0), int)
    assert_type(reading.bits(2), tuple[bool, ...])
    assert_type(reading[0], RandomValue)
    assert_type(len(reading), int)
    assert_type(reading.tx.timing.wire_ms, float)


async def random_access_split(plc: Plc) -> None:
    """Opting in to a split is the only way to be handed a union to narrow."""
    points = [word("D0")]
    assert_type(await plc.read_random(points, allow_split=True), RandomReading | SplitReading)
    assert_type(
        await plc.timed.read_random(points, allow_split=True),
        Reading[RandomReading] | Reading[SplitReading],
    )
    assert_type(await plc.timed.read_random(points), Reading[RandomReading])


async def monitor(plc: Plc) -> None:
    registration = await plc.monitor_register([word("D0")])
    assert_type(registration, MonitorRegistration)
    assert_type(await plc.monitor_read(registration), RandomReading)


async def escape_hatch(plc: Plc) -> None:
    """``expect_response=False`` returns ``None`` instead of widening the normal type."""
    assert_type(await plc.raw_command(0x0401, 0x0000, b""), RawResponse)
    assert_type(await plc.raw_command(0x1006, 0x0000, b"", expect_response=False), None)


# ----------------------------------------------------------------------------------------
# The lifecycle surface, which a supervisor and a health monitor both consume
# ----------------------------------------------------------------------------------------


async def lifecycle(plc: Plc) -> None:
    info = await plc.connect()
    assert_type(info.generation, int)
    assert_type(info.peer, tuple[str, int])
    assert_type(plc.identity, CpuIdentity | None)
    assert_type(plc.generation, int)
    assert_type(plc.counters.transactions_completed, int)
    assert_type(plc.metrics().generation, int)
    await plc.aclose()


# ----------------------------------------------------------------------------------------
# Blocks (DESIGN section 5.8). The whole point of the ``Annotated`` aliases is that a field
# reads back as exactly ``float`` / ``int`` / ``bool`` / ``str`` with no cast at the call
# site; if ``F32`` ever widened to ``Any`` every line of every control loop would silently
# stop being checked, and no runtime test could see it.
# ----------------------------------------------------------------------------------------


@plc_block(base="D0")
class LoopState(PlcBlock):
    setpoint: F32
    process_value: F32
    scan: U32
    fault: Bit = at("M100")
    tag: str = Str(length=8, address="D110")


async def blocks(plc: Plc) -> None:
    plan = plc.bind(LoopState)
    assert_type(plan, BlockPlan[LoopState])
    state = await plan.read()
    assert_type(state, LoopState)
    assert_type(state.setpoint, float)
    assert_type(state.scan, int)
    assert_type(state.fault, bool)
    assert_type(state.tag, str)
    assert_type(await plc.read_block(plan), LoopState)
    assert_type(await plc.write_block(plan, state), None)


async def blocks_split(plc: Plc) -> None:
    """``allow_split=True`` is the only way to be handed something that is not a ``B``."""
    maybe = plc.bind(LoopState, allow_split=True)
    assert_type(maybe, BlockPlan[LoopState] | SplitBlockPlan[LoopState])
    if isinstance(maybe, SplitBlockPlan):
        assert_type(await maybe.read(), Split[LoopState])
    else:
        assert_type(await maybe.read(), LoopState)
