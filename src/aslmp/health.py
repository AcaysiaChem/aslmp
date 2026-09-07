"""Layer 6 -- is this connection healthy, and how do we ask without harming it?

.. rubric:: The one rule that shapes this module

**A probe that finds the in-flight gate held is skipped and counted. Never queued, never
raised.** (DESIGN.md graft G5, and all three design judges asked for it independently.)

An SLMP connection entry on FX5U-32MT/DS fw 1.065 carries **one transaction at a time**:
two TCP requests written before the first response is read return ONE response, for the
LAST request, with end code ``0x0000`` (2026-09-06). :mod:`aslmp.transport.inflight`
makes that inexpressible by handing out a single in-flight slot. A health check that
competed for that slot would therefore delay -- or, under
:attr:`~aslmp.transport.inflight.Concurrency.STRICT`, break -- the very traffic whose
health it is reporting. A monitor that makes a control loop worse is worse than no
monitor.

So the probe stands down. It counts itself in
:attr:`~aslmp.observability.Counters.probes_skipped`, emits
:class:`~aslmp.observability.ProbeSkipped` into the client's own event stream, and
returns. Nothing waits and nothing raises.

.. rubric:: Traffic first, probes only into silence

The monitor's primary input is the loop's own traffic: wire ``on_transaction=monitor.observe``
and every read the loop already does is a liveness measurement that costs nothing. Only
after ``idle_probe_after`` of silence does :meth:`HealthMonitor.run` inject a ``0x0619``
Self Test -- the one SLMP command with no side effect of any kind, whose ~7 ms round trip
is indistinguishable from a real read, which is exactly what makes it a valid probe.

.. rubric:: Why the probe's own transaction is not counted twice

:meth:`HealthMonitor.observe` ignores transactions whose command is ``0x0619``. The probe
path already records its own outcome, and a monitor that also counted its probes as
traffic would keep proving itself healthy on the strength of the questions it asked. The
side effect is that an explicit ``plc.self_test()`` or ``plc.ping()`` from the caller does
not refresh the idle timer either, which is the correct trade: those are also questions,
not work.
"""

from __future__ import annotations

import asyncio
import enum
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final, final

from aslmp.client import Plc
from aslmp.connection import ConnectionState
from aslmp.errors import SlmpConfigurationError, SlmpError, SlmpSinkError
from aslmp.observability import ProbeSkipped
from aslmp.timing import Clock, Nanos, Transaction
from aslmp.transport.inflight import TransactionGate

__all__ = [
    "PROBE_COMMAND",
    "HealthMonitor",
    "HealthSnapshot",
    "ProbeOutcome",
]

PROBE_COMMAND: Final = 0x0619
"""``0x0619`` Self Test: the probe, and the one command excluded from ``observe``.

Zero side effect, and its latency equals a real read's -- measured at about 7 ms on the
FX5U's built-in Ethernet port (FX5U-32MT/DS fw 1.065, 2026-09-06). ``socket.connect()``
proves nothing on this hardware, so this is what "the connection is alive" means here.
"""

_NS_PER_S: Final = 1_000_000_000

Sleeper = Callable[[float], Awaitable[None]]
"""How :meth:`HealthMonitor.run` waits. ``asyncio.sleep`` by default, injectable so the
scheduling can be tested without the test becoming a timing experiment."""


class ProbeOutcome(enum.Enum):
    """What one call to :meth:`HealthMonitor.probe_once` did.

    ``SENT``
        A ``0x0619`` went out and echoed. Recorded as a success.
    ``FAILED``
        A ``0x0619`` went out and did not come back correctly. Recorded as a failure,
        and the exception is kept in :attr:`HealthSnapshot.last_failure` rather than
        raised: a monitor whose job is to report a broken connection must not die of one.
    ``SKIPPED_BUSY``
        The in-flight gate was held. Counted in ``counters.probes_skipped`` and reported
        as :class:`~aslmp.observability.ProbeSkipped`. **This is graft G5**, and it is a
        skip rather than a wait on purpose.
    ``SKIPPED_UNUSABLE``
        The connection is ``NEW``, ``FAILED`` or ``CLOSED``, so there is nothing to
        probe. Not counted as a skipped probe -- ``probes_skipped`` means "stood down
        for live traffic", and diluting it with this would make it unreadable. The state
        itself already forces :attr:`HealthSnapshot.healthy` to ``False``.
    """

    SENT = "sent"
    FAILED = "failed"
    SKIPPED_BUSY = "skipped-busy"
    SKIPPED_UNUSABLE = "skipped-unusable"


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class HealthSnapshot:
    """One frozen answer to "is this connection healthy", with the evidence attached.

    ``healthy`` is the hysteresis output **and** the connection state: a client that is
    ``FAILED`` or ``CLOSED`` is never reported healthy however good the window looks,
    because the window describes a socket that no longer exists.
    """

    at: Nanos
    name: str
    connection_id: str
    generation: int
    healthy: bool
    state: ConnectionState
    consecutive_failures: int
    consecutive_successes: int
    observed: int
    failures: int
    capacity: int
    probes_sent: int
    probes_skipped: int
    last_activity_at: Nanos | None
    last_failure: str | None

    @property
    def failure_ratio(self) -> float:
        """Failures over observations in the window. ``0.0`` when nothing was observed.

        The one place a zero is not a fabrication: it is a count of a thing that did not
        happen over a window that is stated beside it, not an invented measurement.
        """
        return 0.0 if self.observed == 0 else self.failures / self.observed

    def idle_ns(self, *, now: Nanos) -> int | None:
        """Nanoseconds since the last non-probe transaction, or ``None`` if never."""
        return None if self.last_activity_at is None else now - self.last_activity_at

    def __str__(self) -> str:
        verdict = "healthy" if self.healthy else "UNHEALTHY"
        return (
            f"{self.name} [{self.state.value}] {verdict}: {self.failures}/{self.observed} "
            f"failed in window, probes {self.probes_sent} sent / "
            f"{self.probes_skipped} skipped"
        )


@final
class HealthMonitor:
    """Watches one client: its traffic first, a ``0x0619`` into silence second.

    ::

        # A monitor needs its client and a client takes its sink at construction, so the
        # two are tied together through a one-line forwarder.
        watched: list[HealthMonitor] = []
        plc = Plc(
            host,
            profile="melsec:iq-f/fx5u",
            on_transaction=lambda tx: watched[0].observe(tx),
        )
        monitor = HealthMonitor(plc, on_change=alarm_panel.update)
        watched.append(monitor)
        watching = asyncio.create_task(monitor.run())

    Hysteresis, so that one bad read does not flap a plant alarm: ``unhealthy_after``
    consecutive failures make it unhealthy, ``healthy_after`` consecutive successes make
    it healthy again, and it starts **unhealthy** because nothing has proven otherwise
    yet. A monitor that starts optimistic is asserting a measurement it has not taken.
    """

    __slots__ = (
        "_capacity",
        "_client",
        "_clock",
        "_consecutive_failures",
        "_consecutive_successes",
        "_healthy",
        "_healthy_after",
        "_idle_probe_after_ns",
        "_last_activity_at",
        "_last_attempt_at",
        "_last_failure",
        "_on_change",
        "_probe_interval_ns",
        "_sleep",
        "_unhealthy_after",
        "_window",
    )

    def __init__(
        self,
        client: Plc,
        *,
        idle_probe_after: float = 5.0,
        probe_interval: float = 5.0,
        window: int = 1024,
        unhealthy_after: int = 3,
        healthy_after: int = 2,
        on_change: Callable[[HealthSnapshot], None] | None = None,
        clock: Clock = time.monotonic_ns,
        sleep: Sleeper = asyncio.sleep,
    ) -> None:
        if idle_probe_after <= 0:
            raise SlmpConfigurationError(
                f"idle_probe_after must be positive; {idle_probe_after} would probe a "
                f"busy connection continuously."
            )
        if probe_interval <= 0:
            raise SlmpConfigurationError(
                f"probe_interval must be positive; {probe_interval} is a busy loop."
            )
        if window < 1:
            raise SlmpConfigurationError(
                f"the health window must hold at least one observation; got {window}."
            )
        if unhealthy_after < 1 or healthy_after < 1:
            raise SlmpConfigurationError(
                f"hysteresis thresholds must be at least 1; got "
                f"unhealthy_after={unhealthy_after}, healthy_after={healthy_after}."
            )
        self._client = client
        self._idle_probe_after_ns = round(idle_probe_after * _NS_PER_S)
        self._probe_interval_ns = round(probe_interval * _NS_PER_S)
        self._capacity = window
        self._unhealthy_after = unhealthy_after
        self._healthy_after = healthy_after
        self._on_change = on_change
        self._clock = clock
        self._sleep = sleep
        self._window: deque[bool] = deque(maxlen=window)
        self._consecutive_failures = 0
        self._consecutive_successes = 0
        self._healthy = False
        self._last_activity_at: Nanos | None = None
        self._last_attempt_at: Nanos | None = None
        self._last_failure: str | None = None

    # -- inspection ----------------------------------------------------------

    @property
    def client(self) -> Plc:
        """The client being watched. One monitor watches one connection entry."""
        return self._client

    @property
    def healthy(self) -> bool:
        """The hysteresis verdict, ANDed with a usable connection state."""
        return self._healthy and self._client.state.usable

    def snapshot(self) -> HealthSnapshot:
        """One frozen view, with the window's own counts beside the verdict."""
        connection = self._client._conn  # see _gate_of
        return HealthSnapshot(
            at=Nanos(self._clock()),
            name=self._client.name,
            connection_id=connection.connection_id,
            generation=self._client.generation,
            healthy=self.healthy,
            state=self._client.state,
            consecutive_failures=self._consecutive_failures,
            consecutive_successes=self._consecutive_successes,
            observed=len(self._window),
            failures=sum(1 for ok in self._window if not ok),
            capacity=self._capacity,
            probes_sent=self._client.counters.probes_sent,
            probes_skipped=self._client.counters.probes_skipped,
            last_activity_at=self._last_activity_at,
            last_failure=self._last_failure,
        )

    def __repr__(self) -> str:
        verdict = "healthy" if self.healthy else "unhealthy"
        return (
            f"HealthMonitor({self._client.name}, {verdict}, "
            f"observed={len(self._window)}/{self._capacity})"
        )

    # -- the loop's own traffic ----------------------------------------------

    def observe(self, tx: Transaction) -> None:
        """Fold one transaction in. Pass this as the client's ``on_transaction``.

        A ``0x0619`` is ignored here: :meth:`probe_once` records its own outcome, and a
        monitor that counted its probes as traffic too would prove itself healthy with
        its own questions. Everything else counts, including the transactions that
        failed -- a timeout is the strongest health signal there is.
        """
        if tx.command == PROBE_COMMAND:
            return
        received = tx.timing.received_at
        self._last_activity_at = received if received is not None else Nanos(self._clock())
        if tx.ok and tx.timing.is_complete:
            self._record_success()
        else:
            self._record_failure(
                f"transaction {tx.sequence} (0x{tx.command:04X}) ended "
                f"0x{tx.end_code:04X}"
                + ("" if tx.timing.is_complete else " and never completed")
            )

    # -- the probe -----------------------------------------------------------

    async def probe_once(self) -> ProbeOutcome:
        """Attempt exactly one ``0x0619``, subject to the graft G5 skip rule.

        Unconditional in time -- it does not consult ``idle_probe_after``, because
        :meth:`run` owns that schedule and a caller who asks for a probe wants one now.
        Conditional in *contention*: if the in-flight gate is held, this stands down,
        counts itself and returns :attr:`ProbeOutcome.SKIPPED_BUSY`. It never queues
        behind live traffic and it never raises what the probe found; the failure goes
        into the window and into :attr:`HealthSnapshot.last_failure`.
        """
        self._last_attempt_at = Nanos(self._clock())
        if not self._client.state.usable:
            return ProbeOutcome.SKIPPED_UNUSABLE
        gate = _gate_of(self._client)
        if gate.in_flight or gate.waiting:
            self._stand_down(gate)
            return ProbeOutcome.SKIPPED_BUSY
        self._client.counters.probes_sent += 1
        try:
            await self._client.self_test()
        except SlmpError as exc:
            self._record_failure(f"0x0619 probe failed: {exc.headline()}")
            return ProbeOutcome.FAILED
        self._record_success()
        return ProbeOutcome.SENT

    def _stand_down(self, gate: TransactionGate) -> None:
        """Count the skip and say who held the slot. Never queue, never raise (G5)."""
        self._client.counters.probes_skipped += 1
        now = Nanos(self._clock())
        held = "; ".join(holder.describe(now=now) for holder in gate.holders)
        connection = self._client._conn  # see _gate_of
        self._client._emit_event(  # see _gate_of
            ProbeSkipped(
                connection_id=connection.connection_id,
                generation=self._client.generation,
                at=now,
                reason=(
                    f"{gate.in_flight} of {gate.capacity} in-flight slot(s) held "
                    f"({held or 'none named'}), {gate.waiting} queued. A health probe "
                    f"never competes with live traffic for the one slot this hardware "
                    f"has: skipped and counted, never queued."
                ),
            )
        )

    # -- the schedule --------------------------------------------------------

    def due_in(self, *, now: Nanos | None = None) -> float:
        """Seconds until the next probe is due. ``0.0`` means "now".

        Two clocks decide it: silence since the last real transaction
        (``idle_probe_after``) and time since the last probe *attempt*
        (``probe_interval``). The second one counts skips as attempts, which is what
        stops a permanently busy connection from spinning this loop.
        """
        stamp = Nanos(self._clock()) if now is None else now
        activity = self._last_activity_at
        idle_due = stamp if activity is None else activity + self._idle_probe_after_ns
        attempt = self._last_attempt_at
        probe_due = stamp if attempt is None else attempt + self._probe_interval_ns
        due = max(idle_due, probe_due)
        return 0.0 if due <= stamp else (due - stamp) / _NS_PER_S

    async def run(self) -> None:
        """Watch until the client is closed. Cancel it, or close the client, to stop.

        This never raises for what it observes. It raises only for what is wrong with
        *itself*: a bad ``on_change`` callback (as :class:`~aslmp.errors.SlmpSinkError`)
        or cancellation.
        """
        while self._client.state is not ConnectionState.CLOSED:
            delay = self.due_in()
            if delay > 0:
                await self._sleep(delay)
                continue
            await self.probe_once()

    # -- hysteresis ----------------------------------------------------------

    def _record_success(self) -> None:
        self._window.append(True)
        self._consecutive_failures = 0
        self._consecutive_successes += 1
        if not self._healthy and self._consecutive_successes >= self._healthy_after:
            self._flip(healthy=True)

    def _record_failure(self, why: str) -> None:
        self._window.append(False)
        self._last_failure = why
        self._consecutive_successes = 0
        self._consecutive_failures += 1
        if self._healthy and self._consecutive_failures >= self._unhealthy_after:
            self._flip(healthy=False)

    def _flip(self, *, healthy: bool) -> None:
        """Change the verdict and tell the caller. A broken callback is reported, never
        swallowed: it is counted in ``counters.sink_errors`` and raised as
        :class:`~aslmp.errors.SlmpSinkError` so that a monitor nobody is listening to
        does not go on quietly pretending someone is."""
        self._healthy = healthy
        callback = self._on_change
        if callback is None:
            return
        try:
            callback(self.snapshot())
        except Exception as exc:
            self._client.counters.sink_errors += 1
            raise SlmpSinkError(
                f"the HealthMonitor on_change callback raised: "
                f"{type(exc).__name__}: {exc}",
                sink="on_change",
            ) from exc


def _gate_of(client: Plc) -> TransactionGate:
    """The client's in-flight gate.

    :class:`~aslmp.client.Plc` publishes no in-flight accessor, and this module is the
    one place in the package that needs one: graft G5 is *specifically* about not
    submitting a probe while the gate is held. Inferring it from a refusal instead --
    calling ``self_test()`` and catching
    :class:`~aslmp.errors.SlmpConcurrentTransactionError` -- would work only under
    :attr:`~aslmp.transport.inflight.Concurrency.STRICT`, and under ``SERIALIZE`` the
    probe would silently *queue*, which is the one thing G5 forbids. Layer 6 reading
    layer 4 is a legal edge in the DESIGN section 1 DAG; hiding the read behind a
    guessed behaviour would not be legal engineering.
    """
    return client._conn.gate  # the one private read, argued above
