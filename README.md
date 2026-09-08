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
from that one CPU. Latency figures are labelled with their link, because **the link changed a
conclusion**: on Wi-Fi, UDP won the median and TCP won the tail, and that was the stated
reason TCP is the default. It did not reproduce on wire, where UDP wins at every percentile.
The default did not change; its justification did. See [`docs/hardware.md`](docs/hardware.md).

**We have no iQ-R, no Q, no L, and no ASCII connection.** Those paths are implemented, they
are gated, and every one of them ships **labelled unverified**: in the profile as
`Evidence(provenance=MANUAL)`, in the docstring, in `aslmp capabilities`, and in
[`docs/unverified.md`](docs/unverified.md). The label is honest. It is not protection.

**Remote control is partly verified.** RUN, STOP and PAUSE have been driven against the real
CPU and checked against its own free-running scan counter, not just against `SD203`. **Remote
RESET has never been sent** and stays unverified. So does the behaviour that makes
`verify=True` the default — Mitsubishi documents Remote RUN as returning end code `0x0000`
with the switch in STOP while the CPU does not run, and we could not force that condition on
a bench whose switch is in RUN. It remains a manual claim, and the library treats it as true.

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

```
pip install aslmp
```

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
- Configure one entry per concurrent consumer. Our bench has five: TCP 5000, 5002, 5003, 5004
  and UDP 5001.
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
| `D8` | free-running counter (61.6 µs per count) |
| `D100`–`D119`, `M100`–`M119` | scratch |

A free-running counter read *inside the same transaction as your data* gives every transaction
an independent PLC-side timestamp, which separates host scheduling jitter from PLC jitter.
`Plc(plc_clock=...)` is shaped for exactly that.

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

### TCP wins the tail; UDP wins the median. Default to TCP.

300 sequential 2-word reads each, same minute, same host:

| | n | min | p50 | p90 | p99 | max | stdev |
| --- | --- | --- | --- | --- | --- | --- | --- |
| UDP | 300 | 3.99 | **6.20** | 7.99 | 13.80 | 24.36 | 1.79 |
| TCP | 300 | 4.35 | 7.41 | 8.88 | **10.49** | **14.32** | **1.03** |

UDP saves about 1.2 ms at the median because there is no ACK round trip, and pays for it in the
tail — on this link a lost datagram costs a full client timeout, not a fast retransmit. **A
control loop is a jitter problem**, so `TransportKind.TCP` is the default. Do not read the p50
and switch.

### UDP pipelines cleanly to 32 deep, then loses requests with no error at all

| burst | responses | rate | lost |
| --- | --- | --- | --- |
| 8 | 8/8 | 304 txn/s | 0 |
| 32 | 32/32 | 363 txn/s | 0 |
| 64 | 44/64 | 20 txn/s | **20** |

No end code, no ICMP, nothing. A lost request is a serial that never comes back. `aslmp` raises
`SlmpDatagramLostError` naming the serial and the in-flight depth — never a retry, never a
generic timeout.

**Pipelining requires 4E, and 3E/UDP pipelining is not offered at all.** Without serials,
positional matching plus real loss gives silently mismatched replies: the same bug class as TCP
coalescing, one layer up.

It is opt-in and off by default — `Plc(..., transport=UDP, frame=FOUR_E, udp_pipeline_depth=16)`.
Measured through the client on 2026-09-07: 16 pipelined 4E reads in 48 ms (331 txn/s) against
128 ms (125 txn/s) for the same 16 reads one at a time on TCP, with nothing lost and every reply
matched to its own serial. `udp_pipeline_depth` above 1 is refused on 3E, and it is **refused
rather than ignored on TCP**, where it could not take effect.

### `socket.connect()` lies, so connecting runs a handshake

`connect()` performs a `0x0619` Self Test with a per-generation nonce and compares the echo
**byte for byte**. One ~7 ms zero-side-effect round trip proves, simultaneously: the entry was
free, the data code matches, the frame type is accepted, the route bytes are right, the protocol
is right, and the CPU is answering now. `Handshake.NONE` exists for someone who has measured
that they cannot afford 7 ms at startup, and it is documented as trading a truthful connect for
it.

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

Measured on the bench, 2026-09-07: **7.75 ms for the bound block read against 38.24 ms** for the
same five values as five separate batch reads — 4.9x, and one sample instead of five.

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

**This repository publishes no measured latency table of its own from `bench/`.** The scripts
are written and exercised against the simulator; nobody has run them against the FX5U yet. When
somebody does, the numbers go in that document with the control rows attached, or they do not go
in at all.

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
  +0.24 ms at p50, which we do not beat.
- **Apache PLC4X** — `ParserSerializerTestsuite.xml` seeded our golden byte-vector corpus.
  Apache-2.0, a straight licence match, attributed in `NOTICE`.
- **pymcprotocol**, **pymelsec**, **PySLMPClient**, **slmp-rs**, **libslmp2**, **Esmool**,
  **ProtoForge** — each contributed at least one regression test.

---

## Development

```
python -m pytest                       # ~3900 tests, no hardware needed
python -m ruff check src tests bench
python -m mypy
aslmp serve                            # a PLC-shaped socket to point things at
```

Hardware tests live in `tests/hardware/`, are marked `hardware`, are gated on `ASLMP_TEST_HOST`
and never run in CI:

```
ASLMP_TEST_HOST=192.168.10.250 python -m pytest tests/hardware -s
```

`tests/hardware/test_fx5u.py` is 23 tests against a real FX5U-32MT/DS: the handshake, the
low-word-first float decode against the running controller, a bound block read measured against
five separate batch reads, scratch writes including a register above `0x7FFF`, bit access,
4E/UDP pipelining at depth 8 and 16, the three named error classes (`0xC056`, `0xC052`, the
pre-transport monitor refusal), and a same-session raw-socket latency control. It writes only to
`D100`-`D119` and `M100`-`M119`, restores both, and asserts by walking its own AST that no
remote-control command can be reached from it. Every measurement it takes is printed under
`-s` and the last run is tabulated in [`CHANGELOG.md`](CHANGELOG.md).

Architecture and the layering rules that hold it together: [`docs/architecture.md`](docs/architecture.md).

## Licence

Apache-2.0. See `LICENSE` and `NOTICE`.
