"""``aslmp/profile.py``: the types, their refusals, and the two checks that matter.

``check_range`` validates the **span**, not the head address, and ``check_points``
implements a weighted budget rather than a flat count. Both are tested at their
boundaries in both directions, because both are wrong in a way that reaches the PLC:
a start-only range check passes ``D7999`` read as two words straight through, and a flat
point count refuses 160 word points that fit while allowing 138 double-word points that
do not.

Everything here is pure. No socket, no event loop, no file.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from aslmp.errors import (
    SlmpAddressRangeError,
    SlmpCapabilityError,
    SlmpConfigurationError,
    SlmpDeviceNotAllowedHereError,
    SlmpDeviceNotOnCpuError,
    SlmpEncodingNotSupportedError,
    SlmpPointLimitError,
)
from aslmp.profile import (
    BlockRule,
    Capability,
    ClearMode,
    CpuProfile,
    DeviceRange,
    Encoding,
    Evidence,
    Family,
    Flat,
    Limit,
    LimitKey,
    LimitRule,
    Link,
    Refusal,
    Weighted,
)
from aslmp.profiles import FX5U, IQ_R, Q
from aslmp.wire.address import DeviceAddress, parse_address
from aslmp.wire.citations import Citation, Measurement, Provenance
from aslmp.wire.codec import Notation, SpecFormat, Unit
from aslmp.wire.devicetable import DEVICE_TABLE, Radix

CITATION = Citation("JY997D56001", "K", "4.2 (p.69)")
INFERRED_CITATION = Citation(
    "JY997D56001", "K", "4.2 (p.69)", note="carried", provenance=Provenance.INFERRED
)
MEASUREMENT = Measurement(
    cpu="FX5U-32MT/DS", firmware="1.065", date="2026-09-06", note="a real bench run"
)
DOCUMENTED = Evidence.documented(CITATION)

WORD = Unit.WORD
BIT = Unit.BIT
CPU = Link.CPU_BUILTIN
BINARY = Encoding.BINARY


def address(text: str, profile: CpuProfile = FX5U) -> DeviceAddress:
    return parse_address(text, profile)


# ========================================================================================
# Evidence
# ========================================================================================


def test_a_measurement_becomes_live_evidence_that_quotes_the_silicon() -> None:
    evidence = Evidence.measured(MEASUREMENT, note="why it matters")
    assert evidence.provenance is Provenance.LIVE
    assert evidence.source == "FX5U-32MT/DS fw 1.065, 2026-09-06"
    assert evidence.measurement is MEASUREMENT
    assert evidence.citation is None
    assert "measured" in str(evidence)


def test_a_citation_carries_its_own_provenance_into_the_evidence() -> None:
    assert Evidence.documented(CITATION).provenance is Provenance.MANUAL
    assert Evidence.documented(INFERRED_CITATION).provenance is Provenance.INFERRED
    assert Evidence.documented(INFERRED_CITATION).citation is INFERRED_CITATION


def test_evidence_refuses_a_provenance_that_contradicts_its_origin() -> None:
    """A measured fact labelled MANUAL is the quiet lie this type exists to stop."""
    with pytest.raises(ValueError, match="a measurement is LIVE"):
        Evidence(Provenance.MANUAL, MEASUREMENT.reference, "", MEASUREMENT)
    with pytest.raises(ValueError, match="provenance"):
        Evidence(Provenance.LIVE, CITATION.reference, "", CITATION)


def test_evidence_refuses_a_source_that_does_not_match_its_origin() -> None:
    with pytest.raises(ValueError, match="does not match its origin"):
        Evidence(Provenance.MANUAL, "some other manual", "", CITATION)


@pytest.mark.parametrize("source", ["", "   "])
def test_evidence_refuses_a_blank_source(source: str) -> None:
    with pytest.raises(ValueError, match="non-blank"):
        Evidence(Provenance.INFERRED, source)


def test_evidence_refuses_the_wrong_type_of_origin() -> None:
    with pytest.raises(TypeError):
        Evidence.measured(CITATION)  # type: ignore[arg-type]  # the wrong kind on purpose
    with pytest.raises(TypeError):
        Evidence.documented(MEASUREMENT)  # type: ignore[arg-type]  # ditto


# ========================================================================================
# Limit rules
# ========================================================================================


def test_limit_rule_is_sealed() -> None:
    """check_points feeds each shape a different set of counts; a new one is unchecked."""
    with pytest.raises(TypeError, match="sealed"):

        class Sneaky(LimitRule):
            pass


def test_flat_counts_word_dword_and_bit_together() -> None:
    rule = Flat(192)
    assert rule.exceeded_by(word=96, dword=96, bit=0, blocks=0) is None
    assert rule.exceeded_by(word=192, dword=0, bit=0, blocks=0) is None
    over = rule.exceeded_by(word=96, dword=97, bit=0, blocks=0)
    assert over is not None
    assert "193" in over and "192" in over


def test_the_weighted_rule_is_wrong_in_both_directions_as_a_flat_count() -> None:
    """160 word points fit; 138 double-word points do not. A flat 192 gets both wrong."""
    rule = Weighted(12, 14, 1920)
    assert rule.exceeded_by(word=160, dword=0, bit=0, blocks=0) is None
    assert rule.exceeded_by(word=161, dword=0, bit=0, blocks=0) is not None
    assert rule.exceeded_by(word=0, dword=137, bit=0, blocks=0) is None
    assert rule.exceeded_by(word=0, dword=138, bit=0, blocks=0) is not None
    assert rule.describe() == "word x 12 + dword x 14 <= 1920"


def test_the_block_rule_caps_blocks_and_charges_per_block_overhead() -> None:
    rule = BlockRule(120, 4, 760)
    assert rule.exceeded_by(word=0, dword=0, bit=0, blocks=121) is not None
    assert rule.exceeded_by(word=700, dword=0, bit=0, blocks=15) is None
    over = rule.exceeded_by(word=701, dword=0, bit=0, blocks=15)
    assert over is not None
    assert "761" in over and "760" in over


@pytest.mark.parametrize(
    "build",
    [
        lambda: Flat(0),
        lambda: Weighted(0, 14, 1920),
        lambda: Weighted(12, 14, 0),
        lambda: BlockRule(0, 0, 960),
        lambda: BlockRule(120, -1, 960),
        lambda: BlockRule(120, 0, 0),
    ],
)
def test_a_rule_with_an_impossible_ceiling_does_not_build(build: Any) -> None:
    with pytest.raises(ValueError):
        build()


def test_a_limit_needs_a_sixteen_bit_end_code_and_real_evidence() -> None:
    with pytest.raises(ValueError, match="16-bit end code"):
        Limit(Flat(960), 0x1C052, DOCUMENTED)
    with pytest.raises(TypeError):
        Limit(Flat(960), 0xC052, "measured on my bench")  # type: ignore[arg-type]


# ========================================================================================
# DeviceRange
# ========================================================================================


def test_a_present_range_derives_its_point_count() -> None:
    rng = DeviceRange("D", True, 0, 7999, evidence=DOCUMENTED)
    assert rng.points == 8000
    assert "D0 to D7999" in rng.span_text


def test_a_range_whose_point_count_disagrees_with_its_bounds_does_not_build() -> None:
    with pytest.raises(ValueError, match="8000"):
        DeviceRange("D", True, 0, 7999, points=7999, evidence=DOCUMENTED)


def test_an_absent_family_must_say_why() -> None:
    with pytest.raises(ValueError, match="gives no reason"):
        DeviceRange("V", False, evidence=DOCUMENTED)
    with pytest.raises(ValueError, match="no device numbers"):
        DeviceRange("V", False, 0, 10, absent_reason="nope", evidence=DOCUMENTED)


def test_a_zero_point_family_must_say_why() -> None:
    with pytest.raises(ValueError, match="no note"):
        DeviceRange("R", True, points=0, evidence=DOCUMENTED)
    ok = DeviceRange(
        "R",
        True,
        points=0,
        evidence=Evidence.documented(CITATION, note="none allocated by default"),
    )
    assert ok.span_text == "0 points allocated"


def test_a_family_with_no_static_range_must_say_why() -> None:
    with pytest.raises(ValueError, match="never means 'unknown'"):
        DeviceRange("G", True, evidence=DOCUMENTED)
    ok = DeviceRange("G", True, evidence=Evidence.documented(CITATION, note="per module"))
    assert ok.span_text == "no static range"


def test_a_range_for_a_family_the_generic_table_never_heard_of_does_not_build() -> None:
    with pytest.raises(ValueError, match="not a device family"):
        DeviceRange("QQ", True, 0, 10, evidence=DOCUMENTED)


def test_a_backwards_range_does_not_build() -> None:
    with pytest.raises(ValueError, match="before its first"):
        DeviceRange("D", True, 100, 10, evidence=DOCUMENTED)


# ========================================================================================
# CpuProfile construction
# ========================================================================================


def profile_with(**changes: Any) -> CpuProfile:
    return dataclasses.replace(FX5U, **changes)


def test_a_profile_that_forgets_a_device_family_does_not_build() -> None:
    devices = dict(FX5U.devices)
    del devices["ZR"]
    with pytest.raises(ValueError, match=r"\['ZR'\]"):
        profile_with(devices=devices)


def test_a_profile_that_forgets_a_capability_does_not_build() -> None:
    capabilities = dict(FX5U.capabilities)
    del capabilities[Capability.MONITOR]
    with pytest.raises(ValueError, match="monitor"):
        profile_with(capabilities=capabilities)


def test_a_limit_keyed_under_a_disallowed_encoding_does_not_build() -> None:
    with pytest.raises(ValueError, match="does not allow"):
        profile_with(allowed_encodings=frozenset({Encoding.BINARY}))


def test_ascii_xy_oct_off_iq_f_does_not_build() -> None:
    with pytest.raises(ValueError, match="ASCII_XY_OCT"):
        dataclasses.replace(
            IQ_R,
            allowed_encodings=frozenset({Encoding.BINARY, Encoding.ASCII_XY_OCT}),
        )


def test_a_profile_must_allow_binary() -> None:
    with pytest.raises(ValueError, match="does not allow binary"):
        profile_with(allowed_encodings=frozenset({Encoding.ASCII_XY_HEX}))


@pytest.mark.parametrize("field", [b"", b"\x00", b"\x00\x00\x00"])
def test_the_remote_fixed_field_is_exactly_two_bytes(field: bytes) -> None:
    with pytest.raises(ValueError, match="exactly two"):
        profile_with(remote_fixed_field=field)


def test_a_profile_must_allow_the_no_clear_mode() -> None:
    with pytest.raises(ValueError, match=r"ClearMode\.NONE"):
        profile_with(allowed_clear_modes=frozenset({ClearMode.EXCEPT_LATCH}))


def test_a_radix_override_for_an_unknown_family_does_not_build() -> None:
    with pytest.raises(ValueError, match="unknown family"):
        profile_with(radix_overrides={"QQ": Radix.OCTAL})


def test_the_tables_are_frozen_against_the_caller_who_passed_them() -> None:
    """A bound plan validates once; a dict the caller still holds could widen it after."""
    devices = dict(FX5U.devices)
    built = profile_with(devices=devices)
    devices["D"] = DeviceRange(
        "D", True, 0, 999999, evidence=Evidence.documented(CITATION)
    )
    assert built.devices["D"].last == 7999
    with pytest.raises(TypeError):
        built.devices["D"] = devices["D"]  # type: ignore[index]  # a MappingProxyType


# ========================================================================================
# radix_for / notation_for
# ========================================================================================


def test_the_iq_f_octal_override_applies_to_x_and_y_only() -> None:
    assert FX5U.radix_for(DEVICE_TABLE["X"]) is Radix.OCTAL
    assert FX5U.radix_for(DEVICE_TABLE["Y"]) is Radix.OCTAL
    assert FX5U.radix_for(DEVICE_TABLE["B"]) is Radix.HEXADECIMAL
    assert FX5U.radix_for(DEVICE_TABLE["D"]) is Radix.DECIMAL


def test_octal_digits_are_emitted_only_for_iq_f_x_and_y_in_ascii_xy_oct() -> None:
    x = DEVICE_TABLE["X"]
    assert FX5U.notation_for(x, Encoding.ASCII_XY_OCT) is Notation.OCTAL_DIGITS
    assert FX5U.notation_for(x, Encoding.ASCII_XY_HEX) is Notation.VALUE
    assert FX5U.notation_for(x, Encoding.BINARY) is Notation.VALUE
    assert FX5U.notation_for(DEVICE_TABLE["D"], Encoding.ASCII_XY_OCT) is Notation.VALUE


def test_an_encoding_the_profile_does_not_allow_is_refused_not_coerced() -> None:
    with pytest.raises(SlmpEncodingNotSupportedError) as caught:
        IQ_R.notation_for(DEVICE_TABLE["X"], Encoding.ASCII_XY_OCT)
    assert "iQ-F" in str(caught.value)
    assert "ascii-xy-hex" in str(caught.value), "the message lists what is allowed"


# ========================================================================================
# check_range -- the span, not the start
# ========================================================================================


def test_the_last_register_alone_is_legal_and_two_of_it_is_not() -> None:
    """D7999 returned 0x0000; D8000 and D7999-with-2-points both returned 0xC056."""
    FX5U.check_range(address("D7999"), 1, width=WORD)
    with pytest.raises(SlmpAddressRangeError) as caught:
        FX5U.check_range(address("D7999"), 2, width=WORD)
    message = str(caught.value)
    assert "D7999" in message and "D8000" in message and "0xC056" in message


def test_a_word_read_of_a_bit_device_covers_sixteen_device_numbers_per_point() -> None:
    FX5U.check_range(address("M32752"), 1, width=WORD)
    with pytest.raises(SlmpAddressRangeError) as caught:
        FX5U.check_range(address("M32753"), 1, width=WORD)
    assert "16 bit device numbers" in str(caught.value)


def test_the_octal_span_is_computed_on_the_index_and_printed_in_octal() -> None:
    """Y1770 is index 1016; eight bits reach index 1023, which prints as Y1777."""
    FX5U.check_range(address("Y1770"), 8, width=BIT)
    with pytest.raises(SlmpAddressRangeError) as caught:
        FX5U.check_range(address("Y1770"), 9, width=BIT)
    assert "Y2000" in str(caught.value), "the reach is rendered in the device's radix"


def test_a_bit_unit_request_against_a_word_device_is_refused() -> None:
    with pytest.raises(SlmpDeviceNotAllowedHereError, match="word device"):
        FX5U.check_range(address("D0"), 1, width=BIT)


def test_a_family_this_cpu_does_not_have_is_refused_with_the_reason() -> None:
    with pytest.raises(SlmpDeviceNotOnCpuError) as caught:
        FX5U.check_range(DeviceAddress.of(DEVICE_TABLE["V"], 0, radix=Radix.DECIMAL), 1,
                         width=BIT)
    assert "edge relay" in str(caught.value).lower()
    assert "0xC05C" in str(caught.value), "the measured end code is quoted"


def test_a_family_with_no_points_allocated_says_so_rather_than_out_of_range() -> None:
    with pytest.raises(SlmpAddressRangeError) as caught:
        IQ_R.check_range(DeviceAddress.of(DEVICE_TABLE["ZR"], 0, radix=Radix.HEXADECIMAL),
                         1, width=WORD)
    message = str(caught.value)
    assert "0 points" in message
    assert "with_ranges" in message


def test_a_family_with_no_static_range_is_not_span_checked() -> None:
    """G depends on which module is mounted. That is per-installation, not unknown."""
    FX5U.check_range(DeviceAddress.of(DEVICE_TABLE["G"], 60000, radix=Radix.DECIMAL),
                     100, width=WORD)


def test_zero_points_is_a_point_count_error_and_names_the_measured_code() -> None:
    with pytest.raises(SlmpPointLimitError) as caught:
        FX5U.check_range(address("D0"), 0, width=WORD)
    assert "0xC052" in str(caught.value)


def test_a_negative_point_count_is_a_configuration_error() -> None:
    with pytest.raises(SlmpConfigurationError):
        FX5U.check_range(address("D0"), -1, width=WORD)


def test_check_range_rejects_the_wrong_argument_types() -> None:
    with pytest.raises(TypeError):
        FX5U.check_range("D0", 1, width=WORD)  # type: ignore[arg-type]  # a str, not an address
    with pytest.raises(TypeError):
        FX5U.check_range(address("D0"), 1, width="word")  # type: ignore[arg-type]


# ========================================================================================
# check_points -- the weighted budget
# ========================================================================================


def batch_key(unit: Unit = WORD) -> LimitKey:
    return (0x0401, BINARY, unit, CPU)


def test_the_measured_batch_ceilings_are_the_boundary() -> None:
    FX5U.check_points(batch_key(), word=960)
    with pytest.raises(SlmpPointLimitError) as caught:
        FX5U.check_points(batch_key(), word=961)
    assert "0xC052" in str(caught.value)
    FX5U.check_points(batch_key(BIT), bit=3584)
    with pytest.raises(SlmpPointLimitError) as caught:
        FX5U.check_points(batch_key(BIT), bit=3585)
    assert "0xC051" in str(caught.value)


def test_the_random_read_ceiling_is_the_sum_of_both_point_kinds() -> None:
    key: LimitKey = (0x0403, BINARY, WORD, CPU)
    FX5U.check_points(key, word=96, dword=96)
    with pytest.raises(SlmpPointLimitError) as caught:
        FX5U.check_points(key, word=96, dword=97)
    assert "0xC054" in str(caught.value)


def test_the_random_write_budget_is_weighted_and_the_message_shows_the_arithmetic() -> None:
    key: LimitKey = (0x1402, BINARY, WORD, CPU)
    FX5U.check_points(key, word=160)
    FX5U.check_points(key, dword=137)
    with pytest.raises(SlmpPointLimitError) as caught:
        FX5U.check_points(key, dword=138)
    message = str(caught.value)
    assert "138 double-word x 14" in message and "1932" in message and "1920" in message


def test_zero_points_is_refused_before_the_budget_is_consulted() -> None:
    with pytest.raises(SlmpPointLimitError) as caught:
        FX5U.check_points(batch_key())
    assert "0xC052" in str(caught.value)
    assert "no points at all" in str(caught.value)


def test_a_count_a_rule_cannot_spend_is_refused_rather_than_ignored() -> None:
    """An ignored count is an unchecked count."""
    with pytest.raises(SlmpConfigurationError, match="blocks"):
        FX5U.check_points(batch_key(), word=1, blocks=1)
    with pytest.raises(SlmpConfigurationError, match="BlockRule"):
        FX5U.check_points((0x0406, BINARY, WORD, CPU), word=10)
    with pytest.raises(SlmpConfigurationError, match="Weighted"):
        FX5U.check_points((0x1402, BINARY, WORD, CPU), word=1, bit=1)


def test_a_negative_count_is_refused() -> None:
    with pytest.raises(SlmpConfigurationError, match="cannot be negative"):
        FX5U.check_points(batch_key(), word=-1)


def test_the_block_budget_charges_the_inferred_per_block_overhead() -> None:
    key: LimitKey = (0x1406, BINARY, WORD, CPU)
    FX5U.check_points(key, word=700, blocks=15)
    with pytest.raises(SlmpPointLimitError):
        FX5U.check_points(key, word=701, blocks=15)
    assert FX5U.limits[key].evidence.provenance is Provenance.INFERRED


# ========================================================================================
# Capabilities
# ========================================================================================


def test_monitor_is_refused_pre_transport_with_the_measurement_and_an_alternative() -> None:
    assert FX5U.supports(Capability.MONITOR) is False
    with pytest.raises(SlmpCapabilityError) as caught:
        FX5U.require(Capability.MONITOR, what="monitor_register(['D0','D4'])")
    message = str(caught.value)
    assert "monitor_register(['D0','D4'])" in message
    assert "0x0801" in message and "0xC059" in message
    assert "FX5U-32MT/DS fw 1.065" in message
    assert "read_random" in message


def test_a_supported_capability_is_silent() -> None:
    assert FX5U.supports(Capability.BATCH_ACCESS) is True
    FX5U.require(Capability.BATCH_ACCESS, what="read_words('D0', 10)")


def test_the_long_device_spec_is_refused_on_iq_f_and_allowed_on_iq_r() -> None:
    with pytest.raises(SlmpCapabilityError, match="0x0002"):
        FX5U.require(Capability.LONG_DEVICE_SPEC, what="spec=SpecFormat.LONG")
    assert IQ_R.supports(Capability.LONG_DEVICE_SPEC) is True
    assert Q.supports(Capability.LONG_DEVICE_SPEC) is False


def test_require_and_supports_reject_a_non_capability() -> None:
    with pytest.raises(TypeError):
        FX5U.supports("monitor")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        FX5U.require("monitor", what="x")  # type: ignore[arg-type]


def test_a_refusal_needs_a_reason_and_real_evidence() -> None:
    with pytest.raises(ValueError, match="must not be blank"):
        Refusal(reason="  ", evidence=DOCUMENTED)
    with pytest.raises(TypeError):
        Refusal(reason="no", evidence="because I said so")  # type: ignore[arg-type]


# ========================================================================================
# Provenance surface
# ========================================================================================


def test_provenance_counts_add_up_to_every_shipped_fact() -> None:
    counts = FX5U.provenance_counts()
    total = sum(counts.values())
    expected = len(FX5U.facts) + len(FX5U.capabilities) + len(FX5U.devices)
    assert total == expected + len(FX5U.limits)
    assert counts[Provenance.LIVE] > 0


def test_evidence_for_lists_the_valid_names_when_it_misses() -> None:
    with pytest.raises(ValueError) as caught:
        FX5U.evidence_for("nonsense")
    message = str(caught.value)
    assert "monitor" in message and "remote_fixed_field" in message


def test_sources_deduplicates_and_returns_citation_objects() -> None:
    sources = FX5U.sources()
    assert len(sources) == len(set(sources))
    assert all(hasattr(source, "reference") for source in sources)


# ========================================================================================
# Derivation
# ========================================================================================


def test_with_ranges_merges_and_revalidates() -> None:
    narrowed = FX5U.with_ranges(
        {"D": DeviceRange("D", True, 0, 1999, configurable=True, evidence=DOCUMENTED)}
    )
    assert narrowed.devices["D"].last == 1999
    assert narrowed.devices["M"].last == 32767, "families not named keep their figures"
    assert FX5U.devices["D"].last == 7999, "the original is untouched"
    with pytest.raises(SlmpAddressRangeError):
        narrowed.check_range(address("D2000"), 1, width=WORD)


def test_with_ranges_cannot_conjure_a_family_the_silicon_does_not_have() -> None:
    with pytest.raises(SlmpConfigurationError, match="property of the silicon"):
        FX5U.with_ranges(
            {"ZR": DeviceRange("ZR", True, 0, 100, evidence=DOCUMENTED)}
        )


def test_with_ranges_refuses_a_key_that_disagrees_with_its_range() -> None:
    rng = DeviceRange("D", True, 0, 10, evidence=DOCUMENTED)
    with pytest.raises(SlmpConfigurationError, match="must name one family"):
        FX5U.with_ranges({"M": rng})


def test_replace_revalidates() -> None:
    assert FX5U.replace(key="melsec:iq-f/fx5u-test").key == "melsec:iq-f/fx5u-test"
    with pytest.raises(ValueError, match="must not be blank"):
        FX5U.replace(key="  ")


def test_the_default_spec_of_an_iq_f_is_short_because_long_is_refused() -> None:
    assert FX5U.default_spec is SpecFormat.SHORT
    assert IQ_R.default_spec is SpecFormat.SHORT


def test_family_and_link_values_are_the_strings_the_data_tables_use() -> None:
    assert Family.IQ_F.value == "iq-f"
    assert Link.CPU_BUILTIN.value == "cpu"
    assert Link.ETHERNET_MODULE.value == "enet"
    assert Encoding.BINARY.coding == "binary"
    assert Encoding.ASCII_XY_OCT.coding == "ascii"
    assert Encoding.ASCII_XY_HEX.is_ascii is True
    assert Encoding.BINARY.is_ascii is False
    assert int(ClearMode.INCLUDING_LATCH) == 2
