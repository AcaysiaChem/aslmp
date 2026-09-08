# What one FX5U actually does

Every measurement below is from a **MELSEC iQ-F FX5U-32MT/DS, firmware 1.065**, at
192.168.10.250, in RUN with no physical I/O wired — ~1029 scans/s idle. Dates are 2026-09-06
and 2026-09-07.

**Every table names its link.** Two hosts on the same /24:

| host | link | median RTT | what it measured |
| --- | --- | --- | --- |
| a Windows 11 laptop, 192.168.10.41 | **Wi-Fi** | ~7 ms | all of 2026-09-06, the hardware test suite, remote control (§15), the arithmetic and the shipped-script soak (§16) |
| `argus-bench`, 192.168.10.36 | **wired** | 3.64 ms | the transport retest (§5), the queue ladder (§6), the entry-release window (§2.1), the five-minute soak (§16) |

**Anything below that names neither a host nor a date is from the first row** — the Wi-Fi
laptop, 2026-09-06. Where a specific figure's link was not written down at the time, it says so
in place rather than inheriting a label it did not earn; §15's original three-cycle table is the
one case.

Six SLMP connection entries, all verified: TCP 5000 (in use by other tooling), TCP 5002/5003/
5004, UDP 5001 (peer-bound to the laptop) and UDP 5005 (peer-bound to `argus-bench`, added
2026-09-07 for the wired retest).

The link is not a footnote here. **It overturned a published conclusion** — section 5 — so a
latency number without its link is not a measurement in this repository.

Where a Mitsubishi manual and this CPU disagree, the code implements the CPU and cites the
measurement. Where they disagree and we could not test, the code implements the manual and ships
the row labelled — see [`unverified.md`](unverified.md).

This is n=1. One CPU, one firmware, two links, two afternoons. A firmware update could invalidate
any of it, and **nothing in the design tells us when a profile has gone stale**. The second link
is new as of 2026-09-07 and it cost us a published claim on its first day, so treat anything here
that only one link has seen as provisional. `aslmp
verify-ranges` is how you check the device ranges on yours, and `aslmp.testing.run_conformance`
is how you check the end codes -- point it at a live endpoint with your own twelve lines of
socket code, since its `Exchange` protocol is structural.

---

## 1. TCP request coalescing returns the wrong answer and calls it success

Two requests written to the socket before reading the first response:

```
write  read D0-D1
write  read D8-D9
read   -> ONE response, end code 0x0000, carrying D8-D9
```

One response, for the **last** request, reported as a normal completion. On 3E there is no
serial number, so the client has no way to notice. This is silent data corruption produced by
the most natural performance optimisation there is.

**What the library does:** there is no public `send()`. Bytes reach a socket only through a
single-use capability token, exclusive per connection. `Concurrency.STRICT` — the default —
raises `SlmpConcurrentTransactionError` rather than allowing a second request in flight.

**UDP does not do this.** The identical test over UDP returns both responses, correct and in
order, because datagrams are framed. The one-in-flight rule is a TCP rule.

## 2. One TCP connection per configured entry

A second TCP connection to a busy entry completed its three-way handshake in 5.4 ms and was then
closed by the CPU. The incumbent was undisturbed. `socket.connect()` returned success on a
connection that was already dead.

**What the library does:** a non-blocking EOF check immediately after connect (zero wait, zero
bytes on the wire — it only classifies a FIN that has *already* arrived), plus a zero-byte read
on the first read of a generation, both raising `SlmpConnectionEntryBusyError`. Connection
pooling against one entry is worthless and is not offered.

UDP has no such limit: the entry binds to a peer address, not a socket, and two UDP sockets from
different source ports were served concurrently.

### 2.1 A reconnect within about 2 ms of your own `close()` can be refused

Measured 2026-09-07 from **argus-bench, wired**, same /24, **median RTT 3.64 ms**, six trials per
gap. A clean `close()`, then a reconnect to the same entry after the gap:

| gap after a clean `close()` | reconnects that worked |
| --- | --- |
| 0 ms | 1/6 |
| 1 ms | 2/6 |
| 2 ms | **6/6** |
| 5 ms and above, tested to 200 ms | 6/6 |

From **a different host** (the laptop) over **Wi-Fi**, median RTT ~7 ms, the same test succeeded
**30/30 at every gap including 0 ms**. The window is invisible there.

**So it does not behave like a fixed hold period.** What has to elapse tracks the *link* rather
than the clock: the slower link, which spends more time simply carrying the new SYN, never fails.
The reading that fits is a race against the CPU's own connection teardown — its FIN processing —
which a new SYN can arrive ahead of. **That is an inference from these timings and nothing more.**
Nothing here can see inside the CPU, and a scan-cycle boundary or a connection-table sweep would
produce the same table. What follows does not depend on which of those it is:

- **The window is link-dependent.** Whatever the CPU is finishing, the network latency in front of
  the new SYN is time the CPU has already been given. That is why the failure reproduces on wire
  and not on Wi-Fi, and it means a link *faster* than this one should need *more* client-side gap,
  not less. **That direction is a prediction, not a measurement** — the fastest link we have is
  the 3.64 ms one in the table, and `A-ENTRY-RELEASE-RACE`'s probe exists to go and test it on a
  gigabit switch at sub-1 ms RTT.
- **2 ms is a measurement, not a guarantee, and not a spec value.** One CPU, one firmware, one
  link, one host, one day, six trials a gap — and at 1 ms this CPU succeeded twice out of six, so
  a single passing trial proves nothing about a gap. Do not design a timeout around this number.
- **The two rows also differ by host, not only by link.** argus-bench wired against the laptop on
  Wi-Fi changes two variables at once. Latency is the explanation that fits both rows and it is
  the one we act on, but a same-host wired-and-wireless pair is the experiment that would settle
  it, and it has not been run.

**Teardown shape: not distinguished, on the link where it could not have been.** `close()`,
`shutdown(RDWR)` then `close()`, and an abrupt RST via `SO_LINGER` 0 all reconnected cleanly and
immediately — but that comparison was run **over Wi-Fi**, where the paragraph above says a clean
`close()` at a 0 ms gap already succeeds 30/30. On that link nothing could have failed, so the
result rules nothing out; it is recorded here so the next person repeats it on wire rather than
trusting it. The one teardown that did fail intermittently was a socket **dropped without
`close()`** and left to the garbage collector — the FIN goes out whenever the collector gets to
it, which is a different bug with the same symptom.

**What the library does:** nothing, on purpose. `SlmpConnectionEntryBusyError` is correct in this
case too — the entry really was not this socket's — and the package has no default backoff on
reconnect, so an immediate reconnect is the ordinary way to arrive here. What changed is the
error's *explanation*: it names both causes, gives the measured window, and says the fix is a
short settle rather than a hunt for a second client. There is no retry, and there is no wait
inside the transport.

**What the benchmarks do:** the bench scripts and `aslmp bench` bracket the library's rows with a
raw-socket control, so every TCP run releases the entry and takes it straight back, twice. Each of
those seams takes `aslmp.tools.bench.ENTRY_RELEASE_SETTLE_S` — 5 ms, taken outside every timed
section. That is 2.5x the window **on the link it was measured on**, which by the argument above
is not 2.5x anywhere else: bench from a faster host and 5 ms may not be enough. It will fail
loudly if so — `SlmpConnectionEntryBusyError`, naming this section — rather than quietly skew a
row. Until 2026-09-07 these seams had no settle at all, which is why an outside reviewer could not
get `bench/transports.py` to run against this CPU. Registered as `A-ENTRY-RELEASE-RACE`.

## 3. `socket.connect()` proves nothing, so `connect()` runs a handshake

`0x0619` Self Test with payload `b"0619"` + four hex digits of a per-generation nonce, echo
compared **byte for byte**. On the bench a `0x0619` costs the same as a 2-word read — **Wi-Fi,
2026-09-06**, so read the columns against each other and not as absolute costs (the same reads
are 2.42–3.63 ms at p50 on wire, section 5):

| probe | n | min | p50 | p90 | p99 | max | stdev |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `0619` loopback, 4 bytes (touches no device) | 300 | 4.96 | 7.34 | 9.29 | 13.29 | 21.04 | 1.65 |
| `0401` D4, 2 words | 300 | 4.12 | 6.92 | 8.62 | 10.76 | 11.68 | 1.20 |
| `0401` D0–D959, 960 words | 50 | 8.14 | 9.20 | 10.52 | 12.79 | 12.79 | 0.92 |

The loopback touches no device memory and costs the same as a read, so **essentially the whole
~7 ms is transport plus the SLMP module's service processing**, not device access. And 958 extra
words cost about 2.3 ms at p50.

Two consequences: `0x0619` is the right zero-side-effect liveness probe, and **round trips are
expensive while batching is nearly free** — 960 words in one transaction (9.2 ms) against 480
separate 2-word reads (~3.4 s).

Bench frame that works, verbatim:

```
request  50 00 00 FF FF 03 00 0C 00 10 00 19 06 00 00 04 00 41 42 43 44
echo     04 00 41 42 43 44
```

## 4. Wrong encoding, wrong transport, wrong frame type and an overstated length all fail by silence

None of these produces an error on the wire. All four produce nothing at all.

| mistake | what happens |
| --- | --- |
| binary into an ASCII port, or the reverse | `0xC06F` internally, **no response** |
| wrong protocol for the entry (TCP at a UDP entry) | no response |
| wrong frame type at an entry that refuses it | no response |
| `L` **overstated** | CPU blocks for bytes that never come. **No response.** |
| `L` **understated** | `0xC061`, and the connection recovers |

**What the library does:** `SlmpTimeoutError.likely_causes` is computed from context, not
enumerated.

| observation | ordered causes |
| --- | --- |
| **0 bytes**, handshake or first transaction of a generation | `CODING_MISMATCH`, `FRAME_NOT_ACCEPTED`, `PROTOCOL_MISMATCH`, `WRONG_PORT`, `ENTRY_BUSY` |
| **0 bytes**, on a connection that has completed transactions | `PLC_STOPPED_OR_RESET`, `CPU_BUSY`, `NETWORK` — the coding cannot have changed under a live socket |
| **partial** bytes, fewer than `prefix + L` | `REQUEST_LENGTH_OVERSTATED`, `NETWORK`, `CPU_BUSY` |

The overstated/understated asymmetry is why `L` has one expression in the package and three
tests guarding it.

## 5. Latency, TCP against UDP — and the conclusion the link overturned

This is the one place where re-measuring on a different link **changed the answer**, and it is
recorded here in full because it is the strongest argument in this file for labelling links.

### 5.1 What we published first: Wi-Fi, 2026-09-06

300 sequential 2-word reads of D4 on each transport, same minute, laptop at 192.168.10.41 over
**Wi-Fi**:

| | n | min | p50 | p90 | p99 | max | stdev |
| --- | --- | --- | --- | --- | --- | --- | --- |
| UDP | 300 | 3.99 | **6.20** | 7.99 | 13.80 | 24.36 | 1.79 |
| TCP | 300 | 4.35 | 7.41 | 8.88 | **10.49** | **14.32** | **1.03** |

The reading was "UDP wins the median, TCP wins the tail", and that tail was **the stated reason
`TransportKind.TCP` is the default**: a control loop is a jitter problem, so the p99 column wins
the argument.

### 5.2 What a wired retest says: `argus-bench`, 2026-09-07

An outside reviewer challenged the claim. 300 samples each again, 15 warmup, **interleaved
TCP/UDP/TCP/UDP/TCP** so that any drift over the run lands on both transports, from
`argus-bench` at 192.168.10.36 over **wire** (TCP 5002, UDP 5005):

| measurement | n | min | p50 | p90 | p99 | max | sd |
| --- | --- | --- | --- | --- | --- | --- | --- |
| TCP control (before) | 300 | 2.59 | 3.62 | 4.05 | 4.76 | 4.89 | 0.34 |
| **UDP 2-word read** | 300 | 1.92 | **2.42** | **3.40** | **3.57** | **3.87** | 0.40 |
| TCP 2-word read | 300 | 2.39 | 3.63 | 4.05 | 4.69 | 5.08 | 0.36 |
| UDP 2-word read (repeat) | 300 | 1.92 | 2.42 | 3.42 | 3.56 | 3.98 | 0.41 |
| TCP control (after) | 300 | 2.51 | 3.63 | 4.19 | 4.71 | 4.78 | 0.39 |

**Control drift across the whole run: 0.01 ms at p50** — the error bar on everything above is
smaller than any difference in the table. UDP wins p50 by 1.21 ms, p90 by 0.65 ms and **p99 by
1.13 ms**, at essentially equal standard deviation (0.40 against 0.36). The UDP row repeats to
within 0.01 ms at p50 and 0.02 ms at p90.

**UDP wins at every percentile on wire, including the tail.** The Wi-Fi tail result did not
reproduce.

### 5.3 So the earlier conclusion was a property of the radio

Nothing about the protocols changed between those two tables; the medium did. On Wi-Fi a lost
datagram costs a full client timeout while TCP recovers with a fast retransmit, and enough
datagrams are lost on a radio for that to own the p99 column. On wire, over 600 UDP samples,
that mechanism never fired.

**The old sentence "TCP wins the tail" was true of that link and false as a claim about SLMP.**
It is exactly the failure this document's link labels exist to prevent, and we shipped it
anyway.

### 5.4 The default did not change. Its justification did

`TransportKind.TCP` is still the default, and now for a reason that has nothing to do with
latency:

- **A UDP SLMP connection entry on iQ-F is point-to-point.** GX Works3 refuses to save one
  without a destination IP address, so a UDP entry serves exactly one host — and there are at
  most **eight** entries on the CPU, shared with MELSOFT, socket and predefined-protocol
  connections. UDP works only if somebody configured an entry for *your* address. A TCP entry
  serves any peer.
- **Loss is silent on UDP.** TCP retransmits; a dropped datagram is a serial that never comes
  back (section 6).

So TCP is the default for **configurability**, not for speed. Where an entry exists and the link
is wired, UDP is the faster choice at every percentile and a caller should take it explicitly
and knowingly. `aslmp.client.TRANSPORT_CHOICE` carries this note in the code, corrected; the
note it replaced asserted the tail claim and is now false.

Both readings are kept side by side in the shipped table, `src/aslmp/data/ambiguities.tsv`, as
**`A-UDP-TAIL-LATENCY`**, with the probe that would settle it on a third link — because the
withdrawal is more useful to the next person than the conclusion was. (Like every other
transport-level row, it is in the table but not yet in `aslmp ambiguities`, which prints the
ambiguities a *profile* or a *command* declares.)

### 5.5 Jitter shape (Wi-Fi, 2026-09-06)

500 further TCP reads from the laptop, 1 ms buckets:

```
ms : 5   6   7    8    9   10  11  12  13  14  15  16  17  18  22
n  : 1  30  122  158  80  53  33   6   5   4   2   2   1   2   1
```

p50 8.46, p90 11.10, p99 16.22, max 22.17, stdev 1.86. Unimodal with a long right tail, and no
bimodal 40 ms Nagle cluster. The ~0.97 ms scan period is not visible as quantisation.

**Nagle is not the jitter.** `TCP_NODELAY` on versus off differed by 0.32 ms at p50 and 0.12 ms
in stdev, and the *minimum* was lower with Nagle enabled. The library sets `TCP_NODELAY` because
it costs nothing and protects the segmented-write case, **not** as a latency fix.

**The same rig moved 5x at the tail between days:** p50 7.1 / p99 18.8 ms on one afternoon,
p50 10.3 / p99 95.2 ms on another, on that link with nothing else changed. This is why every
published number in this repository carries a same-session raw-socket control — and, since
2026-09-07, why it carries its link as well. A control catches the day. Only the label catches
the medium.

## 6. The UDP receive queue is a hard 32, and overflow is silent

Bursts of 4E reads fired without waiting, then drained. **Wired**, from `argus-bench`,
2026-09-07 (UDP 5005):

| depth | answered | elapsed | rate | lost |
| --- | --- | --- | --- | --- |
| 1 | 1/1 | 4.26 ms | 234 txn/s | 0 |
| 8 | 8/8 | 23.99 ms | 334 txn/s | 0 |
| 16 | 16/16 | 43.08 ms | 371 txn/s | 0 |
| 32 | 32/32 | 79.02 ms | **405 txn/s** | 0 |
| 48 | **32**/48 | 3085 ms | 10 txn/s | **16 lost** |
| 64 | **32**/64 | 3086 ms | 10 txn/s | **32 lost** |

At depth 48 exactly 32 came back. At depth 64 exactly 32 came back. **This is a hard ceiling of
32, not a soft degradation** — the 33rd request onward is discarded with no end code, no ICMP
and no error of any kind. The client learns only by timing out on a 4E serial that never
returns, and only 4E makes that detectable at all.

The rows above 32 take ~3.09 s because they are the client's own timeout expiring on the
missing serials, not the CPU working harder.

**The earlier Wi-Fi ladder was blurrier and this corrects it.** From the laptop over Wi-Fi on
2026-09-06 the same test gave 8/8, 32/32 and then **44/64** — 44 answered, not 32, which read as
"clean at 32, roughly 31% lost at 64" and suggested a soft threshold. Both tables are real. The
reading that fits both, and it is **an inference and not something we can see inside the CPU**:
on a radio the burst arrives spread out, so the CPU drains some of the queue while the rest of
the burst is still in flight and more than 32 requests are eventually served; on wire the whole
burst lands before the CPU drains anything, which is what exposes the queue's actual depth.
Design against 32.

### Throughput, on the link it was measured on

405 txn/s at depth 32 against **276 txn/s for serial TCP at p50 on this wired link**:
pipelining to 32 buys about **1.5x**, not the ~2.7x the noisier Wi-Fi numbers suggested (363
against ~135 txn/s there). The wired link is faster in absolute terms *and* the multiplier is
smaller, because serial TCP got most of the benefit.

Registered as **`A-UDP-PIPELINE-DEPTH`**, re-resolved on 2026-09-07: the row previously said
"clean at 32, roughly 31 percent lost at 64", which was a real measurement of the wrong shape.

**What the library does:** a lost datagram raises `SlmpDatagramLostError` naming the serial and
the in-flight depth at the time. Never a retry. Never a generic timeout. **Pipelining requires
4E**, and 3E/UDP pipelining is refused outright: without serials, positional matching plus real
loss produces silently mismatched replies, which is TCP coalescing one layer up.

Responses never arrived out of order in any test, but SH-081257ENG warns that they can, and the
64-deep result proves loss is real, so positional matching is not relied on anywhere.

## 7. Point limits and datagram size

```
480 points  -> 0x0000, datagram  975 bytes
960 points  -> 0x0000, datagram 1935 bytes
961 points  -> 0xC052, datagram   24 bytes
1000 points -> 0xC052, datagram   24 bytes
```

The 960-word ceiling is a protocol limit, not an MTU limit. Note that a 960-point read is
**1935 bytes, well over the 1500-byte Ethernet MTU**, so large UDP reads are IP-fragmented and
every fragment must arrive. Another reason large reads belong on TCP.

Binary-searched limits on the built-in port, binary coding, all shipped as `Provenance.LIVE`:

| command | limit | end code when exceeded |
| --- | --- | --- |
| `0401` word units | 960 | `0xC052` |
| `0401` bit units | 3584 | `0xC051` |
| `0403` random read | 192 points | `0xC054` |
| `1402` random write | `word×12 + dword×14 ≤ 1920`, bit cap 188 | — |
| `D` device range | D0–D7999 | `0xC056` |

Both transports share these ceilings, so validation is transport-independent.

## 8. TCP segmentation is real, and intermittent

One of three identical 1931-byte reads arrived in two segments, split at the 1460-byte MSS
(2026-09-06). Re-measured through the library on 2026-09-07: **one of six**, arriving as
`9 + 1451 + 471` bytes — 1451 rather than 1460 because the prefix read had already taken the
first nine bytes of that segment. The other five arrived as `9 + 1922`, and an immediately
following run of six split none of them. Do not write a test that demands the split.

Reads are therefore length-driven — never `recv(4096)` — and the receive timestamp is taken
**after the last chunk**. Stamping the first would report 11.0 ms for a 14.0 ms transaction.

Note what "segmented" must *not* mean. Reading the fixed prefix and then exactly `L` more units
takes two `recv` calls whatever the network does, so a response with two chunks is the ordinary
case and carries no information. `Chunk.partial` — this read came back **short of what it asked
for** — is the only honest evidence of a split, and `TransactionTiming.segmented` and
`Counters.segmented_responses` are computed from it. Counting chunks instead reported 100% of
TCP transactions as segmented, which is what the first assembled build did.

## 9. Three corrections to the doc-derived end-code map

| code | the documents implied | this CPU returns |
| --- | --- | --- |
| `0xC05C` | `0xC05B` for a bad or absent device code | `0xC05C` for device code `0x00`, `0xFF`, `V0`, `ZR0`, `DX0` |
| `0xC052` | an address error for a zero point count | `0xC052` — a **point-count** error |
| `0xC061` | `0xC057` for a request-length mismatch | `0xC061` when understated; **no response** when overstated |

All 16 abnormal frames captured were exactly 20 bytes with `L = 0x000B` and no command-defined
error trailer.

## 10. `0x0801` / `0x0802` are not iQ-F commands

Monitor Registration and Execute Monitor both return **`0xC059`** — "command or subcommand that
cannot be used by the CPU module" — not the `0xC05D` "monitor not registered" a reader of the
generic SLMP reference would expect. Measured twice through independent code paths.

`aslmp` refuses them pre-transport with a typed `SlmpCapabilityError` citing the measurement, and
**never emulates them with a `0x0403`**. On this CPU a random read is one round trip either way,
and 3.0x faster at p50 than three batch reads.

## 11. 4E works on the built-in port, contradicting two manuals

JY997D56001-K §2.1 and JY997D56201-B p.25 both say the FX5 CPU module supports 3E only. This CPU
accepted 4E frames on an entry configured for 3E and echoed the serial number correctly.

The library defaults to 3E and **permits** 4E, labelled observed rather than contractual. If
Mitsubishi confirms the behaviour is intentional, defaulting to 4E on iQ-F would turn the
coalescing corruption of §1 into a loud `SlmpSerialMismatchError`, and would be the single
largest correctness win available.

## 12. The documented-illegal things this CPU accepts

Two, both with end code `0x0000`:

- **`TS0` in a Device Read Random.** Timer contact points are documented as not permitted in
  `0403`. The CPU read it and returned success. `aslmp` refuses `TS`/`TC`/`STS`/`STC`/`CS`/`CC`/
  `LCS`/`LCC` in random access anyway — this guard is load-bearing rather than decorative.
- **A write to `Y8`.** Under octal notation `Y8` and `Y9` do not exist. The CPU accepted the
  write and reported success. Rejecting the digits 8 and 9 in an X/Y literal is the client's job
  and nothing else will do it.

## 13. X and Y are octal, and the wire carries the value

Method: clear Y0–Y63, set exactly one bit at a chosen **wire** device number, then read four
words back from Y head 0 and see which *linear* position lit.

```
wire number  0 (0x00) -> linear bit 0
wire number  8 (0x08) -> linear bit 8
wire number 16 (0x10) -> linear bit 16
```

The device-number field carries the **value** of the address, not its written digits:

| GX Works3 | meaning | wire |
| --- | --- | --- |
| `Y0` | 1st output | 0 |
| `Y7` | 8th output | 7 |
| `Y10` | 9th output | **8** |
| `Y17` | 16th output | 15 |
| `Y20` | 17th output | **16** (0x10) |

A library that sends the digits as written puts `Y10` on the wire as 10 and hits the 11th
output — off by two, `0x0000`, no error. `Y70` written as `0x70` is off by 56.

This settles the design's largest open ambiguity, `A-IQF-XY`, and it is kept in `aslmp
ambiguities` with the measurement attached because the reading it rules out is what several
other libraries implement.

**iQ-R differs — X and Y are hexadecimal there** — so the radix belongs to the profile, not to
the device letter. `X1F` is legal on iQ-R and a parse error on iQ-F.

## 14. `f32` is low word first, and the monitoring timer is inert

Word order was proved four ways, through both the batch path and the `0403` double-word path,
which agree. A `0403` double-word access point *is* one IEEE-754 float, natively, with no
byte-swapping helper anywhere in the library.

`word_order` is still exposed on the API, because it is a **PLC-program convention** rather than
a protocol fact and somebody's ladder will disagree.

The monitoring timer field is inert on this CPU: `0x0000`, `0x0010`, `0x0028` and `0x00F0` were
all answered normally in ~7 ms. JY997D56001-K p.27 requires `0000H` for the FX5 CPU port and says
a non-zero timer is "supported only for Ethernet modules"; here it is ignored rather than
refused.

## 15. Remote RUN, STOP and PAUSE work — and entering RUN is asynchronous

Measured 2026-09-07 from the **laptop at 192.168.10.41 over Wi-Fi**, TCP entry 5003, with
`Plc(allow_remote_control=True)`; the sequence is now a hardware test of its own,
`tests/hardware/test_remote_control.py`, on entry 5004. The independent oracle throughout is the
**free-running scan
counter in D8**, a REAL written by the PLC's own program: if the CPU is executing it advances at
~1029/s and if it is not it does not move. SD203 is what the library reads; D8 is what checks
SD203.

We had refused to send these at all, because this CPU's memory-card error once left it declining
a remote RUN and needing a physical power cycle. The card is out, and GX Works3 drove remote STOP
and RUN repeatedly through the same session, so the reason to abstain was gone. **`0x1006` Remote
RESET and `0x1005` Latch Clear were still not sent, and are not going to be.**

`0x1002` STOP, `0x1001` RUN and `0x1003` PAUSE all did what they say, confirmed against D8 and
not only against SD203. Every sequence ended with the CPU back in RUN and the counter advancing.

### A remote STOP clears non-latched device memory, and D8 restarts from zero

Not something we went looking for. Measured 2026-09-07 from the laptop over Wi-Fi, TCP entry
5004: `D100`/`D101` were written `0x1234`/`0x5678` and `D8` stood at 693,829 with the CPU in RUN.
One `0x1002` Remote STOP later, and **while still in STOP**, `D100`/`D101` read `0`/`0` and `D8`
read `0.0`. After the following `0x1001` RUN, `D8` was at 338 and climbing again.

Three consequences worth writing down:

- **It is a third, independent confirmation that the CPU really stopped** — a clear of
  non-latched memory is something only the CPU can do, and neither SD203 nor a client-side
  decode can fake it.
- **`D8` is free-running only within one RUN.** Its value is not comparable across a remote
  STOP, and a monotonicity assertion spanning one would fail. Everything in
  `tests/hardware/test_remote_control.py` compares deltas inside a single window for this
  reason; nothing there holds a `D8` value across a transition.
- **Running the remote-control suite wipes the `D100`-`D119` / `M100`-`M119` scratch and any
  other non-latched device on this CPU.** It restores nothing, because there is nothing to
  restore — the CPU did the clearing. `test_fx5u.py` leaves that scratch at zero anyway, so the
  two suites do not interfere, but do not park anything in D-memory across a remote STOP and
  expect to find it.

`0x1001` was sent only with `ClearMode.NONE`; the clearing above is the CPU's own STOP/RUN
behaviour and not a clear mode we asked for. `A-CLEAR-MODE` is unaffected and stays open.

### The asymmetry, and the bug it found in our own verification

Three stop/run cycles, polling SD203 every 2 ms after the command (each poll is a round trip, so
on this link the sampling grain is ~7 ms, not 2 ms — the times below are upper bounds):

| | first poll after the command | reached the target state |
| --- | --- | --- |
| after remote **STOP** | `STOP` — correct, **3 of 3** | ~18–21 ms |
| after remote **RUN** | `STOP` — **wrong, 2 of 3** | ~25–33 ms, on the second poll |

**Reproduced later the same day**, by `tests/hardware/test_remote_control.py` on TCP entry 5004
from the same Wi-Fi laptop, three cycles: `stop` needed one SD203 read in 3 cycles of 3, `run`
needed two in **3 cycles of 3**, and a verified `run()` took 29.8 / 37.7 / 36.3 ms end to end
including its reads. Same asymmetry, one cycle more of it. The counts are a property of this CPU
on this link on this day and the test prints them rather than asserting them; what it asserts is
the inequality — entering RUN never costs *fewer* extra polls than leaving it.

**The original table's link was not written down.** It was taken on 2026-09-07 with GX Works3
also driving the CPU, which puts it on the Windows laptop over Wi-Fi, and the ~7 ms sampling
grain above matches that link; but the notes it came from do not say so, so treat the host label
on that specific table as an inference. The reproduction in the paragraph above is the row whose
link is known.

**Leaving RUN is effectively synchronous; entering RUN is not.** The CPU needs an extra scan or
two to start executing.

Our `_apply` read SD203 exactly **once**, immediately after the command. So `verify=True` — the
safety default — raised `SlmpRemoteStateNotReachedError` for a RUN that the CPU had accepted and
was about to perform, a false negative roughly two thirds of the time. The default that exists to
stop the library reporting a state change that did not happen was instead reporting a failure
that did not happen. It is fixed: SD203 is polled to a bounded deadline,
`aslmp.client.REMOTE_SETTLE_SECONDS` = 0.25 s, an order of magnitude above the worst transition
seen, returning the instant the state matches.

**That is not a retry and not silent recovery**, and the distinction is the whole point. The
remote-control command is sent **exactly once** and is never re-sent; what repeats is the
*observation*. Re-sending a state-changing command would be recovery; re-reading a status
register that has not settled yet is measuring properly. `RemoteResult.polls` reports how many
reads it took, so the asymmetry is visible in the result rather than hidden inside it.

After the fix, `run(verify=True)` costs **3 transactions** — the command plus two SD203 reads —
which is exactly what the measurement above predicts, and `stop(verify=False)` costs 1.

### What this did *not* settle

- **Remote RESET (`0x1006`) has never been sent.** It is the one command whose expected outcome
  is an absent response, and the one that reboots the CPU.
- **Remote Latch Clear (`0x1005`) has never been sent** either. Clearing a latch range on a
  machine nobody is watching is not a measurement worth taking.
- **We could not force the lie that makes `verify=True` the default.** Mitsubishi documents
  Remote RUN as completing with end code `0x0000` while the switch is in STOP and the CPU does
  not run (SH(NA)-080956ENG-M p.131). The bench CPU's switch is in RUN, and with it there, RUN
  was truthful: sent with `verify=False`, end code `0x0000`, and D8 advanced. So the reason for
  the default remains a **manual claim, not our measurement** — see
  [`unverified.md`](unverified.md). The reviewer who prompted this work independently reports
  seeing **GX Works3 announce "The RUN operation has been completed" while P.RUN stayed dark and
  the CPU never scanned** — the same lie through Mitsubishi's own tool. It raises confidence in
  the manual's warning; it is somebody else's observation through a GUI, with no frame capture
  and no end code written down, and it is **not a measurement of ours**.
- **The `1002`/`1005`/`1006` two-byte fixed field.** The iQ-F profile's `00 00` was sent and
  accepted, but `01 00` was never sent, so we cannot say whether `00 00` is *required* or merely
  *accepted*. `A-REMOTE-FIXED` stays open.

## 16. Five minutes of a real closed loop, and the PLC's own arithmetic closing through us

The strongest end-to-end evidence in this file is not a latency number. It is that **the PLC's
own controller gain closes exactly through this library**.

The bench PLC runs a proportional-only bath controller with a 60.0 °C setpoint and **a
proportional band of 12 %/K**, and it has no physical I/O, so the only plant its output can act
on is a simulated one. The soak harness closes that loop from the host: every cycle reads the
whole loop record in **one `0x0403`** through a bound block plan (D0 SP, D2 PV, D4 MV, D6 Err,
D8 scan), integrates a first-order bath model using **the PLC's own heater duty** as the input,
and writes the new process value back to D2. The PLC then closes the loop against us.

Run 2026-09-07 from `argus-bench` (192.168.10.36, **wired**) against TCP entry 5002. **This
table came from the throwaway harness `bench/soak.py` was promoted from, not from
`bench/soak.py` as it now stands**, which is why it carries no control rows: the bracketing
raw-socket controls, the free-running reference sample and the `--rate`/`--duration` arguments
were all added during the promotion. A rerun of the shipped script will therefore print more
rows and a different transaction total, and it will print the link's drift beside the loop's,
which this table cannot.

| | |
| --- | --- |
| cycles | **15,000** in **300.0 s** at exactly **50.0 Hz** |
| transactions | **30,005** completed, against 30,000 loop transactions plus the handshake, the `D2` as-found read, the restore and its read-back — which is 30,004. The odd one is unaccounted for, and it is recorded that way rather than explained away |
| sustained rate | 100 txn/s |
| errors | **0** |
| reconnects | **0** |
| cadence overruns | **0** |
| entry-busy / concurrent rejections | 0 / 0 |
| PLC scan rate under this load | **969/s**, against **1029/s** idle |

| ms | p50 | p90 | p99 | p99.9 | max | sd |
| --- | --- | --- | --- | --- | --- | --- |
| block read (`0x0403`, 5 values) | 3.67 | 4.23 | 4.69 | 5.52 | 6.72 | 0.48 |
| whole cycle (read + model + write) | 7.23 | 8.26 | 8.94 | 10.10 | 11.20 | 0.65 |

**Nothing drifted.** The block read's p50 moved from 3.72 ms over the first fifth of the run to
3.67 ms over the last — in the wrong direction for a leak, and of the same order as the 0.01 ms
control drift in section 5. Two transactions per cycle at 50 Hz cost this CPU about **6% of its
scan rate**, which is the number to quote when somebody asks what polling costs the PLC. One
CPU, one firmware, one cadence.

Read the absolute milliseconds as the weakest rows here. They are one host on one link on one
afternoon, and section 5 is the standing demonstration of what that is worth. The counters, the
internal drift and the arithmetic below are what a soak is actually evidence for; `bench/soak.py`
brackets every run with the same raw-socket control the rest of this repository requires, so a
rerun prints the link's drift next to the loop's.

### The shipped script, on the other link, with the controls it now carries

The table above cannot be reproduced from this repository as it stands, so here is one that can.
`bench/soak.py --port 5002 --rate 25 --duration 45 --control-samples 150`, run 2026-09-07 from
the **laptop at 192.168.10.41 over Wi-Fi**. It is a different link, a different cadence and a
run 6.7x shorter, so **none of these milliseconds are comparable to the wired table** — that is
the point of printing both.

| ms | n | min | p50 | p90 | p99 | p99.9 | max | sd |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| **raw socket (before)** | 150 | 5.45 | 7.56 | 9.50 | 16.71 | — | 18.37 | 1.65 |
| block read `0x0403`, free-running | 150 | 5.06 | 7.98 | 9.87 | 14.28 | — | 15.24 | 1.41 |
| block read `0x0403`, under cadence | 1125 | 5.14 | 7.91 | 10.44 | 26.93 | 57.42 | 86.06 | 4.53 |
| write f32 `0x1401`, under cadence | 1125 | 5.07 | 7.73 | 10.04 | 27.93 | 60.17 | 75.26 | 3.93 |
| whole cycle | 1125 | 12.66 | 16.59 | 20.44 | 51.34 | 95.74 | 101.33 | 6.86 |
| **raw socket (after)** | 150 | 6.02 | 7.74 | 9.59 | 18.91 | — | 21.15 | 1.96 |

1,125 cycles in 45.0 s at 25.0 Hz; 2,425 transactions completed of 2,425 started; **0 failed
cycles, 0 reconnects, 0 entry-busy, 0 concurrent rejections, 0 timeouts**; **53 cadence
overruns**, all of them cycles whose tail did not fit a 40 ms period on a radio; PLC at 997
scans/s under this load against 1018 idle. Control drift 7.56 → 7.74 ms at p50 (0.18 ms) — an
error bar eighteen times the wired run's, which is what a radio costs you. The soak's own drift
was +0.86 ms across the run, larger than the control's and not distinguishable from it at this
length.

The row that matters is the last one: **1,125 of 1,125 snapshots satisfied
`MV == clamp(Err * 12, 0, 100)`, worst deviation exactly 0.0.** `D2` was restored to 0.0 and read
back. The same script was then killed mid-run on purpose: the restore on its own connection
failed (`SlmpNotConnectedError` — the FAILED state is sticky and there is no reconnection
anywhere in this package), it said so, took the entry again on a second connection and put `D2`
back at 0.0. That path is exercised, not assumed.

### The arithmetic that makes it a proof rather than a plausible curve

A control loop that *looks* like it is working is easy to produce and hard to trust: a client
that decodes garbage can still draw a smooth line. This one is checkable, because the controller
is proportional-only with a band of **12 %/K**, so within a single `0x0403` snapshot the CPU's
own numbers must satisfy

```
MV  ==  clamp(Err * 12, 0, 100)
```

and **both sides come back from the PLC, in the same snapshot, through this library**. The soak
asserts it on every cycle and a single failure fails the run.

Six spot values, taken 2026-09-07 from the **laptop at 192.168.10.41 over Wi-Fi**, TCP entry
5003, by writing `D2` and reading `D0`/`D4`/`D6` back. These are the full `f32` values as Python
prints them, not a rounded rendering, so the identity can be checked digit by digit:

| PV written to D2 | Err (D6) | Err × 12 in f64 | MV (D4) | MV − clamp(Err×12) |
| --- | --- | --- | --- | --- |
| 59.56 | 0.4399986267089844 | 5.2799835205078125 | **5.2799835205078125** | 0.0 |
| 56.523 | 3.477001190185547 | 41.72401428222656 | **41.72401428222656** | 0.0 |
| 55.0 | 5.0 | 60.0 | **60.0** | 0.0 |
| 60.0 | 0.0 | 0.0 | **0.0** | 0.0 |
| 61.0 | −1.0 | −12.0 | **0.0** (floor) | 0.0 |
| 0.0 | 60.0 | 720.0 | **100.0** (ceiling) | 0.0 |

Bit-exact in all six, both clamps included. `D2` was restored to the 0.0 it was found at and read
back.

Earlier drafts of this table quoted the second pair as `Err` 3.477 → `MV` **41.725** and
explained the mismatch as display precision. It was not display precision: the measured `MV` is
41.72401428222656, which is `Err × 12` exactly, and 41.725 was a transcription error. The row
above is the re-measurement that settles it.

`bench/soak.py` allows 1e-4 % of deviation, for an f32-versus-f64 rounding of at most ~6e-6 %
that could legitimately occur. It has never been touched: across the 1,125-cycle Wi-Fi
verification run of 2026-09-07 the worst deviation over 1,125 snapshots was **exactly 0.0**, as
it was in the six spot values above. Arithmetic is the one thing in this file that is not a
property of the link — which is why it is the assertion the harness makes, and the latencies are
not.

**The PLC's own gain arithmetic closes through this client to the last bit of an `f32`.** That
is a stronger statement than any latency number in this file: it exercises the block plan's
field offsets, the low-word-first float decode, the `0x0403` read, the `0x1401` write and the
cadence at once, and it fails loudly if any one of them is wrong.

What it does **not** prove: the bath is our model, not the PLC's, so this validates the data
path and the controller's arithmetic, not any physical process. `D2` was restored to the value
it was found with (0.0) and read back to confirm.

## Reproducing any of this

```
aslmp probe    HOST --profile melsec:iq-f/fx5u --samples 100
aslmp bench    HOST --profile melsec:iq-f/fx5u
aslmp verify-ranges HOST --profile melsec:iq-f/fx5u
python bench/transports.py      --host HOST --profile melsec:iq-f/fx5u
python bench/access_patterns.py --host HOST --profile melsec:iq-f/fx5u
```

Everything in that list is read-only, `verify-ranges` included. None of them can send a
remote-control command.

Section 16 is the exception and says so at the top of its own file:

```
python bench/soak.py --host HOST --profile melsec:iq-f/fx5u --port 5002 --rate 50 --duration 300
```

**`bench/soak.py` writes `D2` once per cycle for the whole run**, because a loop that closes
through the CPU is the only way to measure the path a controller actually depends on. It reads
`D2` on the way in, restores it on the way out — on a second connection if the first one died,
and by printing the exact `aslmp write` command if both fail. Do not point it at a PLC you are
not allowed to write. It cannot send a remote-control command either.

Section 15 lives behind a deliberately awkward door. **No CLI subcommand can issue
`0x1001`/`0x1002`/`0x1003`/`0x1005`/`0x1006`** — `tests/unit/test_tools.py` asserts that by
walking every `aslmp/tools` module's AST rather than grepping its text — and nothing in `bench/`
constructs a client with `allow_remote_control=True`. The one place that does is a hardware test
of its own, `tests/hardware/test_remote_control.py`, kept out of `test_fx5u.py` so that the main
suite's total ban stays mechanically checked:

```
ASLMP_TEST_HOST=192.168.10.250 python -m pytest tests/hardware/test_remote_control.py -s
```

It uses TCP entry 5004, verifies against D8 rather than SD203, returns the CPU to RUN in a
`finally` and proves it with the scan counter, and asserts by walking its own AST that Latch
Clear and Reset cannot be reached from it. **It will stop your CPU.** Read it before you run it.
