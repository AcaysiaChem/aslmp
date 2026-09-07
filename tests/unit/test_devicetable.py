"""The generated device table, against a hand-typed copy of the manual's own pages.

``devicetable.py`` is generated from ``data/devices.tsv``, so a test that reads the TSV
and compares it to the module proves only that the generator ran. The oracle here is
:data:`FROM_THE_MANUAL` below: the device code table typed in a second time, from
SH(NA)-080956ENG-M pp.35-36 and SH(NA)-080008-AB p.68, by hand. If a device code is
wrong in the TSV it is wrong in both the module and a TSV-reading test, and only a
second reading of the printed page catches it.

The rest is structure that address parsing and frame building silently depend on: the
prefix order (``SB0`` is link special relay 0, not step relay ``B0``), the short/long
specification coherence, the two-word devices, and the fact that every row can name the
document or the CPU it came from.
"""

from __future__ import annotations

import dataclasses

import pytest

from aslmp.wire.citations import Citation, Measurement, Provenance
from aslmp.wire.codec import ASCII, BINARY, CODECS, Codec, SpecFormat, Unit
from aslmp.wire.devicetable import DEVICE_TABLE, PREFIXES_LONGEST_FIRST, DeviceType, Radix

DEC = Radix.DECIMAL
HEX = Radix.HEXADECIMAL

# name -> (ascii2, ascii4, code_short, code_long, radix)
# SH(NA)-080956ENG-M section 5.2 pp.35-36, with the retentive timer family and the step
# relay from SH(NA)-080008-AB section 8.1 p.68. ``None`` for ascii2/code_short means the
# device is reachable only with subcommand 0002/0003.
FROM_THE_MANUAL: dict[str, tuple[str | None, str, int | None, int, Radix]] = {
    "SM": ("SM", "SM**", 0x91, 0x0091, DEC),
    "SD": ("SD", "SD**", 0xA9, 0x00A9, DEC),
    "X": ("X*", "X***", 0x9C, 0x009C, HEX),
    "Y": ("Y*", "Y***", 0x9D, 0x009D, HEX),
    "M": ("M*", "M***", 0x90, 0x0090, DEC),
    "L": ("L*", "L***", 0x92, 0x0092, DEC),
    "F": ("F*", "F***", 0x93, 0x0093, DEC),
    "V": ("V*", "V***", 0x94, 0x0094, DEC),
    "B": ("B*", "B***", 0xA0, 0x00A0, HEX),
    "D": ("D*", "D***", 0xA8, 0x00A8, DEC),
    "W": ("W*", "W***", 0xB4, 0x00B4, HEX),
    "TS": ("TS", "TS**", 0xC1, 0x00C1, DEC),
    "TC": ("TC", "TC**", 0xC0, 0x00C0, DEC),
    "TN": ("TN", "TN**", 0xC2, 0x00C2, DEC),
    "LTS": (None, "LTS*", None, 0x0051, DEC),
    "LTC": (None, "LTC*", None, 0x0050, DEC),
    "LTN": (None, "LTN*", None, 0x0052, DEC),
    "STS": ("SS", "STS*", 0xC7, 0x00C7, DEC),
    "STC": ("SC", "STC*", 0xC6, 0x00C6, DEC),
    "STN": ("SN", "STN*", 0xC8, 0x00C8, DEC),
    "LSTS": (None, "LSTS", None, 0x0059, DEC),
    "LSTC": (None, "LSTC", None, 0x0058, DEC),
    "LSTN": (None, "LSTN", None, 0x005A, DEC),
    "CS": ("CS", "CS**", 0xC4, 0x00C4, DEC),
    "CC": ("CC", "CC**", 0xC3, 0x00C3, DEC),
    "CN": ("CN", "CN**", 0xC5, 0x00C5, DEC),
    "LCS": (None, "LCS*", None, 0x0055, DEC),
    "LCC": (None, "LCC*", None, 0x0054, DEC),
    "LCN": (None, "LCN*", None, 0x0056, DEC),
    "SB": ("SB", "SB**", 0xA1, 0x00A1, HEX),
    "SW": ("SW", "SW**", 0xB5, 0x00B5, HEX),
    "S": ("S*", "S***", 0x98, 0x0098, DEC),
    "DX": ("DX", "DX**", 0xA2, 0x00A2, HEX),
    "DY": ("DY", "DY**", 0xA3, 0x00A3, HEX),
    "Z": ("Z*", "Z***", 0xCC, 0x00CC, DEC),
    "LZ": (None, "LZ**", None, 0x0062, DEC),
    "R": ("R*", "R***", 0xAF, 0x00AF, DEC),
    "ZR": ("ZR", "ZR**", 0xB0, 0x00B0, HEX),
    "RD": (None, "RD**", None, 0x002C, DEC),
    "G": ("G*", "G***", 0xAB, 0x00AB, DEC),
    "BL": ("BL", "BL**", 0xDC, 0x00DC, DEC),
}

# One point spans two response words. SH(NA)-080956ENG-M section 5.2: the long timer,
# long retentive timer, long counter current values and the long index register.
TWO_WORD_DEVICES = frozenset({"LTN", "LSTN", "LCN", "LZ"})

# SH(NA)-080956ENG-M / JY997D56001-K p.78: contacts and coils are not valid points in
# Read Random, Write Random or a monitor registration.
NO_RANDOM_ACCESS = frozenset(
    {"TS", "TC", "STS", "STC", "CS", "CC", "LTS", "LTC", "LSTS", "LSTC", "LCS", "LCC", "BL"}
)

# The two-character mnemonic is not always the device name padded. SH(NA)-080008-AB p.68
# gives the retentive timer family SS / SC / SN.
MNEMONIC_EXCEPTIONS = {"STS": "SS", "STC": "SC", "STN": "SN"}


def parametrize_devices() -> pytest.MarkDecorator:
    names = sorted(DEVICE_TABLE)
    return pytest.mark.parametrize("name", names, ids=names)


# ==========================================================================================
# Against the manual, read a second time
# ==========================================================================================


def test_the_table_holds_exactly_the_devices_we_typed_in_by_hand() -> None:
    assert set(DEVICE_TABLE) == set(FROM_THE_MANUAL), (
        "the generated device table and the hand-typed copy of SH(NA)-080956ENG-M "
        "pp.35-36 disagree about which devices exist"
    )


@parametrize_devices()
def test_every_device_matches_the_hand_typed_manual_page(name: str) -> None:
    """A device code is a byte on the wire. This is the second pair of eyes on it."""
    ascii2, ascii4, code_short, code_long, radix = FROM_THE_MANUAL[name]
    device = DEVICE_TABLE[name]
    assert device.ascii2 == (ascii2 or ""), f"{name}: 2-character mnemonic"
    assert device.ascii4 == ascii4, f"{name}: 4-character mnemonic"
    assert device.code_short == code_short, f"{name}: 1-byte device code"
    assert device.code_long == code_long, f"{name}: 2-byte device code"
    assert device.radix == radix, f"{name}: default device-number radix"


def test_the_step_relay_is_cited_to_the_manual_that_prints_it_consistently() -> None:
    """SH(NA)-080956ENG-M p.36 prints the step relay's two code columns swapped.

    Every other row on that page reads ``<2-byte> / <1-byte>``; S does not. The row is
    therefore cited to SH(NA)-080008-AB p.68, which agrees with the pattern, and the
    disagreement is registered as ``A-STEP-RELAY-COLUMNS``.
    """
    step = DEVICE_TABLE["S"]
    assert step.code_short == 0x98
    assert step.code_long == 0x0098
    assert step.citation is not None
    assert step.citation.manual == "SH(NA)-080008"
    assert "A-STEP-RELAY-COLUMNS" in step.note


def test_the_retentive_timer_mnemonics_are_not_the_device_name() -> None:
    """``STS`` goes out as ``b"SS"`` under the short specification. Nothing else does."""
    for name, mnemonic in MNEMONIC_EXCEPTIONS.items():
        assert DEVICE_TABLE[name].ascii2 == mnemonic
        assert ASCII.device_code(DEVICE_TABLE[name], SpecFormat.SHORT) == mnemonic.encode()
        assert DEVICE_TABLE[name].ascii4 == f"{name}*"


# ==========================================================================================
# Structure the rest of the wire layer depends on
# ==========================================================================================


@parametrize_devices()
def test_the_key_is_the_name_and_it_is_upper_case(name: str) -> None:
    device = DEVICE_TABLE[name]
    assert device.name == name
    assert name.isupper()
    assert name.isalpha()


@parametrize_devices()
def test_short_and_long_availability_are_coherent(name: str) -> None:
    """``ascii2``, ``code_short`` and ``min_spec`` say the same thing or the table lies.

    A device with a 1-byte code but no 2-character mnemonic would encode in binary and
    raise in ASCII, which is a coding-dependent bug -- the worst kind here.
    """
    device = DEVICE_TABLE[name]
    short_available = device.code_short is not None
    assert bool(device.ascii2) is short_available, f"{name}: mnemonic/code disagree"
    assert (device.min_spec is SpecFormat.SHORT) is short_available
    if short_available:
        assert len(device.ascii2) == 2
        assert device.code_short == device.code_long, (
            f"{name}: the 1-byte code and the 2-byte code are the same number in every "
            f"row of SH(NA)-080956ENG-M pp.35-36"
        )
    assert len(device.ascii4) == 4


@parametrize_devices()
def test_the_four_character_mnemonic_is_the_name_padded_with_stars(name: str) -> None:
    assert DEVICE_TABLE[name].ascii4 == name.ljust(4, "*")


@parametrize_devices()
def test_the_two_character_mnemonic_is_the_name_padded_or_a_named_exception(name: str) -> None:
    device = DEVICE_TABLE[name]
    if not device.ascii2:
        return
    expected = MNEMONIC_EXCEPTIONS.get(name, name.ljust(2, "*"))
    assert device.ascii2 == expected, (
        f"{name}: an unexpected 2-character mnemonic. If SH(NA)-080008-AB really prints "
        f"{device.ascii2!r}, add it to MNEMONIC_EXCEPTIONS with the page."
    )


@parametrize_devices()
def test_device_codes_fit_their_fields(name: str) -> None:
    device = DEVICE_TABLE[name]
    if device.code_short is not None:
        assert 0 <= device.code_short <= 0xFF
    assert 0 <= device.code_long <= 0xFFFF


def test_device_codes_are_unique() -> None:
    """Two devices sharing a code makes one of them unreachable and the other wrong."""
    for attribute in ("code_short", "code_long"):
        seen: dict[int, str] = {}
        for name, device in DEVICE_TABLE.items():
            code = getattr(device, attribute)
            if code is None:
                continue
            assert code not in seen, (
                f"{name} and {seen[code]} share {attribute} 0x{code:04X}"
            )
            seen[code] = name


def test_mnemonics_are_unique() -> None:
    for attribute in ("ascii2", "ascii4"):
        seen: dict[str, str] = {}
        for name, device in DEVICE_TABLE.items():
            text = getattr(device, attribute)
            if not text:
                continue
            assert text not in seen, f"{name} and {seen[text]} share {attribute} {text!r}"
            seen[text] = name


@parametrize_devices()
def test_words_per_point_is_one_except_for_the_long_value_devices(name: str) -> None:
    """A double-word device costs two response words per point, and the block layout
    and every point budget in the library depend on knowing which those are."""
    expected = 2 if name in TWO_WORD_DEVICES else 1
    assert DEVICE_TABLE[name].words_per_point == expected


@parametrize_devices()
def test_the_eligibility_flags_are_booleans(name: str) -> None:
    device = DEVICE_TABLE[name]
    for flag in ("batch_ok", "random_ok", "monitor_ok", "block_ok"):
        assert isinstance(getattr(device, flag), bool), f"{name}.{flag}"


@parametrize_devices()
def test_contacts_and_coils_are_refused_in_random_access(name: str) -> None:
    """Client-side, because the PLC does not refuse them.

    JY997D56001-K p.78 says TS, TC, STS, STC, CS and CC must not be used in Read Random
    and predicts CPU error 0x4032. An FX5U-32MT/DS on firmware 1.065 ACCEPTED a ``TS0``
    word point and answered end code ``0x0000`` with data (measured 2026-09-06). This
    column is the only thing that stops it.
    """
    device = DEVICE_TABLE[name]
    if name in NO_RANDOM_ACCESS:
        assert device.random_ok is False, f"{name} must not be a random-access point"
        assert device.monitor_ok is False, f"{name} must not be a monitor point"
    else:
        assert device.random_ok is True, name


@parametrize_devices()
def test_the_long_only_devices_are_absent_from_batch_reads(name: str) -> None:
    """JY997D56001-K: ``0401`` is not applicable to double-word devices or ``LZ``."""
    device = DEVICE_TABLE[name]
    if name in {"LTS", "LTC", "LTN", "LSTS", "LSTC", "LSTN", "LZ"}:
        assert device.batch_ok is False, name


def test_the_sfc_block_device_is_present_only_to_be_refused() -> None:
    """JY997D56001-K lists BL as SLMP-incompatible.

    It is in the table so ``BL0`` gets a typed refusal naming the reason rather than an
    unknown-device error that reads like a typo.
    """
    block = DEVICE_TABLE["BL"]
    assert (block.batch_ok, block.random_ok, block.monitor_ok, block.block_ok) == (
        False,
        False,
        False,
        False,
    )


# ==========================================================================================
# Prefix ordering: the thing address parsing silently depends on
# ==========================================================================================


def test_prefixes_cover_the_table_exactly() -> None:
    assert set(PREFIXES_LONGEST_FIRST) == set(DEVICE_TABLE)
    assert len(PREFIXES_LONGEST_FIRST) == len(DEVICE_TABLE)


def test_prefixes_are_ordered_longest_first() -> None:
    """Longest-prefix match, or ``SB0`` parses as step relay ``B0``.

    Every one of these pairs is a real address that a shortest-match parser reads as a
    different device: the second element is a prefix of the first.
    """
    lengths = [len(name) for name in PREFIXES_LONGEST_FIRST]
    assert lengths == sorted(lengths, reverse=True)
    order = {name: index for index, name in enumerate(PREFIXES_LONGEST_FIRST)}
    for longer, shorter in (
        ("SB", "S"),
        ("SW", "S"),
        ("SM", "S"),
        ("SD", "S"),
        ("STS", "S"),
        ("STN", "S"),
        ("LTS", "L"),
        ("LCN", "L"),
        ("LSTN", "L"),
        ("LZ", "L"),
        ("ZR", "Z"),
        ("DX", "D"),
        ("DY", "D"),
        ("BL", "B"),
    ):
        assert order[longer] < order[shorter], (
            f"{longer!r} must be tried before {shorter!r}, or {longer}0 parses as "
            f"{shorter} device {longer[len(shorter):]}0"
        )


def test_no_device_name_is_a_prefix_of_a_later_one() -> None:
    """The ordering property, restated as the thing it actually guarantees."""
    for index, name in enumerate(PREFIXES_LONGEST_FIRST):
        for later in PREFIXES_LONGEST_FIRST[index + 1 :]:
            assert not later.startswith(name) or later == name, (
                f"{later!r} starts with {name!r} but is tried after it"
            )


# ==========================================================================================
# Provenance
# ==========================================================================================


@parametrize_devices()
def test_every_device_can_name_its_source(name: str) -> None:
    """A device code with no provenance is a byte nobody can check."""
    device = DEVICE_TABLE[name]
    source = device.source
    assert isinstance(source, Citation | Measurement)
    assert source.reference
    if device.provenance is Provenance.LIVE:
        assert device.measurement is not None
        assert device.measurement.cpu
        assert device.measurement.firmware
    else:
        assert device.citation is not None
        assert device.citation.manual
        assert device.citation.revision
        assert device.citation.section


@parametrize_devices()
def test_a_measurement_and_a_citation_never_contradict_the_provenance(name: str) -> None:
    device = DEVICE_TABLE[name]
    if device.provenance is not Provenance.LIVE:
        assert device.measurement is None, f"{name}: not live, but names silicon"


def test_the_source_property_prefers_the_measurement() -> None:
    """Where a manual and the silicon disagree, the silicon wins and the code says so."""
    for device in DEVICE_TABLE.values():
        if device.measurement is not None:
            assert device.source is device.measurement


# ==========================================================================================
# The table and the codec, together
# ==========================================================================================


@parametrize_devices()
@pytest.mark.parametrize("codec", CODECS, ids=lambda c: c.name)
def test_every_device_encodes_at_its_own_specification_format(codec: Codec, name: str) -> None:
    device = DEVICE_TABLE[name]
    specs = (
        (SpecFormat.SHORT, SpecFormat.LONG)
        if device.code_short is not None
        else (SpecFormat.LONG,)
    )
    for spec in specs:
        field = codec.device_code(device, spec)
        assert len(field) == codec.device_code_len(spec), f"{name} {spec.value}"


@parametrize_devices()
def test_the_binary_short_code_is_the_single_byte_the_manual_prints(name: str) -> None:
    device = DEVICE_TABLE[name]
    if device.code_short is None:
        return
    assert BINARY.device_code(device, SpecFormat.SHORT) == bytes((device.code_short,))
    assert BINARY.device_code(device, SpecFormat.LONG) == device.code_long.to_bytes(2, "little")


@parametrize_devices()
def test_the_ascii_code_is_never_the_hexadecimal_of_the_binary_one(name: str) -> None:
    """``D`` is ``b"D*"``. A table that leaked hex here would read as valid ASCII."""
    device = DEVICE_TABLE[name]
    rendered = ASCII.device_code(device, SpecFormat.LONG)
    assert rendered != f"{device.code_long:04X}".encode(), name
    assert rendered.decode("ascii").rstrip("*") == name


# ==========================================================================================
# The value type itself
# ==========================================================================================


def test_device_type_is_frozen() -> None:
    """The table is a constant. A caller that could edit a device code would."""
    device = DEVICE_TABLE["D"]
    with pytest.raises(dataclasses.FrozenInstanceError):
        # assigning to a frozen dataclass field is the behaviour under test
        device.code_short = 0x00  # type: ignore[misc]


def test_radix_is_the_base_itself() -> None:
    """``Radix`` is an ``IntEnum`` whose value is the base, so it feeds ``int(text, r)``
    and ``codec.device_number(..., base=r)`` with no lookup table in between."""
    assert int(Radix.OCTAL) == 8
    assert int(Radix.DECIMAL) == 10
    assert int(Radix.HEXADECIMAL) == 16
    assert int("45", Radix.OCTAL) == 37
    assert int("1F", Radix.HEXADECIMAL) == 31


def test_units_are_the_codecs_units() -> None:
    """One ``Unit`` enum, shared, so a bit device cannot be word-shaped somewhere else."""
    assert {device.unit for device in DEVICE_TABLE.values()} <= {Unit.BIT, Unit.WORD}
    assert DEVICE_TABLE["D"].unit is Unit.WORD
    assert DEVICE_TABLE["M"].unit is Unit.BIT


def test_a_device_with_no_source_at_all_cannot_report_one() -> None:
    """``DeviceType.source`` raises rather than inventing a provenance."""
    orphan = dataclasses.replace(DEVICE_TABLE["D"], citation=None, measurement=None)
    assert isinstance(orphan, DeviceType)
    with pytest.raises(ValueError, match="neither a citation nor a measurement"):
        _ = orphan.source
