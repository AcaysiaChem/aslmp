# The `aslmp` command line

One console script, eleven subcommands. `aslmp --help` lists them; `aslmp <cmd> --help` explains
one.

**Subcommands are imported lazily and that is tested.** `aslmp --help` does not import
`asyncio`, `socket` or the client — the top-level help is a table of strings rather than an
`argparse` subparser tree, because building a subparser tree means importing every subcommand to
ask it for its arguments.

**Exit codes.** `0` the command did what it was asked; `1` the PLC, the network or the profile
said no, and the diagnostic is on stderr; `2` the arguments were wrong and nothing was sent.

**No subcommand can issue a remote-control command.** Remote RUN, STOP, PAUSE, LATCH CLEAR and
RESET are gated in the library behind `Plc(allow_remote_control=True)`, and there is no flag
here that reaches them. A shell history is not an interlock. `tests/unit/test_tools.py` asserts
it from the syntax tree.

## Connection arguments

Every subcommand that opens a socket takes the same set, with the same defaults as `Plc`:

| flag | default | what it is |
| --- | --- | --- |
| `HOST` | required | the PLC |
| `--port` | 5000 | the connection entry's port |
| `--profile` | **required** | e.g. `melsec:iq-f/fx5u`. `aslmp identify` prints it |
| `--transport` | `tcp` | a GX Works3 connection-entry fact |
| `--frame` | `3E` | a GX Works3 connection-entry fact |
| `--encoding` | `binary` | the **port-wide** Communication Data Code |
| `--link` | `cpu` | `cpu` = built-in port, `enet` = FX5-ENET module. Selects the limit table |
| `--timeout` | 3.0 | client-side deadline, seconds |
| `--capture-frames` | off | keep request and response bytes on every transaction |

None of these is auto-detected, and none is retried in another value. Getting `--encoding` wrong
costs one handshake round trip and produces a diagnostic naming `CODING_MISMATCH` first; getting
it silently corrected would cost an afternoon.

---

## `aslmp identify HOST`

The one command that does **not** take `--profile`, because it is how you find out which one to
pass. `0x0619` and `0x0101` carry no device address, so the profile used to send them cannot
change a byte of the request.

```
$ aslmp identify 192.168.10.250 --port 5002
model             FX5U-32MT/DS
model code        0x4A49
family            iq-f
raw 0x0101 field  46 58 35 55 2D 33 32 4D 54 2F 44 53 20 20 20 20
profile           melsec:iq-f/fx5u

  Plc('192.168.10.250', 5002, profile='melsec:iq-f/fx5u')
```

An unrecognised model code raises rather than guessing a family from the code's high byte.

## `aslmp probe HOST --profile KEY`

Connect, prove liveness, identify, and time some further self tests.

Prints what the handshake proves, which is more than people expect from a connect: the entry was
free (a busy entry accepts the TCP connection and then FINs), the data code matches (a mismatch
is `0xC06F`, reported as **silence**), the frame format is accepted, the route bytes are right,
and the CPU answered inside the deadline just now.

`--samples N` adds N further `0x0619` round trips and prints min/median/max. That is a smoke
test, not a measurement — use `aslmp bench` for a distribution with a control.

## `aslmp read HOST ADDRESS --profile KEY`

```
$ aslmp read 192.168.10.250 D0 --as f32 --timing --profile melsec:iq-f/fx5u
60.0
  6.92 ms wire, 1 chunk(s), command 0x0401 sub 0x0000, 15 bytes back
```

That transcript is real, and its 6.92 ms belongs to the laptop at 192.168.10.41 over **Wi-Fi**
(~7 ms median RTT) against FX5U-32MT/DS fw 1.065, 2026-09-06 — not to your link. `--timing`
prints what *your* round trip cost; the point of the flag is that you never have to take ours.

`--as` takes `bit i16 u16 i32 u32 f32 f64 str words bits` and is **required**, for the same
reason `--profile` is: a register carries no type on the wire, so there is no default that could
be right. It defaulted to `u16` for one revision, which made `aslmp read 192.168.10.250 D8`
print `54720` on the bench this library was built against — `D8` holds a `REAL` there, so that
number is the low half of a float's bit pattern and the CPU answers `0x0000` either way.
Required **in the parser**, so `aslmp read --help` prints `--as {bit,i16,...}` with no brackets;
until 2026-09-07 it was declared optional and refused afterwards, and the usage line said
`[--as {...}]` directly above a help string beginning "REQUIRED".
`--count` applies only to `words`, `bits` and `f32`; naming it with any other kind is a usage
error rather than a silently ignored flag. `--as str` requires `--length`, because a string's
word count is part of the request and cannot be inferred from the response.

Addresses are written the way GX Works3 shows them. On iQ-F **X and Y are octal**: `Y20` is the
17th output and goes on the wire as 16. `Y8` is refused, because it does not exist under octal
notation — and the CPU would accept it and answer `0x0000`.

`--word-order` overrides how a 32-bit value is assembled from two words. That is a PLC-program
convention, not a protocol fact; the default matches what a GX Works3 `EMOV` writes.

## `aslmp write HOST ADDRESS VALUE --profile KEY`

Device memory only. `--as` takes `bit i16 u16 i32 u32 f32 f64 str words`; a bit value is one of
`on/off`, `true/false`, `yes/no`, `1/0`; `--as words` takes a comma-separated list and sends one
`0x1401`.

The value is parsed **before the socket opens**, so a typo is a usage error and nothing was
sent. `--verify` reads the value back and raises `SlmpVerificationError` on a disagreement; it is
off by default because for a device write the CPU's `0x0000` is truthful.

## `aslmp cite [CODE...]`

The artifact you hand a Mitsubishi engineer.

```
$ aslmp cite 0x0403
0x0403  Device Read Random
  direction       request
  mutates         no -- a failed read has no outcome to be unknown about
  implemented by  ReadRandom
  source  SH(NA)-080956ENG-M 6.4 pp.53-61
  source  JY997D56001-K 4.3 pp.76-88
  source  FX5U-32MT/DS fw 1.065, 2026-09-06
```

`0x0403`, `403` and `0403` all mean the same thing: SLMP codes are printed in hexadecimal in
every Mitsubishi document and nowhere in decimal, so a bare `403` is read as hex.

`--end-code 0xC059` explains one end code, with what raises it and whether the row came from a
manual or from silicon. `--all` prints every command. `--manuals` lists every document this
package cites, with its revision — revisions matter, because two revisions of JY997D56001
disagree about the FX5 X/Y examples.

An unimplemented command code is a failure, not a nearest match: answering with a neighbouring
row prints the wrong manual page.

## `aslmp capabilities [PROFILE]`

What a profile allows, refuses and says nothing about, with the evidence for each row and its
provenance (`live` / `manual` / `inferred`).

`--unverified-only` filters to the rows nobody has ever sent to hardware — the honest reading of
what this library is guessing at. `--all` covers every shipped profile.

## `aslmp ambiguities`

Every place the sources contradict each other or the hardware, with five fields: the question, at
least two mutually exclusive readings, what this library actually does (which may be *refuse*),
why, and **the one experiment that would settle it**.

An ambiguity with no stated probe is an opinion, so the probe field is required by the type.
`--keys-only` for the list, `--key A-IQF-XY` for one.

## `aslmp verify-ranges HOST --profile KEY`

Probe a CPU for its real device-range boundaries and compare them with the shipped profile.

Static range tables go stale by construction — on iQ-R every range is repartitionable in GX
Works3 and `R`/`ZR` default to zero points — so this is the mitigation for a weakness the design
ships knowingly.

Each probe is one 1-point read, starting at the profile's declared boundary (so a table that is
right costs two reads) and bisecting when it is wrong. When the CPU has *more* than the table
says, the search doubles upward: the table is what is under test, so it is never used as a
ceiling. A full iQ-F sweep is a few hundred round trips, a few seconds.

`--python` prints a paste-ready `profile.with_ranges(...)` call. It is printed and never applied:
replacing a shipped table on the strength of one sweep would be exactly the silent substitution
the rest of the library refuses.

## `aslmp proxy --target HOST:PORT`

Sit between somebody else's SLMP client and your PLC. For when you need to see the wire and
Wireshark is not an option — a locked-down plant PC, no capture driver, an HMI you cannot modify.

**Forwards first, decodes afterwards, on a copy.** Bytes go out the far side the moment they
arrive. A proxy that parsed before forwarding would add its parse to the latency it is measuring
and would drop the frames nobody understands — which are the interesting ones. Each frame gets a
monotonic timestamp and a delta from the previous frame in that direction.

If decoding fails, that direction stops being annotated and says so once; forwarding is
untouched. There is no resynchronisation, because guessing where the next frame starts invents
transactions that never happened.

TCP only, and not out of laziness: a UDP SLMP entry on iQ-F is point-to-point and demands a
destination IP, so the CPU would answer only the proxy — and reconfiguring the entry to name the
proxy host means the client under test is no longer talking to the configuration you wanted to
observe.

Remember that the PLC serves **one** TCP connection per entry: while the proxy holds it, the real
client must point at the proxy.

## `aslmp bench HOST --profile KEY`

Latency distributions beside a same-session raw-socket control. See
[`benchmarking.md`](benchmarking.md).

There is no `--no-control`; the flag exists only so the refusal can name it.

## `aslmp serve`

Run the conformance simulator: a PLC-shaped socket reproducing what our FX5U actually does,
including the parts nobody would design on purpose — coalescing that answers only the last
request, an entry that accepts a second connection and FINs it, silence on a coding mismatch, and
an overstated length that hangs.

```
aslmp serve                      # the FX5U target, pathologies on, ephemeral ports
aslmp serve --port 5002          # a fixed port for the plain TCP binary 3E entry
aslmp serve --target pedantic    # the manuals with none of the bugs
aslmp serve --healthy            # our CPU without its pathologies
aslmp serve --transcript         # print every frame as it is served
```

Running a suite against `pedantic` and against `fx5u-32mt-ds` and diffing the two **is a
document**: it is the list of behaviours that exist only to accommodate real silicon. The
`r04cpu` target prints an unverified warning, because it is a reading rather than an observation.
