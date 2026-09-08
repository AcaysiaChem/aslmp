"""The pathology board: every switch is a thing a real PLC was measured doing.

Layer 2.5 (``aslmp.testing``). Pure data -- no sockets, no asyncio, no state.

This is the part of the package that has no equivalent anywhere. The survey found three
open SLMP servers in the world; all three answer only the happy path, and one of them
rejects every correct 3E client because it compares the subheader constant ``0x0054``
against a little-endian unpack of ``50 00``. A simulator that can only be right is not a
test fixture, it is a second implementation of the same optimism.

**Every switch below cites the measurement it reproduces**, and each one is independently
settable so that a test can turn on exactly the failure it is about. Nothing here is a
knob for making the simulator "more realistic in general": a switch that cannot be tied
to an observation on a named CPU and firmware does not belong on this board.

The defaults of :class:`Pathology` are **all off**. A target then declares the board its
silicon actually presents (:attr:`~aslmp.testing.targets.SimulatorTarget.pathology`), so
``PEDANTIC`` runs clean and ``FX5U_32MT_DS`` runs with the measured misbehaviour on --
and diffing the two suites is precisely the document of where silicon and manual part.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from aslmp.wire.citations import Citation, Measurement

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Mapping

    from aslmp.wire.citations import Source

__all__ = [
    "COALESCING",
    "FX5U_MEASURED",
    "HEALTHY",
    "ONE_CONNECTION",
    "PATHOLOGY_SOURCES",
    "REMOTE_RUN_LIES",
    "RESET_NO_RESPONSE",
    "SEGMENTATION",
    "SILENT_ON_CODING_MISMATCH",
    "TS_ACCEPTED",
    "UDP_PIPELINE_LOSS",
    "Pathology",
]


COALESCING: Final = Measurement(
    cpu="FX5U-32MT/DS",
    firmware="1.065",
    date="2026-09-06",
    host="192.168.10.41 (laptop)",
    medium="wi-fi, ~7 ms median RTT",
    note=(
        "Two SLMP requests written before the first response is read arrive in one TCP "
        "segment and produce ONE response, for the LAST request, with end code 0x0000. "
        "Proved with 4E serials: three coalesced requests answered only serial 0xC102, "
        "four answered only 0xA003. On a 3E frame there is no correlation field at all, "
        "so the client pairs the answer with the FIRST request and a setpoint read "
        "returns the scan counter, with no error anywhere."
    ),
)
"""H1. The reason the in-flight gate is structural rather than advisory."""

ONE_CONNECTION: Final = Measurement(
    cpu="FX5U-32MT/DS",
    firmware="1.065",
    date="2026-09-06",
    host="192.168.10.41 (laptop)",
    medium="wi-fi, ~7 ms median RTT",
    note=(
        "A second TCP connection to a busy SLMP connection entry COMPLETES the handshake "
        "in 5.4 ms and is then closed by the CPU: recv() returns 0 bytes before anything "
        "was sent. The incumbent connection is not disturbed. socket.connect() therefore "
        "proves nothing, and a connection pool against one entry is worthless. This note "
        "said 'the slot frees immediately on close' until 2026-09-07; that clause is "
        "WITHDRAWN. Measured wired from argus-bench, a reconnect within about 2 ms of a "
        "clean close() is refused (1/6 at 0 ms, 6/6 from 2 ms), so the entry is released "
        "as the CPU processes the FIN rather than instantly -- ambiguity "
        "A-ENTRY-RELEASE-RACE. The window was invisible from this Wi-Fi host, which is "
        "how the clause survived: 30/30 at every gap including 0 ms."
    ),
)
"""H2. Why a zero-byte read on a fresh connection is its own named error."""

SEGMENTATION: Final = Measurement(
    cpu="FX5U-32MT/DS",
    firmware="1.065",
    date="2026-09-06",
    host="192.168.10.41 (laptop)",
    medium="wi-fi, ~7 ms median RTT",
    note=(
        "One of three identical 1931-byte responses arrived as two chunks, 1460 bytes "
        "then 471, 3.0 ms apart. Reads must be length-driven and the receive stamp taken "
        "after the LAST chunk."
    ),
)
"""H12. Why ``segment_at`` exists and why the timing stamp is not the first byte."""

SILENT_ON_CODING_MISMATCH: Final = Measurement(
    cpu="FX5U-32MT/DS",
    firmware="1.065",
    date="2026-09-06",
    host="192.168.10.41 (laptop)",
    medium="wi-fi, ~7 ms median RTT",
    note=(
        "An ASCII request sent to a binary connection entry produced no response at all "
        "-- no end code, no reset, nothing. Wrong frame type and an overstated data "
        "length fail the same way. Silence is the diagnosis, which is why the timeout "
        "has to name its likely cause from context rather than reporting 'no reply'."
    ),
)
"""Why ``silence_on_wrong_encoding`` is the FX5U default rather than ``0xC06F``."""

TS_ACCEPTED: Final = Measurement(
    cpu="FX5U-32MT/DS",
    firmware="1.065",
    date="2026-09-06",
    host="192.168.10.41 (laptop)",
    medium="wi-fi, ~7 ms median RTT",
    note=(
        "A 0403 Device Read Random carrying a word point at TS0 (device code 0xC1) "
        "returned end code 0x0000 and one word of data, although JY997D56001-K p.78 "
        "forbids TS in Read Random and predicts CPU error 0x4032. On 2026-09-07 the same "
        "CPU accepted a bit write at Y wire number 8, an address octal notation cannot "
        "express. The CPU accepts requests its own manual forbids; the client's refusal "
        "is the only thing standing there."
    ),
)
"""Why ``accept_illegal_random_points`` proves the client refuses what the PLC allows."""

UDP_PIPELINE_LOSS: Final = Measurement(
    cpu="FX5U-32MT/DS",
    firmware="1.065",
    date="2026-09-06",
    host="192.168.10.41 (laptop)",
    medium="wi-fi, ~7 ms median RTT",
    note=(
        "4E requests fired without waiting: 8 of 8 and 32 of 32 came back, in order; at "
        "64 only 44 came back over this link, which read as a soft ~31 percent loss. The "
        "WIRED retest of 2026-09-07 shows that reading is the wrong SHAPE: from "
        "argus-bench, depth 48 answered exactly 32 and depth 64 answered exactly 32, so "
        "the queue is a hard ceiling of 32 and a slow link merely lets the CPU drain "
        "part of it mid-burst. Design against 32, not against a percentage. Either way "
        "the lost requests produce no end code, no ICMP and no error of any kind -- the "
        "client learns only by timing out on a serial that never returns. UDP does NOT "
        "suffer the TCP coalescing corruption: the same two-requests-no-read test "
        "returns both responses correctly."
    ),
)
"""Why a lost datagram must raise its own named error carrying serial and depth."""

RESET_NO_RESPONSE: Final = Citation(
    manual="SH(NA)-080956ENG",
    revision="M",
    section="p.136",
    note=(
        "On a successful Remote Reset 'the response request is not be sent back to the "
        "external device'. An absent response is the documented success, not a timeout."
    ),
)
"""Why ``remote_reset_no_response`` defaults on wherever remote reset is served."""

REMOTE_RUN_LIES: Final = Citation(
    manual="SH(NA)-080956ENG",
    revision="M",
    section="6.9 Remote RUN",
    note=(
        "With the CPU switch in STOP a Remote RUN 'will be completed normally. However, "
        "the access destination does not become the RUN state.' End code 0x0000 on a "
        "state that was never reached, which is why verify=True is the default."
    ),
)
"""Why ``remote_run_lies`` gives ``verify=True`` something real to catch."""

PATHOLOGY_SOURCES: Final[Mapping[str, Source]] = {
    "coalesce_requests": COALESCING,
    "single_connection": ONE_CONNECTION,
    "segment_at": SEGMENTATION,
    "silence_on_wrong_encoding": SILENT_ON_CODING_MISMATCH,
    "accept_illegal_random_points": TS_ACCEPTED,
    "overstated_length_hangs": SILENT_ON_CODING_MISMATCH,
    "udp_drop_above_depth": UDP_PIPELINE_LOSS,
    "remote_reset_no_response": RESET_NO_RESPONSE,
    "remote_run_lies": REMOTE_RUN_LIES,
    "accept_4e_on_3e_entry": Measurement(
        cpu="FX5U-32MT/DS",
        firmware="1.065",
        date="2026-09-06",
        host="192.168.10.41 (laptop)",
        medium="wi-fi, ~7 ms median RTT",
        note=(
            "A 4E frame sent to a connection entry configured for 3E was answered "
            "normally, with the serial echoed, although two Mitsubishi manuals say the "
            "frame format is a property of the entry."
        ),
    ),
    "zero_4e_tail": Measurement(
        cpu="FX5U-32MT/DS",
        firmware="1.065",
        date="2026-09-06",
        host="192.168.10.41 (laptop)",
        medium="wi-fi, ~7 ms median RTT",
        note=(
            "The two subheader bytes after the 4E serial number are not free: the CPU "
            "returns them zeroed regardless of what was sent, so they are not a second "
            "correlation field."
        ),
    ),
    "stale_reply": COALESCING,
    "wrong_serial_echo": COALESCING,
}
"""Every switch, and the measurement or manual paragraph it reproduces.

A test asserts this covers every field of :class:`Pathology`, so a switch cannot be added
without saying which observation put it there.
"""


@dataclass(frozen=True, slots=True)
class Pathology:
    """Which measured misbehaviours a simulated CPU exhibits.

    Frozen: a board is captured by a running server and by every transcript entry it
    explains, and one that could be mutated mid-test would make a failure unattributable.
    Use :meth:`replace` to derive a board with one switch flipped, which is how each is
    tested in isolation.
    """

    coalesce_requests: bool = False
    """Several SLMP messages in one TCP read produce ONE response, for the **last** of
    them, end code ``0x0000``; the rest are discarded silently (:data:`COALESCING`).

    This is the switch the whole architecture exists for. Turning it on and removing a
    client's in-flight gate must make the conformance test fail; if it does not, the
    gate is not doing anything.
    """

    single_connection: bool = False
    """A connection past an entry's ``max_connections`` is accepted and then immediately
    closed, leaving the incumbent untouched (:data:`ONE_CONNECTION`)."""

    segment_at: int | None = None
    """Split any response longer than this many bytes into chunks of this size
    (:data:`SEGMENTATION`). ``1460`` is the measured split."""

    segment_gap_s: float = 0.0
    """Seconds between segmented chunks. The bench saw 3.0 ms."""

    silence_on_wrong_encoding: bool = False
    """A request whose subheader is not this entry's coding gets no response at all
    (:data:`SILENT_ON_CODING_MISMATCH`). With it off, the target's
    ``ascii_into_binary_entry`` end code is answered instead."""

    accept_illegal_random_points: frozenset[str] = frozenset()
    """Device families to serve in ``0403``/``1402`` although the manual forbids them,
    answering ``0x0000`` with data (:data:`TS_ACCEPTED`). ``frozenset({"TS"})``
    reproduces the measured case."""

    overstated_length_hangs: bool = False
    """A request whose declared ``L`` exceeds the bytes that arrive is waited on forever
    rather than answered (:data:`SILENT_ON_CODING_MISMATCH`). With it off the target's
    ``request_length_mismatch`` code is answered after
    :attr:`overstated_length_grace_s`."""

    overstated_length_grace_s: float = 0.05
    """How long to wait for the missing bytes before answering, when
    :attr:`overstated_length_hangs` is off. Never consulted when it is on."""

    stale_reply: bool = False
    """Answer each request with the **previous** response on that connection. The shape
    of the coalescing corruption seen from one transaction later, and the failure a
    library that leaves an unread tail on the socket produces on every subsequent read."""

    udp_drop_above_depth: int | None = None
    """Silently drop a datagram that arrives while this many are already unanswered
    (:data:`UDP_PIPELINE_LOSS`). 32 was clean on the bench; 64 lost 31% over Wi-Fi
    (2026-09-06) and everything past the 32nd request when measured wired (2026-09-07)."""

    udp_service_delay_s: float = 0.002
    """How long one datagram takes to service. Only observable when
    :attr:`udp_drop_above_depth` is set: without service time nothing is ever in flight,
    and the measured loss is a receive-queue overflow rather than a rate limit."""

    accept_4e_on_3e_entry: bool = False
    """Serve a 4E frame on an entry configured for 3E, and vice versa."""

    zero_4e_tail: bool = True
    """Return the two subheader bytes after the 4E serial as zeros whatever arrived."""

    wrong_serial_echo: bool = False
    """Echo a 4E serial that is not the one that was sent -- what the client sees when
    the coalescing corruption is caught in band."""

    late_reply_s: float = 0.0
    """Delay every response by this many seconds. A reply that arrives after the client
    has given up must be dropped and counted, never returned as the next answer."""

    remote_run_lies: bool = False
    """Answer ``1001``/``1002``/``1003`` ``0x0000`` and change nothing.

    See :data:`REMOTE_RUN_LIES`. A CPU that lies about every remote command, for no
    reason it will tell you -- distinct from the *documented* case that citation
    describes, which this simulator now models properly: put the key switch
    (:attr:`~aslmp.testing.dispatch.SessionState.switch_position`) in STOP and an honest
    CPU answers a Remote RUN ``0x0000`` while ``SD203`` still reads STOP.

    Until 2026-09-07 this switch could catch nothing whatever it was set to, because
    ``SD203`` was not derived from the handlers at all: a lying CPU and an honest one
    looked identical to every client, which is a curious property for the one pathology
    whose stated purpose is to give ``verify=True`` something real to catch.
    """

    remote_reset_no_response: bool = True
    """Send no response to Remote Reset and tear the connection down
    (:data:`RESET_NO_RESPONSE`). This is documented behaviour rather than a defect, so it
    is on by default."""

    drop_every_nth_datagram: int | None = None
    """Drop every ``n``-th UDP datagram outright. Deterministic loss, for the epoch and
    rebind paths, without needing a depth to build up."""

    sources: tuple[Source, ...] = field(default_factory=tuple, compare=False)
    """Extra provenance a caller wants carried alongside a custom board."""

    def __post_init__(self) -> None:
        if self.segment_at is not None and self.segment_at < 1:
            raise ValueError(f"segment_at must be a positive byte count, not {self.segment_at}")
        if self.udp_drop_above_depth is not None and self.udp_drop_above_depth < 1:
            raise ValueError(
                f"udp_drop_above_depth is the number of unanswered datagrams that may be "
                f"in flight before loss begins; {self.udp_drop_above_depth} is not one"
            )
        if self.drop_every_nth_datagram is not None and self.drop_every_nth_datagram < 1:
            raise ValueError("drop_every_nth_datagram must be a positive datagram count")
        for name in ("segment_gap_s", "late_reply_s", "udp_service_delay_s"):
            value = getattr(self, name)
            if value < 0:
                raise ValueError(f"{name} must not be negative; got {value}")
        object.__setattr__(
            self, "accept_illegal_random_points", frozenset(self.accept_illegal_random_points)
        )

    def replace(self, **changes: Any) -> Pathology:
        """This board with the named switches changed. The way one is tested alone.

        Any rather than object because the switches have five different types and
        dataclasses.replace checks each against its own field; object would make
        every call a type error while proving nothing.
        """
        return dataclasses.replace(self, **changes)

    @property
    def active(self) -> tuple[str, ...]:
        """Every switch that is not at its default, for a diagnostic or a diff."""
        default = Pathology()
        return tuple(
            f.name
            for f in dataclasses.fields(self)
            if f.name != "sources" and getattr(self, f.name) != getattr(default, f.name)
        )

    def cites(self) -> tuple[Source, ...]:
        """The measurements behind the switches this board has on."""
        out: list[Source] = []
        for name in self.active:
            source = PATHOLOGY_SOURCES.get(name)
            if source is not None and source not in out:
                out.append(source)
        out.extend(source for source in self.sources if source not in out)
        return tuple(out)

    def __str__(self) -> str:
        active = self.active
        return f"Pathology({', '.join(active) if active else 'clean'})"


HEALTHY: Final = Pathology()
"""No pathology at all: the manual as written, answering every request correctly."""

FX5U_MEASURED: Final = Pathology(
    coalesce_requests=True,
    single_connection=True,
    silence_on_wrong_encoding=True,
    accept_illegal_random_points=frozenset({"TS", "TC", "STS", "STC", "CS", "CC"}),
    overstated_length_hangs=True,
    accept_4e_on_3e_entry=True,
    zero_4e_tail=True,
    remote_reset_no_response=True,
)
"""Everything an FX5U-32MT/DS on firmware 1.065 was measured doing, and nothing else.

``segment_at`` is **not** included even though segmentation is measured: it happened on
one of three identical 1931-byte reads, so it is a probability rather than a rule, and a
test that wants it turns it on deterministically. ``stale_reply``,
``wrong_serial_echo``, ``late_reply_s``, ``remote_run_lies`` and the UDP loss switches
are likewise off here because they need parameters a default cannot honestly choose.
"""
