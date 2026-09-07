"""Every ``validate()`` refusal, one test each -- and every decode-side refusal too.

These are the tests that matter most on this hardware, because the PLC is not a backstop.
An FX5U-32MT/DS on firmware 1.065 **accepted** a ``TS0`` word point in a Read Random and
answered ``0x0000`` with data, against its own manual, and it accepted a write to ``Y8``,
an address octal notation cannot express (measured 2026-09-06 and 2026-09-07). Anything
this library does not refuse, it sends -- and this firmware will answer some of it with a
plausible-looking wrong number.

Each refusal also has to say the right thing. Where a limit is measured, the message
quotes the end code the CPU itself would have answered (``0xC051``, ``0xC052``,
``0xC054``, ``0xC056``, ``0xC059``), so a user who has seen that code in a packet capture
can match it against our client-side "no".
"""

from __future__ import annotations

import pytest

from aslmp.commands import (
    AccessWidth,
    BlockSpec,
    BlockWrite,
    ClearError,
    EncodeContext,
    ExecuteMonitor,
    LockPassword,
    MonitorRegistration,
    RandomPoint,
    RandomWrite,
    ReadBits,
    ReadBlocks,
    ReadRandom,
    ReadTypeName,
    ReadWords,
    RegisterMonitor,
    RemoteLatchClear,
    RemotePause,
    RemoteReset,
    RemoteRun,
    RemoteStop,
    SelfTest,
    UnlockPassword,
    WriteBits,
    WriteBlocks,
    WriteRandom,
    WriteRandomBits,
    WriteWords,
    bit_point,
    dword,
    word,
)
from aslmp.commands.base import Command
from aslmp.commands.info import DEFAULT_LOOPBACK
from aslmp.commands.random import BitWrite
from aslmp.errors import (
    SlmpAddressRangeError,
    SlmpCapabilityError,
    SlmpConfigurationError,
    SlmpDeviceNotAllowedHereError,
    SlmpDeviceNotOnCpuError,
    SlmpPayloadShapeError,
    SlmpPointLimitError,
    SlmpValueRangeError,
)
from aslmp.profile import ClearMode, Encoding, Link
from aslmp.profiles import FX5U, IQ_R
from aslmp.wire.codec import ASCII, BINARY, SpecFormat


def fx5u(**kw: object) -> EncodeContext:
    fields: dict[str, object] = {
        "codec": BINARY,
        "spec": SpecFormat.SHORT,
        "profile": FX5U,
        "encoding": Encoding.BINARY,
        "link": Link.CPU_BUILTIN,
    }
    fields.update(kw)
    return EncodeContext(**fields)  # type: ignore[arg-type]  # a test-local kwargs bag


def iqr(**kw: object) -> EncodeContext:
    fields: dict[str, object] = {
        "codec": BINARY,
        "spec": SpecFormat.SHORT,
        "profile": IQ_R,
        "encoding": Encoding.BINARY,
        "link": Link.CPU_BUILTIN,
    }
    fields.update(kw)
    return EncodeContext(**fields)  # type: ignore[arg-type]  # a test-local kwargs bag


CTX = fx5u()
IQR = iqr()
REMOTE = fx5u(allow_remote_control=True)
REMOTE_IQR = iqr(allow_remote_control=True)


def refusal(command: Command[object], ctx: EncodeContext = CTX) -> str:
    """Validate and return the message, asserting that it refused at all."""
    with pytest.raises(Exception) as caught:
        command.validate(ctx)
    return str(caught.value)


# ======================================================================================
# The encoding context itself
# ======================================================================================


def test_a_context_pairing_the_wrong_codec_with_an_encoding_is_refused() -> None:
    """Binary bytes with ASCII lengths is the overstated-L hang, before it is sent."""
    with pytest.raises(SlmpConfigurationError, match="two views of one GX Works3"):
        fx5u(codec=ASCII, encoding=Encoding.BINARY)


def test_a_context_using_an_encoding_the_profile_forbids_is_refused() -> None:
    """``ASCII code (X, Y OCT)`` is an iQ-F own-node setting and exists nowhere else."""
    with pytest.raises(SlmpConfigurationError, match="ASCII_XY_OCT"):
        iqr(codec=ASCII, encoding=Encoding.ASCII_XY_OCT)


# ======================================================================================
# 0401 / 1401 batch
# ======================================================================================


def test_zero_points_is_a_point_count_error_and_says_so() -> None:
    """Measured: ``00 00 00 A8 00 00`` returned 0xC052, not an address error."""
    message = refusal(ReadWords("D0", 0))
    assert "0xC052" in message
    assert "zero" in message.lower()


def test_961_word_points_quotes_the_measured_ceiling_and_its_end_code() -> None:
    with pytest.raises(SlmpPointLimitError) as caught:
        ReadWords("D0", 961).validate(CTX)
    message = str(caught.value)
    assert "960" in message and "961" in message
    assert "0xC052" in message


def test_3585_bit_points_quotes_the_measured_bit_ceiling() -> None:
    """3584, not the generic reference manual's 7168. Binary-searched on the bench."""
    with pytest.raises(SlmpPointLimitError) as caught:
        ReadBits("M0", 3585).validate(CTX)
    message = str(caught.value)
    assert "3584" in message
    assert "0xC051" in message


def test_3584_bit_points_is_accepted() -> None:
    ReadBits("M0", 3584).validate(CTX)


def test_a_bit_unit_read_of_a_word_device_is_refused() -> None:
    with pytest.raises(SlmpDeviceNotAllowedHereError, match="word device"):
        ReadBits("D0", 16).validate(CTX)


def test_the_span_is_checked_and_not_only_the_start() -> None:
    """``D7999`` alone is legal; ``D7999`` for two words is not. Both measured."""
    ReadWords("D7999", 1).validate(CTX)
    with pytest.raises(SlmpAddressRangeError, match="0xC056"):
        ReadWords("D7999", 2).validate(CTX)


def test_a_device_family_this_cpu_does_not_have_is_refused_before_the_wire() -> None:
    """``V``, ``ZR``, ``DX`` and ``DY`` all returned 0xC05C on the FX5U, not 0xC05B."""
    for name in ("V0", "ZR0", "DX0", "DY0"):
        with pytest.raises(SlmpDeviceNotOnCpuError):
            ReadWords(name, 1).validate(CTX)


def test_r_is_present_on_an_fx5u_and_is_not_refused() -> None:
    """Measured: ``R0`` read fine where ``ZR0`` returned 0xC05C."""
    ReadWords("R0", 1).validate(CTX)


@pytest.mark.parametrize("name", ["LTS0", "LTC0", "LTN0", "LSTS0", "LSTC0", "LZ0"])
def test_the_long_families_batch_access_forbids_are_refused(name: str) -> None:
    with pytest.raises(SlmpDeviceNotAllowedHereError):
        ReadWords(name, 1).validate(IQR)


def test_a_negative_point_count_is_refused_at_construction() -> None:
    with pytest.raises(SlmpConfigurationError, match="negative"):
        ReadWords("D0", -1)


def test_the_long_device_specification_is_refused_on_an_iq_f() -> None:
    """Subcommand 0x0002 answered 0xC059 on the FX5U, measured."""
    with pytest.raises(SlmpCapabilityError, match="0xC059"):
        ReadWords("D0", 1).validate(fx5u(spec=SpecFormat.LONG))


def test_a_word_value_that_does_not_fit_16_bits_is_refused_not_masked() -> None:
    """``pymcprotocol`` writes 0x1FFFF into a 16-bit register as 0xFFFF and says ok."""
    with pytest.raises(SlmpValueRangeError, match="does not fit"):
        WriteWords("D0", (0x1FFFF,))


def test_a_write_of_a_signed_value_is_accepted_as_twos_complement() -> None:
    assert WriteWords("D0", (-1,)).encode(CTX).endswith(b"\xff\xff")


def test_an_odd_bit_write_length_is_not_twice_the_binary_length() -> None:
    """The one place the 2x rule breaks. Overstating it gets no response at all."""
    command = WriteBits("M0", (True, False, True, False, True))
    assert command.payload_len(CTX) == len(command.encode(CTX))
    ascii_ctx = fx5u(codec=ASCII, encoding=Encoding.ASCII_XY_HEX)
    assert command.payload_len(ascii_ctx) == len(command.encode(ascii_ctx))
    binary_data = len(command.encode(CTX)) - CTX.device_spec_len() - 2
    ascii_data = len(command.encode(ascii_ctx)) - ascii_ctx.device_spec_len() - 4
    assert ascii_data == 2 * binary_data - 1


# ======================================================================================
# 0403 / 1402 random -- the control-loop primitive
# ======================================================================================


@pytest.mark.parametrize("name", ["TS0", "TC0", "STS0", "STC0", "CS0", "CC0"])
def test_the_contacts_and_coils_the_fx5u_would_have_accepted_are_refused(
    name: str,
) -> None:
    """The load-bearing guard. ``0403`` with a ``TS0`` word point returned 0x0000 with
    data on FX5U-32MT/DS fw 1.065 (2026-09-06), against JY997D56001-K p.78."""
    with pytest.raises(SlmpDeviceNotAllowedHereError) as caught:
        ReadRandom((word(name),)).validate(CTX)
    assert "FX5U-32MT/DS" in str(caught.value)


@pytest.mark.parametrize("name", ["LCS0", "LCC0"])
def test_the_long_counter_contacts_and_coils_are_refused_in_random(name: str) -> None:
    with pytest.raises(SlmpDeviceNotAllowedHereError):
        ReadRandom((word(name),)).validate(IQR)


def test_the_current_values_are_allowed_in_random() -> None:
    """``TN``, ``STN``, ``CN`` are fine -- only the contacts and coils are excluded."""
    ReadRandom((word("TN0"), word("STN0"), word("CN0"))).validate(CTX)


def test_193_random_points_quotes_the_measured_ceiling() -> None:
    with pytest.raises(SlmpPointLimitError) as caught:
        ReadRandom(tuple(word(f"D{i}") for i in range(193))).validate(CTX)
    message = str(caught.value)
    assert "192" in message
    assert "0xC054" in message


def test_192_random_points_is_accepted_in_either_mix() -> None:
    """Measured: 192 word, 192 double-word and 96+96 all returned 0x0000."""
    ReadRandom(tuple(word(f"D{i}") for i in range(192))).validate(CTX)
    ReadRandom(tuple(dword(f"D{2 * i}") for i in range(192))).validate(CTX)
    mixed = tuple(word(f"D{i}") for i in range(96)) + tuple(
        dword(f"D{200 + 2 * i}") for i in range(96)
    )
    ReadRandom(mixed).validate(CTX)


def test_a_random_request_with_no_points_is_refused() -> None:
    assert "0xC052" in refusal(ReadRandom(()))


def test_a_bits_point_on_a_word_device_is_refused() -> None:
    with pytest.raises(SlmpDeviceNotAllowedHereError, match="word device"):
        ReadRandom((bit_point("D0"),)).validate(CTX)


def test_a_width_and_kind_that_disagree_are_refused_at_construction() -> None:
    with pytest.raises(SlmpConfigurationError, match="cannot be read as"):
        RandomPoint("D0", AccessWidth.DWORD, "u16")
    with pytest.raises(SlmpConfigurationError, match="cannot be read as"):
        RandomPoint("D0", AccessWidth.WORD, "f32")


def test_a_double_word_point_at_the_end_of_the_range_is_refused() -> None:
    """One double-word point at D7999 reaches D8000, which returned 0xC056."""
    ReadRandom((word("D7999"),)).validate(CTX)
    with pytest.raises(SlmpAddressRangeError, match="0xC056"):
        ReadRandom((dword("D7999"),)).validate(CTX)


def test_the_1402_budget_is_weighted_and_a_flat_count_would_be_wrong_both_ways() -> None:
    """160 word points fit under ``word x 12 + dword x 14 <= 1920``; 138 dword do not."""
    writes = tuple(
        RandomWrite(word(f"D{i}"), 0) for i in range(160)
    )
    WriteRandom(writes).validate(CTX)
    too_many = tuple(RandomWrite(dword(f"D{2 * i}"), 0) for i in range(138))
    with pytest.raises(SlmpPointLimitError) as caught:
        WriteRandom(too_many).validate(CTX)
    assert "1920" in str(caught.value)


def test_a_bits_kind_cannot_be_written_by_word_units() -> None:
    with pytest.raises(SlmpConfigurationError, match="bit units"):
        RandomWrite(bit_point("M0"), 1)


def test_a_word_random_write_value_that_does_not_fit_is_refused() -> None:
    item = RandomWrite(word("D0"), 0x1FFFF)
    with pytest.raises(SlmpValueRangeError, match="does not fit"):
        item.wire_value()


def test_189_bit_write_points_exceeds_the_documented_188() -> None:
    with pytest.raises(SlmpPointLimitError) as caught:
        WriteRandomBits(
            tuple(BitWrite(f"M{i}", True) for i in range(189))
        ).validate(CTX)
    assert "188" in str(caught.value)


def test_a_bit_write_to_a_word_device_is_refused() -> None:
    with pytest.raises(SlmpDeviceNotAllowedHereError, match="no single bit"):
        WriteRandomBits((BitWrite("D0", True),)).validate(CTX)


# ======================================================================================
# 0406 / 1406 block
# ======================================================================================


def test_121_blocks_exceeds_the_block_count_ceiling() -> None:
    with pytest.raises(SlmpPointLimitError) as caught:
        ReadBlocks(tuple(BlockSpec(f"D{i}", 1) for i in range(121))).validate(CTX)
    assert "120" in str(caught.value)


def test_a_block_read_over_960_total_points_is_refused() -> None:
    with pytest.raises(SlmpPointLimitError):
        ReadBlocks((BlockSpec("D0", 500), BlockSpec("D1000", 500))).validate(CTX)


@pytest.mark.parametrize("name", ["LTN0", "LSTN0", "LCN0", "LZ0"])
def test_the_families_block_access_forbids_are_refused(name: str) -> None:
    with pytest.raises(SlmpDeviceNotAllowedHereError):
        ReadBlocks((BlockSpec(name, 1),)).validate(IQR)


def _zeros(address: str, points: int) -> BlockWrite:
    return BlockWrite(address, tuple(0 for _ in range(points)))


def test_the_1406_budget_charges_per_block_overhead() -> None:
    """``BlockRule(120, 4, 760)`` on the iQ-F: 750 points plus 2 x 4 words of per-block
    overhead is 758 and fits; 760 points plus the same overhead does not. The overhead
    figure is Provenance.INFERRED in every shipped profile and the refusal says so."""
    WriteBlocks((_zeros("D0", 700), _zeros("D1000", 50))).validate(CTX)
    with pytest.raises(SlmpPointLimitError):
        WriteBlocks((_zeros("D0", 700), _zeros("D1000", 60))).validate(CTX)


# ======================================================================================
# 0801 / 0802 monitor -- capability-gated, never a wire attempt
# ======================================================================================


def test_monitor_registration_is_refused_on_an_iq_f_citing_the_measurement() -> None:
    with pytest.raises(SlmpCapabilityError) as caught:
        RegisterMonitor((word("D0"),)).validate(CTX)
    message = str(caught.value)
    assert "0xC059" in message
    assert "FX5U-32MT/DS" in message


def test_execute_monitor_is_refused_on_an_iq_f() -> None:
    registration = MonitorRegistration(
        points=(word("D0"),), profile_key=FX5U.key, subcommand=0x0000
    )
    with pytest.raises(SlmpCapabilityError, match="0xC059"):
        ExecuteMonitor(registration).validate(CTX)


def test_monitor_is_never_silently_substituted_with_a_read_random() -> None:
    """The refusal offers ``read_random()`` and does not perform it."""
    with pytest.raises(SlmpCapabilityError) as caught:
        RegisterMonitor((word("D0"),)).validate(CTX)
    assert "read_random" in str(caught.value)


def test_monitor_registration_is_available_on_an_iq_r() -> None:
    RegisterMonitor((word("D0"), dword("D2"))).validate(IQR)


@pytest.mark.parametrize("name", ["LTN0", "LSTN0", "LCN0"])
def test_the_long_current_values_cannot_be_registered_for_monitoring(name: str) -> None:
    """A broader exclusion than 0403's: SH(NA)-080956ENG-M p.63 lists LTN, LSTN, LCN."""
    with pytest.raises(SlmpDeviceNotAllowedHereError):
        RegisterMonitor((word(name),)).validate(IQR)


def test_a_registration_from_another_profile_is_refused() -> None:
    registration = MonitorRegistration(
        points=(word("D0"),), profile_key=FX5U.key, subcommand=0x0000
    )
    with pytest.raises(SlmpConfigurationError, match="registered against"):
        ExecuteMonitor(registration).validate(IQR)


def test_an_empty_registration_is_refused_at_construction() -> None:
    with pytest.raises(SlmpConfigurationError, match="nothing to read"):
        MonitorRegistration(points=(), profile_key=IQ_R.key, subcommand=0)


# ======================================================================================
# Remote control -- the interlock, the clear mode and the fixed field
# ======================================================================================


@pytest.mark.parametrize(
    "command",
    [
        RemoteRun(),
        RemoteStop(),
        RemotePause(),
        RemoteLatchClear(),
        RemoteReset(),
        UnlockPassword("secret"),
        LockPassword("secret"),
    ],
    ids=lambda c: type(c).__name__,
)
def test_remote_control_needs_the_explicit_interlock(command: Command[None]) -> None:
    """Every one of these can stop a running machine over a cleartext socket."""
    if isinstance(command, UnlockPassword | LockPassword):
        pytest.skip("passwords are gated by the capability, not the run interlock")
    with pytest.raises(SlmpConfigurationError, match="allow_remote_control=True"):
        command.validate(CTX)


def test_remote_run_with_the_interlock_set_is_accepted() -> None:
    RemoteRun().validate(REMOTE)


@pytest.mark.parametrize(
    "clear", [ClearMode.EXCEPT_LATCH, ClearMode.INCLUDING_LATCH], ids=lambda c: c.name
)
def test_a_clear_mode_an_iq_f_does_not_document_is_refused(clear: ClearMode) -> None:
    """The FX5 manual contradicts itself on one page; refusing is the safe direction."""
    with pytest.raises(SlmpConfigurationError) as caught:
        RemoteRun(clear=clear).validate(REMOTE)
    assert "A-CLEAR-MODE" in str(caught.value)


def test_the_same_clear_mode_is_accepted_on_an_iq_r() -> None:
    RemoteRun(clear=ClearMode.INCLUDING_LATCH).validate(REMOTE_IQR)


def test_the_fixed_field_comes_from_the_profile_and_differs_by_family() -> None:
    """A-REMOTE-FIXED: 00 00 on iQ-F, 01 00 on the SLMP-reference families."""
    assert RemoteStop().encode(REMOTE) == b"\x00\x00"
    assert RemoteStop().encode(REMOTE_IQR) == b"\x01\x00"


def test_the_fixed_field_is_overridable_for_whoever_runs_the_probe() -> None:
    assert RemoteStop(b"\x01\x00").encode(REMOTE) == b"\x01\x00"
    assert RemoteReset(b"\x00\x00").encode(REMOTE_IQR) == b"\x00\x00"


def test_a_fixed_field_of_the_wrong_width_is_refused() -> None:
    with pytest.raises(SlmpConfigurationError, match="exactly two bytes"):
        RemoteStop(b"\x01").encode(REMOTE)


def test_remote_reset_is_the_only_command_whose_response_may_be_absent() -> None:
    assert RemoteReset.response_optional is True
    for other in (RemoteRun, RemoteStop, RemotePause, RemoteLatchClear, ReadWords):
        assert other.response_optional is False


# ======================================================================================
# 0619 / 0101 / 1617
# ======================================================================================


@pytest.mark.parametrize(
    "payload", [b"", b"ZZZZ", b"abcd", b"AB CD", b"\x00\x01", b"0" * 961]
)
def test_a_loopback_payload_outside_the_documented_charset_is_refused(
    payload: bytes,
) -> None:
    """Both manuals restrict 0619 data to '0'-'9' and 'A'-'F'. A-LOOPBACK-CHARSET."""
    with pytest.raises(SlmpConfigurationError) as caught:
        SelfTest(payload).validate(CTX)
    assert "A-LOOPBACK-CHARSET" in str(caught.value)


def test_every_shipped_default_loopback_payload_passes_its_own_check() -> None:
    """Including our own defaults, which is the whole point of the rule."""
    for codec in (BINARY, ASCII):
        assert codec.loopback_payload_ok(DEFAULT_LOOPBACK)
        assert codec.loopback_payload_ok(b"0619" + b"A1B2")
    SelfTest().validate(CTX)


def test_the_handshake_payload_shape_is_a_legal_loopback_payload() -> None:
    """``connect()`` sends b'0619' plus four hex digits of a per-generation nonce."""
    for nonce in (0x0000, 0x1234, 0xFFFF):
        SelfTest(b"0619" + f"{nonce:04X}".encode("ascii")).validate(CTX)


def test_a_self_test_echo_that_differs_is_refused_rather_than_returned() -> None:
    command = SelfTest(b"ABCD")
    with pytest.raises(SlmpPayloadShapeError, match="exact echo"):
        command.decode(b"\x04\x00ABCE", CTX)


def test_a_self_test_count_that_disagrees_with_its_data_is_refused() -> None:
    command = SelfTest(b"ABCD")
    with pytest.raises(SlmpPayloadShapeError):
        command.decode(b"\x03\x00ABCD", CTX)


def test_a_non_ascii_model_name_is_refused_rather_than_guessed() -> None:
    payload = bytes(range(0x80, 0x90)) + b"\x49\x4a"
    with pytest.raises(SlmpPayloadShapeError, match="not ASCII"):
        ReadTypeName().decode(payload, CTX)


def test_clear_error_sends_the_detail_page_subcommand() -> None:
    """0000, not the 0001 the FX5 command-list table gives. A-1617-SUBCOMMAND."""
    assert ClearError().subcommand(CTX) == 0x0000
    assert ClearError().payload_len(CTX) == 0


# ======================================================================================
# 1630 / 1631 passwords
# ======================================================================================


def test_an_empty_password_is_refused() -> None:
    with pytest.raises(SlmpConfigurationError, match="nothing to send"):
        UnlockPassword("").validate(CTX)


def test_an_over_long_password_is_refused_rather_than_truncated() -> None:
    with pytest.raises(SlmpConfigurationError, match="at most"):
        LockPassword("a" * 33).validate(CTX)


@pytest.mark.parametrize("password", ["with space", "tab\there", "café", "nul\x00"])
def test_a_password_outside_printable_ascii_is_refused(password: str) -> None:
    with pytest.raises(SlmpConfigurationError, match="printable"):
        UnlockPassword(password).validate(CTX)


def test_a_password_never_appears_in_a_diagnostic() -> None:
    """``describe()`` lands on the request line of every exception."""
    assert "hunter2" not in UnlockPassword("hunter2").describe()
    assert "7 characters" in UnlockPassword("hunter2").describe()


# ======================================================================================
# Decode-side refusals: a short response is never zero-filled
# ======================================================================================


def test_a_short_word_response_is_refused_not_zero_filled() -> None:
    """``pymcprotocol`` turns [111, 222, 333, 444] into [111, 222, 0, 0] here."""
    with pytest.raises(SlmpPayloadShapeError, match="zero-fills"):
        ReadWords("D0", 4).decode(b"\x6f\x00\xde\x00", CTX)


def test_a_long_word_response_is_refused_not_truncated() -> None:
    with pytest.raises(SlmpPayloadShapeError):
        ReadWords("D0", 1).decode(b"\x6f\x00\xde\x00", CTX)


def test_an_empty_response_never_grades_as_a_successful_read() -> None:
    with pytest.raises(SlmpPayloadShapeError):
        ReadWords("D0", 1).decode(b"", CTX)


def test_a_write_that_comes_back_with_data_is_refused() -> None:
    """The measured write response is the 11-byte minimum frame with nothing after it."""
    with pytest.raises(SlmpPayloadShapeError, match="no response data"):
        WriteWords("D0", (1,)).decode(b"\x00\x00", CTX)


def test_a_short_random_response_is_refused() -> None:
    command = ReadRandom((word("D0"), dword("D2")))
    with pytest.raises(SlmpPayloadShapeError):
        command.decode(b"\x01\x00\x02\x00", CTX)


def test_a_short_block_response_is_refused() -> None:
    command = ReadBlocks((BlockSpec("D0", 2), BlockSpec("M0", 1)))
    with pytest.raises(SlmpPayloadShapeError):
        command.decode(b"\x00" * 4, CTX)
