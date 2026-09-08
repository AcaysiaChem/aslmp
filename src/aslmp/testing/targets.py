"""Three CPUs to simulate: the manual as written, our silicon, and an iQ-R we have never seen.

Layer 2.5 (``aslmp.testing``). Pure data.

A conformance simulator is only worth anything if it can be **wrong in the same places
the hardware is wrong**. So the behaviour a target exhibits is declared here, as data,
and not read out of :class:`~aslmp.profile.CpuProfile`:

* :data:`PEDANTIC` -- SH(NA)-080956ENG-M and SH(NA)-080008-AB, taken at their word. The
  generic bit ceiling of 7168, ``0xC05B`` for a device the CPU does not have, ``0xC056``
  for a zero point count, ``0xC057`` for a length mismatch, the long device
  specification available, monitoring available. Nothing this target does was ever seen
  on a wire; it is the reference implementation of the documents.
* :data:`FX5U_32MT_DS` -- what our silicon actually does, measured on firmware 1.065
  across 2026-09-06 and 2026-09-07. **Three end codes are corrections** to the
  doc-derived mapping: ``0xC05C`` (not ``0xC05B``), ``0xC052`` (not ``0xC056``) and
  ``0xC061`` (not ``0xC057``). The bit ceiling is 3584, exactly half the generic figure.
  Monitoring and the long device specification return ``0xC059``.
* :data:`R04CPU` -- built from the manuals and **labelled unverified**, because there is
  no iQ-R in the building. :attr:`SimulatorTarget.verified` is ``False`` and
  :meth:`SimulatorTarget.warn_if_unverified` returns the sentence a report must print.

Running the same conformance suite against :data:`PEDANTIC` and :data:`FX5U_32MT_DS` and
diffing the results is a document: it is exactly the list of behaviours this library has
that exist only to accommodate real silicon. :func:`diff_targets` renders it directly.

**Device *ranges* are read from the profile**, through
:func:`~aslmp.testing.memory.ranges_from_profile`, rather than restated: a range is
generated ground-truth data in
``aslmp/data/ranges_*.tsv`` and a second hand-typed copy would drift. Everything that is
*behaviour* -- ceilings, end codes, capability refusals, notation -- is declared here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from aslmp.profile import ClearMode, CpuProfile, Encoding
from aslmp.profiles import FX5U, IQ_R
from aslmp.testing.memory import DeviceMemory, ranges_from_profile
from aslmp.testing.pathology import FX5U_MEASURED, HEALTHY, Pathology
from aslmp.wire.citations import Citation, Measurement, Provenance
from aslmp.wire.codec import SpecFormat

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Mapping

    from aslmp.wire.citations import Source

__all__ = [
    "ALL_TARGETS",
    "FX5U_32MT_DS",
    "PEDANTIC",
    "R04CPU",
    "EndCodePolicy",
    "LimitPolicy",
    "SimulatorTarget",
    "TargetDiff",
    "by_key",
    "diff_targets",
]


SLMP_REFERENCE: Final = Citation(
    manual="SH(NA)-080956ENG",
    revision="M",
    section="chapters 5-7 and appendix 3",
    provenance=Provenance.MANUAL,
    note=(
        "The SLMP reference manual: device codes, command layouts, point ceilings and "
        "the end-code table, for the protocol rather than for any one CPU."
    ),
)
"""What :data:`PEDANTIC` implements, and nothing else."""

FX5_MANUAL: Final = Citation(
    manual="JY997D56001",
    revision="K",
    section="pp.66-78, p.111",
    provenance=Provenance.MANUAL,
    note="The MELSEC iQ-F SLMP reference: FX5 device ranges, point ceilings and exclusions.",
)
"""The iQ-F-specific document, which the silicon still departs from in three places."""

FX5U_END_CODES: Final = Measurement(
    cpu="FX5U-32MT/DS",
    firmware="1.065",
    date="2026-09-06",
    note=(
        "Sixteen provoked abnormal responses, every one exactly 20 bytes with L = 0x000B "
        "and no command-defined error data. Three corrections to the doc-derived "
        "mapping: 0xC05C (not 0xC05B) for a device family the CPU does not have and for "
        "a garbage device code; 0xC052 (not 0xC056) for a ZERO point count, which is a "
        "point-count error and not an address error; 0xC061 (not 0xC057) for a request "
        "data length mismatch. D8000 -> 0xC056 and D7999 with two points -> 0xC056, so "
        "D0..D7999 is the confirmed span and it is the span that is validated."
    ),
)
"""The end-code corrections that make :data:`FX5U_32MT_DS` differ from :data:`PEDANTIC`."""

FX5U_LIMITS: Final = Measurement(
    cpu="FX5U-32MT/DS",
    firmware="1.065",
    date="2026-09-06",
    note=(
        "Binary-searched on hardware: 0401 word units 960 (961 -> 0xC052); 0401 bit units "
        "3584 (3585 -> 0xC051), exactly HALF the generic SLMP reference ceiling of 7168; "
        "0403 word+double-word access points 192 (193 -> 0xC054). Reconfirmed over UDP, "
        "so the ceilings are protocol limits and not MTU limits: a 960-point read is a "
        "1935-byte datagram."
    ),
)
"""Why a client that ships the generic reference's numbers builds frames this CPU rejects."""

FX5U_CAPABILITIES: Final = Measurement(
    cpu="FX5U-32MT/DS",
    firmware="1.065",
    date="2026-09-06",
    note=(
        "Subcommand 0x0002, the long device specification, returned 0xC059. Monitor "
        "Registration 0x0801 and Execute Monitor 0x0802 returned 0xC059 -- not the "
        "0xC05D 'monitor not registered' a reader of the generic reference would expect. "
        "An unknown command 0x9999 also returned 0xC059, with 99 99 echoed in the error "
        "information block."
    ),
)
"""Why the iQ-F target answers ``0xC059`` where the reference target serves the request."""

IQ_R_UNVERIFIED: Final = Citation(
    manual="SH(NA)-081263ENG",
    revision="AA",
    section="2.1 performance specifications; SH(NA)-080956ENG-M for the protocol",
    provenance=Provenance.MANUAL,
    note=(
        "Built from the documents. NO iQ-R HAS EVER BEEN CONNECTED TO THIS LIBRARY. "
        "Every behaviour this target exhibits is a reading of a manual, and the three "
        "places the FX5U departed from its own manual are the reason that distinction is "
        "printed rather than assumed."
    ),
)
"""The label :data:`R04CPU` carries into every report it appears in."""


@dataclass(frozen=True, slots=True)
class EndCodePolicy:
    """Which end code a target answers for each way a request can be wrong.

    Every field is a 16-bit end code as it appears on the wire (little-endian in binary,
    four upper-case hex characters in ASCII). The three that differ between
    :data:`PEDANTIC` and :data:`FX5U_32MT_DS` are ``unknown_device_code`` /
    ``device_absent`` (``0xC05B`` against ``0xC05C``), ``zero_point_count`` (``0xC056``
    against ``0xC052``) and ``request_length_mismatch`` (``0xC057`` against ``0xC061``),
    and those three are the whole reason this is a policy object rather than a module of
    constants.

    ``password_locked`` is what every command but ``1630`` answers while ``1631`` holds
    the port locked -- "nothing else can be done until the port is unlocked"
    (SH(NA)-081257ENG rev AD, 3.5 List of Error Codes). No CPU here has ever been asked
    for it on a wire: every target ships an empty password, so it is a manual figure and
    is marked as one wherever a report prints provenance.
    """

    unsupported_command: int = 0xC059
    unsupported_subcommand: int = 0xC059
    unknown_device_code: int = 0xC05B
    device_absent: int = 0xC05B
    device_not_allowed_here: int = 0xC05B
    address_out_of_range: int = 0xC056
    zero_point_count: int = 0xC056
    batch_word_limit: int = 0xC052
    batch_bit_limit: int = 0xC051
    random_point_limit: int = 0xC054
    random_bit_point_limit: int = 0xC053
    block_count_limit: int = 0xC0D8
    request_length_mismatch: int = 0xC057
    monitor_not_registered: int = 0xC05D
    illegal_random_device: int = 0x4032
    ascii_into_binary_entry: int = 0xC06F
    remote_control_disabled: int = 0x408B
    password_incorrect: int = 0xC200
    password_locked: int = 0xC201

    def __post_init__(self) -> None:
        for name, value in self.as_mapping().items():
            if not 0 <= value <= 0xFFFF or value == 0:
                raise ValueError(
                    f"EndCodePolicy.{name} = {value!r} is not a non-zero 16-bit end "
                    f"code. 0x0000 is normal completion and can never be a refusal."
                )

    def as_mapping(self) -> Mapping[str, int]:
        """Every field, for a diff or a report."""
        return {
            "unsupported_command": self.unsupported_command,
            "unsupported_subcommand": self.unsupported_subcommand,
            "unknown_device_code": self.unknown_device_code,
            "device_absent": self.device_absent,
            "device_not_allowed_here": self.device_not_allowed_here,
            "address_out_of_range": self.address_out_of_range,
            "zero_point_count": self.zero_point_count,
            "batch_word_limit": self.batch_word_limit,
            "batch_bit_limit": self.batch_bit_limit,
            "random_point_limit": self.random_point_limit,
            "random_bit_point_limit": self.random_bit_point_limit,
            "block_count_limit": self.block_count_limit,
            "request_length_mismatch": self.request_length_mismatch,
            "monitor_not_registered": self.monitor_not_registered,
            "illegal_random_device": self.illegal_random_device,
            "ascii_into_binary_entry": self.ascii_into_binary_entry,
            "remote_control_disabled": self.remote_control_disabled,
            "password_incorrect": self.password_incorrect,
            "password_locked": self.password_locked,
        }


@dataclass(frozen=True, slots=True)
class LimitPolicy:
    """The point ceilings a target enforces, per coding.

    Per coding because they are measured per coding: an FX5U-32MT/DS serves 960 word
    points in binary and the FX5 manual's ASCII column halves that to 480. A client that
    computes an ASCII budget by dividing the binary one by two happens to be right here
    and is wrong the moment a family does not halve, which is why both numbers are
    written down.
    """

    batch_word: int
    batch_bit: int
    random_points: int
    random_bit_points: int
    write_random_word_weight: int
    write_random_dword_weight: int
    write_random_budget: int
    max_blocks: int
    read_block_points: int
    write_block_points: int
    ascii_batch_word: int
    ascii_batch_bit: int
    ascii_random_points: int

    def batch_word_for(self, encoding: Encoding) -> int:
        """The ``0401``/``1401`` word ceiling for this coding."""
        return self.ascii_batch_word if encoding.is_ascii else self.batch_word

    def batch_bit_for(self, encoding: Encoding) -> int:
        """The ``0401``/``1401`` bit ceiling for this coding."""
        return self.ascii_batch_bit if encoding.is_ascii else self.batch_bit

    def random_points_for(self, encoding: Encoding) -> int:
        """The ``0403`` access-point ceiling for this coding."""
        return self.ascii_random_points if encoding.is_ascii else self.random_points

    def as_mapping(self) -> Mapping[str, int]:
        """Every ceiling, for a diff or a report."""
        return {
            "batch_word": self.batch_word,
            "batch_bit": self.batch_bit,
            "random_points": self.random_points,
            "random_bit_points": self.random_bit_points,
            "write_random_word_weight": self.write_random_word_weight,
            "write_random_dword_weight": self.write_random_dword_weight,
            "write_random_budget": self.write_random_budget,
            "max_blocks": self.max_blocks,
            "read_block_points": self.read_block_points,
            "write_block_points": self.write_block_points,
            "ascii_batch_word": self.ascii_batch_word,
            "ascii_batch_bit": self.ascii_batch_bit,
            "ascii_random_points": self.ascii_random_points,
        }


@dataclass(frozen=True, slots=True)
class SimulatorTarget:
    """One CPU's worth of declared behaviour: what it serves, refuses and gets wrong.

    ``profile`` supplies device *ranges* only. Everything the simulator decides -- point
    ceilings, end codes, which capabilities exist, whether ``X``/``Y`` ASCII device
    numbers are octal -- is declared on this object, so that a conformance run compares
    two independent statements rather than one statement with itself.
    """

    key: str
    label: str
    verified: bool
    evidence: Source
    model_name: str
    model_code: int
    profile: CpuProfile
    limits: LimitPolicy
    end_codes: EndCodePolicy
    pathology: Pathology
    long_device_spec: bool
    monitor: bool
    block_access: bool
    remote_control: bool
    remote_reset: bool
    remote_password: bool
    clear_error: bool
    four_e_frames: bool
    xy_ascii_octal: bool
    remote_fixed_field: bytes
    clear_modes: frozenset[ClearMode]
    password: str = ""
    sources: tuple[Source, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if len(self.model_name) > 16:
            raise ValueError(
                f"the 0101 model name field is 16 characters in both codings "
                f"(SH(NA)-080956ENG-M p.137); {self.model_name!r} is "
                f"{len(self.model_name)}"
            )
        if not 0 <= self.model_code <= 0xFFFF:
            raise ValueError(f"model code 0x{self.model_code:X} is not 16-bit")
        if len(self.remote_fixed_field) != 2:
            raise ValueError(
                "the 1002/1005/1006 fixed field is two wire units; an iQ-F writes "
                "00 00 and the SLMP reference families write 01 00"
            )

    # -- what the 0101 response carries -------------------------------------------

    @property
    def model_name_field(self) -> bytes:
        """The 16 ASCII characters of the ``0101`` model-name field, space padded.

        Text, not a number: ASCII coding does **not** double it, while the model code
        that follows it is a numeric field and does (SH(NA)-080956ENG-M p.137).
        """
        return self.model_name.ljust(16).encode("ascii")

    # -- capability questions the dispatcher asks ----------------------------------

    def supports_spec(self, spec: SpecFormat) -> bool:
        """Whether this CPU serves the given device-specification width."""
        return spec is SpecFormat.SHORT or self.long_device_spec

    def accepts_random_device(
        self, device: str, *, random_ok: bool, pathology: Pathology | None = None
    ) -> bool:
        """Whether a ``0403``/``1402`` access point at ``device`` is served.

        ``random_ok`` is the generic device table's own column. A target may override it
        upward through :attr:`Pathology.accept_illegal_random_points`, which is how the
        measured ``TS0`` acceptance is reproduced -- and how a test proves the client
        refuses what this PLC would have allowed.

        ``pathology`` is the board **in force**, which a
        :class:`~aslmp.testing.server.PlcSimulator` may have been handed instead of this
        target's own. Omit it and the target's own board answers, as it always did.
        """
        board = self.pathology if pathology is None else pathology
        return random_ok or device in board.accept_illegal_random_points

    def memory(self) -> DeviceMemory:
        """Fresh device memory shaped by this target's profile ranges."""
        return DeviceMemory(ranges_from_profile(self.profile))

    def warn_if_unverified(self) -> str:
        """The sentence a report prints for a target nobody has ever plugged in."""
        if self.verified:
            return ""
        return (
            f"{self.label} is UNVERIFIED: it is built from documents and no such CPU has "
            f"ever been connected to this library. The FX5U-32MT/DS departed from its own "
            f"manual in three end codes and one point ceiling, so treat every expectation "
            f"here as a reading rather than a fact."
        )

    def cites(self) -> tuple[Source, ...]:
        """Everything behind this target's declared behaviour."""
        out: list[Source] = [self.evidence]
        for source in (*self.sources, *self.pathology.cites()):
            if source not in out:
                out.append(source)
        return tuple(out)

    def __str__(self) -> str:
        return f"{self.label}{'' if self.verified else ' (unverified)'}"


# ----------------------------------------------------------------------------------------
# The three targets
# ----------------------------------------------------------------------------------------

_REFERENCE_LIMITS: Final = LimitPolicy(
    batch_word=960,
    batch_bit=7168,
    random_points=192,
    random_bit_points=188,
    write_random_word_weight=12,
    write_random_dword_weight=14,
    write_random_budget=1920,
    max_blocks=120,
    read_block_points=960,
    write_block_points=960,
    ascii_batch_word=960,
    ascii_batch_bit=7168,
    ascii_random_points=192,
)
"""SH(NA)-080956ENG-M's own figures, the same in both codings because the reference
manual states point counts and not character budgets."""

PEDANTIC: Final = SimulatorTarget(
    key="pedantic",
    label="PEDANTIC (the manual as written)",
    verified=True,
    evidence=SLMP_REFERENCE,
    model_name="SLMP-REFERENCE",
    model_code=0x0360,
    profile=IQ_R,
    limits=_REFERENCE_LIMITS,
    end_codes=EndCodePolicy(),
    pathology=HEALTHY,
    long_device_spec=True,
    monitor=True,
    block_access=True,
    remote_control=True,
    remote_reset=True,
    remote_password=True,
    clear_error=True,
    four_e_frames=True,
    xy_ascii_octal=False,
    remote_fixed_field=b"\x01\x00",
    clear_modes=frozenset(ClearMode),
    sources=(SLMP_REFERENCE,),
)
"""The SLMP reference implemented exactly, with no measured misbehaviour at all.

``verified=True`` here means "this target faithfully implements the document", not "a CPU
was observed doing this". Model code ``0x0360`` is the reference manual's own RCPU
catch-all, which identifies a family rather than a model, and which no shipped profile
claims -- so a client that connects here and runs the identify handshake gets
``SlmpProfileMismatchError``, correctly.
"""

FX5U_32MT_DS: Final = SimulatorTarget(
    key="fx5u-32mt-ds",
    label="FX5U-32MT/DS fw 1.065",
    verified=True,
    evidence=FX5U_END_CODES,
    model_name="FX5U-32MT/DS",
    model_code=0x4A49,
    profile=FX5U,
    limits=LimitPolicy(
        batch_word=960,
        batch_bit=3584,
        random_points=192,
        random_bit_points=188,
        write_random_word_weight=12,
        write_random_dword_weight=14,
        write_random_budget=1920,
        max_blocks=120,
        read_block_points=960,
        write_block_points=760,
        ascii_batch_word=480,
        ascii_batch_bit=1792,
        ascii_random_points=96,
    ),
    end_codes=EndCodePolicy(
        unknown_device_code=0xC05C,
        device_absent=0xC05C,
        device_not_allowed_here=0xC05C,
        zero_point_count=0xC052,
        request_length_mismatch=0xC061,
        illegal_random_device=0xC05C,
    ),
    pathology=FX5U_MEASURED,
    long_device_spec=False,
    monitor=False,
    block_access=True,
    remote_control=True,
    remote_reset=True,
    remote_password=True,
    clear_error=True,
    four_e_frames=True,
    xy_ascii_octal=True,
    remote_fixed_field=b"\x00\x00",
    clear_modes=frozenset({ClearMode.NONE}),
    sources=(FX5U_END_CODES, FX5U_LIMITS, FX5U_CAPABILITIES, FX5_MANUAL),
)
"""Our silicon: the CI default, reproducing the 2026-09-06/07 measurements exactly.

``block_access`` is ``True`` because the FX5 manual documents ``0406``/``1406``, and it is
the one capability here that was **never exercised on the bench** -- the read block
ceiling is ``Provenance.MANUAL`` and the write block figure is ``Provenance.INFERRED``.
Everything else on this target was provoked on a wire.
"""

R04CPU: Final = SimulatorTarget(
    key="r04cpu",
    label="R04CPU (manual-derived, UNVERIFIED)",
    verified=False,
    evidence=IQ_R_UNVERIFIED,
    model_name="R04CPU",
    model_code=0x4800,
    profile=IQ_R,
    limits=_REFERENCE_LIMITS,
    end_codes=EndCodePolicy(),
    pathology=HEALTHY,
    long_device_spec=True,
    monitor=True,
    block_access=True,
    remote_control=True,
    remote_reset=True,
    remote_password=True,
    clear_error=True,
    four_e_frames=True,
    xy_ascii_octal=False,
    remote_fixed_field=b"\x01\x00",
    clear_modes=frozenset(ClearMode),
    sources=(IQ_R_UNVERIFIED, SLMP_REFERENCE),
)
"""An iQ-R built entirely from documents. There is no iQ-R in the building.

It differs from :data:`PEDANTIC` in the two things a real model has and a reference does
not: a model name and a model code that :func:`aslmp.profiles.by_model_code` resolves. It
does **not** claim to be what an R04CPU does; :meth:`SimulatorTarget.warn_if_unverified`
says so in every report.
"""

ALL_TARGETS: Final[Mapping[str, SimulatorTarget]] = {
    PEDANTIC.key: PEDANTIC,
    FX5U_32MT_DS.key: FX5U_32MT_DS,
    R04CPU.key: R04CPU,
}
"""Every shipped target, by key."""


def by_key(key: str) -> SimulatorTarget:
    """The target named ``key``, or raise listing the ones that exist."""
    found = ALL_TARGETS.get(key)
    if found is not None:
        return found
    raise KeyError(
        f"{key!r} is not a simulator target. Available: {sorted(ALL_TARGETS)}. There is "
        f"no default and no nearest match: a target is a claim about how a CPU behaves."
    )


# ----------------------------------------------------------------------------------------
# The diff, which is the document
# ----------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TargetDiff:
    """Where two targets part, as data and as a printable table."""

    left: SimulatorTarget
    right: SimulatorTarget
    rows: tuple[tuple[str, str, str], ...]

    def __bool__(self) -> bool:
        return bool(self.rows)

    def __len__(self) -> int:
        return len(self.rows)

    def names(self) -> tuple[str, ...]:
        """Every differing behaviour, by name.

        Not ``keys()``: this is not a mapping, and calling it one invites a caller to
        subscript it with a behaviour name and get a row index.
        """
        return tuple(row[0] for row in self.rows)

    def to_markdown(self) -> str:
        """The table a reviewer reads: one row per behaviour that differs.

        Running the conformance suite against both targets and diffing is the same
        document arrived at empirically; this is it stated declaratively, and a test
        asserts the two agree on the count of differing behaviours.
        """
        head = (
            f"# {self.left.label} vs {self.right.label}\n\n"
            f"{len(self.rows)} behaviour(s) differ. Every row is a place this library "
            f"needs a per-CPU answer, and a library with one global answer is wrong on "
            f"one side of it.\n\n"
            f"| behaviour | {self.left.label} | {self.right.label} |\n"
            f"| --- | --- | --- |\n"
        )
        body = "".join(f"| `{name}` | {left} | {right} |\n" for name, left, right in self.rows)
        notes = []
        for target in (self.left, self.right):
            warning = target.warn_if_unverified()
            if warning:
                notes.append(f"\n> {warning}\n")
        return head + body + "".join(notes)

    def __str__(self) -> str:
        return self.to_markdown()


def _hex(value: int) -> str:
    return f"0x{value:04X}"


def diff_targets(left: SimulatorTarget, right: SimulatorTarget) -> TargetDiff:
    """Every declared behaviour on which ``left`` and ``right`` disagree.

    ``diff_targets(PEDANTIC, FX5U_32MT_DS).to_markdown()`` is the readable document of
    where silicon and manual part: the three corrected end codes, the halved bit ceiling,
    the two capabilities that answer ``0xC059``, the octal ``X``/``Y`` notation, the
    ``00 00`` remote fixed field, the single clear mode, and every pathology switch our
    CPU has on and the reference does not.

    .. rubric:: What this document is not

    It is a diff of **declared CPU behaviour**, and that is the whole of its scope. Two
    kinds of rig-specific truth sit outside it and cannot appear here however wrong they
    get, so neither an empty row nor a matching row is evidence about them:

    * **What a register holds.** ``D8`` on our bench is ``IO_Scan``, a ``REAL`` the
      program advances by 1.0 and resets above 1.0e7 -- see
      :meth:`~aslmp.testing.memory.DeviceMemory.bump_f32`. That is a property of the
      *program* on that CPU, not of the CPU model, and the simulator having modelled it
      as an integer double word until 2026-09-07 would not have moved a single row here.
    * **The board actually in force.** These rows compare each target's *own*
      :class:`~aslmp.testing.pathology.Pathology`. A
      :class:`~aslmp.testing.server.PlcSimulator` may have been handed a different one,
      and what that simulator does is that board's behaviour, not this table's.
    """
    rows: list[tuple[str, str, str]] = []

    left_codes = left.end_codes.as_mapping()
    right_codes = right.end_codes.as_mapping()
    for name in left_codes:
        if left_codes[name] != right_codes[name]:
            rows.append((f"end_code.{name}", _hex(left_codes[name]), _hex(right_codes[name])))

    left_limits = left.limits.as_mapping()
    right_limits = right.limits.as_mapping()
    for name in left_limits:
        if left_limits[name] != right_limits[name]:
            rows.append((f"limit.{name}", str(left_limits[name]), str(right_limits[name])))

    flags = (
        "long_device_spec",
        "monitor",
        "block_access",
        "remote_control",
        "remote_reset",
        "remote_password",
        "clear_error",
        "four_e_frames",
        "xy_ascii_octal",
        "verified",
    )
    for name in flags:
        left_value = getattr(left, name)
        right_value = getattr(right, name)
        if left_value != right_value:
            rows.append((name, str(left_value), str(right_value)))

    if left.remote_fixed_field != right.remote_fixed_field:
        rows.append(
            (
                "remote_fixed_field",
                left.remote_fixed_field.hex(" ").upper(),
                right.remote_fixed_field.hex(" ").upper(),
            )
        )
    if left.clear_modes != right.clear_modes:
        rows.append(
            (
                "clear_modes",
                ", ".join(sorted(mode.name for mode in left.clear_modes)),
                ", ".join(sorted(mode.name for mode in right.clear_modes)),
            )
        )
    if left.profile.key != right.profile.key:
        rows.append(("device_ranges", left.profile.key, right.profile.key))

    left_active = set(left.pathology.active)
    right_active = set(right.pathology.active)
    for name in sorted(left_active | right_active):
        left_value = getattr(left.pathology, name)
        right_value = getattr(right.pathology, name)
        if left_value != right_value:
            rows.append((f"pathology.{name}", _render(left_value), _render(right_value)))

    return TargetDiff(left=left, right=right, rows=tuple(rows))


def _render(value: object) -> str:
    if isinstance(value, frozenset):
        return "{" + ", ".join(sorted(str(item) for item in value)) + "}" if value else "none"
    return str(value)
