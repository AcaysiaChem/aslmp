# Security

Two things live in this file, and the first matters more than the second: what the **protocol**
gives you, which is nothing, and how to report a bug in **this library**.

---

## 1. SLMP has no security model

SLMP, as it is published, has:

- **no authentication** — nothing in the exchange establishes who is talking, and no credential
  is involved at any point;
- **no encryption** — every request and response crosses the wire in the clear, including a
  remote password;
- **no integrity protection** — nothing above what Ethernet and TCP already do, so a frame
  altered in flight is a frame the CPU obeys;
- **no authorization** — the protocol has no notion of a read-only peer.

**Anyone who can reach the configured TCP or UDP port can read and write any device the CPU
exposes.** Not only `D` registers: `M` bits and **`Y` outputs** too — and on a CPU whose
parameters allow it, Remote RUN, STOP and PAUSE. Reaching the port *is* the authorization.

This is a property of the protocol as Mitsubishi and the CC-Link Partner Association define it,
not a defect in this library, and **no client can fix it**. A Python library that added
encryption or authentication would only be talking to itself: the CPU at the other end speaks
SLMP and nothing else. There is no mitigation here to look for. A Python developer reaching for
a "PLC client" is usually carrying assumptions from HTTP — TLS somewhere, a token somewhere,
an audit log somewhere. None of those exist on this wire.

This is not a hypothetical reading of the specification. **CVE-2025-7405** records missing
authentication for a critical function on MELSEC iQ-F CPU modules, and **CVE-2025-7731** records
cleartext transmission of authentication information in SLMP on the same family — an attacker on
the segment captures the traffic, reads or writes device values with the credentials, and may
stop the program. The published countermeasures are to tunnel SLMP inside a VPN and to restrict
physical access to the LAN: network measures, because the protocol is what it is. Earlier entries in
the surrounding Mitsubishi Ethernet surface, each verified against NVD on 2026-09-24 rather
than cited as a bare number:

| CVE | CVSS | What it is |
| --- | --- | --- |
| CVE-2020-5594 | 9.8 | Cleartext transmission between CPU modules and GX Works3/GX Works2, across iQ-R, iQ-F, Q, L and FX. |
| CVE-2020-16226 | 9.8 | Predictable TCP sequence numbers allow a legitimate device to be impersonated and arbitrary commands executed (CWE-342). |
| CVE-2023-4699 | 10.0 | Missing authentication for a critical function: an unauthenticated attacker executes commands with a crafted packet (CWE-306, CWE-345). |

The middle one deserves more than its number. It means the peer restriction described below
is weaker than it looks: an attacker does not necessarily have to *be* the configured host to
be treated as it. Take that as the reason the connection-entry list is called a coarse
allowlist here and not an access control.

This library defends against none of them. It is a client, and it can only speak the protocol
the CPU speaks.

### What a deployment can do about it

None of these are features of `aslmp`. They are things you do to the network and to the CPU,
which is the only place the problem is solvable at all. They are also a starting list written
by people who build a client, not a security assessment of your installation and not a complete
one — whoever is responsible for that network makes that call, not this file:

- **Segment the control network.** No route from a business VLAN, no NAT or port forward, never
  reachable from the internet. Treat an SLMP port exactly as you would an unauthenticated telnet
  session on a machine that moves physical things.
- **Pin a firewall rule to the client's address.** A layer-3 source address is spoofable and
  this is not authentication; it is a smaller surface, which is still worth having.
- **Use the CPU's connection-entry list as a coarse allowlist.** On iQ-F, GX Works3 refuses to
  save a UDP SLMP entry without a destination IP, so a UDP entry serves exactly one peer
  address; and one TCP entry serves exactly one connection at a time — a second connection
  completes its handshake and is then dropped by the CPU (measured 2026-09-06, FX5U-32MT/DS fw
  1.065, from the laptop at 192.168.10.41 over Wi-Fi at ~7 ms median RTT; see the README). There
  are at most eight entries on the CPU, shared across SLMP, MELSOFT, socket and
  predefined-protocol connections. This bounds *how many* peers can be served and, for UDP,
  *which address* is served. It identifies nobody, and address spoofing defeats the UDP form of
  it.
- **Leave Remote Reset disabled** in the CPU parameters, where Mitsubishi leaves it.
- **Keep `allow_remote_control=False`**, which is this library's default. Be clear about what
  that buys: it stops *your own code* from issuing RUN, STOP, PAUSE, RESET or Latch Clear by
  accident. It does nothing about anyone else on the wire. `aslmp`'s command line has no path to
  those commands at all, and a test asserts it by walking the CLI's AST.
- **Use the CPU's own key switch.** Mitsubishi documents Remote RUN as returning end code
  `0x0000` while a CPU whose switch is in STOP does not run. We could not reproduce that on a
  bench whose switch is in RUN, so in this repository it is a manual claim and not a measurement
  — [`docs/unverified.md`](docs/unverified.md).
- **Assume a remote STOP destroys data.** On our bench a `0x1002` Remote STOP cleared
  non-latched device memory: `D100`/`D101` read back as zero and the free-running counter `D8`
  restarted from zero (FX5U-32MT/DS fw 1.065, 2026-09-07, laptop at 192.168.10.41 over Wi-Fi,
  TCP entry 5004; [`docs/hardware.md`](docs/hardware.md) §15). An attacker who can reach the
  port and whose CPU permits remote control does not merely halt the machine — they take the
  contents of D-memory with them.

### The remote password is not a secret channel

The `0x1630` / `0x1631` remote-password commands send the password as **literal characters** in
both binary and ASCII coding — not hashed, not encoded, not transformed (SH(NA)-080956ENG-M
§5.10). Anyone with a packet capture between the client and the PLC has the password. It gates
access to the port; it does not protect itself. This is the subject of **CVE-2025-7731**.
Neither command has ever been sent to our bench, and they ship labelled unverified in
[`docs/unverified.md`](docs/unverified.md).

Two consequences:

- **Do not reuse a password that means anything anywhere else**, and do not treat a remote
  password as a reason to relax any of the network measures above.
- **It can reach your logs.** An `aslmp` diagnostic prints the head of the request frame — the
  first 24 bytes. On a 3E binary unlock the password's characters begin at byte 17, so the first
  **seven** of them appear in a traceback that somebody may paste into an issue. (This is from
  the package's own encoder, not from a CPU.) The command's `describe()` deliberately prints the
  password's length and never its characters; the hexdump is the hole. Redact the `sent` and
  `received` lines of any diagnostic that came from an unlock.

### What this library does and does not do on its own

- It opens **no connection you did not configure**: no telemetry, no update check, no
  phone-home. Importing the package opens no socket, starts no thread and reads no file.
- It has **zero runtime dependencies**, so nothing in its transitive graph does either, because
  there is no transitive graph.
- It **never retries, reconnects implicitly, clamps a value or substitutes a default**, so it
  will not repeat a write you did not intend. A lint-level AST test enforces that over the whole
  package.
- It is **not a safety system, and it has no certification of any kind** — including protocol
  conformance certification. The "conformance simulator" in this repository (`aslmp serve`,
  `aslmp.testing`) is a test double that reproduces what *our one bench CPU* was observed to do.
  It is not a certification suite and nobody has assessed it.

### Supply chain

**There is no `aslmp` on PyPI yet.** Until the first release, a package published under that
name is not this project. Install from a checkout or a git URL — see the README.

---

## 2. Reporting a vulnerability in `aslmp`

Please report privately first, before opening a public issue.

- **Preferred — GitHub private vulnerability reporting.** On
  <https://github.com/AcaysiaChem/aslmp>, the *Security* tab → *Report a vulnerability*. It
  reaches the maintainers privately and needs no new inbox on either side.
- **Fallback — dev@acaysia.com**, the address in this package's metadata.

**This is a small project.** There is no security team, no bug bounty, and **no response-time
commitment** — that is an honest description of the maintenance capacity, not a policy. We would
rather say it than print a service level nobody agreed to. You will hear back when a maintainer
reads the report. If you believe something is being actively exploited, say so in the first
line.

### What makes a report easy to act on

- The output of `aslmp --version`, your Python version and OS, and the profile key, transport
  and frame type in use.
- **A reproduction that needs no PLC.** Raw bytes, or a test against the bundled simulator
  (`aslmp serve`), is the most useful report this project can receive; byte vectors are how
  almost every bug here has been fixed.
- What you believe the impact is, and on whose equipment.

Please do **not** test against a production PLC, or against any equipment that is not your own
bench.

### Roughly in scope

Anything where `aslmp` itself is the problem:

- a decode that returns a wrong value while reporting success — the worst bug class this library
  has, and the one most of its design is aimed at;
- an address, length or plausibility check that can be bypassed, so that a write lands somewhere
  the caller did not name;
- a remote-control command reachable without `allow_remote_control=True`, or reachable from the
  command line at all;
- a hostile or malformed response that hangs the client, allocates without bound, or escapes the
  documented exception tree;
- a secret that ends up somewhere it should not — the hexdump note above is an example of the
  shape.

### Out of scope

- **"SLMP has no authentication."** That is section 1: real, serious, and not ours to fix.
- The paths listed in [`docs/unverified.md`](docs/unverified.md) being unverified. The label is
  the disclosure; a report that they are unverified tells us what that file already says.
- A PLC exposed to a hostile network. The deployment is the finding there, and section 1 is
  where it is addressed.
- Occupying a CPU's connection entry, or any other denial of service that consists of sending a
  PLC traffic the protocol permits. That is a property of the CPU's eight-entry limit and it is
  documented in the README.

---

This file describes risk and reporting. It is not a warranty, a guarantee of any behaviour, or a
promise of support. The software is licensed under Apache-2.0, and sections 7 and 8 of
[`LICENSE`](LICENSE) are what govern warranty and liability.
