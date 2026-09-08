# What ships unverified

Everything in this file is **implemented, gated, and reasoned from Mitsubishi documents rather
than from hardware**. It is listed here, in `aslmp capabilities`, in each module's docstring, and
in the profile as `Evidence(provenance=MANUAL)` or `INFERRED`.

One section — remote control — is now **partly** verified, and it is written up that way rather
than moved out wholesale: three of the five commands have been sent to a real CPU, two have not,
and the specific behaviour that justifies a safety default has not been reproduced. A section
that goes from "all manual" to "mostly measured" is exactly where a reader stops checking, so
each claim in it says which it is.

**This list is billed as complete, so completeness is now checked rather than asserted.** It
was not complete: until 2026-09-07 it omitted four selectable profiles, three of them iQ-F.
`tests/unit/test_citations.py::test_every_shipped_profile_is_accounted_for_in_unverified_md`
fails if a profile key exists that this file does not name, because a page whose whole value is
"here is what we do not know" is worth nothing if adding a profile can quietly leave it behind.

The label is honest reporting. **It is not protection.** The conformance simulator gives every
one of these paths CI coverage, but the simulator was written from the same manuals as the
client, so a misread section passes on both sides of the test. Read this list as "we believe
this and we could be wrong", not as "this has been checked in another way".

To see it from the command line:

```
aslmp capabilities melsec:iq-f/fx5u --unverified-only
aslmp capabilities --all
aslmp ambiguities
```

---

## Seven of the eight shipped profiles, one of which you can select by accident

`aslmp.profiles.KEYS` has eight entries. **One** of them has ever been connected to:

| profile key | CPU | evidence |
| --- | --- | --- |
| `melsec:iq-f/fx5u` | FX5U-32MT/DS fw 1.065 | **measured**, and only that model on that firmware |
| `melsec:iq-f/fx5uc` | FX5UC | **never connected** — manual only |
| `melsec:iq-f/fx5uj` | FX5UJ | **never connected** — manual only |
| `melsec:iq-f/fx5s` | FX5S | **never connected** — manual only |
| `melsec:iq-r` | iQ-R R04–R120 | **never connected** — manual only |
| `melsec:iq-r/r00` | iQ-R R00 / R01 / R02 | **never connected** — manual only |
| `melsec:q` | MELSEC-Q | **never connected**, and no range table at all |
| `melsec:l` | MELSEC-L | **never connected**, and no range table at all |

This file used to name only iQ-R, Q and L, which left out the three that a reader is most
likely to assume are covered: **`fx5uc`, `fx5uj` and `fx5s` are iQ-F**, they share this
package's iQ-F builders, and every FX5U measurement in `hardware.md` was taken on a different
piece of silicon from any of them. Same family is not same CPU. Concretely:

- **`melsec:iq-f/fx5uc`** carries the FX5U's device table verbatim, from the same JY997D55401
  section, plus the monitor and long-specification refusals carried across from the FX5U's
  measured `0xC059`. Carrying a *refusal* across is the safe direction — it costs a typed error
  naming `capability_overrides` where a wrong "supported" costs a round trip — but nothing here
  was observed on an FX5UC.
- **`melsec:iq-f/fx5uj`** overrides nineteen device ranges with the FX5UJ's smaller ones
  (`M` to 7679, `F` to 127, `ST` to 15) and **carries the other nine over from the FX5U**:
  `X`, `Y`, `S`, `SM`, `SD`, `Z`, `LZ`, `R` and `D` ship as `Provenance.INFERRED` because the
  sources read do not state them separately for an FX5UJ. Those nine are the first thing to
  point `aslmp verify-ranges` at.
- **`melsec:iq-f/fx5s`** takes the FX5U's device points as the tables read them, and four of
  its twelve model-code **names** (`0x4B55`–`0x4B58`) are our reading of a survey extract that
  cannot be right as printed — ambiguity `A-FX5S-MODEL-CODES`. The codes are the manual's; a
  wrong name shows up in `CpuIdentity.model` and cannot make `connect()` accept the wrong CPU,
  because the code is what is compared.
- **`melsec:iq-r/r00`** is the narrower range table for R00/R01/R02, which have 8192 `X`/`Y`/`M`
  points where the family profile says 12288. Both profiles claim those three model codes, so
  `connect()` accepts either declaration and **nothing chooses between them**: pick the wrong
  one and `M9000` passes validation and comes back `0xC056`. It is reachable by name only, and
  `aslmp identify` offers it.

**On all seven, every capability is `MANUAL` or `INFERRED` — including the five the FX5U has
measured**: batch access, random access, self test, read type name and 4E acceptance. Nothing
below repeats them per profile, because "we have never connected to this CPU" already says it,
and a per-capability list would suggest the omissions were the interesting part.

**With two exceptions, on the three iQ-F variants.** `fx5uc`, `fx5uj` and `fx5s` each report
**2 `LIVE` rows** from `provenance_counts()`: the monitor and long-device-specification
*refusals*, carried across with the FX5U's own measurement attached. Carrying a refusal is the
safe direction — a wrong "refused" costs a typed error naming `capability_overrides`, a wrong
"supported" costs a round trip and a `0xC059` — but it is an FX5U measurement wearing another
profile's label, and until 2026-09-07 all three module docstrings said every row was `MANUAL`,
which those two rows made untrue. They now say so.

The three iQ-F variants are also the three most likely to be selected by somebody who read the
bench numbers in `hardware.md` and assumed they transfer. They do not. `aslmp capabilities
<key>` prints the provenance of every row of whichever one you name, and
`profile.provenance_counts()` gives the totals — for `melsec:iq-f/fx5u` today that is 19 live,
67 manual and 3 inferred, which is the honest shape of even the measured profile.

---

## The whole ASCII path

All three data codes: `Encoding.ASCII_XY_HEX`, `Encoding.ASCII_XY_OCT`, and the binary/ASCII
differences that hang off them.

**Why we could not test it.** On an iQ-F, `Communication Data Code` is an **Own Node Setting for
the whole Ethernet port**, not a per-entry one. Switching it to ASCII takes down every binary
connection on the CPU at once, including the four the bench depends on. It is a parameter
change, not a purchase, so this is fixable by whoever has ten minutes and a spare CPU.

Specifically unverified:

- the mnemonic device code on the wire (`b"D*"`, `b"TN"`, `b"SS"`, `b"X***"`) rather than the hex
  of `0xA8`;
- the **field-order swap** — binary emits `[number][code]`, ASCII emits `[code][number]`;
- the odd-bit-count length rule: `bit_data_len(n)` is `n` for ASCII and `ceil(n/2)` for binary,
  so `ascii_len(n) == 2*binary_len(n) - (n % 2)`. This is the one place "ASCII is twice binary"
  breaks, and it is exactly the sort of thing that is right in a manual and wrong in silicon;
- **dword high-word-first in ASCII**, which falls out of `f"{v:08X}"` rather than being a
  separate rule;
- whether lower-case hexadecimal is accepted on receive;
- `ASCII_XY_OCT` rendering: `Y45` on iQ-F as `"000045"` rather than `"000025"`;
- the halved point ceilings (`0401` word 480, bit 1792; `0403` effectively 96).

Registered as ambiguity `A-ASCII-*`. **Probe:** configure a second GX Works3 connection entry
with Communication Data Code = ASCII (X,Y OCT), and a third with ASCII (X,Y HEX).

## The iQ-R, Q and L profiles

Every radix, limit and capability in `profiles/iq_r.py`, `profiles/q.py` and `profiles/l.py` is
read out of a manual. We have never connected to one.

**Device ranges are the exception, and this file described them wrongly until 2026-09-07.** It
said every device range in all three was manual-derived. That is true of iQ-R, whose ranges come
from SH(NA)-081263ENG's default parameter assignment. It is **not** true of Q and L, which ship
**no device-range table at all** — `ranges_iqf.tsv` and `ranges_iqr.tsv` are the only two that
exist. On those two profiles every family the generic device table lists with the short device
specification is declared *present with no static range*, so `check_range` validates that the
device exists and does **not** validate the span; the eleven long-specification families and the
SFC block device are declared absent with the reason. `with_ranges()` is how a user supplies the
real numbers.

That gap is deliberate and is worth more than a filled-in table would be. Inventing Q ranges
from the iQ-R figures would have looked exactly like knowledge and would have refused a
legitimate `D14000` on a Q26UDEHCPU while citing a manual section that says no such thing. But
"manual-derived ranges" and "no ranges" are different promises to a user, and only one of them
was on this page.

Notably unverified there:

- **`SpecFormat.LONG`** — subcommand `0x0002`/`0x0003`, the 4-byte device number with a 2-byte
  device code. Our FX5U refuses it outright with `0xC059`, so we cannot even test the encoder
  against a CPU that would accept it. Ambiguity `A-LONG-SPEC-BYTE-ORDER`: the 2-byte device code
  is `9C 00` per SLMP-REF and MC-REF, and `00 9C` in a JY997D56001 figure.
- **`0x0801` / `0x0802` positively.** We have measured that they are *refused* on iQ-F. We have
  never seen one succeed. The `0x0802`-without-`0x0801` → `0xC05D` behaviour, the clearing of a
  registration on restart, and the two-clients-clobber-each-other behaviour are all manual-derived.
- **`R` and `ZR` default to zero points.** On an iQ-R every range is repartitionable in GX
  Works3, and out of the box these are unallocated — so **SLMP-REF's own `ZR16384` example fails
  on a factory iQ-R**. The shipped table will be wrong for someone on day one; that is what
  `aslmp verify-ranges` and `profile.with_ranges()` are for.
- **`ZR`'s radix**, hexadecimal per SLMP-REF and decimal per the FX5 documentation. Ambiguity
  `A-ZR-RADIX`; ZR is absent on iQ-F so we cannot settle it here.
- **The `0x4000`–`0x4FFF` CPU end codes.** The detail lives in SH(NA)-081264ENG, which we have
  not read. Those codes currently fall through to `SlmpCpuError` with a generic description.

## Remote control — RUN, STOP and PAUSE are now measured; RESET is not

**This section shrank on 2026-09-07.** `0x1001` Run, `0x1002` Stop and `0x1003` Pause have been
sent to the real CPU and checked against its own free-running scan counter, and the numbers are
in [`hardware.md`](hardware.md) section 15. The reason we had abstained — this CPU's memory-card
error once left it declining a remote RUN and needing a physical power cycle — no longer holds:
the card is out, and GX Works3 drove remote STOP and RUN repeatedly through the same session.

### Still unverified, and staying that way

- **`0x1006` Remote Reset has never been sent.** It is the one command whose expected outcome is
  an *absent* response, and the one that reboots the CPU. `reset()` returns a `ResetOutcome`
  rather than raising on silence, and that whole path is simulator-tested only.
- **`0x1005` Latch Clear has never been sent**, and clearing a latch range on a machine we
  cannot see is not a measurement worth taking.

### The claim that justifies the `verify=True` default is still a manual claim

`verify=True` is the **default** on run/stop/pause because Mitsubishi documents Remote RUN as
completing with end code `0x0000` while the switch is in STOP and the CPU does not run
(SH(NA)-080956ENG-M p.131). Returning that `0x0000` as success would be a silent lie, so the
library reads SD203 back and raises `SlmpRemoteStateNotReachedError` if the state was not
reached.

**We could not force that condition and therefore have not measured it.** The bench CPU's switch
is in RUN, and with the switch there, Remote RUN is truthful: sent with `verify=False` it
returned `0x0000` and the scan counter advanced. Reproducing the lie needs physical access to
the switch, so it remains a claim we take from the manual and implement as if true.

One corroboration that is **not** our measurement and must not be read as one: the reviewer who
challenged these numbers independently saw **GX Works3 report "The RUN operation has been
completed" while P.RUN stayed dark and the CPU never started scanning** — the same lie, through
Mitsubishi's own tool. It raises our confidence in the manual's warning. It is somebody else's
observation, taken through a GUI we did not instrument, with no frame capture and no end code
written down, and it settles nothing.

### The remote-control ambiguities, and what sending the commands did and did not settle

- ambiguity `A-REMOTE-FIXED`: the `1002`/`1005`/`1006` two-byte fixed field is `01 00` in
  SLMP-REF and `00 00` in JY997D56001. The iQ-F profile ships `00 00` and the SLMP-REF families
  ship `01 00`. The iQ-F value **has now been sent and was accepted**, so `00 00` is known to
  work on this CPU — but `01 00` was never sent, so we cannot tell *required* from *merely
  accepted*, and the CPU may well ignore the field. **The row stays open**, because a
  differential test is the only thing that closes it and we did not run one.
- ambiguity `A-CLEAR-MODE`: JY997D56001 p.105 says only `00H` is valid and prints `02H` on the
  same page. The iQ-F profile refuses anything but `NONE`. Every Remote RUN we sent carried
  `00H`, so that value is measured; `01H` and `02H` remain unsent and refused.

**Probe for both:** an FX5U with a scratch program. Send `1002` with `00 00`, record the end
code, repeat with `01 00`; send `1001` with clear mode `01H` and `02H` and check whether devices
actually cleared.

### And the gate stays exactly where it was

Nothing above widens the surface. The whole of it is still behind
`Plc(allow_remote_control=True)`, the CLI still has no path to it, and
`tests/unit/test_tools.py` still proves that by walking the AST of every `aslmp/tools` module.
Three verified commands do not make a fourth safe, and they do not make any of them safe to
reach from a shell history.

## `0x0406` / `0x1406` block access

Never exercised on the bench. The encoder, the `BlockRule` limits (`120` blocks, `≤960` total
points; iQ-F `1406` total `≤760`) and the word-blocks-before-bit-blocks wire order are all
manual-derived. `aslmp bench --suite block` and `bench/access_patterns.py` both include a block
row specifically so that the first person with hardware gets a number.

## `0x1617` Clear Error

Never sent, on any CPU. It is `Provenance.MANUAL` in every profile that offers it, including
the FX5U's — which makes it the one FX5U capability that is neither measured nor remote-control
gated, and it was missing from this page until 2026-09-07.

Its **subcommand is an open ambiguity**, `A-1617-SUBCOMMAND`: the FX5 manual's command-list
table says `0001` and its own detail section says `0000`, on the same document. The library
sends `0000` — the value every other non-device command in the catalogue uses — and refuses
anything else client-side. Nothing here can tell which the CPU wants, because the command needs
a CPU that has a clearable error and the bench CPU has been in RUN with none; the one fault it
did have, a memory-card error, was cleared by taking the card out and power-cycling, not over
SLMP.

**Probe:** on a CPU with a clearable error, send `1617` with subcommand `0000`, record the end
code, then repeat with `0001`.

## `0x1630` / `0x1631` remote password

Never sent. The password is transmitted as literal characters in both codings — which is worth
knowing regardless of whether the frame layout is right.

## `0x2101` Ondemand receipt

An unsolicited frame from the PLC. It needs a ladder instruction to provoke and we did not write
one. The library recognises it on receive and raises `SlmpUnsolicitedFrameError` rather than
parsing it as somebody's response; that path is simulator-tested only.

## 4E beyond one transaction in flight

Our FX5U **accepts 4E and echoes the serial correctly** on an entry configured for 3E, which two
Mitsubishi manuals say is impossible. We measured n=1 depth on TCP and up to 64 deep on UDP.

What is unverified is everything about 4E on iQ-F *as a supported feature*: whether the
behaviour is intentional, whether it survives a firmware update, and whether it is safe to build
on. The library permits 4E and does not default to it.

**Question for MEAU:** is 4E on the FX5 built-in port intentional? If yes, defaulting to it on
iQ-F would turn the measured TCP coalescing corruption into a loud `SlmpSerialMismatchError`, and
is the largest single correctness win available to this design.

## Everything, at n=1

Even the measured half of this library is one CPU, one firmware, two hosts and two afternoons.
A firmware update could invalidate the coalescing behaviour, the accept-then-FIN, the `0xC05C`
mapping or 4E acceptance, and **nothing here would tell us.**

It is n=1 in a second dimension too, and that one has already bitten us. Until 2026-09-07 every
latency number here came from **one Wi-Fi link**, and the transport conclusion drawn from it —
"TCP wins the tail" — did not survive a wired retest ([`hardware.md`](hardware.md) section 5).
The default did not change, but its published justification was wrong for a day. Assume the same
about anything here that a second link has not seen: the entry-release window, the queue depth's
exact shape, and every absolute millisecond in this repository.

If you run this against a CPU we have not seen, `aslmp verify-ranges`, `aslmp capabilities` and
`aslmp.testing.run_conformance` produce exactly the artifact that would fix a row in this file.
