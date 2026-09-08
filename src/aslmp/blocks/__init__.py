"""Blocks: declare a block of typed fields once, read it in one random read.

Three modules at three different layers, which is why this package is not uniform in
``tests/unit/test_layering.py``'s map:

* :mod:`aslmp.blocks.fields` -- Layer 1. The ``Annotated`` aliases and the two
  default-slot helpers. Pure; no profile.
* :mod:`aslmp.blocks.layout` -- Layer 2. ``@plc_block`` and the layout it computes at
  class-definition time. Pure; no connection, no socket.
* :mod:`aslmp.blocks.plan` -- Layer 5. ``bind()``, which joins a layout to a connected
  client, and the plans it returns.

**``plan`` is re-exported lazily and that is load-bearing.** Importing it pulls in
``aslmp.client`` and therefore ``socket``, and ``aslmp.blocks.layout`` is one of the
modules ``tests/unit/test_layering.py`` imports in a fresh subprocess to prove that
everything below layer 3 stays importable in a process with no event loop. A package
facade that imported ``plan`` eagerly would break that for the whole package, because
importing a submodule executes this file first. So ``from aslmp.blocks import bind``
works, and ``import aslmp.blocks.layout`` still costs no socket.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aslmp.blocks.fields import (
    F32,
    F64,
    I16,
    I32,
    U16,
    U32,
    Bit,
    BlockTiming,
    BlockTransaction,
    Bounds,
    PlcBlock,
    SlmpImplausibleValueError,
    Str,
    Word,
    at,
)
from aslmp.blocks.layout import BitFold, BlockLayout, FieldPlan, plc_block

if TYPE_CHECKING:
    from aslmp.blocks.plan import BlockPlan, Split, SplitBlockPlan, bind

__all__ = [
    "F32",
    "F64",
    "I16",
    "I32",
    "U16",
    "U32",
    "Bit",
    "BitFold",
    "BlockLayout",
    "BlockPlan",
    "BlockTiming",
    "BlockTransaction",
    "Bounds",
    "FieldPlan",
    "PlcBlock",
    "SlmpImplausibleValueError",
    "Split",
    "SplitBlockPlan",
    "Str",
    "Word",
    "at",
    "bind",
    "plc_block",
]

_LAZY = frozenset({"BlockPlan", "Split", "SplitBlockPlan", "bind"})


def __getattr__(name: str) -> Any:
    """Import ``aslmp.blocks.plan`` on first use, and never at package import.

    A module-level ``__getattr__`` (PEP 562) is the only way to publish a layer-5 name
    from a package whose layer-2 module must stay socket-free.
    """
    if name in _LAZY:
        from aslmp.blocks import plan

        return getattr(plan, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)
