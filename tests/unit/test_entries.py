"""Named handles per configured connection entry -- and the dispatch that is not there.

The load-bearing assertion in this file is an **absence**. DESIGN.md graft G7 removed
``EntryBank.lease()`` because a connection entry is not fungible capacity: our own six
are four TCP and two UDP, and UDP on FX5U-32MT/DS fw 1.065 has both a different latency
distribution (p50 2.42 ms against 3.63 ms, 2026-09-07 from ``argus-bench`` over the wired
link) and a different failure mode
(a lost datagram with no end code and no ICMP). A UDP entry is also point-to-point, so it
is not even reachable from the same hosts. A group that chose an entry for the caller
would move a read between those two silently, and the only trace would be a latency number
that had changed shape.

An absence cannot be defended by a docstring, so it is asserted here by reflection, next
to a group built over the entries the simulator serves -- including the UDP and the
ASCII ones -- so that "attribution survives" is demonstrated rather than claimed.
"""

from __future__ import annotations

import pytest

from aslmp.client import Plc
from aslmp.connection import ConnectionState
from aslmp.entries import Entry, EntryGroup
from aslmp.errors import SlmpConfigurationError, SlmpTransportError
from aslmp.profile import Encoding
from aslmp.testing.server import BENCH_ENTRIES, PlcSimulator
from aslmp.transport.base import TransportKind
from aslmp.wire.frames import FrameType

pytestmark = pytest.mark.simulator

FX5U = "melsec:iq-f/fx5u"
BENCH_HOST = "192.168.10.250"


def a_client(port: int = 5002, **kwargs: object) -> Plc:
    """A client aimed at the bench's own settings. Constructing one opens no socket."""
    return Plc(BENCH_HOST, port, profile=FX5U, **kwargs)  # type: ignore[arg-type]  # kwargs


def client_for(simulator: PlcSimulator, entry: str) -> Plc:
    host, port = simulator.address(entry)
    configured = simulator.entry(entry)
    return Plc(
        host,
        port,
        profile=FX5U,
        encoding=configured.encoding,
        frame=configured.frame,
        transport=(
            TransportKind.TCP if configured.protocol == "tcp" else TransportKind.UDP
        ),
        timeout=2.0,
        name=entry,
    )


# ========================================================================================
# The absence (graft G7)
# ========================================================================================


def test_the_group_has_no_dispatch_of_any_kind() -> None:
    """No lease, no round-robin, no failover. Naming an entry is the only way in."""
    forbidden = {
        "lease",
        "acquire",
        "next",
        "any",
        "pick",
        "choose",
        "round_robin",
        "least_loaded",
        "failover",
        "borrow",
        "release",
    }
    assert forbidden.isdisjoint(dir(EntryGroup))


def test_the_group_is_not_a_sequence_so_indexing_cannot_mean_the_nth_entry() -> None:
    """``group[0]`` must not quietly be "whichever entry sorted first"."""
    group = EntryGroup({"a": a_client(5002), "b": a_client(5003)})
    with pytest.raises(SlmpConfigurationError, match="no entry named 0"):
        group[0]  # type: ignore[index]  # the point of the test


# ========================================================================================
# Entry, the description
# ========================================================================================


def test_an_entry_carries_the_three_connection_entry_facts() -> None:
    entry = Entry(
        name="udp",
        host=BENCH_HOST,
        port=5001,
        transport=TransportKind.UDP,
        encoding=Encoding.ASCII_XY_OCT,
        frame=FrameType.FOUR_E,
    )
    assert entry.address == (BENCH_HOST, 5001)
    rendered = str(entry)
    assert "udp://192.168.10.250:5001" in rendered
    assert "4E" in rendered
    assert "ascii-xy-oct" in rendered


def test_entry_defaults_match_the_clients_declared_constants() -> None:
    """A default is a constant the caller overrides; it is never a detection."""
    entry = Entry(name="tcp", host=BENCH_HOST, port=5002)
    assert entry.transport is TransportKind.TCP
    assert entry.encoding is Encoding.BINARY
    assert entry.frame is FrameType.THREE_E


@pytest.mark.parametrize(
    ("name", "port", "match"),
    [("", 5002, "needs a name"), ("tcp", 70000, "not a port number")],
)
def test_an_unusable_entry_is_refused_at_construction(
    name: str, port: int, match: str
) -> None:
    with pytest.raises(SlmpConfigurationError, match=match):
        Entry(name=name, host=BENCH_HOST, port=port)


def test_entry_of_describes_a_live_client() -> None:
    client = a_client(5001, transport=TransportKind.UDP, frame=FrameType.FOUR_E)
    entry = Entry.of("udp", client)
    assert entry == Entry(
        name="udp",
        host=BENCH_HOST,
        port=5001,
        transport=TransportKind.UDP,
        encoding=Encoding.BINARY,
        frame=FrameType.FOUR_E,
    )


# ========================================================================================
# The group, with no socket
# ========================================================================================


def test_an_empty_group_is_refused() -> None:
    with pytest.raises(SlmpConfigurationError, match="nothing to name"):
        EntryGroup({})


def test_a_missing_name_names_the_entries_that_do_exist() -> None:
    group = EntryGroup({"fast": a_client(5001), "safe": a_client(5002)})
    with pytest.raises(SlmpConfigurationError) as caught:
        group["fasst"]
    message = str(caught.value)
    assert "'fast'" in message
    assert "'safe'" in message
    assert "never picks an entry for you" in message


def test_the_group_preserves_configuration_order() -> None:
    group = EntryGroup(
        {"third": a_client(5004), "first": a_client(5002), "second": a_client(5003)}
    )
    assert group.names == ("third", "first", "second")
    assert list(group) == ["third", "first", "second"]
    assert len(group) == 3
    assert "first" in group
    assert "fourth" not in group


def test_the_group_snapshots_the_mapping_it_was_given() -> None:
    """A caller mutating their dict afterwards must not re-point a live handle."""
    source = {"a": a_client(5002)}
    group = EntryGroup(source)
    source["a"] = a_client(5003)
    assert group["a"].peer == (BENCH_HOST, 5002)


def test_entries_describes_every_handle_with_its_own_transport() -> None:
    group = EntryGroup(
        {
            "safe": a_client(5002),
            "fast": a_client(5001, transport=TransportKind.UDP),
        }
    )
    described = group.entries()
    assert described["safe"].transport is TransportKind.TCP
    assert described["fast"].transport is TransportKind.UDP
    assert described["fast"].port == 5001


def test_metrics_are_per_entry_and_never_merged() -> None:
    group = EntryGroup({"a": a_client(5002), "b": a_client(5003)})
    metrics = group.metrics()
    assert set(metrics) == {"a", "b"}
    assert metrics["a"].latency is None  # no samples is None, never a zeroed p99
    assert metrics["a"].connection_id != metrics["b"].connection_id
    assert group.states() == {"a": ConnectionState.NEW, "b": ConnectionState.NEW}


def test_the_repr_says_where_every_entry_is() -> None:
    group = EntryGroup({"a": a_client(5002)})
    assert repr(group) == "EntryGroup(a=new)"


# ========================================================================================
# The group, against the simulator
# ========================================================================================


async def test_connect_all_opens_every_entry_and_keeps_the_attribution() -> None:
    """Five entries, four transports/codings, one CPU. Nothing is merged or chosen."""
    async with PlcSimulator() as simulator:
        group = EntryGroup(
            {entry.name: client_for(simulator, entry.name) for entry in BENCH_ENTRIES}
        )
        opened = await group.connect_all()
        try:
            assert set(opened) == {entry.name for entry in BENCH_ENTRIES}
            assert all(info.identity is not None for info in opened.values())
            described = group.entries()
            assert described["udp"].transport is TransportKind.UDP
            assert described["tcp"].transport is TransportKind.TCP
            assert described["tcp-4e"].frame is FrameType.FOUR_E
            assert described["tcp-ascii"].encoding is Encoding.ASCII_XY_HEX
            assert set(group.states().values()) == {ConnectionState.READY}
            # Attribution survives: each handle read through its own socket.
            assert await group["tcp"].read_f32("D0") == pytest.approx(0.0)
            assert await group["udp"].read_f32("D0") == pytest.approx(0.0)
        finally:
            await group.aclose()
        assert set(group.states().values()) == {ConnectionState.CLOSED}


async def test_a_failed_entry_closes_the_ones_already_open_and_re_raises() -> None:
    """A group that reported success with two of its entries shut would be a half-truth."""
    async with PlcSimulator() as simulator:
        dead = PlcSimulator()
        await dead.start()
        dead_host, dead_port = dead.address("tcp")
        await dead.aclose()

        good = client_for(simulator, "tcp")
        bad = Plc(dead_host, dead_port, profile=FX5U, timeout=1.0, connect_timeout=1.0)
        group = EntryGroup({"good": good, "bad": bad})
        with pytest.raises(SlmpTransportError):
            await group.connect_all()
        assert good.state is ConnectionState.CLOSED
        assert good.counters.disconnects == 1


async def test_the_group_is_an_async_context_manager() -> None:
    async with PlcSimulator() as simulator:
        group = EntryGroup({"tcp": client_for(simulator, "tcp")})
        async with group:
            assert group.states() == {"tcp": ConnectionState.READY}
        assert group.states() == {"tcp": ConnectionState.CLOSED}


async def test_aclose_is_idempotent() -> None:
    async with PlcSimulator() as simulator:
        group = EntryGroup({"tcp": client_for(simulator, "tcp")})
        await group.connect_all()
        await group.aclose()
        await group.aclose()
        assert group["tcp"].state is ConnectionState.CLOSED
