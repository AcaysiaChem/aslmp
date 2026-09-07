# Benchmarking

## The rule

**No table without a same-session raw-socket control.**

`aslmp bench` enforces it (there is a `--no-control` flag whose only purpose is to be refused
by name), and `bench/_report.py` raises `MissingControlError` if a report is assembled without
one.

This is not ceremony. The same machine, the same PLC and the same Wi-Fi link produced:

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

A mean would hide the only thing worth knowing. On our bench UDP wins the median (6.20 vs
7.41 ms) and TCP wins the tail (p99 10.49 vs 13.80, stdev 1.03 vs 1.79). **For a control loop,
only the second of those matters** — which is why `TransportKind.TCP` is the default.

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
```

`transports.py` runs each transport with **its own** control — UDP's control is a different
kernel path, so borrowing TCP's would compare two different things. `access_patterns.py` asks
one control loop's four floats four ways: one `0x0403` random read, one contiguous `0x0401`, four
separate `0x0401`s, and one `0x0406` block.

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
that produced the design, each with its own control, and they name the CPU, the firmware and the
date.

**`bench/` has published nothing yet.** The scripts are written and exercised against the
simulator; nobody has run them against the FX5U. When somebody does, the tables go in with the
control rows attached, or they do not go in at all.

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
