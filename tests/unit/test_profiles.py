"""The shipped profiles against ``aslmp/data/*.tsv``, row by row.

The TSVs are the independent oracle. ``aslmp/profile.py`` and ``aslmp/profiles/`` are
hand-written Python -- there is no generator for them, unlike the device and end-code
tables -- so the guard against drift is this file: every device range, every point
budget, every model code and every ambiguity in a shipped profile is compared against
the row it came from, in both directions, and a figure that exists in only one of the
two fails the build.

The comparison includes **provenance**, which is the point. A limit that quietly
promotes itself from MANUAL to LIVE, or an iQ-R row that claims a measurement, is a
false claim about hardware nobody here has, and it is exactly as wrong as a bad byte.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping

import pytest

from aslmp.data import Row, read_table
from aslmp.errors import (
    SlmpConfigurationError,
    SlmpEncodingNotSupportedError,
    SlmpProfileMismatchError,
)
from aslmp.profile import (
    BlockRule,
    Capability,
    ClearMode,
    CpuProfile,
    Encoding,
    Evidence,
    Family,
    Flat,
    Limit,
    LimitRule,
    Link,
    Refusal,
    Weighted,
)
from aslmp.profiles import ALL, KEYS, by_key, by_model_code, claiming
from aslmp.profiles.fx5u import BENCH_CPU, BENCH_FIRMWARE, FX5U
from aslmp.profiles.iq_r import IQ_R, IQR_R00
from aslmp.wire.citations import Measurement, Provenance
from aslmp.wire.codec import Unit
from aslmp.wire.devicetable import DEVICE_TABLE, Radix

# Profiles whose rows live in the data under a DIFFERENT profile_key. The iQ-R family
# profile carries the R04-group ranges and the family's budgets; the R00 profile is the
# narrower range table for three CPUs the family profile also covers, and shares those
# same budgets.
_LIMIT_KEY_IN_DATA: Mapping[str, str] = {"melsec:iq-r/r00": "melsec:iq-r"}
_RANGE_KEY_IN_DATA: Mapping[str, str] = {"melsec:iq-r": "melsec:iq-r/r04"}

# Families for which aslmp/data/ ships no range table at all. Declared here so that the
# absence is a listed exception rather than a silently skipped comparison.
_NO_RANGE_TABLE: frozenset[str] = frozenset({"melsec:q", "melsec:l"})

_PROVENANCE = {p.value: p for p in Provenance}
_RADIX = {"octal": Radix.OCTAL, "decimal": Radix.DECIMAL, "hexadecimal": Radix.HEXADECIMAL}
_UNIT = {"word": Unit.WORD, "bit": Unit.BIT}
_LINK = {"cpu": Link.CPU_BUILTIN, "enet": Link.ETHERNET_MODULE}


PROFILES: tuple[CpuProfile, ...] = tuple(ALL.values())
Q_PROFILE: CpuProfile = ALL["melsec:q"]


def profiles() -> Iterator[CpuProfile]:
    yield from PROFILES


def range_rows() -> tuple[Row, ...]:
    return read_table("ranges_iqf") + read_table("ranges_iqr")


def limits_key(profile: CpuProfile) -> str:
    return _LIMIT_KEY_IN_DATA.get(profile.key, profile.key)


def range_key(profile: CpuProfile) -> str:
    return _RANGE_KEY_IN_DATA.get(profile.key, profile.key)


def parse_rule(text: str) -> LimitRule:
    """``"weighted:12,14,1920"`` -> the rule object a profile should be carrying."""
    kind, _, arguments = text.partition(":")
    values = [int(part) for part in arguments.split(",")]
    if kind == "flat":
        return Flat(values[0])
    if kind == "weighted":
        return Weighted(values[0], values[1], values[2])
    if kind == "block":
        return BlockRule(values[0], values[1], values[2])
    raise AssertionError(f"unknown rule syntax in limits.tsv: {text!r}")


def encodings_for(profile: CpuProfile, coding: str) -> tuple[Encoding, ...]:
    """One ``ascii`` row serves every ASCII encoding the profile allows."""
    if coding == "binary":
        return (Encoding.BINARY,)
    return tuple(e for e in Encoding if e.is_ascii and e in profile.allowed_encodings)


def evidence_of(entry: Evidence | Refusal) -> Evidence:
    return entry if isinstance(entry, Evidence) else entry.evidence


def all_evidence(profile: CpuProfile) -> Iterator[tuple[str, Evidence]]:
    for name, value in profile.facts.items():
        yield f"facts[{name}]", value
    for cap, entry in profile.capabilities.items():
        yield f"capabilities[{cap.value}]", evidence_of(entry)
    for name, rng in profile.devices.items():
        yield f"devices[{name}]", rng.evidence
    for key, limit in profile.limits.items():
        yield f"limits[{key!r}]", limit.evidence


# ========================================================================================
# Device ranges against ranges_iqf.tsv / ranges_iqr.tsv
# ========================================================================================


@pytest.mark.parametrize("profile", PROFILES, ids=lambda p: p.key)
def test_every_family_in_the_generic_table_is_declared(profile: CpuProfile) -> None:
    """A family a profile has forgotten looks exactly like one it refuses."""
    assert set(profile.devices) == set(DEVICE_TABLE)


@pytest.mark.parametrize("profile", PROFILES, ids=lambda p: p.key)
def test_ranges_match_the_shipped_table(profile: CpuProfile) -> None:
    key = range_key(profile)
    rows = [row for row in range_rows() if row["profile_key"] == key]
    if profile.key in _NO_RANGE_TABLE:
        assert not rows, f"{key} now has range rows; delete its entry from _NO_RANGE_TABLE"
        pytest.skip(f"aslmp/data/ ships no device-range table for {key}")
    assert rows, f"no range rows for {key} in aslmp/data/"
    for row in rows:
        name = row["device"]
        rng = profile.devices[name]
        present = row["present"] == "yes"
        assert rng.present is present, f"{key} {name}: present"
        if not present:
            continue
        assert rng.configurable is (row["configurable"] == "yes"), f"{key} {name}: config"
        assert rng.notation == row["notation"], f"{key} {name}: notation"
        expected_first = int(row["first"]) if row["first"] else None
        expected_last = int(row["last"]) if row["last"] else None
        expected_points = int(row["points"]) if row["points"] else None
        assert (rng.first, rng.last, rng.points) == (
            expected_first,
            expected_last,
            expected_points,
        ), f"{key} {name}: range"
        if row["first"]:
            assert profile.radix_for(DEVICE_TABLE[name]) is _RADIX[row["radix"]], (
                f"{key} {name}: radix"
            )


@pytest.mark.parametrize("profile", PROFILES, ids=lambda p: p.key)
def test_range_provenance_matches_the_shipped_table(profile: CpuProfile) -> None:
    """A range that promoted itself to LIVE is a false claim about the silicon."""
    key = range_key(profile)
    for row in range_rows():
        if row["profile_key"] != key:
            continue
        evidence = profile.devices[row["device"]].evidence
        expected = _PROVENANCE[row["provenance"]]
        assert evidence.provenance is expected, f"{key} {row['device']}: provenance"
        origin = evidence.origin
        assert origin is not None, f"{key} {row['device']}: no origin"
        if expected is Provenance.LIVE:
            assert isinstance(origin, Measurement)
            assert (origin.cpu, origin.firmware) == (row["cpu"], row["firmware"])
        else:
            assert not isinstance(origin, Measurement)
            assert (origin.manual, origin.revision) == (row["manual"], row["revision"])
            assert origin.section == row["section"]


def test_the_radix_of_a_rangeless_row_comes_from_the_generic_device_table() -> None:
    """One recorded disagreement between two shipped tables, and which one wins.

    ``ranges_iqr.tsv`` prints ``decimal`` in the radix column of its ``DX``, ``DY``,
    ``RD``, ``G`` and ``BL`` rows -- the five that state no range at all, and that the
    file itself marks INFERRED because "the iQ-R performance table does not list this
    device separately". ``devices.tsv`` has ``DX`` and ``DY`` hexadecimal, citing
    SH(NA)-080956ENG's device-code table, and they address the same I/O as ``X`` and
    ``Y``, which are hexadecimal there too.

    The generic device table wins, and no iQ-R profile overrides a radix. The
    comparison above is therefore scoped to rows that state a range; this test exists so
    the exclusion is a decision on the record rather than a loop that quietly skips.
    """
    for name in ("DX", "DY"):
        assert DEVICE_TABLE[name].radix is Radix.HEXADECIMAL
        assert IQ_R.radix_for(DEVICE_TABLE[name]) is Radix.HEXADECIMAL
        assert name not in IQ_R.radix_overrides
        assert IQ_R.devices[name].first is None, "the row that disagrees states no range"


def test_the_fx5u_d_range_is_the_measured_one() -> None:
    """The single most quoted number in the package: D ends at D7999."""
    rng = FX5U.devices["D"]
    assert (rng.first, rng.last, rng.points) == (0, 7999, 8000)
    assert rng.evidence.provenance is Provenance.LIVE
    origin = rng.evidence.origin
    assert isinstance(origin, Measurement)
    assert "0xC056" in origin.note, "the measurement must name the code D8000 returned"


def test_iqr_file_registers_default_to_zero_points() -> None:
    """An out-of-the-box iQ-R has no file register: the message must say so."""
    for name in ("R", "ZR"):
        rng = IQ_R.devices[name]
        assert rng.present is True
        assert rng.points == 0
        assert "ZERO POINTS" in rng.evidence.note or "zero" in rng.evidence.note.lower()


# ========================================================================================
# Limits against limits.tsv
# ========================================================================================


@pytest.mark.parametrize("profile", PROFILES, ids=lambda p: p.key)
def test_limits_match_the_shipped_table(profile: CpuProfile) -> None:
    key = limits_key(profile)
    rows = [row for row in read_table("limits") if row["profile_key"] == key]
    assert rows, f"no limit rows for {key} in aslmp/data/"
    expected: dict[tuple[int, Encoding, Unit, Link], tuple[LimitRule, int, Provenance]] = {}
    for row in rows:
        command = int(row["command"], 16)
        for encoding in encodings_for(profile, row["coding"]):
            expected[(command, encoding, _UNIT[row["unit"]], _LINK[row["link"]])] = (
                parse_rule(row["rule"]),
                int(row["end_code"], 16),
                _PROVENANCE[row["provenance"]],
            )
    assert set(profile.limits) == set(expected), (
        f"{key}: the shipped limit keys and the limits.tsv rows disagree"
    )
    for limit_key, (rule, end_code, provenance) in expected.items():
        limit = profile.limits[limit_key]
        assert limit.rule == rule, f"{key} {limit_key}: rule"
        assert limit.end_code_if_exceeded == end_code, f"{key} {limit_key}: end code"
        assert limit.evidence.provenance is provenance, f"{key} {limit_key}: provenance"


def test_the_measured_fx5u_ceilings() -> None:
    """The four numbers the bench established, and the codes it answered with."""
    binary, cpu = Encoding.BINARY, Link.CPU_BUILTIN
    word = FX5U.limit(0x0401, binary, Unit.WORD, cpu)
    assert word.rule == Flat(960)
    assert word.end_code_if_exceeded == 0xC052
    assert word.evidence.provenance is Provenance.LIVE
    bit = FX5U.limit(0x0401, binary, Unit.BIT, cpu)
    assert bit.rule == Flat(3584), "the generic manual's 7168 is not this CPU's ceiling"
    assert bit.end_code_if_exceeded == 0xC051
    assert FX5U.limit(0x0403, binary, Unit.WORD, cpu).rule == Flat(192)
    assert FX5U.limit(0x1402, binary, Unit.WORD, cpu).rule == Weighted(12, 14, 1920)


def test_the_link_is_part_of_the_limit_key() -> None:
    """192 is the built-in port's Read Random ceiling; an FX5-ENET's is 123."""
    binary = Encoding.BINARY
    assert FX5U.limit(0x0403, binary, Unit.WORD, Link.CPU_BUILTIN).rule == Flat(192)
    assert FX5U.limit(0x0403, binary, Unit.WORD, Link.ETHERNET_MODULE).rule == Flat(123)
    assert FX5U.limit(0x1401, binary, Unit.WORD, Link.ETHERNET_MODULE).rule == Flat(949)


def test_a_missing_limit_key_raises_and_does_not_borrow_a_neighbour() -> None:
    with pytest.raises(SlmpConfigurationError) as caught:
        IQ_R.limit(0x0403, Encoding.BINARY, Unit.WORD, Link.ETHERNET_MODULE)
    assert "0x0403" in str(caught.value)
    assert "0x0403/binary/word/cpu" in str(caught.value), "the message lists what we have"


# ========================================================================================
# Model codes against model_codes.tsv
# ========================================================================================


def test_model_codes_match_the_shipped_table() -> None:
    rows = read_table("model_codes")
    expected: dict[str, dict[int, str]] = {}
    for row in rows:
        if not row["profile_key"]:
            continue
        expected.setdefault(row["profile_key"], {})[int(row["code"], 16)] = row["model"]
    for key, codes in expected.items():
        assert dict(ALL[key].model_codes) == codes, f"{key}: model codes"
    for profile in profiles():
        if profile is IQR_R00:
            continue  # a narrower view of codes melsec:iq-r also claims
        assert dict(profile.model_codes) == expected[profile.key]


def test_the_family_catch_all_code_is_claimed_by_nobody() -> None:
    """0x0360 RCPU names a family, not a model. connect() must raise on it."""
    assert claiming(0x0360) == ()
    with pytest.raises(SlmpProfileMismatchError):
        by_model_code(0x0360)


def test_the_one_model_code_seen_off_a_wire() -> None:
    assert by_model_code(0x4A49) is FX5U
    assert FX5U.model_codes[0x4A49] == BENCH_CPU


# ========================================================================================
# Ambiguities against ambiguities.tsv
# ========================================================================================


def test_ambiguities_are_verbatim_from_the_shipped_table() -> None:
    """The table handed to a Mitsubishi engineer must be one table, not two."""
    rows = {row["key"]: row for row in read_table("ambiguities")}
    seen: set[str] = set()
    for profile in profiles():
        for ambiguity in profile.ambiguities:
            seen.add(ambiguity.key)
            row = rows.get(ambiguity.key)
            assert row is not None, f"{ambiguity.key} is not in ambiguities.tsv"
            assert ambiguity.question == row["question"], ambiguity.key
            assert ambiguity.readings == tuple(row["readings"].split("|")), ambiguity.key
            assert ambiguity.chosen == row["chosen"], ambiguity.key
    assert "A-IQF-XY" in seen and "A-IQF-MONITOR" in seen


def test_every_iqf_profile_carries_the_xy_ambiguity() -> None:
    for profile in profiles():
        if profile.family is not Family.IQ_F:
            continue
        keys = {a.key for a in profile.ambiguities}
        assert {"A-IQF-XY", "A-XY-OCTAL-LEGALITY", "A-IQF-MONITOR"} <= keys


# ========================================================================================
# Provenance honesty
# ========================================================================================


@pytest.mark.parametrize("profile", PROFILES, ids=lambda p: p.key)
def test_only_the_fx5u_profile_claims_a_measurement(profile: CpuProfile) -> None:
    """A measurement names one piece of silicon. Nothing else may borrow it.

    The FX5UC, FX5UJ and FX5S carry the same figures from the same manual, and the
    monitor and long-spec refusals cite the FX5U measurement in their evidence NOTE --
    but their provenance is MANUAL, because nobody here has plugged one in.
    """
    for where, evidence in all_evidence(profile):
        if evidence.provenance is not Provenance.LIVE:
            continue
        assert profile is FX5U or where.startswith("capabilities["), (
            f"{profile.key} {where} claims a live measurement"
        )
        origin = evidence.origin
        assert isinstance(origin, Measurement), f"{profile.key} {where}"
        assert (origin.cpu, origin.firmware) == (BENCH_CPU, BENCH_FIRMWARE)


@pytest.mark.parametrize("profile", PROFILES, ids=lambda p: p.key)
def test_the_unverified_families_claim_nothing_live(profile: CpuProfile) -> None:
    if profile.family is Family.IQ_F:
        pytest.skip("iQ-F is the measured family")
    counts = profile.provenance_counts()
    assert counts[Provenance.LIVE] == 0, f"{profile.key} claims a measurement"
    assert counts[Provenance.MANUAL] + counts[Provenance.INFERRED] > 0


@pytest.mark.parametrize("profile", PROFILES, ids=lambda p: p.key)
def test_every_shipped_fact_carries_an_origin(profile: CpuProfile) -> None:
    """Rule 4: a limit or a range with no manual and no measurement does not ship."""
    for where, evidence in all_evidence(profile):
        assert evidence.origin is not None, f"{profile.key} {where} has no source"


# ========================================================================================
# Radix, notation and capabilities
# ========================================================================================


@pytest.mark.parametrize("profile", PROFILES, ids=lambda p: p.key)
def test_xy_is_octal_on_iq_f_and_hexadecimal_everywhere_else(profile: CpuProfile) -> None:
    """The measured fact the whole no-generic-profile rule exists to protect."""
    expected = Radix.OCTAL if profile.family is Family.IQ_F else Radix.HEXADECIMAL
    for name in ("X", "Y"):
        assert profile.radix_for(DEVICE_TABLE[name]) is expected


@pytest.mark.parametrize("profile", PROFILES, ids=lambda p: p.key)
def test_no_other_family_has_its_radix_overridden(profile: CpuProfile) -> None:
    for name, dt in DEVICE_TABLE.items():
        if name in {"X", "Y"}:
            continue
        assert profile.radix_for(dt) is dt.radix


@pytest.mark.parametrize("profile", PROFILES, ids=lambda p: p.key)
def test_ascii_xy_oct_is_iq_f_only(profile: CpuProfile) -> None:
    allowed = Encoding.ASCII_XY_OCT in profile.allowed_encodings
    assert allowed is (profile.family is Family.IQ_F)
    if allowed:
        return
    with pytest.raises(SlmpEncodingNotSupportedError):
        profile.notation_for(DEVICE_TABLE["X"], Encoding.ASCII_XY_OCT)


@pytest.mark.parametrize("profile", PROFILES, ids=lambda p: p.key)
def test_every_capability_is_declared(profile: CpuProfile) -> None:
    assert set(profile.capabilities) == set(Capability)


@pytest.mark.parametrize("profile", PROFILES, ids=lambda p: p.key)
def test_monitor_is_refused_exactly_on_iq_f(profile: CpuProfile) -> None:
    """0x0801 / 0x0802 return 0xC059 on an iQ-F, measured twice. Never emulated."""
    entry = profile.capabilities[Capability.MONITOR]
    if profile.family is not Family.IQ_F:
        assert isinstance(entry, Evidence)
        return
    assert isinstance(entry, Refusal)
    assert entry.end_code_if_attempted == 0xC059
    assert entry.evidence.provenance is Provenance.LIVE
    assert "read_random" in entry.alternative


@pytest.mark.parametrize("profile", PROFILES, ids=lambda p: p.key)
def test_the_long_device_spec_is_available_only_on_iq_r(profile: CpuProfile) -> None:
    entry = profile.capabilities[Capability.LONG_DEVICE_SPEC]
    expected_supported = profile.family is Family.IQ_R
    assert isinstance(entry, Evidence) is expected_supported


@pytest.mark.parametrize("profile", PROFILES, ids=lambda p: p.key)
def test_the_remote_fixed_field_is_per_family_and_never_guessed(
    profile: CpuProfile,
) -> None:
    expected = b"\x00\x00" if profile.family is Family.IQ_F else b"\x01\x00"
    assert profile.remote_fixed_field == expected
    assert "A-REMOTE-FIXED" in profile.evidence_for("remote_fixed_field").note


@pytest.mark.parametrize("profile", PROFILES, ids=lambda p: p.key)
def test_iq_f_allows_only_the_documented_clear_mode(profile: CpuProfile) -> None:
    if profile.family is Family.IQ_F:
        assert profile.allowed_clear_modes == frozenset({ClearMode.NONE})
    else:
        assert profile.allowed_clear_modes == frozenset(ClearMode)


# ========================================================================================
# The registry
# ========================================================================================


def test_by_key_round_trips_every_shipped_profile() -> None:
    for key in KEYS:
        assert by_key(key).key == key


def test_an_unknown_key_lists_the_valid_ones_and_names_no_fallback() -> None:
    with pytest.raises(SlmpConfigurationError) as caught:
        by_key("iq-f")
    message = str(caught.value)
    for key in KEYS:
        assert repr(key) in message
    assert "no generic profile" in message
    assert "aslmp identify" in message


@pytest.mark.parametrize("bad", ["", "melsec:iq-f", "MELSEC:IQ-F/FX5U", "fx5u"])
def test_a_near_miss_is_still_a_refusal(bad: str) -> None:
    """Nothing normalises case or accepts a short name: an alias goes ambiguous quietly."""
    with pytest.raises(SlmpConfigurationError):
        by_key(bad)


def test_by_model_code_answers_with_exactly_one_profile() -> None:
    for profile in ALL.values():
        for code in profile.model_codes:
            resolved = by_model_code(code)
            assert profile in claiming(code)
            assert resolved in claiming(code)


def test_the_r00_overlap_is_visible_rather_than_resolved_silently() -> None:
    assert by_model_code(0x48A0) is IQ_R
    assert claiming(0x48A0) == (IQ_R, IQR_R00)
    assert IQR_R00.devices["M"].last == 8191
    assert IQ_R.devices["M"].last == 12287


def test_profile_keys_are_unique_and_stable() -> None:
    assert len(set(KEYS)) == len(KEYS)
    assert KEYS[0] == "melsec:iq-f/fx5u"
    for key in KEYS:
        assert re.fullmatch(r"melsec:[a-z0-9/-]+", key), key


def test_a_profile_reprs_as_its_key_and_not_as_four_hundred_dataclasses() -> None:
    assert repr(FX5U) == "CpuProfile('melsec:iq-f/fx5u')"
    assert str(FX5U) == "melsec:iq-f/fx5u"


def test_profiles_compare_by_identity() -> None:
    """Two profiles with identical tables are still two different targets."""
    assert FX5U == FX5U
    assert FX5U.replace(key="melsec:iq-f/fx5u-copy") != FX5U
    assert len({FX5U, IQ_R, Q_PROFILE}) == 3


def test_limits_are_keyed_only_under_allowed_encodings() -> None:
    for profile in profiles():
        for _command, encoding, _unit, _link in profile.limits:
            assert encoding in profile.allowed_encodings, profile.key


def test_evidence_for_resolves_the_documented_names() -> None:
    assert FX5U.evidence_for("monitor").provenance is Provenance.LIVE
    assert FX5U.evidence_for("D").provenance is Provenance.LIVE
    assert isinstance(FX5U.evidence_for("0x0403/binary/word/cpu"), Evidence)
    assert FX5U.evidence_for("xy-radix").measurement is not None
    with pytest.raises(ValueError, match="no fact named"):
        FX5U.evidence_for("the-cpu's-favourite-colour")


def test_sources_are_quotable_references() -> None:
    references = [source.reference for source in FX5U.sources()]
    assert any("JY997D56001-K" in reference for reference in references)
    assert any(BENCH_CPU in reference for reference in references)


def test_every_limit_is_a_limit_and_every_rule_is_sealed() -> None:
    for profile in profiles():
        for limit in profile.limits.values():
            assert isinstance(limit, Limit)
            assert type(limit.rule) in {Flat, Weighted, BlockRule}
