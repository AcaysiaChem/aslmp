"""It must always be possible to tear a simulator down. That is the whole file.

A test double that can hang its caller's suite is worse than no test double, because the
failure it produces is the least informative one available: pytest simply stops, with no
test named and no stack, and on CI the job runs until somebody's timeout kills it.

``PlcSimulator.aclose()`` could do exactly that, and did. ``Server.wait_closed()`` waits
for every ACCEPTED connection to detach, and there is a window in which one exists that
the simulator has never been told about: asyncio attaches a new transport to the server
inside the transport's constructor and only then schedules ``connection_made``, so
between those two steps the connection is in ``server._clients`` while the handler that
would register its writer has not run. ``aclose()`` closes the writers it knows about,
``server.close()`` only stops listening, and nobody closes that one.

Measured 2026-09-24, Python 3.13.14 on Windows, by connecting a raw blocking socket and
stepping the loop a controlled number of times before calling ``aclose()``::

    yields=0  server._clients=0  bound.writers=0  -> returned
    yields=2  server._clients=1  bound.writers=0  -> HUNG
    yields=5  server._clients=1  bound.writers=1  -> returned

It stayed hidden because ``wait_closed()`` returned immediately before CPython 3.12.1 and
only waits from 3.12.1 on. On the nine-cell matrix at commit d78ef84 that produced a
pattern no single-cell CI could have read: every 3.12 cell hung, no 3.11 cell hung, and
3.13 hung only where the runner happened to land in the window.
"""

from __future__ import annotations

import asyncio

# A blocking connect is the whole technique here: it completes the handshake in the
# kernel without the event loop, which is what makes the window steppable. asyncio's own
# open_connection yields, and yielding is exactly what closes the window.
import socket  # noqa: TID251

from aslmp.testing.server import PlcSimulator

# Long enough that a slow runner is never mistaken for a hang, short enough that a real
# hang fails this test in seconds instead of stopping the suite.
_PATIENCE = 10.0

# The window is one loop iteration wide, and WHICH iteration depends on the event loop:
# proactor on Windows and selector elsewhere do not accept on the same step. So the test
# sweeps rather than guessing -- whatever the window is, aclose() must survive all of it.
_STEPS = range(6)


async def test_aclose_returns_however_late_a_connection_was_accepted() -> None:
    """Sweep the accept window. Every position must tear down, none may hang."""
    hung: list[int] = []
    for steps in _STEPS:
        simulator = PlcSimulator()
        await simulator.start()
        host, port = simulator.address("tcp")
        # A BLOCKING connect: the kernel completes the handshake with no event-loop
        # involvement, so the yields below decide exactly how far asyncio has got.
        raw = socket.create_connection((host, port))
        try:
            for _ in range(steps):
                await asyncio.sleep(0)
            try:
                await asyncio.wait_for(simulator.aclose(), timeout=_PATIENCE)
            except TimeoutError:
                hung.append(steps)
        finally:
            raw.close()
    assert not hung, (
        f"aclose() did not return within {_PATIENCE:g} s when the connection was "
        f"accepted at loop step(s) {hung}. A connection accepted but not yet announced "
        f"to the handler is invisible to aclose(), and Server.wait_closed() waits for it "
        f"forever from CPython 3.12.1 on. See PlcSimulator._finish_closing."
    )


async def test_aclose_returns_with_a_fully_established_connection() -> None:
    """The ordinary case, so the sweep above cannot pass by never connecting at all."""
    simulator = PlcSimulator()
    await simulator.start()
    host, port = simulator.address("tcp")
    reader, writer = await asyncio.open_connection(host, port)
    assert reader is not None
    try:
        await asyncio.wait_for(simulator.aclose(), timeout=_PATIENCE)
    finally:
        writer.close()


async def test_aclose_is_idempotent() -> None:
    """Tearing down twice must not raise, and must not wait a second time."""
    simulator = PlcSimulator()
    await simulator.start()
    await asyncio.wait_for(simulator.aclose(), timeout=_PATIENCE)
    await asyncio.wait_for(simulator.aclose(), timeout=_PATIENCE)
