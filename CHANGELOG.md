# Changelog

Notable changes to `aslmp`. This project follows [Semantic Versioning](https://semver.org/) and
the stability contract in DESIGN section 2: `aslmp.__all__` is the public surface, deprecations
carry two minor versions' notice, and nothing is removed in a minor release.

Dates are the date the work landed. Hardware statements mean **MELSEC iQ-F FX5U-32MT/DS,
firmware 1.065** unless another CPU is named.

## 0.1.0 — 2026-09-25

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
- `TransportKind.TCP` is the default — for **configurability**, not for latency. A UDP SLMP entry
  on iQ-F is point-to-point (GX Works3 will not save one without a destination IP, and there are
  eight entries in total), and loss is silent on UDP. The latency argument this default originally
  rested on was withdrawn on 2026-09-07; see the wired re-measurement below.
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
   `pip install aslmp` resolve to the same bytes. (Neither resolves to anything at all today:
   there is no `aslmp` on PyPI and the README says to install from a checkout. The argument is
   about what a Python extra can select, and it holds whenever the first release happens.)
   A Python extra selects *dependencies*; it
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
iron. `tests/hardware/test_fx5u.py` had **23 test functions** at that run, marked `hardware`,
gated on `ASLMP_TEST_HOST`, and they all passed against FX5U-32MT/DS fw 1.065 at
192.168.10.250. It has **24 functions and collects 25 tests today**: one function was added
afterwards (the peer-bound wired UDP entry) and one has been parametrized twice all along, so
even that day pytest reported 24 where this entry said 23.

That is the whole of the "23 against 25" disagreement with the README, and it is written out
rather than quietly corrected because the cause is general: **functions and collected tests are
two different counts**, and a number typed into prose is a third thing that was true once.
Counts belong in output, not in documents; `python -m pytest -q` prints the real one, which is
why the README no longer states a suite total at all.

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
  UDP is measured clean to depth 32 and lossy above it, but the client had no door to it and
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
| raw-socket control, same session, n=120 | 7.39 / 10.79 ms — **indistinguishable from the library at this n** (see below) |
| one bound block read (5 values, one `0x0403`), n=9 | 7.75 ms **of wire time** |
| the same five values as five `0x0401`s, n=9 | 38.24 ms **of wall time** — not the same clock, see below |
| 16 serial reads, TCP | 128 ms, 125 txn/s |
| 16 pipelined 4E reads, UDP depth 16 | 48 ms, 331 txn/s — **2.6x**, 0 lost |
| 4E/UDP burst at depth 8 | 8/8 answered, 25.8 ms, 311 txn/s, every serial matched |
| 960-word read (1931 bytes) | 8.4–11.3 ms; split at the MSS on 1 of 6, then 0 of 6 |
| `D8000` with `validate_ranges=False` | `0xC056` → `SlmpDeviceRangeError` |
| 961 words via `raw_command` | `0xC052` → `SlmpWordPointCountError` |
| two `0x0401`s written in one `send` | **13 bytes back — ONE response, end code `0x0000`** |

**Two numbers in that table were withdrawn on 2026-09-07 for claiming more than the run
supports**, and the rows above are the restated versions:

- **"the library's overhead is 0.07 ms at p50" is gone.** It was finer than its own noise
  floor. That run was n=120 from the laptop at 192.168.10.41 over **Wi-Fi** at ~7 ms median
  RTT; at n=120 with sd ~1.03 ms, the standard error on a *median* is about
  1.253 × 1.03 / √120 ≈ 0.12 ms, and on a difference of two medians about 0.17 ms — more than
  twice the quantity claimed. The run also took its raw-socket control **once, before** the
  library's samples, where `docs/benchmarking.md`'s own rule is a control before *and* after so
  that the drift between them is the error bar. What the run supports is: **7.46 against 7.39 ms
  at p50, indistinguishable at this n, and the library is not faster than a raw socket** — which
  is the assertion the test actually makes, and the one worth having. `prior art` still records
  `plc-comm-slmp`'s +0.24 ms, which we do not claim to beat.
- **"4.9x for block reads" is gone.** It divided the block's *wire* time by five reads' *wall*
  time: the numerator excludes this client's scheduling and the denominator includes four
  helpings of it. The like-for-like figure — the sum of the five reads' own wire stamps — is
  recorded by the same test as `five_reads_wire_sum_p50_ms` and printed under `-s`, and was not
  written down, so it is not published. From two figures that were: single `read_f32` wire p50
  was 7.46 ms on that link and day, so five are ~37.3 ms of wire and the like-for-like
  multiplier is **about 4.8x** — an inference from published numbers, labelled as one. The
  argument for blocks was never the multiplier: five reads are five moments and one `0x0403` is
  one moment.

`docs/benchmarking.md` now carries both as rules — **no ratio between two different clocks**,
and one control is not a bracket.

The coalescing corruption was re-measured on this run and is unchanged: the whole one-in-flight
architecture is still load-bearing. Floats are low word first, proved against the running
controller (`60.0` at `D0` as `0x0000 0x4270`; assembled the other way it reads `2.383e-41`), and
`3.4028235e38` round-trips through `D100` as `0xFFFF 0x7F7F` — the register above `0x7FFF` that
`pymcprotocol` packs with `'<h'` and cannot write.

### Re-measured on a WIRED link, 2026-09-07 — one published claim withdrawn, two of our bugs found

An external reviewer challenged several of the claims above. Re-measuring from a second host —
`argus-bench`, 192.168.10.36, **wired**, 3.64 ms median RTT, through a UDP connection entry
(5005) added to the CPU for it — **overturned one of them and exposed two real bugs**. Everything
in this block is written from the wired numbers, and every latency table in the documentation now
names its host and its link as well as the CPU, the firmware and the date.

- **Withdrawn: "UDP wins the median, TCP wins the tail."** That was the published justification
  for `TransportKind.TCP` being the default, and it **did not reproduce on wire**. Interleaved
  TCP/UDP/TCP/UDP/TCP, 300 samples each, controls before and after drifting 0.01 ms at p50: UDP
  wins **every** percentile — p50 2.42 against 3.63 ms, p90 3.40 against 4.05, p99 3.56 against
  4.69 — at essentially equal standard deviation (0.40 against 0.36). The Wi-Fi tail result was a
  property of the radio, where a lost datagram costs a full client timeout and TCP fast
  retransmits. **The default is unchanged and its justification is replaced**: a UDP entry on
  iQ-F is point-to-point, so it only serves hosts somebody configured an entry for, out of eight;
  a TCP entry serves any peer; and loss is silent on UDP. `aslmp.client.TRANSPORT_CHOICE` carries
  the corrected note, `docs/hardware.md` section 5 prints both tables side by side, and the
  withdrawal is a row of the shipped ambiguity table (`A-UDP-TAIL-LATENCY`), because what we got
  wrong in public is more use to the next person than the conclusion was.
- **Corrected: the UDP receive queue is a hard 32, not a soft degradation.** Wired, depth 48
  answered exactly 32 and lost 16; depth 64 answered exactly 32 and lost 32. The earlier Wi-Fi
  reading of 44/64 — "~31% loss at 64" — came from a link slow enough that the CPU drained part
  of the queue while the rest of the burst was still arriving. Pipelining to 32 buys ~1.5x over
  serial TCP on wire (405 against 276 txn/s), not the ~2.7x the Wi-Fi numbers suggested.
- **Remote RUN, STOP and PAUSE are now measured**, against the CPU's own free-running scan
  counter rather than SD203 alone. The reason for abstaining — a memory-card error that once left
  this CPU declining a remote RUN — is gone with the card. **`0x1006` Reset and `0x1005` Latch
  Clear were still not sent and remain unverified**, as does the behaviour that makes
  `verify=True` the default: Remote RUN returning `0x0000` with the switch in STOP could not be
  forced on a bench whose switch is in RUN, so it stays a manual claim in `docs/unverified.md`.
  (The reviewer independently saw GX Works3 report "The RUN operation has been completed" while
  P.RUN stayed dark — the same lie through Mitsubishi's own tool, and still not our measurement.)
- **Bug, shipped and fixed: `verify=True` raised for a RUN the CPU had accepted.** Leaving RUN is
  effectively synchronous (SD203 correct on the first poll, 3 of 3, ~18–21 ms) but **entering RUN
  is not** (SD203 still `STOP` on the first poll in 2 of 3 cycles, reaching `RUN` at 25–33 ms).
  `_apply` read SD203 exactly once, immediately, so the safety default produced a false
  `SlmpRemoteStateNotReachedError` roughly two thirds of the time — unusable in practice. Now it
  observes SD203 to a bounded deadline (`REMOTE_SETTLE_SECONDS` = 0.25 s, an order of magnitude
  above the worst transition seen) and returns the instant the state matches. **This is not a
  retry and not silent recovery**: the state-changing command is sent exactly once and never
  re-sent; what repeats is the *observation*, and `RemoteResult.polls` reports how many reads it
  took. `run(verify=True)` costs 3 transactions, which is what the measurement predicts.
- **Bug, shipped and fixed: the README's own block example declared the wrong field type.** The
  front-door example read the free-running counter as `U32` where the PLC program's global label
  is `FLOAT [Single Precision]`, decoding a float's bit pattern as `1226168560`. Nastier than it
  looks: IEEE-754 patterns rise monotonically for positive floats, so the counter still
  *increased* every cycle and a naive "is it advancing?" check passed — ours did. Only the rate
  is wrong, and it drifts, because a `+1.0` moves the `U32` reading by one ulp-step. The example
  is corrected and says so, and the response in code is **declared plausibility bounds**:
  `Annotated[float, F32(minimum=…, maximum=…)]` on block fields and `minimum=`/`maximum=` on the
  typed scalar reads, raising `SlmpImplausibleValueError` rather than returning the value.
  Nothing sniffs types and nothing clamps: a D register carries no type on the wire, so the
  library holds you to the promise you made and refuses to guess.
- **The entry-release race, measured and explained.** A reconnect within ~2 ms of your own clean
  `close()` can be refused: 1/6 at 0 ms, 2/6 at 1 ms, 6/6 from 2 ms out to 200 ms, wired. Over
  Wi-Fi the same test was 30/30 at every gap including 0 ms — **the window is link-dependent and
  invisible on a slow link**, which is why an outside reviewer could not run `bench/transports.py`
  at all. It is a race against the CPU's FIN processing, not a hold period, and a *faster* link
  should need *more* client-side gap. The library still does nothing about it on purpose; the
  bench scripts take a named `ENTRY_RELEASE_SETTLE_S` (5 ms) outside every timed section, and
  `SlmpConnectionEntryBusyError` now names both causes. Ambiguity `A-ENTRY-RELEASE-RACE`.
- **A five-minute closed-loop soak, `bench/soak.py`.** 15,000 cycles, 30,005 transactions, 300.0 s
  at exactly 50.0 Hz, from `argus-bench` (192.168.10.36) over the **wired** link at 3.64 ms
  median RTT, TCP 5002: **0 errors, 0 reconnects, 0 entry-busy, 0 cadence
  overruns**, block read p50 3.67 / p99 4.69 ms with the p50 moving 3.72 → 3.67 ms across the run,
  and the CPU scanning at 969/s under load against that session's own idle reference of 1029/s —
  a cost of about 5–6 % of scan rate, the range being the 1.1 % disagreement between that
  reference and the repository's standing idle figure of 1018/s (`hardware.md` section 17).
  That table came from the throwaway harness `bench/soak.py` was promoted from, which is why it
  has no control rows. The claim worth having is not the
  latency: the bench PLC's controller has a proportional band of 12 %/K, so `MV == clamp(Err * 12,
  0, 100)` must hold within every `0x0403` snapshot, and the soak asserts it **every cycle**.
  Read back in full `f32` precision, `Err` 0.4399986267089844 → `MV` 5.2799835205078125 and `Err`
  3.477001190185547 → `MV` 41.72401428222656, plus a sweep across both clamps — deviating by
  exactly 0.0 everywhere. A client that decoded garbage could still draw a smooth latency
  curve; it could not close the PLC's own gain arithmetic to the last bit of an `f32`. This is the
  first table `bench/` has published, and it is the only script here that writes to a PLC.

### The honesty sweep, 2026-09-07 — tracing a wrong number instead of patching where it showed

An adversarial review's headline was that the `U32`-against-a-`REAL` misread had been fixed
where it was found and **not traced**. It was right. This pass follows the arithmetic instead.

- **`README.md`'s "`D8` — 61.6 µs per count" was the misread's residue, and is replaced.**
  Measured properly, `D8` read as the `f32` it is: **20,374 counts in 20.014 s = 1018.0 scans/s,
  982.3 µs per scan**, FX5U-32MT/DS fw 1.065, 2026-09-07, from the laptop at 192.168.10.41 over
  Wi-Fi, agreeing with 1 s, 5 s and 10 s windows to 0.4 %. The old figure was **15.9x** too
  small, which is not a coincidence: at the ~613,775 the counter stood at, one `+1.0` in the
  REAL moves the bit pattern by 16, so a `U32` reader counts sixteen times too fast. A wrong
  declared type does not stop at a wrong reading — it produces every number computed from that
  reading, and those land in prose, where no test can reach them.
- **Every figure derived from a `D8` rate or a scan period was re-derived**, and the table of
  what moved and what deliberately did not is `docs/hardware.md` **section 17**, which is new.
  The repository now publishes **one** idle scan rate with its conditions (1018/s), the
  `~0.97 ms` scan period becomes 0.98 ms, and the soak's "about 6 % of scan rate" becomes
  "about 5–6 %" because it is a ratio of two scan rates and inherits the 1.1 % disagreement
  between the two idle references. Prose in `bench/`, `tests/hardware/` and `aslmp.testing`
  still quotes ~1024 and ~1029; those files were outside this pass and are named in section 17
  rather than left for somebody to find. **(Superseded the same day.** Naming them in a table
  was a promise, not a fix, and the next entry below is what closing them actually took. That
  is the pattern this project keeps repeating and the reason there is now a test.)
- **Two retracted claims were still shipping in runtime text.** `aslmp --help` told five of
  eleven subcommands that "TCP wins the latency tail" — the claim withdrawn on 2026-09-07 and
  corrected in `client.TRANSPORT_CHOICE` — and `aslmp.testing.pathology.ONE_CONNECTION`, a
  `Measurement`, asserted that "the slot frees immediately on close", which the ~2 ms
  entry-release race disproves. Both are corrected, as is the same sentence in the shipped
  ambiguity row `A-ONE-TCP-CONNECTION`, which now carries the withdrawal and points at
  `A-ENTRY-RELEASE-RACE`.
- **`Measurement` and the TSV provenance tail gained `host`, `medium` and `samples`.** The
  three conditions whose absence cost this project a published claim now have somewhere to
  live, optional so that every existing row stays valid, and filled in wherever the source
  actually records them — every live row of 2026-09-06 (one host, one link existed that day)
  and the wired 2026-09-07 timing rows. Empty means "not written down", which is deliberately
  not the same statement as "no host". The column is `medium` rather than `link` because
  `limits.tsv` already spends `link` on the CPU port against an FX5-ENET module, whose budgets
  differ (960 against 949 points): the first draft used `link`, `read_table`'s `dict(zip(...))`
  silently kept only one of the two, and every FX5U limit row lost its `cpu`/`enet` key. A test
  now refuses any tail column that collides with a table's own.
- **`docs/unverified.md` was billed as complete and was not.** It omitted four selectable
  profiles — `melsec:iq-f/fx5uc`, `fx5uj`, `fx5s` and `melsec:iq-r/r00` — three of them iQ-F,
  where a reader will assume the FX5U bench numbers carry over. It also said Q and L ship
  manual-derived device ranges; they ship **no range table at all**, and check presence without
  checking spans. Both corrected, and completeness is now a test over `aslmp.profiles.KEYS`
  rather than a claim.
- **`pip install aslmp` does not work**, because there is no `aslmp` on PyPI. The README now
  says to install from a checkout until the first release, and the suite's test count has been
  taken out of the README rather than left to go stale.

### Behaviour changes, 2026-09-07 — three of them break callers

The pass before this one traced a wrong number. This one closes the *shape* the number came
from: a default standing in for a fact only the caller knows. `aslmp` is `0.1.0.dev0` with
nothing released, so each guess is **removed** rather than deprecated and carried forward for
a compatibility nobody needs yet.

#### Breaking: three arguments lost their defaults, because the defaults were guesses

- **`PlcClockSource.kind` is required.** The class carried an address and nothing else, and
  `blocks/plan.py` read the clock point as a hard-coded unsigned double word. On the bench
  that wrote it, `D8` is a `REAL`, so `tx.plc_clock` published a float's **bit pattern**: a
  number that rises on every scan, is monotonic, is never flagged, and is wrong by a factor
  that *drifts* as the float crosses a power of two. Measured in one session: 1018 real
  counts/s reported as 16,274. `kind` then spent one revision defaulting to `"u32"` "for
  compatibility" — the identical defect one layer out, since `PlcClockSource("D8")` would
  still have published 16,274 for 1018. It has no default now. `bounds=` is the optional
  second half: a clock outside the declared range raises `SlmpImplausibleValueError` rather
  than being published.
- **`aslmp read --as` and `aslmp write --as` are required.** `read`'s defaulted to `u16`,
  which made `aslmp read 192.168.10.250 D8` print `54720` — the low half of that same float's
  bit pattern, end code `0x0000`. "What is in `D8`" is the first question a newcomer asks and
  the register does not know the answer, so the caller is asked, exactly as `--profile` asks.
  `read` was fixed first and `write` — the *mutating* half of the same pair, where a wrong
  `--as` moves a machine rather than misprinting a number — kept the default for one revision.
  Both are `required=True` in their parsers now, over one shared `KindRequiredParser` and one
  shared `VALUE_KINDS`, so the pair cannot drift apart again.
- **`unsigned()`'s `signed_field` is required.** It promised "never masked and never
  truncated" in its own docstring and then accepted the union of the signed and unsigned
  ranges before masking, so `write_i16(40000)` was accepted and read back `-25536`. It
  enforces the declared type now. `signed_field` then spent one revision defaulting to
  `None` — the same guess-shaped default again, on the function whose whole job is to refuse
  guesses — and that default is gone. `None` remains a legal *explicit* argument for exactly
  one caller: `write_words`, the raw-register door, where the caller has said they mean bits.

#### Refusals where there used to be a plausible wrong answer

- **`monitor_read` refuses a registration this client did not make.** A `MonitorRegistration`
  from another client raised nothing and sent an `0x0802` that read whatever *that* client's
  list happened to be — right-looking values from the wrong registers. It now raises
  `SlmpConfigurationError`, names both clients, and sends nothing.
- **`read_block` / `write_block` ignored their receiver entirely** — `return await
  plan.read()`, with `self` unused. A `Plc` pointed at a non-existent host and never connected
  returned a populated block and reported a successful write, while the real PLC's registers
  moved on a different client. A plan used on the wrong client now raises and names both peers.
- **`raw_command(expect_response=False)` corrupted the next transaction.** It put a
  response-producing request on the wire, never read the answer, and left the connection
  `READY`, so the following read returned the previous request's bytes with end code `0x0000`
  — the exact corruption the in-flight gate exists to prevent, reached without touching the
  gate. The connection is retired instead: the escape hatch costs the socket, which is the
  honest price for nothing having read the answer.
- **`raw_command` bypassed the `allow_remote_control` interlock**, because the interlock lived
  in `Command.validate()` and `_RawCommand.validate()` is a deliberate no-op. The interlock is
  a property of the client and is checked there now.
- **`write_bits` no longer coerces** `2`, `-1` or `"yes"` to `True`.
- **Value-domain failures on write escaped the documented error tree** as bare `OverflowError`,
  `struct.error` and `TypeError`. They are `SlmpValueRangeError`, and nothing is sent.

#### The simulator agreed with the defect, which is why ~4,100 tests missed it

- **`aslmp.testing` modelled `D8`/`D9` as an integer double word**, and its docstrings said so.
  The bench's `D8` is a `REAL`. So every simulator-backed test of `plc_clock` passed while the
  library published a bit pattern: a test that cannot fail is worse than no test, because it is
  counted. The simulator models the register the way the silicon does; `bump_u32` stays for a
  counter a CPU really declares `DWORD`, and says explicitly that it is not the bench's `D8`.
  The replacement test was proved to fail without the fix, two independent ways.
- **`PlcSimulator(pathology=...)` was honoured by the socket layer and ignored by every
  handler**, so `remote_run_lies`, `remote_reset_no_response` and
  `accept_illegal_random_points` were dead when set that way — including the one whose stated
  purpose is to give `verify=True` something real to catch.

#### The command line said things that were not true

- **`aslmp ambiguities` surfaced 14 of the 30 rows** in the shipped table, and
  `--key A-UDP-TAIL-LATENCY` reported no such ambiguity — denying the existence of this
  project's own published retraction. A test now asserts the CLI surfaces exactly as many rows
  as the file holds.
- **`aslmp capabilities` told every user, for every profile, that we deliberately never sent a
  remote-control command**, after RUN, STOP and PAUSE had been driven against this CPU and
  verified against its scan counter.
- **`import aslmp; aslmp.sync` raised `AttributeError`** while README section 1 advertises
  `aslmp.sync.Plc`. The documented submodules resolve through the lazy `__getattr__` now, and
  importing `aslmp` still pulls in neither `socket` nor `asyncio`.
- A deliberately answerless transaction was booked as a **failure** while the call returned
  successfully, and `aslmp serve` wrote its banner through a block-buffered stdout while
  defaulting to an ephemeral port, so backgrounding the simulator to a log file left the port
  unknowable.
- **`aslmp read`'s usage line printed `[--as {...}]`** — brackets, meaning optional — directly
  above a help string that began "REQUIRED", in the command this session had just made
  required. The requirement lived in the body, where argparse could not see it. `--as` is
  `required=True` in the parser now, so the usage line, the help and the exit status agree,
  and the reason a caller is being asked (`WHY_AS_IS_REQUIRED`) is still what gets printed
  rather than argparse's one-liner — that is what `KindRequiredParser` is for.

### One number, one place: the scan rate and the latency conditions (2026-09-07)

The section above and the honesty sweep before it each fixed an instance and left a sibling.
This pass exists to stop that being true of the *numbers*, and to leave a test behind rather
than a promise.

- **The repository published one idle scan rate and then contradicted itself in four files.**
  `docs/hardware.md` said 1018/s; `bench/soak.py` said 1029; `tests/hardware/test_remote_control.py`
  said ~1029 twice; `tests/hardware/test_fx5u.py` said "the documented ~1024" and then
  *depended* on it, as `expected = 1024 * 2.0`. ~1024 was never a measurement. The published
  figure is **1018 scans/s, 982 µs per scan** with its conditions in `docs/hardware.md`
  section 17; `982.3 µs` is `1e6 / 1018.0` rather than a second measurement; and every one of
  those files now cites that section instead of restating the number as a fact of its own.
  `tests/hardware/test_fx5u.py` derives its expectation from `IDLE_SCAN_RATE_HZ`.
- **Section 17's own measurement named the wrong host.** The 20 s window — 20,374 counts in
  20.014 s — was taken from `argus-bench` (192.168.10.36) on the **wired** link, not from the
  laptop over Wi-Fi as published; the laptop's own 20 s window is a separate figure, **1018.4**,
  and the two are 0.04 % apart. Nothing about the number moves, which is exactly why the
  mislabel survived. A table that names the wrong host is not labelled.
- **A within-run pair may no longer be split from its partner.** `969/s` under load is the
  wired soak's loaded figure and `1029/s` is that same run's own idle reference; quoting either
  alone turns a within-session ratio into a repository-wide claim, which is how ~1029 became a
  standing figure in two test modules. The percentage that pair supports — "about 5-6 %" — is
  derived from both endpoints (5.8 % against its own reference, 4.8 % against the published
  figure) rather than asserted.
- **`tests/hardware/test_remote_control.py` stopped deriving a scan rate from a 300 ms
  window.** Two ~7 ms round trips are 5 % of 300 ms, and dividing anyway is how this repository
  came to carry three figures for one quantity. The counts (324 running, 0.0 stopped, 326
  running again) are what that test asserts on and they stay; the rate is quoted from
  section 17.
- **Latency figures that named neither a host nor a link were found and labelled** — which is
  the rule this project adopted *in writing* after withdrawing "TCP wins the tail", and then
  did not apply to its own front door. The TCP handshake's 5.4 ms, `probe`'s round trip, the
  `TCP_NODELAY` p50 difference, the block-against-five-reads pair, the UDP queue ladder, the
  27 ms atomicity split, the withdrawn `+0.07 ms` overhead, and — in `README.md`,
  `docs/benchmarking.md` and
  `bench/_report.py`, three copies of the same unlabelled figure — the two afternoons that are
  this project's own argument for demanding a control. Where a condition was never written down
  (the dates of those two afternoons) it is printed as a gap in the record rather than guessed
  at. `docs/architecture.md`'s "11.0 ms for a 14.0 ms transaction" is now labelled as
  illustrative arithmetic, because it is, and an unlabelled illustration reads as a measurement.
- **The `D8` misread finally has an ambiguity row**, `A-SCAN-COUNTER-TYPE`, carrying the same
  candour as `A-UDP-TAIL-LATENCY`: both are there because the project published them wrong. Its
  third reading — "the wire settles it" — is the false one, and saying so is the whole reason
  the required arguments above exist. Its open half is a question for a Mitsubishi engineer: is
  there **any** iQ-F command that reports a device's declared type? If one exists it closes the
  class rather than the instance.
- **The enforcement test is the deliverable.** `tests/unit/test_citations.py` reads the 20 s
  row out of `docs/hardware.md` section 17, divides counts by seconds, and holds every scan
  figure in `README.md`, `CHANGELOG.md`, `docs/`, `bench/`, `tests/hardware/` and `src/aslmp/`
  against that one derived value; refuses `~1024`, `~1029` alone and `61.6 µs` by name;
  requires every scan period in prose to equal `1e6 / 1018.0`; requires every file carrying a
  scan figure to cite section 17; requires every latency table and every `p50` line in the
  reader-facing documents to name a host and a medium; and requires an option whose help calls
  itself required to be required in its own parser. A number that drifts, a sibling left
  behind, or a help string that contradicts its usage line fails the suite instead of waiting
  for the next review.

### Prepared for publication (2026-09-24)

Documentation, disclosure and repository scaffolding, ahead of the repository being opened.
No change to the client's behaviour, the wire, or any published measurement.

- **`SECURITY.md` is new, and its first half is not about this library.** SLMP has no
  authentication, no encryption, no integrity protection and no notion of a read-only peer:
  anything that can reach the configured port can read and write any device the CPU exposes,
  outputs included, and can issue Remote RUN/STOP/PAUSE on a CPU whose parameters allow it.
  That is the protocol as published and **no client can fix it**, so the file says what a
  network and a CPU can do about it instead, explicitly as measures taken there and not as
  features here. It also records that the `0x1630`/`0x1631` remote password travels as literal
  characters in both codings, and that the first seven of them land inside the 24-byte hexdump
  an `aslmp` diagnostic prints -- verified against this package's own encoder, not against a
  CPU. The second half is how to report a vulnerability in `aslmp`, with no response-time
  commitment, because there is no security team to promise one.
- **A safety notice at the top of `README.md`**, above the tagline and the first example. This
  library writes to industrial control equipment and can halt a CPU; it is not a safety system,
  carries no SIL or PL rating, no certification of any kind, and has never been assessed by a
  functional-safety body. Interlocks belong in the PLC program and in hardware. A `Safety`
  section says the same thing in code terms, and a `Trademarks` section states the independence
  that `NOTICE` already stated.
- **`NOTICE` corrected.** SLMP and CC-Link were attributed to Mitsubishi Electric; they are the
  CC-Link Partner Association's. MELSOFT and the FX5 variants were added.
- **`allow_remote_control` is a software gate, not a safety interlock**, and both places that
  could be read the other way now say so. `OverrunPolicy.RAISE` no longer offers "a watchdog
  feed" as an example of what it is for -- a deadline checked on this side of the network is
  not a watchdog, and the README had already said the library has none.
- **A passage about Mitsubishi's documented Remote RUN behaviour was rewritten.** Calling a
  documented success code a "lie" of the vendor's tool was rhetoric standing where a
  description belongs; `docs/hardware.md` §15 and `docs/unverified.md` now name the behaviour
  and leave the word for the thing this library refuses to do to its own callers.
- **`tests/unit/test_citations.py::all_sources` walks `*.md` at the repository root** rather
  than a list of two documents, so `SECURITY.md`, `CONTRIBUTING.md` and anything added beside
  them are held to the same rules as `README.md` from the day they appear. A list of files is a
  sibling waiting to happen, which is this suite's own recurring finding.
- **`.hypothesis/` is ignored.** Hypothesis writes its own `.gitignore`, so git hid the
  directory and `hatchling` did not: the sdist was shipping the constants cache, each file
  headed with an absolute local path. Also ignored: `.env`, `.tox/`, `.nox/`, `coverage.xml`,
  `*.pcap` and the usual editor and OS droppings.
- **Repository scaffolding**: `CONTRIBUTING.md` (safety first, the three checks, the evidence
  rule, and a walkthrough for converting an unverified row), `CODE_OF_CONDUCT.md` (Contributor
  Covenant 2.1), a CI workflow over 3.11/3.12/3.13 x Linux/Windows/macOS that refuses to run
  with `ASLMP_TEST_HOST` set, a release workflow that publishes by OIDC and checks the tag
  against `src/aslmp/_version.py`, two issue forms whose required fields are `Measurement`'s,
  and a pull-request template with a provenance section.

### Known limitations shipped knowingly

Listed in full in [`docs/unverified.md`](docs/unverified.md) and in `aslmp capabilities
--unverified-only`. In short: our iron is **one** FX5U-32MT/DS on firmware 1.065, binary 3E/4E,
over TCP and UDP, from two hosts on two links. The entire ASCII path, the iQ-R / Q / L profiles,
Remote Reset and Latch Clear, `0x0406`/`0x1406` block access, the remote password commands and
`0x2101` receipt are all implemented, gated and **labelled unverified**. Remote RUN, STOP and
PAUSE are measured; the switch-in-STOP behaviour that justifies `verify=True` is not.

`bench/transports.py` and `bench/access_patterns.py` have published no measured numbers: they are
exercised against the simulator and have not been run against the FX5U. The Wi-Fi table above
comes from `tests/hardware/test_fx5u.py`, which carries its own same-session raw-socket control;
the wired transport comparison carries a control before and after, and their 0.01 ms drift is
published beside it. Treat the wired soak's absolute milliseconds as the weakest row in it — its
counters, its internal p50 drift and its per-cycle arithmetic assertion are what it is evidence
for, and `bench/soak.py` brackets every future run with the standard control.
