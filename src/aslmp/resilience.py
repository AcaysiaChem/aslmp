"""Layer 6 -- the **only** place in this package where a connection is rebuilt.

.. rubric:: Why reconnection is a separate object with a mandatory policy

``plc-comm-slmp`` rebuilds its socket after a failure inside the call that failed. That
one line is why its published latency numbers cannot be trusted: a sample that includes
a hidden TCP handshake is indistinguishable from a slow PLC, and the reader has no field
to tell them apart. Worse, on this hardware the rebuild can *succeed and then be FINed* --
a second TCP connection to a one-entry SLMP configuration completed its handshake in
5.4 ms and was closed by the CPU with the incumbent undisturbed (FX5U-32MT/DS fw 1.065,
2026-09-06) -- so an unbounded automatic retry against a busy entry is a spin loop with a
socket in it.

So (DESIGN.md graft G12): :class:`~aslmp.client.Plc` never reconnects. Its ``FAILED``
state is sticky and its socket is closed. Reconnection is this object, it is opt-in, its
``policy`` has **no default** -- you cannot get backoff by accident and you cannot get
*no* backoff by accident either -- and every reconnect emits
:class:`~aslmp.observability.Reconnecting` and
:class:`~aslmp.observability.Reconnected` into the client's own event stream, with the
generation before and after.

.. rubric:: A transaction during a reconnect raises. It does not wait.

That is structural rather than policy: :meth:`aslmp.connection.Connection.reopen` closes
the transport first, and :meth:`~aslmp.connection.Connection.require_usable` refuses
every transaction while the state is not ``OPEN`` or ``READY``. A control loop that
blocked for the length of a backoff would be a control loop that had silently stopped
controlling; it gets :class:`~aslmp.errors.SlmpNotConnectedError` on the next cycle
instead, immediately, with ``reason`` naming the state.

.. rubric:: The CPU on the other end may not be the same CPU

A reconnect can land on a different machine -- a swapped controller, a re-used IP, a
patch lead moved. ``Y20`` is output 16 on an iQ-F and output 32 on an iQ-R and **both
answer end code 0x0000**, so nothing on the wire objects. The supervisor therefore
compares the ``0x0101`` model code across the reconnect and, if it changed, closes the
connection and stands down with :class:`~aslmp.errors.SlmpTargetChangedError` (graft G6).
"""

from __future__ import annotations

import asyncio
import contextlib
import random
from collections.abc import Awaitable, Callable
from types import TracebackType
from typing import Final, Protocol, Self, final, runtime_checkable

from aslmp._clock import DEFAULT_CLOCK
from aslmp.client import Plc
from aslmp.connection import ConnectionState
from aslmp.errors import (
    SlmpConfigurationError,
    SlmpError,
    SlmpNotConnectedError,
    SlmpTargetChangedError,
)
from aslmp.observability import (
    ConnectionEvent,
    ConnectionFailed,
    Disconnected,
    Reconnected,
    Reconnecting,
    TargetChanged,
)
from aslmp.timing import Clock, Nanos

__all__ = [
    "ExponentialBackoff",
    "ReconnectPolicy",
    "Supervisor",
]

_NS_PER_S: Final = 1_000_000_000
_MAX_DOUBLINGS: Final = 64
"""Where the exponent stops. ``0.25 * 2**64`` is already 146 million years; going
further only risks an ``OverflowError`` in the middle of a recovery."""

Sleeper = Callable[[float], Awaitable[None]]
"""How the supervisor waits out a backoff. ``asyncio.sleep`` by default."""


@runtime_checkable
class ReconnectPolicy(Protocol):
    """How long to wait before the next attempt, and when to stop attempting.

    Two methods rather than one because they answer different questions and a library
    that fused them would have to guess: ``delay`` is pacing, ``give_up`` is surrender.
    A policy that never gives up is legal and is what a plant usually wants; a policy
    that gives up is what a commissioning script wants. Neither is the default, because
    :class:`Supervisor` has no default policy at all.
    """

    def delay(self, attempt: int) -> float:
        """Seconds to wait before attempt number ``attempt`` (1 for the first)."""
        ...

    def give_up(self, attempt: int, elapsed: float) -> bool:
        """Called after attempt ``attempt`` has failed, ``elapsed`` seconds in."""
        ...


@final
class ExponentialBackoff:
    """Doubling delay with symmetric jitter, optionally bounded by an attempt count.

    ``initial=0.25`` because an immediate retry is the specific failure this exists to
    prevent: a reconnect against an SLMP entry that is already in use completes its TCP
    handshake and is then FINed by the CPU (FX5U-32MT/DS fw 1.065, 2026-09-06), so a
    zero-delay retry is a loop that opens and loses a socket as fast as the network
    allows. ``maximum=30.0`` because a CPU that has been down for half a minute is not
    coming back in the next 30 ms, and a supervisor that keeps asking is just noise on a
    plant network.

    The jitter is symmetric (``base * (1 +/- jitter)``) and is not decoration: several
    clients reconnecting to one CPU after a switch reboot arrive together otherwise, and
    an SLMP entry serves one connection.
    """

    __slots__ = ("_initial", "_jitter", "_max_attempts", "_maximum", "_random")

    def __init__(
        self,
        *,
        initial: float = 0.25,
        maximum: float = 30.0,
        jitter: float = 0.2,
        max_attempts: int | None = None,
        random_source: Callable[[], float] = random.random,
    ) -> None:
        if initial <= 0:
            raise SlmpConfigurationError(
                f"initial backoff must be positive; {initial} s retries instantly, and "
                f"an instant retry against a busy SLMP entry is a spin loop with a "
                f"socket in it."
            )
        if maximum < initial:
            raise SlmpConfigurationError(
                f"maximum backoff {maximum} s is below the initial {initial} s."
            )
        if not 0.0 <= jitter < 1.0:
            raise SlmpConfigurationError(
                f"jitter is a fraction of the delay and must be in [0, 1); got {jitter}. "
                f"At 1.0 or above the delay can be zero or negative."
            )
        if max_attempts is not None and max_attempts < 1:
            raise SlmpConfigurationError(
                f"max_attempts must be at least 1 or None (never give up); got "
                f"{max_attempts}."
            )
        self._initial = initial
        self._maximum = maximum
        self._jitter = jitter
        self._max_attempts = max_attempts
        self._random = random_source

    @property
    def initial(self) -> float:
        return self._initial

    @property
    def maximum(self) -> float:
        return self._maximum

    @property
    def jitter(self) -> float:
        return self._jitter

    @property
    def max_attempts(self) -> int | None:
        return self._max_attempts

    def delay(self, attempt: int) -> float:
        """``initial * 2 ** (attempt - 1)``, capped at ``maximum``, then jittered."""
        if attempt < 1:
            raise SlmpConfigurationError(
                f"attempt numbers start at 1; got {attempt}."
            )
        base = min(self._maximum, self._initial * 2.0 ** min(attempt - 1, _MAX_DOUBLINGS))
        if self._jitter == 0.0:
            return base
        return base * (1.0 + self._jitter * (2.0 * self._random() - 1.0))

    def give_up(self, attempt: int, elapsed: float) -> bool:
        """True once ``max_attempts`` attempts have failed. ``elapsed`` is not consulted.

        The protocol carries ``elapsed`` because a deadline-shaped policy is a
        reasonable thing to write; this one is attempt-shaped and says so rather than
        pretending to use it.
        """
        del elapsed
        return self._max_attempts is not None and attempt >= self._max_attempts

    def __repr__(self) -> str:
        return (
            f"ExponentialBackoff(initial={self._initial}, maximum={self._maximum}, "
            f"jitter={self._jitter}, max_attempts={self._max_attempts})"
        )


@final
class Supervisor:
    """Watches one client and rebuilds its connection when it fails. Opt-in, observable.

    ::

        async with Plc(host, profile="melsec:iq-f/fx5u") as plc:
            async with Supervisor(plc, policy=ExponentialBackoff()) as sup:
                async for tick in Cadence(timedelta(milliseconds=100)):
                    try:
                        pv = await plc.read_f32("D2")
                    except SlmpNotConnectedError:
                        continue        # a reconnect is under way; it does not block us

    The supervisor owns no socket and closes no client it did not have to. Leaving the
    ``async with`` stops the supervision, not the connection.
    """

    __slots__ = (
        "_client",
        "_clock",
        "_failure",
        "_listening",
        "_policy",
        "_ready",
        "_reason",
        "_reconnects",
        "_sleep",
        "_stopped",
        "_surrendered",
        "_task",
        "_wake",
    )

    def __init__(
        self,
        client: Plc,
        *,
        policy: ReconnectPolicy,
        clock: Clock = DEFAULT_CLOCK,
        sleep: Sleeper = asyncio.sleep,
    ) -> None:
        self._client = client
        self._policy = policy
        self._clock = clock
        self._sleep = sleep
        self._reconnects = 0
        self._listening = False
        self._failure: SlmpError | None = None
        self._reason = "not started"
        self._stopped = False
        self._surrendered = False
        self._task: asyncio.Task[None] | None = None
        self._ready: asyncio.Event | None = None
        self._wake: asyncio.Event | None = None

    # -- inspection ----------------------------------------------------------

    @property
    def client(self) -> Plc:
        return self._client

    @property
    def policy(self) -> ReconnectPolicy:
        return self._policy

    @property
    def reconnects(self) -> int:
        """Successful reconnects this supervisor performed. Also in ``counters``."""
        return self._reconnects

    @property
    def supervising(self) -> bool:
        """True between ``__aenter__`` and ``__aexit__``, while the watcher is alive."""
        return self._task is not None and not self._stopped

    @property
    def surrendered(self) -> bool:
        """True once the policy gave up, or the CPU on the other end changed."""
        return self._surrendered

    @property
    def failure(self) -> SlmpError | None:
        """The last reconnect failure, or the reason this supervisor stood down."""
        return self._failure

    def __repr__(self) -> str:
        return (
            f"Supervisor({self._client.name}, reconnects={self._reconnects}, "
            f"supervising={self.supervising}, surrendered={self._surrendered})"
        )

    # -- lifecycle -----------------------------------------------------------

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        await self.aclose()

    async def start(self) -> None:
        """Connect if the client has never been connected, then start watching.

        Idempotent only in the sense that starting twice is a mistake and says so: two
        watchers on one client would race each other into two reconnects.
        """
        if self._task is not None:
            raise SlmpConfigurationError(
                "this Supervisor is already running. One supervisor watches one client; "
                "two would race each other into two reconnects of the same socket."
            )
        self._ready = asyncio.Event()
        self._wake = asyncio.Event()
        self._stopped = False
        if not self._listening:
            # Registered before anything can fail, and before the watcher task exists:
            # a ConnectionFailed that arrives in the gap between start() and the task's
            # first await would otherwise be the one failure nobody reconnects from.
            self._client.add_event_listener(self._on_event)
            self._listening = True
        if self._client.state is ConnectionState.NEW:
            await self._client.connect()
        if self._client.state.usable:
            self._ready.set()
        else:
            self._reason = f"supervision started with the connection {self._client.state.value}"
            self._wake.set()
        self._task = asyncio.create_task(
            self._supervise(), name=f"aslmp-supervisor-{self._client.name}"
        )

    async def aclose(self) -> None:
        """Stop supervising. Does **not** close the client: the caller owns that."""
        self._stopped = True
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def wait_ready(self, *, timeout: float | None = None) -> None:
        """Block until the connection is usable again, or say why it will not be.

        Raises the reconnect failure if the policy gave up, or
        :class:`~aslmp.errors.SlmpTargetChangedError` if the CPU changed. A ``timeout``
        that expires raises :class:`~aslmp.errors.SlmpNotConnectedError` naming the
        state -- never a stale "ready".
        """
        ready = self._ready
        if ready is None:
            raise SlmpConfigurationError(
                "this Supervisor has not been started; use 'async with Supervisor(...)' "
                "or await start() before waiting on it."
            )
        if timeout is None:
            await ready.wait()
        else:
            try:
                await asyncio.wait_for(ready.wait(), timeout)
            except TimeoutError as exc:
                raise SlmpNotConnectedError(
                    f"waited {timeout:g} s for {self._client.name} to become ready and "
                    f"it is {self._client.state.value}. Reconnection is paced by "
                    f"{self._policy!r} and is never hurried by a waiter.",
                    reason=self._client.state.value,
                ) from exc
        self._raise_if_dead()
        failure = self._failure
        if self._surrendered and failure is not None:
            raise failure

    def _raise_if_dead(self) -> None:
        """Surface an exception that killed the watcher instead of waiting on a corpse."""
        task = self._task
        if task is None or not task.done() or task.cancelled():
            return
        died = task.exception()
        if died is not None:
            raise died

    # -- the watcher ---------------------------------------------------------

    def _on_event(self, event: ConnectionEvent, /) -> None:
        """The client's own event stream is the trigger. No polling, no state scraping.

        Readiness is cleared **here**, synchronously, rather than in the watcher task:
        the connection is down the instant the event is emitted, and a waiter that
        arrived before the task next ran would otherwise be handed the previous
        connection's "ready" -- a stale success, which is the whole class of bug this
        library is written against.
        """
        if self._stopped or self._wake is None:
            return
        if isinstance(event, ConnectionFailed):
            self._reason = event.reason
            if self._ready is not None:
                self._ready.clear()
            self._wake.set()
        elif isinstance(event, Disconnected) and event.expected:
            # Closed on purpose by the caller. Supervising a closed client would be
            # reconnecting something somebody deliberately shut -- and a waiter must be
            # told that rather than left blocked on a connection nobody is rebuilding.
            self._stopped = True
            self._surrender(
                SlmpNotConnectedError(
                    f"{self._client.name} was closed by the caller, so supervision "
                    f"stopped. A Supervisor rebuilds a connection that failed; it does "
                    f"not reopen one somebody deliberately shut.",
                    reason="closed",
                )
            )
            self._wake.set()

    async def _supervise(self) -> None:
        wake = self._wake
        ready = self._ready
        if wake is None or ready is None:  # pragma: no cover - start() sets both
            raise SlmpConfigurationError("the supervisor was started without its events.")
        while True:
            await wake.wait()
            wake.clear()
            if self._stopped or self._client.state is ConnectionState.CLOSED:
                return
            if self._client.state.usable:
                continue
            ready.clear()
            await self._reconnect(self._reason)
            if self._surrendered:
                return

    async def _reconnect(self, reason: str) -> None:
        client = self._client
        started = Nanos(self._clock())
        previous_model = None if client.identity is None else client.identity.model_code
        attempt = 1
        while True:
            delay = self._policy.delay(attempt)
            _emit(
                client,
                Reconnecting(
                    connection_id=_connection_id(client),
                    generation=client.generation,
                    at=Nanos(self._clock()),
                    attempt=attempt,
                    delay_s=delay,
                    reason=reason,
                ),
            )
            if delay > 0:
                await self._sleep(delay)
            if self._stopped:
                return
            previous_generation = client.generation
            failure = await self._attempt(attempt=attempt, reason=reason, started=started)
            if self._surrendered:
                return
            if failure is not None:
                attempt += 1
                continue
            if not await self._confirm_target(previous_model):
                return
            self._reconnects += 1
            self._failure = None
            _emit(
                client,
                Reconnected(
                    connection_id=_connection_id(client),
                    generation=client.generation,
                    at=Nanos(self._clock()),
                    previous_generation=previous_generation,
                ),
            )
            self._set_ready()
            return

    async def _attempt(
        self, *, attempt: int, reason: str, started: Nanos
    ) -> SlmpError | None:
        """One reconnect attempt. Returns the failure, or ``None`` on success.

        A separate method rather than a ``try`` inside the loop so that the failure is
        *returned* and acted on in one place. Nothing here recovers: the exception is
        kept on the supervisor, handed to every :meth:`wait_ready` waiter once the policy
        gives up, and chained under whatever the next attempt raises.
        """
        try:
            await self._client.reconnect(reason=reason)
        except SlmpError as exc:
            self._failure = exc
            elapsed = (self._clock() - started) / _NS_PER_S
            if self._policy.give_up(attempt, elapsed):
                self._surrender(exc)
            return exc
        return None

    async def _confirm_target(self, previous_model: int | None) -> bool:
        """Graft G6: the socket came back; the CPU behind it may not be the same one.

        ``True`` when the CPU is the one this connection was bound to -- including the
        honest "we never asked" case, where ``Handshake.SELF_TEST`` or ``NONE`` means
        there is no model code on either side and inventing a comparison would be worse
        than admitting there is none.
        """
        client = self._client
        current = None if client.identity is None else client.identity.model_code
        if previous_model is None or current is None or current == previous_model:
            return True
        _emit(
            client,
            TargetChanged(
                connection_id=_connection_id(client),
                generation=client.generation,
                at=Nanos(self._clock()),
                previous_model_code=previous_model,
                model_code=current,
            ),
        )
        await client.aclose()
        self._surrender(
            SlmpTargetChangedError(
                f"the reconnect to {client.name} reached model code 0x{current:04X} "
                f"({client.model}), and before the failure this connection was bound to "
                f"0x{previous_model:04X}. X and Y are octal on an iQ-F and hexadecimal "
                f"everywhere else and both families answer end code 0x0000, so nothing "
                f"on the wire would have objected. The connection has been closed rather "
                f"than handed back.",
                expected_model_code=previous_model,
                actual_model_code=current,
            )
        )
        return False

    def _surrender(self, exc: SlmpError) -> None:
        """Stop trying, keep the reason, and wake every waiter so none of them hangs."""
        self._surrendered = True
        self._failure = exc
        self._set_ready()

    def _set_ready(self) -> None:
        ready = self._ready
        if ready is not None:
            ready.set()


def _emit(client: Plc, event: ConnectionEvent) -> None:
    """Publish into the client's own event stream.

    :class:`~aslmp.observability.Reconnecting`, ``Reconnected`` and ``TargetChanged`` are
    :class:`~aslmp.observability.ConnectionEvent` subclasses carrying that connection's
    id and generation, and they belong beside ``Connecting`` and ``Connected`` where a
    caller has already attached a listener. :class:`~aslmp.client.Plc` publishes no
    public emit -- a private one used from one place inside the package is a smaller
    problem than a second, parallel event stream a caller has to subscribe to separately.
    """
    client._emit_event(event)


def _connection_id(client: Plc) -> str:
    """The connection's stable id. Survives a reopen, which is exactly the point here."""
    return client._conn.connection_id
