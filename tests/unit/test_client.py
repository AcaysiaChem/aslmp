"""The client's shape, its refusals, and the generator that keeps its twin honest.

Nothing here opens a socket. Everything that needs one is in
``tests/integration/test_client_against_simulator.py``; what is asserted here is the half
that is true before a byte moves, which is most of the half that matters:

* **Two required arguments.** ``host`` and ``profile``. There is no generic profile,
  because ``Y20`` is output 16 on an iQ-F and output 32 on an iQ-R and both CPUs answer
  end code ``0x0000`` -- nothing on the wire tells them apart.
* **Validate before a byte is built.** A bad address on a client that has never connected
  raises the address error, not "not connected". That ordering is the first line of
  ``_run`` and it is what makes every refusal testable with no PLC.
* **The third ``L`` guard is armed.** ``Command.body_len(ctx)`` reaches
  ``FrameFormat.build(expect_body_len=...)`` on every request. U4 built that guard and it
  had no caller; an AST assertion here is what stops it from losing one again. An
  understated ``L`` returns ``0xC061`` and the connection recovers; an **overstated** one
  gets no response at all and is indistinguishable from a dead PLC (measured on
  FX5U-32MT/DS fw 1.065, 2026-09-06).
* **The two surfaces cannot drift.** ``aslmp/timed.py`` is generated from ``client.py``'s
  AST and committed, and regenerating it must be a byte-for-byte no-op. That check lives
  here rather than in a docstring, together with a reflective parity assertion on the
  method names and parameter lists, because the generator and the parity test fail for
  different reasons.
"""

from __future__ import annotations

import ast
import importlib.util
import inspect
import struct
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from aslmp.blocks.fields import U16, Bounds
from aslmp.blocks.layout import plc_block
from aslmp.client import (
    REMOTE_CONTROL_COMMANDS,
    Handshake,
    MonitoringTimer,
    Plc,
    PlcClockSource,
    _build_transport,
    _check_point_value,
    _from_words,
    _RawCommand,
    _SilentRawCommand,
    _string_words,
    _to_words,
)
from aslmp.commands.base import (
    EncodeContext,
    WordOrder,
    boolean,
    encoded,
    real,
    unsigned,
)
from aslmp.commands.batch import ReadWords
from aslmp.commands.random import RandomWrite, word
from aslmp.errors import (
    ClientSummary,
    SlmpBlockLayoutError,
    SlmpCapabilityError,
    SlmpConfigurationError,
    SlmpDeviceRadixError,
    SlmpMonitoringTimerError,
    SlmpNotConnectedError,
    SlmpValueRangeError,
)
from aslmp.profile import Capability, Encoding, Evidence, Link, Refusal
from aslmp.profiles import FX5U, IQ_R
from aslmp.timed import TimedApi
from aslmp.transport.base import TransportKind
from aslmp.transport.tcp import TcpTransport
from aslmp.transport.udp import UdpTransport
from aslmp.wire.citations import Provenance
from aslmp.wire.codec import BINARY, SpecFormat
from aslmp.wire.frames import FOUR_E, THREE_E, FrameType

REPO_ROOT = Path(__file__).resolve().parents[2]
CLIENT = REPO_ROOT / "src" / "aslmp" / "client.py"
TOOL = REPO_ROOT / "tools" / "gen_timed.py"
MAX_LINE = 96
BENCH = "192.168.10.250"


def a_client(port: int = 5002, **kwargs: Any) -> Plc:
    """A client aimed at the bench's own settings. Constructing one opens no socket.

    ``Any`` because ``Plc`` takes twenty differently typed keyword arguments and this
    helper forwards whichever one a test names; the callee checks every one of them.
    """
    return Plc(BENCH, port, profile="melsec:iq-f/fx5u", **kwargs)


def load_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_aslmp_gen_timed", TOOL)
    if spec is None or spec.loader is None:  # pragma: no cover - packaging accident
        pytest.fail("cannot import tools/gen_timed.py")
    module = importlib.util.module_from_spec(spec)
    # Registered before execution: the tool declares a slotted dataclass, and
    # `dataclasses` resolves annotations through `sys.modules[cls.__module__]`.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# ========================================================================================
# Construction
# ========================================================================================


def test_the_client_takes_exactly_two_required_arguments() -> None:
    """``host`` and ``profile``. Everything else is a declared constant, not a probe."""
    signature = inspect.signature(Plc.__init__)
    required = [
        name
        for name, parameter in signature.parameters.items()
        if name != "self" and parameter.default is inspect.Parameter.empty
    ]
    assert required == ["host", "profile"]
    assert signature.parameters["profile"].kind is inspect.Parameter.KEYWORD_ONLY


def test_the_connection_entry_facts_carry_the_factory_defaults() -> None:
    """A default is a constant the caller overrides. It is not auto-detection.

    A wrong coding, a wrong frame type and a wrong transport all fail by **silence** on
    FX5U-32MT/DS fw 1.065, so none of the three may ever be retried in another value;
    they are declared, and the handshake catches a mistake in about 7 ms.
    """
    defaults = {
        name: parameter.default for name, parameter in inspect.signature(Plc).parameters.items()
    }
    assert defaults["transport"] is TransportKind.TCP
    assert defaults["frame"] is FrameType.THREE_E
    assert defaults["encoding"] is Encoding.BINARY
    assert defaults["handshake"] is Handshake.SELF_TEST_AND_IDENTIFY
    assert defaults["port"] == 5000
    assert defaults["allow_remote_control"] is False


def test_a_profile_may_be_named_by_its_key() -> None:
    plc = a_client()
    assert plc.profile is FX5U
    assert plc.peer == (BENCH, 5002)
    assert plc.name == f"{BENCH}:5002"


def test_an_unknown_profile_key_is_refused_with_the_ones_that_exist() -> None:
    """There is no generic profile and ``by_key`` has no fallback."""
    with pytest.raises(SlmpConfigurationError):
        Plc(BENCH, profile="melsec:iq-f/fx5z")


def test_an_encoding_the_profile_does_not_allow_is_refused_at_construction() -> None:
    """``ASCII code (X, Y OCT)`` is an iQ-F own-node setting and exists nowhere else."""
    with pytest.raises(SlmpConfigurationError, match="ASCII"):
        Plc(BENCH, profile=IQ_R, encoding=Encoding.ASCII_XY_OCT)


def test_a_4e_frame_is_refused_on_a_profile_that_has_no_4e() -> None:
    """Capability gating is pre-transport; nothing tries it to find out."""
    without_4e = FX5U.replace(
        capabilities={
            **FX5U.capabilities,
            Capability.FOUR_E_FRAME: _refusal_for(Capability.FOUR_E_FRAME),
        }
    )
    with pytest.raises(SlmpCapabilityError):
        Plc(BENCH, profile=without_4e, frame=FrameType.FOUR_E)


def _refusal_for(capability: Capability) -> Refusal:
    return Refusal(
        reason=f"this CPU does not have {capability.value}",
        evidence=Evidence(Provenance.MANUAL, source="a test", note=""),
    )


def test_the_client_is_what_an_exception_prints_on_its_target_line() -> None:
    """``Plc`` satisfies ``errors.ClientSummary`` structurally, checked by ``mypy``."""
    summary: ClientSummary = a_client()
    assert summary.peer == (BENCH, 5002)
    assert summary.model is None
    assert summary.model_code is None
    assert summary.transport is TransportKind.TCP
    assert summary.encoding is Encoding.BINARY
    assert summary.frame is FrameType.THREE_E


def test_a_capability_override_records_who_claimed_it_and_why() -> None:
    """An override is INFERRED and carries the caller's reason, never a bare True."""
    plc = a_client(capability_overrides={Capability.MONITOR: "fw 1.080 release notes"})
    evidence = plc.profile.evidence_for("monitor")
    assert plc.profile.supports(Capability.MONITOR)
    assert "fw 1.080 release notes" in evidence.note
    assert evidence.provenance.value == "inferred"


def test_a_capability_override_with_no_reason_is_refused() -> None:
    with pytest.raises(SlmpConfigurationError, match="must say why"):
        a_client(capability_overrides={Capability.MONITOR: "  "})


def test_the_plc_clock_source_is_stored_and_never_folded_in_silently() -> None:
    """Only a bound block plan appends the extra point; a caller's request is untouched."""
    source = PlcClockSource("D8", kind="f32")
    plc = a_client(plc_clock=source)
    assert plc.plc_clock is source
    assert a_client().plc_clock is None


# ========================================================================================
# The monitoring timer, which is not the deadline
# ========================================================================================


def test_the_indefinite_timer_is_zero_and_means_wait_forever() -> None:
    """``0x0000`` is not "no timeout" and not "zero milliseconds" (SH(NA)-080956ENG-M p.24).

    JY997D56001-K p.27 mandates it for the FX5 CPU module's own port.
    """
    assert MonitoringTimer.INDEFINITE.units == 0
    assert "indefinite" in str(MonitoringTimer.INDEFINITE)


@pytest.mark.parametrize(("seconds", "units"), [(0.25, 1), (0.5, 2), (1.0, 4), (60.0, 240)])
def test_whole_units_convert(seconds: float, units: int) -> None:
    assert MonitoringTimer.seconds(seconds).units == units


def test_a_timer_that_is_not_a_whole_unit_raises_and_names_both_neighbours() -> None:
    """Nothing rounds. A shortened timer is a PLC that gives up before you asked it to."""
    with pytest.raises(SlmpMonitoringTimerError) as caught:
        MonitoringTimer.seconds(0.3)
    assert "0.25" in str(caught.value)
    assert "0.5" in str(caught.value)


def test_a_deadline_shorter_than_the_timer_is_refused_at_construction() -> None:
    """DESIGN.md section 4.6: they are two independent mechanisms.

    A client deadline that expires first converts the PLC's decodable timeout end code
    into a bare socket timeout, throwing away the one signal that separates "the CPU gave
    up" from "the network ate it".
    """
    with pytest.raises(SlmpMonitoringTimerError, match="two independent mechanisms"):
        a_client(timeout=1.0, monitoring_timer=MonitoringTimer.seconds(2.0))
    a_client(timeout=3.0, monitoring_timer=MonitoringTimer.seconds(2.0))


def test_the_indefinite_timer_never_constrains_the_deadline() -> None:
    a_client(timeout=0.05, monitoring_timer=MonitoringTimer.INDEFINITE)


# ========================================================================================
# One control flow: validate first, and nothing is sent
# ========================================================================================


async def test_validation_raises_before_the_connection_is_even_consulted() -> None:
    """Step 1 of ``_run``: raise before a byte is BUILT, let alone sent.

    ``Y8`` is not a legal octal address at all, and an FX5U-32MT/DS on firmware 1.065
    **accepted** a write at that wire number and answered ``0x0000``. This client refuses
    it without a socket, which is the only place it can be refused.
    """
    plc = a_client()
    with pytest.raises(SlmpDeviceRadixError):
        await plc.read_u16("Y8")


async def test_a_valid_call_on_an_unconnected_client_says_it_is_not_connected() -> None:
    """The counterpart: once validation passes, the missing socket is what is reported."""
    plc = a_client()
    with pytest.raises(SlmpNotConnectedError):
        await plc.read_u16("D0")


def test_the_frame_build_call_passes_expect_body_len() -> None:
    """The third ``L`` guard, asserted structurally rather than hoped for.

    ``payload_len == len(encode)`` is checked by ``checked_encode`` and by a property
    test. ``expect_body_len`` is the *independent* restatement of the same invariant at
    the frame boundary, and U4 shipped it with no caller. A grep-shaped assertion is what
    keeps it wired: the failure it guards is silent in the worst direction.
    """
    tree = ast.parse(CLIENT.read_text(encoding="utf-8"), filename=str(CLIENT))
    builds = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "build"
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "frame"
    ]
    assert builds, "client.py no longer builds a frame at all"
    for call in builds:
        keywords = {keyword.arg for keyword in call.keywords}
        assert "expect_body_len" in keywords, (
            "every FrameFormat.build() in the client must pass expect_body_len. An "
            "understated data length returns 0xC061 and the connection recovers; an "
            "OVERSTATED one gets no response at all and looks exactly like a dead PLC."
        )


def test_a_command_whose_body_len_lies_is_refused_before_the_socket() -> None:
    """The guard fires, rather than being passed and ignored."""
    from aslmp.wire.raw import SlmpFrameFormatError

    plc = a_client()
    command = ReadWords("D0", 2)
    context = EncodeContext(
        codec=BINARY,
        spec=SpecFormat.SHORT,
        profile=FX5U,
        encoding=Encoding.BINARY,
        link=Link.CPU_BUILTIN,
    )
    payload = command.encode(context)
    summary = command.summary(context)

    class Liar(ReadWords):
        def body_len(self, ctx: EncodeContext) -> int:
            return super().body_len(ctx) + 2

    with pytest.raises(SlmpFrameFormatError, match="OVERSTATED"):
        plc._build(Liar("D0", 2), payload, summary, 0x0000, None)


# ========================================================================================
# Word order and packing
# ========================================================================================


def test_low_word_first_is_the_identity_and_matches_the_bench() -> None:
    """1234.5 written as one double word put ``00 50 9A 44`` on the wire, and
    ``D104 = 0x5000``, ``D105 = 0x449A`` came back (FX5U-32MT/DS fw 1.065, 2026-09-06)."""
    import struct

    words = _to_words(struct.pack("<f", 1234.5), WordOrder.LOW_FIRST)
    assert words == (0x5000, 0x449A)


def test_high_word_first_reverses_the_words_and_not_the_bytes_inside_one() -> None:
    """Byte order within a register is a codec property, not a PLC-program convention."""
    import struct

    words = _to_words(struct.pack("<f", 1234.5), WordOrder.HIGH_FIRST)
    assert words == (0x449A, 0x5000)


@pytest.mark.parametrize("order", list(WordOrder))
@pytest.mark.parametrize("value", [0.0, 1234.5, -1.0, 3.4e38])
def test_packing_round_trips_through_both_conventions(order: WordOrder, value: float) -> None:
    import struct

    packed = struct.pack("<f", value)
    assert _from_words(_to_words(packed, order), order) == packed


@pytest.mark.parametrize(("length", "words"), [(1, 1), (2, 1), (3, 2), (8, 4), (9, 5)])
def test_a_string_takes_two_characters_per_register(length: int, words: int) -> None:
    assert _string_words(length) == words


@pytest.mark.parametrize("length", [0, -1, True, 2.5])
def test_a_string_length_that_is_not_a_positive_count_is_refused(length: object) -> None:
    with pytest.raises(SlmpConfigurationError):
        _string_words(length)  # type: ignore[arg-type]  # that is the assertion


# ========================================================================================
# Transports
# ========================================================================================


def test_tcp_is_one_connection_and_one_transaction_in_flight() -> None:
    """A second connection to a one-entry configuration completes and is then FINed, so
    pooling against one entry cannot work and the depth is not configurable."""
    transport = _build_transport(BENCH, 5002, kind=TransportKind.TCP, nodelay=True,
                                 carries_serial=False)
    assert isinstance(transport, TcpTransport)
    assert transport.max_in_flight == 1


def test_udp_is_built_with_the_frames_own_serial_fact() -> None:
    """The transport cannot import ``wire`` and cannot ask a frame whether it has a
    serial No.; it is told, exactly as it is told how to recognise its own response."""
    three_e = _build_transport(BENCH, 5001, kind=TransportKind.UDP, nodelay=True,
                               carries_serial=THREE_E.carries_serial)
    four_e = _build_transport(BENCH, 5001, kind=TransportKind.UDP, nodelay=True,
                              carries_serial=FOUR_E.carries_serial)
    assert isinstance(three_e, UdpTransport)
    assert isinstance(four_e, UdpTransport)
    assert three_e.max_in_flight == 1
    assert four_e.max_in_flight == 1


def test_a_udp_client_gets_a_udp_transport() -> None:
    plc = a_client(port=5001, transport=TransportKind.UDP)
    assert plc.transport is TransportKind.UDP
    assert "udp" in repr(plc)


def test_the_client_can_ask_for_a_udp_pipeline_depth_and_only_on_4e() -> None:
    """Measured on FX5U-32MT/DS fw 1.065 (2026-09-06): 4E/UDP bursts are clean to depth
    32 and lose ~31% at 64 with no end code and no ICMP. The depth is therefore a
    caller's explicit request, never a default, and 3E has no serial to correlate by."""
    deep = _build_transport(BENCH, 5001, kind=TransportKind.UDP, nodelay=True,
                            carries_serial=FOUR_E.carries_serial, udp_pipeline_depth=8)
    assert isinstance(deep, UdpTransport)
    assert deep.max_in_flight == 8
    with pytest.raises(SlmpConfigurationError, match="serial"):
        _build_transport(BENCH, 5001, kind=TransportKind.UDP, nodelay=True,
                         carries_serial=THREE_E.carries_serial, udp_pipeline_depth=8)


def test_a_pipeline_depth_on_tcp_is_refused_not_ignored() -> None:
    """Accepting a number that cannot take effect is the shape of failure this library
    exists to refuse. TCP coalesces, so its depth is 1 structurally."""
    with pytest.raises(SlmpConfigurationError, match="0x0000"):
        _build_transport(BENCH, 5002, kind=TransportKind.TCP, nodelay=True,
                         carries_serial=False, udp_pipeline_depth=8)
    with pytest.raises(SlmpConfigurationError, match="udp_pipeline_depth"):
        a_client(port=5002, udp_pipeline_depth=2)


def test_a_pipelining_udp_client_gives_the_gate_that_capacity() -> None:
    plc = a_client(
        port=5001,
        transport=TransportKind.UDP,
        frame=FrameType.FOUR_E,
        udp_pipeline_depth=16,
    )
    assert plc.transport is TransportKind.UDP


# ========================================================================================
# The escape hatch's command
# ========================================================================================


def test_a_raw_command_reports_its_own_code_rather_than_the_class_attribute() -> None:
    """``CODE`` is a property of a *known* command; a raw command is the absence of one.

    Nothing in the client reads ``cmd.CODE`` -- it reads ``cmd.summary(ctx)``, which this
    class overrides. If that ever changes, a raw ``0x0403`` would go out as ``0x0000``.
    """
    context = EncodeContext(
        codec=BINARY,
        spec=SpecFormat.SHORT,
        profile=FX5U,
        encoding=Encoding.BINARY,
        link=Link.CPU_BUILTIN,
    )
    command = _RawCommand(0x0403, 0x0002, b"\x01\x00")
    summary = command.summary(context)
    assert summary.command == 0x0403
    assert summary.subcommand == 0x0002
    assert command.subcommand(context) == 0x0002
    assert command.payload_len(context) == 2
    assert command.encode(context) == b"\x01\x00"
    assert "0x0403" in command.describe()


def test_a_raw_command_validates_nothing_and_says_so() -> None:
    context = EncodeContext(
        codec=BINARY,
        spec=SpecFormat.SHORT,
        profile=FX5U,
        encoding=Encoding.BINARY,
        link=Link.CPU_BUILTIN,
    )
    _RawCommand(0xFFFF, 0xFFFF, b"\xde\xad").validate(context)


@pytest.mark.parametrize("value", [-1, 0x10000])
def test_a_raw_command_outside_the_16_bit_field_is_refused(value: int) -> None:
    with pytest.raises(SlmpConfigurationError):
        _RawCommand(value, 0x0000)


def test_only_the_silent_raw_command_declares_that_nothing_will_answer() -> None:
    """``response_optional`` is otherwise true for ``0x1006`` Remote Reset alone."""
    assert _RawCommand(0x0401, 0x0000).response_optional is False
    assert _SilentRawCommand(0x1006, 0x0000).response_optional is True
    assert "expect_response=False" in _SilentRawCommand(0x1006, 0x0000).describe()


# ========================================================================================
# The generated twin (DESIGN.md section 4.8)
# ========================================================================================


def run_generator(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(TOOL), *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )


def test_the_committed_timed_module_matches_the_generator() -> None:
    """The CI no-op check. A hand-edit here is a surface that has quietly diverged."""
    result = run_generator()
    assert result.returncode == 0, (
        f"src/aslmp/timed.py is stale:\n{result.stdout}\n{result.stderr}\n"
        f"Regenerate it with: python tools/gen_timed.py --write"
    )


def test_the_generator_is_deterministic() -> None:
    """Two runs must agree byte for byte, or the no-op check above means nothing."""
    first = run_generator("--stdout")
    second = run_generator("--stdout")
    assert first.returncode == second.returncode == 0
    assert first.stdout == second.stdout


def test_the_generated_module_is_parseable_python_that_fits_the_linter() -> None:
    result = run_generator("--stdout")
    assert result.returncode == 0, result.stderr
    ast.parse(result.stdout, filename="<gen_timed output>")
    long_lines = [
        f"line {number}: {len(line)} columns"
        for number, line in enumerate(result.stdout.splitlines(), start=1)
        if len(line) > MAX_LINE
    ]
    assert not long_lines, "\n".join(long_lines)


def test_the_generated_module_says_it_is_generated() -> None:
    head = run_generator("--stdout").stdout[:2000]
    assert "@generated" in head
    assert "gen_timed.py" in head
    assert "Do not edit this file" in head


def test_the_generator_refuses_a_method_with_no_seam() -> None:
    """A ``@mirrored`` method that returns through neither helper has no record to hand
    back, and the generator says so instead of emitting a plausible wrong file."""
    tool = load_tool()
    source = (
        "class Plc:\n"
        "    @mirrored\n"
        "    async def read_nothing(self) -> int:\n"
        "        return 0\n"
    )
    tree = ast.parse(source)
    node = tree.body[0].body[0]  # type: ignore[attr-defined]  # the parsed method
    with pytest.raises(SystemExit, match=r"self\._done"):
        tool.seam_of(node)


def test_the_generator_refuses_a_method_that_uses_both_seams() -> None:
    tool = load_tool()
    source = (
        "class Plc:\n"
        "    @mirrored\n"
        "    async def both(self) -> int:\n"
        "        if x:\n"
        "            return self._done(1, tx)\n"
        "        return self._ack(1, tx)\n"
    )
    node = ast.parse(source).body[0].body[0]  # type: ignore[attr-defined]  # the method
    with pytest.raises(SystemExit):
        tool.seam_of(node)


def test_the_generator_distributes_a_union_rather_than_wrapping_it() -> None:
    """``Reading[A] | Reading[B]``, not ``Reading[A | B]``.

    ``Reading`` is invariant in its parameter, so the wrapped form makes the
    ``Literal[False]`` overload unsatisfiable and hands a caller who did not ask for a
    split a union to narrow.
    """
    tool = load_tool()
    union = ast.parse("A | B", mode="eval").body
    assert tool.timed_return(union, "_done") == "Reading[A] | Reading[B]"
    assert tool.timed_return(union, "_ack") == "WriteAck"
    single = ast.parse("float", mode="eval").body
    assert tool.timed_return(single, "_done") == "Reading[float]"


def mirrored_names() -> list[str]:
    tool = load_tool()
    tree = ast.parse(CLIENT.read_text(encoding="utf-8"), filename=str(CLIENT))
    return [method.name for method in tool.collect(tree)]


def test_every_mirrored_method_appears_on_both_surfaces() -> None:
    """DESIGN.md section 4.8's reflective parity check, by name."""
    names = mirrored_names()
    assert names, "no method of Plc is marked @mirrored"
    timed_public = {
        name
        for name, member in inspect.getmembers(TimedApi, inspect.isfunction)
        if not name.startswith("_")
    }
    assert timed_public == set(names)
    for name in names:
        assert callable(getattr(Plc, name))


@pytest.mark.parametrize("name", mirrored_names())
def test_the_two_surfaces_have_identical_parameter_lists(name: str) -> None:
    """Same names, same kinds, same defaults. Only the return type differs."""
    primary = inspect.signature(getattr(Plc, name))
    twin = inspect.signature(getattr(TimedApi, name))
    assert list(primary.parameters) == list(twin.parameters)
    for parameter, mirror in zip(
        primary.parameters.values(), twin.parameters.values(), strict=True
    ):
        assert parameter.kind is mirror.kind
        assert parameter.default == mirror.default


@pytest.mark.parametrize("name", mirrored_names())
def test_the_generated_docstring_is_the_one_a_person_wrote(name: str) -> None:
    """Copied verbatim from ``client.py``, so there is one place to fix a wrong sentence."""
    assert getattr(TimedApi, name).__doc__ == getattr(Plc, name).__doc__


def test_the_timed_facade_is_built_once_and_cached() -> None:
    """A control loop that reaches for ``plc.timed`` every cycle allocates nothing."""
    plc = a_client()
    facade = plc.timed
    assert isinstance(facade, TimedApi)
    assert plc.timed is facade
    assert facade.plc is plc


# ========================================================================================
# The four silent-wrong-data paths, each with the refusal that closes it
# ========================================================================================


def test_a_plc_clock_declares_its_type_and_is_not_assumed_to_be_a_double_word() -> None:
    """``PlcClockSource`` carried an address and nothing else; plan.py hard-coded u32.

    On the bench ``D8`` is a ``REAL``, so the timing feature published the float's bit
    pattern -- monotonic, plausible, and 16x the real rate (FX5U-32MT/DS fw 1.065 from
    this host over TCP 5002, 2026-09-07: 1018.1 counts/s read as f32, 16273.5 read as u32).
    """
    source = PlcClockSource("D8", kind="f32", bounds=Bounds(0.0, 1.0e7))
    assert source.spec.kind == "f32"
    assert source.spec.struct_code == "f"
    assert source.spec.words == 2
    assert source.spec.bounds == Bounds(0.0, 1.0e7)
    assert a_client(plc_clock=source).plc_clock is source


def test_a_plc_clock_kind_that_is_not_one_access_point_is_refused_at_construction() -> None:
    for kind in ("bits", "f64", "real", ""):
        with pytest.raises(SlmpConfigurationError, match="not a type one access point"):
            PlcClockSource("D8", kind=kind)  # type: ignore[arg-type]


def test_a_plc_clock_bound_its_width_cannot_reach_is_refused_at_construction() -> None:
    """The same rule a bounded block field lives under, from the same code."""
    with pytest.raises(SlmpBlockLayoutError, match="outside what a U16 can hold"):
        PlcClockSource("D8", kind="u16", bounds=Bounds(maximum=70_000))


def test_write_i16_refuses_a_value_outside_the_signed_range_it_named() -> None:
    """The regression: ``write_i16(40000)`` used to be accepted and read back -25536.

    Verified on the CPU before the fix (FX5U-32MT/DS fw 1.065, TCP 5002, 2026-09-07):
    ``write_i16("D100", 40000)`` returned normally and ``read_i16("D100")`` answered
    ``-25536``, with end code ``0x0000`` at every step.
    """
    assert unsigned(40_000, bits=16, what="x", signed_field=None) == 40_000  # raw: may
    with pytest.raises(SlmpValueRangeError, match="signed 16-bit field"):
        unsigned(40_000, bits=16, what="write_i16(D100)", signed_field=True)
    with pytest.raises(SlmpValueRangeError, match="unsigned 16-bit field"):
        unsigned(-1, bits=16, what="write_u16(D100)", signed_field=False)
    assert unsigned(-1, bits=16, what="x", signed_field=True) == 0xFFFF
    assert unsigned(65_535, bits=16, what="x", signed_field=False) == 0xFFFF


def test_a_word_value_that_is_not_an_int_is_a_slmp_error_and_not_a_type_error() -> None:
    """``write_words("D100", [1.5])`` raised a bare TypeError from ``1.5 & 0xFFFF``."""
    with pytest.raises(SlmpValueRangeError, match="takes an int, not float"):
        unsigned(1.5, bits=16, what="write_words(D100) value 0", signed_field=None)
    with pytest.raises(SlmpValueRangeError, match="takes an int, not bool"):
        unsigned(True, bits=16, what="write_words(D100) value 0", signed_field=None)


def test_a_float_value_domain_failure_is_a_slmp_error_and_not_an_overflow_error() -> None:
    """``write_f32("D100", 1e39)`` raised OverflowError; ``"x"`` raised struct.error."""
    with pytest.raises(SlmpValueRangeError, match="past what an IEEE-754"):
        real(1e39, bits=32, what="write_f32(D100)")
    with pytest.raises(SlmpValueRangeError, match="takes a real number, not str"):
        real("x", bits=32, what="write_f32(D100)")
    with pytest.raises(SlmpValueRangeError, match="too large to be a float at all"):
        real(10**400, bits=32, what="write_f32(D100)")
    assert real(float("inf"), bits=32, what="x") == float("inf")


def test_the_f32_boundary_is_struct_pack_and_not_a_constant() -> None:
    """A regression: comparing against FLT_MAX refused values that pack perfectly well.

    The representable maximum is not the acceptable maximum. Under round-to-nearest every
    double below the midpoint of FLT_MAX and the next exponent rounds DOWN to FLT_MAX, so
    ``3.4028235e38`` -- the literal everybody writes for "max float32", and the one our own
    hardware test uses against the PLC -- is legal. An earlier fix rejected it.

    The boundary is therefore decided by ``struct.pack`` rather than by a constant, which
    makes it exact by construction. This test pins the two sides of it.
    """
    packs_fine = [
        3.4028234663852886e38,   # FLT_MAX itself
        3.4028235e38,            # the usual literal; rounds down to FLT_MAX
        -3.4028235e38,
        3.4028235677973360e38,   # just under the midpoint; still rounds down
    ]
    for value in packs_fine:
        struct.pack("<f", value)                      # the oracle: it really does pack
        assert real(value, bits=32, what="probe") == value

    does_not_pack = [3.4028235677973366e38, -3.4028235677973366e38, 1e39]
    for value in does_not_pack:
        # OverflowError, not struct.error: CPython raises the former for a float whose
        # magnitude is past the format, and that difference is exactly why the bare
        # exception escaping to callers was worth catching in the first place.
        with pytest.raises((OverflowError, struct.error)):
            struct.pack("<f", value)
        with pytest.raises(SlmpValueRangeError, match="past what an IEEE-754"):
            real(value, bits=32, what="probe")
    assert real(1e39, bits=64, what="x") == 1e39


def test_a_bit_value_that_is_not_a_bool_is_refused_rather_than_coerced() -> None:
    """``write_bits("M100", [2, -1, "yes"])`` used to write three ones and say ok."""
    values: tuple[object, ...] = (2, -1, "yes", 0.0, [], None)
    for value in values:
        with pytest.raises(SlmpValueRangeError, match="takes True or False"):
            boolean(value, what="write_bits(M100) value 0")
    assert boolean(True, what="x") is True
    assert boolean(False, what="x") is False


def test_a_raw_remote_control_command_needs_the_same_interlock_remote_does() -> None:
    """``_RawCommand.validate()`` is a no-op, so the client is where this has to live.

    Verified before the fix (FX5U-32MT/DS fw 1.065, 2026-09-07): ``plc.remote.run()``
    raised and ``plc.raw_command(0x1001, 0x0000, ...)`` succeeded on the same client.
    """
    assert set(REMOTE_CONTROL_COMMANDS) == {0x1001, 0x1002, 0x1003, 0x1005, 0x1006}
    locked = a_client()
    for code in REMOTE_CONTROL_COMMANDS:
        with pytest.raises(SlmpConfigurationError, match="allow_remote_control=True"):
            locked._require_interlock_for(code)
    locked._require_interlock_for(0x0403)
    unlocked = a_client(allow_remote_control=True)
    for code in REMOTE_CONTROL_COMMANDS:
        unlocked._require_interlock_for(code)


def test_read_block_and_write_block_refuse_a_plan_bound_to_another_client() -> None:
    """The regression: ``self`` was unused, so both transacted on the plan's own client.

    Verified before the fix (FX5U-32MT/DS fw 1.065, TCP 5002, 2026-09-07): a ``Plc`` that
    had never been connected, aimed at a host that does not exist, returned a populated
    block and reported a successful write, while the bound client's counters moved and
    ``D100``/``D101`` on the real CPU took the values passed to the ghost.
    """

    @plc_block(base="D100")
    class Two:
        a: U16
        b: U16

    bound = a_client()
    plan = bound.bind(Two)
    ghost = Plc("10.255.255.1", 5099, profile="melsec:iq-f/fx5u")
    assert plan.plc is bound

    for what in ("read_block()", "write_block()"):
        with pytest.raises(SlmpConfigurationError, match="bound to") as caught:
            ghost._own_plan(plan, what)
        assert what in str(caught.value)
    assert bound._own_plan(plan, "read_block()") is plan


def test_write_random_holds_each_value_to_the_type_its_own_point_names() -> None:
    """The same defect one layer along: a point's ``kind`` is a named type too.

    This check refuses at the call, naming the caller's own value index, before a frame
    exists. It is no longer the only thing that refuses:
    ``RandomWrite.wire_value()`` reads the point's declared domain too, so a caller who
    builds the command directly gets the same answer
    (``tests/unit/test_commands_random.py``). Both now read one table, on the point.
    """
    from aslmp.commands.random import dword, word

    _check_point_value(RandomWrite(word("D100", kind="i16"), 32_767), 0)
    _check_point_value(RandomWrite(word("D100", kind="u16"), -1), 0)
    _check_point_value(RandomWrite(dword("D100", kind="f32"), 1.5), 0)
    for bad in (
        RandomWrite(word("D100", kind="i16"), 40_000),
        RandomWrite(dword("D100", kind="i32"), 3_000_000_000),
        RandomWrite(dword("D100", kind="f32"), 1e39),
        RandomWrite(word("D100", kind="u16"), 70_000),
    ):
        with pytest.raises(SlmpValueRangeError):
            _check_point_value(bad, 0)


ACCENTED = "caf" + chr(0xE9)
"""``cafe`` with an acute accent, built rather than typed so this file stays ASCII.

ASCII cannot carry it and UTF-8 encodes it as two bytes, which is the whole test."""


async def test_write_str_refuses_a_value_domain_failure_inside_the_error_tree() -> None:
    """The regression: ``value.encode(encoding)`` was unguarded on all three write paths.

    ``plc.write_str("D100", b"x", length=4)`` raised ``AttributeError`` -- ``bytes`` has
    no ``.encode`` -- from inside a write path, so a caller's ``except SlmpError`` around
    the write saw nothing and the traceback read as a library bug. A character the codec
    cannot carry raised ``UnicodeEncodeError`` and an unknown codec name raised
    ``LookupError``, neither of them in the DESIGN section 3.1 tree either.

    All three are refused before the socket is consulted, which is what the
    ``SlmpNotConnectedError`` on the good value proves: this client has no connection, so
    anything that got as far as sending would report that instead.
    """
    plc = a_client()
    with pytest.raises(SlmpValueRangeError, match="takes a str, not bytes"):
        await plc.write_str("D100", b"x", length=4)  # type: ignore[arg-type]
    with pytest.raises(SlmpValueRangeError, match="takes a str, not int"):
        await plc.write_str("D100", 42, length=4)  # type: ignore[arg-type]
    with pytest.raises(SlmpValueRangeError, match="cannot be encoded as ascii"):
        await plc.write_str("D100", ACCENTED, length=8)
    with pytest.raises(SlmpConfigurationError, match="not a codec Python knows"):
        await plc.write_str("D100", "ok", length=4, encoding="utf-9")
    with pytest.raises(SlmpNotConnectedError):
        await plc.write_str("D100", "ok", length=4)

    # And the same three on plc.timed, which is the generated copy of this method.
    with pytest.raises(SlmpValueRangeError, match="takes a str, not bytes"):
        await plc.timed.write_str("D100", b"x", length=4)  # type: ignore[arg-type]
    with pytest.raises(SlmpValueRangeError, match="cannot be encoded as ascii"):
        await plc.timed.write_str("D100", ACCENTED, length=8)
    with pytest.raises(SlmpConfigurationError, match="not a codec Python knows"):
        await plc.timed.write_str("D100", "ok", length=4, encoding="utf-9")


def test_a_string_that_cannot_be_encoded_is_not_silently_substituted() -> None:
    """``str.encode`` has ``errors="replace"``, and this library does not use it: a part
    number written with ``?`` where its accent was is a different part number."""
    assert encoded("ok", encoding="ascii", what="x") == b"ok"
    assert encoded(ACCENTED, encoding="utf-8", what="x") == bytes((99, 97, 102, 195, 169))
    with pytest.raises(SlmpValueRangeError) as caught:
        encoded(ACCENTED, encoding="ascii", what="write_str(D100)")
    assert "write_str(D100)" in str(caught.value)
    assert "character 3" in str(caught.value)


def test_a_plc_clock_kind_is_required_rather_than_defaulting_to_a_guess() -> None:
    """The last of the same shape: a default standing in for a fact only the caller has.

    ``kind`` defaulted to ``"u32"`` "for compatibility" in the revision that introduced
    it, which reproduces the exact defect it was added to fix -- on the bench that
    motivated the class, ``D8`` is a ``REAL`` and ``PlcClockSource("D8")`` would still
    have published a float's bit pattern. Nothing on the wire can detect the omission,
    so the omission is refused instead. 0.1.0.dev0, no released users, no guess carried
    forward.
    """
    parameter = inspect.signature(PlcClockSource).parameters["kind"]
    assert parameter.default is inspect.Parameter.empty
    with pytest.raises(TypeError, match="kind"):
        PlcClockSource("D8")  # type: ignore[call-arg]
    assert PlcClockSource("D8", kind="f32").spec.struct_code == "f"


def test_monitor_read_refuses_a_registration_made_by_another_client() -> None:
    """``_own_plan``'s weaker cousin, and the same failure mode.

    ``ExecuteMonitor.validate`` checks the registration's PROFILE KEY, which two clients
    aimed at two different FX5Us share exactly. A ``0802`` request carries no device
    specification at all, so the registration is the only thing that can parse the
    response: executed against the wrong CPU it returns that CPU's registers labelled
    with this list's addresses, end code ``0x0000``.
    """
    from aslmp.commands.monitor import MonitorRegistration

    line_one = a_client()
    line_two = Plc("10.255.255.1", 5099, profile="melsec:iq-f/fx5u")
    registration = MonitorRegistration(
        points=(word("D0"),), profile_key=FX5U.key, subcommand=0
    ).owned_by(line_one)

    assert line_one._own_registration(registration, "monitor_read()") is registration
    with pytest.raises(SlmpConfigurationError, match="registration made by") as caught:
        line_two._own_registration(registration, "monitor_read()")
    assert "monitor_read()" in str(caught.value)
    assert FX5U.key in str(caught.value)

    # The profile key alone would have let this through: it is the same key.
    assert registration.profile_key == FX5U.key == line_two.profile.key


async def test_monitor_read_calls_that_guard_before_it_touches_the_socket() -> None:
    """The guard has to be wired in, not merely available.

    Both clients are iQ-F, where ``0802`` is a capability refusal -- the CPU answers
    ``0xC059`` and the profile refuses pre-transport. So the owner's call gets as far as
    command validation and raises ``SlmpCapabilityError``, while the stranger's never
    reaches it: ownership is checked first, in the client, before anything is built.
    """
    from aslmp.commands.monitor import MonitorRegistration

    owner = a_client()
    stranger = Plc("10.255.255.1", 5099, profile="melsec:iq-f/fx5u")
    registration = MonitorRegistration(
        points=(word("D0"),), profile_key=FX5U.key, subcommand=0
    ).owned_by(owner)

    with pytest.raises(SlmpConfigurationError, match="registration made by"):
        await stranger.monitor_read(registration)
    with pytest.raises(SlmpConfigurationError, match="registration made by"):
        await stranger.timed.monitor_read(registration)
    with pytest.raises(SlmpCapabilityError, match="0xC059"):
        await owner.monitor_read(registration)


def test_a_monitor_registration_nobody_registered_is_refused_too() -> None:
    """A hand-built registration never described an 0801 any CPU acknowledged."""
    from aslmp.commands.monitor import MonitorRegistration

    plc = a_client()
    orphan = MonitorRegistration(points=(word("D0"),), profile_key=FX5U.key, subcommand=0)
    assert orphan.owner is None
    with pytest.raises(SlmpConfigurationError, match="constructed directly"):
        plc._own_registration(orphan, "monitor_read()")


def test_the_owner_stamp_is_not_part_of_a_registrations_identity() -> None:
    """Two registrations describing the same request still compare equal, and no repr of
    one drags a whole client into a log line."""
    from aslmp.commands.monitor import MonitorRegistration

    plain = MonitorRegistration(points=(word("D0"),), profile_key=FX5U.key, subcommand=0)
    stamped = plain.owned_by(a_client())
    assert stamped == plain
    assert stamped is not plain
    assert plain.owner is None  # owned_by copies; it never mutates
    assert "Plc" not in repr(stamped)
