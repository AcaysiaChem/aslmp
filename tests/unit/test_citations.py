"""The provenance value types, and the integrity of every shipped data table.

Two things are being defended here.

The first is that a fact with no provenance cannot be represented: ``Citation`` and
``Measurement`` validate at construction, and there is no "unknown manual" default to
fall back to. The second is that the tables in ``aslmp/data/`` actually carry what they
claim to -- every row a manual, a revision and a section, or else a CPU model and a
firmware version. These tests force *presence* and *shape*; they cannot force truth,
which is stated plainly as a residual weakness in DESIGN section 7.

The third arrived on 2026-09-07 and is at the bottom of this file: the numbers this
repository publishes in **prose**. A measurement in a TSV has a schema and a test; the
same measurement in a README has neither, which is why both claims this project has had
to withdraw were prose, and why one scan rate reached four files and three different
values before anyone noticed. Those tests derive one figure from the one row that
measured it and hold every other appearance of it against that -- so a document cannot
disagree with itself, and a sibling left behind fails the suite rather than waiting for
the next review.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from importlib import import_module
from pathlib import Path

import pytest

from aslmp.data import DATA_DIR, PROVENANCE_TAIL, TABLES, Row, read_table, table_path
from aslmp.wire.citations import Ambiguity, Citation, Measurement, Provenance

PROVENANCES = {"live", "manual", "inferred"}
AMBIGUITY_MENTION = re.compile(r"A-[A-Z][A-Z0-9]{2,}(?:-[A-Z0-9]+)*")
HEX_CODE = re.compile(r"\A0x[0-9A-F]{4}\Z")

# The measurements that this library is allowed to claim. Everything we know about PLC
# pathology is n=1: one CPU, one firmware, one afternoon.
FX5U = "FX5U-32MT/DS"
FX5U_FW = "1.065"


def every_row() -> Iterator[tuple[str, int, Row]]:
    """(table, 1-based row number, row) for every row of every shipped table."""
    for name in sorted(TABLES):
        for index, row in enumerate(read_table(name), start=1):
            yield name, index, row


# ======================================================================================
# The value types
# ======================================================================================


def test_citation_renders_a_reference_a_reader_can_open() -> None:
    citation = Citation("JY997D56001", "K", "4.2 Device range (p.66-67)")
    assert citation.reference == "JY997D56001-K 4.2 Device range (p.66-67)"
    assert str(citation) == citation.reference
    assert citation.provenance is Provenance.MANUAL


def test_citation_marked_inferred_says_so_when_rendered() -> None:
    citation = Citation("JY997D56001", "K", "p.99", provenance=Provenance.INFERRED)
    assert "inferred" in str(citation)


@pytest.mark.parametrize("field", ["manual", "revision", "section"])
def test_citation_refuses_a_blank_required_field(field: str) -> None:
    values = {"manual": "SH(NA)-080956ENG", "revision": "M", "section": "5.2"}
    values[field] = "   "
    with pytest.raises(ValueError, match=field):
        Citation(values["manual"], values["revision"], values["section"])


def test_citation_refuses_live_provenance() -> None:
    """A measured fact is a Measurement. It has to name the silicon."""
    with pytest.raises(ValueError, match="Measurement"):
        Citation("JY997D56001", "K", "p.69", provenance=Provenance.LIVE)


def test_measurement_names_the_silicon_and_is_always_live() -> None:
    measurement = Measurement(FX5U, FX5U_FW, "2026-09-06")
    assert measurement.provenance is Provenance.LIVE
    assert str(measurement) == "FX5U-32MT/DS fw 1.065, 2026-09-06"


@pytest.mark.parametrize("field", ["cpu", "firmware", "date"])
def test_measurement_refuses_a_blank_required_field(field: str) -> None:
    values = {"cpu": FX5U, "firmware": FX5U_FW, "date": "2026-09-06"}
    values[field] = ""
    with pytest.raises(ValueError, match=field):
        Measurement(values["cpu"], values["firmware"], values["date"])


@pytest.mark.parametrize("date", ["06/09/2026", "20260906", "2026-9-6", "yesterday"])
def test_measurement_refuses_a_date_that_is_not_iso(date: str) -> None:
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        Measurement(FX5U, FX5U_FW, date)


def test_measurement_refuses_a_date_that_is_not_a_real_day() -> None:
    with pytest.raises(ValueError, match="calendar date"):
        Measurement(FX5U, FX5U_FW, "2026-02-30")


def test_a_measurement_with_no_conditions_renders_exactly_as_it_always_did() -> None:
    """The new fields are additive. An existing row's rendering must not move."""
    measurement = Measurement(FX5U, FX5U_FW, "2026-09-06")
    assert measurement.conditions == ""
    assert str(measurement) == measurement.reference


def test_a_measurement_carries_the_host_the_medium_and_the_n() -> None:
    """The three conditions that cost this project a published claim.

    "TCP wins the latency tail" was measured correctly, over Wi-Fi, and generalised to
    SLMP; a wired retest overturned it (``A-UDP-TAIL-LATENCY``). A timing that does not
    say where the client stood, what it stood on and how many samples it took cannot be
    checked by the next person, so the type has somewhere to put all three.
    """
    measurement = Measurement(
        FX5U,
        FX5U_FW,
        "2026-09-07",
        host="192.168.10.36 (argus-bench)",
        medium="wired, 3.64 ms median RTT",
        samples=300,
    )
    assert measurement.conditions == (
        "from 192.168.10.36 (argus-bench), over wired, 3.64 ms median RTT, n=300"
    )
    assert str(measurement).startswith(measurement.reference)
    assert "n=300" in str(measurement)


@pytest.mark.parametrize("samples", [0, -1])
def test_measurement_refuses_a_sample_count_below_one(samples: int) -> None:
    with pytest.raises(ValueError, match="at least 1"):
        Measurement(FX5U, FX5U_FW, "2026-09-06", samples=samples)


@pytest.mark.parametrize("samples", ["300", 300.0, True])
def test_measurement_refuses_a_sample_count_that_is_not_an_int(samples: object) -> None:
    with pytest.raises(TypeError, match="samples"):
        Measurement(FX5U, FX5U_FW, "2026-09-06", samples=samples)  # type: ignore[arg-type]


def test_measurement_is_frozen() -> None:
    measurement = Measurement(FX5U, FX5U_FW, "2026-09-06")
    with pytest.raises(AttributeError):
        measurement.cpu = "R04CPU"  # type: ignore[misc]  # proving frozen at run time


def test_ambiguity_holds_both_readings_and_what_we_did() -> None:
    ambiguity = Ambiguity(
        key="A-ZR-RADIX",
        question="Is ZR hexadecimal or decimal?",
        readings=("hexadecimal per SH(NA)-080956ENG", "decimal per JY997D56001"),
        chosen="hexadecimal",
        reason="two of three documents agree",
        probe="write ZR0 and ZR16 from GX Works3 and read ZR16 back with 0x10 and 0x16",
    )
    assert "A-ZR-RADIX" in str(ambiguity)
    assert "hexadecimal" in str(ambiguity)


def test_ambiguity_refuses_a_single_reading() -> None:
    """One reading is not an ambiguity; it is a fact with a citation."""
    with pytest.raises(ValueError, match="at least two readings"):
        Ambiguity("A-X", "q?", ("only one",), "that one", "because", "probe it")


def test_ambiguity_refuses_a_mutable_readings_list() -> None:
    with pytest.raises(TypeError, match="tuple"):
        Ambiguity("A-X", "q?", ["a", "b"], "a", "because", "probe")  # type: ignore[arg-type]


def test_ambiguity_refuses_duplicate_readings() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        Ambiguity("A-X", "q?", ("a", "a"), "a", "because", "probe")


@pytest.mark.parametrize("key", ["IQF-XY", "a-iqf-xy", "A_IQF_XY", "A-"])
def test_ambiguity_refuses_a_malformed_key(key: str) -> None:
    with pytest.raises(ValueError, match="key"):
        Ambiguity(key, "q?", ("a", "b"), "a", "because", "probe")


# ======================================================================================
# The shipped tables: shape
# ======================================================================================


def test_every_declared_table_exists_and_parses() -> None:
    for name in TABLES:
        assert table_path(name).is_file(), f"{name}.tsv is missing from {DATA_DIR}"
        assert read_table(name), f"{name}.tsv has no rows"


def test_read_table_refuses_an_unknown_name() -> None:
    with pytest.raises(KeyError, match="known tables"):
        read_table("devices_v2")


def test_tables_are_ascii_and_free_of_stray_whitespace() -> None:
    """These files are read by humans in a diff. Keep them mechanical."""
    problems: list[str] = []
    for name in TABLES:
        path: Path = table_path(name)
        raw = path.read_bytes()
        if not raw.isascii():
            problems.append(f"{name}.tsv is not ASCII")
        for lineno, line in enumerate(raw.decode("ascii").splitlines(), start=1):
            if line != line.rstrip(" "):
                problems.append(f"{name}.tsv:{lineno}: trailing space")
            if "  " in line.replace("\t", ""):
                continue  # double spaces inside prose are fine
    assert not problems, "\n".join(problems)


def test_every_table_carries_the_provenance_tail() -> None:
    for name, columns in TABLES.items():
        if name == "manuals":
            continue  # manuals.tsv IS the provenance target; it has no tail of its own
        assert columns[-len(PROVENANCE_TAIL) :] == PROVENANCE_TAIL, (
            f"{name}.tsv must end with the standard provenance tail {PROVENANCE_TAIL}"
        )


def test_no_table_spends_a_column_name_the_provenance_tail_also_uses() -> None:
    """A duplicated column name is silently lost, and this has already happened.

    ``read_table`` builds each row with ``dict(zip(header, fields))``, so two columns
    of the same name leave only the last. When ``host``/``medium``/``samples`` were
    added, the middle one was first called ``link`` -- which ``limits.tsv`` already
    spends on the CPU built-in port against an FX5-ENET module, whose budgets differ
    (960 against 949 points). Every FX5U limit row would have lost its ``cpu``/``enet``
    key to a Wi-Fi label, and nothing but this test would have said so.
    """
    for name, columns in TABLES.items():
        if name == "manuals":
            continue
        own = columns[: -len(PROVENANCE_TAIL)]
        clash = sorted(set(own) & set(PROVENANCE_TAIL))
        assert not clash, (
            f"{name}.tsv declares {clash}, which the provenance tail also declares. "
            f"read_table would keep only one of the two."
        )
        assert len(set(columns)) == len(columns), f"{name}.tsv has a duplicate column"


def test_every_row_declares_a_known_provenance() -> None:
    for name, index, row in every_row():
        if name == "manuals":
            continue
        assert row["provenance"] in PROVENANCES, (
            f"{name}.tsv row {index}: provenance {row['provenance']!r} is not one of "
            f"{sorted(PROVENANCES)}"
        )


def test_measured_rows_name_the_cpu_and_the_firmware() -> None:
    """The single most important property of this table set.

    A row that says "the hardware disagrees with the manual" and does not say WHICH
    hardware is a rumour, and a firmware update would silently invalidate it with
    nothing to point at.
    """
    for name, index, row in every_row():
        if name == "manuals" or row["provenance"] != "live":
            continue
        where = f"{name}.tsv row {index}"
        assert row["cpu"], f"{where}: provenance is live but no CPU is named"
        assert row["firmware"], f"{where}: provenance is live but no firmware is named"
        assert row["measured"], f"{where}: provenance is live but no date is given"
        # Constructing it is the real assertion: Measurement validates the date.
        Measurement(row["cpu"], row["firmware"], row["measured"])


def test_unmeasured_rows_do_not_pretend_to_be_measured() -> None:
    for name, index, row in every_row():
        if name == "manuals" or row["provenance"] == "live":
            continue
        where = f"{name}.tsv row {index}"
        for field in ("cpu", "firmware", "measured", "host", "medium", "samples"):
            assert not row[field], (
                f"{where}: provenance is {row['provenance']!r} but {field} is set. "
                f"Either it was measured, in which case say live, or it was not."
            )


def test_the_conditions_columns_are_optional_but_never_malformed() -> None:
    """``host``, ``medium`` and ``samples`` may be empty. They may not be junk.

    Empty means "not written down at the time", which is a different statement from
    "no host" and is the reason they are not required: back-filling a host onto a row
    whose notes never named one would be inventing evidence, which is the failure this
    whole tail exists to prevent.
    """
    for name, index, row in every_row():
        if name == "manuals":
            continue
        where = f"{name}.tsv row {index}"
        if row["samples"]:
            assert row["samples"].isdigit() and int(row["samples"]) >= 1, (
                f"{where}: samples is {row['samples']!r}; it is an n, so a positive "
                f"integer or empty"
            )
        for field in ("host", "medium"):
            assert row[field] == row[field].strip(), f"{where}: {field} is padded"


def test_every_latency_bearing_ambiguity_names_its_host_and_medium() -> None:
    """The rows where the medium could have changed the answer must name it.

    ``A-UDP-TAIL-LATENCY`` is in this list because it is the row that had to be
    withdrawn: measured correctly on Wi-Fi and published as a property of SLMP. A
    reader cannot tell whether one of these transfers to their plant without knowing
    what it was measured over.
    """
    rows = {row["key"]: row for row in read_table("ambiguities")}
    for key in ("A-UDP-TAIL-LATENCY", "A-UDP-PIPELINE-DEPTH", "A-ENTRY-RELEASE-RACE"):
        row = rows[key]
        assert row["host"], f"{key}: a timing row with no host"
        assert row["medium"], f"{key}: a timing row with no medium"


def test_every_row_has_a_manual_or_a_measurement() -> None:
    for name, index, row in every_row():
        if name == "manuals":
            continue
        assert row["manual"] or row["provenance"] == "live", (
            f"{name}.tsv row {index}: no manual and no measurement. Every constant in "
            f"this library must be checkable against something."
        )


def test_every_cited_manual_exists_with_that_revision_and_was_read() -> None:
    manuals = {
        (row["manual"], row["revision"]): row for row in read_table("manuals")
    }
    for name, index, row in every_row():
        if name == "manuals" or not row["manual"]:
            continue
        where = f"{name}.tsv row {index}"
        key = (row["manual"], row["revision"])
        assert row["revision"], f"{where}: cites {row['manual']} with no revision"
        assert row["section"], (
            f"{where}: cites {row['manual']}-{row['revision']} with no section"
        )
        assert key in manuals, (
            f"{where}: cites {key}, which is not a row of manuals.tsv. Two revisions of "
            f"JY997D56001 disagree about the FX5 X/Y examples, so a citation without a "
            f"revision that we actually hold cannot be checked."
        )
        assert manuals[key]["status"] == "read", (
            f"{where}: cites {key}, which manuals.tsv marks {manuals[key]['status']!r}. "
            f"Nothing may cite a document we do not have."
        )
        # Constructing it is the real assertion.
        Citation(
            row["manual"],
            row["revision"],
            row["section"],
            provenance=(
                Provenance.INFERRED if row["provenance"] == "inferred" else Provenance.MANUAL
            ),
        )


def test_manuals_table_is_self_consistent() -> None:
    seen: set[tuple[str, str]] = set()
    for row in read_table("manuals"):
        key = (row["manual"], row["revision"])
        assert key not in seen, f"manuals.tsv lists {key} twice"
        seen.add(key)
        assert row["title"], f"manuals.tsv {key}: no title"
        assert row["status"] in {"read", "not_obtained"}, f"manuals.tsv {key}: bad status"
        if row["status"] == "read":
            assert row["url"], f"manuals.tsv {key}: read but no URL"
        else:
            assert not row["url"], f"manuals.tsv {key}: not obtained but has a URL"


# ======================================================================================
# ambiguities.tsv -- the table we hand a Mitsubishi engineer
# ======================================================================================


def ambiguity_rows() -> Mapping[str, Row]:
    return {row["key"]: row for row in read_table("ambiguities")}


def test_every_ambiguity_row_builds_an_ambiguity() -> None:
    rows = read_table("ambiguities")
    assert len({row["key"] for row in rows}) == len(rows), "duplicate ambiguity key"
    for row in rows:
        Ambiguity(
            key=row["key"],
            question=row["question"],
            readings=tuple(row["readings"].split("|")),
            chosen=row["chosen"],
            reason=row["reason"],
            probe=row["probe"],
        )
        assert row["status"] in {
            "open",
            "resolved_by_measurement",
            "resolved_by_choice",
        }, f"{row['key']}: bad status {row['status']!r}"
        if row["status"] == "resolved_by_measurement":
            assert row["provenance"] == "live", (
                f"{row['key']}: claims to be resolved by measurement but carries "
                f"provenance {row['provenance']!r}"
            )


@pytest.mark.parametrize(
    "key",
    [
        "A-BATCH-BIT-LIMIT",  # 7168 vs 3584 vs measured 3584
        "A-REMOTE-FIXED",  # the 1002/1005/1006 fixed field: 01 00 vs 00 00
        "A-ZR-RADIX",  # hexadecimal vs decimal
        "A-IQF-4E",  # 4E on the FX5 built-in port
        "A-CLEAR-MODE",  # Remote RUN clear mode
        "A-IQF-XY",  # the octal X/Y wire number
    ],
)
def test_the_ambiguities_that_must_be_registered_are_registered(key: str) -> None:
    assert key in ambiguity_rows(), (
        f"{key} is not in ambiguities.tsv. Every place the manuals contradict each "
        f"other or the hardware is a row in that table, not a comment in the source."
    )


def test_every_ambiguity_key_mentioned_anywhere_is_a_real_row() -> None:
    """A note that points at ``A-SOMETHING`` must point at something."""
    known = set(ambiguity_rows())
    dangling: list[str] = []
    for name, index, row in every_row():
        for column, value in row.items():
            if name == "ambiguities" and column == "key":
                continue
            for mention in AMBIGUITY_MENTION.findall(value):
                if mention not in known:
                    dangling.append(f"{name}.tsv row {index} ({column}): {mention}")
    assert not dangling, "references to ambiguities that do not exist:\n  " + "\n  ".join(
        dangling
    )


# ======================================================================================
# end_codes.tsv
# ======================================================================================


def end_code_rows() -> Mapping[str, Row]:
    return {row["code"]: row for row in read_table("end_codes")}


def test_end_codes_are_unique_and_well_formed() -> None:
    rows = read_table("end_codes")
    assert len({row["code"] for row in rows}) == len(rows), "duplicate end code"
    for row in rows:
        assert HEX_CODE.match(row["code"]), (
            f"end code {row['code']!r} must be written 0xNNNN with upper-case hex, so "
            f"that grepping for it finds the same string in the source and in an "
            f"exception message"
        )
        assert row["name"], f"{row['code']}: no name"
        assert row["description"], f"{row['code']}: no description"
        if row["exception_class"]:
            assert row["exception_class"].startswith("Slmp"), row["exception_class"]
            assert row["exception_class"].endswith("Error"), row["exception_class"]
            assert row["exception_class"].isidentifier(), row["exception_class"]


@pytest.mark.parametrize(
    ("code", "expected_class", "must_mention"),
    [
        ("0xC05C", "SlmpRequestContentError", "0xC05B"),
        ("0xC052", "SlmpWordPointCountError", "ZERO"),
        ("0xC061", "SlmpPlcFramingError", "0xC057"),
    ],
)
def test_the_three_hardware_corrections_are_live_and_say_what_the_docs_predicted(
    code: str, expected_class: str, must_mention: str
) -> None:
    """The corrections DESIGN section 3.3 requires, each carrying its measurement.

    A correction that does not record what the manual said is just an unexplained
    magic number six months later.
    """
    row = end_code_rows()[code]
    assert row["exception_class"] == expected_class
    assert row["provenance"] == "live", f"{code} must be marked measured, not doc-derived"
    assert row["cpu"] == FX5U and row["firmware"] == FX5U_FW
    assert must_mention in row["note"], (
        f"{code}'s note must record what the doc-derived mapping expected "
        f"({must_mention!r}), so the correction can be re-checked when a manual "
        f"revision lands"
    )


def test_the_overstated_length_hang_is_recorded_against_c061() -> None:
    """Understated fails loudly; overstated hangs and looks exactly like a dead PLC.

    That asymmetry is the whole reason ``L`` is produced by one expression in the
    package and guarded three ways, so it has to be written down where the end code is.
    """
    note = end_code_rows()["0xC061"]["note"]
    assert "OVERSTAT" in note.upper()
    assert "NO RESPONSE" in note.upper()


@pytest.mark.parametrize("code", ["0xC051", "0xC054", "0xC056", "0xC059"])
def test_the_measured_end_codes_carry_their_measurement(code: str) -> None:
    row = end_code_rows()[code]
    assert row["provenance"] == "live"
    assert row["cpu"] == FX5U
    assert row["firmware"] == FX5U_FW


def test_c06f_is_documented_as_producing_silence() -> None:
    """The single most misleading failure in the protocol, and it has no wire evidence."""
    row = end_code_rows()["0xC06F"]
    assert "NO ERROR RESPONSE IS SENT" in row["description"].upper()
    assert not row["exception_class"], (
        "0xC06F never reaches a client, so it must not claim an exception class: it is "
        "consumed by SlmpTimeoutError.likely_causes instead"
    )


def test_exception_classes_named_here_exist_once_the_hierarchy_does() -> None:
    """Activates as soon as build unit U5 lands ``aslmp/errors/``."""
    errors = pytest.importorskip("aslmp.errors")
    exported = {name for name in dir(errors) if name.startswith("Slmp")}
    if not exported:
        pytest.skip("aslmp.errors is still a skeleton; the hierarchy belongs to unit U5")
    missing = sorted(
        {row["exception_class"] for row in read_table("end_codes") if row["exception_class"]}
        - exported
    )
    assert not missing, (
        f"end_codes.tsv names exception classes that aslmp.errors does not export: "
        f"{missing}"
    )


# ======================================================================================
# limits.tsv
# ======================================================================================


def test_limit_rules_are_syntactically_valid() -> None:
    for row in read_table("limits"):
        rule = row["rule"]
        kind, _, params = rule.partition(":")
        numbers = [int(p) for p in params.split(",")] if params else []
        where = f"{row['profile_key']} {row['command']} {row['coding']} {row['unit']}"
        if kind == "flat":
            assert len(numbers) == 1, f"{where}: flat takes one number, got {rule!r}"
        elif kind == "weighted":
            assert len(numbers) == 3, f"{where}: weighted takes three numbers, got {rule!r}"
        elif kind == "block":
            assert len(numbers) == 3, f"{where}: block takes three numbers, got {rule!r}"
        else:
            pytest.fail(f"{where}: unknown limit rule kind {kind!r}")
        if kind == "block":
            max_blocks, overhead, max_total = numbers
            assert max_blocks > 0 and max_total > 0, f"{where}: a limit of zero is not a limit"
            assert overhead >= 0, f"{where}: negative per-block overhead"
        else:
            assert all(n > 0 for n in numbers), f"{where}: a limit of zero is not a limit"
        assert HEX_CODE.match(row["end_code"]), (
            f"{where}: end_code {row['end_code']!r} must be 0xNNNN. It is quoted "
            f"verbatim in the exception, so a user can match our refusal against what "
            f"the CPU would have said."
        )


def fx5u_limit(command: str, coding: str, unit: str, link: str = "cpu") -> Row:
    for row in read_table("limits"):
        if (
            row["profile_key"] == "melsec:iq-f/fx5u"
            and row["command"] == command
            and row["coding"] == coding
            and row["unit"] == unit
            and row["link"] == link
        ):
            return row
    pytest.fail(f"no FX5U limit for {command} {coding} {unit} on {link}")


@pytest.mark.parametrize(
    ("command", "unit", "rule", "end_code"),
    [
        ("0x0401", "word", "flat:960", "0xC052"),
        ("0x0401", "bit", "flat:3584", "0xC051"),
        ("0x0403", "word", "flat:192", "0xC054"),
    ],
)
def test_the_measured_fx5u_limits_are_marked_measured(
    command: str, unit: str, rule: str, end_code: str
) -> None:
    """960 / 3584 / 192, binary-searched on the CPU -- not carried from a manual."""
    row = fx5u_limit(command, "binary", unit)
    assert row["rule"] == rule
    assert row["end_code"] == end_code
    assert row["provenance"] == "live", (
        f"{command} {unit} on the FX5U was binary-searched on hardware and must not be "
        f"shipped as a doc-derived figure"
    )
    assert row["cpu"] == FX5U
    assert row["firmware"] == FX5U_FW
    assert row["note"], "a measured limit must record how it was established"


def test_the_bit_limit_note_records_that_the_generic_manual_says_7168() -> None:
    """A client that hard-codes SLMP-REF's ceiling builds frames this CPU rejects."""
    note = fx5u_limit("0x0401", "binary", "bit")["note"]
    assert "7168" in note
    assert "A-BATCH-BIT-LIMIT" in note
    assert read_table("limits"), "sanity"


def test_the_ethernet_module_link_has_its_own_budget() -> None:
    """192 is the built-in port's Read Random ceiling; an FX5-ENET's is 123.

    A design that models the budget as one per-CPU constant is wrong on one of them,
    which is why every LimitKey carries the Link.
    """
    assert fx5u_limit("0x0403", "binary", "word", link="enet")["rule"] == "flat:123"
    assert fx5u_limit("0x1401", "binary", "word", link="enet")["rule"] == "flat:949"


def test_limits_cover_every_iq_f_profile() -> None:
    keys = {row["profile_key"] for row in read_table("limits")}
    for key in (
        "melsec:iq-f/fx5u",
        "melsec:iq-f/fx5uc",
        "melsec:iq-f/fx5uj",
        "melsec:iq-f/fx5s",
        "melsec:iq-r",
    ):
        assert key in keys, f"no point limits for {key}"


# ======================================================================================
# devices.tsv
# ======================================================================================


def test_device_names_and_codes_are_unique() -> None:
    rows = read_table("devices")
    names = [row["name"] for row in rows]
    assert len(set(names)) == len(names), "duplicate device name"
    shorts = [row["code_short"] for row in rows if row["code_short"]]
    assert len(set(shorts)) == len(shorts), "two devices share a 1-byte device code"
    longs = [row["code_long"] for row in rows]
    assert len(set(longs)) == len(longs), "two devices share a 2-byte device code"


def test_short_and_long_device_codes_agree() -> None:
    """The 2-byte form is the 1-byte form zero-extended, for every device that has both."""
    for row in read_table("devices"):
        if not row["code_short"]:
            continue
        assert int(row["code_long"], 16) == int(row["code_short"], 16), (
            f"{row['name']}: code_long {row['code_long']} is not code_short "
            f"{row['code_short']} zero-extended"
        )


def test_ascii_mnemonics_are_padded_to_width() -> None:
    """ASCII sends the mnemonic, never the hex of the code. D is "D*", never "A8"."""
    for row in read_table("devices"):
        if row["ascii2"]:
            assert len(row["ascii2"]) == 2, f"{row['name']}: ascii2 must be two characters"
        if row["ascii4"]:
            assert len(row["ascii4"]) == 4, f"{row['name']}: ascii4 must be four characters"
        assert "*" not in row["ascii4"][:1], f"{row['name']}: padding must follow the name"


def test_the_retentive_timer_mnemonics_are_the_surprising_ones() -> None:
    """STS is "SS" and STC is "SC" in the two-character form, not "ST"."""
    devices = {row["name"]: row for row in read_table("devices")}
    assert devices["STS"]["ascii2"] == "SS"
    assert devices["STC"]["ascii2"] == "SC"
    assert devices["STN"]["ascii2"] == "SN"


def test_contacts_and_coils_are_refused_in_random_and_monitor() -> None:
    """Load-bearing: the FX5U ACCEPTED a TS0 point in a Read Random and answered 0x0000."""
    devices = {row["name"]: row for row in read_table("devices")}
    for name in ("TS", "TC", "STS", "STC", "CS", "CC", "LCS", "LCC"):
        assert devices[name]["random_ok"] == "no", f"{name} must be refused in Read Random"
        assert devices[name]["monitor_ok"] == "no", f"{name} must be refused in monitor"


def test_x_and_y_default_to_the_iq_r_radix_and_point_at_the_octal_note() -> None:
    """The radix belongs to the profile, not to the letter."""
    devices = {row["name"]: row for row in read_table("devices")}
    for name in ("X", "Y"):
        assert devices[name]["radix"] == "hexadecimal"
        assert "OCTAL on iQ-F" in devices[name]["note"]
        assert "A-IQF-XY" in devices[name]["note"]


def test_long_only_devices_have_no_short_code() -> None:
    for row in read_table("devices"):
        if row["min_spec"] == "long":
            assert not row["code_short"], f"{row['name']}: long-spec device with a short code"
            assert not row["ascii2"], f"{row['name']}: long-spec device with a 2-char mnemonic"


# ======================================================================================
# ranges_iqf.tsv / ranges_iqr.tsv
# ======================================================================================


def range_row(table: str, profile: str, device: str) -> Row:
    for row in read_table(table):
        if row["profile_key"] == profile and row["device"] == device:
            return row
    pytest.fail(f"{table}.tsv has no row for {profile} {device}")


def test_every_range_row_names_a_device_in_the_device_table() -> None:
    known = {row["name"] for row in read_table("devices")}
    for table in ("ranges_iqf", "ranges_iqr"):
        for row in read_table(table):
            assert row["device"] in known, (
                f"{table}.tsv names {row['device']}, which is not in devices.tsv"
            )


def test_range_rows_are_internally_consistent() -> None:
    for table in ("ranges_iqf", "ranges_iqr"):
        for row in read_table(table):
            where = f"{table}.tsv {row['profile_key']} {row['device']}"
            assert row["present"] in {"yes", "no"}, where
            if row["present"] == "no":
                assert not row["points"], f"{where}: absent device with a point count"
                assert row["note"], f"{where}: absent device with no explanation"
                continue
            if not row["points"]:
                assert row["note"], (
                    f"{where}: present with no static range and no note. An empty range "
                    f"means 'not statically validated', never 'unknown'."
                )
                continue
            if row["points"] == "0":
                # Present in the silicon, zero points allocated by the default
                # parameters -- iQ-R file registers, step relays, retentive timers.
                # The exception has to explain that rather than say "out of range".
                assert not row["first"] and not row["last"], (
                    f"{where}: zero points with a range"
                )
                assert row["note"], f"{where}: zero points and no explanation"
                continue
            first, last, points = int(row["first"]), int(row["last"]), int(row["points"])
            assert last - first + 1 == points, f"{where}: first/last/points disagree"
            assert row["radix"] in {"decimal", "hexadecimal", "octal"}, where


def test_fx5u_x_and_y_are_octal_with_1024_linear_points() -> None:
    """GX Works3 prints X0 to X1777; the wire carries 0 to 1023."""
    for device in ("X", "Y"):
        row = range_row("ranges_iqf", "melsec:iq-f/fx5u", device)
        assert row["radix"] == "octal"
        assert row["points"] == "1024"
        assert row["last"] == "1023"
        assert row["notation"] == f"{device}0 to {device}1777"
        assert row["provenance"] == "live"
        assert "8 and 9" in row["note"], (
            "the note must record that the CPU accepts the illegal digits 8 and 9, "
            "because rejecting them is the client's job and nothing else will do it"
        )


def test_fx5u_data_registers_end_at_d7999_and_the_span_is_what_matters() -> None:
    row = range_row("ranges_iqf", "melsec:iq-f/fx5u", "D")
    assert (row["first"], row["last"], row["points"]) == ("0", "7999", "8000")
    assert row["provenance"] == "live"
    assert "2 POINTS" in row["note"], (
        "D7999 with two points also returned 0xC056, which is why check_range validates "
        "the span and not the start address. Say so where the range is written down."
    )


@pytest.mark.parametrize("device", ["V", "ZR", "DX", "DY"])
def test_the_devices_an_fx5_does_not_have_are_marked_absent(device: str) -> None:
    row = range_row("ranges_iqf", "melsec:iq-f/fx5u", device)
    assert row["present"] == "no"


def test_r_is_present_on_fx5_even_though_zr_is_not() -> None:
    assert range_row("ranges_iqf", "melsec:iq-f/fx5u", "R")["present"] == "yes"


@pytest.mark.parametrize("device", ["R", "ZR"])
def test_iq_r_file_registers_default_to_zero_points(device: str) -> None:
    """SH(NA)-080956ENG's own ZR16384 example fails on an out-of-the-box iQ-R."""
    row = range_row("ranges_iqr", "melsec:iq-r/r04", device)
    assert row["present"] == "yes"
    assert row["points"] == "0"
    assert row["configurable"] == "yes"
    assert row["note"], "the exception message has to explain this, not just say 'out of range'"


def test_no_iq_r_range_claims_to_have_been_measured() -> None:
    """We have never had an iQ-R. Nothing in that table may say LIVE."""
    for row in read_table("ranges_iqr"):
        assert row["provenance"] != "live", (
            f"ranges_iqr.tsv {row['profile_key']} {row['device']} claims a measurement. "
            f"No iQ-R has ever been on this bench."
        )


# ======================================================================================
# model_codes.tsv
# ======================================================================================


def test_model_codes_are_unique_and_well_formed() -> None:
    rows = read_table("model_codes")
    assert len({row["code"] for row in rows}) == len(rows), "duplicate model code"
    for row in rows:
        assert HEX_CODE.match(row["code"]), row["code"]
        assert row["model"], f"{row['code']}: no model name"
        assert row["family"] in {"iq-f", "iq-r", "q", "l"}, row["family"]


def test_the_one_model_code_we_have_seen_on_a_wire_is_marked_measured() -> None:
    rows = {row["code"]: row for row in read_table("model_codes")}
    row = rows["0x4A49"]
    assert row["model"] == FX5U
    assert row["profile_key"] == "melsec:iq-f/fx5u"
    assert row["provenance"] == "live"
    assert row["firmware"] == FX5U_FW


def test_the_rcpu_catch_all_claims_no_profile() -> None:
    """0x0360 identifies a family, not a model. connect() must raise, not guess."""
    rows = {row["code"]: row for row in read_table("model_codes")}
    assert rows["0x0360"]["profile_key"] == ""
    assert "profile" in rows["0x0360"]["note"]


def test_every_profile_key_in_the_tables_is_spelled_the_same_way() -> None:
    keys: set[str] = set()
    for table in ("limits", "ranges_iqf", "ranges_iqr"):
        keys |= {row["profile_key"] for row in read_table(table)}
    keys |= {row["profile_key"] for row in read_table("model_codes") if row["profile_key"]}
    shape = re.compile(r"melsec:[a-z0-9-]+(/[a-z0-9]+)?")
    bad = sorted(key for key in keys if not shape.fullmatch(key))
    assert not bad, f"profile keys must look like melsec:iq-f/fx5u; got {bad}"


# ======================================================================================
# docs/unverified.md, which is billed as complete
# ======================================================================================


def test_every_shipped_profile_is_accounted_for_in_unverified_md() -> None:
    """A page that lists what we have not verified must list all of it.

    It did not. Until 2026-09-07 it named iQ-R, Q and L and omitted four selectable
    profiles -- ``melsec:iq-f/fx5uc``, ``fx5uj``, ``fx5s`` and ``melsec:iq-r/r00`` --
    three of them iQ-F, which is precisely where a reader assumes the FX5U bench
    numbers carry over. They do not: a measurement names one piece of silicon.

    Importing ``aslmp.profiles`` here rather than hard-coding the eight keys is the
    point of the test. A ninth profile that nobody documents fails this, and a keys
    list copied into the test would not.
    """
    from aslmp.profiles import KEYS

    page = (Path(__file__).resolve().parents[2] / "docs" / "unverified.md").read_text(
        encoding="utf-8"
    )
    missing = sorted(key for key in KEYS if key not in page)
    assert not missing, (
        f"docs/unverified.md does not name {missing}. Every shipped profile is either "
        f"measured on hardware we own -- which is one of them -- or it is on that page."
    )


def test_unverified_md_still_says_which_profile_is_the_measured_one() -> None:
    """The list is only useful if the one exception is unmistakable."""
    page = (Path(__file__).resolve().parents[2] / "docs" / "unverified.md").read_text(
        encoding="utf-8"
    )
    assert "melsec:iq-f/fx5u" in page
    assert FX5U in page, "the page must name the CPU the measured profile rests on"


# ======================================================================================
# The numbers the documents publish
# ======================================================================================
#
# Everything above this line defends a fact that lives in a TSV, where a test can reach
# it. These defend the facts that live in prose, which is where this project has twice
# published something it had to withdraw -- and where, both times, the fix landed on the
# instance and left the siblings. A number in a README is not typed, not executed and not
# imported by anything, so nothing but a test like this one can notice when one copy of
# it moves and the other four do not.
#
# The rule these enforce: a measured quantity has ONE source, every other appearance of
# it is derived from that source or cites it, and a figure that names no conditions is
# not a measurement.

REPO = Path(__file__).resolve().parents[2]
HARDWARE_MD = REPO / "docs" / "hardware.md"

IDLE_SCAN_ROW = re.compile(
    r"^\|\s*20 s\s*\|\s*\*\*([\d,]+)\*\*\s*\|\s*([\d.]+)\s*\|\s*\*\*([\d.]+)\*\*\s*\|",
    re.MULTILINE,
)
"""The 20 s row of ``docs/hardware.md`` section 17: counts, seconds, scans/s.

Parsed rather than retyped, so this test cannot disagree with the document it is
policing. If the row is reformatted this fails loudly, which is the correct outcome:
the row is a published measurement and its shape is part of what is published.
"""

SCAN_FIGURE = re.compile(
    r"(?<![\w.])(\d{3,4}(?:\.\d+)?)(?!\d)"
    r"(?:\s*(?:\w+\s+){0,2}(?:scans?|counts?)\s*/\s*s|\s*/\s*s)(?![\w/])"
)
"""A scan rate written out: ``1018 scans/s``, ``969/s``, ``1018 real counts/s``.

Anchored on the unit rather than on the number, so ``1024`` device points and a
``window: int = 1024`` are invisible to it and a rate is not. ``405 txn/s`` is a
transaction rate and not a scan rate, so it is invisible too: the unit has to be a scan
or a count, or else the number has to sit directly against the ``/s``.
"""

SCAN_PERIOD = re.compile(
    r"(?<![\w.])(\d{1,4}(?:\.\d+)?)\s*(?:us|µs)\s+per\s+(?:scan|count)"
)
"""``982 us per scan``, ``982.3 us per scan``, ``61.6 us per count``."""

SCAN_RATE_CONSTANT = re.compile(
    r"^\s*([A-Z][A-Z0-9_]*SCAN_RATE[A-Z0-9_]*)\s*(?::[^=]+)?=\s*([\d_]+(?:\.\d+)?)\s*$",
    re.MULTILINE,
)
"""A named constant that *is* the scan rate, e.g. ``IDLE_SCAN_RATE_HZ = 1018.0``.

The literal a test asserts against has no unit beside it, so :data:`SCAN_FIGURE` cannot
see it -- and that literal is the one that matters most, because it is the only copy of
this number the *code* depends on. ``expected = 1024 * 2.0`` in
``tests/hardware/test_fx5u.py`` was exactly that: a figure the repository carried in
prose, promoted to an assertion, and 1 % away from the measurement.
"""

PUBLISHED_SCAN_RATES = {"1018", "1018.0", "1018.4"}
"""The one published idle rate, and the two host figures it is the round number of.

``docs/hardware.md`` section 17 carries the conditions. Nothing else may introduce a
fourth spelling of this quantity.
"""

RUN_LOCAL_SCAN_RATES = {
    "969": "1029",
    "1029": "969",
    "997": "1018",
}
"""Loaded/idle pairs that belong to ONE run, and the partner each may not be split from.

969 against 1029 is the wired five-minute soak; 997 against 1018 is the Wi-Fi run of the
shipped script. A cost-of-polling percentage is a ratio of two scan rates, so quoting
either half alone turns a within-run reference into a repository-wide claim -- which is
exactly how 1029 came to be published as an idle scan rate in two test modules and a
bench script.
"""

RETRACTION_MARKERS = (
    "until 2026",
    "withdrawn",
    "used to",
    "earlier note",
    "never a measurement",
    "was ",
    "said ~",
    "said 1029",
    "printed",
    "residue",
    "retired",
    "disagree",
    "spread",
    "carried ~1024",
    "reconciled",
    "drift",
    "stale",
)
"""Words that mark a number as history rather than as a claim.

A withdrawn figure has to stay quotable -- this project's whole argument is that saying
what you got wrong is worth more than the wrong answer was -- so it may appear, but only
where the surrounding lines say it is not the answer.
"""

PROSE_FILES = (
    "README.md",
    "CHANGELOG.md",
    "docs/architecture.md",
    "docs/benchmarking.md",
    "docs/cli.md",
    "docs/errors.md",
    "docs/hardware.md",
    "docs/unverified.md",
    "bench/_report.py",
    "bench/access_patterns.py",
    "bench/soak.py",
    "bench/transports.py",
    "tests/hardware/test_fx5u.py",
    "tests/hardware/test_remote_control.py",
)
"""Everything a reader meets a number in, outside the package itself."""

HOST_TOKENS = ("192.168.10.41", "192.168.10.36", "argus-bench")
MEDIUM_TOKENS = ("wi-fi", "wifi", "wired", "radio")
"""The two conditions a latency figure must name.

Not the CPU and not the date, which were already required: the host and the medium are
the two whose absence cost this project a published claim (``A-UDP-TAIL-LATENCY``), and
naming them is the rule ``docs/benchmarking.md`` adopted in writing afterwards.
"""

LATENCY_COLUMN = re.compile(r"\bp50\b|\bp99\b|\bp90\b|\bstdev\b", re.IGNORECASE)
"""A Markdown header row that makes the table below it a latency table."""

PERCENTILE_CLAIM = re.compile(
    r"\bp(?:50|90|99)\b[^|\n]{0,3}?(?<![\w.])\d+\.\d+"
    r"|(?<![\w.])[-+]?\d+\.\d+\s*ms\b[^|\n]{0,24}?\bat p(?:50|90|99)\b"
)
"""A percentile with an actual number against it, in running text.

Deliberately not every mention of ``p50``: a column heading, an f-string that formats one
at run time, and a sentence about what percentiles are for are not claims about this
bench. ``p50 3.67`` and ``+0.07 ms at p50`` are.
"""


def prose_sources() -> Iterator[tuple[str, str]]:
    """(path, text) for every document and script that carries published numbers."""
    for name in PROSE_FILES:
        path = REPO / name
        assert path.is_file(), f"{name} is in PROSE_FILES and does not exist"
        yield name, path.read_text(encoding="utf-8")


def package_sources() -> Iterator[tuple[str, str]]:
    """(path, text) for every module of the package itself."""
    for path in sorted((REPO / "src" / "aslmp").rglob("*.py")):
        yield path.relative_to(REPO).as_posix(), path.read_text(encoding="utf-8")


def near(lines: Sequence[str], index: int, radius: int = 4) -> str:
    """The window a marker or a partner number is allowed to live in, lower-cased."""
    return "\n".join(lines[max(0, index - radius) : index + radius + 1]).lower()


TABLE_RULE = re.compile(r"^\|[\s:|-]+\|\s*$")
"""The ``| --- | --- |`` line, which is what makes the row above it a header."""


def markdown_tables(lines: Sequence[str]) -> Iterator[tuple[int, list[str]]]:
    """(header line number, every line of the table) for each Markdown table.

    A table is judged as a whole and attributed to its **header**, because that is where
    its caption is. Judging each row separately attributes a ``p50`` on the fourth row of
    a table to a caption walk that stops at the third row, which finds nothing -- and a
    latency figure that is simply lower down a table is not less of a claim.
    """
    index = 0
    while index < len(lines) - 1:
        if lines[index].startswith("|") and TABLE_RULE.match(lines[index + 1]):
            end = index + 2
            while end < len(lines) and lines[end].startswith("|"):
                end += 1
            yield index, list(lines[index:end])
            index = end
            continue
        index += 1


def caption_of(lines: Sequence[str], header: int) -> list[str]:
    """The prose between a table's header row and whatever precedes it.

    Bounded by the previous table rather than by a fixed line count, because a fixed
    count lets a table inherit its neighbour's caption: ``README.md`` prints the Wi-Fi
    and the wired transport comparisons seven lines apart, and a window wide enough to
    reach the second one's own caption also reaches the first one's host.
    """
    start = header
    while start > 0 and header - start < 15:
        if lines[start - 1].lstrip().startswith("|"):
            break
        start -= 1
    return list(lines[start : header + 1])


def published_idle_scan_rate() -> float:
    """The one idle scan rate, derived from the one row that measured it.

    counts / seconds, checked against the rate the row itself prints. Deriving rather
    than reading is the point: the document cannot disagree with its own arithmetic.
    """
    matches = IDLE_SCAN_ROW.findall(HARDWARE_MD.read_text(encoding="utf-8"))
    assert len(matches) == 1, (
        f"docs/hardware.md must carry exactly one 20 s scan-counter row; found "
        f"{len(matches)}. That row is the source every other scan figure in this "
        f"repository is checked against."
    )
    counts, seconds, printed = matches[0]
    derived = int(counts.replace(",", "")) / float(seconds)
    assert abs(derived - float(printed)) / derived < 0.001, (
        f"docs/hardware.md section 17 says {counts} counts in {seconds} s and prints "
        f"{printed} scans/s, but {counts}/{seconds} is {derived:.1f}. The table "
        f"disagrees with its own arithmetic."
    )
    return derived


def test_the_published_scan_rate_is_derived_from_its_own_measurement() -> None:
    """The source row divides out to the rate it prints, to a tenth of a percent."""
    assert 1000.0 < published_idle_scan_rate() < 1040.0


def test_no_scan_rate_literal_drifts_from_the_published_one() -> None:
    """Every scan rate in the tree is the published figure, or a pair, or history.

    This is the test the third adversarial round asked for. The repository carried
    ~1024, ~1029 and 1018 for one quantity across four files; the pass that reconciled
    them wrote in ``docs/hardware.md`` section 17 that ``bench/soak.py``,
    ``tests/hardware/*`` and ``aslmp.testing`` were "not touched by this sweep" and
    "should be reconciled when the file is next opened". That sentence is a promise, and
    a promise is what this project keeps discovering it made instead of a fix. This is
    the fix: a fourth spelling of the idle scan rate fails the suite.

    Three ways to pass, and no fourth:

    * the published figure, ``1018`` (or the two host figures it rounds, 1018.0 wired
      and 1018.4 over Wi-Fi);
    * a loaded/idle pair belonging to one run, with its partner within four lines --
      because a cost-of-polling percentage inherits both of its endpoints and neither
      half means anything alone;
    * a retracted figure standing next to a word that says it is retracted.

    A named ``*_SCAN_RATE_*`` constant gets a fourth, stricter treatment: it must equal
    the measurement outright. It has no unit beside it for :data:`SCAN_FIGURE` to find,
    and it is the only copy of this number that anything *executes*.
    """
    rate = published_idle_scan_rate()
    rounded = f"{rate:.0f}"
    assert rounded in PUBLISHED_SCAN_RATES, (
        f"{rounded} is derived from docs/hardware.md section 17 but is not in "
        f"PUBLISHED_SCAN_RATES; the test and the document have come apart"
    )

    problems: list[str] = []
    for name, text in list(prose_sources()) + list(package_sources()):
        for constant, value in SCAN_RATE_CONSTANT.findall(text):
            if abs(float(value.replace("_", "")) - rate) > 0.5:
                problems.append(
                    f"{name}: {constant} = {value}, against the {rate:.1f} scans/s "
                    f"docs/hardware.md section 17 measures. A constant is the one copy "
                    f"of this number the code depends on."
                )
        lines = text.splitlines()
        for number, line in enumerate(lines):
            for value in SCAN_FIGURE.findall(line):
                if value in PUBLISHED_SCAN_RATES:
                    continue
                window = near(lines, number)
                partner = RUN_LOCAL_SCAN_RATES.get(value)
                if partner is not None and partner in window:
                    continue
                if any(marker in window for marker in RETRACTION_MARKERS):
                    continue
                problems.append(
                    f"{name}:{number + 1}: {value}/s. The published idle scan rate is "
                    f"{rounded}/s (docs/hardware.md section 17). A different number is "
                    f"allowed only as half of a named within-run pair, with its partner "
                    f"beside it, or as history with a word that says so."
                )
    assert not problems, "scan rates that have drifted:\n  " + "\n  ".join(problems)


def test_every_scan_period_is_arithmetic_on_the_published_rate() -> None:
    """``982 us per scan`` is ``1e6 / 1018``, and nothing else is.

    ``61.6 us per count`` shipped in the README's register map for a day, looking exactly
    like a specification. It was the ``U32`` misread's arithmetic residue -- the
    bit-pattern step of an f32 at the value the counter stood at -- and it is the reason
    this test exists rather than a review checklist. A period is now checked against the
    rate at whatever precision it is quoted to, so it cannot be typed independently.
    """
    expected = 1e6 / published_idle_scan_rate()
    problems: list[str] = []
    for name, text in list(prose_sources()) + list(package_sources()):
        lines = text.splitlines()
        for number, line in enumerate(lines):
            for value in SCAN_PERIOD.findall(line):
                decimals = len(value.partition(".")[2])
                if abs(float(value) - round(expected, decimals)) < 10**-decimals:
                    continue
                if any(marker in near(lines, number) for marker in RETRACTION_MARKERS):
                    continue
                problems.append(
                    f"{name}:{number + 1}: {value} us per scan, against "
                    f"1e6/{published_idle_scan_rate():.1f} = {expected:.1f}"
                )
    assert not problems, "scan periods that are not arithmetic on the rate:\n  " + "\n  ".join(
        problems
    )


def test_every_file_that_quotes_a_scan_figure_cites_where_it_came_from() -> None:
    """A number a reader cannot trace is a number nobody can check.

    Four files carried an idle scan rate as a bare fact of their own, and that is how
    three of them ended up disagreeing: nothing tied any of them to a measurement, so
    nothing noticed when one moved. Every one of them now names ``docs/hardware.md``
    section 17, which is the single place the rate and its conditions live.
    """
    problems: list[str] = []
    for name, text in list(prose_sources()) + list(package_sources()):
        if name == "docs/hardware.md":
            continue  # it IS the citation
        if not (SCAN_FIGURE.search(text) or SCAN_PERIOD.search(text)):
            continue
        lowered = text.lower()
        if "section 17" in lowered or "§17" in lowered or "§ 17" in lowered:
            continue
        problems.append(name)
    assert not problems, (
        "these quote a scan figure without citing docs/hardware.md section 17, which is "
        "where the measurement and its conditions live:\n  " + "\n  ".join(problems)
    )


def test_every_latency_table_names_its_host_and_its_medium() -> None:
    """The rule this project adopted in writing, applied to its own front door.

    ``docs/benchmarking.md``: "a latency table that does not name its link is not
    reproducible, no matter how good its control is. Name the host, the medium and the
    median RTT." That was written after withdrawing "TCP wins the tail", a claim that was
    correctly measured on one link and published as a property of the protocol. It was
    then not applied to the README's own tables, to the two-afternoon table that is the
    argument for demanding a control at all, or to the UDP queue ladder.

    A table counts as labelled if a host token and a medium token appear in its caption
    -- the prose between its header row and the end of whatever came before it, up to
    fifteen lines (:func:`caption_of`).
    """
    problems: list[str] = []
    for name, text in prose_sources():
        if not name.endswith(".md"):
            continue
        lines = text.splitlines()
        for header, table in markdown_tables(lines):
            if not any(LATENCY_COLUMN.search(row) for row in table):
                continue
            caption = "\n".join(caption_of(lines, header)).lower()
            if not any(host in caption for host in HOST_TOKENS):
                problems.append(f"{name}:{header + 1}: latency table with no host")
            elif not any(medium in caption for medium in MEDIUM_TOKENS):
                problems.append(f"{name}:{header + 1}: latency table with no medium")
    assert not problems, "latency tables missing their conditions:\n  " + "\n  ".join(problems)


def test_every_percentile_claim_in_prose_names_its_host_and_its_medium() -> None:
    """The same rule off the tables, which is where most of them actually were.

    ``p50 7.1 / p99 18.8 ms on one day and p50 10.3 / p99 95.2 ms on another`` appeared in
    three files -- ``README.md``, ``docs/benchmarking.md`` and ``bench/_report.py`` -- as
    the project's own argument for never publishing a latency number without a control,
    and named neither host nor link in any of them.
    """
    problems: list[str] = []
    for name, text in prose_sources():
        lines = text.splitlines()
        for number, line in enumerate(lines):
            if line.lstrip().startswith("|") or not PERCENTILE_CLAIM.search(line):
                continue
            window = near(lines, number, radius=12)
            if not any(host in window for host in HOST_TOKENS):
                problems.append(f"{name}:{number + 1}: a p50 with no host")
            elif not any(medium in window for medium in MEDIUM_TOKENS):
                problems.append(f"{name}:{number + 1}: a p50 with no medium")
    assert not problems, "percentile claims missing their conditions:\n  " + "\n  ".join(
        problems
    )


def test_no_option_help_claims_a_requirement_its_parser_does_not_enforce() -> None:
    """``aslmp read`` printed ``[--as {...}]`` above a help string beginning "REQUIRED".

    The brackets are argparse saying optional. The word was the author saying otherwise,
    and the exit status agreed with the author -- so the usage line, the only part of
    that a hurried reader parses, was the part that was false. It is the same defect as a
    docstring that contradicts its code, in the command the same session had just made
    required.

    ``--length``'s "required for --as str" is a conditional and is not caught: the
    pattern only fires on an unqualified claim about the option itself.
    """
    from aslmp.tools import SUBCOMMANDS

    claim = re.compile(r"\brequired\b(?!\s+(?:for|when|if|with|only|wherever))", re.I)
    problems: list[str] = []
    for command in SUBCOMMANDS:
        module = import_module(f"aslmp.tools.{command.replace('-', '_')}")
        builder = getattr(module, "build_parser", None)
        if builder is None:
            continue
        for action in builder()._actions:
            if not action.option_strings or not action.help:
                continue
            if claim.search(action.help) and not action.required:
                problems.append(
                    f"aslmp {command} {action.option_strings[0]}: help calls it "
                    f"required, argparse does not"
                )
    assert not problems, "help strings argparse contradicts:\n  " + "\n  ".join(problems)


def test_the_typed_flag_is_required_in_the_parser_on_both_halves_of_the_pair() -> None:
    """``read`` and ``write`` both, because the pair is how this keeps happening.

    ``--as`` was made required on ``aslmp read`` with the reasoning that "a register
    carries no type on the wire, so there is no default that could be right" -- and left
    defaulting to ``u16`` on ``aslmp write``, the half that moves a machine. Requiring it
    in the parser rather than in the body is what makes the usage line true; asserting
    both here is what stops the next one being fixed alone.
    """
    from aslmp.tools import read as read_tool
    from aslmp.tools import write as write_tool

    parsers = (("read", read_tool.build_parser()), ("write", write_tool.build_parser()))
    for label, parser in parsers:
        action = next(a for a in parser._actions if "--as" in a.option_strings)
        assert action.required, f"aslmp {label}: --as must be required in the parser"
        usage_line = parser.format_usage()
        assert "--as {" in usage_line, f"aslmp {label}: usage line does not show --as"
        assert "[--as" not in usage_line, (
            f"aslmp {label}: the usage line brackets --as, which means optional, and it "
            f"is not"
        )
