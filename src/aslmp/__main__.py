"""Make ``python -m aslmp`` work.

The console script installed by the wheel points at :mod:`aslmp.tools.__main__`, but
``python -m aslmp`` is the natural thing to reach for, and it is the fallback whenever
the script is not on PATH -- which is common on Windows and inside CI containers that
install with ``--target`` or run out of a virtualenv they never activate.

This module deliberately does nothing except delegate. It must stay cheap: importing
``aslmp.tools`` pulls in ``argparse`` and the subcommand table, and the subcommands
themselves import lazily, so ``python -m aslmp --help`` still touches neither
``socket`` nor ``asyncio``. ``tests/unit/test_tools.py`` asserts that.
"""

from __future__ import annotations

import sys

from aslmp.tools.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
