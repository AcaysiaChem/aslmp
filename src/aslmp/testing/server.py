"""The conformance simulator: an asyncio SLMP server that can be wrong on purpose.

Layer 2.5 (``aslmp.testing``). Imports L0-L2 only. **It must not import
``aslmp.transport``, ``aslmp.connection`` or ``aslmp.client``**, and
``tests/unit/test_layering.py`` enforces that: if the simulator were built on the
client's transport, a bug in the transport, the in-flight gate, the reconnection logic
or the client's control flow would be invisible to every client-against-server test in
the suite, because both sides would share it.

**It serves a list of entries**, ``(name, protocol, port, encoding, frame,
max_connections)``, because that is the shape of the PLC-side configuration in GX Works3
and it is what makes the one-connection-per-entry rule and the coding-mismatch silence
reproducible at all. Our own bench has five: four TCP and one UDP, and the UDP one is
point-to-point because GX Works3 refuses to save a UDP SLMP entry without a destination
IP address.

**Byte-level pathology lives here; semantic behaviour lives in
:mod:`aslmp.testing.dispatch`.** Coalescing, segmentation, the accept-then-FIN, the
silence on a coding mismatch, the datagram loss -- none of those are decisions about a
request, they are things that happen to bytes, and keeping them out of the dispatcher is
what lets every handler stay a pure function.

``server.transcript`` records every byte in both directions with a monotonic timestamp,
and ``server.error_history`` records every refusal that never reached the wire. Between
them, a failing test can print exactly what the CPU saw and exactly what it decided.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Literal

from aslmp._clock import DEFAULT_CLOCK
from aslmp.profile import Encoding
from aslmp.testing.dispatch import Dispatcher, Reply, SessionState, Silence
from aslmp.testing.pathology import Pathology
from aslmp.testing.targets import FX5U_32MT_DS, SimulatorTarget
from aslmp.wire.codec import ASCII, BINARY, Codec, SlmpCodecError
from aslmp.wire.frames import FOUR_E, FRAMES, THREE_E, FrameFormat, FrameType, response_body
from aslmp.wire.raw import ErrorInfo, SlmpFrameError
from aslmp.wire.route import Route, SlmpRouteError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Awaitable, Callable, Sequence
    from types import TracebackType
    from typing import Self

    from aslmp.testing.dispatch import Outcome
    from aslmp.testing.memory import DeviceMemory
    from aslmp.testing.scenario import Scenario
    from aslmp.wire.raw import RawRequest

__all__ = [
    "BENCH_ENTRIES",
    "ONDEMAND_COMMAND",
    "Entry",
    "PlcSimulator",
    "ServerEvent",
    "TranscriptRecord",
    "codec_for",
]


ONDEMAND_COMMAND: Final = 0x2101
"""The one PLC-originated message. Never an answer to anybody's request."""

_REQUEST_MIN_UNITS: Final = 6
"""Binary units a request's fixed fields occupy after the prefix: the monitoring
timer, the command and the subcommand. An ``L`` below this describes no request."""

_READ_CHUNK: Final = 65536

_CLOSE_TIMEOUT: Final = 2.0
"""How long ``aclose()`` waits for a closed listener to let go. See
:meth:`PlcSimulator._finish_closing` for why this is bounded at all."""
"""How much the server asks the socket for at once.

Deliberately large: reading in small pieces would hide the coalescing corruption, which
happens precisely because several SLMP messages arrive in ONE read.
"""


def codec_for(encoding: Encoding) -> Codec:
    """The codec that renders ``encoding``. Two ASCII members, one codec."""
    return ASCII if encoding.is_ascii else BINARY


@dataclass(frozen=True, slots=True)
class Entry:
    """One SLMP connection entry, as GX Works3 configures it.

    ``max_connections`` is 1 by default because that is what an FX5U SLMP connection
    entry is: a second TCP connection completed its handshake in 5.4 ms and was then
    closed by the CPU, with the incumbent undisturbed (measured on FX5U-32MT/DS fw
    1.065, 2026-09-06). It is only *enforced* when
    :attr:`~aslmp.testing.pathology.Pathology.single_connection` is on, so a target that
    does not have the limit can share this shape.

    ``port=0`` binds an ephemeral port; read the real one back from
    :meth:`PlcSimulator.port_of`.
    """

    name: str
    protocol: Literal["tcp", "udp"]
    port: int = 0
    encoding: Encoding = Encoding.BINARY
    frame: FrameType = FrameType.THREE_E
    max_connections: int = 1

    def __post_init__(self) -> None:
        if self.protocol not in ("tcp", "udp"):
            raise ValueError(f"Entry.protocol is 'tcp' or 'udp', not {self.protocol!r}")
        if not 0 <= self.port <= 65535:
            raise ValueError(f"Entry.port {self.port} is not a port number")
        if self.max_connections < 1:
            raise ValueError("an entry serves at least one connection")

    @property
    def codec(self) -> Codec:
        """The codec this entry's Communication Data Code setting selects."""
        return codec_for(self.encoding)

    @property
    def frame_format(self) -> FrameFormat:
        """The frame format this entry is configured for."""
        return FRAMES[self.frame]

    def __str__(self) -> str:
        return (
            f"{self.name}: {self.protocol.upper()} {self.frame.value} "
            f"{self.encoding.value}, {self.max_connections} connection(s)"
        )


BENCH_ENTRIES: Final[tuple[Entry, ...]] = (
    Entry(name="tcp", protocol="tcp"),
    Entry(name="tcp-4e", protocol="tcp", frame=FrameType.FOUR_E),
    Entry(name="udp", protocol="udp"),
    Entry(name="udp-4e", protocol="udp", frame=FrameType.FOUR_E),
    Entry(name="tcp-ascii", protocol="tcp", encoding=Encoding.ASCII_XY_HEX),
)
"""Five entries shaped like our bench: four TCP-or-UDP binary and one ASCII.

The ASCII entry is a **deliberate departure from the iQ-F**, where the Communication
Data Code is a single Own Node parameter for the whole Ethernet port and binary and ASCII
therefore cannot coexist. Our bench could not test ASCII for exactly that reason, which
is the strongest possible argument for the simulator offering it.
"""


@dataclass(frozen=True, slots=True)
class TranscriptRecord:
    """One direction's worth of bytes on one entry, stamped from a monotonic clock."""

    at: int
    entry: str
    peer: str
    direction: Literal["rx", "tx"]
    data: bytes

    @property
    def hex(self) -> str:
        """The bytes as a diagnostic prints them."""
        return self.data.hex(" ").upper()

    def __str__(self) -> str:
        arrow = "<-" if self.direction == "rx" else "->"
        return f"[{self.entry}] {arrow} {self.peer} {len(self.data)}B {self.hex}"


@dataclass(frozen=True, slots=True)
class ServerEvent:
    """Something the simulated CPU decided that no response can express.

    A refused connection, a silently discarded coalesced request, a dropped datagram, a
    coding mismatch answered with nothing. Every one of these is invisible from the
    client's socket -- that is what makes them worth recording, and what makes a test
    that asserts on them a real test rather than a restatement of the response.
    """

    at: int
    entry: str
    kind: str
    detail: str

    def __str__(self) -> str:
        return f"[{self.entry}] {self.kind}: {self.detail}"


@dataclass(slots=True)
class _Bound:
    """One entry, listening."""

    entry: Entry
    port: int
    server: asyncio.AbstractServer | None = None
    transport: asyncio.DatagramTransport | None = None
    writers: list[asyncio.StreamWriter] = field(default_factory=list)
    last_peer: tuple[str, int] | None = None
    udp_pending: int = 0
    udp_seen: int = 0


class PlcSimulator:
    """An SLMP server that behaves like the PLC you name, including its defects.

    ::

        async with PlcSimulator(target=FX5U_32MT_DS) as plc:
            host, port = plc.address("tcp")
            ...

    One simulator is one CPU: the device memory and the session state are shared across
    every entry, because they are on the same silicon. The pathology board defaults to
    the target's own (:attr:`~aslmp.testing.targets.SimulatorTarget.pathology`), so
    ``FX5U_32MT_DS`` misbehaves out of the box and ``PEDANTIC`` does not.

    ``scan_per_request=True`` runs the bench's own program while the CPU serves: ``D8``
    advances once per request, so registers move underneath a client and a stale-value
    bug has something to fail against
    (:attr:`~aslmp.testing.dispatch.Dispatcher.scan_per_request`).
    """

    __slots__ = (
        "_bound",
        "_clock",
        "_dispatcher",
        "_entries",
        "_events",
        "_host",
        "_last_response",
        "_pathology",
        "_records",
        "_started",
        "_tasks",
    )

    def __init__(
        self,
        *,
        target: SimulatorTarget = FX5U_32MT_DS,
        entries: Sequence[Entry] = BENCH_ENTRIES,
        pathology: Pathology | None = None,
        memory: DeviceMemory | None = None,
        scenario: Scenario | None = None,
        scan_per_request: bool = False,
        host: str = "127.0.0.1",
    ) -> None:
        self._entries = tuple(entries)
        if not self._entries:
            raise ValueError("a simulator serves at least one connection entry")
        names = [entry.name for entry in self._entries]
        if len(set(names)) != len(names):
            raise ValueError(f"connection entry names must be unique; got {names}")
        self._pathology = target.pathology if pathology is None else pathology
        self._dispatcher = Dispatcher(
            target=target,
            memory=target.memory() if memory is None else memory,
            scenario=scenario,
            pathology=self._pathology,
            scan_per_request=scan_per_request,
        )
        self._host = host
        self._bound: dict[str, _Bound] = {}
        self._records: list[TranscriptRecord] = []
        self._events: list[ServerEvent] = []
        self._tasks: set[asyncio.Task[None]] = set()
        self._last_response: dict[int, bytes] = {}
        self._clock = DEFAULT_CLOCK
        self._started = False

    # -- lifecycle -----------------------------------------------------------------

    async def start(self) -> None:
        """Bind every entry. Ephemeral ports are readable afterwards."""
        if self._started:
            raise RuntimeError("this simulator is already started")
        loop = asyncio.get_running_loop()
        for entry in self._entries:
            if entry.protocol == "tcp":
                server = await asyncio.start_server(
                    _tcp_callback(self, entry), self._host, entry.port
                )
                sockets = server.sockets
                port = int(sockets[0].getsockname()[1]) if sockets else entry.port
                self._bound[entry.name] = _Bound(entry=entry, port=port, server=server)
            else:
                transport, _protocol = await loop.create_datagram_endpoint(
                    functools.partial(_DatagramEndpoint, self, entry),
                    local_addr=(self._host, entry.port),
                )
                port = int(transport.get_extra_info("sockname")[1])
                self._bound[entry.name] = _Bound(entry=entry, port=port, transport=transport)
        self._started = True

    async def aclose(self) -> None:
        """Close every listener and every open connection."""
        for bound in self._bound.values():
            for writer in list(bound.writers):
                writer.close()
            bound.writers.clear()
            if bound.server is not None:
                bound.server.close()
                await self._finish_closing(bound)
            if bound.transport is not None:
                bound.transport.close()
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        self._bound.clear()
        self._started = False

    async def _finish_closing(self, bound: _Bound) -> None:
        """Wait for a closed listener to let go of its connections, but never forever.

        ``Server.wait_closed()`` waits for every ACCEPTED connection to detach, and there
        is a window in which one exists that this simulator has never been told about and
        therefore cannot have closed. asyncio attaches a new transport to the server
        inside the transport's constructor and only then schedules ``connection_made``,
        so between those two steps the connection is in ``server._clients`` while the
        handler that would register its writer has not run. ``aclose()`` closes the
        writers it knows about, ``server.close()`` only stops listening, and nothing is
        ever going to close that one: ``wait_closed()`` then waits on it forever.

        Measured on 2026-09-24, Python 3.13.14 on Windows, by connecting a raw blocking
        socket and stepping the loop a controlled number of times before calling
        ``aclose()``::

            yields=0  server._clients=0  bound.writers=0  -> returned
            yields=2  server._clients=1  bound.writers=0  -> HUNG
            yields=5  server._clients=1  bound.writers=1  -> returned

        It is a one-iteration window and it was invisible until recently: ``wait_closed()``
        returned immediately before CPython 3.12.1 and only waits from 3.12.1 on. That is
        why CI hung on every 3.12 cell, on no 3.11 cell, and on 3.13 only where the runner
        happened to land in the window (2026-09-24, nine-cell matrix, commit d78ef84).

        3.13 added ``abort_clients()`` for exactly this and it makes the teardown
        deterministic. Below 3.13 there is no public way to reach that connection, so the
        wait is bounded instead and the simulator SAYS it gave up rather than hanging the
        caller's test suite. A test double that cannot be torn down is worse than one that
        tears down loudly.
        """
        assert bound.server is not None  # narrowed by the caller
        try:
            await asyncio.wait_for(bound.server.wait_closed(), _CLOSE_TIMEOUT)
        except TimeoutError:
            # Force the stragglers, but only now: aborting before the ordinary wait kills
            # handlers that have not yet read bytes already sitting in their buffers.
            abort = getattr(bound.server, "abort_clients", None)  # CPython 3.13+
            if abort is not None:
                abort()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(bound.server.wait_closed(), _CLOSE_TIMEOUT)
            self._event(
                bound.entry,
                "close_timed_out",
                f"the listener stopped accepting but a connection did not detach within "
                f"{_CLOSE_TIMEOUT:g} s, so aclose() stopped waiting for it. On CPython "
                f"below 3.13 a connection accepted in the instant before close cannot be "
                f"reached to be closed; it goes when the process does. The simulator is "
                f"shut down either way.",
            )

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    # -- introspection -------------------------------------------------------------

    @property
    def target(self) -> SimulatorTarget:
        """Which CPU this simulator is pretending to be."""
        return self._dispatcher.target

    @property
    def pathology(self) -> Pathology:
        """The board in force."""
        return self._pathology

    @property
    def memory(self) -> DeviceMemory:
        """The device memory, for seeding and for asserting after a write."""
        return self._dispatcher.memory

    @property
    def dispatcher(self) -> Dispatcher:
        """The command dispatcher, for the session state and the scan counter."""
        return self._dispatcher

    @property
    def state(self) -> SessionState:
        """The CPU's session state: the key switch, the last remote request, the lock.

        The inputs only. What the CPU *reports* is ``SD203`` in :attr:`memory`, and
        :meth:`cpu_state` reads it the way a client does.
        """
        return self._dispatcher.state

    def cpu_state(self) -> int:
        """What ``SD203`` holds: one of the :class:`~aslmp.testing.dispatch.CpuRunState`
        values, read out of device memory rather than out of the state object.

        Re-derived at the top of every served request, so a key switch turned since the
        last one reaches the register on the next scan.
        """
        return self._dispatcher.cpu_state()

    @property
    def entries(self) -> tuple[Entry, ...]:
        """Every configured entry."""
        return self._entries

    @property
    def transcript(self) -> tuple[TranscriptRecord, ...]:
        """Every byte in both directions, in order."""
        return tuple(self._records)

    @property
    def error_history(self) -> tuple[ServerEvent, ...]:
        """Every decision no response could express."""
        return tuple(self._events)

    def events_of(self, kind: str) -> tuple[ServerEvent, ...]:
        """Just the events of one kind, which is what a test asserts on."""
        return tuple(event for event in self._events if event.kind == kind)

    def entry(self, name: str) -> Entry:
        """The configured entry called ``name``."""
        for entry in self._entries:
            if entry.name == name:
                return entry
        raise KeyError(
            f"no connection entry named {name!r}; this simulator has "
            f"{[entry.name for entry in self._entries]}"
        )

    def port_of(self, name: str) -> int:
        """The bound port of ``name``. Only meaningful after :meth:`start`."""
        bound = self._bound.get(name)
        if bound is None:
            raise KeyError(f"connection entry {name!r} is not listening; call start() first")
        return bound.port

    def address(self, name: str) -> tuple[str, int]:
        """``(host, port)`` for ``name``."""
        return self._host, self.port_of(name)

    def clear_transcript(self) -> None:
        """Forget every recorded byte and event. For a test with a noisy setup phase."""
        self._records.clear()
        self._events.clear()

    # -- recording -----------------------------------------------------------------

    def _record(
        self, entry: Entry, peer: object, direction: Literal["rx", "tx"], data: bytes
    ) -> None:
        self._records.append(
            TranscriptRecord(
                at=self._clock(),
                entry=entry.name,
                peer=str(peer),
                direction=direction,
                data=bytes(data),
            )
        )

    def _event(self, entry: Entry, kind: str, detail: str) -> None:
        self._events.append(
            ServerEvent(at=self._clock(), entry=entry.name, kind=kind, detail=detail)
        )

    # -- framing -------------------------------------------------------------------

    def _frame_for(self, entry: Entry, data: bytes) -> FrameFormat | None:
        """Which frame format this message is, or ``None`` if it is neither.

        Two independent questions, and they were one until 2026-09-07. **Does this CPU
        family speak the format at all** is
        :meth:`~aslmp.testing.targets.SimulatorTarget.serves_frame`, and a format it does
        not speak is not recognised on any entry -- ``None`` here, which the caller turns
        into the same silence or ``0xC06F`` any unrecognised subheader gets. **Is the
        format allowed on *this* entry** is
        :attr:`~aslmp.testing.pathology.Pathology.accept_4e_on_3e_entry`: an
        FX5U-32MT/DS accepts a 4E frame on a connection entry configured for 3E
        (measured 2026-09-06), against two Mitsubishi manuals, so that is a switch and
        not a constant.
        """
        codec = entry.codec
        configured = entry.frame_format
        head = configured.request.for_codec(codec)
        if data[: len(head)] == head:
            return configured if self.target.serves_frame(configured.frame_type) else None
        if not self._pathology.accept_4e_on_3e_entry:
            return None
        other = FOUR_E if configured is THREE_E else THREE_E
        other_head = other.request.for_codec(codec)
        if data[: len(other_head)] == other_head:
            return other if self.target.serves_frame(other.frame_type) else None
        return None

    def _peek(
        self, entry: Entry, frame: FrameFormat, data: bytes
    ) -> tuple[Route, int | None, bytes, int] | None:
        """``(route, serial, subheader tail, L)``, or ``None`` while the prefix is short.

        Deliberately **lenient**: it does not apply the minimum-``L`` rule, because a
        request whose ``L`` is below the minimum is a real thing an FX5U answers rather
        than ignores. ``L = 0x0000`` came back ``0xC061`` with the command echoed as
        ``0x0000`` -- the CPU read the command out of the monitoring timer field
        (measured 2026-09-06).
        """
        codec = entry.codec
        if len(data) < frame.prefix_units(codec):
            return None
        subheader_units = frame.subheader_units(codec)
        step = codec.number_len(16)
        serial: int | None = None
        tail = b""
        try:
            if frame.carries_serial:
                serial = codec.read_number(data, step, bits=16)
                tail = bytes(data[2 * step : subheader_units])
            route = Route.decode(data, subheader_units, codec)
            declared = codec.read_number(data, subheader_units + Route.wire_len(codec), bits=16)
        except (SlmpCodecError, SlmpRouteError):
            return None
        return route, serial, tail, declared

    def _build_response(
        self,
        entry: Entry,
        frame: FrameFormat,
        *,
        route: Route,
        serial: int | None,
        tail: bytes,
        command: int,
        subcommand: int,
        reply: Reply,
    ) -> bytes:
        codec = entry.codec
        if reply.end_code:
            body = response_body(
                codec,
                end_code=reply.end_code,
                error_info=ErrorInfo(responding=route, command=command, subcommand=subcommand),
            )
        else:
            body = response_body(codec, end_code=0x0000, payload=reply.payload)
        echoed = serial
        if serial is not None and self._pathology.wrong_serial_echo:
            echoed = (serial + 1) & 0xFFFF
        raw = frame.build_response(route=route, body=body, codec=codec, serial=echoed)
        if frame.carries_serial and not self._pathology.zero_4e_tail and tail:
            step = codec.number_len(16)
            raw = raw[: 2 * step] + tail + raw[2 * step + len(tail) :]
        return raw

    def _error_frame(
        self,
        entry: Entry,
        frame: FrameFormat,
        *,
        route: Route,
        serial: int | None,
        end_code: int,
    ) -> bytes:
        """An abnormal response echoing command and subcommand as zero.

        The measured shape of the ``L = 0x0000`` case: the CPU answered ``0xC061`` and
        echoed ``cmd = 0x0000 sub = 0x0000``, because it read the command out of the
        monitoring timer field of a frame that had none.
        """
        return self._build_response(
            entry,
            frame,
            route=route,
            serial=serial,
            tail=b"",
            command=0x0000,
            subcommand=0x0000,
            reply=Reply(end_code),
        )

    def _serve_one(
        self, entry: Entry, frame: FrameFormat, request: RawRequest
    ) -> Outcome:
        """Hand one decoded request to the dispatcher."""
        return self._dispatcher.handle(request, codec=entry.codec, encoding=entry.encoding)

    def _parse_messages(
        self, entry: Entry, buffer: bytearray
    ) -> tuple[list[tuple[FrameFormat, RawRequest]], list[bytes], str | None]:
        """Take every complete message out of ``buffer``.

        Returns the decoded requests, any ready-made error frames (the below-minimum
        ``L`` case, which is answered rather than decoded), and a fault reason when the
        buffer holds something that is not this entry's protocol at all.
        """
        messages: list[tuple[FrameFormat, RawRequest]] = []
        canned: list[bytes] = []
        codec = entry.codec
        while buffer:
            if len(buffer) < codec.number_len(16):
                break  # too few bytes to recognise a subheader at all
            frame = self._frame_for(entry, bytes(buffer))
            if frame is None:
                head = bytes(buffer[:4]).hex(" ").upper()
                return messages, canned, (
                    f"subheader {head} is not this entry's "
                    f"{entry.frame.value} {entry.encoding.value} request subheader"
                )
            peeked = self._peek(entry, frame, bytes(buffer))
            if peeked is None:
                if len(buffer) >= frame.prefix_units(codec):
                    return messages, canned, "the frame prefix does not decode in this coding"
                break
            route, serial, _tail, declared = peeked
            prefix = frame.prefix_units(codec)
            minimum = _REQUEST_MIN_UNITS * codec.width
            if declared < minimum:
                canned.append(
                    self._error_frame(
                        entry,
                        frame,
                        route=route,
                        serial=serial,
                        end_code=self.target.end_codes.request_length_mismatch,
                    )
                )
                # Everything buffered goes with it. The CPU that answered 0xC061 for an
                # L of 0x0000 also swallowed the twelve bytes that followed it, and the
                # connection recovered on the next request (FX5U-32MT/DS fw 1.065,
                # measured 2026-09-06). Leaving them here would invent a second framing
                # error for one mistake.
                buffer.clear()
                continue
            total = prefix + declared
            if len(buffer) < total:
                break
            raw = bytes(buffer[:total])
            del buffer[:total]
            try:
                messages.append((frame, frame.parse_request(raw, codec)))
            except SlmpFrameError:
                canned.append(
                    self._error_frame(
                        entry,
                        frame,
                        route=route,
                        serial=serial,
                        end_code=self.target.end_codes.request_length_mismatch,
                    )
                )
                buffer.clear()
                break
        return messages, canned, None

    # -- TCP -----------------------------------------------------------------------

    async def serve_connection(
        self, entry: Entry, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Serve one accepted TCP connection until the peer closes it.

        Public because ``asyncio.start_server``'s callback is created outside the class;
        a caller has no reason to invoke it.
        """
        peer = writer.get_extra_info("peername")
        bound = self._bound.get(entry.name)
        if bound is None:  # pragma: no cover - the listener outlives its record only on close
            writer.close()
            return
        if self._pathology.single_connection and len(bound.writers) >= entry.max_connections:
            self._event(
                entry,
                "connection_refused",
                f"{peer}: the entry already has {len(bound.writers)} connection(s) and "
                f"serves {entry.max_connections}. Accepted, then closed immediately -- "
                f"the incumbent is undisturbed and the slot frees on close (measured on "
                f"FX5U-32MT/DS fw 1.065, 2026-09-06).",
            )
            writer.close()
            return
        bound.writers.append(writer)
        bound.last_peer = peer
        try:
            await self._tcp_loop(entry, reader, writer, peer)
        except (ConnectionResetError, BrokenPipeError):  # pragma: no cover - peer-dependent
            pass
        finally:
            if writer in bound.writers:
                bound.writers.remove(writer)
            self._last_response.pop(id(writer), None)
            writer.close()

    async def _tcp_loop(
        self,
        entry: Entry,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        peer: object,
    ) -> None:
        buffer = bytearray()
        while True:
            data = await self._read_tcp(entry, reader, writer, peer, buffer)
            if data is None:
                return
            if not data:
                continue
            self._record(entry, peer, "rx", data)
            buffer += data
            messages, canned, fault = self._parse_messages(entry, buffer)
            framing_error = bool(canned)
            for frame_bytes in canned:
                await self._send(entry, writer, peer, frame_bytes)
            if len(messages) > 1 and self._pathology.coalesce_requests:
                dropped = messages[:-1]
                self._event(
                    entry,
                    "coalesced",
                    f"{len(messages)} SLMP messages arrived in one read; answering only "
                    f"the LAST, end code 0x0000, and discarding "
                    f"{[f'0x{req.command:04X}' for _f, req in dropped]} silently. On 3E "
                    f"there is no serial, so the client pairs the answer with the FIRST "
                    f"request (measured on FX5U-32MT/DS fw 1.065, 2026-09-06).",
                )
                messages = messages[-1:]
            for frame, request in messages:
                end_code, close = await self._answer_tcp(entry, frame, request, writer, peer)
                if end_code == self.target.end_codes.request_length_mismatch:
                    framing_error = True
                if close:
                    return
            if fault is None:
                continue
            buffer.clear()
            if framing_error:
                # The CPU that answered 0xC061 for an understated data length also
                # swallowed the bytes that fell outside the frame it had just read, and
                # answered the NEXT request normally (FX5U-32MT/DS fw 1.065, measured
                # 2026-09-06). Reporting the remainder as a second, different fault would
                # invent a coding mismatch out of one length mistake.
                self._event(
                    entry,
                    "resynchronised",
                    f"discarding the bytes left over by a framing error: {fault}",
                )
                continue
            if self._pathology.silence_on_wrong_encoding:
                self._event(
                    entry,
                    "coding_mismatch_silence",
                    f"{fault}. An FX5U-32MT/DS fw 1.065 answers this with NOTHING -- "
                    f"no end code, no reset (measured 2026-09-06).",
                )
                continue
            self._event(entry, "coding_mismatch", fault)
            await self._send(
                entry,
                writer,
                peer,
                self._error_frame(
                    entry,
                    entry.frame_format,
                    route=Route.OWN_STATION,
                    serial=0 if entry.frame_format.carries_serial else None,
                    end_code=self.target.end_codes.ascii_into_binary_entry,
                ),
            )

    async def _read_tcp(
        self,
        entry: Entry,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        peer: object,
        buffer: bytearray,
    ) -> bytes | None:
        """One read.

        ``None`` means the peer is gone. Empty ``bytes`` means "nothing new, keep going"
        -- which is what an answered overstated-length timeout leaves behind.
        """
        if not buffer or self._pathology.overstated_length_hangs:
            return await reader.read(_READ_CHUNK) or None
        try:
            return await asyncio.wait_for(
                reader.read(_READ_CHUNK), self._pathology.overstated_length_grace_s
            ) or None
        except TimeoutError:
            self._event(
                entry,
                "overstated_length",
                f"{len(buffer)} byte(s) of a frame that declared more never completed. "
                f"An FX5U-32MT/DS fw 1.065 blocks here forever and the client sees a "
                f"timeout indistinguishable from a dead PLC; this target answers "
                f"instead (measured 2026-09-06).",
            )
            frame = self._frame_for(entry, bytes(buffer)) or entry.frame_format
            peeked = self._peek(entry, frame, bytes(buffer))
            route = Route.OWN_STATION if peeked is None else peeked[0]
            serial = None if peeked is None else peeked[1]
            buffer.clear()
            await self._send(
                entry,
                writer,
                peer,
                self._error_frame(
                    entry,
                    frame,
                    route=route,
                    serial=serial,
                    end_code=self.target.end_codes.request_length_mismatch,
                ),
            )
            return b""

    async def _answer_tcp(
        self,
        entry: Entry,
        frame: FrameFormat,
        request: RawRequest,
        writer: asyncio.StreamWriter,
        peer: object,
    ) -> tuple[int | None, bool]:
        """Serve one request.

        Returns the end code that went out (``None`` for silence) and whether the
        connection must be torn down.
        """
        outcome = self._serve_one(entry, frame, request)
        if isinstance(outcome, Silence):
            self._event(entry, "silence", outcome.reason)
            if outcome.close_after:
                writer.close()
                return None, True
            return None, False
        raw = self._build_response(
            entry,
            frame,
            route=request.route,
            serial=request.serial,
            tail=request.subheader_tail,
            command=request.command,
            subcommand=request.subcommand,
            reply=outcome,
        )
        if self._pathology.stale_reply:
            previous = self._last_response.get(id(writer))
            self._last_response[id(writer)] = raw
            if previous is None:
                self._event(
                    entry,
                    "stale_reply_primed",
                    "the first request on this connection has no previous response to "
                    "return, so it is answered normally; every later one is one behind.",
                )
            else:
                self._event(
                    entry,
                    "stale_reply",
                    f"answering 0x{request.command:04X} with the PREVIOUS response on "
                    f"this connection",
                )
                raw = previous
        await self._send(entry, writer, peer, raw)
        if outcome.close_after:
            writer.close()
            return outcome.end_code, True
        return outcome.end_code, False

    async def _send(
        self, entry: Entry, writer: asyncio.StreamWriter, peer: object, data: bytes
    ) -> None:
        """Write a response, segmented and delayed as the board says."""
        if self._pathology.late_reply_s:
            await asyncio.sleep(self._pathology.late_reply_s)
        limit = self._pathology.segment_at
        if limit is not None and len(data) > limit:
            self._event(
                entry,
                "segmented",
                f"a {len(data)}-byte response split at {limit}. One of three identical "
                f"1931-byte reads arrived as 1460 + 471, 3.0 ms apart, on FX5U-32MT/DS "
                f"fw 1.065 (measured 2026-09-06).",
            )
            for start in range(0, len(data), limit):
                chunk = data[start : start + limit]
                writer.write(chunk)
                await writer.drain()
                self._record(entry, peer, "tx", chunk)
                if self._pathology.segment_gap_s and start + limit < len(data):
                    await asyncio.sleep(self._pathology.segment_gap_s)
            return
        writer.write(data)
        await writer.drain()
        self._record(entry, peer, "tx", data)

    # -- UDP -----------------------------------------------------------------------

    def feed_datagram(self, entry: Entry, data: bytes, addr: tuple[str, int]) -> None:
        bound = self._bound.get(entry.name)
        if bound is None:  # pragma: no cover - closed mid-flight
            return
        bound.last_peer = addr
        bound.udp_seen += 1
        self._record(entry, addr, "rx", data)
        nth = self._pathology.drop_every_nth_datagram
        if nth is not None and bound.udp_seen % nth == 0:
            self._event(
                entry,
                "datagram_dropped",
                f"datagram {bound.udp_seen} from {addr} dropped deterministically",
            )
            return
        depth = self._pathology.udp_drop_above_depth
        if depth is not None and bound.udp_pending >= depth:
            self._event(
                entry,
                "datagram_dropped",
                f"{bound.udp_pending} datagram(s) already unanswered and this CPU serves "
                f"{depth} in flight. Dropped with no end code, no ICMP and no error of "
                f"any kind: 64 pipelined 4E reads returned 44 on FX5U-32MT/DS fw 1.065 "
                f"(measured 2026-09-06).",
            )
            return
        bound.udp_pending += 1
        task = asyncio.get_running_loop().create_task(self._serve_datagram(entry, data, addr))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _serve_datagram(self, entry: Entry, data: bytes, addr: tuple[str, int]) -> None:
        bound = self._bound.get(entry.name)
        try:
            if self._pathology.udp_service_delay_s:
                await asyncio.sleep(self._pathology.udp_service_delay_s)
            if bound is None or bound.transport is None:
                return
            buffer = bytearray(data)
            messages, canned, fault = self._parse_messages(entry, buffer)
            framing_error = bool(canned)
            for frame_bytes in canned:
                self._sendto(entry, bound.transport, addr, frame_bytes)
            for frame, request in messages:
                outcome = self._serve_one(entry, frame, request)
                if isinstance(outcome, Silence):
                    self._event(entry, "silence", outcome.reason)
                    continue
                if outcome.end_code == self.target.end_codes.request_length_mismatch:
                    framing_error = True
                if self._pathology.late_reply_s:
                    await asyncio.sleep(self._pathology.late_reply_s)
                self._sendto(
                    entry,
                    bound.transport,
                    addr,
                    self._build_response(
                        entry,
                        frame,
                        route=request.route,
                        serial=request.serial,
                        tail=request.subheader_tail,
                        command=request.command,
                        subcommand=request.subcommand,
                        reply=outcome,
                    ),
                )
            if fault is None and not buffer:
                return
            trailing = fault or (
                f"{len(buffer)} trailing byte(s): a UDP datagram must be exactly one "
                f"SLMP message"
            )
            # A datagram whose declared length understated its own body leaves a tail
            # that is nobody's message. The CPU answered 0xC061 and swallowed it
            # (FX5U-32MT/DS fw 1.065, measured 2026-09-06); reporting the remainder as a
            # second, different fault would invent a coding mismatch out of one mistake.
            kind = "resynchronised" if framing_error else "datagram_malformed"
            self._event(entry, kind, trailing)
        finally:
            if bound is not None:
                bound.udp_pending -= 1

    def _sendto(
        self,
        entry: Entry,
        transport: asyncio.DatagramTransport,
        addr: tuple[str, int],
        data: bytes,
    ) -> None:
        transport.sendto(data, addr)
        self._record(entry, addr, "tx", data)

    # -- the one message a PLC sends on its own ------------------------------------

    async def push_ondemand(
        self, payload: bytes = b"", *, entry_name: str | None = None
    ) -> int:
        """Send an unsolicited ``2101`` Ondemand frame. Returns how many peers got it.

        It goes out with a **request** subheader, because the PLC is the sender. A client
        that resynchronises on it -- skipping forward to the next thing that looks like a
        subheader -- turns the previous transaction's data into this transaction's
        answer, which is why an Ondemand frame must raise and never be parsed as
        somebody's response.
        """
        sent = 0
        for name, bound in self._bound.items():
            if entry_name is not None and name != entry_name:
                continue
            entry = bound.entry
            codec = entry.codec
            frame = entry.frame_format
            body = (
                codec.number(0, bits=16)
                + codec.number(ONDEMAND_COMMAND, bits=16)
                + codec.number(0, bits=16)
                + payload
            )
            raw = frame.build(
                route=Route.OWN_STATION,
                body=body,
                codec=codec,
                serial=0 if frame.carries_serial else None,
            )
            for writer in list(bound.writers):
                writer.write(raw)
                await writer.drain()
                self._record(entry, writer.get_extra_info("peername"), "tx", raw)
                sent += 1
            if bound.transport is not None and bound.last_peer is not None:
                self._sendto(entry, bound.transport, bound.last_peer, raw)
                sent += 1
        return sent


def _tcp_callback(
    simulator: PlcSimulator, entry: Entry
) -> Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]:
    """A per-entry ``asyncio.start_server`` callback.

    A closure factory rather than a lambda in the loop, so that each listener holds its
    own entry: the bug where every server serves the last entry in the list is silent and
    produces a simulator that answers the wrong coding on four of five ports.
    """

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await simulator.serve_connection(entry, reader, writer)

    return handle


class _DatagramEndpoint(asyncio.DatagramProtocol):
    """One UDP entry's protocol. A datagram is one SLMP message, always."""

    def __init__(self, simulator: PlcSimulator, entry: Entry) -> None:
        self._simulator = simulator
        self._entry = entry

    def datagram_received(self, data: bytes, addr: tuple[str | Any, int]) -> None:
        self._simulator.feed_datagram(self._entry, data, (str(addr[0]), int(addr[1])))
