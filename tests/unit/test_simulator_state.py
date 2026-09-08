"""No simulator state may be write-only. The enforcement test for a whole family of bugs.

Layer 2.5, pure: no server, no socket, no clock. Every observation in this file is made
the only way a client can make one -- by sending a request and reading the end code and
the response data back.

.. rubric:: What went wrong three times

:class:`~aslmp.testing.dispatch.SessionState` is the mutable memory of a simulated CPU,
and until 2026-09-07 six of its seven fields were written by handlers and read by
nothing, or not touched at all. ``monitor_points`` was the only one a client could see:

* ``run_state`` -- set by ``1001``, ``1002`` and ``1003``. ``SD203`` was never derived
  from it, so after a Remote STOP that answered ``0x0000`` a client's
  ``read_cpu_status()`` still returned RUN, ``SD203`` still read 0, and the scan counter
  went on counting through a stopped CPU. Every verified-remote test in the suite
  therefore set ``SD203`` to the answer it was about to assert, which means each of them
  would have passed against a ``1002`` handler consisting of ``return Reply(0x0000)``.
* ``switch_position`` -- never written and never read. It read as "we model the key
  switch" and modelled nothing.
* ``password_locked`` -- set by ``1631`` and cleared by ``1630``, consulted by no
  handler, so locking a port changed no answer this CPU ever gave.
* ``error_flag`` -- assigned ``False`` by ``1617`` and never assigned ``True`` by
  anything.
* ``served`` -- counted, and returned by no SLMP command that exists.
* ``scan`` -- a second copy of ``D8``/``D9``.

The individual fixes are elsewhere. **This file is the thing that makes the fifth one
unnecessary**: a field a client cannot distinguish two values of is not a model of
hardware, it is a decoration that makes a handler look implemented, and adding one here
fails the build.

.. rubric:: The rule, and why it is shaped like this

Every field of :class:`~aslmp.testing.dispatch.SessionState` must have a probe below, and
each probe must show that two values of that field produce two different answers *on the
wire*. A probe cannot cheat by reading the field, because a probe is not given the
field -- it is a fixed sequence of requests, and what is compared is the ``(end code,
response data)`` pairs that come back.

The structural half (every field has a probe) is what catches the *next* write-only
field. The behavioural half (the probe actually distinguishes) is what stops a future
probe from being a comment.

Both halves were checked by breaking them on 2026-09-07: adding
``SessionState.last_self_test_bytes`` and an assignment to it in the ``0619`` handler
failed :func:`test_no_session_state_field_is_write_only` by name, and giving that field a
probe made of a plain ``0401`` read then failed
:func:`test_each_session_state_field_is_visible_to_a_client` for it. A test nobody has
watched fail is a test nobody has tested.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any, Final

import pytest

from aslmp.identity import SD203
from aslmp.profile import Encoding
from aslmp.testing.dispatch import (
    CPU_STATUS_DEVICE,
    CPU_STATUS_INDEX,
    PASSWORD_EXEMPT_COMMANDS,
    CpuRunState,
    Dispatcher,
    ServerContext,
    SessionState,
    Silence,
)
from aslmp.testing.targets import FX5U_32MT_DS, PEDANTIC, SimulatorTarget
from aslmp.wire.codec import BINARY, Unit
from aslmp.wire.devicetable import DEVICE_TABLE
from aslmp.wire.frames import THREE_E, request_body
from aslmp.wire.route import Route

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from aslmp.wire.raw import RawRequest


# --------------------------------------------------------------------------------------
# Requests, and the only kind of observation this file admits
# --------------------------------------------------------------------------------------


def make_request(*, command: int, subcommand: int = 0x0000, payload: bytes = b"") -> RawRequest:
    """A request frame built by the library and parsed back, as the wire delivers it."""
    body = request_body(
        BINARY, monitoring_timer=0, command=command, subcommand=subcommand, payload=payload
    )
    raw = THREE_E.build(route=Route.OWN_STATION, body=body, codec=BINARY)
    return THREE_E.parse_request(raw, BINARY)


def word_spec(device: str, index: int) -> bytes:
    """A binary short-format device specification: three number bytes, then the code."""
    code = DEVICE_TABLE[device].code_short
    assert code is not None, f"{device} has no short device code"
    return index.to_bytes(3, "little") + bytes((code,))


def batch_read(device: str, index: int, count: int = 1) -> RawRequest:
    """``0401``, one contiguous run of words."""
    return make_request(
        command=0x0401, payload=word_spec(device, index) + BINARY.number(count, bits=16)
    )


READ_SD203: Final = batch_read(CPU_STATUS_DEVICE, CPU_STATUS_INDEX)
"""The one request a client has for "what state is this CPU in": ``SD203`` as a word."""

READ_D0: Final = batch_read("D", 0)
"""Any harmless read at all, for probes about whether a CPU is answering."""

REMOTE_RUN: Final = make_request(
    command=0x1001,
    payload=BINARY.number(1, bits=16) + BINARY.number(0, bits=8) + BINARY.number(0, bits=8),
)
"""``1001`` with the one clear mode an iQ-F's table has."""

EXECUTE_MONITOR: Final = make_request(command=0x0802)
"""``0802``: read the registered list back. No request data at all."""

Observation = tuple[tuple[int, bytes], ...]
"""What a client sees: an end code and the response data, per request. Nothing else."""


def observe(plc: Dispatcher, requests: Sequence[RawRequest]) -> Observation:
    """Send the requests and return only what came back.

    Deliberately the whole of the interface: a probe that wanted to look at
    ``plc.state`` could not express it through this function, which is the point. A
    :class:`~aslmp.testing.dispatch.Silence` is an observation too -- an absent response
    is what a successful Remote Reset looks like -- so it is recorded rather than
    unpacked.
    """
    out: list[tuple[int, bytes]] = []
    for request in requests:
        outcome = plc.handle(request, codec=BINARY, encoding=Encoding.BINARY)
        if isinstance(outcome, Silence):
            out.append((-1, outcome.reason.encode()))
        else:
            out.append((outcome.end_code, outcome.payload))
    return tuple(out)


# --------------------------------------------------------------------------------------
# One probe per field
# --------------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class StateProbe:
    """Two values of one field, and the requests that tell them apart.

    ``why`` is the sentence that says *how* a client sees it, and it is printed when the
    probe fails to distinguish, because a probe that stops distinguishing means either
    the field went dead again or the command that exposed it changed.
    """

    values: tuple[Any, Any]
    requests: tuple[RawRequest, ...]
    why: str
    target: SimulatorTarget = FX5U_32MT_DS
    """Which CPU answers. Our silicon refuses ``0801``/``0802`` outright with ``0xC059``
    (measured 2026-09-06), so the monitor list is probed against the reference target --
    a field only one CPU family exposes is still a field a client can read."""

    def seen(self, value: Any, field: str) -> Observation:
        """A fresh CPU with this field set to ``value``, and what it then answers."""
        plc = Dispatcher(target=self.target, memory=self.target.memory())
        setattr(plc.state, field, value)
        return observe(plc, self.requests)


MONITOR_REGISTRATION: Final = ((DEVICE_TABLE["D"], 100, Unit.WORD, 1),)
"""One registered ``0801`` point, in the shape the handler stores."""

PROBES: Final[dict[str, StateProbe]] = {
    "switch_position": StateProbe(
        values=(CpuRunState.RUN, CpuRunState.STOP),
        requests=(REMOTE_RUN, READ_SD203),
        why=(
            "a Remote RUN against a CPU whose key is in STOP is 'completed normally. "
            "However, the access destination does not become the RUN state' "
            "(SH(NA)-080956ENG-M 6.9, p.131), so SD203 still reads STOP afterwards"
        ),
    ),
    "remote_request": StateProbe(
        values=(CpuRunState.RUN, CpuRunState.STOP),
        requests=(READ_SD203,),
        why="SD203 is what 1001/1002/1003 move, and a 0401 is how a client reads it",
    ),
    "monitor_points": StateProbe(
        values=(None, MONITOR_REGISTRATION),
        requests=(EXECUTE_MONITOR,),
        why="0802 against an unregistered CPU is 0xC05D, and against a registered one is data",
        target=PEDANTIC,
    ),
    "password_locked": StateProbe(
        values=(False, True),
        requests=(READ_D0,),
        why="a locked port answers 0xC201 to everything but 1630 Remote Password Unlock",
    ),
}
"""How a client sees each field of :class:`~aslmp.testing.dispatch.SessionState`.

Adding a field to that class without adding a row here fails
:func:`test_no_session_state_field_is_write_only`, which is the whole reason this table
is written out by hand rather than derived from anything.
"""


# --------------------------------------------------------------------------------------
# The enforcement
# --------------------------------------------------------------------------------------


def test_no_session_state_field_is_write_only() -> None:
    """Every field of ``SessionState`` has a probe, or it must not exist.

    The failure this catches is not hypothetical and not small: it is a handler whose
    effect leaves no trace a client can find, and a test suite that then sets the trace
    by hand and asserts on it. Three review rounds went green over exactly that.

    If this fails on a field you just added, there are two honest ways out. Wire it to
    something a request can reach -- a device register, an end code, a response layout --
    and add the probe that shows it. Or delete it, and say in the handler's docstring
    that this CPU model does not model that thing, which is a smaller lie than a field
    nobody can read.
    """
    declared = {field.name for field in dataclasses.fields(SessionState)}
    probed = set(PROBES)
    assert probed == declared, (
        f"SessionState fields with no probe (write-only until proven otherwise): "
        f"{sorted(declared - probed)}; probes for fields that no longer exist: "
        f"{sorted(probed - declared)}"
    )


@pytest.mark.parametrize("field", sorted(PROBES))
def test_each_session_state_field_is_visible_to_a_client(field: str) -> None:
    """The probe has to actually distinguish, or it is a comment with a docstring."""
    probe = PROBES[field]
    low, high = probe.values
    first = probe.seen(low, field)
    second = probe.seen(high, field)
    assert first != second, (
        f"SessionState.{field} = {low!r} and = {high!r} produce identical answers to "
        f"{len(probe.requests)} request(s), so no client can tell them apart. "
        f"The probe says: {probe.why}"
    )


def test_session_state_cannot_grow_a_field_at_runtime() -> None:
    """``slots=True``, so :func:`dataclasses.fields` really is the whole of the state.

    Without it the table above would only cover the fields somebody remembered to
    declare, and ``ctx.state.whatever = 1`` in a handler would sail past.
    """
    state = SessionState()
    with pytest.raises(AttributeError):
        state.undeclared_field = 1  # type: ignore[attr-defined]


def test_a_handler_is_given_no_other_mutable_state_to_write() -> None:
    """The only state a handler can reach is what ``ServerContext`` hands it.

    ``ServerContext`` is frozen, so a handler cannot rebind any of it; the two things it
    can mutate through are ``memory`` (which a client reads by definition) and ``state``
    (which the table above covers). The dispatcher itself is not in the context, so
    ``scan_per_request`` and the scenario cannot be written from inside a command.
    """
    assert ServerContext.__dataclass_params__.frozen  # type: ignore[attr-defined]
    reachable = {field.name for field in dataclasses.fields(ServerContext)}
    assert "memory" in reachable and "state" in reachable
    assert not reachable & {"scenario", "scan_per_request", "dispatcher"}


# --------------------------------------------------------------------------------------
# The register the whole family hangs off
# --------------------------------------------------------------------------------------


def test_the_simulator_and_the_library_name_the_same_register() -> None:
    """``SD203``, spelled independently on both sides and compared here.

    The simulator does not import :data:`aslmp.identity.SD203`, for the reason its
    request decoders are not ``encode()`` run backwards: if both halves took the number
    out of one constant, a test proving a client can see a Remote STOP would be proving
    that two halves of one library share a variable. So they are written twice and
    checked once, which is a comparison.
    """
    assert f"{CPU_STATUS_DEVICE}{CPU_STATUS_INDEX}" == SD203


def test_a_cpu_that_cannot_report_its_own_state_is_refused_at_construction() -> None:
    """A simulated CPU with no ``SD203`` would be a CPU whose run state is unobservable.

    Which is the defect this family is about, so it is a construction error rather than
    a quietly skipped write.
    """
    from aslmp.testing.memory import DeviceMemory, MemoryRange

    with pytest.raises(ValueError, match="report its own operating status"):
        Dispatcher(target=FX5U_32MT_DS, memory=DeviceMemory((MemoryRange("D", 0, 10),)))


def test_a_locked_port_still_serves_the_command_that_unlocks_it() -> None:
    """0xC201 to everything else, and ``1630`` is the documented way out.

    Without this, "refuse while locked" could be implemented as "refuse everything",
    which is a CPU nobody can recover.
    """
    plc = Dispatcher(target=FX5U_32MT_DS, memory=FX5U_32MT_DS.memory())
    plc.state.password_locked = True
    assert observe(plc, (READ_D0,))[0][0] == 0xC201

    unlock = make_request(command=0x1630, payload=BINARY.number(0, bits=16))
    assert observe(plc, (unlock,))[0][0] == 0x0000
    assert plc.state.password_locked is False
    assert observe(plc, (READ_D0,))[0][0] == 0x0000
    assert set(PASSWORD_EXEMPT_COMMANDS) == {0x1630}


def test_a_hand_written_sd203_does_not_survive_the_next_request() -> None:
    """The cheat that made four tests pass for the wrong reason, closed.

    ``SD203`` is derived from the key switch and the last remote request at the top of
    every served request, exactly as a CPU re-reads its own switch every scan. Poking the
    register is therefore a transient and not a way to stage the answer a test is about
    to assert.
    """
    plc = Dispatcher(target=FX5U_32MT_DS, memory=FX5U_32MT_DS.memory())
    plc.memory.set_u16(CPU_STATUS_DEVICE, CPU_STATUS_INDEX, CpuRunState.STOP)
    assert plc.memory.get_u16(CPU_STATUS_DEVICE, CPU_STATUS_INDEX) == CpuRunState.STOP

    end_code, payload = observe(plc, (READ_SD203,))[0]
    assert end_code == 0x0000
    assert BINARY.read_words(payload, 0, 1)[0] == CpuRunState.RUN, (
        "the CPU reports what its switch and its last remote request say, not what a "
        "test wrote into the register"
    )


def test_the_scan_counter_stops_with_the_cpu() -> None:
    """``advance_scan`` on a stopped CPU returns the count and does not add to it.

    Measured behaviour of the bench is that ``IO_Scan`` is advanced by the program, and a
    stopped CPU runs no program. The simulator incremented ``D8`` through a Remote STOP
    until 2026-09-07, which disarmed the one oracle
    ``tests/hardware/test_remote_control.py`` trusts over ``SD203`` itself.
    """
    plc = Dispatcher(target=FX5U_32MT_DS, memory=FX5U_32MT_DS.memory())
    assert plc.advance_scan() == 1.0

    plc.state.switch_position = CpuRunState.STOP
    plc.publish_cpu_state()
    assert plc.advance_scan() == 1.0
    assert plc.advance_scan() == 1.0
    assert plc.memory.get_f32("D", 8) == 1.0

    plc.state.switch_position = CpuRunState.RUN
    assert plc.advance_scan() == 2.0


def test_scan_per_request_makes_the_registers_move_under_a_client() -> None:
    """The property ``advance_scan``'s docstring claimed and did not provide.

    It was called by tests and never while serving a request, so "a simulator whose
    registers never move lets a stale-value bug pass" described the simulator itself.
    Off by default, because the bench CPU scans on a wall clock rather than per request;
    on, it is the closest a request-driven simulator gets to a CPU that is running.
    """
    still = Dispatcher(target=FX5U_32MT_DS, memory=FX5U_32MT_DS.memory())
    observe(still, (READ_D0, READ_D0))
    assert still.memory.get_f32("D", 8) == 0.0, "the default stands still"

    moving = Dispatcher(
        target=FX5U_32MT_DS, memory=FX5U_32MT_DS.memory(), scan_per_request=True
    )
    first = observe(moving, (batch_read("D", 8, 2),))
    second = observe(moving, (batch_read("D", 8, 2),))
    assert first != second, "two identical requests, two different answers"
    assert moving.memory.get_f32("D", 8) == 2.0

    stopped = Dispatcher(
        target=FX5U_32MT_DS, memory=FX5U_32MT_DS.memory(), scan_per_request=True
    )
    stopped.state.switch_position = CpuRunState.STOP
    assert observe(stopped, (READ_D0,)) == observe(stopped, (READ_D0,))
    assert stopped.memory.get_f32("D", 8) == 0.0


def test_the_dispatcher_publishes_its_state_before_it_has_served_anything() -> None:
    """A CPU reports RUN from the moment it exists, without being asked a question first.

    Device memory, read directly, so a client's very first ``0401`` at ``SD203`` gets the
    CPU's actual state rather than whatever a freshly allocated register file holds --
    which is zero, which happens to *be* RUN, which is exactly the sort of coincidence
    that lets a disconnected register look connected for three review rounds.
    """
    plc = Dispatcher(target=FX5U_32MT_DS, memory=FX5U_32MT_DS.memory())
    assert plc.cpu_state() == CpuRunState.RUN

    stopped = Dispatcher(
        target=FX5U_32MT_DS,
        memory=FX5U_32MT_DS.memory(),
        state=SessionState(switch_position=CpuRunState.STOP),
    )
    assert stopped.cpu_state() == CpuRunState.STOP
