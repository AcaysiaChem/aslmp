"""The conformance suite, end to end, and the one test the architecture rests on.

Three things happen in this file.

1. The shipped suite is run against every target, over TCP and over UDP, and must come
   back clean. Nothing here imports ``aslmp.transport``: the suite is its own client, so
   a transport bug is visible rather than cancelled out.
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
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

import pytest

from aslmp.testing.conformance import (
    TcpExchange,
    UdpExchange,
    run_conformance,
    standard_cases,
)
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
