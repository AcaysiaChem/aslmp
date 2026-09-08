"""The ``Command`` contract itself: what a command class must declare, and why.

DESIGN section 4.4 makes ``mutates`` abstract and ``CITES`` non-empty. Those are not
documentation requests. ``mutates`` is what lets the client tell
:class:`~aslmp.errors.SlmpNotSentError` ("provably did not happen") from
:class:`~aslmp.errors.SlmpOutcomeUnknownError` ("may have happened") when a transaction
fails mid-flight, and a command that forgot to declare it would silently take the wrong
branch. ``CITES`` is what makes "a Mitsubishi engineer can check any byte we emit
against their own document" enforceable rather than aspirational.

So both are checked when the class is created, not by a test somebody can forget to run.
The tests here prove the check fires.
"""

from __future__ import annotations

import inspect

import pytest

from aslmp.commands import COMMANDS, Command, EncodeContext, ReadWords, SelfTest
from aslmp.commands.base import CommandSummary, signed, unsigned
from aslmp.errors import SlmpConfigurationError, SlmpUsageError, SlmpValueRangeError
from aslmp.profile import Encoding, Link
from aslmp.profiles import FX5U
from aslmp.wire.citations import Citation, Measurement
from aslmp.wire.codec import BINARY, SpecFormat

CITE = Citation(manual="SH(NA)-080956ENG", revision="M", section="6 p.45")

CTX = EncodeContext(
    codec=BINARY,
    spec=SpecFormat.SHORT,
    profile=FX5U,
    encoding=Encoding.BINARY,
    link=Link.CPU_BUILTIN,
)


class _Complete(Command[None]):
    """A minimal, fully declared command, as a control for the tests below."""

    CODE = 0x0619
    NAME = "control"
    mutates = False
    CITES = (CITE,)

    def subcommand(self, ctx: EncodeContext) -> int:
        return 0

    def validate(self, ctx: EncodeContext) -> None:
        return None

    def payload_len(self, ctx: EncodeContext) -> int:
        return 0

    def encode(self, ctx: EncodeContext) -> bytes:
        return b""

    def decode(self, payload: bytes, ctx: EncodeContext) -> None:
        return None

    def describe(self) -> str:
        return "control()"


def test_the_control_class_builds() -> None:
    """If this fails, the refusals below are proving nothing."""
    assert _Complete().describe() == "control()"


def test_a_command_that_forgets_mutates_does_not_import() -> None:
    with pytest.raises(TypeError, match="mutates"):

        class _NoMutates(Command[None]):
            CODE = 0x0619
            NAME = "no mutates"
            CITES = (CITE,)


def test_a_command_that_forgets_cites_does_not_import() -> None:
    with pytest.raises(TypeError, match="CITES"):

        class _NoCites(Command[None]):
            CODE = 0x0619
            NAME = "no cites"
            mutates = False


def test_an_empty_cites_tuple_is_refused() -> None:
    with pytest.raises(TypeError, match="non-empty"):

        class _EmptyCites(Command[None]):
            CODE = 0x0619
            NAME = "empty cites"
            mutates = False
            CITES = ()


def test_a_cites_entry_that_is_not_a_source_is_refused() -> None:
    with pytest.raises(TypeError, match="Citation"):

        class _BadCite(Command[None]):
            CODE = 0x0619
            NAME = "bad cite"
            mutates = False
            CITES = ("SH(NA)-080956ENG p.45",)  # type: ignore[assignment]


def test_a_command_that_forgets_its_code_does_not_import() -> None:
    with pytest.raises(TypeError, match="CODE"):

        class _NoCode(Command[None]):
            NAME = "no code"
            mutates = False
            CITES = (CITE,)


def test_a_command_that_forgets_its_name_does_not_import() -> None:
    with pytest.raises(TypeError, match="NAME"):

        class _NoName(Command[None]):
            CODE = 0x0619
            mutates = False
            CITES = (CITE,)


def test_an_abstract_intermediate_is_exempt() -> None:
    class _Base(Command[None], abstract=True):
        """Not a command; a shared implementation."""

    assert _Base.ABSTRACT is True


# ======================================================================================
# The citation contract, over every shipped command
# ======================================================================================


def test_every_shipped_command_carries_at_least_one_citation() -> None:
    for spec in COMMANDS.values():
        for command in spec.commands:
            assert command.CITES, f"{command.__qualname__} carries no citation"


def test_every_citation_names_a_manual_and_a_revision() -> None:
    for spec in COMMANDS.values():
        for source in spec.cites:
            if isinstance(source, Citation):
                assert source.manual.strip()
                assert source.revision.strip()
                assert source.section.strip()
            else:
                assert isinstance(source, Measurement)
                assert source.cpu.strip()
                assert source.firmware.strip()


def test_every_manual_a_command_cites_is_in_the_shipped_manual_table() -> None:
    """A citation nobody can look up is worse than no citation."""
    import csv
    from pathlib import Path

    data = Path("src/aslmp/data/manuals.tsv")
    lines = [
        line
        for line in data.read_text(encoding="utf-8").splitlines()
        if not line.startswith("#")
    ]
    known = {
        (row["manual"], row["revision"])
        for row in csv.DictReader(lines, delimiter="\t")
    }
    for spec in COMMANDS.values():
        for source in spec.cites:
            if isinstance(source, Citation):
                assert (source.manual, source.revision) in known, (
                    f"0x{spec.code:04X} cites {source.reference}, which is not a row of "
                    f"data/manuals.tsv"
                )


def test_every_measurement_a_command_cites_names_our_bench() -> None:
    """A Measurement outranks a manual everywhere, so it has to name the silicon."""
    for spec in COMMANDS.values():
        for source in spec.cites:
            if isinstance(source, Measurement):
                assert source.cpu == "FX5U-32MT/DS"
                assert source.firmware == "1.065"
                assert source.date.startswith("2026-")


# ======================================================================================
# The shared machinery
# ======================================================================================


def test_body_len_is_the_three_fixed_fields_plus_the_payload() -> None:
    command = ReadWords("D0", 3)
    assert command.body_len(CTX) == 6 + command.payload_len(CTX)


def test_checked_encode_refuses_a_payload_len_that_disagrees() -> None:
    """The runtime half of the guard against the measured overstated-length hang."""

    class _Liar(Command[None]):
        CODE = 0x0619
        NAME = "liar"
        mutates = False
        CITES = (CITE,)

        def subcommand(self, ctx: EncodeContext) -> int:
            return 0

        def validate(self, ctx: EncodeContext) -> None:
            return None

        def payload_len(self, ctx: EncodeContext) -> int:
            return 4

        def encode(self, ctx: EncodeContext) -> bytes:
            return b"AB"

        def decode(self, payload: bytes, ctx: EncodeContext) -> None:
            return None

        def describe(self) -> str:
            return "liar()"

    with pytest.raises(SlmpConfigurationError) as caught:
        _Liar().checked_encode(CTX)
    assert "OVERSTATED" in str(caught.value)
    assert "no response at all" in str(caught.value)


def test_summary_carries_what_an_exception_may_print() -> None:
    command = ReadWords("D0", 3)
    summary = command.summary(CTX, request_bytes=21)
    assert isinstance(summary, CommandSummary)
    assert summary.command == 0x0401
    assert summary.subcommand == 0x0000
    assert summary.request_bytes == 21
    assert summary.describe() == "read_words('D0', 3)"


def test_the_subcommand_is_derived_from_unit_and_spec() -> None:
    """Never a literal at a call site: 0401 with a hard-coded 0000 against a bit device
    returns word-packed data that decodes into plausible booleans."""
    from aslmp.commands import ReadBits

    assert ReadWords("D0", 1).subcommand(CTX) == 0x0000
    assert ReadBits("M0", 1).subcommand(CTX) == 0x0001


def test_unsigned_refuses_rather_than_masking() -> None:
    assert unsigned(-1, bits=16, what="x", signed_field=None) == 0xFFFF
    assert unsigned(0xFFFF, bits=16, what="x", signed_field=None) == 0xFFFF
    with pytest.raises(SlmpValueRangeError, match="does not fit"):
        unsigned(0x10000, bits=16, what="x", signed_field=None)


def test_unsigned_has_no_default_domain_so_a_call_site_cannot_forget_one() -> None:
    """The permissive union must be typed out, not inherited by omission.

    ``signed_field`` spent one revision defaulting to ``None``, which made enforcement
    opt-in per call site: ``RandomWrite.wire_value`` forgot, and a point declared
    ``kind='i16'`` masked 40000 to 0x9C40 exactly as before the fix. There is no value
    that is safe as a default for a signed field, an unsigned field and a raw register
    at once, so the parameter is required and this test holds the signature.
    """
    parameter = inspect.signature(unsigned).parameters["signed_field"]
    assert parameter.default is inspect.Parameter.empty
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    with pytest.raises(TypeError, match="signed_field"):
        unsigned(1, bits=16, what="x")  # type: ignore[call-arg]


def test_a_value_outside_the_field_is_a_value_range_error_not_a_configuration_error() -> None:
    """DESIGN section 3.1 gives ``SlmpValueRangeError`` to "value outside the declared
    field's domain". A bad datum and an incoherently configured client are different
    mistakes with different fixes, and before this the datum was reported as the
    configuration."""
    with pytest.raises(SlmpValueRangeError):
        unsigned(0x10000, bits=16, what="x", signed_field=None)
    assert issubclass(SlmpValueRangeError, SlmpUsageError)
    assert not issubclass(SlmpValueRangeError, SlmpConfigurationError)


def test_signed_is_twos_complement() -> None:
    assert signed(0xFFFF, bits=16) == -1
    assert signed(0x7FFF, bits=16) == 0x7FFF
    assert signed(0x80000000, bits=32) == -0x80000000


def test_a_command_is_frozen_and_slotted() -> None:
    """A prebuilt plan holds one for the life of a control loop."""
    command = SelfTest(b"ABCD")
    with pytest.raises(AttributeError):
        command.payload = b"BEEF"  # type: ignore[misc]


def test_the_context_is_frozen_too() -> None:
    with pytest.raises(AttributeError):
        CTX.spec = SpecFormat.LONG  # type: ignore[misc]


def test_the_context_resolves_a_literal_only_through_its_profile() -> None:
    """``Y20`` is output 16 on an iQ-F and output 32 on an iQ-R, and both answer 0x0000."""
    from aslmp.profiles import IQ_R

    iqr = EncodeContext(
        codec=BINARY,
        spec=SpecFormat.SHORT,
        profile=IQ_R,
        encoding=Encoding.BINARY,
        link=Link.CPU_BUILTIN,
    )
    assert CTX.address("Y20").index == 16
    assert iqr.address("Y20").index == 32
