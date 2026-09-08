# Errors: what you get, and what it tells you

Two rules underneath all of it.

**Nothing recovers silently.** Nothing in this library retries, clamps, substitutes a default or
returns a stale value. Reconnection is never implicit and is always an observable event. An AST
test over the whole package forbids bare `except:`, `except Exception` without a re-raise or
wrap, `try/except/pass`, any falsy return from an except body, and any retry loop or sleep in the
transport or the client. Every one of those patterns appears in a library we surveyed, and every
one of them turns a failed read into a plausible-looking value.

**Where the failure happened decides what is raised.**

| when | raised |
| --- | --- |
| validation, capability, range, point limit, layout | `SlmpUsageError` subclass — **nothing was sent** |
| the socket write raised before any byte reached the OS | `SlmpNotSentError` — **provably did not happen** |
| after the request reached the OS, `mutates` command | `SlmpOutcomeUnknownError` |
| after the request reached the OS, read-only command | the ordinary `SlmpTransportError` / `SlmpProtocolError` |
| a well-formed response with a non-zero end code | the `SlmpEndCodeError` subclass for that code |
| end code `0x0000` and the CPU still did not do it | `SlmpSemanticError` subclass |

The classification is driven by `Command.mutates`, which is an abstract `ClassVar` — a command
class that omits it is a type error — so a new command cannot forget the distinction.

## The tree

```
SlmpError
├── SlmpUsageError(ValueError)          pre-transport. NOTHING WAS SENT.
├── SlmpTransportError                  the socket. no end code exists.
│   └── SlmpTimeoutError(TimeoutError)  .likely_causes, .bytes_received, .deadline_s
├── SlmpProtocolError                   bytes arrived; they are not a valid response
├── SlmpEndCodeError                    the PLC answered, in its own words
├── SlmpSemanticError                   end code 0x0000, and it still is not true
└── SlmpOutcomeUnknownError             a STATE-CHANGING request failed AFTER the bytes went out
```

Three decisions in that shape are worth stating.

**`SlmpUsageError` is also a `ValueError` and `SlmpTimeoutError` is also a `TimeoutError`.** An
existing `except ValueError` or `except TimeoutError` keeps working. Interoperability beats
single-inheritance purity, because the alternative is people writing `except Exception`.

**`SlmpOutcomeUnknownError` is a sibling of the whole tree, not a `SlmpTransportError`.**
`except SlmpTransportError: retry()` is correct for a read and a data-loss bug for a write.
Nesting it under the transport branch would re-arm exactly that bug. It carries
`.reason: OutcomeUnknownReason` (`TIMEOUT`, `SEND_INCOMPLETE`, `CONNECTION_LOST`, `CANCELLED`,
`RESPONSE_CORRUPT`), `.sent`, `.request`, `.tx`, and the original as `__cause__`.

**Reads never raise it.** A failed read has no outcome to be unknown about.

## Every exception renders the same way

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
            through independent code paths.
  action    Monitor Register / Execute Monitor are iQ-R commands. On iQ-F use read_random()
            — one round trip either way, and 3.0x faster at p50 than three batch reads.
  manual    JY997D56001-K §6 Troubleshooting; SH(NA)-080956ENG-M p.33 accessibility matrix
```

`sent` and `received` appear when the client was constructed with `capture_frames=True`.

**`__str__` never raises**, and never renders an end code in decimal. There is a test asserting
both for every row in the table, because `pymelsec` renders `0xC056` as the string `'0x49238'`
and then `str(e)` raises `TypeError` — a diagnostic that fails while you are reading it.

**An unknown end code still raises a named class.** `SlmpEndCodeError` with
`description="undocumented end code"`, formatted `0x%04X`, and a line asking you to report it
with the CPU model and firmware. Never a bare integer, never `"slmp_end_code_c059"`.

## Reading a timeout

`SlmpTimeoutError.likely_causes` is **computed from context**, not enumerated, because on this
hardware four different mistakes all produce exactly nothing:

| what you observed | ordered causes |
| --- | --- |
| 0 bytes, on the handshake or the first transaction of a generation | `CODING_MISMATCH`, `FRAME_NOT_ACCEPTED`, `PROTOCOL_MISMATCH`, `WRONG_PORT`, `ENTRY_BUSY` |
| 0 bytes, on a connection that has completed transactions before | `PLC_STOPPED_OR_RESET`, `CPU_BUSY`, `NETWORK` — the coding cannot have changed under a live socket |
| **partial** bytes, fewer than `prefix + L` | `REQUEST_LENGTH_OVERSTATED`, `NETWORK`, `CPU_BUSY` |

The partial-bytes row matters: an overstated request length makes the CPU block waiting for bytes
that never come, which is indistinguishable from a dead PLC by any other means.

## The errors you are most likely to meet, and what to do

| exception | what happened | what to do |
| --- | --- | --- |
| `SlmpConnectionEntryBusyError` | either the entry already has its one TCP connection, or **you are reconnecting into your own `close()`** | with nothing else connected it is the second: settle ~5 ms before retaking an entry you just released ([`hardware.md` 2.1](hardware.md#21-a-reconnect-within-about-2-ms-of-your-own-close-can-be-refused)). Otherwise use a different entry. Either way do not retry: neither cause is fixed by asking twice |
| `SlmpTimeoutError` with `CODING_MISMATCH` first | almost always `--encoding` / `Encoding` wrong | check `Communication Data Code` in the Own Node Settings; it is port-wide |
| `SlmpConcurrentTransactionError` | two requests in flight on one TCP connection | that is the gate working. Serialise, or use a second connection entry |
| `SlmpProfileMismatchError` | the CPU's model code is not in the declared profile | run `aslmp identify` and pass what it prints |
| `SlmpDeviceRadixError` | `X1F` or `Y8` on an iQ-F | X and Y are octal there. `Y10` means the 9th output |
| `SlmpAddressRangeError` | the **span** leaves the device range | it takes the span, not the start: `D7999` alone is legal, `D7999` for 2 words is not |
| `SlmpPointLimitError` | over 960 words / 3584 bits / 192 random points | the exception names the limit, its evidence, and the end code the CPU would have returned |
| `SlmpCapabilityError` | `0x0801` on iQ-F, or `SpecFormat.LONG` on iQ-F | it is refused pre-transport and never emulated. Use `read_random()` |
| `SlmpDatagramLostError` | a UDP request never came back | it names the serial and the in-flight depth. Reduce the depth; 32 was clean and 64 lost 31% on our bench |
| `SlmpTargetChangedError` | the CPU identity changed across a reconnect | a prebuilt plan carried into a different D-memory layout returns plausible floats. Rebuild it |
| `SlmpOutcomeUnknownError` | a write failed after the bytes went out | **do not blindly retry.** Read the register back and decide |

## Counters and events, instead of logs

There are no `logging` calls in this library. A log call inside the transport lands in the
latency number it describes.

Instead: `plc.counters` (monotonic tallies — `entry_busy`, `concurrent_rejections`,
`segmented_responses`, `stale_datagrams`, `socket_rebinds`, `probes_skipped`, `sink_errors`, …), `plc.metrics()`
for a snapshot with latency percentiles, `on_transaction=` for every record including failed
ones, and `on_event=` / `plc.events()` for a typed `ConnectionEvent` stream.

`aslmp.observability.attach_logging()` is the single bridge to the `logging` module, and it is
opt-in.
