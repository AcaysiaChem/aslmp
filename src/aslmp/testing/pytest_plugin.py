"""Pytest fixtures for the conformance simulator.

Layer 2.5 (``aslmp.testing``). **Imports pytest**, which is the only module in this
package that does -- the ``aslmp[testing]`` extra has no dependencies, so the simulator
itself must be usable from a plain script, a REPL or somebody else's test runner.

Two ways to get the fixtures. Either load the module as a plugin from your rootdir
``conftest.py``::

    pytest_plugins = ["aslmp.testing.pytest_plugin"]

or import the fixtures you want into a test module, which registers them just as well::

    from aslmp.testing.pytest_plugin import slmp_simulator, slmp_target

Both work. There is deliberately no ``pytest11`` entry point: a plugin that installs
itself into every test session in the environment is a plugin that surprises somebody,
and this one binds sockets.

Override :func:`slmp_target`, :func:`slmp_pathology` or :func:`slmp_entries` in your own
conftest to point the whole set at a different CPU, board or port map -- that is the
supported way to run the same suite against ``PEDANTIC`` and ``FX5U_32MT_DS`` and diff
the results.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from aslmp.profile import Encoding
from aslmp.testing.conformance import TcpExchange, context_for
from aslmp.testing.server import BENCH_ENTRIES, Entry, PlcSimulator, codec_for
from aslmp.testing.targets import FX5U_32MT_DS

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import AsyncIterator, Sequence

    from aslmp.commands.base import EncodeContext
    from aslmp.testing.pathology import Pathology
    from aslmp.testing.targets import SimulatorTarget

__all__ = [
    "slmp_context",
    "slmp_entries",
    "slmp_exchange",
    "slmp_pathology",
    "slmp_simulator",
    "slmp_target",
]


@pytest.fixture
def slmp_target() -> SimulatorTarget:
    """Which CPU the simulator pretends to be. Override to run the suite elsewhere.

    Defaults to :data:`~aslmp.testing.targets.FX5U_32MT_DS`, the CI default, because a
    test that passes against a CPU nobody owns is a test about a document.
    """
    return FX5U_32MT_DS


@pytest.fixture
def slmp_pathology(slmp_target: SimulatorTarget) -> Pathology:
    """The pathology board in force. Defaults to the target's own measured one."""
    return slmp_target.pathology


@pytest.fixture
def slmp_entries() -> Sequence[Entry]:
    """The connection entries to bind, all on ephemeral ports."""
    return BENCH_ENTRIES


@pytest_asyncio.fixture
async def slmp_simulator(
    slmp_target: SimulatorTarget,
    slmp_pathology: Pathology,
    slmp_entries: Sequence[Entry],
) -> AsyncIterator[PlcSimulator]:
    """A started simulator, closed at the end of the test."""
    simulator = PlcSimulator(
        target=slmp_target, entries=slmp_entries, pathology=slmp_pathology
    )
    await simulator.start()
    try:
        yield simulator
    finally:
        await simulator.aclose()


@pytest_asyncio.fixture
async def slmp_exchange(slmp_simulator: PlcSimulator) -> AsyncIterator[TcpExchange]:
    """One TCP connection to the ``tcp`` entry, one transaction at a time."""
    entry = slmp_simulator.entry("tcp")
    host, port = slmp_simulator.address("tcp")
    exchange = await TcpExchange.connect(
        host, port, frame=entry.frame_format, codec=codec_for(entry.encoding)
    )
    try:
        yield exchange
    finally:
        await exchange.aclose()


@pytest.fixture
def slmp_context(slmp_target: SimulatorTarget) -> EncodeContext:
    """A binary, short-specification encoding context for the target's profile."""
    return context_for(slmp_target, encoding=Encoding.BINARY)
