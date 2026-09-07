# Changelog

Notable changes to `aslmp`. This project follows [Semantic Versioning](https://semver.org/) and
the stability contract in DESIGN section 2: `aslmp.__all__` is the public surface, deprecations
carry two minor versions' notice, and nothing is removed in a minor release.

Dates are the date the work landed. Hardware statements mean **MELSEC iQ-F FX5U-32MT/DS,
firmware 1.065** unless another CPU is named.

## Unreleased — 0.1.0.dev0

The first assembled version of the library. Everything below is new; there is nothing to
migrate from.

### The client

- `Plc` — async SLMP client for MELSEC iQ-F, iQ-R, Q and L over TCP and UDP, in binary and ASCII
  coding, in 3E and 4E frames. Typed scalar reads and writes (`read_f32`, `write_i32`, …), batch
  arrays, random access (`0x0403` / `0x1402`), block access (`0x0406` / `0x1406`), monitor
  registration, self test, type name, CPU status, clear error, and a `raw_command` escape hatch
  that bypasses command validation and nothing else.
- **Two required arguments: `host` and `profile`.** There is no generic profile and no fallback.
  `X` and `Y` are octal on iQ-F and hexadecimal on iQ-R, so `Y20` is output 16 on one and output
  32 on the other, and **both CPUs answer end code `0x0000`**. `aslmp identify` prints the
  profile string to pass.
- Bare values on the primary surface; `plc.timed.*` returns `Reading[T]` with the full
  transaction record. No `.value` on every line of a control loop.
- Blocks: `@plc_block`, `Annotated` field aliases whose static type is exactly `float` / `int` /
  `bool`, and a synchronous `bind()` that validates every span, checks the point budget,
  prebuilds the `0x0403` frame and compiles one `struct.Struct`. A bind failure surfaces at
  startup, not inside the loop.
- `aslmp.sync.Plc` — the same names over one background event loop owned for the facade's
  lifetime, never `asyncio.run` per call.
- `HealthMonitor`, `Supervisor` + `ExponentialBackoff` (policy required, no default),
  `EntryGroup` (named handles, no dispatch), `Cadence` (no `SKIP` overrun policy).
- `aslmp.testing` — a conformance simulator with three targets and pathology switches, each one
  reproducing something a real PLC did.

### Shaped by measurement

Every item here exists because of something the bench did, and each is written up in
[`docs/hardware.md`](docs/hardware.md).

- **No public `send()` anywhere.** Two TCP requests written before the first response is read
  return one response, for the *last* request, with end code `0x0000` — undetectable silent
  corruption on 3E. Bytes reach a socket only through a single-use capability token, exclusive
  per connection.
- `connect()` performs a `0x0619` Self Test with a per-generation nonce and compares the echo
  byte for byte, because `socket.connect()` demonstrably lies here. Plus a non-blocking EOF check
  that classifies an already-arrived FIN as `SlmpConnectionEntryBusyError`.
- `SlmpTimeoutError.likely_causes` computed from context, because wrong encoding, wrong
  transport, wrong frame type and an overstated request length all fail by **silence**.
- Length-driven reads with the receive stamp taken **after the last chunk**; TCP segmentation is
  real (1 of 3 identical 1931-byte reads split at the 1460-byte MSS).
- UDP: per-transaction epoch, rebind to a fresh source port on timeout, `SlmpDatagramLostError`
  naming the serial and the in-flight depth. Pipelining requires 4E; **3E/UDP pipelining is
  refused outright.**
- `TransportKind.TCP` is the default: UDP wins the median (p50 6.20 vs 7.41 ms) and TCP wins the
  tail (p99 10.49 vs 13.80, stdev 1.03 vs 1.79), and a control loop is a jitter problem.
- Three end-code corrections shipped with `Provenance.LIVE`: `0xC05C` (not `0xC05B`) for an
  absent device family, `0xC052` (a point-count error) for zero points, `0xC061` for an
  understated length with no response at all when overstated.
- `0x0801` / `0x0802` capability-refused on iQ-F citing the measured `0xC059`, and **never
  emulated with a `0x0403`**.
- `TS`/`TC`/`STS`/`STC`/`CS`/`CC` refused in random access even though this CPU **accepted `TS0`
  and answered `0x0000`**; the digits 8 and 9 refused in an X/Y literal even though this CPU
  **accepted a write to `Y8`**.

### Errors and observability

- The exception hierarchy of DESIGN section 3, hand-written, with a generated-and-committed
  ~90-row end-code table. `SlmpUsageError` is also a `ValueError`; `SlmpTimeoutError` is also a
  `TimeoutError`; `SlmpOutcomeUnknownError` is a sibling of the whole tree rather than a
  transport error, because `except SlmpTransportError: retry()` is right for a read and a
  data-loss bug for a write.
- One multi-line `__str__` for every class — target, request, sent, received, routes, timing,
  source, note — which never raises and never renders an end code in decimal.
- No `logging` calls in the library. Typed events, `Counters`, `MetricsSnapshot` and an
  allocation-free `LatencyRecorder`; `observability.attach_logging()` is the single bridge.
- Nothing retries, clamps, substitutes a default or returns a stale value, enforced by an AST
  test over the whole package.

### Command line

- One console script, `aslmp`, with eleven lazily imported subcommands: `probe`, `identify`,
  `read`, `write`, `cite`, `capabilities`, `ambiguities`, `verify-ranges`, `proxy`, `bench`,
  `serve`. `aslmp --help` imports neither `asyncio` nor `socket`, and that is asserted in a
  subprocess.
- **No subcommand can issue Remote RUN, STOP, PAUSE, LATCH CLEAR or RESET.** They are gated in
  the library behind `Plc(allow_remote_control=True)`; a shell history is not an interlock.
- `aslmp bench` refuses to print a table without a same-session raw-socket control, and the
  control's hand-written frame is asserted byte-identical to the library's own.

### Packaging

- **`aslmp/__init__.py` now re-exports the public API through a module-level `__getattr__`
  (PEP 562).** Before this, the package exported only `__version__` and nothing of the public
  API was reachable as `aslmp.X`. Eager imports were not an option: importing any submodule
  executes `__init__` first, so `from aslmp.client import Plc` at module scope would drag
  `socket` into `import aslmp.wire` and break the Tier 0 layering guarantee for every pure module
  at once. A `TYPE_CHECKING` block keeps `mypy`, `pyright` and IDEs seeing ordinary imports, and
  a subprocess test asserts that importing `aslmp` opens no socket, starts no thread and reads
  no file.
- Zero runtime dependencies, deliberately: the wheel must install on a Jetson's aarch64 and on a
  locked-down plant PC with no compiler.

### One deviation from the locked design, recorded

**`aslmp/testing/` ships in the default wheel.** DESIGN section 5.1.7 asks for the opposite ("no
simulator, no bench") and section 1.15 puts the simulator behind an `aslmp[testing]` extra.
Implemented literally, that gate achieves nothing:

1. The `testing` extra declares **no dependencies**, so `pip install aslmp[testing]` and
   `pip install aslmp` resolve to the same bytes. A Python extra selects *dependencies*; it
   cannot select *modules* of its own package. Excluding `aslmp/testing` from the wheel would
   make `pip install aslmp[testing]` install a package **without** the simulator — an extra that
   cannot deliver what it promises.
2. The simulator is nine modules of pure stdlib and is what `aslmp serve` runs. Somebody
   integrating against a PLC they cannot reach wants it from the installed wheel, not from a git
   checkout.
3. The property that actually matters — DESIGN section 4.11's rule that `aslmp.testing` must not
   import `aslmp.transport`, `aslmp.connection` or `aslmp.client`, so that a transport bug cannot
   be invisible to every client-to-server test — is enforced by `tests/unit/test_layering.py` and
   `tests/unit/test_public_surface.py`, and is unaffected by which files are in the archive.

What section 5.1.7 was protecting — a dependency-free, import-light client wheel with exactly one
thing on `PATH` — is asserted in `tests/unit/test_wheel_contents.py` and holds. `bench/` genuinely
stays out: it lives at the repository root and its scripts need a PLC.

### Integration pass — found by assembling the units, and by the bench (2026-09-07)

Five changes that no single build unit could see, plus the first end-to-end run against real
iron. `tests/hardware/test_fx5u.py` is 23 tests, marked `hardware`, gated on `ASLMP_TEST_HOST`,
and all 23 pass against FX5U-32MT/DS fw 1.065 at 192.168.10.250.

- **`Counters.segmented_responses` fired on 100% of TCP transactions.** It counted "more than one
  chunk", but a stream response is read as the fixed prefix and *then* exactly `L` more units, so
  two `recv` calls is the structural floor and never evidence of anything. `Chunk` gained
  `partial` — this read came back *short of what it asked for* — and `TransactionTiming.segmented`
  is now `any(chunk.partial …)`. On the bench a 960-word read (1931 bytes) arrives as `9 + 1922`
  five times in six on one run and not at all on the next, and as `9 + 1451 + 471` once: the MSS
  split is real, intermittent, and now actually distinguishable. `aslmp probe`'s "arrived in more than one TCP segment" line was
  unconditionally true before this and is now a finding.
- **`Plc.bind()`, `Plc.read_block()` and `Plc.write_block()` exist.** DESIGN section 2.7 puts them
  on the client; the blocks unit could not add them to a file it did not own, so the only entry
  point was `aslmp.blocks.bind(plc, Block)`. They are thin delegates with a deferred import
  (`client` and `blocks.plan` are declared Layer 5 peers, so only one of them may name the other
  at module scope). `read_block`/`write_block` take a bound `BlockPlan`, never a class: binding per
  cycle would rebuild and revalidate the frame per cycle, which is the whole cost `bind` pays once.
- **`Plc(udp_pipeline_depth=…)`, additive to the locked constructor of DESIGN section 2.2.** 4E over
  UDP is measured clean to depth 32 and loses ~31% at 64, but the client had no door to it and
  built every `UdpTransport` at depth 1, so a capability the transport implemented was unreachable.
  It defaults to 1, is refused above 1 on 3E (no serial to correlate by) and is **refused, not
  ignored, on TCP** — accepting a number that cannot take effect is the shape of failure this
  library exists to refuse.
- **Six public names were unreachable from the package root:** `PlcBlock` (so the documented
  `class LoopState(PlcBlock)` did not compile against `from aslmp import …`), `BlockTransaction`,
  `BlockTiming`, `ProbeOutcome`, `RandomValue` and `SlmpCadenceOverrunError` — an exception raised
  by an exported class and catchable only through a deeper import. Two tests now lock the two
  sub-surfaces where that direction of rot matters: `aslmp.blocks.__all__` must be a subset of
  `aslmp.__all__`, and every public `Slmp*Error` must be catchable from the root.
- **`tests/unit/test_layering.py`'s purity check was partly vacuous.** It imported `aslmp.wire`,
  whose `__init__.py` is deliberately empty, so that row would have passed with a socket in every
  module of the package. `aslmp.wire.codec`, `aslmp.wire.frames` and `aslmp.blocks.fields` are now
  named directly. Proved by injecting `import socket` into `wire/codec.py` and a
  `commands → client` edge into `commands/base.py`, watching four tests fail with the offending
  edge named, and reverting.

### Measured on the bench, 2026-09-07 (FX5U-32MT/DS fw 1.065, Wi-Fi client at 192.168.10.41)

| what | number |
| --- | --- |
| handshake (`0x0619` + `0x0101`), TCP 5002 | 8.1 ms; `FX5U-32MT/DS`, model code `0x4A49` |
| `read_f32` p50 / p99, n=120, TCP | 7.46 / 10.81 ms |
| raw-socket control, same session, n=120 | 7.39 / 10.79 ms — the library's overhead is **0.07 ms at p50** |
| one bound block read (5 values, one `0x0403`) | 7.75 ms |
| the same five values as five `0x0401`s | 38.24 ms wall — **4.9x** |
| 16 serial reads, TCP | 128 ms, 125 txn/s |
| 16 pipelined 4E reads, UDP depth 16 | 48 ms, 331 txn/s — **2.6x**, 0 lost |
| 4E/UDP burst at depth 8 | 8/8 answered, 25.8 ms, 311 txn/s, every serial matched |
| 960-word read (1931 bytes) | 8.4–11.3 ms; split at the MSS on 1 of 6, then 0 of 6 |
| `D8000` with `validate_ranges=False` | `0xC056` → `SlmpDeviceRangeError` |
| 961 words via `raw_command` | `0xC052` → `SlmpWordPointCountError` |
| two `0x0401`s written in one `send` | **13 bytes back — ONE response, end code `0x0000`** |

The coalescing corruption was re-measured on this run and is unchanged: the whole one-in-flight
architecture is still load-bearing. Floats are low word first, proved against the running
controller (`60.0` at `D0` as `0x0000 0x4270`; assembled the other way it reads `2.383e-41`), and
`3.4028235e38` round-trips through `D100` as `0xFFFF 0x7F7F` — the register above `0x7FFF` that
`pymcprotocol` packs with `'<h'` and cannot write.

### Known limitations shipped knowingly

Listed in full in [`docs/unverified.md`](docs/unverified.md) and in `aslmp capabilities
--unverified-only`. In short: our iron is **one** FX5U-32MT/DS on firmware 1.065, binary 3E/4E,
over TCP and UDP. The entire ASCII path, the iQ-R / Q / L profiles, every remote-control command,
`0x0406`/`0x1406` block access, the remote password commands and `0x2101` receipt are all
implemented, gated and **labelled unverified**.

`bench/` has published no measured numbers yet: the scripts are exercised against the simulator
and have not been run against the FX5U. The numbers in the table above come from
`tests/hardware/test_fx5u.py`, which does carry its own same-session raw-socket control.
