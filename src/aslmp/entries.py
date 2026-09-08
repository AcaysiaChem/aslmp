"""Layer 6 -- several clients, one per **configured connection entry**. Not a pool.

.. rubric:: Why there is no dispatch

An SLMP connection entry is not a unit of capacity. It is a row in the GX Works3 Ethernet
Port "External Device Configuration" table with its own protocol, its own port and --
because the Communication Data Code is an Own Node parameter -- its own coding. Our own
bench has six of them and they are not interchangeable: four are TCP and two are
**UDP**, which on FX5U-32MT/DS fw 1.065 has a different latency distribution (p50 2.42 ms
against 3.63 ms, p99 3.56 against 4.69, n=300 each, 2026-09-07 from ``argus-bench`` over
the **wired** link) and a different failure mode (a lost datagram, with no end code and
no ICMP). A UDP entry is also point-to-point -- it serves the one host GX Works3 was told
about -- so the two are not even reachable from the same places.

So :class:`EntryGroup` has named handles and nothing else. There is deliberately **no**
``lease()``, no round-robin, no "least loaded" and no automatic failover (DESIGN.md graft
G7). A group that picked an entry for you would silently move a read from TCP to UDP
between one cycle and the next, and the only visible trace would be a latency number that
had changed shape for no reason anybody could name. ``tests/unit/test_entries.py``
asserts the absence, because an absence is the one design decision a later contributor
can undo without noticing.

.. rubric:: Why connecting is sequential

:meth:`EntryGroup.connect_all` opens the entries one at a time, in the order they were
given. Every entry in a group is usually the same CPU, ``connect()`` costs one ~7 ms
handshake round trip, and a failure that names *which* entry failed is worth more than
30 ms. The rollback is explicit too: if entry three refuses, the two already open are
closed and the original exception is re-raised -- a half-connected group is a thing
nobody would remember to clean up.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from types import TracebackType
from typing import Self, final

from aslmp.client import Plc
from aslmp.connection import ConnectionInfo, ConnectionState
from aslmp.errors import SlmpConfigurationError, SlmpError
from aslmp.observability import MetricsSnapshot
from aslmp.profile import Encoding
from aslmp.transport.base import TransportKind
from aslmp.wire.frames import FrameType

__all__ = ["Entry", "EntryGroup"]


@final
@dataclass(frozen=True, slots=True)
class Entry:
    """One configured SLMP connection entry, as GX Works3 describes it.

    A description, not a connection: it is what you write down when you configure the
    PLC and what :meth:`EntryGroup.entries` hands back so that a latency number can be
    attributed to the transport that produced it. The three protocol fields carry the
    same factory defaults :class:`aslmp.client.Plc` does, and for the same reason -- a
    default here is a constant you override, never something this library detects.
    """

    name: str
    host: str
    port: int
    transport: TransportKind = TransportKind.TCP
    encoding: Encoding = Encoding.BINARY
    frame: FrameType = FrameType.THREE_E

    def __post_init__(self) -> None:
        if not self.name:
            raise SlmpConfigurationError(
                "a connection entry needs a name: the name is how a caller says which "
                "transport served a request, and an unnamed entry destroys exactly that."
            )
        if not 0 <= self.port <= 65535:
            raise SlmpConfigurationError(
                f"entry {self.name!r} has port {self.port}, which is not a port number."
            )

    @classmethod
    def of(cls, name: str, client: Plc) -> Entry:
        """Describe a live client as the entry it was configured against."""
        host, port = client.peer
        return cls(
            name=name,
            host=host,
            port=port,
            transport=client.transport,
            encoding=client.encoding,
            frame=client.frame,
        )

    @property
    def address(self) -> tuple[str, int]:
        """``(host, port)``."""
        return (self.host, self.port)

    def __str__(self) -> str:
        return (
            f"{self.name}: {self.transport.value}://{self.host}:{self.port} "
            f"{self.frame.value}/{self.encoding.value}"
        )


@final
class EntryGroup:
    """Several :class:`~aslmp.client.Plc` clients under named handles. No dispatch.

    ::

        group = EntryGroup({"fast": udp_client, "safe": tcp_client})
        async with group:
            pv = await group["fast"].read_f32("D2")
            await group["safe"].write_f32("D0", 42.0)

    The mapping is snapshotted at construction and its order is preserved, so
    :meth:`connect_all` and :meth:`aclose` are deterministic.
    """

    __slots__ = ("_clients",)

    def __init__(self, clients: Mapping[str, Plc]) -> None:
        if not clients:
            raise SlmpConfigurationError(
                "an EntryGroup with no entries has nothing to name. Construct it with "
                "at least one {name: Plc} pair."
            )
        self._clients: dict[str, Plc] = dict(clients)

    # -- named handles, and nothing else -------------------------------------

    def __getitem__(self, name: str) -> Plc:
        """The client configured for entry ``name``.

        Raises :class:`~aslmp.errors.SlmpConfigurationError` rather than ``KeyError``,
        naming the entries that do exist: reaching for an entry that was never
        configured is a configuration mistake, and the fix is one line above the call.
        """
        client = self._clients.get(name)
        if client is None:
            known = ", ".join(repr(key) for key in self._clients)
            raise SlmpConfigurationError(
                f"this group has no entry named {name!r}. Configured entries: {known}. "
                f"An EntryGroup never picks an entry for you -- entries are separately "
                f"configured GX Works3 objects with their own protocol, coding and "
                f"latency distribution, and one of ours is UDP."
            )
        return client

    def __contains__(self, name: object) -> bool:
        return name in self._clients

    def __iter__(self) -> Iterator[str]:
        """Iterating a group yields entry **names**, in configuration order."""
        return iter(self._clients)

    def __len__(self) -> int:
        return len(self._clients)

    @property
    def names(self) -> tuple[str, ...]:
        """Every configured entry name, in order."""
        return tuple(self._clients)

    def clients(self) -> Mapping[str, Plc]:
        """The clients themselves, as a fresh mapping. Attribution stays attached."""
        return dict(self._clients)

    def entries(self) -> Mapping[str, Entry]:
        """What each handle is actually configured as -- transport, coding, frame."""
        return {name: Entry.of(name, client) for name, client in self._clients.items()}

    def states(self) -> Mapping[str, ConnectionState]:
        """Where each entry's connection is right now."""
        return {name: client.state for name, client in self._clients.items()}

    def metrics(self) -> Mapping[str, MetricsSnapshot]:
        """One frozen metrics view per entry. Never merged: a merged p99 across a TCP
        and a UDP entry describes no connection that exists."""
        return {name: client.metrics() for name, client in self._clients.items()}

    def __repr__(self) -> str:
        inside = ", ".join(
            f"{name}={client.state.value}" for name, client in self._clients.items()
        )
        return f"EntryGroup({inside})"

    # -- lifecycle -----------------------------------------------------------

    async def connect_all(self) -> Mapping[str, ConnectionInfo]:
        """Connect every entry in order, and prove every one of them.

        On the first failure the entries already opened are closed and the original
        exception is re-raised unchanged: a group that reports success while two of its
        five entries are shut is the kind of half-truth this library exists to refuse.
        """
        opened: dict[str, ConnectionInfo] = {}
        for name, client in self._clients.items():
            try:
                opened[name] = await client.connect()
            except SlmpError:
                await self._close(tuple(opened))
                raise
        return opened

    async def aclose(self) -> None:
        """Close every entry. Every one is attempted; failures are reported together."""
        await self._close(self.names)

    async def _close(self, names: tuple[str, ...]) -> None:
        failures: list[Exception] = []
        for name in names:
            try:
                await self._clients[name].aclose()
            except Exception as exc:
                # Collected, never dropped: every entry is attempted and the lot is
                # re-raised below as an ExceptionGroup. One socket that refuses to shut
                # must not leave the other four open.
                failures.append(exc)
        if failures:
            raise ExceptionGroup(
                f"{len(failures)} of {len(names)} entries failed to close", failures
            )

    async def __aenter__(self) -> Self:
        await self.connect_all()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        await self.aclose()
