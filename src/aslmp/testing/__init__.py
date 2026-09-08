"""``aslmp.testing`` -- a conformance simulator that reproduces real PLC misbehaviour.

Install with the extra: ``pip install aslmp[testing]``. It has no dependencies of its
own; the extra exists so that the client wheel stays import-light and dependency-free.

**Layering.** This package imports layers 0 to 2 only -- ``aslmp.wire``,
``aslmp.errors``, ``aslmp.profile`` and ``aslmp.commands`` -- and
``tests/unit/test_layering.py`` fails the build if that changes. It must never import
``aslmp.transport``, ``aslmp.connection`` or ``aslmp.client``: if the simulator were
built on the client's transport, a bug in the transport, the in-flight gate, the
reconnection logic or the client's control flow would be invisible to every
client-against-server test in the suite, because both sides would share it.

**Why this exists.** A survey of every open SLMP server found three, and all three
answer only the happy path -- one of them rejects every correct 3E client because it
compares the subheader constant ``0x0054`` against a little-endian unpack of ``50 00``.
Nothing usable exists in any language. Meanwhile the failures that actually cost time on
this protocol are not happy-path failures:

* two requests written before the first response is read produce **one** response, for
  the **last** request, end code ``0x0000`` -- undetectable on 3E;
* a second connection to a busy entry completes its TCP handshake and is then closed;
* the wrong coding, the wrong frame type and an overstated data length all fail by
  **silence**;
* 64 pipelined UDP requests return 44, with no end code, no ICMP and no error anywhere;
* the CPU accepts a ``TS`` point its own manual forbids, and answers ``0x0000``.

Every one of those is a switch on :class:`~aslmp.testing.pathology.Pathology`, each
citing the measurement it reproduces, each independently settable.

::

    from aslmp.testing import PlcSimulator, FX5U_32MT_DS

    async with PlcSimulator(target=FX5U_32MT_DS) as plc:
        plc.memory.set_f32("D", 0, 60.0)
        host, port = plc.address("tcp")
        ...

``aslmp.testing.pytest_plugin`` is deliberately **not** re-exported here: it imports
pytest, and this package must stay usable from a plain script.
"""

from __future__ import annotations

from aslmp.testing.conformance import (
    Abnormal,
    CaseResult,
    ConformanceCase,
    ConformanceReport,
    Exchange,
    NoResponse,
    Normal,
    TcpExchange,
    UdpExchange,
    context_for,
    run_conformance,
    standard_cases,
)
from aslmp.testing.dispatch import (
    CPU_STATUS_DEVICE,
    CPU_STATUS_INDEX,
    HANDLERS,
    PASSWORD_EXEMPT_COMMANDS,
    CpuRunState,
    Dispatcher,
    Reply,
    ServerContext,
    SessionState,
    Silence,
    effective_cpu_state,
)
from aslmp.testing.memory import (
    BENCH_SCAN_STEP,
    BENCH_SCAN_WRAP,
    AbsentDeviceError,
    DeviceMemory,
    MemoryRange,
    MemorySnapshot,
    OutOfRangeError,
    ranges_from_profile,
)
from aslmp.testing.pathology import FX5U_MEASURED, HEALTHY, PATHOLOGY_SOURCES, Pathology
from aslmp.testing.scenario import Scenario, Step, abnormal, silent
from aslmp.testing.server import (
    BENCH_ENTRIES,
    Entry,
    PlcSimulator,
    ServerEvent,
    TranscriptRecord,
    codec_for,
)
from aslmp.testing.targets import (
    ALL_TARGETS,
    FX5U_32MT_DS,
    PEDANTIC,
    R04CPU,
    EndCodePolicy,
    LimitPolicy,
    SimulatorTarget,
    TargetDiff,
    by_key,
    diff_targets,
)
from aslmp.testing.vectors import Corpus, Vector, load_corpus, load_vectors

__all__ = [
    "ALL_TARGETS",
    "BENCH_ENTRIES",
    "BENCH_SCAN_STEP",
    "BENCH_SCAN_WRAP",
    "CPU_STATUS_DEVICE",
    "CPU_STATUS_INDEX",
    "FX5U_32MT_DS",
    "FX5U_MEASURED",
    "HANDLERS",
    "HEALTHY",
    "PASSWORD_EXEMPT_COMMANDS",
    "PATHOLOGY_SOURCES",
    "PEDANTIC",
    "R04CPU",
    "Abnormal",
    "AbsentDeviceError",
    "CaseResult",
    "ConformanceCase",
    "ConformanceReport",
    "Corpus",
    "CpuRunState",
    "DeviceMemory",
    "Dispatcher",
    "EndCodePolicy",
    "Entry",
    "Exchange",
    "LimitPolicy",
    "MemoryRange",
    "MemorySnapshot",
    "NoResponse",
    "Normal",
    "OutOfRangeError",
    "Pathology",
    "PlcSimulator",
    "Reply",
    "Scenario",
    "ServerContext",
    "ServerEvent",
    "SessionState",
    "Silence",
    "SimulatorTarget",
    "Step",
    "TargetDiff",
    "TcpExchange",
    "TranscriptRecord",
    "UdpExchange",
    "Vector",
    "abnormal",
    "by_key",
    "codec_for",
    "context_for",
    "diff_targets",
    "effective_cpu_state",
    "load_corpus",
    "load_vectors",
    "ranges_from_profile",
    "run_conformance",
    "silent",
    "standard_cases",
]
