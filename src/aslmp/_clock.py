"""The one clock this package defaults to. Layer 0: stdlib only, no aslmp imports.

It lives in its own module rather than in :mod:`aslmp.timing` because that module is
deliberately clock-free -- it never calls a clock and does not import ``time``, so every
derived property is exact against a fake clock in a test, and
``tests/unit/test_timing.py`` enforces it. :mod:`aslmp.testing` may also only import
layers 0 to 2, and ``timing`` is 2.5, so a constant there would have been out of reach of
the simulator. Both rules said the same thing and both were right.
"""

from __future__ import annotations

import time
from typing import Final

__all__ = ["DEFAULT_CLOCK"]

DEFAULT_CLOCK: Final = time.perf_counter_ns
"""The clock every default in this package uses. **Not** :func:`time.monotonic_ns`.

``time.monotonic()`` is backed by ``GetTickCount64()`` on Windows before CPython 3.13,
and steps at **15.625 ms**. This package supports Python 3.11, so on the oldest
interpreter it claims, on Windows, every stamp landed on a 15.625 ms grid: a real 6 ms
round trip was recorded as ``0.0`` or ``16.0``. Measured 2026-09-12 on Python 3.11.15,
Windows 11, against ``perf_counter_ns`` on the same machine and the same sleep, which
reported 6.2-7.1 ms. It went unnoticed here for the ordinary reason -- development is on
3.13, where ``monotonic`` is ``QueryPerformanceCounter`` and resolves to 100 ns -- and it
surfaced only when the library was driven from a 3.11 environment.

A latency figure quantised to a 16 ms grid is worse than an absent one, because it looks
like data. "Latency as data" is this package's whole argument, so the default clock is the
one documented for measuring short durations. ``perf_counter_ns`` is monotonic within a
process on every supported platform and always has the finest resolution available.

``tests/unit/test_timing.py`` asserts the default resolves finer than a millisecond and
that no module reaches for ``time.monotonic_ns`` again.
"""
