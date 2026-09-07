"""An async SLMP client for Mitsubishi MELSEC PLCs.

Public re-exports only. This module holds no logic and must have no import side
effects: importing ``aslmp`` must not open a socket, read a file, or start a loop.
"""

from __future__ import annotations

from aslmp._version import __version__

__all__ = ["__version__"]
