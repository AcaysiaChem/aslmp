# What ships unverified

Everything in this file is **implemented, gated, and reasoned from Mitsubishi documents rather
than from hardware**. None of it has been sent to a real CPU by us. It is listed here, in
`aslmp capabilities`, in each module's docstring, and in the profile as
`Evidence(provenance=MANUAL)` or `INFERRED`.

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

Every device range, radix, limit and capability in `profiles/iq_r.py`, `profiles/q.py` and
`profiles/l.py` is read out of a manual. We have never connected to one.

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

## Every remote-control command

`0x1001` Run, `0x1002` Stop, `0x1003` Pause, `0x1005` Latch Clear, `0x1006` Reset.

**We deliberately never sent them.** This CPU's memory-card error once left it refusing a remote
RUN and needing a physical power cycle, and a benchmark is not worth a machine that will not
start.

Consequences carried in the code:

- the whole surface is behind `Plc(allow_remote_control=True)`, and the CLI has no path to it at
  all;
- `verify=True` is the **default** on run/stop/pause, because Mitsubishi documents Remote RUN as
  completing normally with the switch in STOP while the CPU does not run. Returning that
  `0x0000` as success would be a silent lie, so the library reads SD203 back and raises
  `SlmpRemoteStateNotReachedError` if the state was not reached. That second round trip is the
  correct price;
- `reset()` expects an **absent** response and returns a `ResetOutcome` rather than raising on
  silence;
- ambiguity `A-REMOTE-FIXED`: the `1002`/`1005`/`1006` two-byte fixed field is `01 00` in
  SLMP-REF and `00 00` in JY997D56001. The iQ-F profile ships `00 00`, the SLMP-REF families ship
  `01 00`, and neither has been sent;
- ambiguity `A-CLEAR-MODE`: JY997D56001 p.105 says only `00H` is valid and prints `02H` on the
  same page. The iQ-F profile refuses anything but `NONE`.

**Probe:** an FX5U in STOP with a scratch program. Send `1002` with `00 00`, record the end code,
repeat with `01 00`.

## `0x0406` / `0x1406` block access

Never exercised on the bench. The encoder, the `BlockRule` limits (`120` blocks, `≤960` total
points; iQ-F `1406` total `≤760`) and the word-blocks-before-bit-blocks wire order are all
manual-derived. `aslmp bench --suite block` and `bench/access_patterns.py` both include a block
row specifically so that the first person with hardware gets a number.

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

Even the measured half of this library is one CPU, one firmware, one entry, one Wi-Fi link, two
afternoons. A firmware update could invalidate the coalescing behaviour, the accept-then-FIN, the
`0xC05C` mapping or 4E acceptance, and **nothing here would tell us.**

If you run this against a CPU we have not seen, `aslmp verify-ranges`, `aslmp capabilities` and
`aslmp.testing.run_conformance` produce exactly the artifact that would fix a row in this file.
