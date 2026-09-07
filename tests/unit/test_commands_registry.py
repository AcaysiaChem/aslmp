"""The command catalogue, and the ``2101`` that must never be parsed as a response.

The registry is the one table the client, the conformance simulator, the pass-through
proxy and ``aslmp cite`` share. Its value is that it cannot disagree with the classes:
each row gathers its citations off the classes it lists and validates that they declare
the code it is keyed by and agree with it about ``mutates``.

The Ondemand tests are here because ``2101`` is registered and implemented by no class.
It is the message that travels the wrong way, and a receive path that treats "the next
thing that arrives" as its response is desynchronised by one of them for the life of the
connection: every later read returns the previous read's data, with end code ``0x0000``
and nothing to say so.
"""

from __future__ import annotations

import pytest

from aslmp.commands import (
    COMMANDS,
    ONDEMAND_COMMAND,
    OnDemandMessage,
    by_code,
    codes,
    is_ondemand,
    parse_ondemand,
    refuse_ondemand_as_response,
)
from aslmp.commands.ondemand import ONDEMAND_MAX_BYTES
from aslmp.errors import SlmpUnsolicitedFrameError
from aslmp.wire.codec import ASCII, BINARY
from aslmp.wire.frames import FOUR_E, THREE_E, FrameFormat, request_body
from aslmp.wire.raw import RawRequest
from aslmp.wire.raw import SlmpUnsolicitedFrameError as WireUnsolicited
from aslmp.wire.route import Route

DESIGN_COMMANDS = {
    0x0101,
    0x0401,
    0x0403,
    0x0406,
    0x0619,
    0x0801,
    0x0802,
    0x1001,
    0x1002,
    0x1003,
    0x1005,
    0x1006,
    0x1401,
    0x1402,
    0x1406,
    0x1617,
    0x1630,
    0x1631,
    0x2101,
}
"""Exactly the commands DESIGN section 1.5 asks build unit U7 to cover."""


def test_the_registry_covers_the_designed_command_set_exactly() -> None:
    assert set(COMMANDS) == DESIGN_COMMANDS


def test_codes_are_ascending_and_match_the_table() -> None:
    assert codes() == tuple(sorted(COMMANDS))


def test_by_code_raises_for_an_unimplemented_command_and_names_the_alternative() -> None:
    with pytest.raises(KeyError) as caught:
        by_code(0x9999)
    assert "raw_command" in str(caught.value)


def test_by_code_refuses_a_non_integer() -> None:
    with pytest.raises(TypeError):
        by_code("0x0403")  # type: ignore[arg-type]


def test_every_row_agrees_with_its_classes_about_the_command_code() -> None:
    for code, spec in COMMANDS.items():
        for command in spec.commands:
            assert code == command.CODE


def test_every_row_agrees_with_its_classes_about_mutates() -> None:
    """The flag drives the not-sent / outcome-unknown split; two statements of it must
    not drift, so the registry asserts them equal at import."""
    for spec in COMMANDS.values():
        for command in spec.commands:
            assert command.mutates == spec.mutates


def test_the_read_commands_do_not_mutate_and_the_write_commands_do() -> None:
    reads = {0x0101, 0x0401, 0x0403, 0x0406, 0x0619, 0x0802, 0x2101}
    for code, spec in COMMANDS.items():
        assert spec.mutates is (code not in reads), f"0x{code:04X}"


def test_monitor_registration_mutates_even_though_no_device_changes() -> None:
    """It replaces the registration for every client talking to that CPU, and a
    mid-flight failure leaves the outcome genuinely unknown."""
    assert COMMANDS[0x0801].mutates is True
    assert COMMANDS[0x0802].mutates is False


def test_ondemand_is_registered_with_no_class_because_we_cannot_send_it() -> None:
    spec = COMMANDS[ONDEMAND_COMMAND]
    assert spec.direction == "ondemand"
    assert spec.commands == ()
    assert spec.cites


def test_every_row_carries_a_citation() -> None:
    for spec in COMMANDS.values():
        assert spec.cites, f"{spec} carries no citation"


def test_the_ambiguities_a_row_carries_are_the_ones_its_classes_declare() -> None:
    keys = {
        code: {a.key for a in spec.ambiguities} for code, spec in COMMANDS.items()
    }
    assert keys[0x1002] == {"A-REMOTE-FIXED"}
    assert keys[0x1005] == {"A-REMOTE-FIXED"}
    assert keys[0x1006] == {"A-REMOTE-FIXED"}
    assert keys[0x1001] == {"A-CLEAR-MODE"}
    assert keys[0x0619] == {"A-LOOPBACK-CHARSET"}
    assert keys[0x1617] == {"A-1617-SUBCOMMAND"}


def test_every_ambiguity_a_command_declares_is_in_the_shipped_table() -> None:
    """``aslmp ambiguities`` prints one table; a key only a docstring knows is a comment."""
    import csv
    from pathlib import Path

    data = Path("src/aslmp/data/ambiguities.tsv")
    lines = [
        line
        for line in data.read_text(encoding="utf-8").splitlines()
        if not line.startswith("#")
    ]
    known = {row["key"] for row in csv.DictReader(lines, delimiter="\t")}
    for spec in COMMANDS.values():
        for ambiguity in spec.ambiguities:
            assert ambiguity.key in known, (
                f"{spec} declares ambiguity {ambiguity.key}, which is not a row of "
                f"data/ambiguities.tsv"
            )


def test_a_registry_row_renders_as_the_command_a_person_would_look_up() -> None:
    assert str(COMMANDS[0x0403]) == "0x0403 Device Read Random"


# ======================================================================================
# 2101 Ondemand
# ======================================================================================


def ondemand_request(payload: bytes = b"hello") -> RawRequest:
    body = request_body(
        BINARY,
        monitoring_timer=0x0000,
        command=ONDEMAND_COMMAND,
        subcommand=0x0000,
        payload=payload,
    )
    built = THREE_E.build(route=Route.OWN_STATION, body=body, codec=BINARY)
    return THREE_E.parse_request(built, BINARY)


def test_is_ondemand_recognises_the_command() -> None:
    assert is_ondemand(ONDEMAND_COMMAND)
    assert not is_ondemand(0x0403)


def test_an_ondemand_frame_parses_into_a_typed_record() -> None:
    message = parse_ondemand(ondemand_request(b"hello"), BINARY)
    assert isinstance(message, OnDemandMessage)
    assert message.data == b"hello"
    assert message.subcommand == 0x0000
    assert "ondemand" in str(message)


def test_parse_ondemand_refuses_a_frame_that_is_not_one() -> None:
    body = request_body(
        BINARY, monitoring_timer=0, command=0x0401, subcommand=0, payload=b"\x00" * 6
    )
    built = THREE_E.build(route=Route.OWN_STATION, body=body, codec=BINARY)
    with pytest.raises(SlmpUnsolicitedFrameError, match="not Ondemand"):
        parse_ondemand(THREE_E.parse_request(built, BINARY), BINARY)


def test_an_undocumented_ondemand_subcommand_is_refused_not_masked() -> None:
    body = request_body(
        BINARY,
        monitoring_timer=0,
        command=ONDEMAND_COMMAND,
        subcommand=0x0001,
        payload=b"",
    )
    built = THREE_E.build(route=Route.OWN_STATION, body=body, codec=BINARY)
    with pytest.raises(SlmpUnsolicitedFrameError, match="0x0000"):
        parse_ondemand(THREE_E.parse_request(built, BINARY), BINARY)


def test_an_oversized_ondemand_is_refused() -> None:
    with pytest.raises(SlmpUnsolicitedFrameError, match="maximum"):
        parse_ondemand(ondemand_request(b"x" * (ONDEMAND_MAX_BYTES + 1)), BINARY)


def test_the_ascii_ondemand_limit_is_in_characters_not_bytes() -> None:
    """1920 bytes is 3840 characters, and the check must not halve the ASCII allowance."""
    message = parse_ondemand(ondemand_request(b"a" * ONDEMAND_MAX_BYTES), ASCII)
    assert len(message.data) == ONDEMAND_MAX_BYTES


def test_the_refusal_says_the_stream_is_not_resynchronised() -> None:
    error = refuse_ondemand_as_response(ondemand_request())
    text = str(error)
    assert "NOT resynchronised" in text
    assert "0x2101" in text


@pytest.mark.parametrize("fmt", [THREE_E, FOUR_E], ids=["3E", "4E"])
def test_a_request_subheader_on_a_response_path_is_refused_by_the_frame_layer(
    fmt: FrameFormat,
) -> None:
    """The wire layer already refuses it; this asserts the two halves agree."""
    serial = 0x0001 if fmt.carries_serial else None
    body = request_body(
        BINARY,
        monitoring_timer=0,
        command=ONDEMAND_COMMAND,
        subcommand=0,
        payload=b"",
    )
    built = fmt.build(
        route=Route.OWN_STATION, body=body, codec=BINARY, serial=serial
    )
    with pytest.raises(WireUnsolicited):
        fmt.parse(built, BINARY)
