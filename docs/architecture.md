# Architecture

Nine layers, one direction, and a build that fails if an import goes the wrong way.

```
aslmp/
  wire/                       L0    pure bytes. stdlib only.
  errors/                     L0.5  the exception hierarchy + the generated end-code table
  profile.py, profiles/       L1    what a CPU family can do, and the evidence for it
  blocks/fields.py            L1
  commands/                   L2    encode + decode + validate on one frozen object
  identity.py                 L2
  blocks/layout.py            L2
  timing.py, observability.py L2.5  pure data: stamps, events, counters, histograms
  transport/                  L3    sockets. never imports wire/.
  connection.py, results.py   L4    frame + codec + route + serial + the in-flight gate
  client.py, timed.py         L5    the only modules that know all of it
  blocks/plan.py              L5
  health, resilience,
  entries, loop               L6
  sync.py                     L7    one background loop for the facade's lifetime
  tools/                      L9    one console script
  testing/                    2.5   the simulator; may import L0-L2 only
```

`tests/unit/test_layering.py` walks every `aslmp.*` import in the package with `ast` and fails
the build on an edge that is not strictly downward. `tests/unit/test_public_surface.py` checks
the same claim from outside, in a subprocess, because a static walk cannot see a lazy import
inside a function that runs at module scope anyway.

## The three rules that do the work

### 1. Layer 0 is pure and it is proved in a subprocess

After `import aslmp.wire`, the modules `socket`, `ssl`, `asyncio`, `selectors`, `threading` and
`logging` must be absent from `sys.modules`. Same for `aslmp.errors`, `aslmp.profile`,
`aslmp.commands` and `aslmp.blocks.layout`.

Importing any submodule executes `aslmp/__init__.py` first, so this rule is what forces the
package facade to be a lazy table (PEP 562 `__getattr__`) rather than a list of imports. An
eager `from aslmp.client import Plc` at the top of that file breaks the guarantee for every
pure module at once.

Two more properties ride along and are tested the same way: importing the package reads no
file (the device table and the end-code table are committed Python literals, not TSV parsing at
import) and starts no thread.

### 2. `transport/` never imports `wire/`

The transport takes a structural protocol:

```python
class Reassembler(Protocol):
    @property
    def bytes_needed(self) -> int: ...
    def feed(self, data: bytes, /) -> None: ...
```

`wire.reader.ResponseAccumulator` happens to satisfy it. **The dependency runs upward; the
knowledge runs downward.** A transport that can name a frame will eventually parse one, and then
segmentation handling and framing live in two places. This edge is banned by name in the
layering test rather than by layer arithmetic, because `wire` is *below* `transport` and the
layer rule alone would permit it.

### 3. There is no public `send()`

Bytes reach a socket only through a `Txn` capability token that is (a) obtainable only from an
async context manager, (b) single-use, and (c) exclusive per connection.

This exists because of one measurement: on this hardware, two requests written before the first
response is read produce **one** response, for the **last** request, with end code `0x0000`. On
3E there is no serial number to catch it. The corruption is undetectable at the protocol level,
so it is made **inexpressible** at the API level instead. The simulator has a
`coalescing_answers_only_last` pathology switch, and removing the gate makes that test fail.

## Where each concern lives, and why there

**`L` has exactly one expression in the package**, in `wire/frames.py`, guarded three ways: an
AST test that no other assignment is named `L` or `length` there or in `commands/`; a property
test that `cmd.payload_len(ctx) == len(cmd.encode(ctx))` for every command × codec × frame; and
a property test that `len(build(...)) == prefix_units(codec) + L`.

Three guards for one addition, because the failure is asymmetric: understating `L` returns
`0xC061` and the connection recovers, while **overstating it produces no response at all** and
is indistinguishable from a dead PLC.

**Every command carries `encode` and `decode` on the same frozen object**, so a request and its
decoder cannot drift, plus `validate()` (pure, no I/O, unit-testable, and skippable in a bound
plan's hot path), `mutates` (abstract — a subclass that omits it is a type error) and `CITES`
(at least one, asserted by a test).

`mutates` is what drives the `SlmpNotSentError` / `SlmpOutcomeUnknownError` split, so a new
command cannot forget the distinction between "provably did not happen" and "may have happened".

**The binary/ASCII dword word-order flip is a property of the codec, not of the value.**
`BINARY.number(v, bits=32) == struct.pack("<I", v)` and
`ASCII.number(v, bits=32) == f"{v:08X}".encode()`. In binary that is low word first; in ASCII it
is high word first; both are the same number, and each falls out of that codec's own endianness
rule. So `f32` has exactly one implementation in the library and no byte-swapping helper exists
anywhere.

**Bit-unit lengths are computed per codec and never by doubling.** `bit_data_len(n)` is `n` for
ASCII and `ceil(n/2)` for binary, so `ascii_len(n) == 2*binary_len(n) - (n % 2)`. That odd-count
term is the one place the "ASCII is twice binary" rule breaks.

**Reads are length-driven and the receive stamp is on the last chunk.** `ResponseAccumulator` is
pure and incremental, so TCP segmentation is deterministic in CI: every golden frame is fed one
byte at a time, split exactly at 1460, and through 200 seeded random splits, and all must yield
an identical `RawResponse`. Stamping the *first* chunk would report 11.0 ms for a 14.0 ms
transaction.

**Nothing in the library calls `logging`.** Observability is typed events and transaction
records; `observability.attach_logging()` is the single bridge, and an AST test asserts the
`logging` module is not named anywhere else. A log call inside the transport lands in the
latency number it describes.

**Reconnection is never implicit.** A failed connection goes sticky `FAILED` with the socket
closed. `Supervisor(client, policy=...)` is a separate object whose `policy` argument has no
default, and transactions during a reconnect raise immediately rather than waiting.

## The package facade

`aslmp/__init__.py` is a name table: 189 public names mapped to the module each lives in,
resolved by a module-level `__getattr__` and written back into the module globals on first use,
so the second lookup is an ordinary attribute access. A `TYPE_CHECKING` block gives `mypy`,
`pyright` and IDEs ordinary imports.

`__all__` is the stability contract of the design: two-minor-version deprecation notice, no
removals in a minor.

## Things deliberately absent

- **No `generic` profile.** An unrecognised model code raises. `X`/`Y` are octal on iQ-F and
  hexadecimal on iQ-R, both CPUs answer `0x0000`, and a radix guess is a silent wrong-register
  bug that no amount of downstream care can catch.
- **No `Encoding.AUTO`, no `xy_notation` parameter, no auto-detection of anything.** Transport,
  frame and encoding are GX Works3 connection-entry facts. They carry factory defaults as
  *declared constants* a caller overrides; a default is not a probe.
- **No `OverrunPolicy.SKIP`.** Silently dropping a cycle to catch up is the scheduling
  equivalent of returning a stale value.
- **No mutable blocks and no `read_*_into`.** They buy a rounding error against a 7 ms transport
  in exchange for a mutable-aliasing hazard.
- **No `read_block(type[B])` door.** `bind()` returns a `BlockPlan[B]` and `read_block` accepts
  nothing else, so the unvalidated revalidate-every-cycle path is not the shortest thing to type
  in a hot loop.
- **No monitor emulation.** `0x0801` / `0x0802` are capability-gated and refused pre-transport on
  iQ-F, never substituted with a `0x0403`.
