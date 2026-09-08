"""No simulator switch may be unexercised, and none may be exercised by name only.

Layer 2.5. This file is the enforcement test for the second family of the same bug
``tests/unit/test_simulator_state.py`` closed for :class:`SessionState`: a knob that is
declared, documented, listed in the comparison table, and that changes **nothing a
client can observe**.

.. rubric:: What went wrong, again

``remote_run_lies`` is the pathology whose stated purpose is to give ``verify=True``
something real to catch. Three handlers consult it -- ``1001`` Remote RUN, ``1002``
Remote STOP and ``1003`` Remote PAUSE -- and exactly one of them was ever tested.
Deleting the early return from ``_handle_remote_run`` **or** from ``_handle_remote_pause``
left the whole suite green on 2026-09-07, so two thirds of the switch was decoration.

``SimulatorTarget.four_e_frames`` was worse: declared on all three targets, printed by
:func:`~aslmp.testing.targets.diff_targets`, and read by no handler and no socket at all.
Setting it ``False`` changed nothing. It is wired to
:meth:`~aslmp.testing.targets.SimulatorTarget.serves_frame` now, and this file is what
says a capability flag has to decide something.

.. rubric:: The rule, and why it is shaped like this

Three tests, and each closes a different way of getting this wrong.

1. **Discovery.** :func:`switch_consumers` parses ``dispatch.py``, ``server.py`` and
   ``targets.py`` and returns every ``(switch, function)`` pair where a pathology switch
   is read. Nothing is hand-listed, so a switch that grows a fourth consumer tomorrow
   appears here the moment it is written, and
   :func:`test_every_consumer_of_every_switch_has_a_probe` fails until somebody
   exercises it. This is the half three hand-written remote-command tests could never
   have.
2. **Behaviour.** Every pair has a probe, and a probe is a fixed exchange with a
   simulated CPU. It is run twice, on two values of that one switch, and what is
   compared is only what came back on the wire. A probe that stops distinguishing means
   the switch went dead.
3. **Aim.** A probe filed under ``("remote_run_lies", "_handle_remote_pause")`` that
   quietly sends ``1002`` would pass (2) while leaving ``1003`` exactly as untested as
   it was. So :func:`test_each_probe_enters_the_consumer_it_is_filed_under` profiles the
   run and asserts the named function was actually entered. Without it this file would
   be the same bug wearing a table.

.. rubric:: Watched failing on 2026-09-07

* Deleting ``if ctx.pathology.remote_run_lies: return Reply(0x0000)`` from
  ``_handle_remote_pause`` fails
  ``test_each_switch_consumer_changes_what_a_client_sees[remote_run_lies-_handle_remote_pause]``
  and nothing else -- which is precisely the mutation that used to leave the suite green.
  The same deletion in ``_handle_remote_run`` and in ``_handle_remote_stop`` fails their
  own rows.
* Re-filing the ``_handle_remote_pause`` probe's request as ``1002`` keeps (2) green and
  fails (3) by name.
* Reverting ``serves_frame`` to ``return True`` fails
  ``test_each_capability_flag_changes_what_a_client_sees[four_e_frames]``.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import dataclasses
import itertools
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from types import CodeType, FunctionType
from typing import TYPE_CHECKING, Any, Final

import pytest

from aslmp.profile import Encoding
from aslmp.testing import dispatch as dispatch_module
from aslmp.testing import server as server_module
from aslmp.testing import targets as targets_module
from aslmp.testing.dispatch import CpuRunState, Dispatcher, SessionState, Silence
from aslmp.testing.pathology import PATHOLOGY_SOURCES, Pathology
from aslmp.testing.server import BENCH_ENTRIES, PlcSimulator
from aslmp.testing.targets import FX5U_32MT_DS, PEDANTIC, SimulatorTarget
from aslmp.wire.codec import ASCII, BINARY, Codec, SpecFormat
from aslmp.wire.devicetable import DEVICE_TABLE
from aslmp.wire.frames import FOUR_E, THREE_E, FrameFormat, request_body
from aslmp.wire.route import Route

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import AsyncIterator, Awaitable, Callable, Sequence

    from aslmp.wire.raw import RawRequest


TCP: Final = "tcp"
TCP_4E: Final = "tcp-4e"
UDP: Final = "udp"

Observation = tuple[Any, ...]
"""What a client saw. Bytes off a socket, or end codes and response data. Nothing else.

Deliberately not a structured type: a probe compares two runs of itself, so the only
thing that matters is that the value is comparable and that it was obtained the way a
client obtains things.
"""


# --------------------------------------------------------------------------------------
# Discovery: who reads which switch, found in the source rather than remembered
# --------------------------------------------------------------------------------------

CONSUMER_MODULES: Final = (dispatch_module, server_module, targets_module)
"""Every module of ``aslmp.testing`` that may consult a board.

``pathology.py`` itself is excluded: it *is* the board, and its own ``active`` and
``cites`` read every field by name through :func:`dataclasses.fields`, which would make
the discovery below unanimous and useless.
"""

SWITCHES: Final[frozenset[str]] = frozenset(
    f.name for f in dataclasses.fields(Pathology) if f.name != "sources"
)
"""Every field of :class:`~aslmp.testing.pathology.Pathology` that decides behaviour.

``sources`` is the one field that is provenance rather than a switch -- extra citations a
caller carries alongside a custom board. It is read by ``Pathology.cites`` and
:func:`test_the_one_pathology_field_that_is_not_a_switch_is_still_read` is what keeps
that from becoming the exemption it looks like.
"""

_BOARD_NAMES: Final = frozenset({"pathology", "board"})
_BOARD_ATTRS: Final = frozenset({"pathology", "_pathology", "board"})


def _is_board(node: ast.expr) -> bool:
    """Whether ``node`` is how this package spells "the pathology board in force"."""
    if isinstance(node, ast.Name):
        return node.id in _BOARD_NAMES
    if isinstance(node, ast.Attribute):
        return node.attr in _BOARD_ATTRS
    return False


def _collect(node: ast.AST, enclosing: str, out: set[tuple[str, str]]) -> None:
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
            _collect(child, child.name, out)
            continue
        if (
            isinstance(child, ast.Attribute)
            and child.attr in SWITCHES
            and _is_board(child.value)
            and enclosing
        ):
            out.add((child.attr, enclosing))
        _collect(child, enclosing, out)


def switch_consumers() -> frozenset[tuple[str, str]]:
    """Every ``(switch, function)`` pair in the package, read out of the source.

    Static rather than dynamic on purpose: a consumer that no test reaches would never
    appear in a coverage-based answer, and a consumer no test reaches is the entire
    subject of this file.
    """
    out: set[tuple[str, str]] = set()
    for module in CONSUMER_MODULES:
        assert module.__file__ is not None
        source = Path(module.__file__).read_text(encoding="utf-8")
        _collect(ast.parse(source), "", out)
    return frozenset(out)


def code_of(name: str) -> CodeType:
    """The code object of the consumer called ``name``, module function or method."""
    for module in CONSUMER_MODULES:
        found = vars(module).get(name)
        if isinstance(found, FunctionType):
            return found.__code__
        for value in vars(module).values():
            if isinstance(value, type):
                member = value.__dict__.get(name)
                if isinstance(member, FunctionType):
                    return member.__code__
    known = [m.__name__ for m in CONSUMER_MODULES]
    raise AssertionError(f"no function named {name} in {known}")


# --------------------------------------------------------------------------------------
# Requests, and the only kind of observation this file admits
# --------------------------------------------------------------------------------------


def word_spec(device: str, index: int) -> bytes:
    """A binary short-format device specification: three number units, then the code."""
    code = DEVICE_TABLE[device].code_short
    assert code is not None, f"{device} has no short device code"
    return index.to_bytes(3, "little") + bytes((code,))


def long_spec(device: str, index: int) -> bytes:
    """The same device in the long specification, whose widths the codec states."""
    number = index.to_bytes(BINARY.device_number_len(SpecFormat.LONG), "little")
    code = DEVICE_TABLE[device].code_long
    return number + code.to_bytes(BINARY.device_code_len(SpecFormat.LONG), "little")


def make_request(
    *, command: int, subcommand: int = 0x0000, payload: bytes = b"", codec: Codec = BINARY
) -> RawRequest:
    """A request frame built by the library and parsed back, as the wire delivers it."""
    body = request_body(
        codec, monitoring_timer=0, command=command, subcommand=subcommand, payload=payload
    )
    raw = THREE_E.build(route=Route.OWN_STATION, body=body, codec=codec)
    return THREE_E.parse_request(raw, codec)


def batch_read(device: str, index: int, count: int = 1) -> RawRequest:
    """``0401``, one contiguous run of words."""
    return make_request(
        command=0x0401, payload=word_spec(device, index) + BINARY.number(count, bits=16)
    )


READ_SD203: Final = batch_read("SD", 203)
"""The one request a client has for "what state is this CPU in"."""

REMOTE_RUN: Final = make_request(
    command=0x1001,
    payload=BINARY.number(1, bits=16) + BINARY.number(0, bits=8) + BINARY.number(0, bits=8),
)
REMOTE_STOP_FX5U: Final = make_request(command=0x1002, payload=BINARY.number(0x0000, bits=16))
REMOTE_PAUSE: Final = make_request(command=0x1003, payload=BINARY.number(1, bits=16))
REMOTE_RESET_FX5U: Final = make_request(command=0x1006, payload=BINARY.number(0x0000, bits=16))

READ_RANDOM_TS0: Final = make_request(
    command=0x0403,
    payload=BINARY.number(1, bits=8) + BINARY.number(0, bits=8) + word_spec("TS", 0),
)
"""``0403`` with one word access point at ``TS0``: what JY997D56001-K p.78 forbids and
what the CPU answered ``0x0000`` with data (measured 2026-09-06)."""


def seen(outcome: object) -> tuple[int, bytes]:
    """One outcome as a client sees it. A silence is an observation too."""
    if isinstance(outcome, Silence):
        return (-1, outcome.reason.encode())
    assert hasattr(outcome, "end_code")
    return (outcome.end_code, outcome.payload)  # type: ignore[attr-defined]


def read_words_frame(
    *,
    device: bytes = b"\x00\x00\x00\xa8",
    count: int = 1,
    frame: FrameFormat = THREE_E,
    codec: Codec = BINARY,
    serial: int | None = None,
) -> bytes:
    """A ``0401`` Device Read request, ready to put on a socket."""
    payload = device + codec.number(count, bits=16)
    body = request_body(
        codec, monitoring_timer=0, command=0x0401, subcommand=0x0000, payload=payload
    )
    return frame.build(route=Route.OWN_STATION, body=body, codec=codec, serial=serial)


def overstated_frame() -> bytes:
    """A ``0401`` whose declared ``L`` is two units more than the bytes that follow."""
    raw = bytearray(read_words_frame())
    at = THREE_E.subheader_units(BINARY) + Route.wire_len(BINARY)
    raw[at : at + 2] = BINARY.number(len(raw) - THREE_E.prefix_units(BINARY) + 2, bits=16)
    return bytes(raw)


async def drain(reader: asyncio.StreamReader, timeout: float = 0.3) -> bytes:
    """Every byte that arrives within ``timeout``; ``b""`` is the observation "silence".

    Length-agnostic on purpose. Several probes are about a response that never comes, or
    about how many responses come, or about the shape of a frame that this file is not
    otherwise entitled to parse, and one raw byte string answers all three.

    A peer that closed reads as ``b""`` on POSIX and raises ``ConnectionResetError`` on
    Windows' proactor loop; both mean "nothing more is coming", and the accept-then-FIN
    of ``single_connection`` is the probe that meets it.
    """
    out = bytearray()
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return bytes(out)
        try:
            chunk = await asyncio.wait_for(reader.read(65536), remaining)
        except (TimeoutError, ConnectionResetError):
            return bytes(out)
        if not chunk:
            return bytes(out)
        out += chunk


class DatagramClient(asyncio.DatagramProtocol):
    """Counts what came back, which is all a UDP probe ever needs."""

    def __init__(self) -> None:
        self.received: list[bytes] = []

    def datagram_received(self, data: bytes, addr: object) -> None:
        del addr
        self.received.append(data)


@contextlib.asynccontextmanager
async def simulator(
    board: Pathology, *, target: SimulatorTarget = FX5U_32MT_DS
) -> AsyncIterator[PlcSimulator]:
    plc = PlcSimulator(target=target, pathology=board, entries=BENCH_ENTRIES)
    await plc.start()
    try:
        yield plc
    finally:
        await plc.aclose()


def tx_lengths(plc: PlcSimulator) -> tuple[int, ...]:
    return tuple(len(r.data) for r in plc.transcript if r.direction == "tx")


def tx_gaps_over_50ms(plc: PlcSimulator) -> tuple[bool, ...]:
    stamps = [r.at for r in plc.transcript if r.direction == "tx"]
    return tuple(
        (later - earlier) > 50_000_000
        for earlier, later in itertools.pairwise(stamps)
    )


# --------------------------------------------------------------------------------------
# One probe per (switch, consumer) pair
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SwitchProbe:
    """Two values of one switch, and the exchange that tells them apart.

    ``run`` is handed a whole board rather than the switch, so it cannot read the switch
    it is about: what it returns has to have come off a socket or out of a response.
    """

    values: tuple[Any, Any]
    run: Callable[[Pathology], Awaitable[Observation]]
    why: str
    base: Pathology = dataclasses.field(default_factory=Pathology)
    """The board the two values are set on. Not always clean: ``segment_gap_s`` is only
    observable when something is being segmented, and ``udp_service_delay_s`` only when
    a depth limit exists for the queue to build against."""

    def board(self, value: Any, switch: str) -> Pathology:
        return self.base.replace(**{switch: value})


def dispatched(
    requests: Sequence[RawRequest],
    *,
    target: SimulatorTarget = FX5U_32MT_DS,
    codec: Codec = BINARY,
    encoding: Encoding = Encoding.BINARY,
    state: Callable[[], SessionState] = SessionState,
) -> Callable[[Pathology], Awaitable[Observation]]:
    """A probe that serves ``requests`` through a dispatcher and returns the answers."""

    async def run(board: Pathology) -> Observation:
        plc = Dispatcher(
            target=target, memory=target.memory(), state=state(), pathology=board
        )
        return tuple(
            seen(plc.handle(request, codec=codec, encoding=encoding)) for request in requests
        )

    return run


async def probe_accept_4e_on_3e(board: Pathology) -> Observation:
    async with simulator(board) as plc:
        reader, writer = await asyncio.open_connection(*plc.address(TCP))
        writer.write(read_words_frame(frame=FOUR_E, serial=0x1234))
        await writer.drain()
        answer = await drain(reader)
        writer.close()
    return (answer,)


async def probe_wrong_serial_echo(board: Pathology) -> Observation:
    async with simulator(board) as plc:
        reader, writer = await asyncio.open_connection(*plc.address(TCP_4E))
        writer.write(read_words_frame(frame=FOUR_E, serial=0x0001))
        await writer.drain()
        answer = await drain(reader)
        writer.close()
    return (answer[:4],)


async def probe_zero_4e_tail(board: Pathology) -> Observation:
    async with simulator(board) as plc:
        reader, writer = await asyncio.open_connection(*plc.address(TCP_4E))
        raw = bytearray(read_words_frame(frame=FOUR_E, serial=0xBEEF))
        raw[4:6] = b"\xaa\xbb"
        writer.write(bytes(raw))
        await writer.drain()
        answer = await drain(reader)
        writer.close()
    return (answer[4:6],)


async def probe_single_connection(board: Pathology) -> Observation:
    async with simulator(board) as plc:
        host, port = plc.address(TCP)
        _first_reader, first_writer = await asyncio.open_connection(host, port)
        second_reader, second_writer = await asyncio.open_connection(host, port)
        second_writer.write(read_words_frame())
        await second_writer.drain()
        answer = await drain(second_reader)
        second_writer.close()
        first_writer.close()
    return (bool(answer),)


async def probe_coalesce_requests(board: Pathology) -> Observation:
    async with simulator(board) as plc:
        reader, writer = await asyncio.open_connection(*plc.address(TCP))
        writer.write(read_words_frame() + read_words_frame())
        await writer.drain()
        answer = await drain(reader)
        writer.close()
    return (len(answer),)


async def probe_silence_on_wrong_encoding(board: Pathology) -> Observation:
    async with simulator(board) as plc:
        reader, writer = await asyncio.open_connection(*plc.address(TCP))
        writer.write(read_words_frame(device=b"D*000000", codec=ASCII))
        await writer.drain()
        answer = await drain(reader)
        writer.close()
    return (answer,)


async def probe_overstated_length(board: Pathology) -> Observation:
    async with simulator(board) as plc:
        reader, writer = await asyncio.open_connection(*plc.address(TCP))
        writer.write(overstated_frame())
        await writer.drain()
        answer = await drain(reader, 0.25)
        writer.close()
    return (bool(answer),)


async def probe_stale_reply(board: Pathology) -> Observation:
    async with simulator(board) as plc:
        plc.memory.set_u16("D", 0, 0xAAAA)
        plc.memory.set_u16("D", 8, 0xBBBB)
        reader, writer = await asyncio.open_connection(*plc.address(TCP))
        answers = []
        for device in (b"\x00\x00\x00\xa8", b"\x08\x00\x00\xa8"):
            writer.write(read_words_frame(device=device))
            await writer.drain()
            answers.append(await drain(reader, 0.2))
        writer.close()
    return tuple(answers)


async def probe_late_reply_tcp(board: Pathology) -> Observation:
    async with simulator(board) as plc:
        reader, writer = await asyncio.open_connection(*plc.address(TCP))
        writer.write(read_words_frame())
        await writer.drain()
        answer = await drain(reader, 0.08)
        writer.close()
    return (bool(answer),)


async def probe_segmentation(board: Pathology) -> Observation:
    async with simulator(board) as plc:
        reader, writer = await asyncio.open_connection(*plc.address(TCP))
        writer.write(read_words_frame(count=960))
        await writer.drain()
        await drain(reader, 0.6)
        writer.close()
        return (tx_lengths(plc), tx_gaps_over_50ms(plc))


async def burst_udp(
    board: Pathology, count: int, *, collect: float, gap: float = 0.0
) -> Observation:
    async with simulator(board) as plc:
        host, port = plc.address(UDP)
        loop = asyncio.get_running_loop()
        transport, protocol = await loop.create_datagram_endpoint(
            DatagramClient, remote_addr=(host, port)
        )
        try:
            for _ in range(count):
                transport.sendto(read_words_frame())
                if gap:
                    await asyncio.sleep(gap)
            await asyncio.sleep(collect)
            answered = len(protocol.received)
        finally:
            transport.close()
    return (answered,)


async def probe_drop_every_nth(board: Pathology) -> Observation:
    return await burst_udp(board, 6, collect=0.3, gap=0.02)


async def probe_udp_depth(board: Pathology) -> Observation:
    return await burst_udp(board, 32, collect=0.8)


async def probe_late_reply_udp(board: Pathology) -> Observation:
    return await burst_udp(board, 1, collect=0.08)


PROBES: Final[dict[tuple[str, str], SwitchProbe]] = {
    ("remote_run_lies", "_handle_remote_run"): SwitchProbe(
        values=(False, True),
        run=dispatched(
            (REMOTE_RUN, READ_SD203),
            state=lambda: SessionState(remote_request=CpuRunState.STOP),
        ),
        why=(
            "a 1001 against a CPU whose last remote request was STOP moves SD203 to RUN, "
            "and a lying one answers 0x0000 and leaves it at STOP"
        ),
    ),
    ("remote_run_lies", "_handle_remote_stop"): SwitchProbe(
        values=(False, True),
        run=dispatched((REMOTE_STOP_FX5U, READ_SD203)),
        why="1002 moves SD203 to STOP; the lie answers 0x0000 and leaves it RUN",
    ),
    ("remote_run_lies", "_handle_remote_pause"): SwitchProbe(
        values=(False, True),
        run=dispatched((REMOTE_PAUSE, READ_SD203)),
        why=(
            "1003 moves SD203 to PAUSE; the lie answers 0x0000 and leaves it RUN. This is "
            "the row whose absence let the pause handler's early return be deleted with "
            "the whole suite still green on 2026-09-07"
        ),
    ),
    ("remote_reset_no_response", "_handle_remote_reset"): SwitchProbe(
        values=(False, True),
        run=dispatched((REMOTE_RESET_FX5U,)),
        why=(
            "SH(NA)-080956ENG-M p.136: on success the response is not sent back, so the "
            "documented behaviour is an absent answer rather than 0x0000"
        ),
    ),
    ("accept_illegal_random_points", "accepts_random_device"): SwitchProbe(
        values=(frozenset(), frozenset({"TS"})),
        run=dispatched((READ_RANDOM_TS0,)),
        why=(
            "a 0403 word point at TS0 is 0xC05C on a CPU that obeys JY997D56001-K p.78 "
            "and 0x0000 with data on the one we measured"
        ),
    ),
    ("accept_4e_on_3e_entry", "_frame_for"): SwitchProbe(
        values=(False, True),
        run=probe_accept_4e_on_3e,
        why="a 4E frame on a 3E entry is either answered as 4E or is not this entry's protocol",
    ),
    ("wrong_serial_echo", "_build_response"): SwitchProbe(
        values=(False, True),
        run=probe_wrong_serial_echo,
        why="the echoed 4E serial is the one that was sent, or it is not",
    ),
    ("zero_4e_tail", "_build_response"): SwitchProbe(
        values=(False, True),
        run=probe_zero_4e_tail,
        why=(
            "the two subheader units after the 4E serial come back zeroed whatever was "
            "sent, which is why they are not a second correlation field. The non-default "
            "branch -- a CPU that echoes them -- had no test until 2026-09-07"
        ),
    ),
    ("single_connection", "serve_connection"): SwitchProbe(
        values=(False, True),
        run=probe_single_connection,
        why="a second connection to a busy entry is either served or accepted and closed",
    ),
    ("coalesce_requests", "_tcp_loop"): SwitchProbe(
        values=(False, True),
        run=probe_coalesce_requests,
        why="two requests in one read produce two responses, or exactly one",
    ),
    ("silence_on_wrong_encoding", "_tcp_loop"): SwitchProbe(
        values=(False, True),
        run=probe_silence_on_wrong_encoding,
        why="an ASCII request on a binary entry is answered 0xC06F, or with nothing at all",
    ),
    ("overstated_length_hangs", "_read_tcp"): SwitchProbe(
        values=(False, True),
        run=probe_overstated_length,
        why=(
            "a frame that declared more than it delivered is answered after the "
            "grace, or never"
        ),
    ),
    ("overstated_length_grace_s", "_read_tcp"): SwitchProbe(
        values=(0.02, 0.6),
        run=probe_overstated_length,
        why=(
            "with the hang off, the grace is how long the CPU waits for the missing bytes "
            "before answering, so it decides whether an answer is inside a 0.25 s deadline"
        ),
    ),
    ("stale_reply", "_answer_tcp"): SwitchProbe(
        values=(False, True),
        run=probe_stale_reply,
        why="the second read answers with D8, or with the D0 response one transaction behind",
    ),
    ("late_reply_s", "_send"): SwitchProbe(
        values=(0.0, 0.3),
        run=probe_late_reply_tcp,
        why="the TCP response is inside a 0.08 s deadline, or it is not",
    ),
    ("segment_at", "_send"): SwitchProbe(
        values=(None, 1460),
        run=probe_segmentation,
        why="a 1931-byte response leaves in one write, or as 1460 + 471",
    ),
    ("segment_gap_s", "_send"): SwitchProbe(
        values=(0.0, 0.15),
        run=probe_segmentation,
        base=Pathology(segment_at=1460),
        why=(
            "the pause between segmented chunks. Only observable while something is being "
            "segmented, which is why this probe's base board has segment_at set"
        ),
    ),
    ("drop_every_nth_datagram", "feed_datagram"): SwitchProbe(
        values=(None, 2),
        run=probe_drop_every_nth,
        why="six datagrams come back six times, or three",
    ),
    ("udp_drop_above_depth", "feed_datagram"): SwitchProbe(
        values=(None, 4),
        run=probe_udp_depth,
        base=Pathology(udp_service_delay_s=0.01),
        why="a burst past the in-flight ceiling loses datagrams with no error of any kind",
    ),
    ("udp_service_delay_s", "_serve_datagram"): SwitchProbe(
        values=(0.0, 0.01),
        run=probe_udp_depth,
        base=Pathology(udp_drop_above_depth=4),
        why=(
            "without service time nothing is ever in flight, so the measured loss is a "
            "receive-queue overflow rather than a rate limit and the depth alone drops nothing"
        ),
    ),
    ("late_reply_s", "_serve_datagram"): SwitchProbe(
        values=(0.0, 0.3),
        run=probe_late_reply_udp,
        why=(
            "the UDP response is inside a 0.08 s deadline, or it is not. The datagram path "
            "delays its own replies and had no test for it until 2026-09-07"
        ),
    ),
}
"""Every ``(switch, consumer)`` pair, and how a client tells the two values apart.

Written out by hand and checked against :func:`switch_consumers`, for the same reason
``tests/unit/test_simulator_state.py``'s table is: a table derived from the code it
polices agrees with the code by construction and proves nothing.
"""


# --------------------------------------------------------------------------------------
# The enforcement, for the pathology board
# --------------------------------------------------------------------------------------


def test_every_pathology_switch_is_consulted_by_something() -> None:
    """A switch nothing reads is the ``four_e_frames`` bug wearing a measurement.

    ``PATHOLOGY_SOURCES`` already makes a switch name its observation. That is a claim
    about provenance and not about effect: a citation says why the switch would matter if
    anything read it.
    """
    consulted = {switch for switch, _consumer in switch_consumers()}
    assert consulted == set(SWITCHES), (
        f"pathology switches nothing in {[m.__name__ for m in CONSUMER_MODULES]} reads: "
        f"{sorted(set(SWITCHES) - consulted)}"
    )


def test_every_consumer_of_every_switch_has_a_probe() -> None:
    """The half three hand-written tests cannot have: a fourth consumer cannot arrive quietly.

    If this fails on a function you just wrote, the fix is a row in :data:`PROBES` that
    shows what changes for a client when that function's switch is flipped. If you cannot
    write one, the branch you added changes nothing anybody can see, and that is the
    finding rather than the obstacle.
    """
    discovered = switch_consumers()
    probed = set(PROBES)
    assert probed == discovered, (
        f"switch consumers with no probe: {sorted(discovered - probed)}; "
        f"probes for consumers that no longer read that switch: {sorted(probed - discovered)}"
    )


@pytest.mark.parametrize(("switch", "consumer"), sorted(PROBES))
async def test_each_switch_consumer_changes_what_a_client_sees(
    switch: str, consumer: str
) -> None:
    """The behavioural half: the probe has to actually distinguish."""
    probe = PROBES[(switch, consumer)]
    low, high = probe.values
    first = await probe.run(probe.board(low, switch))
    second = await probe.run(probe.board(high, switch))
    assert first != second, (
        f"{consumer} reads Pathology.{switch}, but {switch}={low!r} and {switch}={high!r} "
        f"produce identical answers, so no client can tell them apart. "
        f"The probe says: {probe.why}"
    )


@pytest.mark.parametrize(("switch", "consumer"), sorted(PROBES))
async def test_each_probe_enters_the_consumer_it_is_filed_under(
    switch: str, consumer: str
) -> None:
    """Aim, not only effect.

    Two of the three ``remote_run_lies`` consumers are one command code apart. A probe
    filed under ``_handle_remote_pause`` that sent ``1002`` would distinguish its two
    boards perfectly and leave ``1003`` exactly as untested as it was on 2026-09-07,
    which is the shape of the bug this whole file exists for. So the run is profiled and
    the named function has to have been entered.
    """
    probe = PROBES[(switch, consumer)]
    wanted = code_of(consumer)
    entered = False

    def watch(frame: Any, event: str, arg: Any) -> None:
        nonlocal entered
        del arg
        if event == "call" and frame.f_code is wanted:
            entered = True

    sys.setprofile(watch)
    try:
        await probe.run(probe.board(probe.values[1], switch))
    finally:
        sys.setprofile(None)

    assert entered, (
        f"the probe for Pathology.{switch} is filed under {consumer}, and running it "
        f"never entered {consumer}. It is exercising a different consumer of the same "
        f"switch, so the one it names is still untested."
    )


def test_the_one_pathology_field_that_is_not_a_switch_is_still_read() -> None:
    """``sources`` is excluded from the switch rule, so it gets its own sentence.

    An exclusion nobody checks is how the next dead field gets in. This one carries extra
    provenance for a custom board, and ``cites()`` is what reads it.
    """
    extra = PATHOLOGY_SOURCES["coalesce_requests"]
    assert "sources" not in SWITCHES
    assert Pathology().cites() == ()
    assert Pathology(sources=(extra,)).cites() == (extra,)


# --------------------------------------------------------------------------------------
# The same question, asked of the target's capability flags
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CapabilityProbe:
    """One CPU, the same exchange run against both values of one declared capability."""

    base: SimulatorTarget
    run: Callable[[SimulatorTarget], Awaitable[Observation]]
    why: str


def served(
    requests: Sequence[RawRequest],
    *,
    codec: Codec = BINARY,
    encoding: Encoding = Encoding.BINARY,
    seed: Callable[[Dispatcher], None] | None = None,
) -> Callable[[SimulatorTarget], Awaitable[Observation]]:
    async def run(target: SimulatorTarget) -> Observation:
        plc = Dispatcher(target=target, memory=target.memory())
        if seed is not None:
            seed(plc)
        return tuple(
            seen(plc.handle(request, codec=codec, encoding=encoding)) for request in requests
        )

    return run


def _seed_outputs(plc: Dispatcher) -> None:
    """``Y37`` set and ``Y69`` clear: octal ``45`` and decimal ``45`` are different wires."""
    plc.memory.write_bits("Y", 37, (True,))
    plc.memory.write_bits("Y", 69, (False,))


async def probe_four_e_frames(target: SimulatorTarget) -> Observation:
    """A 4E frame on the entry configured for 4E: answered, or not this CPU's protocol."""
    async with simulator(target.pathology, target=target) as plc:
        reader, writer = await asyncio.open_connection(*plc.address(TCP_4E))
        writer.write(read_words_frame(frame=FOUR_E, serial=0x0042))
        await writer.drain()
        answer = await drain(reader)
        writer.close()
    return (answer,)


CAPABILITY_PROBES: Final[dict[str, CapabilityProbe]] = {
    "long_device_spec": CapabilityProbe(
        base=PEDANTIC,
        run=served(
            (
                make_request(
                    command=0x0401,
                    subcommand=0x0002,
                    payload=long_spec("D", 0) + BINARY.number(1, bits=16),
                ),
            )
        ),
        why="subcommand 0x0002 is served, or is 0xC059 before the handler is reached",
    ),
    "monitor": CapabilityProbe(
        base=PEDANTIC,
        run=served(
            (
                make_request(
                    command=0x0801,
                    payload=BINARY.number(1, bits=8)
                    + BINARY.number(0, bits=8)
                    + word_spec("D", 100),
                ),
            )
        ),
        why="0801 Monitor Registration is served, or is 0xC059 on an iQ-F",
    ),
    "block_access": CapabilityProbe(
        base=PEDANTIC,
        run=served(
            (
                make_request(
                    command=0x0406,
                    payload=BINARY.number(1, bits=8)
                    + BINARY.number(0, bits=8)
                    + word_spec("D", 0)
                    + BINARY.number(1, bits=16),
                ),
            )
        ),
        why="0406 Read Block returns one block of data, or refuses the command",
    ),
    "remote_control": CapabilityProbe(
        base=PEDANTIC,
        run=served((REMOTE_RUN,)),
        why="1001 Remote RUN is completed, or the CPU says remote control is disabled",
    ),
    "remote_reset": CapabilityProbe(
        base=PEDANTIC,
        run=served((make_request(command=0x1006, payload=BINARY.number(0x0001, bits=16)),)),
        why="1006 Remote Reset is the documented silence, or a refusal a client can read",
    ),
    "remote_password": CapabilityProbe(
        base=PEDANTIC,
        run=served((make_request(command=0x1630, payload=BINARY.number(0, bits=16)),)),
        why="1630 Remote Password Unlock is served, or the command does not exist here",
    ),
    "clear_error": CapabilityProbe(
        base=PEDANTIC,
        run=served((make_request(command=0x1617),)),
        why="1617 Clear Error answers 0x0000, or refuses",
    ),
    "xy_ascii_octal": CapabilityProbe(
        base=FX5U_32MT_DS,
        run=served(
            (
                make_request(
                    command=0x0401,
                    subcommand=0x0001,
                    payload=b"Y*" + b"000045" + ASCII.number(1, bits=16),
                    codec=ASCII,
                ),
            ),
            codec=ASCII,
            encoding=Encoding.ASCII_XY_OCT,
            seed=_seed_outputs,
        ),
        why=(
            "the ASCII digits 000045 are output 37 on a CPU whose X/Y are octal and output "
            "69 on one whose are not -- the largest silent-wrong-register hazard there is"
        ),
    ),
    "four_e_frames": CapabilityProbe(
        base=FX5U_32MT_DS,
        run=probe_four_e_frames,
        why=(
            "a 4E frame is answered, or the subheader is not one this CPU family knows and "
            "the entry answers nothing. Declared on all three targets, printed in the "
            "comparison table and read by nothing at all until 2026-09-07"
        ),
    ),
}
"""How a client tells two values of each declared capability apart.

:attr:`~aslmp.testing.targets.SimulatorTarget.verified` is the one boolean deliberately
absent: it is a claim about whether anybody has plugged this CPU in, it is printed by
:meth:`~aslmp.testing.targets.SimulatorTarget.warn_if_unverified`, and a client cannot
see it because it is not a behaviour.
"""

NOT_A_CAPABILITY: Final = frozenset({"verified"})


def test_every_declared_capability_has_a_probe() -> None:
    """A boolean on a target is a claim about behaviour, or it must not be a boolean.

    ``four_e_frames`` is why this exists. It was declared on all three targets, listed in
    :func:`~aslmp.testing.targets.diff_targets`' flag table, and consulted by no handler
    and no socket: the diff printed a row that could never differ, which is a document
    that agrees with itself.
    """
    booleans = {
        f.name
        for f in dataclasses.fields(SimulatorTarget)
        if f.type in ("bool", bool) and f.name not in NOT_A_CAPABILITY
    }
    assert booleans, "the field scan found no boolean capabilities at all"
    assert set(CAPABILITY_PROBES) == booleans, (
        f"declared capabilities with no probe: {sorted(booleans - set(CAPABILITY_PROBES))}; "
        f"probes for capabilities that no longer exist: "
        f"{sorted(set(CAPABILITY_PROBES) - booleans)}"
    )


def with_flag(target: SimulatorTarget, flag: str, value: bool) -> SimulatorTarget:
    """``dataclasses.replace`` with a field name chosen at runtime.

    The whole point of these tests is that the field list is DISCOVERED rather than
    hand-kept -- a capability flag added tomorrow must be probed without anyone editing a
    list here. That means the keyword name is a variable, which ``dataclasses.replace``
    cannot express to a type checker: it types every field against the one ``**kwargs``
    value. The ignore is confined to this function, and the runtime guard below is what
    actually protects the call.
    """
    if flag not in {f.name for f in dataclasses.fields(target)}:
        raise AssertionError(f"{flag} is not a field of SimulatorTarget")
    return dataclasses.replace(target, **{flag: value})  # type: ignore[arg-type]


@pytest.mark.parametrize("flag", sorted(CAPABILITY_PROBES))
async def test_each_capability_flag_changes_what_a_client_sees(flag: str) -> None:
    probe = CAPABILITY_PROBES[flag]
    without = await probe.run(with_flag(probe.base, flag, value=False))
    with_it = await probe.run(with_flag(probe.base, flag, value=True))
    assert without != with_it, (
        f"SimulatorTarget.{flag} = False and = True answer identically, so the flag "
        f"declares a capability that decides nothing. The probe says: {probe.why}"
    )


def test_the_unverified_flag_is_excluded_on_the_record() -> None:
    """The one exclusion, and the sentence it is excluded for."""
    assert {"verified"} == NOT_A_CAPABILITY
    assert "UNVERIFIED" in dataclasses.replace(PEDANTIC, verified=False).warn_if_unverified()
    assert dataclasses.replace(PEDANTIC, verified=True).warn_if_unverified() == ""
