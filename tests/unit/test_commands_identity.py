"""``aslmp.identity``: which CPU answered, and what state it is in.

Two facts drive everything here.

**There is no generic profile and no radix guess.** ``X`` and ``Y`` are octal on an iQ-F
and hexadecimal on every other family, so ``Y20`` is output 16 on an FX5U and output 32
on an iQ-R -- and **both CPUs answer end code 0x0000**. Nothing on the wire tells them
apart, so an unrecognised model code raises rather than resolving to a family guessed
from its high byte.

**SLMP has no "what state are you in" command.** The CPU's operating status is SD203,
read as an ordinary word. That is the second round trip ``verify=True`` spends, and it
is what makes the difference between "Remote RUN returned 0x0000" and "the CPU is
running" -- which Mitsubishi documents as two different things.
"""

from __future__ import annotations

import pytest

from aslmp.commands.info import TypeName
from aslmp.errors import SlmpPayloadShapeError, SlmpProfileMismatchError
from aslmp.identity import (
    CPU_STATUS_REGISTER,
    SD203,
    CpuIdentity,
    CpuStatus,
    cpu_status_command,
    decode_cpu_status,
    resolve_profile,
)
from aslmp.profile import Family
from aslmp.profiles import FX5U, IQ_R

FX5U_TYPE_NAME = TypeName(
    model="FX5U-32MT/DS",
    model_code=0x4A49,
    raw=b"FX5U-32MT/DS    ",
)


# ======================================================================================
# CpuStatus
# ======================================================================================


@pytest.mark.parametrize(
    ("value", "status"),
    [
        (0, CpuStatus.RUN),
        (1, CpuStatus.STEP_RUN),
        (2, CpuStatus.STOP),
        (3, CpuStatus.PAUSE),
    ],
)
def test_sd203_decodes_to_the_documented_states(value: int, status: CpuStatus) -> None:
    assert decode_cpu_status((value,)) is status


def test_an_undocumented_status_is_refused_not_rounded_to_the_nearest() -> None:
    """Reporting STOP for a value we do not recognise tells a caller a machine is
    stopped on no evidence. That is the failure verify=True exists to prevent."""
    with pytest.raises(SlmpPayloadShapeError) as caught:
        decode_cpu_status((4,))
    message = str(caught.value)
    assert "SD203" in message
    assert "not a documented CPU operating status" in message


def test_a_status_read_that_returns_the_wrong_shape_is_refused() -> None:
    with pytest.raises(SlmpPayloadShapeError):
        decode_cpu_status((0, 0))
    with pytest.raises(SlmpPayloadShapeError):
        decode_cpu_status(())


def test_running_counts_step_run_and_excludes_pause() -> None:
    assert CpuStatus.RUN.running is True
    assert CpuStatus.STEP_RUN.running is True
    assert CpuStatus.STOP.running is False
    assert CpuStatus.PAUSE.running is False


def test_a_status_renders_with_a_hyphen_as_the_manual_writes_it() -> None:
    assert str(CpuStatus.STEP_RUN) == "STEP-RUN"


def test_the_status_command_is_one_word_of_sd203_and_mutates_nothing() -> None:
    command = cpu_status_command()
    assert command.CODE == 0x0401
    assert command.mutates is False
    assert command.address == SD203
    assert command.count == 1


# ======================================================================================
# resolve_profile
# ======================================================================================


def test_the_bench_model_code_resolves_to_the_fx5u_profile() -> None:
    assert resolve_profile(0x4A49) is FX5U


def test_an_iq_r_model_code_resolves_to_the_iq_r_profile() -> None:
    assert resolve_profile(0x4800) is IQ_R


def test_an_unknown_model_code_raises_and_names_the_tool_that_fixes_it() -> None:
    with pytest.raises(SlmpProfileMismatchError) as caught:
        resolve_profile(0x1234)
    assert "aslmp identify" in str(caught.value)


def test_the_rcpu_family_code_is_deliberately_claimed_by_nobody() -> None:
    """0x0360 names a family, not a model, so it cannot choose a device table."""
    with pytest.raises(SlmpProfileMismatchError):
        resolve_profile(0x0360)


# ======================================================================================
# CpuIdentity
# ======================================================================================


def test_an_identity_takes_its_family_from_the_profile_not_from_the_name() -> None:
    """The model name is a marketing string whose shape has changed between families."""
    identity = CpuIdentity.of(FX5U_TYPE_NAME)
    assert identity.model == "FX5U-32MT/DS"
    assert identity.model_code == 0x4A49
    assert identity.family is Family.IQ_F
    assert identity.raw == b"FX5U-32MT/DS    "


def test_an_identity_for_an_unknown_code_is_not_built_at_all() -> None:
    unknown = TypeName(model="MYSTERY", model_code=0x1234, raw=b"MYSTERY         ")
    with pytest.raises(SlmpProfileMismatchError):
        CpuIdentity.of(unknown)


def test_check_passes_for_the_profile_that_claims_the_code() -> None:
    CpuIdentity.of(FX5U_TYPE_NAME).check(FX5U)


def test_check_raises_when_the_declared_profile_is_a_different_family() -> None:
    """A caller who names melsec:iq-r against an FX5U reads Y20 as a different output
    forever, and the PLC answers 0x0000 either way."""
    identity = CpuIdentity.of(FX5U_TYPE_NAME)
    with pytest.raises(SlmpProfileMismatchError) as caught:
        identity.check(IQ_R)
    error = caught.value
    assert error.model_code == 0x4A49
    assert error.model == "FX5U-32MT/DS"
    assert error.profile_key == IQ_R.key
    assert "octal on an iQ-F" in str(error)
    assert "aslmp identify" in str(error)


def test_an_identity_renders_its_code_in_hexadecimal() -> None:
    """``pymelsec`` formats 0xC056 as the string '0x49238' -- the decimal with 0x glued
    on the front -- and then str(e) raises. Nothing here does arithmetic in a message."""
    assert str(CpuIdentity.of(FX5U_TYPE_NAME)) == "FX5U-32MT/DS (0x4A49, iq-f)"


def test_an_identity_is_frozen() -> None:
    identity = CpuIdentity.of(FX5U_TYPE_NAME)
    with pytest.raises(AttributeError):
        identity.model = "something else"  # type: ignore[misc]


def test_the_model_name_keeps_its_raw_padding_for_a_diagnostic() -> None:
    """The space padding is the evidence the field was read at the right offset."""
    identity = CpuIdentity.of(FX5U_TYPE_NAME)
    assert len(identity.raw) == 16
    assert identity.raw.endswith(b"    ")
    assert not identity.model.endswith(" ")


def test_the_sd203_citation_names_a_manual_the_package_ships_a_row_for() -> None:
    """The revision letter of JY997D55401 was never recorded, and the citation says so
    rather than inventing one. It still has to be a manual somebody can go and find."""
    import csv
    from pathlib import Path

    data = Path("src/aslmp/data/manuals.tsv")
    lines = [
        line
        for line in data.read_text(encoding="utf-8").splitlines()
        if not line.startswith("#")
    ]
    known = {
        (row["manual"], row["revision"])
        for row in csv.DictReader(lines, delimiter=chr(9))
    }
    assert (CPU_STATUS_REGISTER.manual, CPU_STATUS_REGISTER.revision) in known
    assert "SD203" in CPU_STATUS_REGISTER.note
