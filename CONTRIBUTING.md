# Contributing to aslmp

Contributions are welcome, and the most valuable one is not code.

This library's whole argument is that it does not tell you things that are not so. Our
evidence is **one CPU** — a MELSEC iQ-F FX5U-32MT/DS on firmware 1.065. Seven of the eight
shipped profiles have never been connected to, the ASCII path has never been on a wire,
and [`docs/unverified.md`](docs/unverified.md) lists every one of them next to the
experiment that would settle it. If you have an iQ-R, a Q, an L, an FX5UC, an FX5UJ, an
FX5S, or ten minutes and a spare CPU you can switch to ASCII coding, you can convert a row
of that page from "the manual says" into "we measured". Nothing in a pull request is worth
more than that, and [§ A CPU we have never seen](#a-cpu-we-have-never-seen) is a
walkthrough of how.

---

## Before anything else: this software writes to PLCs

`aslmp` writes setpoints to industrial controllers, and with
`Plc(allow_remote_control=True)` it can stop a CPU. A wrong value in a register can move a
real actuator.

- **Do not develop against a machine that is attached to anything that can move.** Use the
  bundled simulator (`aslmp serve`, or `aslmp.testing`) for everything that does not
  strictly need silicon.
- If you do have a bench CPU, keep it a bench CPU: no physical I/O wired, and its own
  isolated network segment.
- SLMP has no authentication and no encryption. Anything that can reach the port can read
  and write device memory, including outputs. See [`SECURITY.md`](SECURITY.md).

This is not a safety system, it is not SIL- or PL-rated, and it has never been assessed by
a functional-safety body. Interlocks belong in the PLC program and in hardware. Nothing in
a contribution may imply otherwise — see [§ What cannot go in](#what-cannot-go-in).

---

## Setting up

With [`uv`](https://docs.astral.sh/uv/):

```
git clone https://github.com/AcaysiaChem/aslmp && cd aslmp
uv venv --python 3.11
uv pip install -e ".[dev]"
```

`--python 3.11` on purpose: 3.11 is the oldest interpreter this package claims, and
developing on the newest one is how the clock bug in
[`src/aslmp/_clock.py`](src/aslmp/_clock.py) survived. Any supported version works; just
know which one you are on.

Better still, have all three, because knowing which one you are on turned out not to be
enough:

```
for v in 3.11 3.12 3.13 3.14; do
  uv venv --python $v ".venv$v" && uv pip install --python ".venv$v" -e ".[dev]"
done
```

On 2026-09-24 the matrix went red and four of its nine cells hung inside pytest for
fourteen minutes apiece. Everything was green locally, on the only interpreter installed,
which was 3.13 -- and 3.13 was the one cell that passed. Every one of the seven distinct
defects behind that run was diagnosed in minutes once 3.11 and 3.12 existed on the machine,
and not one of them was reproducible on 3.13 alone. A sample, because the pattern is the
point and none of these is exotic:

- `Server.wait_closed()` returned immediately before CPython 3.12.1 and genuinely waits
  from 3.12.1 on. A handler that returned without closing its writer was therefore inert
  on 3.11 and hung forever on 3.12.
- Reading an enum member goes through a descriptor on 3.11 and not from 3.12, which left
  a block live on one of `LatencyRecorder`'s lines and failed its memory test on the
  oldest supported interpreter only.
- `import runpy` pulls `threading` on 3.11 and not on 3.12 or 3.13, so an import-cost test
  reported its own probe as a leak.

Run the three checks on the oldest and the newest at minimum before pushing. A cell you
cannot reproduce is a cell you will be debugging through CI logs, fifteen minutes at a
time.

Plain `pip` does the same job:

```
python -m venv .venv && .venv/bin/pip install -e ".[dev]"    # .venv\Scripts\pip on Windows
```

The package itself has **zero runtime dependencies** and that is load-bearing: the wheel
has to install on a Jetson's aarch64 and on a locked-down plant PC with no compiler and no
proxy to PyPI's transitive graph. `[dev]` brings pytest, pytest-asyncio, hypothesis, mypy
and ruff, and nothing that ships.

---

## The three checks

```
python -m ruff check src tests tools bench
python -m mypy
python -m pytest tests -q
```

All three must be clean. CI runs exactly these on Python 3.11, 3.12, 3.13 and 3.14 across
ubuntu-latest, windows-latest and macos-latest, on every push and weekly with nothing
pushed at all.

That matrix is twelve jobs because a real bug here was visible on one of them:
`time.monotonic()` is backed by `GetTickCount64()` on Windows before CPython 3.13 and steps
at 15.625 ms, so every latency figure the library reported on that combination was
quantised to a ~16 ms grid (measured 2026-09-12 on Python 3.11.15, Windows 11, against
`perf_counter_ns` on the same machine and the same sleep). Development here happens on
3.13, where the same call resolves to 100 ns, so nothing local showed it. If a check passes
for you and fails in CI on one cell, that cell is probably telling you something true.

A few things the suite enforces that are easy to trip over:

- **`tests/unit/test_generated_is_current.py`.** `src/aslmp/wire/devicetable.py` and
  `src/aslmp/errors/endcodes.py` are generated from the TSVs in `src/aslmp/data/` and
  committed. If you edit a TSV, re-run the generator in `tools/` and commit the result;
  regeneration is asserted to be a byte-for-byte no-op.
- **`tests/unit/test_layering.py`.** Layer 0 is stdlib-only and I/O-free; `socket` is a
  banned import outside `transport/`. Ruff's `flake8-tidy-imports` catches the obvious
  case, the test catches the rest.
- **`tests/unit/test_tools.py`.** Walks the AST of every `aslmp/tools` module to prove no
  CLI path reaches remote RUN, STOP, PAUSE, RESET or Latch Clear.
- **`tests/unit/test_citations.py`.** The provenance rules, described below. This is the
  one that surprises people.

---

## The test suite

No test needs a PLC. Markers are declared in `pyproject.toml`:

| marker | what it means |
| --- | --- |
| `hardware` | needs a real CPU; skipped unless `ASLMP_TEST_HOST` is set |
| `simulator` | needs `aslmp.testing` |
| `slow` | not part of the fast sweep |

`asyncio_mode = "auto"`, so an `async def test_` needs no decorator.

### Running the hardware tests against a real CPU

```
ASLMP_TEST_HOST=192.168.10.250 python -m pytest tests/hardware -s
```

**Read the files before you run them.**

- `tests/hardware/test_fx5u.py` writes only to `D100`–`D119` and `M100`–`M119`, restores
  both, and asserts by walking its own AST that no remote-control command can be reached
  from it. Every measurement it takes is printed under `-s`; that output is the raw
  material for a provenance row.
- `tests/hardware/test_remote_control.py` is the deliberate exception, in its own file so
  the main suite's ban stays absolute and mechanically checked. **It stops your CPU.** A
  Remote STOP on the bench CPU clears non-latched device memory, so anything parked in
  D-memory is gone, including a free-running scan counter. It returns the CPU to RUN in a
  `finally` and proves it did.

The register map those tests assume is in the README's PLC-side section. Point them at a
CPU with nothing attached to it, or do not point them at anything.

CI never runs them: `ASLMP_TEST_HOST` is not set there, the run is additionally filtered
with `-m "not hardware"`, and `.github/workflows/ci.yml` fails the job if the variable is
present at all. Do not add it as a repository secret.

---

## Evidence: the one rule this project has

**A hardware claim without its conditions is not accepted.** A hardware claim is any
number, limit, range, end code, capability or behaviour asserted about real silicon —
in a TSV, a docstring, a comment, a document, or a test's expected value.

This is not a style preference. Two published claims have been withdrawn from this
repository, and both were figures whose conditions were never written down:

- a transport conclusion ("TCP wins the latency tail") that was measured correctly and was
  a property of the Wi-Fi link it was measured over. A wired retest overturned it;
- a scan period that was a register read with the wrong declared type, put through
  arithmetic, and that reached four files before anyone noticed.

So every shipped fact carries a `Measurement` or a `Citation`, and both validate at
construction — there is no "unknown manual" default to fall back to.
[`src/aslmp/wire/citations.py`](src/aslmp/wire/citations.py) is the type; these are its
fields, and they are what a contribution has to supply:

| field | what it is |
| --- | --- |
| `cpu` | the exact model. `FX5U-32MT/DS` is not evidence about an R04CPU |
| `firmware` | a firmware update can invalidate any row that names one |
| `date` | ISO `YYYY-MM-DD`; anything else is refused |
| `host` | where the client ran — an address, a name, or both |
| `medium` | what ran between that host and the CPU, with median RTT if known |
| `samples` | the `n` behind the figure, or nothing when a count is not what it rests on |
| `note` | what a reader should notice: the binary search, the manual figure it contradicts |

`host`, `medium` and `samples` are optional because most rows are not timings — a device
code that answers `0xC05C` answers it on any link. State them when they could have changed
the answer, and leave them out rather than inventing them when they were not recorded.

Provenance is one of three values and the distinction matters:

- `LIVE` — observed on named silicon.
- `MANUAL` — read out of a Mitsubishi document, cited to revision and section.
- `INFERRED` — carried across from a sibling model or derived. Three iQ-F profiles ship
  rows like this, and `docs/unverified.md` says which.

### Numbers in prose are policed too

The section at the bottom of `tests/unit/test_citations.py` walks `README.md`,
`CHANGELOG.md`, every document in `docs/`, and every Python file under `bench/`, `src/`,
`tests/` and `tools/`, and holds each published figure against the one row that measured
it. A number in a README is not typed, not executed and not imported by anything, which is
precisely why both withdrawn claims were prose. Expect the suite to fail if you quote a
measured figure in a new place without deriving it from its source — that failure is the
feature.

If you are adding a figure and are not sure where its source is: `aslmp cite` and
`aslmp ambiguities` print what the repository already knows.

---

## A CPU we have never seen

This is the contribution to make if you can.

**1. Find out what you have.**

```
aslmp identify HOST --port PORT
aslmp capabilities <profile-key> --unverified-only
aslmp ambiguities
```

`identify` needs no profile — `0x0619` and `0x0101` carry no device address, so the profile
cannot change a byte of them. `capabilities` prints the provenance of every row of the
profile you name; `--unverified-only` is the to-do list.

**2. Read `docs/unverified.md`.** Each section ends with a **Probe:** — the specific
experiment that would settle it. They are written to be run, not admired. A few that are
cheap if you have the hardware:

- the whole **ASCII** path, which needs one GX Works3 connection entry with Communication
  Data Code set to ASCII. On iQ-F that setting is port-wide, so it takes the binary entries
  down with it — which is exactly why we have not done it;
- **iQ-R device ranges**, where `R` and `ZR` default to zero points out of the box, so
  Mitsubishi's own `ZR16384` example fails on a factory CPU;
- `A-LONG-SPEC-BYTE-ORDER`, `A-ZR-RADIX`, `A-1617-SUBCOMMAND`, `A-REMOTE-FIXED` — each
  documented with the two readings and the one differential test between them.

**3. Measure.**

```
aslmp verify-ranges HOST --profile KEY --python
```

One 1-point read at each declared boundary, a binary search where the answers disagree,
nothing written and no CPU state changed. `--python` emits a paste-ready
`profile.with_ranges(...)` call. `aslmp.testing.run_conformance` does the same job for end
codes.

**4. Open the CPU behaviour issue** with the conditions filled in, or send the pull request
directly. A change like that usually touches four things: the TSV row (with its provenance
tail), the profile, `docs/unverified.md`, and a test.

That last one is deliberate friction. `tests/unit/test_citations.py::
test_no_iq_r_range_claims_to_have_been_measured` asserts that **no** iQ-R range row claims
a measurement, because no iQ-R has ever been on this bench. The first person who measures
one has to change that test, in the same commit, with their CPU and firmware in the diff.
It is meant to be impossible to mark a row measured by accident.

---

## Code conventions

Read a neighbouring module before writing a new one; the house style is more specific than
a linter can express. The parts worth stating:

- **Line length 96.** Ruff's configuration lives in `pyproject.toml` and it is the linter
  of record; `ruff check src tests tools bench` is the exact invocation CI runs.
- **`mypy --strict`, with `warn_unreachable`.** No new `# type: ignore` without a comment
  saying what it is hiding.
- **Layering.** `wire/` and `commands/` are I/O-free; sockets live in `transport/` and
  nowhere else. `aslmp.testing` may import layers 0–2 only.
- **Nothing retries, clamps, substitutes a default, or returns a stale value**, anywhere,
  ever. Reconnection is never implicit and is always an observable event. An AST-level test
  enforces this across the package.
- **No public `send()`.** Bytes reach a socket through a single-use capability token,
  because two TCP requests written before the first response is read return one response —
  for the *last* request — with end code `0x0000`.
- **`aslmp.__all__` is the contract.** Two minor versions of deprecation notice; no
  removals in a minor release. Anything not named there is private.
- **Import weight is tested.** `aslmp --help` does not import `asyncio` or `socket`, and
  importing the package opens no socket, starts no thread and reads no file. Keep CLI
  imports lazy.
- **Zero runtime dependencies.** A pull request that adds one needs an argument that
  survives "this has to install on a plant PC with no compiler".

## Documentation conventions

The voice is direct, specific and measured. No marketing, no superlatives, and no number
without its conditions. Three habits carry most of it:

- Say what is true now rather than what is intended. The README's install section says
  there is no `aslmp` on PyPI yet, because there is not.
- When something was wrong, say so where the wrong thing was, and say what it cost. The
  withdrawn claims are still quotable in this repository, next to the correction.
- Every hardware claim names its CPU, firmware, date, host and link. A figure without them
  is treated as unreproducible.

## What cannot go in

- Any claim that this library is a safety system, or is SIL- or PL-rated, or has been
  assessed by anybody. It is none of those things.
- The words **conformant**, **certified**, **compliant** or **validated** about the
  protocol implementation. `aslmp` implements a published protocol and is tested against
  one CPU. It holds no conformance certification. (The bundled *conformance simulator* is a
  test fixture and keeps its name; it certifies nothing.)
- Any suggestion of affiliation with, or endorsement by, Mitsubishi Electric or the CC-Link
  Partner Association. There is none. SLMP, MELSEC, iQ-F, iQ-R, GX Works3, FX5U and CC-Link
  are their trademarks; see `NOTICE`.
- A change that makes an unverified path look verified, or that quietly drops a row from
  `docs/unverified.md`. That page is billed as complete and a test enforces it.

---

## Pull requests

- Branch from `main`. Keep the diff to one idea.
- Commit messages say *why*. A subject line that names the defect is worth more than one
  that names the file.
- Fill in `.github/PULL_REQUEST_TEMPLATE.md`, especially the provenance section if your
  diff touches a number.
- Update `CHANGELOG.md` for anything that changes the public surface or a published fact.
- Expect review to ask where a number came from. That is the whole discipline of the
  project and it is not personal.

## Licence, security, conduct

Contributions are offered under the project's Apache-2.0 licence — see section 5 of
[`LICENSE`](LICENSE). There is no CLA.

Security reports follow [`SECURITY.md`](SECURITY.md) — GitHub private vulnerability
reporting, or dev@acaysia.com — and never a public issue. That file is also where the
protocol's own lack of authentication and encryption is written down, which is the thing
most people reporting a "vulnerability" here have found.

Everyone taking part is expected to follow the
[Code of Conduct](CODE_OF_CONDUCT.md); reports go to dev@acaysia.com.
