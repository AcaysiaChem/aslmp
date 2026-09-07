"""``aslmp.testing.scenario`` -- scripted answers, in order, once.

A pathology says how a CPU *behaves*; a scenario says what happens *next*. The
distinction is why there is no ``fail_sometimes`` switch anywhere: a test that can only
observe the broken state cannot observe the recovery, and half the failures this library
must survive are transient.
"""

from __future__ import annotations

import pytest

from aslmp.testing.dispatch import Reply, Silence
from aslmp.testing.scenario import Scenario, Step, abnormal, silent


def test_an_empty_scenario_never_intervenes() -> None:
    script = Scenario()
    assert script.take(0x0401) is None
    assert script.exhausted


def test_a_step_fires_once_and_is_spent() -> None:
    script = Scenario((abnormal(0xC056),))
    first = script.take(0x0401)
    assert isinstance(first, Reply)
    assert first.end_code == 0xC056
    assert script.take(0x0401) is None
    assert script.exhausted


def test_times_repeats_one_step() -> None:
    """"The next three reads all fail" without three copies of the same line."""
    script = Scenario((abnormal(0xC056, times=3),))
    assert [script.take(0x0401) is not None for _ in range(4)] == [True, True, True, False]


def test_a_step_narrowed_to_one_command_ignores_the_others() -> None:
    script = Scenario((abnormal(0xC056, command=0x0403),))
    assert script.take(0x0401) is None
    assert script.take(0x0403) is not None


def test_a_narrowed_step_does_not_jump_the_queue() -> None:
    """Only the head of the script is considered.

    A script whose steps could be taken out of order is a set of switches wearing a
    list's clothes, and the order is the whole reason to reach for one.
    """
    script = Scenario((abnormal(0xC056, command=0x0401), abnormal(0xC054, command=0x0403)))
    assert script.take(0x0403) is None, "the 0x0403 step is behind an unconsumed one"
    assert script.take(0x0401) is not None
    taken = script.take(0x0403)
    assert isinstance(taken, Reply)
    assert taken.end_code == 0xC054


def test_a_silent_step_answers_nothing() -> None:
    script = Scenario((silent("the entry is configured for the other coding"),))
    taken = script.take(0x0401)
    assert isinstance(taken, Silence)
    assert "coding" in taken.reason


def test_remaining_names_the_steps_that_never_fired() -> None:
    """A script that did not run is a test that did not test what it says it did."""
    script = Scenario((abnormal(0xC056), abnormal(0xC054)))
    script.take(0x0401)
    assert len(script.remaining) == 1
    assert not script.exhausted


def test_reset_rewinds() -> None:
    script = Scenario((abnormal(0xC056),))
    assert script.take(0x0401) is not None
    assert script.take(0x0401) is None, "spent"
    script.reset()
    assert script.take(0x0401) is not None, "and available again"


def test_a_step_applies_at_least_once() -> None:
    with pytest.raises(ValueError, match="at least once"):
        Step(outcome=Reply(0xC056), times=0)


def test_a_step_command_is_a_16_bit_code() -> None:
    with pytest.raises(ValueError, match="16-bit command"):
        Step(outcome=Reply(0xC056), command=0x1FFFF)


def test_a_scenario_refuses_something_that_is_not_a_step() -> None:
    with pytest.raises(TypeError, match="not a Step"):
        Scenario((Reply(0x0000),))  # type: ignore[arg-type]  # the point of the test


def test_str_says_where_the_script_is() -> None:
    script = Scenario((abnormal(0xC056, command=0x0401),))
    assert "0x0401" in str(script)
    script.take(0x0401)
    assert "exhausted" in str(script)
