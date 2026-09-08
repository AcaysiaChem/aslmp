# aslmp

An async SLMP client for Mitsubishi MELSEC PLCs. Zero runtime dependencies, `mypy --strict`
clean, and built from what one real CPU actually does rather than from what its manual says
it does.

```python
import asyncio
import aslmp

async def main() -> None:
    async with aslmp.Plc("192.168.10.250", 5002, profile="melsec:iq-f/fx5u") as plc:
        setpoint = await plc.read_f32("D0")      # a float, not a Reading[float].value
        print(setpoint, "in", (await plc.timed.read_f32("D0")).tx.timing.wire_ms, "ms")

asyncio.run(main())
```

There is a synchronous facade (`aslmp.sync.Plc`) with the same method names and one background
event loop for its lifetime, and a command line with eleven subcommands behind one entry point.

---

## Read this before you trust a number in here

**Our iron is one PLC.** A MELSEC iQ-F **FX5U-32MT/DS on firmware 1.065**, binary 3E and 4E,
over TCP and UDP, in September 2026. Everything this library claims about real silicon comes
from that one CPU, reached from two hosts: a laptop over **Wi-Fi** (~7 ms RTT) and a wired
machine (3.64 ms RTT). Latency figures are labelled with their link, because **the link changed
a conclusion**: on Wi-Fi, UDP won the median and TCP won the tail, and that tail was the stated
reason TCP is the default. It did not reproduce on wire, where UDP wins at every percentile.
The default did not change; its justification did — a UDP entry on iQ-F is point-to-point, so
it exists only for hosts somebody configured it for. See [`docs/hardware.md`](docs/hardware.md)
section 5 for both tables side by side.

**Seven of the eight shipped profiles have never been connected to.** Not just the ones you
would expect: `melsec:iq-r`, `melsec:iq-r/r00`, `melsec:q` and `melsec:l`, but also
`melsec:iq-f/fx5uc`, `melsec:iq-f/fx5uj` and `melsec:iq-f/fx5s` — three **iQ-F** profiles that
a reader may reasonably assume the bench measurements carry over to, and they do not: a
measurement names one piece of silicon. `melsec:iq-f/fx5u` is the whole of our evidence. There
is no ASCII connection either. All of those paths are implemented, gated, and ship **labelled
unverified**: in the profile as `Evidence(provenance=MANUAL)`, in the docstring, in `aslmp
capabilities`, and in [`docs/unverified.md`](docs/unverified.md), which lists every one of the
seven. The label is honest. It is not protection.

**Remote control is partly verified.** RUN, STOP and PAUSE have been driven against the real
CPU and checked against its own free-running scan counter, not just against `SD203`. **Remote
RESET and Latch Clear have never been sent** and stay unverified. So does the behaviour that
makes `verify=True` the default — Mitsubishi documents Remote RUN as returning end code
`0x0000` with the switch in STOP while the CPU does not run, and we could not force that
condition on a bench whose switch is in RUN. It remains a manual claim, and the library treats
it as true. Doing the measurement also found a bug of ours: `verify=True` read `SD203` once,
immediately, and **entering RUN is asynchronous**, so it raised for a RUN the CPU had accepted.
Fixed by observing to a deadline; the command is still sent exactly once.

**The simulator is not evidence.** `aslmp.testing` reproduces our measurements and gives the
unverified paths CI coverage — but it was written from the same manuals as the client and
shares the client's codec, so a misread section passes on both sides. The golden byte vectors
(Mitsubishi's own printed hex, plus Apache PLC4X's independently derived corpus) are the only
external oracle we have, and only for the paths the manuals worked through in hex.

**Nothing here beats a raw socket on latency and nothing can.** The whole argument for this
library is semantics, observability and correctness. If you need the last 0.2 ms, write the
socket code yourself; this README will tell you how.

---

## Install

**There is no `aslmp` on PyPI yet**, and `pip install aslmp` will not get you this package —
it is pre-1.0 and nothing has been released. Until the first release, install from a checkout:

```
git clone <this repository> && cd aslmp
pip install .            # or: pip install -e .[dev]  for the test suite
```

`pip install git+<url>` works too. The first release will make the one-line form true; this
section says what is true now rather than what is intended, because the install command is the
first thing a cold reader runs and a package that does not exist is a poor introduction to a
library whose argument is that it does not tell you things that are not so.

Python 3.11+. No runtime dependencies — deliberately, and load-bearing: the wheel has to
install on a Jetson's aarch64 and on a locked-down plant PC with no compiler and no proxy to
PyPI's transitive graph. The conformance simulator ships in the same wheel and also has no
dependencies.

---

## The PLC side: GX Works3 setup for an iQ-F

`fa-yoshinobu`'s **plc-comm-slmp** already ships per-model setup guides, and they are good.
This one earns its place by being specific to what we actually hit, in the order we hit it,
with the failure mode named for each mistake. Every item below cost us time.

### 1. Navigation

`Navigation → Parameter → FX5UCPU → Module Parameter → Ethernet Port`.

Two panes matter and they are at different levels:

- **Own Node Settings** — IP address, subnet, and **Communication Data Code**. Port-wide.
- **External Device Configuration** — the connection entries. Open it with the **`<Detailed
  Setting>`** button on the `External Device Configuration` row. It launches a separate
  drag-and-drop editor window; nothing about that row suggests a whole second application.

In that editor, drag **`SLMP Connection Module`** from the *Module List* on the right onto the
grid. Each row you drop is one **connection entry**. Set `Protocol` (TCP or UDP), the PLC-side
`Port No.`, and for UDP the destination IP. Then `Close with Reflecting the Setting` — closing
the window any other way discards it.

### 2. Communication Data Code is port-wide, so binary and ASCII cannot coexist

`Communication Data Code` (Binary / ASCII) is an **Own Node Setting** for the entire Ethernet
port, not a per-entry setting. On an iQ-F you cannot have a binary entry and an ASCII entry at
the same time: switching to ASCII breaks every binary connection on the CPU at once.

This is why the ASCII path in this library is unverified. Testing it means taking the bench's
four working binary entries down.

**Failure mode if you get it wrong: silence.** Sending binary into an ASCII port (or the
reverse) produces end code `0xC06F` internally and **no response at all** on the wire. Not an
error, not a reset — nothing, until your client's deadline expires. `aslmp` ranks
`CODING_MISMATCH` first in `SlmpTimeoutError.likely_causes` when zero bytes arrive on the first
transaction of a connection, precisely because this is the most common cause and looks exactly
like a dead PLC.

### 3. A UDP entry demands a destination IP address

GX Works3 refuses to save a UDP SLMP entry without one:

> Sensor/Device of No. 2 IP address is not set… it must be specified closer to IP address of
> sensor/device

There is no accept-from-any UDP SLMP on an iQ-F. **Every UDP peer needs its own entry**, out of
a maximum of 8 entries shared across all SLMP, MELSOFT, socket and predefined-protocol
connections. If you move your client to a different machine, you edit the PLC parameters.

### 4. One TCP connection is served per entry

A second TCP connection to an entry that is already in use **completes its three-way handshake**
(5.4 ms, measured) and is then immediately closed by the CPU — the incumbent connection is
undisturbed. `socket.connect()` returns success and the connection is already dead.

Consequences you have to design around:

- **Connection pooling against one entry is worthless.** One entry, one client.
- Configure one entry per concurrent consumer. Our bench has six: TCP 5000, 5002, 5003, 5004,
  UDP 5001 (bound to the laptop) and UDP 5005 (bound to `argus-bench`). Two UDP entries for two
  hosts, because that is what point-to-point means — see 3 above.
- `aslmp` classifies this precisely: a non-blocking EOF check straight after connect, and a
  zero-byte read on the first transaction of a connection, both raise
  `SlmpConnectionEntryBusyError` rather than a generic timeout.

**A second client is not the only way to get that error, and often not the likeliest one.** An
entry your own client just closed is not instantly available to your next `connect()`: measured
2026-09-07 on a wired link at 3.64 ms median RTT, a reconnect after a clean `close()` succeeded
1/6 at a 0 ms gap and 6/6 from 2 ms out. Over Wi-Fi at ~7 ms RTT it never failed at all, so the
gap you need depends on your link and a **faster** link should need more, not less. If you see
this error with nothing else connected to the CPU, do not go hunting for a second client — settle
a few milliseconds before retaking an entry you just released. Nothing in the library waits or
retries on your behalf; the numbers, the conditions and what they do not prove are in
[`docs/hardware.md`](docs/hardware.md) section 2.1.

UDP has no such limit: the entry is bound to a peer *address*, not to a socket, and two UDP
sockets from different source ports were served concurrently.

### 5. New Ethernet parameters need a physical power cycle

Write the parameters, then **power-cycle the PLC**. GX Works3 says so explicitly after the
write, because **Remote Reset is disabled by default** in the CPU parameters and it cannot
restart the CPU for you.

Do not enable Remote Reset because a tool asked you to. On our own bench, a memory-card error
once left this CPU refusing a remote RUN and needing a physical power cycle anyway.

### 6. The register map on our bench, for reference

All `f32`, low word first — which is what a GX Works3 `EMOV` writes and what we proved four ways
on the wire.

| Device | Meaning |
| --- | --- |
| `D0` | setpoint |
| `D2` | process value |
| `D4` | manipulated variable |
| `D6` | error |
| `D8` | free-running scan counter — a `REAL` the PLC program advances by `1.0` every scan, so **982 µs per count** |
| `D100`–`D119`, `M100`–`M119` | scratch |

A free-running counter read *inside the same transaction as your data* gives every transaction
an independent PLC-side timestamp, which separates host scheduling jitter from PLC jitter.
`Plc(plc_clock=...)` is shaped for exactly that.

**That row said "61.6 µs per count" until 2026-09-07, and the number was not a scan period.**
It was `D8` read as a `U32` — the same misread the block example below is written about — put
through arithmetic. Read as the `f32` it is: **20,374 counts in 20.014 s = 1018.0 scans/s,
982.3 µs per scan**, FX5U-32MT/DS fw 1.065 at 192.168.10.250, 2026-09-07, from the laptop at
192.168.10.41 over Wi-Fi, agreeing with 1 s, 5 s and 10 s windows to 0.4 %. A scan rate is one
of the few numbers here a link cannot move much — it is a count divided by a wall-clock window
of seconds, so a few ms of RTT at each end is 0.03 % of a 20 s window — which is exactly why
the old figure could not be blamed on the medium.

The old number was **15.9x too small**, and the factor is not a coincidence. At the ~613,775
the counter stood at, one `+1.0` in the REAL moves the IEEE-754 bit pattern by 16, so a `U32`
reader counts sixteen times too fast and reports a period sixteen times too short. This is what
a wrong declared type looks like *after* it has been through arithmetic: not a broken number, a
plausible one in the right units, in a table. The bit-pattern reading was found and fixed in
the code a day earlier; this figure is what it left behind, and finding it here is why the pass
that corrected it went looking for *every* number derived from a `D8` rate instead of only the
one that had been reported. `docs/hardware.md` section 17 carries the measurement, the
arithmetic, and the list of everything re-derived from it — including what was deliberately
left alone, and where.

### 7. Check it before you write any code

```
aslmp identify 192.168.10.250 --port 5002
aslmp probe    192.168.10.250 --port 5002 --profile melsec:iq-f/fx5u
```

`identify` needs no profile: `0x0619` and `0x0101` carry no device address, so the profile
cannot change a byte of them. `probe` proves the entry is free, the data code matches, the
frame type is accepted and the CPU is answering *now* — in one ~7 ms round trip with no side
effects.

---

## What we measured, and what it does to your code

Each of these changed the design. The full write-up with numbers is in
[`docs/hardware.md`](docs/hardware.md).

### TCP request coalescing corrupts silently

Two requests written before the first response is read return **one** response — for the
**last** request — with end code `0x0000`. On 3E there is no serial number, so it is
undetectable wrong data reported as success.

So there is **no public `send()` anywhere in this package.** Bytes reach a socket only through
a single-use capability token obtained from an async context manager, exclusive per connection.
`Concurrency.STRICT` (the default) makes a naive `asyncio.gather()` of two reads on one `Plc`
*raise* rather than corrupt. That will be reported as our bug, repeatedly. It is the correct
lesson for this hardware.

**UDP does not have this failure.** Datagrams are framed; the same test returns both responses
correctly. The one-in-flight rule is a TCP rule, not a universal one.

### The transport comparison, and the conclusion the link overturned

This is the claim an outside reviewer challenged, and re-measuring showed the challenge was
right. Both tables are real. 300 sequential 2-word reads per transport in each.

**Wi-Fi, 2026-09-06**, laptop at 192.168.10.41, same minute, same host:

| | n | min | p50 | p90 | p99 | max | stdev |
| --- | --- | --- | --- | --- | --- | --- | --- |
| UDP | 300 | 3.99 | **6.20** | 7.99 | 13.80 | 24.36 | 1.79 |
| TCP | 300 | 4.35 | 7.41 | 8.88 | **10.49** | **14.32** | **1.03** |

**Wired, 2026-09-07**, `argus-bench` at 192.168.10.36, TCP/UDP interleaved so drift lands on
both, controls before and after:

| | n | min | p50 | p90 | p99 | max | sd |
| --- | --- | --- | --- | --- | --- | --- | --- |
| UDP | 300 | 1.92 | **2.42** | **3.40** | **3.57** | **3.87** | 0.40 |
| TCP | 300 | 2.39 | 3.63 | 4.05 | 4.69 | 5.08 | 0.36 |

The raw-socket controls that bracket that run drifted **0.01 ms at p50** across the whole of it,
so the error bar is far smaller than any difference in the table, and a repeat UDP pass landed
within 0.01 ms of the first.

On Wi-Fi, UDP won the median and TCP won the tail, and **that tail was the published reason TCP
is the default**. On wire UDP wins at every percentile, including the tail, at equal standard
deviation — and the control says the run was stable to 0.01 ms at p50. The old conclusion was a
property of the radio: there, a lost datagram costs a full client timeout while TCP fast
retransmits, and enough datagrams are lost on a radio for that to own p99. Over 600 wired UDP
samples it never happened.

**The default did not change. Its justification did.** `TransportKind.TCP` is the default
because **a UDP SLMP entry on iQ-F is point-to-point** — GX Works3 will not save one without a
destination IP, there are at most eight entries on the CPU, and a TCP entry serves any peer —
and because loss is silent on UDP. It is a configurability argument, not a latency one. Where an
entry exists for your host and the link is wired, UDP is faster at every percentile and you
should take it explicitly.

### The UDP receive queue is a hard 32, and overflow is silent

Bursts of 4E reads fired without waiting, then drained. Wired, 2026-09-07:

| depth | answered | rate | lost |
| --- | --- | --- | --- |
| 8 | 8/8 | 334 txn/s | 0 |
| 16 | 16/16 | 371 txn/s | 0 |
| 32 | 32/32 | **405 txn/s** | 0 |
| 48 | **32**/48 | 10 txn/s | **16** |
| 64 | **32**/64 | 10 txn/s | **32** |

Exactly 32 answered at depth 48 and exactly 32 at depth 64: **a hard ceiling, not a soft
degradation.** No end code, no ICMP, nothing — a lost request is a serial that never comes back,
and the 10 txn/s rows are the client's own timeout expiring. `aslmp` raises
`SlmpDatagramLostError` naming the serial and the in-flight depth — never a retry, never a
generic timeout.

(The earlier Wi-Fi ladder read 44/64 and looked like ~31% loss at 64. On a slower link the CPU
drains part of the queue while the rest of the burst is still arriving, so more than 32 get
served. Design against 32.)

**Pipelining requires 4E, and 3E/UDP pipelining is not offered at all.** Without serials,
positional matching plus real loss gives silently mismatched replies: the same bug class as TCP
coalescing, one layer up.

It is opt-in and off by default — `Plc(..., transport=UDP, frame=FOUR_E, udp_pipeline_depth=16)`.
Measured through the client on 2026-09-07 **from the Wi-Fi laptop**: 16 pipelined 4E reads in
48 ms (331 txn/s) against 128 ms (125 txn/s) for the same 16 reads one at a time on TCP, with
nothing lost and every reply matched to its own serial. Expect a smaller multiplier on a faster
link — on wire, depth 32 buys about 1.5x over serial TCP rather than 2.7x, because serial TCP
gets most of the benefit. `udp_pipeline_depth` above 1 is refused on 3E, and it is **refused
rather than ignored on TCP**, where it could not take effect.

### `socket.connect()` lies, so connecting runs a handshake

`connect()` performs a `0x0619` Self Test with a per-generation nonce and compares the echo
**byte for byte**. One zero-side-effect round trip — ~7 ms over Wi-Fi, ~3.6 ms on wire — proves,
simultaneously: the entry was free, the data code matches, the frame type is accepted, the route
bytes are right, the protocol is right, and the CPU is answering now. `Handshake.NONE` exists
for someone who has measured that they cannot afford one round trip at startup, and it is
documented as trading a truthful connect for it.

### Wrong encoding, wrong transport, wrong frame type and an overstated length all fail by silence

All four produce no response whatsoever. `SlmpTimeoutError.likely_causes` is *computed from
context* rather than enumerated: zero bytes on a connection's first transaction ranks
`CODING_MISMATCH` first; partial bytes rank `REQUEST_LENGTH_OVERSTATED` first, because an
overstated `L` makes the CPU block for bytes that never come and looks exactly like a dead PLC.

Understating `L` returns `0xC061` and the connection recovers. The asymmetry is why `L` has
exactly one expression in this package and three separate tests guarding it.

### X and Y are octal on iQ-F, and the PLC will not tell you when you get it wrong

Measured 2026-09-07 by setting one bit at a chosen wire number and reading back which linear
output moved:

| GX Works3 | meaning | wire device number |
| --- | --- | --- |
| `Y0` | 1st output | 0 |
| `Y7` | 8th output | 7 |
| `Y10` | 9th output | **8** |
| `Y20` | 17th output | **16** (0x10) |

A library that sends the digits as written puts `Y10` on the wire as 10 and lands on the 11th
output, with end code `0x0000` and no error anywhere. The error grows with the address.

The CPU also **accepted a write to `Y8`**, which does not exist under octal notation, and
answered `0x0000`. Rejecting the digits 8 and 9 in an X/Y literal is the client's job and
nothing else will do it.

This is also why `profile=` is a **required** argument with no generic fallback: `Y20` is output
16 on an FX5U and output 32 on an iQ-R, and both CPUs answer `0x0000`.

### Five minutes of a real control loop, and the PLC's own arithmetic closing through us

The bench PLC runs a proportional-only bath controller with a **12 %/K** band and no physical
I/O. `bench/soak.py` closes that loop from the host: one `0x0403` per cycle reads the whole
record (SP, PV, MV, Err, scan) as one snapshot, a first-order bath model is integrated using
**the PLC's own heater duty** as its input, and the new process value is written back to `D2`.

2026-09-07, from `argus-bench` over the wired link, TCP 5002: **15,000 cycles, 30,005
transactions, 300.0 s at exactly 50.0 Hz, 0 errors, 0 reconnects, 0 cadence overruns**, block
read p50 3.67 / p99 4.69 ms with p50 moving 3.72 → 3.67 ms between the first fifth of the run
and the last. The CPU scanned at **969/s under that load against its own idle reference of
1029/s in the same session** — two transactions per cycle at 50 Hz cost it **about 5–6 % of its
scan rate**. The range is not hedging: 1029/s is that session's reference and the repository's
standing idle figure is 1018/s (§17 of `docs/hardware.md`), the two disagree by 1.1 %, and the
percentage is a ratio of two scan rates so it inherits both. **That table came from the
throwaway harness `bench/soak.py` was promoted from**, not from the shipped script, which is
why it carries no control rows; `docs/hardware.md` section 16 prints a shorter run of the
shipped script beside it, with the controls it now takes.

The number that matters is not in that paragraph. Because the band is 12 %/K, the CPU's own
values must satisfy

```
MV == clamp(Err * 12, 0, 100)
```

within every single snapshot, and the soak asserts it on **every cycle**. Read back in full `f32`
precision on 2026-09-07 from the Wi-Fi laptop: `Err` 0.4399986267089844 → `MV`
5.2799835205078125, and `Err` 3.477001190185547 → `MV` 41.72401428222656. Both are the error
times twelve to the last bit. The deviation was **exactly 0.0** in those, in a six-value sweep
across both clamps (`Err` 60.0 → `MV` 100.0 at the ceiling, `Err` −1.0 → `MV` 0.0 at the floor)
and in all 1,125 snapshots of a 45 s verification run.

A plausible-looking latency curve can be produced by a client that decodes garbage. **A PLC's
own gain arithmetic closing to the last bit of an `f32` — through the block plan, the
low-word-first decode, the `0x0403` and the `0x1401` — cannot.**

### Other measured facts

- Batch word limit **960**, batch bit limit **3584**, random-access points **192**, `D` ends at
  **D7999**. All refused pre-transport, with the end code the CPU would have returned quoted in
  the exception.
- **Zero points** is a point-count error (`0xC052`), not an address error.
- `0x0801` / `0x0802` Monitor Registration and Execute Monitor return **`0xC059`** on iQ-F. They
  are capability-gated and **never emulated with a `0x0403`** — a silent substitution is exactly
  what this library refuses to do.
- TCP segmentation is real and intermittent: 1 of 3 identical 1931-byte reads split at the
  1460-byte MSS on 2026-09-06, and 1 of 6 (as `9 + 1451 + 471`) through the library on
  2026-09-07. Reads are length-driven and the receive stamp is taken **after the last chunk**.
  A response arriving in two chunks is *not* segmentation — prefix-then-body is structural —
  so `segmented` counts reads that came back short, not chunks.
- `TCP_NODELAY` is not a latency fix here (p50 differs by 0.32 ms and the *minimum* is lower with
  Nagle on). It is set anyway because it costs nothing.
- **Remote RUN, STOP and PAUSE** were driven against the CPU on 2026-09-07 and checked against
  its free-running scan counter, not just `SD203`. Leaving RUN is effectively synchronous;
  **entering RUN is not** — `SD203` still said `STOP` on the first poll in 2 of 3 cycles and
  reached `RUN` 25–33 ms after the command. `verify=True` used to read `SD203` once, immediately,
  and so raised for a RUN the CPU had accepted; it now observes to a 250 ms deadline and returns
  the instant the state matches. The command is still sent exactly once — what repeats is the
  *reading*, and `RemoteResult.polls` says how many it took.
- **A reconnect within ~2 ms of your own `close()` can be refused** — 1/6 at a 0 ms gap, 6/6 from
  2 ms, wired; 30/30 at every gap including 0 ms over Wi-Fi. It is a race against the CPU's FIN
  processing rather than a hold period, so it is link-dependent and a *faster* link should need
  *more* gap. Details and what they do not prove: [`docs/hardware.md`](docs/hardware.md) 2.1.

---

## Blocks: one round trip for a whole control record

Five reads of a controller's registers are five round trips and five different moments. A block
declares the record once, `bind()` validates every span and prebuilds the `0x0403` frame at
startup, and each cycle costs one transaction and one snapshot:

```python
from typing import Annotated

from aslmp import F32, Plc, PlcBlock, plc_block

@plc_block(base="D0")
class LoopState(PlcBlock):
    setpoint:      F32     # D0/D1 — one double-word access point, low word first
    process_value: F32     # D2/D3
    output:        F32     # D4/D5
    error:         F32     # D6/D7
    scan: Annotated[float, F32(minimum=0.0, maximum=1.0e7)]   # D8/D9 — a REAL on this PLC

async with Plc("192.168.10.250", 5002, profile="melsec:iq-f/fx5u") as plc:
    plan = plc.bind(LoopState)          # synchronous; no I/O; fails at startup, not in the loop
    state = await plan.read()           # one 0x0403
    print(state.setpoint, state.tx.timing.wire_ms)
```

> **The declared type is a promise the wire cannot check.** D registers carry no type on
> the wire: sixteen bits are sixteen bits. If you declare `scan: U32` against a register
> the PLC writes as a `REAL`, the two registers decode to `1226168560` — a
> plausible-looking integer that is really a float's bit pattern. The end code is
> `0x0000`, because nothing failed.
>
> An earlier version of this example made exactly that mistake, and it is nastier than it
> looks: IEEE-754 bit patterns rise monotonically for positive floats, so a counter
> declared `U32` still *increases* every cycle and a naive "is it advancing?" check passes.
> Ours did. The only visible symptom is the *rate*, and even that does not hold still: a
> `+1.0` in the REAL moves the `U32` reading by one ulp-step, which is 16 at the 613775.0
> we measured and halves each time the counter crosses a power of two (8 above 2²⁰, 4
> above 2²¹). A wrong rate that drifts is harder to spot than a wrong rate that does not.
>
> Take the field types from the PLC program's own global labels, not from what the value
> looks like. In GX Works3 that is **Label → Global Label**, the `Data Type` column. On
> this rig all five are `FLOAT [Single Precision]`.

### Plausibility bounds: the promise you make, kept

`aslmp` will not guess a register's type and will not sniff whether a value *looks like* a
float — nothing on the wire could support either, and a detector that half-worked would be
worse than none. What it will do is hold a value to a range **you** declare:

```python
scan: Annotated[float, F32(minimum=0.0, maximum=1.0e7)]
```

Every numeric alias takes the same two keywords (`F32 F64 I32 U32 I16 U16 Word`), both are
optional, and either end alone is a whole declaration. A field with no bounds behaves
exactly as it always has: unbounded is the default, because this is a tool for people who
know their process ranges and not a ceremony every field has to perform.

The call goes in the **metadata position of an `Annotated`**, not in the default slot,
because that is the position a type checker does not read as a call: `state.scan` stays
exactly `float`, with no `cast` and no `# type: ignore` at any call site. A bound that
cannot mean anything — a `minimum` above its `maximum`, a NaN end, a bound the field's own
width cannot reach — is refused at class-definition time.

A value outside its declared range raises
[`SlmpImplausibleValueError`](src/aslmp/blocks/fields.py) rather than being returned. It is
a `SlmpSemanticError`, which in this library's error tree means precisely *the PLC said
`0x0000` and the answer is still not one you can use*, and it carries the field, the
bounds, the value, the address and the raw registers.

Declared `Annotated[int, U32(minimum=0, maximum=1_000_000)]`, the reviewer's field says
this and stops, instead of returning a counter that rises at the wrong rate:

```
scan read 1226168560 from D8, which is outside the declared range [0 .. 1000000]. The end
code was 0x0000 and the registers on the wire were 0xD8F0 0x4915, so nothing failed and
nothing was retried. A D register carries no type on the wire -- sixteen bits are sixteen
bits -- so nothing here can tell a wrong declaration from a wrong process value, and nothing
here guesses. The common cause is a declared type that disagrees with the PLC program's own
global label: an f32 read as U32 returns a large integer that is really the float's bit
pattern, and because IEEE-754 patterns rise monotonically for positive floats it even keeps
counting up. Check the type in GX Works3 under Label -> Global Label, in the Data Type
column, and declare what it says there. If the declaration is right and the plant really did
go there, the bound is what you asked for.
```

Bounds are checked on writes too, before a byte leaves the process: `plan.write(scan=-1.0)`
raises `SlmpValueRangeError` and sends nothing. Nothing is ever clamped to fit.
`plan.describe()` prints each field's range beside its type, since that report is the
artifact you hand a Mitsubishi engineer next to the `Global Label` view.

The same promise is available per call, without a block:

```python
level = await plc.read_f32("D20", minimum=0.0, maximum=100.0)
```

on `read_i16`, `read_u16`, `read_i32`, `read_u32`, `read_f32` and `read_f64`, on
`plc.timed`, and on the synchronous facade.

The static type of `state.setpoint` is exactly `float` — the `Annotated` aliases carry the width
in metadata, so there is no `cast` at the call site. `plc.read_block(plan)` and
`plc.write_block(plan, value)` take a **bound plan** and never a class: rebinding per cycle would
revalidate the frame per cycle, which is the whole cost `bind` is there to pay once. A block too
large for one transaction needs `bind(..., allow_split=True)` and then returns a `Split[B]`,
which is deliberately *not* a `B`, because its fields were not one snapshot.

Measured on the bench, 2026-09-07 from the Wi-Fi laptop, n=9 of each: **7.75 ms of wire time
for the bound block read against 38.24 ms of wall time** for the same five values as five
separate batch reads.

**Those two columns are not the same measurement, and the ratio between them is not 4.9x
worth of anything.** 7.75 ms is the block's `wire_ms`; 38.24 ms is a wall clock around five
reads, so it includes this client's own scheduling between them and the block's does not. The
like-for-like figure is the *sum of the five reads' wire times*, which the same test records as
`five_reads_wire_sum_p50_ms` and prints under `-s` — and which was not written down, so it is
not published here. What can be said without it: single `read_f32` wire p50 on that link and
day was 7.46 ms, so five of them are ~37.3 ms of wire, and the honest multiplier is **about
4.8x with a host gap of roughly 0.2 ms per read on top** — an arithmetic inference from two
published numbers, labelled as one. The next run of that test writes the recorded wire sum
down, and then this paragraph is a number instead of an argument.

The multiplier was never the point. Five reads are five moments; one `0x0403` is one snapshot,
and no latency column shows that.

---

## The command line

One console script, eleven subcommands, each imported lazily — `aslmp --help` does not import
`asyncio` or `socket`, and that is a test.

```
aslmp identify 192.168.10.250                        what CPU is that, and which profile?
aslmp probe    HOST --profile KEY                    prove the entry is live, and say what that proves
aslmp read     HOST D0 --as f32 --profile KEY        one typed read
aslmp write    HOST D100 1.25 --as f32 --verify      device memory only
aslmp cite     0x0403                                the manual sections behind a command
aslmp capabilities melsec:iq-f/fx5u                  what a profile allows, with evidence
aslmp ambiguities                                    where the sources disagree, and the probe
aslmp verify-ranges HOST --profile KEY               measure real device ranges
aslmp proxy    --target HOST:PORT                    forward unconditionally, decode a copy
aslmp bench    HOST --profile KEY                    distributions beside a raw-socket control
aslmp serve                                          run the conformance simulator
```

**No subcommand can issue Remote RUN, STOP, PAUSE, LATCH CLEAR or RESET.** They can stop a
running machine over an unauthenticated cleartext socket; the library gates them behind
`Plc(allow_remote_control=True)` and a shell history is not an interlock. A test asserts the CLI
has no path to them.

Full reference: [`docs/cli.md`](docs/cli.md).

---

## Benchmarks

`aslmp bench` **refuses to print a table without a same-session raw-socket control**, and the
scripts in `bench/` do the same. This is not ceremony: the same machine and the same PLC gave
p50 7.1 / p99 18.8 ms on one day and p50 10.3 / p99 95.2 ms on another, with nothing changed. A
published latency number with no control beside it is not a measurement.

The control shares no code with this library — hand-built 3E binary frames, a blocking socket,
`struct` — and it runs twice, before and after, so the drift between the two is the honest error
bar on everything in between. See [`docs/benchmarking.md`](docs/benchmarking.md).

**A control catches the day. It does not catch the medium** — which this project learned the
expensive way, by publishing a transport conclusion that was a property of its Wi-Fi link. Every
table in this repository now names its host, its link and its median RTT, and a number without
them is treated as unreproducible.

`bench/` has published exactly one table so far: the five-minute closed-loop soak above — and
strictly speaking `bench/soak.py` did not produce it, the throwaway harness it was promoted
from did, before the bracketing controls and the `--rate`/`--duration` arguments existed. A
rerun of the shipped script prints more rows and a different transaction total.
[`docs/hardware.md`](docs/hardware.md) section 16 prints one such rerun beside it. The
transport and access-pattern scripts are exercised against the simulator and have not been run
against the FX5U; when somebody runs them, the numbers go in with the control rows and the link
attached, or they do not go in at all. **`bench/soak.py` is the only script in this repository
that writes to a PLC** — `D2`, once a cycle, restored on the way out and with the recovery
command printed if the restore itself fails.

---

## Errors

Every exception renders as a diagnostic block rather than a sentence:

```
aslmp.SlmpUnsupportedCommandError: end code 0xC059 — "Error in command or subcommand
specification. There is a command or subcommand that cannot be used by the CPU module."
  target    FX5U-32MT/DS (model code 0x4A49) at 192.168.10.250:5000 — tcp / binary / 3E
  request   monitor_register(['D0','D4','D8'])  ->  0x0801 sub 0x0000, 20 bytes
  sent      50 00 00 FF FF 03 00 14 00 00 00 01 08 00 00 00 03 00 00 00 A8 ...
  received  D0 00 00 FF FF 03 00 0B 00 59 C0 00 FF FF 03 00 01 08 00 00
  routes    requested 00/FF/03FF/00   responded 00/FF/03FF/00
  timing    7.31 ms  (gen 0, seq 12, 1 chunk, queue 0.00 ms, first byte 7.10 ms)
  observed  0x0801 and 0x0802 both return 0xC059 on FX5U-32MT/DS fw 1.065, measured twice
  action    Monitor Register / Execute Monitor are iQ-R commands. On iQ-F use read_random().
  manual    JY997D56001-K §6 Troubleshooting; SH(NA)-080956ENG-M p.33
```

The tree has four top-level branches and one deliberate sibling:

- `SlmpUsageError` (also a `ValueError`) — **nothing was sent.**
- `SlmpTransportError` — the socket. No end code exists.
- `SlmpProtocolError` — bytes arrived and are not a valid response.
- `SlmpEndCodeError` — the PLC answered, in its own words.
- `SlmpSemanticError` — end code `0x0000`, and it still is not true.
- `SlmpOutcomeUnknownError` — a **state-changing** request failed *after* the bytes went out.
  It is a sibling of the whole tree, not a `SlmpTransportError`, because
  `except SlmpTransportError: retry()` is right for a read and a data-loss bug for a write.

**Nothing retries, clamps, substitutes a default or returns a stale value.** Reconnection is
never implicit and is always an observable event. A lint-level AST test enforces it over the
whole package. Details in [`docs/errors.md`](docs/errors.md).

---

## Security

**SLMP has no encryption and no authentication.** Anything that can reach the port can read and
write device memory, and — if Remote Reset is enabled — stop the CPU. The remote-password
commands (`0x1630` / `0x1631`) send the password as literal characters in the clear.

Treat an SLMP port exactly as you would an unauthenticated telnet port on a machine that moves
physical things:

- Put it on an isolated control network. Never route it to a business network, and never expose
  it to the internet.
- Prefer a firewall rule pinned to the client's address. On iQ-F a UDP entry already names one.
- Keep `allow_remote_control=False` (the default) unless you have decided otherwise on purpose.
- Leave Remote Reset disabled in the CPU parameters, which is where Mitsubishi leaves it.

Published vulnerabilities in the surrounding Mitsubishi Ethernet/SLMP surface, for context on
why the port is not a place to be relaxed: CVE-2020-5594, CVE-2020-16226, CVE-2023-4699,
CVE-2025-7405, CVE-2025-7731. This library does not defend against any of them; it is a client.

---

## Stability

`aslmp.__all__` is the contract. Two-minor-version deprecation notice; no removals in a minor
release. Anything not named there is private, including every module whose name starts with an
underscore.

`aslmp/__init__.py` resolves its public names through a module-level `__getattr__` (PEP 562), so
importing the package opens no socket, starts no thread and reads no file. Static type checkers
see ordinary imports, and there is a subprocess test for the runtime half.

Version: see `CHANGELOG.md`. Pre-1.0 and honest about it.

---

## Prior art

We read these carefully and this library is better for them. A number of regression tests
exist only because somebody else found the bug first, and say so in their docstring.

- **fa-yoshinobu / plc-comm-slmp** — the closest thing to a reference implementation in Python,
  with per-model setup guides that predate ours. Its measured overhead over a raw socket is
  +0.24 ms at p50, and **we have no measurement fine enough to compare with it**: our own
  library-against-control run was n=120 on a Wi-Fi link with sd ~1.03 ms, where the error bar
  on a difference of medians is around 0.17 ms. We published +0.07 ms from it once and have
  withdrawn that; what the run supports is "indistinguishable at this n".
- **Apache PLC4X** — `ParserSerializerTestsuite.xml` seeded our golden byte-vector corpus.
  Apache-2.0, a straight licence match, attributed in `NOTICE`.
- **pymcprotocol**, **pymelsec**, **PySLMPClient**, **slmp-rs**, **libslmp2**, **Esmool**,
  **ProtoForge** — each contributed at least one regression test.

---

## Development

```
python -m pytest                       # no hardware needed; it prints its own count
python -m ruff check src tests tools bench
python -m mypy
aslmp serve                            # a PLC-shaped socket to point things at
```

The suite is a few thousand tests and none of them need a PLC. **The count is not written down
here on purpose**: it moved by tens between the last two edits of this file and a number in
prose goes stale the moment somebody adds a test, which is a small dishonesty of exactly the
kind this README is otherwise careful about. `python -m pytest -q` ends with the real one.

Hardware tests live in `tests/hardware/`, are marked `hardware`, are gated on `ASLMP_TEST_HOST`
and never run in CI:

```
ASLMP_TEST_HOST=192.168.10.250 python -m pytest tests/hardware -s
```

`tests/hardware/test_fx5u.py` collects 25 tests against a real FX5U-32MT/DS -- 24 test
functions, one of which is parametrized twice, and one of the 25 runs only from the wired UDP
entry's configured peer -- covering the
handshake, the
low-word-first float decode against the running controller, a bound block read measured against
five separate batch reads, scratch writes including a register above `0x7FFF`, bit access,
4E/UDP pipelining at depth 8 and 16, the three named error classes (`0xC056`, `0xC052`, the
pre-transport monitor refusal), and a same-session raw-socket latency control. It writes only to
`D100`-`D119` and `M100`-`M119`, restores both, and asserts by walking its own AST that no
remote-control command can be reached from it. Every measurement it takes is printed under
`-s` and the last run is tabulated in [`CHANGELOG.md`](CHANGELOG.md).

`tests/hardware/test_remote_control.py` is 7 tests and the deliberate exception, in its own file
so that the main suite's ban stays absolute and mechanically checked. It drives Remote RUN, STOP
and PAUSE on a separate connection entry, verifies against the scan counter the PLC's own program
increments rather than against `SD203`, returns the CPU to RUN in a `finally` and proves it, and
asserts by its own AST that Latch Clear and Reset cannot be reached from it. **It stops your
CPU**, and a Remote STOP on this CPU clears non-latched device memory, so anything parked in
D-memory is gone -- including the scan counter, which restarts from zero. Read it before you run
it.

Architecture and the layering rules that hold it together: [`docs/architecture.md`](docs/architecture.md).

## Licence

Apache-2.0. See `LICENSE` and `NOTICE`.
