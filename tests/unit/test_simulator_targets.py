"""``aslmp.testing.targets`` -- three CPUs, and the diff that is a document.

The point of having a ``PEDANTIC`` target at all is that the difference between it and
``FX5U_32MT_DS`` is exactly the list of behaviours this library has *only* because real
silicon departs from its own manual. These tests assert that list is what we think it is
and that the unverified iQ-R says so out loud.
"""

from __future__ import annotations

import dataclasses

import pytest

from aslmp.profile import ClearMode
from aslmp.testing.pathology import PATHOLOGY_SOURCES, Pathology
from aslmp.testing.targets import (
    ALL_TARGETS,
    FX5U_32MT_DS,
    PEDANTIC,
    R04CPU,
    EndCodePolicy,
    SimulatorTarget,
    by_key,
    diff_targets,
)
from aslmp.wire.citations import Provenance


def test_every_target_is_reachable_by_key() -> None:
    for key, target in ALL_TARGETS.items():
        assert by_key(key) is target


def test_an_unknown_target_is_refused_with_the_list() -> None:
    """No default and no nearest match: a target is a claim about how a CPU behaves."""
    with pytest.raises(KeyError, match="not a simulator target"):
        by_key("fx5u")


def test_the_iq_r_target_is_labelled_unverified() -> None:
    """There is no iQ-R in the building and the report has to say so."""
    assert R04CPU.verified is False
    warning = R04CPU.warn_if_unverified()
    assert "UNVERIFIED" in warning
    assert "no such CPU has ever been connected" in warning
    assert FX5U_32MT_DS.warn_if_unverified() == ""


def test_the_three_corrected_end_codes() -> None:
    """0xC05C, 0xC052 and 0xC061: where the FX5U contradicts the documentation.

    These three are the whole reason the end codes are a per-target policy rather than a
    module of constants, and each was provoked on a wire (FX5U-32MT/DS fw 1.065,
    2026-09-06).
    """
    assert PEDANTIC.end_codes.unknown_device_code == 0xC05B
    assert FX5U_32MT_DS.end_codes.unknown_device_code == 0xC05C

    assert PEDANTIC.end_codes.zero_point_count == 0xC056
    assert FX5U_32MT_DS.end_codes.zero_point_count == 0xC052

    assert PEDANTIC.end_codes.request_length_mismatch == 0xC057
    assert FX5U_32MT_DS.end_codes.request_length_mismatch == 0xC061


def test_the_measured_point_ceilings() -> None:
    """960 / 3584 / 192, binary-searched on hardware; 3584 is HALF the generic figure."""
    assert FX5U_32MT_DS.limits.batch_word == 960
    assert FX5U_32MT_DS.limits.batch_bit == 3584
    assert FX5U_32MT_DS.limits.random_points == 192
    assert PEDANTIC.limits.batch_bit == 7168
    assert FX5U_32MT_DS.limits.batch_bit * 2 == PEDANTIC.limits.batch_bit


def test_the_ascii_ceilings_are_stated_and_not_derived() -> None:
    """The FX5 manual halves them; the SLMP reference does not. Both are written down."""
    assert FX5U_32MT_DS.limits.ascii_batch_word == 480
    assert PEDANTIC.limits.ascii_batch_word == PEDANTIC.limits.batch_word


def test_the_iq_f_capability_refusals() -> None:
    """0x0801, 0x0802 and subcommand 0x0002 all answer 0xC059 on this silicon."""
    assert FX5U_32MT_DS.monitor is False
    assert FX5U_32MT_DS.long_device_spec is False
    assert PEDANTIC.monitor is True
    assert PEDANTIC.long_device_spec is True


def test_the_remote_fixed_field_is_a_family_fact() -> None:
    assert FX5U_32MT_DS.remote_fixed_field == b"\x00\x00"
    assert PEDANTIC.remote_fixed_field == b"\x01\x00"


def test_the_iq_f_accepts_only_one_clear_mode() -> None:
    assert FX5U_32MT_DS.clear_modes == frozenset({ClearMode.NONE})
    assert ClearMode.INCLUDING_LATCH in PEDANTIC.clear_modes


def test_the_model_name_field_is_sixteen_characters_in_both_codings() -> None:
    """Text, not a number: ASCII coding does not double it (SH(NA)-080956ENG-M p.137)."""
    assert FX5U_32MT_DS.model_name_field == b"FX5U-32MT/DS    "
    assert len(FX5U_32MT_DS.model_name_field) == 16
    assert FX5U_32MT_DS.model_code == 0x4A49


def test_a_model_name_longer_than_the_field_is_refused() -> None:
    with pytest.raises(ValueError, match="16 characters"):
        dataclasses.replace(FX5U_32MT_DS, model_name="X" * 17)


def test_end_code_zero_is_never_a_refusal() -> None:
    """0x0000 is normal completion; a policy field holding it would be a silent success."""
    with pytest.raises(ValueError, match="non-zero 16-bit end code"):
        EndCodePolicy(unsupported_command=0x0000)


def test_the_measured_pathology_is_the_measured_set() -> None:
    """Every switch on by default for our silicon, and nothing speculative."""
    assert set(FX5U_32MT_DS.pathology.active) == {
        "coalesce_requests",
        "single_connection",
        "silence_on_wrong_encoding",
        "accept_illegal_random_points",
        "overstated_length_hangs",
        "accept_4e_on_3e_entry",
    }
    assert PEDANTIC.pathology.active == ()


def test_every_pathology_switch_names_the_observation_behind_it() -> None:
    """A switch that cannot be tied to a measurement does not belong on the board."""
    fields = {
        f.name
        for f in dataclasses.fields(Pathology)
        if f.name
        not in {
            "sources",
            "segment_gap_s",
            "overstated_length_grace_s",
            "udp_service_delay_s",
            "late_reply_s",
            "drop_every_nth_datagram",
        }
    }
    missing = sorted(fields - set(PATHOLOGY_SOURCES))
    assert not missing, f"pathology switches with no cited observation: {missing}"


def test_the_measured_board_cites_hardware() -> None:
    sources = FX5U_32MT_DS.pathology.cites()
    assert sources
    assert any(getattr(source, "cpu", "") == "FX5U-32MT/DS" for source in sources)


def test_the_targets_cite_their_evidence() -> None:
    assert FX5U_32MT_DS.evidence.provenance is Provenance.LIVE
    assert R04CPU.evidence.provenance is Provenance.MANUAL
    assert PEDANTIC.evidence.provenance is Provenance.MANUAL


def test_the_diff_is_the_document() -> None:
    """PEDANTIC against FX5U_32MT_DS: the three end codes, the ceilings, the refusals."""
    diff = diff_targets(PEDANTIC, FX5U_32MT_DS)
    keys = set(diff.names())
    for expected in (
        "end_code.unknown_device_code",
        "end_code.zero_point_count",
        "end_code.request_length_mismatch",
        "limit.batch_bit",
        "long_device_spec",
        "monitor",
        "xy_ascii_octal",
        "remote_fixed_field",
        "clear_modes",
        "pathology.coalesce_requests",
        "pathology.single_connection",
        "pathology.silence_on_wrong_encoding",
        "pathology.accept_illegal_random_points",
    ):
        assert expected in keys, f"{expected} is missing from the target diff"
    text = diff.to_markdown()
    assert "0xC05B" in text
    assert "0xC05C" in text
    assert text.startswith("# PEDANTIC")


def test_a_target_diffed_with_itself_is_empty() -> None:
    assert not diff_targets(FX5U_32MT_DS, FX5U_32MT_DS)


def test_the_diff_warns_about_an_unverified_side() -> None:
    assert "UNVERIFIED" in diff_targets(FX5U_32MT_DS, R04CPU).to_markdown()


def test_the_illegal_random_device_gate_is_the_measured_one() -> None:
    """The FX5U accepted a TS0 word point and answered 0x0000 with data."""
    assert FX5U_32MT_DS.accepts_random_device("TS", random_ok=False) is True
    assert PEDANTIC.accepts_random_device("TS", random_ok=False) is False
    assert PEDANTIC.accepts_random_device("D", random_ok=True) is True


def test_memory_is_shaped_by_the_target_profile() -> None:
    assert FX5U_32MT_DS.memory().range_of("D").last == 7999
    assert not FX5U_32MT_DS.memory().has("ZR")


def test_targets_are_frozen() -> None:
    """A target is captured by a running server; a mutable one is unattributable."""
    with pytest.raises(dataclasses.FrozenInstanceError):
        FX5U_32MT_DS.verified = False  # type: ignore[misc]  # the point of the test


def test_str_names_the_unverified_ones() -> None:
    assert str(R04CPU).endswith("(unverified)")
    assert not str(FX5U_32MT_DS).endswith("(unverified)")


def test_a_target_needs_a_two_unit_fixed_field() -> None:
    with pytest.raises(ValueError, match="two wire units"):
        dataclasses.replace(FX5U_32MT_DS, remote_fixed_field=b"\x00")


def test_all_targets_are_simulator_targets() -> None:
    assert all(isinstance(target, SimulatorTarget) for target in ALL_TARGETS.values())
