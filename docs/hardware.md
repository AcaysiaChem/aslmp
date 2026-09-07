# What one FX5U actually does

Every measurement below is from a **MELSEC iQ-F FX5U-32MT/DS, firmware 1.065**, at
192.168.10.250, in RUN at roughly 1024 scans/s with no physical I/O wired, over a **Wi-Fi** link
from a Windows 11 host on the same /24. Dates are 2026-09-06 and 2026-09-07.

Where a Mitsubishi manual and this CPU disagree, the code implements the CPU and cites the
measurement. Where they disagree and we could not test, the code implements the manual and ships
the row labelled — see [`unverified.md`](unverified.md).

This is n=1. One CPU, one firmware, one link, two afternoons. A firmware update could invalidate
any of it, and **nothing in the design tells us when a profile has gone stale**. `aslmp
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

## 3. `socket.connect()` proves nothing, so `connect()` runs a handshake

`0x0619` Self Test with payload `b"0619"` + four hex digits of a per-generation nonce, echo
compared **byte for byte**. On the bench a `0x0619` costs the same as a 2-word read:

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

## 5. Latency, TCP against UDP

300 sequential 2-word reads of D4 on each transport, same minute, same host:

| | n | min | p50 | p90 | p99 | max | stdev |
| --- | --- | --- | --- | --- | --- | --- | --- |
| UDP | 300 | 3.99 | **6.20** | 7.99 | 13.80 | 24.36 | 1.79 |
| TCP | 300 | 4.35 | 7.41 | 8.88 | **10.49** | **14.32** | **1.03** |

UDP saves ~1.2 ms at the median (no ACK round trip) and pays for it in the tail with nearly
double the standard deviation — a lost datagram on this link costs a full client timeout instead
of a fast retransmit. **TCP is the default because a control loop is a jitter problem.**

Jitter shape, 500 further TCP reads, 1 ms buckets:

```
ms : 5   6   7    8    9   10  11  12  13  14  15  16  17  18  22
n  : 1  30  122  158  80  53  33   6   5   4   2   2   1   2   1
```

p50 8.46, p90 11.10, p99 16.22, max 22.17, stdev 1.86. Unimodal with a long right tail, and no
bimodal 40 ms Nagle cluster. The 0.975 ms scan period is not visible as quantisation.

**Nagle is not the jitter.** `TCP_NODELAY` on versus off differed by 0.32 ms at p50 and 0.12 ms
in stdev, and the *minimum* was lower with Nagle enabled. The library sets `TCP_NODELAY` because
it costs nothing and protects the segmented-write case, **not** as a latency fix.

**The same rig moved 5x at the tail between days:** p50 7.1 / p99 18.8 ms on one afternoon,
p50 10.3 / p99 95.2 ms on another. This is why every published number in this repository carries
a same-session raw-socket control.

## 6. UDP pipelines cleanly to 32, then loses requests with no error anywhere

Bursts of 4E reads fired without waiting, then drained:

| burst | responses | elapsed | rate | lost | in order |
| --- | --- | --- | --- | --- | --- |
| 8 | 8/8 | 26.3 ms | 304 txn/s | 0 | yes |
| 32 | 32/32 | 88.1 ms | 363 txn/s | 0 | yes |
| 64 | 44/64 | 3132.7 ms | 20 txn/s | **20** | yes |

363 txn/s against ~135 txn/s for serial TCP: a 2.7x throughput gain, right up until the PLC's
receive path overflows and drops requests with no end code, no ICMP and nothing else. The client
finds out by timing out on a serial that never comes back.

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

## Reproducing any of this

```
aslmp probe    HOST --profile melsec:iq-f/fx5u --samples 100
aslmp bench    HOST --profile melsec:iq-f/fx5u
aslmp verify-ranges HOST --profile melsec:iq-f/fx5u
python bench/transports.py     --host HOST --profile melsec:iq-f/fx5u
python bench/access_patterns.py --host HOST --profile melsec:iq-f/fx5u
```

Everything in that list is read-only except `verify-ranges`, which is also read-only. None of
them can send a remote-control command.
