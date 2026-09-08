# Benchmarking

## The two rules

**No table without a same-session raw-socket control**, and **no number without its link.**

`aslmp bench` enforces the first (there is a `--no-control` flag whose only purpose is to be
refused by name), and `bench/_report.py` raises `MissingControlError` if a report is assembled
without one. The second is enforced by nothing but discipline, which is why it is written here
in the same size as the first: it is the one this project has already broken.

## Why the second rule exists

A control catches the day. It does not catch the medium.

On 2026-09-06 we published that UDP won the median and **TCP won the tail** on our bench, and
made that tail the stated reason `TransportKind.TCP` is the default. Every number in it was real
and correctly taken. The bench was on Wi-Fi. On 2026-09-07 the same comparison from a wired host
— interleaved TCP/UDP/TCP/UDP/TCP, with controls before and after that drifted 0.01 ms at p50 —
put UDP ahead at **every** percentile including p99. The tail result was a property of the radio,
where a lost datagram costs a full client timeout and TCP fast retransmits.

The default did not change; its justification did, and the corrected reasoning is in
[`hardware.md`](hardware.md) section 5 with both tables side by side. The lesson for this file
is narrower and blunter: **a latency table that does not name its link is not reproducible, no
matter how good its control is.** Name the host, the medium and the median RTT.

## The first rule is not ceremony either

The same machine, the same PLC and the same Wi-Fi link produced:

| afternoon | p50 | p99 |
| --- | --- | --- |
| one | 7.1 ms | 18.8 ms |
| the other | 10.3 ms | 95.2 ms |

Nothing changed between them but the day. A latency number with no control beside it does not
tell you what a library costs; it tells you what the link was doing while the library ran. **A
5x movement in the tail is larger than any overhead this library could possibly have**, so a
number without a control cannot be interpreted at all.

## What the control is

A bare blocking socket that shares **no code with this library**:

- the 3E binary request frame is built from literal bytes and `struct`, by hand;
- the response is read by its **declared length** — never `recv(4096)`;
- the end code and the declared `L` are both checked, so a control that quietly measured a
  failure is impossible;
- `time.monotonic_ns()` on either side of the whole exchange.

It is blocking on purpose. An event loop between the clock and the socket is exactly the overhead
the control exists to exclude.

Hand-writing it is only worth something if it is right, so `tests/unit/test_tools.py` asserts
the control's frame is byte-identical to what `aslmp.wire`'s own frame builder produces for the
same request, and writes the expected 24 bytes out field by field. If those two ever disagree,
either the control is measuring the wrong request or the library is building one — and the bench
means nothing until that is settled.

## Two controls, before and after

Every run brackets the library's suites with a control. The **drift between the two is the
honest error bar** on everything in between, and on a real plant network it is frequently larger
than the thing being measured. Read that line before any other row.

## Distributions, never a mean

min / p50 / p90 / p99 / max / stdev, from **one** nearest-rank implementation used for both the
library rows and the control rows, so the columns are comparable.

Nearest rank, no interpolation: an interpolated p99 of 300 samples reports a latency that was
never observed, which is the wrong kind of number to publish about a machine.

A mean would hide the only thing worth knowing, and the transport comparison is the case in
point. On Wi-Fi the two transports split the columns — UDP took the median (6.20 against
7.41 ms) and TCP took the tail (p99 10.49 against 13.80, sd 1.03 against 1.79) — and a mean
would have reported one winner and lost the fact that a control loop cares about the second
column. On wire there is no split at all: UDP takes p50, p90 and p99 (2.42/3.40/3.57 against
3.63/4.05/4.69) at equal sd. Same client, same CPU, same command; two different shapes, and only
percentiles show that they *are* different shapes.

The default is still `TransportKind.TCP`, now for configurability rather than jitter — a UDP
entry on iQ-F is point-to-point and there are eight entries in total. See
[`hardware.md`](hardware.md) section 5.4.

## Running it

```
aslmp bench 192.168.10.250 --port 5002 --profile melsec:iq-f/fx5u --samples 300
```

Suites: `self-test` (`0x0619`, no device access at all), `batch-1w`, `batch-2w`, `batch-960w`,
`random-4dw`, `block`. **`batch-2w` is the row that lines up with the control** — same command,
same device, same point count — so the difference between those two rows is this library's
overhead and nothing else.

A suite the profile refuses appears as a row with a refusal in its note rather than crashing the
run. On an iQ-F that is information: block access is one of the paths that ships unverified.

Nothing in `aslmp bench` writes to the PLC, and no flag makes it.

The scripts in `bench/` produce Markdown for pasting into an issue or a README:

```
python bench/transports.py      --host HOST --profile KEY --tcp-port 5002 --udp-port 5001
python bench/access_patterns.py --host HOST --profile KEY --port 5002
python bench/soak.py            --host HOST --profile KEY --port 5002 --rate 50 --duration 300
```

`transports.py` runs each transport with **its own** control — UDP's control is a different
kernel path, so borrowing TCP's would compare two different things. Point its `--udp-port` at an
entry configured for *your* host: a UDP entry on iQ-F names one destination IP, so the port that
works from one machine is silence from another. `access_patterns.py` asks one control loop's
four floats four ways: one `0x0403` random read, one contiguous `0x0401`, four separate
`0x0401`s, and one `0x0406` block.

**`soak.py` is the one script here that writes to the PLC**, and it writes `D2` on every cycle
for the whole run, because closing a real control loop is the only way to measure the path a
controller depends on. It reads `D2` on the way in and restores it on the way out — in a
`finally`, then on a fresh connection if the first one died, and by printing the exact `aslmp
write` command that puts it back if both of those fail. It also asserts the CPU's own
proportional-band arithmetic (`MV == clamp(Err * 12, 0, 100)`) on every cycle, so it fails a run
whose latency table would have looked perfectly healthy. Do not point it at a PLC you are not
allowed to write.

## Reading `access_patterns.py`, which is the interesting one

The `4 separate reads` row will often not be four times slower than the single read, because the
CPU's service processing dominates the wire time — a `0x0619` that touches no device memory costs
the same as a 2-word read.

**It is still the wrong shape for a loop, and no latency column shows why.** Those four values
were sampled at four different moments; on our plant a split like that sampled up to 27 ms apart.
That is the entire reason `0x0403` exists, and it is why splitting a random read in this library
returns a `SplitReading` and not a `RandomReading`: the loss of atomicity is in the type, where a
benchmark cannot talk you out of it.

## Published numbers in this repository

The measured tables in [`hardware.md`](hardware.md) come from the raw-frame characterisation runs
that produced the design, each with its own control, and they name the CPU, the firmware, the
date and — since 2026-09-07 — the link and the host.

**`bench/` has published one table**: the five-minute closed-loop soak in `hardware.md`
section 16 — FX5U-32MT/DS fw 1.065, 2026-09-07, from `argus-bench` (192.168.10.36) over the
**wired** link, TCP entry 5002 — which is the run `bench/soak.py` was promoted from. Read its
absolute milliseconds as the weakest thing in that section. What a soak is actually evidence
for is its counters (0 errors, 0 reconnects, 0 overruns in 30,005 transactions), its internal
drift (p50 3.72 → 3.67 ms across the run, which is the honest way to read a long run without a
second machine), and its per-cycle arithmetic assertion. The script brackets every run with the
standard raw-socket control, so a rerun prints the link's drift beside the loop's.

`transports.py` and `access_patterns.py` have still published nothing. They are exercised
against the simulator, and the wired transport comparison in section 5 was taken with a
standalone harness rather than through them. When somebody runs them against a CPU, the tables
go in with the control rows and the link attached, or they do not go in at all.

## Measuring your own loop, without a benchmark

The library hands you the same numbers on every transaction, so you do not need a benchmark to
know what your loop costs:

```python
recorder = aslmp.LatencyRecorder()          # fixed ring, log-linear histogram,
plc = aslmp.Plc(..., on_transaction=recorder)   # allocates nothing after construction
...
print(plc.metrics())
```

`TransactionTiming` carries seven stamps — submitted, gate acquired, encoded, sent, first byte,
received (**after the last chunk**), decoded — plus the chunk list and the generation. So
`queue_ns` (host contention), `first_byte_ns` (network plus PLC service), `transfer_ns` (bulk and
segmentation), `wire_ns` (the number), `decode_ns` and `host_gap_ns` (your own loop's scheduling
gap) are all attributable without a packet capture.

`generation` bumps on every reconnect and every UDP rebind, so a percentile that moved can always
be asked "did the socket change underneath me?".
