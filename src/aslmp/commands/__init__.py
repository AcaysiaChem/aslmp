"""Every SLMP command this package speaks, as frozen objects that encode and decode.

Layer 2. May import ``aslmp.wire``, ``aslmp.errors``, ``aslmp.profile`` and
``aslmp.profiles``. Importing this package must not pull ``socket``, ``ssl``,
``asyncio``, ``selectors``, ``threading`` or ``logging`` into ``sys.modules``
(``tests/unit/test_layering.py`` proves it in a subprocess), so every command is
testable one byte at a time with no event loop and no PLC.

One command is one object carrying its request bytes **and** its response decoder,
because a ``0403`` response has no framing of its own and the only thing that can parse
it is the request that produced it (:mod:`aslmp.commands.base`).

======================  ==============================================================
``base``                ``Command``, ``EncodeContext``, ``WordOrder``, the shared checks
``batch``               ``0401`` / ``1401`` -- contiguous runs
``random``              ``0403`` / ``1402`` -- the control-loop primitive
``block``               ``0406`` / ``1406`` -- several runs in one frame
``monitor``             ``0801`` / ``0802`` -- capability-gated, refused on iQ-F
``remote``              ``1001`` ``1002`` ``1003`` ``1005`` ``1006`` -- interlocked
``info``                ``0101`` ``0619`` ``1617`` -- identity, liveness, error clear
``password``            ``1630`` / ``1631`` -- literal characters, no encryption anywhere
``ondemand``            ``2101`` -- recognised on receive, never sent
``registry``            one catalogue, keyed by command code
======================  ==============================================================
"""

from __future__ import annotations

from aslmp.commands.base import (
    AddressLike,
    Command,
    CommandSummary,
    EncodeContext,
    WordOrder,
)
from aslmp.commands.batch import ReadBits, ReadWords, WriteBits, WriteWords
from aslmp.commands.block import BlockSpec, BlockWrite, ReadBlocks, WriteBlocks
from aslmp.commands.info import ClearError, ReadTypeName, SelfTest, TypeName
from aslmp.commands.monitor import ExecuteMonitor, MonitorRegistration, RegisterMonitor
from aslmp.commands.ondemand import (
    ONDEMAND_COMMAND,
    OnDemandMessage,
    is_ondemand,
    parse_ondemand,
    refuse_ondemand_as_response,
)
from aslmp.commands.password import LockPassword, UnlockPassword
from aslmp.commands.random import (
    AccessWidth,
    BitWrite,
    PointKind,
    RandomPoint,
    RandomValue,
    RandomWrite,
    ReadRandom,
    WriteRandom,
    WriteRandomBits,
    bit_point,
    dword,
    word,
)
from aslmp.commands.registry import COMMANDS, CommandSpec, by_code, codes
from aslmp.commands.remote import (
    RemoteLatchClear,
    RemotePause,
    RemoteReset,
    RemoteRun,
    RemoteStop,
    RunMode,
)

__all__ = [
    "COMMANDS",
    "ONDEMAND_COMMAND",
    "AccessWidth",
    "AddressLike",
    "BitWrite",
    "BlockSpec",
    "BlockWrite",
    "ClearError",
    "Command",
    "CommandSpec",
    "CommandSummary",
    "EncodeContext",
    "ExecuteMonitor",
    "LockPassword",
    "MonitorRegistration",
    "OnDemandMessage",
    "PointKind",
    "RandomPoint",
    "RandomValue",
    "RandomWrite",
    "ReadBits",
    "ReadBlocks",
    "ReadRandom",
    "ReadTypeName",
    "ReadWords",
    "RegisterMonitor",
    "RemoteLatchClear",
    "RemotePause",
    "RemoteReset",
    "RemoteRun",
    "RemoteStop",
    "RunMode",
    "SelfTest",
    "TypeName",
    "UnlockPassword",
    "WordOrder",
    "WriteBits",
    "WriteBlocks",
    "WriteRandom",
    "WriteRandomBits",
    "WriteWords",
    "bit_point",
    "by_code",
    "codes",
    "dword",
    "is_ondemand",
    "parse_ondemand",
    "refuse_ondemand_as_response",
    "word",
]
