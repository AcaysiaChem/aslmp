<!--
Thank you. CONTRIBUTING.md has the long version of everything below; this is the short
list that gets a change merged. Delete the sections that do not apply, but do not delete
the provenance section if your diff touches a number.
-->

## What this changes, and why

<!-- One paragraph. If it is a fix, say what the wrong behaviour was, not just what the
     new behaviour is. -->

## Checks

Run all three locally — CI runs them on 3.11, 3.12 and 3.13 across Linux, Windows and
macOS, and the matrix exists because a bug in this library was visible on exactly one of
those nine cells.

- [ ] `python -m ruff check src tests tools bench`
- [ ] `python -m mypy`
- [ ] `python -m pytest tests -q`

## Does this touch a hardware claim?

A hardware claim is any number, limit, range, end code, capability or behaviour that is
asserted about real silicon — in a `.tsv`, in a docstring, in a doc, in a comment, or in
a test's expected value. **This project does not accept one without its conditions.**
Two published claims have been withdrawn here, and both were prose figures whose
conditions were never written down.

If your diff has one, fill this in; if it does not, write "no hardware claim" and move on.

- **CPU model:**
- **Firmware version:**
- **Date measured (ISO):**
- **Host:** <!-- where the client ran; an address, a name, or both -->
- **Link:** <!-- wired / Wi-Fi, with median RTT if known -->
- **n:** <!-- samples behind the figure, or "not a sample-based claim" -->

- [ ] The claim is expressed as a `Measurement(...)` / `Evidence.measured(...)`, not as a
      bare string, so the provenance and the source cannot drift apart.
- [ ] Provenance is `LIVE` only where silicon was actually observed. A manual reading is
      `MANUAL`; a value carried across from a sibling model is `INFERRED`, and says so.
- [ ] If this converts an unverified row to a measured one, `docs/unverified.md` was
      updated in the same commit. That page is billed as complete and a test enforces it.

## Everything else

- [ ] No new runtime dependency. The package has zero, deliberately: the wheel has to
      install on a Jetson's aarch64 and on a plant PC with no compiler.
- [ ] Nothing retries, clamps, substitutes a default, or returns a stale value. An
      AST-level test enforces this over the whole package; if you had to work around it,
      say so here rather than widening the exemption.
- [ ] No new path from the CLI to remote RUN / STOP / PAUSE / RESET / Latch Clear. A
      shell history is not an interlock, and a test walks the AST of `aslmp/tools` to
      prove there is no such path.
- [ ] Public surface changes are reflected in `aslmp.__all__` and `CHANGELOG.md`.
- [ ] If this makes an unverified path look verified, or reads as a promise about safety,
      certification or conformance, it does not go in. This library is not a safety
      system, holds no conformance certification, and has no affiliation with Mitsubishi
      Electric or the CC-Link Partner Association.

By opening this pull request you are offering the contribution under the project's
Apache-2.0 licence (see section 5 of `LICENSE`).
