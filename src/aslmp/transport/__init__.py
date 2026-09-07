"""Layer 3 -- the only place in this library where a socket is legal.

And the only place forbidden to know what a frame is: the edge
``aslmp.transport -> aslmp.wire`` is banned outright by ``tests/unit/test_layering.py``,
not by layer arithmetic, because ``wire`` sits *below* ``transport`` and the layer rule
alone would permit it. What crosses the boundary instead is structure --
:class:`~aslmp.transport.base.Reassembler`,
:class:`~aslmp.transport.base.Correlation` and
:class:`~aslmp.transport.base.TransportObserver`. The dependency runs upward; the
knowledge runs downward.

Nothing here is a public API. :class:`aslmp.connection.Connection` is what binds a
transport to a frame format, and the only way to put bytes on a socket anywhere in this
package is the single-use capability token that connection hands out.
"""

from __future__ import annotations

from aslmp.transport.base import (
    DEFAULT_BUFFER_CAPACITY,
    DEFAULT_UDP_PIPELINE_DEPTH,
    MAX_UDP_PIPELINE_DEPTH,
    NULL_OBSERVER,
    Binding,
    Correlation,
    Deadline,
    Reassembler,
    RecvBuffer,
    Transport,
    TransportKind,
    TransportObserver,
    WireResult,
    accept_any,
    timeout_error,
)
from aslmp.transport.inflight import Concurrency, GateSlot, Holder, TransactionGate
from aslmp.transport.tcp import TcpTransport
from aslmp.transport.udp import FOREIGN_SOURCE, STALE_EPOCH, UNMATCHED, UdpTransport

__all__ = [
    "DEFAULT_BUFFER_CAPACITY",
    "DEFAULT_UDP_PIPELINE_DEPTH",
    "FOREIGN_SOURCE",
    "MAX_UDP_PIPELINE_DEPTH",
    "NULL_OBSERVER",
    "STALE_EPOCH",
    "UNMATCHED",
    "Binding",
    "Concurrency",
    "Correlation",
    "Deadline",
    "GateSlot",
    "Holder",
    "Reassembler",
    "RecvBuffer",
    "TcpTransport",
    "TransactionGate",
    "Transport",
    "TransportKind",
    "TransportObserver",
    "UdpTransport",
    "WireResult",
    "accept_any",
    "timeout_error",
]
