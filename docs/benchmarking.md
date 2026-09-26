# Benchmarking

## The three rules

**No table without a same-session raw-socket control**, **no number without its link**, and
**no ratio between two different clocks.**

`aslmp bench` enforces the first (there is a `--no-control` flag whose only purpose is to be
refused by name), and `bench/_report.py` raises `MissingControlError` if a report is assembled
without one. The second and third are enforced by nothing but discipline, which is why they are
written here in the same size as the first: the second is the one this project has already
broken in public, and the third it broke quietly.

## Why the third rule exists

A speed-up is a ratio, and a ratio is only meaningful if its two halves were measured the same
way. Ours were not. The published "**4.9x for block reads**" divided the block's **wire time**
(7.75 ms, the library's own `wire_ms` stamp) by five separate reads' **wall time** (38.24 ms,
`perf_counter` around the loop) — n=9 of each, 2026-09-07, from the laptop at 192.168.10.41
over **Wi-Fi** at ~7 ms median RTT — so the numerator excluded the client's scheduling and the
denominator included four helpings of it. The same hardware test records the like-for-like
figure — `five_reads_wire_sum_p50_ms`, the sum of the five reads' own wire stamps — and prints
it under `-s`; it is simply not the one that got published.

The rule, then: **compare wire against wire, or wall against wall, and say which.** Where only
the mismatched pair exists, publish both columns, name the asymmetry, and label any multiplier
derived from them as an inference. A block read's actual argument does not need the multiplier
anyway: five reads are five moments and one `0x0403` is one moment, and no latency column shows
that.

**And quote the `n`.** A percentile without its sample count cannot be given an error bar by
the reader, which is how "+0.07 ms at p50" came to be published from n=120 — laptop at
192.168.10.41 over **Wi-Fi**, ~7 ms median RTT — with sd ~1.03 ms, where the standard error on
a difference of medians is around 0.17 ms, more than twice the quantity claimed. `Measurement` and the shipped tables now have `host`, `medium` and `samples`
columns so that a fact carries all three conditions with it.

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

The laptop at 192.168.10.41 over **Wi-Fi** (~7 ms median RTT), against this same FX5U-32MT/DS
on firmware 1.065, produced:

| afternoon | host | link | p50 | p99 |
| --- | --- | --- | --- | --- |
| one | 192.168.10.41 | Wi-Fi, ~7 ms median RTT | 7.1 ms | 18.8 ms |
| the other | 192.168.10.41 | Wi-Fi, ~7 ms median RTT | 10.3 ms | 95.2 ms |

The two dates were not written down at the time. That is a defect in the record and it is
printed as one rather than guessed at: this file's own rule is that a table names its host, its
medium and its median RTT, and the date is the fourth condition every measured row in
`ambiguities.tsv` carries. Nothing changed between the two runs but the day. A latency number with no control beside it does not
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

**One control is not a bracket, and a run with one is not publishable.**
`tests/hardware/test_fx5u.py`'s latency test takes its raw-socket control once, before the
library's samples, and the overhead figure that came out of it — "+0.07 ms at p50", from the
laptop at 192.168.10.41 over **Wi-Fi** at ~7 ms median RTT — was published as though it had
been bracketed. It had not, and at n=120 with sd ~1.03 ms the
difference of medians has a standard error around 0.17 ms in any case, so the run could not
have resolved 0.07 ms even bracketed. The restated claim is in `CHANGELOG.md`: the library's
p50 was **indistinguishable from a raw socket's at this n**, which is the finding, and it is a
better one than a number that implies a precision the run did not have.

## Distributions, never a mean

min / p50 / p90 / p99 / max / stdev, from **one** nearest-rank implementation used for both the
library rows and the control rows, so the columns are comparable.

Nearest rank, no interpolation: an interpolated p99 of 300 samples reports a latency that was
never observed, which is the wrong kind of number to publish about a machine.

A mean would hide the only thing worth knowing, and the transport comparison is the case in
point. On **Wi-Fi** (laptop, 192.168.10.41, ~7 ms median RTT, 2026-09-06) the two transports
split the columns — UDP took the median (6.20 against 7.41 ms) and TCP took the tail (p99 10.49
against 13.80, sd 1.03 against 1.79) — and a mean would have reported one winner and lost the
fact that a control loop cares about the second column. On **wire** (`argus-bench`,
192.168.10.36, 3.64 ms median RTT, 2026-09-07) there is no split at all: UDP takes p50, p90 and
p99 (2.42/3.40/3.57 against 3.63/4.05/4.69) at equal sd. Same client, same CPU, same command; two different shapes, and only
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
were sampled at four different moments; on our bench a split like that sampled up to 27 ms apart
(2026-09-06, laptop at 192.168.10.41 over **Wi-Fi**, ~7 ms median RTT — four round trips of a
7 ms link is where most of that 27 ms comes from, which is the point).
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
plc = aslmp.Plc(..., on_transaction=recorder)   # retains nothing after construction
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
