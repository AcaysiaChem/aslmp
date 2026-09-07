"""Golden byte vectors, replayed **through the simulator**.

The simulator and the client share ``aslmp.wire.codec`` and the generated device table.
That is the one place a bug could hide from every client-against-server test in the
suite, it is named as a residual risk in DESIGN section 7, and this file is its
mitigation: the request bytes and the response bytes below were printed in a Mitsubishi
manual or captured off an FX5U-32MT/DS, and they owe nothing to either side of this
library.

The replay is deliberately **not** circular. Memory is seeded from the vector's
``decoded`` field -- the human-readable values the manual's prose gives ("T100 = 1234H",
"three floats: 60.0, 23.892..., ...") -- and the assertion is on the raw response bytes.
If the simulator laid the response out differently from the printed page, no amount of
agreement between our encoder and our decoder would hide it.
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Any

import pytest

from aslmp.profile import Encoding
from aslmp.testing.dispatch import Dispatcher, Reply
from aslmp.testing.targets import FX5U_32MT_DS, PEDANTIC, SimulatorTarget
from aslmp.testing.vectors import Corpus, load_corpus, load_vectors, merge
from aslmp.wire.address import parse_address
from aslmp.wire.citations import Provenance
from aslmp.wire.codec import BINARY
from aslmp.wire.frames import THREE_E, request_body
from aslmp.wire.route import Route

VECTORS = Path(__file__).resolve().parents[1] / "vectors"

TARGETS: dict[str, SimulatorTarget] = {
    "melsec:iq-r": PEDANTIC,
    "melsec:iq-f/fx5u": FX5U_32MT_DS,
}
"""Which simulated CPU stands in for each profile the corpus was written against."""


# --------------------------------------------------------------------------------------
# The loader
# --------------------------------------------------------------------------------------


def test_every_shipped_corpus_loads() -> None:
    for name in ("codec_vectors", "command_vectors", "frame_vectors"):
        corpus = load_corpus(VECTORS / f"{name}.jsonl")
        assert len(corpus) > 0, f"{name} is empty"


def test_a_vector_knows_whether_it_came_off_a_wire() -> None:
    """A printed example and a capture are different kinds of evidence."""
    corpus = load_corpus(VECTORS / "frame_vectors.jsonl")
    assert corpus.hardware(), "the frame corpus carries measured rows"
    assert corpus.manual(), "and printed ones"
    assert all(vector.measured for vector in corpus.hardware())
    assert all(
        vector.provenance is Provenance.MANUAL for vector in corpus.manual()
    )


def test_an_unknown_provenance_is_refused(tmp_path: Path) -> None:
    """A row nobody can weigh is a row that should not be in the corpus."""
    path = tmp_path / "bad.jsonl"
    path.write_text('{"id": "x", "hex": "00", "provenance": "vibes"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="provenance"):
        load_vectors(path)


def test_bad_hexadecimal_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "bad.jsonl"
    path.write_text('{"id": "x", "hex": "zz", "provenance": "manual"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="hexadecimal"):
        load_vectors(path)


def test_comments_and_blank_lines_are_skipped(tmp_path: Path) -> None:
    path = tmp_path / "ok.jsonl"
    path.write_text(
        "# a note\n\n" + '{"id": "x", "hex": "5000", "provenance": "manual"}\n',
        encoding="utf-8",
    )
    assert len(load_vectors(path)) == 1


def test_an_exclusion_is_data_and_must_name_a_real_row() -> None:
    """DESIGN section 0.3 excludes the 3E binary X/Y examples in JY997D56001.

    An exclusion that lives in a comment is an exclusion nobody can audit, and one that
    names nothing is an exclusion that stopped working.
    """
    corpus = load_corpus(VECTORS / "frame_vectors.jsonl")
    first = corpus.vectors[0].key
    trimmed = corpus.excluding({first: "for the sake of the example"})
    assert len(trimmed) == len(corpus) - 1
    assert trimmed.excluded == ((first, "for the sake of the example"),)
    with pytest.raises(KeyError, match="no such row"):
        corpus.excluding({"not-a-row": "why"})


def test_merging_refuses_duplicate_keys() -> None:
    corpus = load_corpus(VECTORS / "codec_vectors.jsonl")
    with pytest.raises(ValueError, match="appears in both"):
        merge("doubled", (corpus, corpus))


def test_merging_keeps_every_row() -> None:
    left = load_corpus(VECTORS / "codec_vectors.jsonl")
    right = load_corpus(VECTORS / "frame_vectors.jsonl")
    both = merge("all", (left, right))
    assert len(both) == len(left) + len(right)
    assert isinstance(both, Corpus)


# --------------------------------------------------------------------------------------
# The replay
# --------------------------------------------------------------------------------------


def _rows() -> list[dict[str, Any]]:
    return [dict(vector.fields) for vector in load_corpus(VECTORS / "command_vectors.jsonl")]


REPLAYABLE = {"ReadWords", "ReadBits", "ReadRandom", "ReadTypeName", "SelfTest"}
"""Read-direction commands whose response is fully determined by device memory.

Writes, blocks, monitoring and remote control are covered by
``tests/unit/test_simulator_dispatch.py`` against the library's own encoder; the rows
here are the ones where a printed page can arbitrate.
"""


def _replay_rows() -> list[dict[str, Any]]:
    return [
        row
        for row in _rows()
        if row["cls"] in REPLAYABLE
        and row["codec"] == "binary"
        and row.get("response")
        and row["profile"] in TARGETS
    ]


def _seed(plc: Dispatcher, row: dict[str, Any], target: SimulatorTarget) -> None:
    """Put the manual's own stated values into device memory."""
    args = row["args"]
    kind = row["decoded_kind"]
    if kind == "words":
        address = parse_address(args["address"], target.profile)
        plc.memory.write_words(address.type.name, address.index, row["decoded"])
    elif kind == "bits":
        address = parse_address(args["address"], target.profile)
        plc.memory.write_bits(address.type.name, address.index, row["decoded"])
    elif kind == "random":
        for (literal, width, point_kind), value in zip(
            args["points"], row["decoded"], strict=True
        ):
            address = parse_address(literal, target.profile)
            name = address.type.name
            if point_kind == "bits":
                plc.memory.write_bits(name, address.index, value)
            elif point_kind == "f32":
                plc.memory.set_f32(name, address.index, value)
            elif width == "dword":
                plc.memory.set_u32(name, address.index, value)
            else:
                plc.memory.set_u16(name, address.index, value)


@pytest.mark.parametrize("row", _replay_rows(), ids=lambda row: str(row["id"]))
def test_the_simulator_reproduces_the_golden_response(row: dict[str, Any]) -> None:
    """Seed the manual's values, send the manual's request, get the manual's bytes."""
    target = TARGETS[row["profile"]]
    if row["cls"] == "ReadTypeName" and row["decoded"]["model"] != target.model_name:
        pytest.skip(
            f"{row['decoded']['model']} is not the model {target.label} identifies as; "
            f"the 0101 response is a property of the CPU, not of device memory"
        )
    plc = Dispatcher(target=target, memory=target.memory())
    _seed(plc, row, target)
    body = request_body(
        BINARY,
        monitoring_timer=0,
        command=row["command"],
        subcommand=row["subcommand"],
        payload=bytes.fromhex(row["request"]),
    )
    raw = THREE_E.build(route=Route.OWN_STATION, body=body, codec=BINARY)
    outcome = plc.handle(
        THREE_E.parse_request(raw, BINARY), codec=BINARY, encoding=Encoding.BINARY
    )
    assert isinstance(outcome, Reply)
    assert outcome.end_code == 0x0000, f"{row['id']}: {row['meaning']}"
    assert outcome.payload == bytes.fromhex(row["response"]), (
        f"{row['id']} ({row['section']}): {row['meaning']}"
    )


def test_the_replay_covers_the_measured_rows_too() -> None:
    """Not just printed examples: the FX5U captures are replayed as well."""
    measured = [row for row in _replay_rows() if row["provenance"] == "live"]
    assert len(measured) >= 4, "the hardware captures must be in the replay set"


def test_the_manual_p56_random_read_lays_out_word_then_dword() -> None:
    """SH(NA)-080956ENG-M p.56, in full, because it is the layout that matters most.

    Four word points then three double-word points, and ``4E 4F 54 4C`` is
    ``D1500 = 0x4F4E`` then ``D1501 = 0x4C54`` -- the low word first, which is why one
    double-word point is one f32 with no helper anywhere.
    """
    row = next(item for item in _rows() if item["id"] == "m-0403-p56-mixed")
    target = TARGETS[row["profile"]]
    plc = Dispatcher(target=target, memory=target.memory())
    _seed(plc, row, target)
    assert plc.memory.read_words("D", 1500, 2) == (0x4F4E, 0x4C54)
    body = request_body(
        BINARY,
        monitoring_timer=0,
        command=row["command"],
        subcommand=row["subcommand"],
        payload=bytes.fromhex(row["request"]),
    )
    raw = THREE_E.build(route=Route.OWN_STATION, body=body, codec=BINARY)
    outcome = plc.handle(
        THREE_E.parse_request(raw, BINARY), codec=BINARY, encoding=Encoding.BINARY
    )
    assert isinstance(outcome, Reply)
    assert outcome.payload.hex().upper() == row["response"].upper()


def test_the_hardware_f32_triple_reads_back_as_the_floats_it_was_seeded_with() -> None:
    """The bench's three double-word points: 60.0, 23.892..., and a raw u32."""
    row = next(item for item in _rows() if item["id"] == "h-0403-dword-f32-triple")
    target = TARGETS[row["profile"]]
    plc = Dispatcher(target=target, memory=target.memory())
    _seed(plc, row, target)
    assert plc.memory.get_f32("D", 0) == pytest.approx(60.0)
    words = plc.memory.read_words("D", 0, 2)
    assert struct.unpack("<f", struct.pack("<HH", *words))[0] == pytest.approx(60.0)
