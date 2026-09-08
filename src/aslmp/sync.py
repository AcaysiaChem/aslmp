"""Layer 7 -- the same client, without ``async``, over **one** background event loop.

.. rubric:: What this is not

It is not ``asyncio.run`` behind a synchronous name. That is the shape most sync facades
take and it is wrong here for a reason this hardware makes concrete: ``asyncio.run``
creates and destroys an event loop per call, which means a new socket per call, which
means a new TCP connection to an SLMP entry that **serves one connection at a time**. A
second connection to a one-entry configuration completes its handshake in 5.4 ms and is
then FINed by the CPU with the incumbent undisturbed (FX5U-32MT/DS fw 1.065,
2026-09-06). A per-call loop would therefore either reconnect on every read -- paying a
handshake this library measures at ~7 ms and hiding it inside the read's latency -- or
fail against its own previous call.

.. rubric:: What it is

One :class:`Plc` owns one event loop on one thread named ``aslmp-<name>``, from
construction until :meth:`Plc.close`. Every call is
:func:`asyncio.run_coroutine_threadsafe` onto that loop, and the calling thread blocks on
the result. The loop is the client's home: the socket, the in-flight gate and the
timing stamps all live on it, and nothing about them changes because the caller is
synchronous.

.. rubric:: Constructing one inside a running loop raises

If there is already an event loop running on this thread, you are in async code and
:class:`aslmp.client.Plc` is the class you want. Building a sync facade there would put a
second loop on a second thread and then block the first one on it -- the classic way to
deadlock an application that was working. So it refuses, and says which class to use.

.. rubric:: What is deliberately missing

``plc.remote``, ``plc.timed`` and ``plc.events()`` are not mirrored. The first two hand
back objects whose methods are coroutines and the third is an async iterator; wrapping
them would mean three more facades, and remote control in particular is the one surface
where a caller should be looking at the async API and its interlocks directly.

.. note::

   DESIGN.md section 4.8 wants this file generated from ``client.py``'s AST the way
   ``aslmp/timed.py`` is, with CI asserting that regenerating it is a byte-for-byte
   no-op. ``tools/`` belongs to another build unit; the mirrored block below was produced
   mechanically from the same AST and must be regenerated the same way, so
   ``tools/gen_sync.py`` and its regeneration test are still owed.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from collections.abc import Coroutine, Mapping, Sequence
from types import TracebackType
from typing import Any, Final, Literal, Self, TypeVar, final, overload

from aslmp.client import Handshake, MonitoringTimer, PlcClockSource
from aslmp.client import Plc as AsyncPlc
from aslmp.commands.base import AddressLike, WordOrder
from aslmp.commands.block import BlockSpec, BlockWrite
from aslmp.commands.info import DEFAULT_LOOPBACK
from aslmp.commands.monitor import MonitorRegistration
from aslmp.commands.random import RandomPoint, RandomWrite
from aslmp.connection import ConnectionInfo, ConnectionState
from aslmp.errors import SlmpConfigurationError, SlmpNotConnectedError
from aslmp.identity import CpuIdentity, CpuStatus
from aslmp.observability import Counters, EventSink, MetricsSnapshot
from aslmp.profile import Capability, CpuProfile, Encoding, Link
from aslmp.results import RandomReading, SplitReading
from aslmp.timing import Clock, TransactionSink
from aslmp.transport.base import TransportKind
from aslmp.transport.inflight import Concurrency
from aslmp.wire.codec import SpecFormat
from aslmp.wire.frames import FrameType
from aslmp.wire.raw import RawResponse
from aslmp.wire.route import Route

__all__ = ["Plc", "shutdown"]

T = TypeVar("T")

_START_TIMEOUT: Final = 5.0
"""How long to wait for the background loop's thread to come up. A thread that has not
started an event loop in five seconds is not going to."""

_RUNTIMES: Final[list[_Runtime]] = []
_REGISTRY_LOCK: Final = threading.Lock()


def _running_loop() -> asyncio.AbstractEventLoop | None:
    """The event loop running on **this** thread, or ``None``.

    ``contextlib.suppress`` rather than ``except RuntimeError: return None`` because a
    falsy return out of an exception handler is exactly the shape this library refuses
    everywhere else, and the rule should not have an exception for its own plumbing.
    """
    loop: asyncio.AbstractEventLoop | None = None
    with contextlib.suppress(RuntimeError):
        loop = asyncio.get_running_loop()
    return loop


@final
class _Runtime:
    """One event loop, on one thread, owned for the lifetime of one facade.

    Not part of the public surface. Its whole job is that ``submit`` is cheap and that
    the loop it submits to is the *same* loop every time, because on this hardware a
    second loop means a second socket and a second socket means a FIN.
    """

    __slots__ = ("_closed", "_loop", "_thread")

    def __init__(self, name: str) -> None:
        if _running_loop() is not None:
            raise SlmpConfigurationError(
                f"aslmp.sync.Plc({name!r}) was constructed inside a running event loop. "
                f"You are already in async code: use aslmp.client.Plc, which is the same "
                f"API with 'await'. Starting a second loop on a second thread and then "
                f"blocking this one on it is how a working application deadlocks."
            )
        self._loop: asyncio.AbstractEventLoop | None = None
        self._closed = False
        started = threading.Event()
        self._thread = threading.Thread(
            target=self._serve, args=(started,), name=f"aslmp-{name}", daemon=True
        )
        self._thread.start()
        if not started.wait(_START_TIMEOUT):  # pragma: no cover - a stuck interpreter
            raise SlmpConfigurationError(
                f"the background event loop thread aslmp-{name} did not start within "
                f"{_START_TIMEOUT:g} s."
            )
        with _REGISTRY_LOCK:
            _RUNTIMES.append(self)

    @property
    def thread_name(self) -> str:
        return self._thread.name

    @property
    def closed(self) -> bool:
        return self._closed

    def _serve(self, started: threading.Event) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        started.set()
        try:
            loop.run_forever()
        finally:
            self._drain(loop)
            asyncio.set_event_loop(None)
            loop.close()

    @staticmethod
    def _drain(loop: asyncio.AbstractEventLoop) -> None:
        """Cancel what is left and let it finish, rather than closing over live tasks."""
        pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(
                asyncio.gather(*pending, return_exceptions=True)
            )
        loop.run_until_complete(loop.shutdown_asyncgens())

    def submit(self, coro: Coroutine[Any, Any, T]) -> T:
        """Run ``coro`` on the background loop and block this thread for its result."""
        loop = self._loop
        if loop is None or self._closed:
            coro.close()
            raise SlmpNotConnectedError(
                f"the background loop {self._thread.name} has been shut down; this "
                f"client cannot be used again. Construct a new aslmp.sync.Plc.",
                reason="shutdown",
            )
        if threading.current_thread() is self._thread:
            coro.close()
            raise SlmpConfigurationError(
                f"a synchronous aslmp call was made from inside {self._thread.name}, "
                f"the background loop's own thread -- normally from an on_transaction or "
                f"on_event callback. Blocking that thread on the loop it is running "
                f"deadlocks it. Callbacks must not call back into the client."
            )
        return asyncio.run_coroutine_threadsafe(coro, loop).result()

    def shutdown(self, timeout: float = 5.0) -> None:
        """Stop the loop and join its thread. Idempotent; raises if the thread hangs."""
        if self._closed:
            return
        self._closed = True
        with _REGISTRY_LOCK:
            if self in _RUNTIMES:
                _RUNTIMES.remove(self)
        loop = self._loop
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)
        self._thread.join(timeout)
        if self._thread.is_alive():  # pragma: no cover - a wedged callback
            raise SlmpConfigurationError(
                f"{self._thread.name} did not stop within {timeout:g} s. Something on "
                f"that loop is not yielding; this is reported rather than left as a "
                f"thread that outlives the process's own idea of shutdown."
            )


def shutdown(timeout: float = 5.0) -> None:
    """Stop every background loop this module still owns.

    A last resort for an interpreter shutting down, not the normal path: the normal path
    is ``with Plc(...) as plc:`` or :meth:`Plc.close`. Every runtime is given the full
    ``timeout``, and the first one that will not stop raises -- a facade that reported a
    clean shutdown while a thread kept running would be the same lie in a different
    place.
    """
    with _REGISTRY_LOCK:
        runtimes = list(_RUNTIMES)
    for runtime in runtimes:
        runtime.shutdown(timeout)


@final
class Plc:
    """:class:`aslmp.client.Plc`, synchronously, over one loop owned for its lifetime.

    ::

        with Plc("192.168.10.250", 5002, profile="melsec:iq-f/fx5u") as plc:
            pv = plc.read_f32("D2")
            plc.write_f32("D100", 1234.5)

    Every parameter is the async client's, unchanged, including the three declared
    connection-entry constants -- ``transport``, ``frame`` and ``encoding`` -- which are
    never auto-detected here either.
    """

    __slots__ = ("_plc", "_runtime")

    def __init__(
        self,
        host: str,
        port: int = 5000,
        *,
        profile: CpuProfile | str,
        transport: TransportKind = TransportKind.TCP,
        frame: FrameType = FrameType.THREE_E,
        encoding: Encoding = Encoding.BINARY,
        link: Link = Link.CPU_BUILTIN,
        route: Route = Route.OWN_STATION,
        spec: SpecFormat | None = None,
        timeout: float = 3.0,
        connect_timeout: float = 3.0,
        monitoring_timer: MonitoringTimer = MonitoringTimer.INDEFINITE,
        handshake: Handshake = Handshake.SELF_TEST_AND_IDENTIFY,
        concurrency: Concurrency = Concurrency.STRICT,
        validate_ranges: bool = True,
        word_order: WordOrder = WordOrder.LOW_FIRST,
        tcp_nodelay: bool = True,
        udp_pipeline_depth: int = 1,
        allow_remote_control: bool = False,
        capability_overrides: Mapping[Capability, str] | None = None,
        plc_clock: PlcClockSource | None = None,
        capture_frames: bool = False,
        clock: Clock = time.monotonic_ns,
        on_transaction: TransactionSink | None = None,
        on_event: EventSink | None = None,
        name: str | None = None,
    ) -> None:
        # The runtime first: it is what refuses to be built inside a running loop, and
        # refusing before a client object exists means there is nothing half-built to
        # clean up. The async client itself opens no socket at construction.
        self._runtime = _Runtime(name if name is not None else f"{host}:{port}")
        self._plc = AsyncPlc(
            host,
            port,
            profile=profile,
            transport=transport,
            frame=frame,
            encoding=encoding,
            link=link,
            route=route,
            spec=spec,
            timeout=timeout,
            connect_timeout=connect_timeout,
            monitoring_timer=monitoring_timer,
            handshake=handshake,
            concurrency=concurrency,
            validate_ranges=validate_ranges,
            word_order=word_order,
            tcp_nodelay=tcp_nodelay,
            udp_pipeline_depth=udp_pipeline_depth,
            allow_remote_control=allow_remote_control,
            capability_overrides=capability_overrides,
            plc_clock=plc_clock,
            capture_frames=capture_frames,
            clock=clock,
            on_transaction=on_transaction,
            on_event=on_event,
            name=name,
        )

    # -- what this facade owns -----------------------------------------------

    @property
    def thread_name(self) -> str:
        """The background loop's thread name: ``aslmp-<name>``. One per facade."""
        return self._runtime.thread_name

    @property
    def asynchronous(self) -> AsyncPlc:
        """The wrapped async client. **Only touch it from the background loop.**

        Present because a caller occasionally needs the surfaces this facade does not
        mirror -- ``remote``, ``timed``, ``events()`` -- and hiding it would send them to
        a private attribute anyway. Calling one of its coroutines from the calling thread
        does nothing at all; it must be scheduled onto ``thread_name``'s loop.
        """
        return self._plc

    # -- inspection, delegated ------------------------------------------------

    @property
    def peer(self) -> tuple[str, int]:
        """``(host, port)`` as configured."""
        return self._plc.peer

    @property
    def model(self) -> str | None:
        """The model name the CPU gave, or ``None`` before the identify handshake."""
        return self._plc.model

    @property
    def model_code(self) -> int | None:
        """The ``0x0101`` model code, or ``None``. Never guessed from the profile."""
        return self._plc.model_code

    @property
    def transport(self) -> TransportKind:
        """The configured transport. A GX Works3 connection-entry fact."""
        return self._plc.transport

    @property
    def encoding(self) -> Encoding:
        """The configured Communication Data Code. An own-node parameter."""
        return self._plc.encoding

    @property
    def frame(self) -> FrameType:
        """The configured frame format. A GX Works3 connection-entry fact."""
        return self._plc.frame

    @property
    def name(self) -> str:
        """A label for this client, for a thread name and for a diagnostic."""
        return self._plc.name

    @property
    def profile(self) -> CpuProfile:
        """The declared profile. Never switched under the caller."""
        return self._plc.profile

    @property
    def state(self) -> ConnectionState:
        """Where the connection is. ``FAILED`` is sticky and never left implicitly."""
        return self._plc.state

    @property
    def generation(self) -> int:
        """Bumps on every reconnect and every UDP rebind. The anti-lie field."""
        return self._plc.generation

    @property
    def identity(self) -> CpuIdentity | None:
        """Which CPU answered ``0x0101``, or ``None`` if it was never asked."""
        return self._plc.identity

    @property
    def info(self) -> ConnectionInfo | None:
        """The record of the established connection, with the handshake's own timing."""
        return self._plc.info

    @property
    def counters(self) -> Counters:
        """The live tallies. Never reset by this library."""
        return self._plc.counters

    @property
    def plc_clock(self) -> PlcClockSource | None:
        """The configured PLC-side counter, for a bound block plan to fold in."""
        return self._plc.plc_clock

    def metrics(self) -> MetricsSnapshot:
        """One frozen view of the counters and the latency window."""
        return self._plc.metrics()

    def add_event_listener(self, listener: EventSink) -> None:
        """Add a connection-event callback. **It runs on the background loop's thread.**"""
        self._plc.add_event_listener(listener)

    def __repr__(self) -> str:
        return f"aslmp.sync.{self._plc!r} on {self._runtime.thread_name}"

    # -- lifecycle -------------------------------------------------------------

    def connect(self) -> ConnectionInfo:
        """Open the socket and prove it with ``0x0619`` and ``0x0101``."""
        return self._runtime.submit(self._plc.connect())

    def reconnect(self, *, reason: str) -> ConnectionInfo:
        """Rebuild the socket and re-prove it. **Never implicit**, here either."""
        return self._runtime.submit(self._plc.reconnect(reason=reason))

    def close(self, *, timeout: float = 5.0) -> None:
        """Close the connection, then stop and join this facade's loop thread.

        Idempotent, and the loop does not survive it: the facade owns that loop for its
        lifetime and this is the end of the lifetime.
        """
        if not self._runtime.closed:
            self._runtime.submit(self._plc.aclose())
        self._runtime.shutdown(timeout)

    def __enter__(self) -> Self:
        self.connect()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        self.close()

    # ====================================================================================
    # The mirrored surface. Same names, same parameters, no `async`.
    # ====================================================================================

    def read_bit(self, address: AddressLike, /) -> bool:
        """One bit device, as a ``bool``. ``0x0401`` in bit units."""
        return self._runtime.submit(self._plc.read_bit(address))

    def read_i16(
        self,
        address: AddressLike,
        /,
        *,
        minimum: float | None = None,
        maximum: float | None = None,
    ) -> int:
        """One register as a signed 16-bit integer.

        ``minimum`` and ``maximum`` are the optional plausibility bounds of the
        async call of the same name, and mean exactly what they mean there.
        """
        return self._runtime.submit(
            self._plc.read_i16(address, minimum=minimum, maximum=maximum)
        )

    def read_u16(
        self,
        address: AddressLike,
        /,
        *,
        minimum: float | None = None,
        maximum: float | None = None,
    ) -> int:
        """One register as an unsigned 16-bit integer.

        ``minimum`` and ``maximum`` are the optional plausibility bounds of the
        async call of the same name, and mean exactly what they mean there.
        """
        return self._runtime.submit(
            self._plc.read_u16(address, minimum=minimum, maximum=maximum)
        )

    def read_i32(
        self,
        address: AddressLike,
        /,
        *,
        word_order: WordOrder | None = None,
        minimum: float | None = None,
        maximum: float | None = None,
    ) -> int:
        """Two consecutive registers as a signed 32-bit integer.

        ``minimum`` and ``maximum`` are the optional plausibility bounds of the
        async call of the same name, and mean exactly what they mean there.
        """
        return self._runtime.submit(
            self._plc.read_i32(
                address, word_order=word_order, minimum=minimum, maximum=maximum
            )
        )

    def read_u32(
        self,
        address: AddressLike,
        /,
        *,
        word_order: WordOrder | None = None,
        minimum: float | None = None,
        maximum: float | None = None,
    ) -> int:
        """Two consecutive registers as an unsigned 32-bit integer.

        ``minimum`` and ``maximum`` are the optional plausibility bounds of the
        async call of the same name, and mean exactly what they mean there.
        """
        return self._runtime.submit(
            self._plc.read_u32(
                address, word_order=word_order, minimum=minimum, maximum=maximum
            )
        )

    def read_f32(
        self,
        address: AddressLike,
        /,
        *,
        word_order: WordOrder | None = None,
        minimum: float | None = None,
        maximum: float | None = None,
    ) -> float:
        """Two consecutive registers as one IEEE-754 single.

        Low word first, measured four ways on FX5U-32MT/DS fw 1.065: 1234.5 written as
        one double-word point put ``00 50 9A 44`` on the wire and read back
        ``D104 = 0x5000``, ``D105 = 0x449A`` (2026-09-06).

        ``minimum`` and ``maximum`` are the optional plausibility bounds of the
        async call of the same name, and mean exactly what they mean there.
        """
        return self._runtime.submit(
            self._plc.read_f32(
                address, word_order=word_order, minimum=minimum, maximum=maximum
            )
        )

    def read_f64(
        self,
        address: AddressLike,
        /,
        *,
        word_order: WordOrder | None = None,
        minimum: float | None = None,
        maximum: float | None = None,
    ) -> float:
        """Four consecutive registers as one IEEE-754 double.

        ``minimum`` and ``maximum`` are the optional plausibility bounds of the
        async call of the same name, and mean exactly what they mean there.
        """
        return self._runtime.submit(
            self._plc.read_f64(
                address, word_order=word_order, minimum=minimum, maximum=maximum
            )
        )

    def read_str(
        self, address: AddressLike, /, *, length: int, encoding: str = "ascii"
    ) -> str:
        """``length`` characters packed two per register, trimmed at the first NUL.

        ``length`` is required and is in characters. A string region has no in-band
        length, so the alternatives to naming it are reading a fixed maximum -- which
        returns the next field's bytes -- or scanning for a NUL, which is a second round
        trip whose answer can change between the two.
        """
        return self._runtime.submit(
            self._plc.read_str(address, length=length, encoding=encoding)
        )

    def write_bit(
        self, address: AddressLike, value: bool, /, *, verify: bool = False
    ) -> None:
        """One bit device. ``0x1401`` in bit units."""
        self._runtime.submit(self._plc.write_bit(address, value, verify=verify))

    def write_i16(
        self, address: AddressLike, value: int, /, *, verify: bool = False
    ) -> None:
        """One register from a signed 16-bit integer. Never masked, never clamped."""
        self._runtime.submit(self._plc.write_i16(address, value, verify=verify))

    def write_u16(
        self, address: AddressLike, value: int, /, *, verify: bool = False
    ) -> None:
        """One register from an unsigned 16-bit integer."""
        self._runtime.submit(self._plc.write_u16(address, value, verify=verify))

    def write_i32(
        self,
        address: AddressLike,
        value: int,
        /,
        *,
        word_order: WordOrder | None = None,
        verify: bool = False,
    ) -> None:
        """Two registers from a signed 32-bit integer."""
        self._runtime.submit(
            self._plc.write_i32(address, value, word_order=word_order, verify=verify)
        )

    def write_u32(
        self,
        address: AddressLike,
        value: int,
        /,
        *,
        word_order: WordOrder | None = None,
        verify: bool = False,
    ) -> None:
        """Two registers from an unsigned 32-bit integer."""
        self._runtime.submit(
            self._plc.write_u32(address, value, word_order=word_order, verify=verify)
        )

    def write_f32(
        self,
        address: AddressLike,
        value: float,
        /,
        *,
        word_order: WordOrder | None = None,
        verify: bool = False,
    ) -> None:
        """Two registers from one IEEE-754 single, low word first."""
        self._runtime.submit(
            self._plc.write_f32(address, value, word_order=word_order, verify=verify)
        )

    def write_f64(
        self,
        address: AddressLike,
        value: float,
        /,
        *,
        word_order: WordOrder | None = None,
        verify: bool = False,
    ) -> None:
        """Four registers from one IEEE-754 double."""
        self._runtime.submit(
            self._plc.write_f64(address, value, word_order=word_order, verify=verify)
        )

    def write_str(
        self,
        address: AddressLike,
        value: str,
        /,
        *,
        length: int,
        encoding: str = "ascii",
        verify: bool = False,
    ) -> None:
        """``length`` characters, NUL padded, two per register.

        A string longer than ``length`` raises rather than being truncated to fit: a
        silently shortened part number is a wrong part number.
        """
        self._runtime.submit(
            self._plc.write_str(address, value, length=length, encoding=encoding, verify=verify)
        )

    def read_words(self, address: AddressLike, /, count: int) -> tuple[int, ...]:
        """``count`` consecutive registers, as unsigned words. Batch ceiling 960 words."""
        return self._runtime.submit(self._plc.read_words(address, count))

    def read_bits(self, address: AddressLike, /, count: int) -> tuple[bool, ...]:
        """``count`` consecutive bit devices. Batch ceiling 3584 bits on an FX5U."""
        return self._runtime.submit(self._plc.read_bits(address, count))

    def read_f32_array(
        self, address: AddressLike, /, count: int, *, word_order: WordOrder | None = None
    ) -> tuple[float, ...]:
        """``count`` IEEE-754 singles from ``2 * count`` consecutive registers."""
        return self._runtime.submit(
            self._plc.read_f32_array(address, count, word_order=word_order)
        )

    def write_words(self, address: AddressLike, values: Sequence[int], /) -> None:
        """``len(values)`` consecutive registers."""
        self._runtime.submit(self._plc.write_words(address, values))

    def write_bits(self, address: AddressLike, values: Sequence[bool], /) -> None:
        """``len(values)`` consecutive bit devices."""
        self._runtime.submit(self._plc.write_bits(address, values))

    def write_f32_array(
        self,
        address: AddressLike,
        values: Sequence[float],
        /,
        *,
        word_order: WordOrder | None = None,
    ) -> None:
        """``len(values)`` IEEE-754 singles into ``2 * len(values)`` registers."""
        self._runtime.submit(self._plc.write_f32_array(address, values, word_order=word_order))

    @overload
    def read_random(
        self, points: Sequence[RandomPoint], /, *, allow_split: Literal[False] = False
    ) -> RandomReading: ...

    @overload
    def read_random(
        self, points: Sequence[RandomPoint], /, *, allow_split: Literal[True]
    ) -> RandomReading | SplitReading: ...

    def read_random(
        self, points: Sequence[RandomPoint], /, *, allow_split: bool = False
    ) -> RandomReading | SplitReading:
        """Scattered word and double-word points in **one** snapshot. Results POSITIONAL.

        The wire demands every word specification before every double-word one and
        carries no framing between the groups; the command sorts, encodes, decodes and
        restores the caller's order, so ``reading[2]`` is the third point as written.

        ``allow_split=True`` returns a **different type** when the request does not fit
        the CPU's point ceiling, because splitting destroys the single-snapshot atomicity
        that is the entire reason to send a ``0403``. With the default
        ``allow_split=False`` an over-budget request raises
        :class:`~aslmp.errors.SlmpPointLimitError` and nothing is sent.
        """
        # Branched rather than forwarded: the async twin is @overload-ed on a
        # Literal, and a `bool` matches neither variant. The branch is what keeps the
        # narrow return type the overloads promise.
        if allow_split:
            return self._runtime.submit(self._plc.read_random(points, allow_split=True))
        return self._runtime.submit(self._plc.read_random(points, allow_split=False))

    def write_random(self, writes: Sequence[RandomWrite], /) -> None:
        """Scattered word and double-word writes in one ``1402``.

        The budget is weighted, not flat: ``word x 12 + dword x 14 <= 1920`` on an FX5U
        (measured). A flat count is wrong in both directions -- 160 word points fit and
        138 double-word points do not.
        """
        self._runtime.submit(self._plc.write_random(writes))

    def read_blocks(
        self, blocks: Sequence[BlockSpec], /
    ) -> tuple[tuple[int, ...], ...]:
        """Several contiguous runs in one frame; one tuple of words per block, in order."""
        return self._runtime.submit(self._plc.read_blocks(blocks))

    def write_blocks(self, blocks: Sequence[BlockWrite], /) -> None:
        """Several contiguous runs written in one frame."""
        self._runtime.submit(self._plc.write_blocks(blocks))

    def monitor_register(
        self, points: Sequence[RandomPoint], /
    ) -> MonitorRegistration:
        """``0801``: register a point list for later ``0802`` reads.

        Refused pre-transport on an iQ-F, which answers ``0xC059`` -- not the ``0xC05D``
        "monitor not registered" a reader of the generic reference would expect. It is
        never emulated with a ``0403``: substituting a different command that returns
        similar-looking data is exactly the silent recovery this library forbids.
        """
        return self._runtime.submit(self._plc.monitor_register(points))

    def monitor_read(
        self, registration: MonitorRegistration, /
    ) -> RandomReading:
        """``0802``: read the registered list. Positional, exactly like ``read_random``."""
        return self._runtime.submit(self._plc.monitor_read(registration))

    def self_test(self, payload: bytes = DEFAULT_LOOPBACK, /) -> bytes:
        """``0619``: ask the module to echo ``payload``. Zero side effects.

        The default is ``b"ABCD"`` -- ``41 42 43 44``, the exact bytes proven on the
        bench. Both manuals restrict loopback data to ``'0'``-``'9'`` and ``'A'``-``'F'``
        and ``Codec.loopback_payload_ok`` enforces it, against our own defaults included.
        """
        return self._runtime.submit(self._plc.self_test(payload))

    def ping(self) -> float:
        """One ``0619`` round trip, in milliseconds. The liveness probe.

        Zero side effects, and its latency equals a real read (~7 ms on FX5U-32MT/DS fw
        1.065), which is what makes it a usable baseline rather than a lower bound.
        """
        return self._runtime.submit(self._plc.ping())

    def read_type_name(self) -> CpuIdentity:
        """``0101``: which CPU is on the other end, with its family resolved.

        The family comes from the profile that claims the model code, never from parsing
        the name: the name is a marketing string whose shape has changed between
        families, and the model code is what decides whether ``Y20`` is output 16 or 32.
        """
        return self._runtime.submit(self._plc.read_type_name())

    def read_cpu_status(self) -> CpuStatus:
        """The CPU operating status, from SD203 read as an ordinary word.

        SLMP has no "what state are you in" command. An undocumented value raises rather
        than being rounded to the nearest state: "the CPU is stopped" is not something to
        say on no evidence.
        """
        return self._runtime.submit(self._plc.read_cpu_status())

    def clear_error(self) -> None:
        """``1617``: clear the own-station error code and the error LED."""
        self._runtime.submit(self._plc.clear_error())

    @overload
    def raw_command(
        self,
        command: int,
        subcommand: int,
        payload: bytes = b"",
        /,
        *,
        mutates: bool = True,
        expect_response: Literal[True] = True,
    ) -> RawResponse: ...

    @overload
    def raw_command(
        self,
        command: int,
        subcommand: int,
        payload: bytes = b"",
        /,
        *,
        mutates: bool = True,
        expect_response: Literal[False],
    ) -> None: ...

    def raw_command(
        self,
        command: int,
        subcommand: int,
        payload: bytes = b"",
        /,
        *,
        mutates: bool = True,
        expect_response: bool = True,
    ) -> RawResponse | None:
        """Send arbitrary command bytes. **Bypasses command validation and nothing else.**

        The frame, the ``L`` guard, the one-in-flight gate, the end-code raise and the
        transaction record all still apply. It exists because this CPU accepted requests
        its own manual forbids, and a library that can only send what it approves of
        cannot be used to find out what a CPU really does.

        ``expect_response=False`` is overloaded to return ``None`` rather than widening
        the normal return type, for the same reason ``allow_split`` is: a caller who did
        not ask for silence never has to narrow a union.
        """
        # Branched for the same reason read_random is: `expect_response` is a bool
        # here and a Literal on the async twin's overloads.
        if expect_response:
            return self._runtime.submit(
                self._plc.raw_command(
                    command, subcommand, payload, mutates=mutates, expect_response=True
                )
            )
        return self._runtime.submit(
            self._plc.raw_command(
                command, subcommand, payload, mutates=mutates, expect_response=False
            )
        )

