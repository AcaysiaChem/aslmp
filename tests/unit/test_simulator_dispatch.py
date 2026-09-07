"""``aslmp.testing.dispatch`` -- one request in, one end code and some data out.

Pure: no server is started anywhere in this file. Every command the simulator serves is
a function of a decoded request, and that is what makes the whole command surface
testable without a socket.

The decoders here are written independently of ``aslmp.commands``. These tests are
therefore a real cross-check of the request layouts, not a restatement of them: the
request bytes are built by the library's encoder and taken apart by the simulator's own
reader, and the two only agree if the layout is right.
"""

from __future__ import annotations

import dataclasses
import struct

import pytest

from aslmp.commands.registry import COMMANDS
from aslmp.profile import Encoding
from aslmp.testing.dispatch import (
    HANDLERS,
    Dispatcher,
    Reply,
    Silence,
    registered_request_codes,
)
from aslmp.testing.scenario import Scenario, abnormal
from aslmp.testing.targets import FX5U_32MT_DS, PEDANTIC, SimulatorTarget
from aslmp.wire.codec import ASCII, BINARY, Codec, SpecFormat
from aslmp.wire.devicetable import DEVICE_TABLE
from aslmp.wire.frames import THREE_E, request_body
from aslmp.wire.raw import RawRequest
from aslmp.wire.route import Route


def make_request(
    codec: Codec, *, command: int, subcommand: int = 0x0000, payload: bytes = b""
) -> RawRequest:
    """A request frame built by the library and parsed back, as the wire delivers it."""
    body = request_body(
        codec, monitoring_timer=0, command=command, subcommand=subcommand, payload=payload
    )
    raw = THREE_E.build(route=Route.OWN_STATION, body=body, codec=codec)
    return THREE_E.parse_request(raw, codec)


def serve(
    target: SimulatorTarget,
    request: RawRequest,
    *,
    codec: Codec = BINARY,
    encoding: Encoding = Encoding.BINARY,
    dispatcher: Dispatcher | None = None,
) -> Reply | Silence:
    plc = dispatcher or Dispatcher(target=target, memory=target.memory())
    return plc.handle(request, codec=codec, encoding=encoding)


def reply(outcome: Reply | Silence) -> Reply:
    assert isinstance(outcome, Reply), f"expected a reply, got {outcome}"
    return outcome


# --------------------------------------------------------------------------------------
# The table itself
# --------------------------------------------------------------------------------------


def test_every_registered_request_command_has_a_handler() -> None:
    """Adding a command to the library without teaching the simulator fails the build."""
    assert tuple(sorted(HANDLERS)) == registered_request_codes()


def test_the_simulator_serves_nothing_the_library_does_not_implement() -> None:
    """A command only one side knows means one of the two is wrong about the protocol."""
    assert set(HANDLERS) <= set(COMMANDS)


def test_the_ondemand_command_has_no_handler() -> None:
    """0x2101 is PLC-originated. An external device cannot send one, so nothing serves it."""
    assert 0x2101 not in HANDLERS


def test_the_device_code_columns_have_no_duplicates() -> None:
    """The reverse tables are keyed by code; a collision would serve the wrong family."""
    for column in ("code_short", "code_long", "ascii2", "ascii4"):
        values = [getattr(dt, column) for dt in DEVICE_TABLE.values()]
        present = [value for value in values if value]
        assert len(present) == len(set(present)), f"{column} has duplicates"


# --------------------------------------------------------------------------------------
# Diagnostics
# --------------------------------------------------------------------------------------


def test_self_test_echoes_exactly() -> None:
    """0619 is an exact echo of the count and the data; anything else is not our answer."""
    payload = BINARY.number(4, bits=16) + b"ABCD"
    out = reply(serve(FX5U_32MT_DS, make_request(BINARY, command=0x0619, payload=payload)))
    assert out.end_code == 0x0000
    assert out.payload == payload


def test_read_type_name_returns_the_measured_frame() -> None:
    """FX5U-32MT/DS space-padded to 16 characters, then model code 0x4A49 little-endian.

    The bytes are the ones captured from the bench on 2026-09-06.
    """
    out = reply(serve(FX5U_32MT_DS, make_request(BINARY, command=0x0101)))
    assert out.payload == bytes.fromhex("465835552d33324d542f445320202020494a")


def test_an_unknown_command_is_refused() -> None:
    """0x9999 returned 0xC059 with 99 99 echoed in the error information block."""
    out = reply(serve(FX5U_32MT_DS, make_request(BINARY, command=0x9999)))
    assert out.end_code == 0xC059


def test_the_long_device_specification_is_refused_on_an_iq_f() -> None:
    """Subcommand 0x0002 returned 0xC059; on this silicon LTN and LZ are unreachable."""
    out = reply(serve(FX5U_32MT_DS, make_request(BINARY, command=0x0401, subcommand=0x0002)))
    assert out.end_code == 0xC059


def test_the_long_device_specification_is_available_on_the_reference() -> None:
    payload = (
        (100).to_bytes(4, "little") + (0x00A8).to_bytes(2, "little") + BINARY.number(1, bits=16)
    )
    out = reply(
        serve(
            PEDANTIC,
            make_request(BINARY, command=0x0401, subcommand=0x0002, payload=payload),
        )
    )
    assert out.end_code == 0x0000
    assert out.payload == BINARY.words((0,))


def test_an_undocumented_subcommand_bit_is_refused() -> None:
    """A subcommand we do not fully understand is a response layout we do not know."""
    out = reply(serve(FX5U_32MT_DS, make_request(BINARY, command=0x0401, subcommand=0x0040)))
    assert out.end_code == 0xC059


# --------------------------------------------------------------------------------------
# Batch access
# --------------------------------------------------------------------------------------


def _batch_read(codec: Codec, spec: bytes, count: int) -> bytes:
    return spec + codec.number(count, bits=16)


def test_batch_read_returns_the_words_that_are_there() -> None:
    plc = Dispatcher(target=FX5U_32MT_DS, memory=FX5U_32MT_DS.memory())
    plc.memory.write_words("D", 100, (0x1111, 0x2222))
    payload = _batch_read(BINARY, bytes.fromhex("640000A8"), 2)
    out = reply(
        serve(
            FX5U_32MT_DS,
            make_request(BINARY, command=0x0401, payload=payload),
            dispatcher=plc,
        )
    )
    assert out.payload == bytes.fromhex("11112222")


def test_batch_read_in_bit_units_packs_two_points_per_byte() -> None:
    """M100-M107 -> 00 01 00 11: first point in the HIGH nibble (p.40, p.47)."""
    plc = Dispatcher(target=PEDANTIC, memory=PEDANTIC.memory())
    plc.memory.write_bits(
        "M", 100, (False, False, False, True, False, False, True, True)
    )
    payload = _batch_read(BINARY, bytes.fromhex("64000090"), 8)
    out = reply(
        serve(
            PEDANTIC,
            make_request(BINARY, command=0x0401, subcommand=0x0001, payload=payload),
            dispatcher=plc,
        )
    )
    assert out.payload == bytes.fromhex("00010011")


def test_a_zero_point_count_is_a_point_count_error_on_our_silicon() -> None:
    """00 00 00 A8 00 00 returned 0xC052, not the address error the documents predict."""
    payload = _batch_read(BINARY, bytes.fromhex("000000A8"), 0)
    assert reply(
        serve(FX5U_32MT_DS, make_request(BINARY, command=0x0401, payload=payload))
    ).end_code == 0xC052
    assert reply(
        serve(PEDANTIC, make_request(BINARY, command=0x0401, payload=payload))
    ).end_code == 0xC056


def test_one_point_past_the_word_ceiling_is_refused() -> None:
    """960 succeeded and 961 returned 0xC052, binary-searched on hardware."""
    payload = _batch_read(BINARY, bytes.fromhex("000000A8"), 961)
    assert reply(
        serve(FX5U_32MT_DS, make_request(BINARY, command=0x0401, payload=payload))
    ).end_code == 0xC052
    ok = _batch_read(BINARY, bytes.fromhex("000000A8"), 960)
    assert reply(
        serve(FX5U_32MT_DS, make_request(BINARY, command=0x0401, payload=ok))
    ).end_code == 0x0000


def test_the_bit_ceiling_is_half_the_generic_figure() -> None:
    """3584 on our silicon; 3585 -> 0xC051. The SLMP reference says 7168."""
    over = _batch_read(BINARY, bytes.fromhex("00000090"), 3585)
    assert reply(
        serve(
            FX5U_32MT_DS, make_request(BINARY, command=0x0401, subcommand=0x0001, payload=over)
        )
    ).end_code == 0xC051
    assert reply(
        serve(PEDANTIC, make_request(BINARY, command=0x0401, subcommand=0x0001, payload=over))
    ).end_code == 0x0000


def test_the_ascii_ceiling_is_halved_on_an_iq_f() -> None:
    payload = (
        ASCII.device_code(DEVICE_TABLE["D"], SpecFormat.SHORT)
        + ASCII.device_number(0, SpecFormat.SHORT, base=10)
        + ASCII.number(481, bits=16)
    )
    out = reply(
        serve(
            FX5U_32MT_DS,
            make_request(ASCII, command=0x0401, payload=payload),
            codec=ASCII,
            encoding=Encoding.ASCII_XY_HEX,
        )
    )
    assert out.end_code == 0xC052


def test_the_span_is_validated_not_the_start() -> None:
    """D7999 for one point is 0x0000 and for two it is 0xC056. Both measured."""
    head = bytes.fromhex("3F1F00A8")
    assert reply(
        serve(
            FX5U_32MT_DS,
            make_request(BINARY, command=0x0401, payload=_batch_read(BINARY, head, 1)),
        )
    ).end_code == 0x0000
    assert reply(
        serve(
            FX5U_32MT_DS,
            make_request(BINARY, command=0x0401, payload=_batch_read(BINARY, head, 2)),
        )
    ).end_code == 0xC056


def test_an_absent_family_is_0xc05c_on_our_silicon_and_0xc05b_on_the_reference() -> None:
    """ZR0 (device code 0xB0) returned 0xC05C on an FX5U, against the documentation."""
    payload = _batch_read(BINARY, bytes.fromhex("000000B0"), 1)
    assert reply(
        serve(FX5U_32MT_DS, make_request(BINARY, command=0x0401, payload=payload))
    ).end_code == 0xC05C


def test_a_device_code_in_no_table_is_refused() -> None:
    """0x00 and 0xFF both returned 0xC05C on the bench."""
    for code in (0x00, 0xFF):
        payload = _batch_read(BINARY, bytes((0, 0, 0, code)), 1)
        assert reply(
            serve(FX5U_32MT_DS, make_request(BINARY, command=0x0401, payload=payload))
        ).end_code == 0xC05C


def test_bit_units_against_a_word_device_are_refused() -> None:
    payload = _batch_read(BINARY, bytes.fromhex("000000A8"), 1)
    out = reply(
        serve(
            FX5U_32MT_DS,
            make_request(BINARY, command=0x0401, subcommand=0x0001, payload=payload),
        )
    )
    assert out.end_code == FX5U_32MT_DS.end_codes.device_not_allowed_here


def test_batch_write_lands_in_memory() -> None:
    plc = Dispatcher(target=FX5U_32MT_DS, memory=FX5U_32MT_DS.memory())
    payload = _batch_read(BINARY, bytes.fromhex("640000A8"), 2) + BINARY.words((0xAAAA, 0xBBBB))
    out = reply(
        serve(
            FX5U_32MT_DS,
            make_request(BINARY, command=0x1401, payload=payload),
            dispatcher=plc,
        )
    )
    assert out.end_code == 0x0000
    assert out.payload == b""
    assert plc.memory.read_words("D", 100, 2) == (0xAAAA, 0xBBBB)


# --------------------------------------------------------------------------------------
# Request length
# --------------------------------------------------------------------------------------


def test_a_short_request_payload_is_a_length_error() -> None:
    """The point count is missing: 0xC061 on our silicon, 0xC057 on the reference."""
    payload = bytes.fromhex("640000A8")
    assert reply(
        serve(FX5U_32MT_DS, make_request(BINARY, command=0x0401, payload=payload))
    ).end_code == 0xC061
    assert reply(
        serve(PEDANTIC, make_request(BINARY, command=0x0401, payload=payload))
    ).end_code == 0xC057


def test_surplus_request_data_is_a_length_error() -> None:
    """Bytes after the fields a command defines mean the frame is not what it says."""
    payload = _batch_read(BINARY, bytes.fromhex("640000A8"), 1) + b"\x00\x00"
    assert reply(
        serve(FX5U_32MT_DS, make_request(BINARY, command=0x0401, payload=payload))
    ).end_code == 0xC061


# --------------------------------------------------------------------------------------
# Random access
# --------------------------------------------------------------------------------------


def test_read_random_lays_out_words_then_double_words() -> None:
    """The two counts are the only boundary in the response; there is no type tag."""
    plc = Dispatcher(target=FX5U_32MT_DS, memory=FX5U_32MT_DS.memory())
    plc.memory.set_u16("D", 0, 0x1234)
    plc.memory.set_f32("D", 4, 60.0)
    payload = (
        BINARY.number(1, bits=8)
        + BINARY.number(1, bits=8)
        + bytes.fromhex("000000A8")
        + bytes.fromhex("040000A8")
    )
    out = reply(
        serve(
            FX5U_32MT_DS,
            make_request(BINARY, command=0x0403, payload=payload),
            dispatcher=plc,
        )
    )
    assert out.payload == BINARY.number(0x1234, bits=16) + BINARY.number(
        int(struct.unpack("<I", struct.pack("<f", 60.0))[0]), bits=32
    )


def test_a_word_point_on_a_bit_device_is_the_sixteen_bit_window() -> None:
    plc = Dispatcher(target=PEDANTIC, memory=PEDANTIC.memory())
    plc.memory.write_bits("M", 100, (True, False, True))
    payload = BINARY.number(1, bits=8) + BINARY.number(0, bits=8) + bytes.fromhex("64000090")
    out = reply(
        serve(PEDANTIC, make_request(BINARY, command=0x0403, payload=payload), dispatcher=plc)
    )
    assert out.payload == BINARY.number(0b101, bits=16)


def test_one_point_past_the_random_ceiling_is_refused() -> None:
    """192 succeeded, 193 returned 0xC054."""
    spec = bytes.fromhex("000000A8")
    payload = BINARY.number(193, bits=8) + BINARY.number(0, bits=8) + spec * 193
    assert reply(
        serve(FX5U_32MT_DS, make_request(BINARY, command=0x0403, payload=payload))
    ).end_code == 0xC054


def test_our_silicon_accepts_a_ts_point_its_own_manual_forbids() -> None:
    """The load-bearing one. TS0 in a 0403 returned 0x0000 with data on the bench.

    The client refuses this before a byte is built. That refusal is only worth anything
    if the PLC would have said yes -- so the simulator says yes, and the reference
    target says no.
    """
    payload = BINARY.number(1, bits=8) + BINARY.number(0, bits=8) + bytes.fromhex("000000C1")
    assert reply(
        serve(FX5U_32MT_DS, make_request(BINARY, command=0x0403, payload=payload))
    ).end_code == 0x0000
    assert reply(
        serve(PEDANTIC, make_request(BINARY, command=0x0403, payload=payload))
    ).end_code == 0x4032


def test_write_random_puts_the_low_word_first() -> None:
    """1234.5 as one double-word point: D104 = 0x5000, D105 = 0x449A, measured."""
    plc = Dispatcher(target=FX5U_32MT_DS, memory=FX5U_32MT_DS.memory())
    raw = int(struct.unpack("<I", struct.pack("<f", 1234.5))[0])
    payload = (
        BINARY.number(0, bits=8)
        + BINARY.number(1, bits=8)
        + bytes.fromhex("680000A8")
        + BINARY.number(raw, bits=32)
    )
    out = reply(
        serve(
            FX5U_32MT_DS,
            make_request(BINARY, command=0x1402, payload=payload),
            dispatcher=plc,
        )
    )
    assert out.end_code == 0x0000
    assert plc.memory.read_words("D", 104, 2) == (0x5000, 0x449A)


def test_write_random_in_bit_units_drives_single_bits() -> None:
    plc = Dispatcher(target=PEDANTIC, memory=PEDANTIC.memory())
    payload = (
        BINARY.number(2, bits=8)
        + bytes.fromhex("32000090")
        + BINARY.number(0, bits=8)
        + bytes.fromhex("33000090")
        + BINARY.number(1, bits=8)
    )
    out = reply(
        serve(
            PEDANTIC,
            make_request(BINARY, command=0x1402, subcommand=0x0001, payload=payload),
            dispatcher=plc,
        )
    )
    assert out.end_code == 0x0000
    assert plc.memory.read_bits("M", 50, 2) == (False, True)


# --------------------------------------------------------------------------------------
# Block access
# --------------------------------------------------------------------------------------


def test_block_read_returns_each_block_in_wire_order() -> None:
    plc = Dispatcher(target=PEDANTIC, memory=PEDANTIC.memory())
    plc.memory.write_words("D", 0, (1, 2))
    plc.memory.write_bits("M", 0, (True,))
    payload = (
        BINARY.number(1, bits=8)
        + BINARY.number(1, bits=8)
        + bytes.fromhex("000000A8")
        + BINARY.number(2, bits=16)
        + bytes.fromhex("00000090")
        + BINARY.number(1, bits=16)
    )
    out = reply(
        serve(PEDANTIC, make_request(BINARY, command=0x0406, payload=payload), dispatcher=plc)
    )
    assert out.payload == BINARY.words((1, 2, 1))


def test_block_write_lands_in_memory() -> None:
    plc = Dispatcher(target=PEDANTIC, memory=PEDANTIC.memory())
    payload = (
        BINARY.number(1, bits=8)
        + BINARY.number(0, bits=8)
        + bytes.fromhex("000000A8")
        + BINARY.number(2, bits=16)
        + BINARY.words((7, 8))
    )
    out = reply(
        serve(PEDANTIC, make_request(BINARY, command=0x1406, payload=payload), dispatcher=plc)
    )
    assert out.end_code == 0x0000
    assert plc.memory.read_words("D", 0, 2) == (7, 8)


# --------------------------------------------------------------------------------------
# Monitoring
# --------------------------------------------------------------------------------------


def test_monitoring_is_0xc059_on_an_iq_f_not_0xc05d() -> None:
    """The measured refusal, and the reason there is no 0403 substitution anywhere."""
    payload = BINARY.number(1, bits=8) + BINARY.number(0, bits=8) + bytes.fromhex("000000A8")
    assert reply(
        serve(FX5U_32MT_DS, make_request(BINARY, command=0x0801, payload=payload))
    ).end_code == 0xC059
    assert reply(
        serve(FX5U_32MT_DS, make_request(BINARY, command=0x0802))
    ).end_code == 0xC059


def test_execute_monitor_without_registration_is_0xc05d_on_the_reference() -> None:
    assert reply(serve(PEDANTIC, make_request(BINARY, command=0x0802))).end_code == 0xC05D


def test_a_registration_survives_until_the_next_one_replaces_it() -> None:
    """SLMP issues no handle, so a second client's list clobbers the first one's."""
    plc = Dispatcher(target=PEDANTIC, memory=PEDANTIC.memory())
    plc.memory.set_u16("D", 0, 0x4321)
    register = BINARY.number(1, bits=8) + BINARY.number(0, bits=8) + bytes.fromhex("000000A8")
    assert reply(
        serve(PEDANTIC, make_request(BINARY, command=0x0801, payload=register), dispatcher=plc)
    ).end_code == 0x0000
    out = reply(serve(PEDANTIC, make_request(BINARY, command=0x0802), dispatcher=plc))
    assert out.payload == BINARY.number(0x4321, bits=16)


# --------------------------------------------------------------------------------------
# Remote control and passwords
# --------------------------------------------------------------------------------------


def test_remote_reset_answers_with_silence_and_a_teardown() -> None:
    """SH(NA)-080956ENG-M p.136: on success the response is not sent back."""
    payload = BINARY.number(0x0000, bits=16)
    outcome = serve(FX5U_32MT_DS, make_request(BINARY, command=0x1006, payload=payload))
    assert isinstance(outcome, Silence)
    assert outcome.close_after is True


def test_the_remote_fixed_field_is_checked_against_the_family() -> None:
    """An iQ-F writes 00 00 where the SLMP reference families write 01 00."""
    iq_f = BINARY.number(0x0000, bits=16)
    assert reply(
        serve(FX5U_32MT_DS, make_request(BINARY, command=0x1002, payload=iq_f))
    ).end_code == 0x0000
    assert reply(
        serve(PEDANTIC, make_request(BINARY, command=0x1002, payload=iq_f))
    ).end_code == 0xC059


def test_remote_run_refuses_a_clear_mode_an_iq_f_does_not_have() -> None:
    payload = BINARY.number(1, bits=16) + BINARY.number(2, bits=8) + BINARY.number(0, bits=8)
    assert reply(
        serve(FX5U_32MT_DS, make_request(BINARY, command=0x1001, payload=payload))
    ).end_code == 0xC059


def test_remote_run_can_be_made_to_lie() -> None:
    """Remote RUN with the switch in STOP completes normally and does not run."""
    lying = dataclasses.replace(
        FX5U_32MT_DS, pathology=FX5U_32MT_DS.pathology.replace(remote_run_lies=True)
    )
    plc = Dispatcher(target=lying, memory=lying.memory())
    plc.state.run_state = 0x0002
    payload = BINARY.number(1, bits=16) + BINARY.number(0, bits=8) + BINARY.number(0, bits=8)
    assert reply(
        serve(lying, make_request(BINARY, command=0x1001, payload=payload), dispatcher=plc)
    ).end_code == 0x0000
    assert plc.state.run_state == 0x0002


def test_a_wrong_password_is_refused() -> None:
    payload = BINARY.number(4, bits=16) + b"1234"
    assert reply(
        serve(FX5U_32MT_DS, make_request(BINARY, command=0x1630, payload=payload))
    ).end_code == 0xC200


def test_clear_error_answers_with_nothing_after_the_end_code() -> None:
    out = reply(serve(FX5U_32MT_DS, make_request(BINARY, command=0x1617)))
    assert (out.end_code, out.payload) == (0x0000, b"")


# --------------------------------------------------------------------------------------
# ASCII, and the X/Y radix that is the largest silent-wrong-register hazard
# --------------------------------------------------------------------------------------


def test_ascii_x_y_device_numbers_are_octal_only_under_the_octal_setting() -> None:
    """Y45 on an iQ-F is output 37. Under ASCII (X, Y OCT) that is the digits '000045'.

    Sending the digits as written is how ``Y10`` lands on the 11th output with end code
    0x0000 and no error anywhere (measured on FX5U-32MT/DS fw 1.065, 2026-09-07). The
    simulator decides the base from the **target**, not from the client's profile, so
    the client's rule is actually under test here.
    """
    plc = Dispatcher(target=FX5U_32MT_DS, memory=FX5U_32MT_DS.memory())
    plc.memory.write_bits("Y", 37, (True,))
    payload = b"Y*" + b"000045" + ASCII.number(1, bits=16)
    out = reply(
        serve(
            FX5U_32MT_DS,
            make_request(ASCII, command=0x0401, subcommand=0x0001, payload=payload),
            codec=ASCII,
            encoding=Encoding.ASCII_XY_OCT,
            dispatcher=plc,
        )
    )
    assert out.payload == ASCII.bits((True,))

    hex_read = reply(
        serve(
            FX5U_32MT_DS,
            make_request(ASCII, command=0x0401, subcommand=0x0001, payload=payload),
            codec=ASCII,
            encoding=Encoding.ASCII_XY_HEX,
            dispatcher=plc,
        )
    )
    assert hex_read.payload == ASCII.bits((False,)), "0x45 is 69, and Y69 is not set"


def test_an_octal_digit_out_of_range_reaches_a_different_output() -> None:
    """The CPU does not validate octal legality; wire number 8 was ACCEPTED.

    Rejecting the digits 8 and 9 in an X/Y literal is the client's job and nothing else
    will do it. This test is what says so: the simulator serves ``Y8`` happily.
    """
    plc = Dispatcher(target=FX5U_32MT_DS, memory=FX5U_32MT_DS.memory())
    plc.memory.write_bits("Y", 8, (True,))
    payload = BINARY.number(8, bits=16) + b"\x00" + b"\x9d" + BINARY.number(1, bits=16)
    out = reply(
        serve(
            FX5U_32MT_DS,
            make_request(BINARY, command=0x0401, subcommand=0x0001, payload=payload),
            dispatcher=plc,
        )
    )
    assert out.end_code == 0x0000
    assert out.payload == BINARY.bits((True,))


def test_an_ascii_device_number_with_a_bad_digit_is_refused_not_read_as_zero() -> None:
    """libslmp2's wordcodec.c:27 reads a bad nibble as 0 and invents a plausible value."""
    payload = b"D*" + b"00X100" + ASCII.number(1, bits=16)
    out = reply(
        serve(
            FX5U_32MT_DS,
            make_request(ASCII, command=0x0401, payload=payload),
            codec=ASCII,
            encoding=Encoding.ASCII_XY_HEX,
        )
    )
    assert out.end_code == FX5U_32MT_DS.end_codes.unknown_device_code


def test_an_ascii_mnemonic_in_no_table_is_refused() -> None:
    payload = b"ZZ" + b"000000" + ASCII.number(1, bits=16)
    out = reply(
        serve(
            FX5U_32MT_DS,
            make_request(ASCII, command=0x0401, payload=payload),
            codec=ASCII,
            encoding=Encoding.ASCII_XY_HEX,
        )
    )
    assert out.end_code == FX5U_32MT_DS.end_codes.unknown_device_code


# --------------------------------------------------------------------------------------
# Scenarios
# --------------------------------------------------------------------------------------


def test_a_scenario_takes_precedence_and_is_then_spent() -> None:
    """Transient failures need a script, not a switch left on."""
    script = Scenario((abnormal(0xC056, command=0x0401),))
    plc = Dispatcher(
        target=FX5U_32MT_DS, memory=FX5U_32MT_DS.memory(), scenario=script
    )
    payload = _batch_read(BINARY, bytes.fromhex("000000A8"), 1)
    first = reply(
        serve(
            FX5U_32MT_DS,
            make_request(BINARY, command=0x0401, payload=payload),
            dispatcher=plc,
        )
    )
    second = reply(
        serve(
            FX5U_32MT_DS,
            make_request(BINARY, command=0x0401, payload=payload),
            dispatcher=plc,
        )
    )
    assert (first.end_code, second.end_code) == (0xC056, 0x0000)
    assert script.exhausted


def test_the_scan_counter_advances() -> None:
    plc = Dispatcher(target=FX5U_32MT_DS, memory=FX5U_32MT_DS.memory())
    assert plc.advance_scan() == 1
    assert plc.advance_scan() == 2
    assert plc.memory.get_u32("D", 8) == 2


def test_a_reply_cannot_carry_both_an_end_code_and_data() -> None:
    """An abnormal response is the error information block and nothing else (p.28)."""
    with pytest.raises(ValueError, match="abnormal"):
        Reply(0xC056, b"\x00\x00")
