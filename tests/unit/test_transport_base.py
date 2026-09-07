"""The transport's pure parts: deadlines, the reusable buffer, and computed silence.

No socket in this file. Everything here is a function of numbers, which is the point:
the timeout classification of graft G3 is the mechanism that tells one measured silence
from another, and it must be checkable without a PLC that is refusing to answer.
"""

from __future__ import annotations

import pytest

from aslmp.errors import SlmpConfigurationError, SlmpTimeoutError, TimeoutCause
from aslmp.transport.base import (
    DEFAULT_UDP_PIPELINE_DEPTH,
    MAX_UDP_PIPELINE_DEPTH,
    NULL_OBSERVER,
    Correlation,
    Deadline,
    RecvBuffer,
    TransportKind,
    accept_any,
    timeout_error,
)

NS_PER_S = 1_000_000_000


class FakeClock:
    """A monotonic clock a test drives by hand. Nanoseconds, like the real one."""

    def __init__(self, now: int = 0) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += int(seconds * NS_PER_S)


# --------------------------------------------------------------------------------------
# Deadline
# --------------------------------------------------------------------------------------


def test_deadline_counts_down_on_its_injected_clock() -> None:
    clock = FakeClock()
    deadline = Deadline.after(3.0, clock=clock)
    assert deadline.total_s == 3.0
    assert deadline.remaining_s() == pytest.approx(3.0)
    assert not deadline.expired()
    clock.advance(1.25)
    assert deadline.remaining_s() == pytest.approx(1.75)
    assert deadline.elapsed_s() == pytest.approx(1.25)
    assert not deadline.expired()


def test_deadline_remaining_goes_negative_and_is_never_clamped() -> None:
    """A clamp would hide how far past the budget a call ran, which is the diagnosis."""
    clock = FakeClock()
    deadline = Deadline.after(0.5, clock=clock)
    clock.advance(2.0)
    assert deadline.expired()
    assert deadline.remaining_s() == pytest.approx(-1.5)


@pytest.mark.parametrize("seconds", [0.0, -1.0])
def test_deadline_refuses_a_non_positive_budget(seconds: float) -> None:
    with pytest.raises(SlmpConfigurationError) as caught:
        Deadline.after(seconds, clock=FakeClock())
    assert "3.99" in str(caught.value)  # the fastest transaction measured on the bench


# --------------------------------------------------------------------------------------
# RecvBuffer
# --------------------------------------------------------------------------------------


def test_recv_buffer_reuses_one_allocation() -> None:
    """The steady state of a control loop must not allocate a receive buffer per cycle."""
    buffer = RecvBuffer(64)
    first = buffer.window(20)
    first[:4] = b"ABCD"
    second = buffer.window(20)
    assert bytes(second[:4]) == b"ABCD"
    assert buffer.capacity == 64


def test_recv_buffer_grows_only_when_it_must() -> None:
    buffer = RecvBuffer(16)
    buffer.window(16)
    assert buffer.capacity == 16
    window = buffer.window(100)
    assert len(window) == 100
    assert buffer.capacity >= 100


@pytest.mark.parametrize("nbytes", [0, -1])
def test_recv_buffer_refuses_an_empty_window(nbytes: int) -> None:
    with pytest.raises(SlmpConfigurationError):
        RecvBuffer(32).window(nbytes)


def test_recv_buffer_refuses_a_useless_capacity() -> None:
    with pytest.raises(SlmpConfigurationError):
        RecvBuffer(0)


# --------------------------------------------------------------------------------------
# Graft G3 -- the computed timeout
# --------------------------------------------------------------------------------------


def a_timeout(*, bytes_received: int, completed: int) -> SlmpTimeoutError:
    return timeout_error(
        deadline=Deadline.after(3.0, clock=FakeClock()),
        bytes_received=bytes_received,
        completed_transactions=completed,
        peer=("192.168.10.250", 5002),
        kind=TransportKind.TCP,
        where="reading the response",
    )


def test_silence_on_an_unproven_connection_blames_the_coding_first() -> None:
    """Sending ASCII into a binary entry is 0xC06F, and 0xC06F is silence on this CPU."""
    error = a_timeout(bytes_received=0, completed=0)
    assert error.likely_causes[0] is TimeoutCause.CODING_MISMATCH
    assert TimeoutCause.ENTRY_BUSY in error.likely_causes
    assert error.bytes_received == 0


def test_silence_after_a_working_transaction_does_not_blame_the_coding() -> None:
    """The coding cannot have changed under a live socket, so it is not a candidate."""
    error = a_timeout(bytes_received=0, completed=7)
    assert TimeoutCause.CODING_MISMATCH not in error.likely_causes
    assert error.likely_causes[0] is TimeoutCause.PLC_STOPPED_OR_RESET


def test_partial_bytes_blame_the_overstated_length_first() -> None:
    """An overstated L leaves the CPU blocked for bytes that never come."""
    error = a_timeout(bytes_received=9, completed=0)
    assert error.likely_causes[0] is TimeoutCause.REQUEST_LENGTH_OVERSTATED


def test_the_timeout_message_names_the_peer_the_transport_and_the_budget() -> None:
    error = a_timeout(bytes_received=0, completed=0)
    rendered = str(error)
    assert "192.168.10.250:5002" in rendered
    assert "tcp" in rendered
    assert "3 s" in rendered
    assert "coding_mismatch" in rendered


def test_a_timeout_is_also_a_builtin_timeout_error() -> None:
    """``except TimeoutError`` is a pattern people already have; it must keep working."""
    assert isinstance(a_timeout(bytes_received=0, completed=1), TimeoutError)


# --------------------------------------------------------------------------------------
# Correlation, observers, constants
# --------------------------------------------------------------------------------------


def test_the_default_correlation_takes_the_next_datagram() -> None:
    """Correct only at depth 1, which is why 3E is never given a depth above 1."""
    correlation = Correlation()
    assert correlation.label is None
    assert correlation.matches(b"anything at all")
    assert accept_any(b"")


def test_a_correlation_carries_the_label_for_the_diagnosis() -> None:
    correlation = Correlation(matches=lambda data: data.startswith(b"\xd4"), label=0x1234)
    assert correlation.label == 0x1234
    assert correlation.matches(b"\xd4\x00")
    assert not correlation.matches(b"\xd0\x00")


def test_the_null_observer_accepts_both_reports_and_says_nothing() -> None:
    NULL_OBSERVER.datagram_dropped(reason="stale-epoch", nbytes=24, source=None)
    NULL_OBSERVER.socket_rebound(previous_local=("", 1), local=("", 2), reason="timeout")


def test_the_default_pipeline_depth_sits_below_the_measured_ceiling() -> None:
    """8 was clean, 32 was clean, 64 lost twenty requests with no error of any kind."""
    assert DEFAULT_UDP_PIPELINE_DEPTH < MAX_UDP_PIPELINE_DEPTH == 32


def test_transport_kind_values_are_the_strings_the_records_carry() -> None:
    assert TransportKind.TCP.value == "tcp"
    assert TransportKind.UDP.value == "udp"
