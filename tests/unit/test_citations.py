"""The provenance value types, and the integrity of every shipped data table.

Two things are being defended here.

The first is that a fact with no provenance cannot be represented: ``Citation`` and
``Measurement`` validate at construction, and there is no "unknown manual" default to
fall back to. The second is that the tables in ``aslmp/data/`` actually carry what they
claim to -- every row a manual, a revision and a section, or else a CPU model and a
firmware version. These tests force *presence* and *shape*; they cannot force truth,
which is stated plainly as a residual weakness in DESIGN section 7.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
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
    kwargs = {"cpu": FX5U, "firmware": FX5U_FW, "date": "2026-09-06"}
    kwargs[field] = ""
    with pytest.raises(ValueError, match=field):
        Measurement(**kwargs)


@pytest.mark.parametrize("date", ["06/09/2026", "20260906", "2026-9-6", "yesterday"])
def test_measurement_refuses_a_date_that_is_not_iso(date: str) -> None:
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        Measurement(FX5U, FX5U_FW, date)


def test_measurement_refuses_a_date_that_is_not_a_real_day() -> None:
    with pytest.raises(ValueError, match="calendar date"):
        Measurement(FX5U, FX5U_FW, "2026-02-30")


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
        for field in ("cpu", "firmware", "measured"):
            assert not row[field], (
                f"{where}: provenance is {row['provenance']!r} but {field} is set. "
                f"Either it was measured, in which case say live, or it was not."
            )


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
