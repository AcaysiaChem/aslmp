"""The conformance suite, end to end, and the one test the architecture rests on.

Four things happen in this file.

1. The shipped suite is run against every target, over TCP and over UDP, and must come
   back clean. The suite is its own client -- ``run_conformance`` and ``TcpExchange``
   reach ``aslmp.transport`` from nowhere, and ``aslmp.testing`` is forbidden to import
   it at all (DESIGN section 4.11) -- so a transport bug is visible here rather than
   cancelled out by both sides sharing it.
2. The two reports are diffed. Running the same questions against ``PEDANTIC`` and
   against ``FX5U_32MT_DS`` produces, empirically, the same list of divergences that
   :func:`~aslmp.testing.targets.diff_targets` states declaratively -- and a test asserts
   the two agree, so neither can drift.
3. **The proof that the in-flight gate is load-bearing.** A client that holds one
   transaction in flight reads two registers correctly. A client that writes both
   requests before reading gets ONE response, for the LAST request, with end code
   ``0x0000`` -- and the same assertions applied to it fail. That failure is asserted
   here, so "removing the gate makes this test fail" is a thing CI checks rather than a
   thing a docstring claims.
4. **The oracle that agreed with the defect.** The last section *does* build a real
   client, because it is about a decode rather than about the wire: the simulator's
   ``D8`` now holds the ``REAL`` the bench holds, so a ``plc_clock`` declared ``u32``
   reads a float's bit pattern here exactly as it did off the silicon. Until 2026-09-07
   the simulator wrote an integer there and the test could not fail.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Final

import pytest

from aslmp.blocks.fields import F32, Bounds, PlcBlock, SlmpImplausibleValueError
from aslmp.blocks.layout import plc_block
from aslmp.blocks.plan import bind
from aslmp.client import Plc, PlcClockSource
from aslmp.testing.conformance import (
    TcpExchange,
    UdpExchange,
    run_conformance,
    standard_cases,
)
from aslmp.testing.memory import BENCH_SCAN_WRAP
from aslmp.testing.pathology import HEALTHY
from aslmp.testing.pytest_plugin import (  # noqa: F401 - importing registers the fixtures
    slmp_context,
    slmp_entries,
    slmp_exchange,
    slmp_pathology,
    slmp_simulator,
    slmp_target,
)
from aslmp.testing.server import Entry, PlcSimulator, codec_for
from aslmp.testing.targets import (
    ALL_TARGETS,
    FX5U_32MT_DS,
    PEDANTIC,
    R04CPU,
    SimulatorTarget,
    diff_targets,
)
from aslmp.transport.base import TransportKind
from aslmp.wire.codec import BINARY
from aslmp.wire.frames import FOUR_E, THREE_E, request_body
from aslmp.wire.route import Route

pytestmark = pytest.mark.simulator


@contextlib.asynccontextmanager
async def running(
    target: SimulatorTarget, **kwargs: object
) -> AsyncIterator[PlcSimulator]:
    plc = PlcSimulator(target=target, **kwargs)  # type: ignore[arg-type]  # heterogeneous kwargs
    await plc.start()
    try:
        yield plc
    finally:
        await plc.aclose()


def read_frame_for(device: bytes, count: int = 1) -> bytes:
    """A 0401 Device Read request. Built here, not through the client's transport."""
    payload = device + BINARY.number(count, bits=16)
    body = request_body(
        BINARY, monitoring_timer=0, command=0x0401, subcommand=0x0000, payload=payload
    )
    return THREE_E.build(route=Route.OWN_STATION, body=body, codec=BINARY)


D0 = b"\x00\x00\x00\xa8"
D8 = b"\x08\x00\x00\xa8"
SETPOINT = 0x1111
SCAN = 0x8888


# ======================================================================================
# 1. The suite
# ======================================================================================


@pytest.mark.parametrize("key", sorted(ALL_TARGETS))
async def test_the_suite_passes_against_every_target_over_tcp(key: str) -> None:
    """Each target answers its own declared behaviour, all the way down to the bytes."""
    target = ALL_TARGETS[key]
    async with running(target) as plc:
        entry = plc.entry("tcp")
        host, port = plc.address("tcp")
        exchange = await TcpExchange.connect(
            host, port, frame=entry.frame_format, codec=codec_for(entry.encoding)
        )
        try:
            report = await run_conformance(exchange, target, allow_writes=True)
        finally:
            await exchange.aclose()
    report.assert_clean()
    assert not report.of("skip")
    assert len(report.of("pass")) == len(standard_cases(target))


async def test_the_suite_passes_over_udp() -> None:
    """A datagram is one message, so every case works unchanged over UDP.

    UDP does not suffer the TCP coalescing corruption -- the same two-requests-with-no-
    read test returns both responses (measured on FX5U-32MT/DS fw 1.065, 2026-09-06) --
    which is why the suite can be pointed at either transport without changing a case.
    """
    async with running(FX5U_32MT_DS) as plc:
        entry = plc.entry("udp")
        host, port = plc.address("udp")
        exchange = await UdpExchange.connect(
            host, port, frame=entry.frame_format, codec=codec_for(entry.encoding)
        )
        try:
            report = await run_conformance(exchange, FX5U_32MT_DS, allow_writes=True)
        finally:
            await exchange.aclose()
    report.assert_clean()


async def test_writes_are_skipped_unless_they_are_explicitly_allowed() -> None:
    """The suite is meant to be pointed at a machine that is running."""
    async with running(FX5U_32MT_DS) as plc:
        entry = plc.entry("tcp")
        host, port = plc.address("tcp")
        exchange = await TcpExchange.connect(
            host, port, frame=entry.frame_format, codec=codec_for(entry.encoding)
        )
        try:
            report = await run_conformance(exchange, FX5U_32MT_DS)
        finally:
            await exchange.aclose()
    report.assert_clean()
    skipped = report.of("skip")
    assert skipped
    assert all(result.case.mutates for result in skipped)
    assert all("allow_writes=True" in result.detail for result in skipped)


async def test_a_mutating_case_touches_only_the_scratch_registers() -> None:
    """D100 and M100: the bench's own scratch range, and nothing outside it."""
    async with running(FX5U_32MT_DS) as plc:
        before = plc.memory.snapshot()
        entry = plc.entry("tcp")
        host, port = plc.address("tcp")
        exchange = await TcpExchange.connect(
            host, port, frame=entry.frame_format, codec=codec_for(entry.encoding)
        )
        try:
            await run_conformance(exchange, FX5U_32MT_DS, allow_writes=True)
        finally:
            await exchange.aclose()
        assert plc.memory.read_words("D", 100, 1) == (0x1234,)
        assert plc.memory.read_bits("M", 100, 1) == (True,)
        plc.memory.restore(before)
        assert plc.memory.read_words("D", 100, 1) == (0,)


async def test_an_unverified_target_says_so_in_its_report() -> None:
    """There is no iQ-R in the building and the report has to print that."""
    async with running(R04CPU) as plc:
        entry = plc.entry("tcp")
        host, port = plc.address("tcp")
        exchange = await TcpExchange.connect(
            host, port, frame=entry.frame_format, codec=codec_for(entry.encoding)
        )
        try:
            report = await run_conformance(exchange, R04CPU, allow_writes=True)
        finally:
            await exchange.aclose()
    report.assert_clean()
    assert report.verified is False
    assert "UNVERIFIED" in report.to_markdown()


async def test_a_failing_case_is_reported_rather_than_swallowed() -> None:
    """Point the FX5U suite at a reference CPU and the corrected end codes disagree."""
    async with running(PEDANTIC) as plc:
        entry = plc.entry("tcp")
        host, port = plc.address("tcp")
        exchange = await TcpExchange.connect(
            host, port, frame=entry.frame_format, codec=codec_for(entry.encoding)
        )
        try:
            report = await run_conformance(exchange, FX5U_32MT_DS, allow_writes=True)
        finally:
            await exchange.aclose()
    assert report.failures
    with pytest.raises(AssertionError, match="conformance case"):
        report.assert_clean()


# ======================================================================================
# 2. The diff, arrived at empirically
# ======================================================================================


async def _end_codes(target: SimulatorTarget) -> dict[str, int | None]:
    async with running(target) as plc:
        entry = plc.entry("tcp")
        host, port = plc.address("tcp")
        exchange = await TcpExchange.connect(
            host, port, frame=entry.frame_format, codec=codec_for(entry.encoding)
        )
        try:
            report = await run_conformance(
                exchange, target, cases=standard_cases(target), allow_writes=True
            )
        finally:
            await exchange.aclose()
    report.assert_clean()
    return report.end_codes


async def test_running_both_targets_and_diffing_is_the_document() -> None:
    """The empirical diff must be exactly the three corrected end codes.

    ``diff_targets`` says the same thing declaratively. If the two ever disagree, one of
    them is describing a CPU that does not exist.
    """
    pedantic = await _end_codes(PEDANTIC)
    measured = await _end_codes(FX5U_32MT_DS)
    differing = {
        key for key in pedantic if pedantic[key] != measured.get(key)
    }
    assert differing == {
        "batch_read_zero_points",
        "bad_device_code",
        "understated_length",
    }
    assert (pedantic["batch_read_zero_points"], measured["batch_read_zero_points"]) == (
        0xC056,
        0xC052,
    )
    assert (pedantic["bad_device_code"], measured["bad_device_code"]) == (0xC05B, 0xC05C)
    assert (pedantic["understated_length"], measured["understated_length"]) == (
        0xC057,
        0xC061,
    )

    declared = diff_targets(PEDANTIC, FX5U_32MT_DS)
    for name in (
        "end_code.zero_point_count",
        "end_code.unknown_device_code",
        "end_code.request_length_mismatch",
    ):
        assert name in declared.names(), (
            "the empirical diff found a divergence the declarative one does not state"
        )


async def test_the_declarative_diff_renders_a_document() -> None:
    text = diff_targets(PEDANTIC, FX5U_32MT_DS).to_markdown()
    assert "| `end_code.zero_point_count` | 0xC056 | 0xC052 |" in text
    assert "| `limit.batch_bit` | 7168 | 3584 |" in text
    assert "| `monitor` | True | False |" in text
    assert "pathology.coalesce_requests" in text


# ======================================================================================
# 3. The test that matters most
# ======================================================================================


async def assert_both_registers_read_correctly(answers: list[bytes | None]) -> None:
    """The assertion a correct client must satisfy. Applied to both clients below.

    Deliberately one function, used twice: "removing the gate makes this test fail" only
    means something if it is the *same* test.
    """
    assert len(answers) == 2, f"two requests, two responses; got {len(answers)}"
    first, second = answers
    assert first is not None, "the first read was never answered"
    assert second is not None, "the second read was never answered"
    assert first[THREE_E.data_offset(BINARY) :] == BINARY.words((SETPOINT,))
    assert second[THREE_E.data_offset(BINARY) :] == BINARY.words((SCAN,))


async def _gated(host: str, port: int) -> list[bytes | None]:
    """One transaction in flight: write, read, write, read."""
    exchange = await TcpExchange.connect(host, port, frame=THREE_E, codec=BINARY)
    try:
        return [
            await exchange(read_frame_for(D0)),
            await exchange(read_frame_for(D8)),
        ]
    finally:
        await exchange.aclose()


async def _ungated(host: str, port: int) -> list[bytes | None]:
    """Both requests written before either response is read. The gate removed."""
    reader, writer = await asyncio.open_connection(host, port)
    try:
        writer.write(read_frame_for(D0) + read_frame_for(D8))
        await writer.drain()
        answers: list[bytes | None] = []
        for _ in range(2):
            try:
                prefix = await asyncio.wait_for(
                    reader.readexactly(THREE_E.prefix_units(BINARY)), 0.3
                )
                declared = BINARY.read_number(
                    prefix,
                    THREE_E.subheader_units(BINARY) + Route.wire_len(BINARY),
                    bits=16,
                )
                answers.append(prefix + await reader.readexactly(declared))
            except (TimeoutError, asyncio.IncompleteReadError, ConnectionError):
                answers.append(None)
        return answers
    finally:
        writer.close()


async def test_the_in_flight_gate_is_what_prevents_the_corruption() -> None:
    """The whole architecture, in one test.

    With the gate the two registers read correctly. Without it the FX5U answers ONCE,
    for the LAST request, end code ``0x0000`` -- so the setpoint read returns the scan
    counter with no error anywhere (measured on FX5U-32MT/DS fw 1.065, 2026-09-06). The
    second half of this test asserts that the **same** assertions fail: if the simulator
    could not reproduce the bug this library exists to prevent, the architecture would
    be unverified.
    """
    async with running(FX5U_32MT_DS, entries=(Entry(name="tcp", protocol="tcp"),)) as plc:
        plc.memory.set_u16("D", 0, SETPOINT)
        plc.memory.set_u16("D", 8, SCAN)
        host, port = plc.address("tcp")

        await assert_both_registers_read_correctly(await _gated(host, port))
        # This entry serves one connection at a time, exactly as the silicon does: the
        # slot frees when the CPU notices the close, and reconnecting before it has is
        # a different measured failure than the one under test here.
        await asyncio.sleep(0.05)

        answers = await _ungated(host, port)
        with pytest.raises(AssertionError):
            await assert_both_registers_read_correctly(answers)

        first, second = answers
        assert first is not None
        assert second is None, "only one response comes back"
        end_code = BINARY.read_number(first, THREE_E.prefix_units(BINARY), bits=16)
        assert end_code == 0x0000, "and it reports success"
        assert first[THREE_E.data_offset(BINARY) :] == BINARY.words((SCAN,)), (
            "the one response carries the SECOND request's register; a client that "
            "paired it with the first would return the scan counter as the setpoint"
        )
        assert plc.events_of("coalesced")


async def test_a_clean_target_cannot_prove_the_gate() -> None:
    """The negative control.

    On a CPU without the defect the ungated client also passes, which is exactly why the
    pathology has to be reproducible: a simulator that is only ever correct would let the
    gate be deleted without a single test going red.
    """
    async with running(
        PEDANTIC, pathology=HEALTHY, entries=(Entry(name="tcp", protocol="tcp"),)
    ) as plc:
        plc.memory.set_u16("D", 0, SETPOINT)
        plc.memory.set_u16("D", 8, SCAN)
        host, port = plc.address("tcp")
        await assert_both_registers_read_correctly(await _ungated(host, port))


async def test_a_4e_serial_turns_the_corruption_into_a_mismatch() -> None:
    """3E cannot detect this at all; 4E can, which is a measured reason to prefer it."""
    async with running(FX5U_32MT_DS) as plc:
        host, port = plc.address("tcp-4e")
        reader, writer = await asyncio.open_connection(host, port)
        try:
            for serial in (0x1000, 0x1001):
                payload = D0 + BINARY.number(1, bits=16)
                body = request_body(
                    BINARY,
                    monitoring_timer=0,
                    command=0x0401,
                    subcommand=0x0000,
                    payload=payload,
                )
                writer.write(
                    FOUR_E.build(
                        route=Route.OWN_STATION, body=body, codec=BINARY, serial=serial
                    )
                )
            await writer.drain()
            prefix = await asyncio.wait_for(
                reader.readexactly(FOUR_E.prefix_units(BINARY)), 1.0
            )
            echoed = BINARY.read_number(prefix, BINARY.number_len(16), bits=16)
            assert echoed == 0x1001, "the answer belongs to the LAST request"
        finally:
            writer.close()


# ======================================================================================
# The pytest plugin
# ======================================================================================


async def test_the_shipped_fixtures_wire_a_simulator_together(
    slmp_simulator: PlcSimulator,  # noqa: F811 - a fixture parameter, not a redefinition
    slmp_exchange: TcpExchange,  # noqa: F811
    slmp_target: SimulatorTarget,  # noqa: F811
) -> None:
    """``aslmp.testing.pytest_plugin`` is the whole thing in three fixtures.

    Imported here rather than loaded through a ``pytest11`` entry point: a plugin that
    installs itself into every session in the environment is a plugin that surprises
    somebody, and this one binds sockets.
    """
    assert slmp_target is FX5U_32MT_DS, "the CI default is our silicon"
    assert slmp_simulator.pathology is FX5U_32MT_DS.pathology
    slmp_simulator.memory.set_f32("D", 0, 60.0)
    report = await run_conformance(slmp_exchange, slmp_target, allow_writes=True)
    report.assert_clean()


# ======================================================================================
# Day-one regression, named after somebody else's defect
# ======================================================================================


async def test_3e_request_subheader_is_0x5000_not_0x0054() -> None:
    """ProtoForge's ``mc/server.py:56`` rejects every correct 3E client.

    It compares the constant ``0x0054`` against a little-endian unpack of the two
    subheader bytes. ``5000H`` goes on the wire as ``50 00`` -- the one binary field in
    the whole protocol that is not little-endian -- so a server that unpacks it gets
    ``0x0050`` and refuses. This simulator serves the correct bytes and refuses the
    byte-swapped ones.
    """
    assert THREE_E.request.binary == b"\x50\x00"
    async with running(
        PEDANTIC, entries=(Entry(name="tcp", protocol="tcp"),)
    ) as plc:
        host, port = plc.address("tcp")
        reader, writer = await asyncio.open_connection(host, port)
        try:
            good = read_frame_for(D0)
            writer.write(good)
            await writer.drain()
            answer = await asyncio.wait_for(
                reader.readexactly(THREE_E.prefix_units(BINARY)), 1.0
            )
            assert answer[:2] == b"\xd0\x00"
        finally:
            writer.close()

        reader, writer = await asyncio.open_connection(host, port)
        try:
            writer.write(b"\x00\x50" + read_frame_for(D0)[2:])
            await writer.drain()
            prefix = await asyncio.wait_for(
                reader.readexactly(THREE_E.prefix_units(BINARY)), 1.0
            )
            declared = BINARY.read_number(
                prefix, THREE_E.subheader_units(BINARY) + Route.wire_len(BINARY), bits=16
            )
            body = await asyncio.wait_for(reader.readexactly(declared), 1.0)
            end_code = BINARY.read_number(body, 0, bits=16)
            assert end_code == PEDANTIC.end_codes.ascii_into_binary_entry
        finally:
            writer.close()
        assert plc.events_of("coding_mismatch"), (
            "a byte-swapped subheader is not this entry's request subheader"
        )


# ======================================================================================
# The oracle that agreed with the defect
# ======================================================================================


@plc_block(base="D0")
class Loop(PlcBlock):
    """Two of the bench's five ``f32`` points. The clock is not a field; it rides along."""

    setpoint: F32
    process_value: F32


def clocked_client(simulator: PlcSimulator, entry: str, clock: PlcClockSource) -> Plc:
    """A real client, over the real transport, against a running simulator.

    The only place in this file that imports the client half of the package, and it is
    here on purpose: this test is about the *decode*, so both sides being independent --
    which is what the rest of the file buys -- is not what is under examination.
    """
    host, port = simulator.address(entry)
    configured = simulator.entry(entry)
    return Plc(
        host,
        port,
        profile="melsec:iq-f/fx5u",
        encoding=configured.encoding,
        frame=configured.frame,
        transport=TransportKind.TCP,
        timeout=2.0,
        plc_clock=clock,
    )


SCANS: Final = 4096
"""Counts to advance ``D8`` by. Any exact ``f32`` integer does; four seconds of bench."""

SCANS_AS_U32: Final = 0x45800000
"""``4096.0f``'s bit pattern, i.e. 1166016512 -- what a ``u32`` decode of D8 publishes."""


async def test_a_simulator_backed_f32_clock_catches_the_u32_decode() -> None:
    """The test that would have caught the shipped defect, now that the oracle can fail.

    Until 2026-09-07 the simulator's ``advance_scan`` wrote an **integer** double word to
    ``D8``, so a client that decoded that point as ``u32`` -- which is what
    :class:`~aslmp.client.PlcClockSource` did unconditionally -- read back exactly the
    count the test had put there. The oracle carried the same misreading as the code it
    was checking, and ~4100 tests plus two review rounds went green over it.

    ``D8`` on the bench is ``IO_Scan``, a ``REAL``: the CPU's own ST is
    ``IO_Scan := IO_Scan + 1.0`` (FX5U-32MT/DS fw 1.065 at 192.168.10.250, 2026-09-07).
    The simulator holds one now, so the two declarations disagree here exactly as they
    disagree on the wire -- and the ``f32`` arm of this test fails if the clock is
    decoded as ``u32``, which is the property that was missing.

    Both arms answer end code ``0x0000``. Nothing on the wire distinguishes them; only
    the declaration does.
    """
    async with running(FX5U_32MT_DS) as simulator:
        simulator.memory.set_f32("D", 0, 60.0)
        simulator.memory.set_f32("D", 2, 0.0)
        simulator.memory.set_f32("D", 8, 0.0)
        for _ in range(SCANS):
            simulator.dispatcher.advance_scan()
        assert simulator.memory.get_f32("D", 8) == float(SCANS), "the CPU counted 4096"

        declared_real = PlcClockSource(
            "D8", kind="f32", bounds=Bounds(0.0, BENCH_SCAN_WRAP), label="scan"
        )
        async with clocked_client(simulator, "tcp", declared_real) as plc:
            right = await bind(plc, Loop).read()

        async with clocked_client(simulator, "tcp-4e", PlcClockSource("D8", kind="u32")) as plc:
            wrong = await bind(plc, Loop).read()

    assert right.tx is not None and wrong.tx is not None
    assert right.setpoint == wrong.setpoint == 60.0, "the block itself decodes either way"

    # The assertion the missing test was missing. Decode D8 as u32 and this fails.
    assert right.tx.plc_clock == SCANS

    # And the same registers, declared u32, publish a plausible useless number instead.
    assert wrong.tx.plc_clock == SCANS_AS_U32
    assert wrong.tx.plc_clock > BENCH_SCAN_WRAP, (
        "285 thousand times the count, rising monotonically, end code 0x0000"
    )


async def test_a_declared_bound_is_the_only_thing_that_catches_the_next_one() -> None:
    """A register carries no type, so the bound is the defence -- and it does fire.

    Same wrong declaration as above, this time with the range the bench's own ST implies
    (``IF IO_Scan > 1.0E7``). The client refuses rather than publishing, and names the
    point, its address and the registers it came from.
    """
    async with running(FX5U_32MT_DS) as simulator:
        simulator.memory.set_f32("D", 0, 60.0)
        simulator.memory.set_f32("D", 2, 0.0)
        simulator.memory.set_f32("D", 8, float(SCANS))
        bounded_wrong = PlcClockSource(
            "D8", kind="u32", bounds=Bounds(0, int(BENCH_SCAN_WRAP)), label="scan"
        )
        async with clocked_client(simulator, "tcp", bounded_wrong) as plc:
            with pytest.raises(SlmpImplausibleValueError) as caught:
                await bind(plc, Loop).read()

    assert caught.value.field == "scan"
    assert caught.value.address == "D8"
    assert caught.value.value == SCANS_AS_U32
