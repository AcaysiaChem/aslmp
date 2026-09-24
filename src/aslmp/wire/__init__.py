"""Layer 0: the bytes. Pure functions of data, no I/O of any kind.

Nothing here opens a socket, reads a clock, or touches a file. ``tests/unit/test_layering``
proves it in a fresh subprocess by asserting that importing this package leaves ``socket``,
``ssl``, ``asyncio``, ``selectors``, ``threading`` and ``logging`` out of ``sys.modules`` --
so a frame builder can be checked against Mitsubishi's printed hex with no PLC, no network
and no mocking.

This module is deliberately empty of re-exports. Importing ``aslmp.wire`` should pull in
nothing: the codec, the device table, the address parser and the frame builders are imported
by name from their own modules, which is what keeps the purity test honest rather than
making it pass because the package happens to export little.
"""
