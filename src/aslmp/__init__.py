"""An async SLMP client for Mitsubishi MELSEC PLCs.

``aslmp`` speaks SLMP (Seamless Message Protocol, the successor to MC protocol) over
TCP and UDP, in binary and ASCII, in 3E and 4E frames, against MELSEC iQ-F, iQ-R, Q and
L CPUs. What it knows about real silicon it learned from an FX5U-32MT/DS on firmware
1.065; see ``README.md`` for exactly which claims are measured and which are read out of
a manual.

**This module is a name table and nothing else.** Every public name of DESIGN section 2
resolves here, and none of them is imported until it is asked for.

**Why a module-level ``__getattr__`` (PEP 562).** ``aslmp.wire``, ``aslmp.errors``,
``aslmp.profile``, ``aslmp.commands`` and ``aslmp.blocks.layout`` are pure bytes: DESIGN
section 5.1.2 makes it a Tier 0 test that importing any of them leaves ``socket``,
``ssl``, ``asyncio``, ``selectors``, ``threading`` and ``logging`` out of
``sys.modules``. Importing a submodule executes this file first, so a plain
``from aslmp.client import Plc`` at the top of this file would drag ``socket`` into
every one of them at once and fail the tier whose failure means nothing else matters.
A lazy table is not a style preference here; it is the only way to publish a layer-5
name from the top of a package whose layer-0 modules must stay socket-free.

The cost is real and worth naming: ``aslmp.Plc`` is resolved by a dictionary lookup and
an ``importlib`` call on first use, tab-completion in a REPL works through
:func:`__dir__`, and a typo raises ``AttributeError`` from here rather than at import.
Static analysers read the ``if TYPE_CHECKING`` block, so ``mypy``, ``pyright`` and an
IDE see ordinary imports.

``__all__`` is the stability contract of DESIGN section 2: two-minor-version deprecation
notice, no removals in a minor. Anything not named here is private, including every
module whose name starts with an underscore.

The one thing that is public and *not* in ``__all__`` is a submodule. ``aslmp.sync``,
``aslmp.testing`` and their siblings resolve as attributes of this package through
:data:`_SUBMODULES` -- the README documents ``aslmp.sync.Plc`` and a bare
``__getattr__`` over ``_EXPORTS`` made that an ``AttributeError`` -- but they are
reached by import rather than by the table, so they do not join the name contract.
:data:`_SUBMODULES` says why at length.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any, Final

from aslmp._version import __version__

if TYPE_CHECKING:
    # Imported for type checkers only. At runtime these arrive through __getattr__,
    # so each is written ``X as X``: an explicit re-export for mypy and pyright, and
    # the form that says the name is published rather than used here.
    from aslmp.blocks import (
        F32 as F32,
    )
    from aslmp.blocks import (
        F64 as F64,
    )
    from aslmp.blocks import (
        I16 as I16,
    )
    from aslmp.blocks import (
        I32 as I32,
    )
    from aslmp.blocks import (
        U16 as U16,
    )
    from aslmp.blocks import (
        U32 as U32,
    )
    from aslmp.blocks import (
        Bit as Bit,
    )
    from aslmp.blocks import (
        BitFold as BitFold,
    )
    from aslmp.blocks import (
        BlockLayout as BlockLayout,
    )
    from aslmp.blocks import (
        BlockPlan as BlockPlan,
    )
    from aslmp.blocks import (
        BlockTiming as BlockTiming,
    )
    from aslmp.blocks import (
        BlockTransaction as BlockTransaction,
    )
    from aslmp.blocks import (
        Bounds as Bounds,
    )
    from aslmp.blocks import (
        FieldPlan as FieldPlan,
    )
    from aslmp.blocks import (
        PlcBlock as PlcBlock,
    )
    from aslmp.blocks import (
        SlmpImplausibleValueError as SlmpImplausibleValueError,
    )
    from aslmp.blocks import (
        Split as Split,
    )
    from aslmp.blocks import (
        SplitBlockPlan as SplitBlockPlan,
    )
    from aslmp.blocks import (
        Str as Str,
    )
    from aslmp.blocks import (
        Word as Word,
    )
    from aslmp.blocks import (
        at as at,
    )
    from aslmp.blocks import (
        bind as bind,
    )
    from aslmp.blocks import (
        plc_block as plc_block,
    )
    from aslmp.client import (
        Handshake as Handshake,
    )
    from aslmp.client import (
        MonitoringTimer as MonitoringTimer,
    )
    from aslmp.client import (
        Plc as Plc,
    )
    from aslmp.client import (
        PlcClockSource as PlcClockSource,
    )
    from aslmp.client import (
        RemoteControl as RemoteControl,
    )
    from aslmp.commands import (
        AccessWidth as AccessWidth,
    )
    from aslmp.commands import (
        BlockSpec as BlockSpec,
    )
    from aslmp.commands import (
        BlockWrite as BlockWrite,
    )
    from aslmp.commands import (
        MonitorRegistration as MonitorRegistration,
    )
    from aslmp.commands import (
        RandomPoint as RandomPoint,
    )
    from aslmp.commands import (
        RandomWrite as RandomWrite,
    )
    from aslmp.commands import (
        RunMode as RunMode,
    )
    from aslmp.commands import (
        WordOrder as WordOrder,
    )
    from aslmp.commands import (
        bit_point as bit_point,
    )
    from aslmp.commands import (
        dword as dword,
    )
    from aslmp.commands import (
        word as word,
    )
    from aslmp.commands.random import (
        RandomValue as RandomValue,
    )
    from aslmp.connection import (
        ConnectionInfo as ConnectionInfo,
    )
    from aslmp.connection import (
        ConnectionState as ConnectionState,
    )
    from aslmp.entries import (
        Entry as Entry,
    )
    from aslmp.entries import (
        EntryGroup as EntryGroup,
    )
    from aslmp.errors import (
        OutcomeUnknownReason as OutcomeUnknownReason,
    )
    from aslmp.errors import (
        SlmpAddressRangeError as SlmpAddressRangeError,
    )
    from aslmp.errors import (
        SlmpAddressSyntaxError as SlmpAddressSyntaxError,
    )
    from aslmp.errors import (
        SlmpAsciiConversionError as SlmpAsciiConversionError,
    )
    from aslmp.errors import (
        SlmpBitPointCountError as SlmpBitPointCountError,
    )
    from aslmp.errors import (
        SlmpBlockLayoutError as SlmpBlockLayoutError,
    )
    from aslmp.errors import (
        SlmpBusyError as SlmpBusyError,
    )
    from aslmp.errors import (
        SlmpCapabilityError as SlmpCapabilityError,
    )
    from aslmp.errors import (
        SlmpConcurrentTransactionError as SlmpConcurrentTransactionError,
    )
    from aslmp.errors import (
        SlmpConfigurationError as SlmpConfigurationError,
    )
    from aslmp.errors import (
        SlmpConnectionClosedError as SlmpConnectionClosedError,
    )
    from aslmp.errors import (
        SlmpConnectionEntryBusyError as SlmpConnectionEntryBusyError,
    )
    from aslmp.errors import (
        SlmpConnectionLostError as SlmpConnectionLostError,
    )
    from aslmp.errors import (
        SlmpConnectionStateError as SlmpConnectionStateError,
    )
    from aslmp.errors import (
        SlmpCpuDataTooLargeError as SlmpCpuDataTooLargeError,
    )
    from aslmp.errors import (
        SlmpCpuDeviceSpecError as SlmpCpuDeviceSpecError,
    )
    from aslmp.errors import (
        SlmpCpuError as SlmpCpuError,
    )
    from aslmp.errors import (
        SlmpCpuFileError as SlmpCpuFileError,
    )
    from aslmp.errors import (
        SlmpCpuModuleError as SlmpCpuModuleError,
    )
    from aslmp.errors import (
        SlmpCpuRemoteDisabledError as SlmpCpuRemoteDisabledError,
    )
    from aslmp.errors import (
        SlmpCpuRunningError as SlmpCpuRunningError,
    )
    from aslmp.errors import (
        SlmpCpuUnsupportedRequestError as SlmpCpuUnsupportedRequestError,
    )
    from aslmp.errors import (
        SlmpDatagramLostError as SlmpDatagramLostError,
    )
    from aslmp.errors import (
        SlmpDatagramSourceError as SlmpDatagramSourceError,
    )
    from aslmp.errors import (
        SlmpDataLengthError as SlmpDataLengthError,
    )
    from aslmp.errors import (
        SlmpDeviceNotAccessibleError as SlmpDeviceNotAccessibleError,
    )
    from aslmp.errors import (
        SlmpDeviceNotAllowedHereError as SlmpDeviceNotAllowedHereError,
    )
    from aslmp.errors import (
        SlmpDeviceNotOnCpuError as SlmpDeviceNotOnCpuError,
    )
    from aslmp.errors import (
        SlmpDeviceRadixError as SlmpDeviceRadixError,
    )
    from aslmp.errors import (
        SlmpDeviceRangeError as SlmpDeviceRangeError,
    )
    from aslmp.errors import (
        SlmpEncodingNotSupportedError as SlmpEncodingNotSupportedError,
    )
    from aslmp.errors import (
        SlmpEndCodeError as SlmpEndCodeError,
    )
    from aslmp.errors import (
        SlmpError as SlmpError,
    )
    from aslmp.errors import (
        SlmpFrameFormatError as SlmpFrameFormatError,
    )
    from aslmp.errors import (
        SlmpHandshakeError as SlmpHandshakeError,
    )
    from aslmp.errors import (
        SlmpMonitoringTimerError as SlmpMonitoringTimerError,
    )
    from aslmp.errors import (
        SlmpMonitorNotRegisteredError as SlmpMonitorNotRegisteredError,
    )
    from aslmp.errors import (
        SlmpNotConnectedError as SlmpNotConnectedError,
    )
    from aslmp.errors import (
        SlmpNotSentError as SlmpNotSentError,
    )
    from aslmp.errors import (
        SlmpOutcomeUnknownError as SlmpOutcomeUnknownError,
    )
    from aslmp.errors import (
        SlmpPayloadShapeError as SlmpPayloadShapeError,
    )
    from aslmp.errors import (
        SlmpPlcFramingError as SlmpPlcFramingError,
    )
    from aslmp.errors import (
        SlmpPlcTimeoutError as SlmpPlcTimeoutError,
    )
    from aslmp.errors import (
        SlmpPointCountError as SlmpPointCountError,
    )
    from aslmp.errors import (
        SlmpPointLimitError as SlmpPointLimitError,
    )
    from aslmp.errors import (
        SlmpProfileMismatchError as SlmpProfileMismatchError,
    )
    from aslmp.errors import (
        SlmpProtocolError as SlmpProtocolError,
    )
    from aslmp.errors import (
        SlmpRandomPointCountError as SlmpRandomPointCountError,
    )
    from aslmp.errors import (
        SlmpRemotePasswordError as SlmpRemotePasswordError,
    )
    from aslmp.errors import (
        SlmpRemotePasswordLockedError as SlmpRemotePasswordLockedError,
    )
    from aslmp.errors import (
        SlmpRemotePasswordLockoutError as SlmpRemotePasswordLockoutError,
    )
    from aslmp.errors import (
        SlmpRemoteStateNotReachedError as SlmpRemoteStateNotReachedError,
    )
    from aslmp.errors import (
        SlmpRequestContentError as SlmpRequestContentError,
    )
    from aslmp.errors import (
        SlmpRouteMismatchError as SlmpRouteMismatchError,
    )
    from aslmp.errors import (
        SlmpRoutingError as SlmpRoutingError,
    )
    from aslmp.errors import (
        SlmpSemanticError as SlmpSemanticError,
    )
    from aslmp.errors import (
        SlmpSerialMismatchError as SlmpSerialMismatchError,
    )
    from aslmp.errors import (
        SlmpShortDatagramError as SlmpShortDatagramError,
    )
    from aslmp.errors import (
        SlmpSinkError as SlmpSinkError,
    )
    from aslmp.errors import (
        SlmpTargetChangedError as SlmpTargetChangedError,
    )
    from aslmp.errors import (
        SlmpTimeoutError as SlmpTimeoutError,
    )
    from aslmp.errors import (
        SlmpTrailingDataError as SlmpTrailingDataError,
    )
    from aslmp.errors import (
        SlmpTransportError as SlmpTransportError,
    )
    from aslmp.errors import (
        SlmpUnknownDeviceError as SlmpUnknownDeviceError,
    )
    from aslmp.errors import (
        SlmpUnsolicitedFrameError as SlmpUnsolicitedFrameError,
    )
    from aslmp.errors import (
        SlmpUnsupportedCommandError as SlmpUnsupportedCommandError,
    )
    from aslmp.errors import (
        SlmpUsageError as SlmpUsageError,
    )
    from aslmp.errors import (
        SlmpValueRangeError as SlmpValueRangeError,
    )
    from aslmp.errors import (
        SlmpVerificationError as SlmpVerificationError,
    )
    from aslmp.errors import (
        SlmpWordPointCountError as SlmpWordPointCountError,
    )
    from aslmp.errors import (
        SlmpWriteProtectedError as SlmpWriteProtectedError,
    )
    from aslmp.errors import (
        TimeoutCause as TimeoutCause,
    )
    from aslmp.health import (
        HealthMonitor as HealthMonitor,
    )
    from aslmp.health import (
        HealthSnapshot as HealthSnapshot,
    )
    from aslmp.health import (
        ProbeOutcome as ProbeOutcome,
    )
    from aslmp.identity import (
        CpuIdentity as CpuIdentity,
    )
    from aslmp.identity import (
        CpuStatus as CpuStatus,
    )
    from aslmp.loop import (
        Cadence as Cadence,
    )
    from aslmp.loop import (
        OverrunPolicy as OverrunPolicy,
    )
    from aslmp.loop import (
        SlmpCadenceOverrunError as SlmpCadenceOverrunError,
    )
    from aslmp.loop import (
        Tick as Tick,
    )
    from aslmp.observability import (
        Connected as Connected,
    )
    from aslmp.observability import (
        ConnectFailed as ConnectFailed,
    )
    from aslmp.observability import (
        Connecting as Connecting,
    )
    from aslmp.observability import (
        ConnectionEvent as ConnectionEvent,
    )
    from aslmp.observability import (
        ConnectionFailed as ConnectionFailed,
    )
    from aslmp.observability import (
        Counters as Counters,
    )
    from aslmp.observability import (
        DatagramDropped as DatagramDropped,
    )
    from aslmp.observability import (
        Disconnected as Disconnected,
    )
    from aslmp.observability import (
        EventSink as EventSink,
    )
    from aslmp.observability import (
        HandshakeFailed as HandshakeFailed,
    )
    from aslmp.observability import (
        LatencyRecorder as LatencyRecorder,
    )
    from aslmp.observability import (
        MetricsSnapshot as MetricsSnapshot,
    )
    from aslmp.observability import (
        Percentiles as Percentiles,
    )
    from aslmp.observability import (
        ProbeSkipped as ProbeSkipped,
    )
    from aslmp.observability import (
        Reconnected as Reconnected,
    )
    from aslmp.observability import (
        Reconnecting as Reconnecting,
    )
    from aslmp.observability import (
        SinkFailed as SinkFailed,
    )
    from aslmp.observability import (
        SocketRebound as SocketRebound,
    )
    from aslmp.observability import (
        TargetChanged as TargetChanged,
    )
    from aslmp.observability import (
        attach_logging as attach_logging,
    )
    from aslmp.observability import (
        fanout as fanout,
    )
    from aslmp.profile import (
        BlockRule as BlockRule,
    )
    from aslmp.profile import (
        Capability as Capability,
    )
    from aslmp.profile import (
        ClearMode as ClearMode,
    )
    from aslmp.profile import (
        CpuProfile as CpuProfile,
    )
    from aslmp.profile import (
        DeviceRange as DeviceRange,
    )
    from aslmp.profile import (
        Encoding as Encoding,
    )
    from aslmp.profile import (
        Evidence as Evidence,
    )
    from aslmp.profile import (
        Family as Family,
    )
    from aslmp.profile import (
        Flat as Flat,
    )
    from aslmp.profile import (
        Limit as Limit,
    )
    from aslmp.profile import (
        LimitKey as LimitKey,
    )
    from aslmp.profile import (
        LimitRule as LimitRule,
    )
    from aslmp.profile import (
        Link as Link,
    )
    from aslmp.profile import (
        Refusal as Refusal,
    )
    from aslmp.profile import (
        Weighted as Weighted,
    )
    from aslmp.resilience import (
        ExponentialBackoff as ExponentialBackoff,
    )
    from aslmp.resilience import (
        ReconnectPolicy as ReconnectPolicy,
    )
    from aslmp.resilience import (
        Supervisor as Supervisor,
    )
    from aslmp.results import (
        BlockReading as BlockReading,
    )
    from aslmp.results import (
        RandomReading as RandomReading,
    )
    from aslmp.results import (
        Reading as Reading,
    )
    from aslmp.results import (
        RemoteResult as RemoteResult,
    )
    from aslmp.results import (
        ResetOutcome as ResetOutcome,
    )
    from aslmp.results import (
        SplitReading as SplitReading,
    )
    from aslmp.results import (
        WriteAck as WriteAck,
    )
    from aslmp.timed import (
        TimedApi as TimedApi,
    )
    from aslmp.timing import (
        Chunk as Chunk,
    )
    from aslmp.timing import (
        Clock as Clock,
    )
    from aslmp.timing import (
        Nanos as Nanos,
    )
    from aslmp.timing import (
        Phase as Phase,
    )
    from aslmp.timing import (
        Transaction as Transaction,
    )
    from aslmp.timing import (
        TransactionSink as TransactionSink,
    )
    from aslmp.timing import (
        TransactionTiming as TransactionTiming,
    )
    from aslmp.transport import (
        Concurrency as Concurrency,
    )
    from aslmp.transport import (
        TransportKind as TransportKind,
    )
    from aslmp.wire.address import (
        DeviceAddress as DeviceAddress,
    )
    from aslmp.wire.address import (
        format_address as format_address,
    )
    from aslmp.wire.address import (
        parse_address as parse_address,
    )
    from aslmp.wire.citations import (
        Ambiguity as Ambiguity,
    )
    from aslmp.wire.citations import (
        Citation as Citation,
    )
    from aslmp.wire.citations import (
        Measurement as Measurement,
    )
    from aslmp.wire.citations import (
        Provenance as Provenance,
    )
    from aslmp.wire.citations import (
        Source as Source,
    )
    from aslmp.wire.codec import (
        Notation as Notation,
    )
    from aslmp.wire.codec import (
        SpecFormat as SpecFormat,
    )
    from aslmp.wire.codec import (
        Unit as Unit,
    )
    from aslmp.wire.devicetable import (
        DeviceType as DeviceType,
    )
    from aslmp.wire.devicetable import (
        Radix as Radix,
    )
    from aslmp.wire.frames import (
        FrameType as FrameType,
    )
    from aslmp.wire.raw import (
        ErrorInfo as ErrorInfo,
    )
    from aslmp.wire.raw import (
        RawResponse as RawResponse,
    )
    from aslmp.wire.route import (
        Route as Route,
    )


# ----------------------------------------------------------------------------------------
# The table
# ----------------------------------------------------------------------------------------
#
# One row per public name. The module string is the only place the import lives, so
# moving a class between modules is a one-line change here and no change for a caller.
# ``tests/unit/test_tools.py`` asserts every row resolves and that __all__ and this
# table name exactly the same set.

_EXPORTS: Final[dict[str, str]] = {
    # -- the client and its lifecycle ------------------------------------------------
    "Plc": "aslmp.client",
    "Handshake": "aslmp.client",
    "MonitoringTimer": "aslmp.client",
    "PlcClockSource": "aslmp.client",
    "RemoteControl": "aslmp.client",
    "TimedApi": "aslmp.timed",
    "ConnectionInfo": "aslmp.connection",
    "ConnectionState": "aslmp.connection",
    # -- results ---------------------------------------------------------------------
    "Reading": "aslmp.results",
    "WriteAck": "aslmp.results",
    "RandomReading": "aslmp.results",
    "RandomValue": "aslmp.commands.random",
    "SplitReading": "aslmp.results",
    "BlockReading": "aslmp.results",
    "ResetOutcome": "aslmp.results",
    "RemoteResult": "aslmp.results",
    # -- timing and observability ----------------------------------------------------
    "Nanos": "aslmp.timing",
    "Chunk": "aslmp.timing",
    "Clock": "aslmp.timing",
    "Phase": "aslmp.timing",
    "Transaction": "aslmp.timing",
    "TransactionSink": "aslmp.timing",
    "TransactionTiming": "aslmp.timing",
    "ConnectionEvent": "aslmp.observability",
    "Connecting": "aslmp.observability",
    "Connected": "aslmp.observability",
    "ConnectFailed": "aslmp.observability",
    "ConnectionFailed": "aslmp.observability",
    "Disconnected": "aslmp.observability",
    "Reconnecting": "aslmp.observability",
    "Reconnected": "aslmp.observability",
    "HandshakeFailed": "aslmp.observability",
    "DatagramDropped": "aslmp.observability",
    "ProbeSkipped": "aslmp.observability",
    "SinkFailed": "aslmp.observability",
    "SocketRebound": "aslmp.observability",
    "TargetChanged": "aslmp.observability",
    "EventSink": "aslmp.observability",
    "Counters": "aslmp.observability",
    "MetricsSnapshot": "aslmp.observability",
    "LatencyRecorder": "aslmp.observability",
    "Percentiles": "aslmp.observability",
    "fanout": "aslmp.observability",
    "attach_logging": "aslmp.observability",
    # -- profiles and what a CPU can do ----------------------------------------------
    "CpuProfile": "aslmp.profile",
    "Capability": "aslmp.profile",
    "ClearMode": "aslmp.profile",
    "DeviceRange": "aslmp.profile",
    "Encoding": "aslmp.profile",
    "Evidence": "aslmp.profile",
    "Family": "aslmp.profile",
    "Limit": "aslmp.profile",
    "LimitKey": "aslmp.profile",
    "LimitRule": "aslmp.profile",
    "Flat": "aslmp.profile",
    "Weighted": "aslmp.profile",
    "BlockRule": "aslmp.profile",
    "Link": "aslmp.profile",
    "Refusal": "aslmp.profile",
    "CpuIdentity": "aslmp.identity",
    "CpuStatus": "aslmp.identity",
    # -- addresses, frames and provenance --------------------------------------------
    "DeviceAddress": "aslmp.wire.address",
    "parse_address": "aslmp.wire.address",
    "format_address": "aslmp.wire.address",
    "DeviceType": "aslmp.wire.devicetable",
    "Radix": "aslmp.wire.devicetable",
    "SpecFormat": "aslmp.wire.codec",
    "Unit": "aslmp.wire.codec",
    "Notation": "aslmp.wire.codec",
    "FrameType": "aslmp.wire.frames",
    "Route": "aslmp.wire.route",
    "RawResponse": "aslmp.wire.raw",
    "ErrorInfo": "aslmp.wire.raw",
    "Citation": "aslmp.wire.citations",
    "Measurement": "aslmp.wire.citations",
    "Ambiguity": "aslmp.wire.citations",
    "Provenance": "aslmp.wire.citations",
    "Source": "aslmp.wire.citations",
    # -- transport knobs --------------------------------------------------------------
    "TransportKind": "aslmp.transport",
    "Concurrency": "aslmp.transport",
    # -- commands and the random-access point vocabulary -------------------------------
    "RandomPoint": "aslmp.commands",
    "RandomWrite": "aslmp.commands",
    "BlockSpec": "aslmp.commands",
    "BlockWrite": "aslmp.commands",
    "MonitorRegistration": "aslmp.commands",
    "AccessWidth": "aslmp.commands",
    "WordOrder": "aslmp.commands",
    "RunMode": "aslmp.commands",
    "word": "aslmp.commands",
    "dword": "aslmp.commands",
    "bit_point": "aslmp.commands",
    # -- blocks -------------------------------------------------------------------------
    "F32": "aslmp.blocks",
    "F64": "aslmp.blocks",
    "I32": "aslmp.blocks",
    "U32": "aslmp.blocks",
    "I16": "aslmp.blocks",
    "U16": "aslmp.blocks",
    "Word": "aslmp.blocks",
    "Bit": "aslmp.blocks",
    "Str": "aslmp.blocks",
    "at": "aslmp.blocks",
    "plc_block": "aslmp.blocks",
    "bind": "aslmp.blocks",
    "BlockLayout": "aslmp.blocks",
    "BlockPlan": "aslmp.blocks",
    "SplitBlockPlan": "aslmp.blocks",
    "Split": "aslmp.blocks",
    "BitFold": "aslmp.blocks",
    "FieldPlan": "aslmp.blocks",
    "PlcBlock": "aslmp.blocks",
    "BlockTransaction": "aslmp.blocks",
    "BlockTiming": "aslmp.blocks",
    # Plausibility bounds. ``SlmpImplausibleValueError`` is raised by a bounded read and
    # documented in the README, so it has to be catchable without a second, deeper
    # import; ``Bounds`` is the type of its ``.bounds`` attribute, which a
    # ``mypy --strict`` handler cannot annotate otherwise. Both still live in
    # ``aslmp.blocks.fields`` -- the error belongs in the DESIGN section 3.1 tree in
    # ``aslmp/errors/__init__.py`` and should move there when that module is next
    # opened, which is a one-line change to the row below and none for a caller.
    "SlmpImplausibleValueError": "aslmp.blocks",
    "Bounds": "aslmp.blocks",
    # -- supervision, entries, cadence, health -------------------------------------------
    "HealthMonitor": "aslmp.health",
    "HealthSnapshot": "aslmp.health",
    "ProbeOutcome": "aslmp.health",
    "ReconnectPolicy": "aslmp.resilience",
    "ExponentialBackoff": "aslmp.resilience",
    "Supervisor": "aslmp.resilience",
    "Entry": "aslmp.entries",
    "EntryGroup": "aslmp.entries",
    "Cadence": "aslmp.loop",
    "Tick": "aslmp.loop",
    "OverrunPolicy": "aslmp.loop",
    "SlmpCadenceOverrunError": "aslmp.loop",
    # -- the exception hierarchy of DESIGN section 3 --------------------------------------
    "SlmpError": "aslmp.errors",
    "SlmpUsageError": "aslmp.errors",
    "SlmpAddressSyntaxError": "aslmp.errors",
    "SlmpUnknownDeviceError": "aslmp.errors",
    "SlmpDeviceRadixError": "aslmp.errors",
    "SlmpDeviceNotOnCpuError": "aslmp.errors",
    "SlmpAddressRangeError": "aslmp.errors",
    "SlmpPointLimitError": "aslmp.errors",
    "SlmpDeviceNotAllowedHereError": "aslmp.errors",
    "SlmpCapabilityError": "aslmp.errors",
    "SlmpEncodingNotSupportedError": "aslmp.errors",
    "SlmpMonitoringTimerError": "aslmp.errors",
    "SlmpBlockLayoutError": "aslmp.errors",
    "SlmpValueRangeError": "aslmp.errors",
    "SlmpConfigurationError": "aslmp.errors",
    "SlmpConcurrentTransactionError": "aslmp.errors",
    "SlmpTransportError": "aslmp.errors",
    "SlmpConnectionEntryBusyError": "aslmp.errors",
    "SlmpNotConnectedError": "aslmp.errors",
    "SlmpConnectionLostError": "aslmp.errors",
    "SlmpConnectionClosedError": "aslmp.errors",
    "SlmpHandshakeError": "aslmp.errors",
    "SlmpNotSentError": "aslmp.errors",
    "SlmpDatagramSourceError": "aslmp.errors",
    "SlmpDatagramLostError": "aslmp.errors",
    "SlmpTimeoutError": "aslmp.errors",
    "TimeoutCause": "aslmp.errors",
    "SlmpProtocolError": "aslmp.errors",
    "SlmpFrameFormatError": "aslmp.errors",
    "SlmpSerialMismatchError": "aslmp.errors",
    "SlmpRouteMismatchError": "aslmp.errors",
    "SlmpShortDatagramError": "aslmp.errors",
    "SlmpTrailingDataError": "aslmp.errors",
    "SlmpUnsolicitedFrameError": "aslmp.errors",
    "SlmpPayloadShapeError": "aslmp.errors",
    "SlmpEndCodeError": "aslmp.errors",
    "SlmpUnsupportedCommandError": "aslmp.errors",
    "SlmpDeviceRangeError": "aslmp.errors",
    "SlmpDeviceNotAccessibleError": "aslmp.errors",
    "SlmpPointCountError": "aslmp.errors",
    "SlmpBitPointCountError": "aslmp.errors",
    "SlmpWordPointCountError": "aslmp.errors",
    "SlmpRandomPointCountError": "aslmp.errors",
    "SlmpRequestContentError": "aslmp.errors",
    "SlmpPlcFramingError": "aslmp.errors",
    "SlmpAsciiConversionError": "aslmp.errors",
    "SlmpDataLengthError": "aslmp.errors",
    "SlmpPlcTimeoutError": "aslmp.errors",
    "SlmpBusyError": "aslmp.errors",
    "SlmpMonitorNotRegisteredError": "aslmp.errors",
    "SlmpWriteProtectedError": "aslmp.errors",
    "SlmpRoutingError": "aslmp.errors",
    "SlmpConnectionStateError": "aslmp.errors",
    "SlmpRemotePasswordError": "aslmp.errors",
    "SlmpRemotePasswordLockedError": "aslmp.errors",
    "SlmpRemotePasswordLockoutError": "aslmp.errors",
    "SlmpCpuError": "aslmp.errors",
    "SlmpCpuUnsupportedRequestError": "aslmp.errors",
    "SlmpCpuDataTooLargeError": "aslmp.errors",
    "SlmpCpuRunningError": "aslmp.errors",
    "SlmpCpuFileError": "aslmp.errors",
    "SlmpCpuDeviceSpecError": "aslmp.errors",
    "SlmpCpuModuleError": "aslmp.errors",
    "SlmpCpuRemoteDisabledError": "aslmp.errors",
    "SlmpSemanticError": "aslmp.errors",
    "SlmpRemoteStateNotReachedError": "aslmp.errors",
    "SlmpVerificationError": "aslmp.errors",
    "SlmpProfileMismatchError": "aslmp.errors",
    "SlmpTargetChangedError": "aslmp.errors",
    "SlmpSinkError": "aslmp.errors",
    "SlmpOutcomeUnknownError": "aslmp.errors",
    "OutcomeUnknownReason": "aslmp.errors",
}

__all__ = ["__version__", *sorted(_EXPORTS)]

_SUBMODULES: Final[frozenset[str]] = frozenset(
    {
        "blocks",
        "client",
        "commands",
        "connection",
        "data",
        "entries",
        "errors",
        "health",
        "identity",
        "loop",
        "observability",
        "profile",
        "profiles",
        "resilience",
        "results",
        "sync",
        "testing",
        "timed",
        "timing",
        "tools",
        "transport",
        "wire",
    }
)
"""The public submodules, reachable as attributes of the package.

Python binds a submodule onto its parent package as a side effect of importing it, so
``import aslmp.sync`` has always made ``aslmp.sync`` work. What did not work is the form
the README documents on line 19 and every REPL user tries first::

    import aslmp
    aslmp.sync.Plc          # AttributeError, before this table existed

-- because a module-level ``__getattr__`` (PEP 562) *replaces* the default attribute
lookup on a package, and the one above answered only from :data:`_EXPORTS`. Every
submodule of the package was unreachable that way: ``sync``, ``testing``, ``tools``,
``blocks``, ``profiles``, ``errors``, ``wire``, ``transport``, ``commands``, ``data``.

The names are written out rather than discovered, for the same reason ``_EXPORTS`` is:
a ``pkgutil`` walk would import nothing but would still have to touch the filesystem on
first attribute access, and it would publish whatever happened to be lying in the
package directory. ``tests/unit/test_public_surface.py`` holds this set against the
package directory, so adding a module and forgetting this line fails the build.

Not in ``__all__``, deliberately, and this is the one place to say why rather than to
leave it looking like an omission. ``__all__`` is the DESIGN section 2 stability
contract over the *names this table resolves*: one row, one object, two minor versions
of notice before it moves. A submodule is not one of those -- it is reached by import,
it is named by ``aslmp.<name>`` in code that never says ``from aslmp import``, and
``tests/unit/test_tools.py`` holds ``__all__`` equal to ``_EXPORTS`` plus the version
precisely so that nothing can be added to the contract without a row. They are in
:func:`__dir__`, so REPL completion still finds them.
"""


def __getattr__(name: str) -> Any:
    """Resolve one public name, importing its module the first time it is asked for.

    The resolved object is written back into this module's globals, so the second
    lookup is an ordinary attribute access with no dictionary indirection and no
    ``importlib`` call. A submodule resolves the same way and costs the same once:
    ``importlib`` also binds it onto the package itself.
    """
    module_name = _EXPORTS.get(name)
    if module_name is None:
        if name in _SUBMODULES:
            module = importlib.import_module(f"{__name__}.{name}")
            globals()[name] = module
            return module
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(module_name), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Everything reachable from here: the lazy names and the submodules.

    Both, so that REPL completion after ``aslmp.`` shows ``Plc`` and ``sync`` -- the
    two things a reader of the README types first.
    """
    return sorted({*__all__, *_SUBMODULES})
