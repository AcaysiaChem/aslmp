"""Declarative command dispatch: request bytes in, an end code and response data out.

Layer 2.5 (``aslmp.testing``). No sockets and no asyncio -- this module is a pure
function of a decoded request, so every command the simulator serves is unit-testable
with no server running at all.

**The request decoders here are written independently of ``aslmp.commands``.** They are
deliberately not ``encode()`` run backwards: two directions that share one function agree
by construction and prove nothing, which is exactly the trap the whole
simulator-does-not-import-transport rule exists to avoid one layer up. What the two sides
*do* share is :mod:`aslmp.wire.codec` and the generated device table, and that is the
named residual risk of DESIGN section 7, whose only real mitigation is the golden-vector
corpus.

:data:`HANDLERS` is keyed by command code and a test asserts it covers every
external-device-originated row of :data:`aslmp.commands.registry.COMMANDS`. Adding a
command to the library without teaching the simulator to serve it fails the build.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, Literal

from aslmp.commands.registry import COMMANDS
from aslmp.testing.memory import (
    BENCH_SCAN_STEP,
    BENCH_SCAN_WRAP,
    AbsentDeviceError,
    OutOfRangeError,
    SimulatorMemoryError,
)
from aslmp.wire.codec import (
    ASCII,
    Notation,
    SlmpCodecError,
    SpecFormat,
    Unit,
    parse_ascii_device_code,
)
from aslmp.wire.devicetable import DEVICE_TABLE, DeviceType
from aslmp.wire.devspec import emit_base
from aslmp.wire.subcommand import decode_subcommand

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable, Mapping, Sequence

    from aslmp.profile import Encoding
    from aslmp.testing.memory import DeviceMemory
    from aslmp.testing.pathology import Pathology
    from aslmp.testing.scenario import Scenario
    from aslmp.testing.targets import SimulatorTarget
    from aslmp.wire.codec import Codec
    from aslmp.wire.raw import RawRequest

__all__ = [
    "CPU_STATUS_DEVICE",
    "CPU_STATUS_INDEX",
    "HANDLERS",
    "PASSWORD_EXEMPT_COMMANDS",
    "CpuRunState",
    "DecodedDevice",
    "Dispatcher",
    "Outcome",
    "PayloadCursor",
    "RefusalError",
    "Reply",
    "ServerContext",
    "SessionState",
    "Silence",
    "effective_cpu_state",
    "publish_cpu_state",
    "registered_request_codes",
]


# ----------------------------------------------------------------------------------------
# Outcomes
# ----------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Reply:
    """The simulated CPU answers. ``end_code`` 0 carries data; anything else does not."""

    end_code: int
    payload: bytes = b""
    close_after: bool = False

    def __post_init__(self) -> None:
        if self.end_code and self.payload:
            raise ValueError(
                f"end code 0x{self.end_code:04X} is abnormal, so the frame carries an "
                f"error information block and no response data (SH(NA)-080956ENG-M p.28)"
            )


@dataclass(frozen=True, slots=True)
class Silence:
    """The simulated CPU answers nothing at all, and says why in the transcript.

    Three measured causes and one documented one: a coding mismatch, a frame-type
    mismatch, an overstated data length, and a successful Remote Reset, which
    SH(NA)-080956ENG-M p.136 says produces no response. Every one of them looks
    identical from the client's socket, which is why the timeout has to rank its likely
    causes from context.
    """

    reason: str
    close_after: bool = False


Outcome = Reply | Silence
"""What a handler returns."""


class RefusalError(Exception):
    """Internal: a request this CPU answers with an abnormal end code.

    Carries the code the *target* declares for that class of refusal, so the same
    handler serves ``0xC05B`` on :data:`~aslmp.testing.targets.PEDANTIC` and the
    measured ``0xC05C`` on :data:`~aslmp.testing.targets.FX5U_32MT_DS`.
    """

    def __init__(self, end_code: int, detail: str) -> None:
        super().__init__(f"0x{end_code:04X}: {detail}")
        self.end_code = end_code
        self.detail = detail


# ----------------------------------------------------------------------------------------
# Mutable CPU state
# ----------------------------------------------------------------------------------------


class CpuRunState:
    """The four states SD203 reports. A plain class so the values are the wire values."""

    RUN: Final = 0x0000
    STEP_RUN: Final = 0x0001
    STOP: Final = 0x0002
    PAUSE: Final = 0x0003


CPU_STATUS_DEVICE: Final = "SD"
CPU_STATUS_INDEX: Final = 203
"""Where a simulated CPU publishes its operating status: ``SD203``, an ordinary word.

Spelled out here rather than imported from :data:`aslmp.identity.SD203`, for the reason
this module's decoders are not ``encode()`` run backwards: if the client reads the
register the simulator writes *because both took the number out of one constant*, then a
test proving a client can see a Remote STOP proves only that the two halves share a
variable. ``tests/unit/test_simulator_state.py`` asserts the two spellings agree, which
is a comparison rather than an identity.
"""

PASSWORD_EXEMPT_COMMANDS: Final[frozenset[int]] = frozenset({0x1630})
"""What a locked CPU still serves: ``1630`` Remote Password Unlock, and nothing else.

"The remote password status of the port used for communications is locked... Nothing else
can be done until the port is unlocked" -- 0xC201, SH(NA)-081257ENG rev AD, 3.5 List of
Error Codes. ``1631`` is not exempt: locking an already-locked port is one of the things
that cannot be done.
"""


@dataclass(slots=True)
class SessionState:
    """What a simulated CPU remembers between requests.

    Mutable, and shared across every connection to one simulator, because that is what a
    CPU is: SLMP issues no registration handle, so two clients registering a monitor list
    clobber each other, and reproducing that is the point.

    .. rubric:: Every field here is visible to a client, and a test enforces it

    A field a handler writes and nothing can read is worse than an unimplemented handler,
    because it looks implemented. Until 2026-09-07 six of the seven fields here were
    write-only or never touched at all -- ``monitor_points`` was the only one a client
    could see -- and the damage was not theoretical: ``run_state`` was written by ``1001``,
    ``1002`` and ``1003`` and read by nothing, ``SD203`` was never derived from it, so a
    Remote STOP that answered ``0x0000`` left ``read_cpu_status()`` reporting RUN and left
    the scan counter counting -- and every verified-remote test in the suite set ``SD203``
    to the answer it wanted by hand beforehand, which is to say each of them would have
    passed against a handler that returned ``0x0000`` and did nothing.

    So: ``SD203`` in device memory is the CPU's operating status, and this class holds
    only the *inputs* to it. ``tests/unit/test_simulator_state.py`` refuses any field
    here that a client cannot distinguish two values of through requests alone.

    .. rubric:: What is deliberately not here

    * ``run_state`` -- replaced by :attr:`remote_request` plus ``SD203``, above.
    * ``error_flag`` -- set to ``False`` by ``1617`` Clear Error, never set to ``True``
      by anything, and readable by nothing. This CPU model has no error latch; a boolean
      pretending otherwise made ``1617`` look modelled when it is only answered.
    * ``served`` -- a request count no SLMP command returns.
      :attr:`~aslmp.testing.server.PlcSimulator.transcript` already holds every request,
      and it holds the bytes rather than a number.
    * ``scan`` -- a mirror of ``D8``/``D9``, which is where the counter actually lives.
      Two copies of one number is one copy too many; read
      :meth:`~aslmp.testing.memory.DeviceMemory.get_f32` (and note that register is a
      ``REAL``, not a ``u32``).
    """

    switch_position: int = CpuRunState.RUN
    """The RUN/STOP key switch: a physical contact, and the first input to ``SD203``.

    A client can neither move it nor read it, and it is still not dead configuration --
    it decides whether a Remote RUN takes. SH(NA)-080956ENG-M 6.9 Remote RUN, p.131: with
    the switch in STOP a Remote RUN "will be completed normally. However, the access
    destination does not become the RUN state." End code ``0x0000`` on a state that was
    never reached is precisely why ``verify=True`` is the client's default, and with this
    field wired to ``SD203`` the simulator can finally produce it.
    """

    remote_request: int = CpuRunState.RUN
    """What the last ``1001``/``1002``/``1003`` asked for: the *request*, never the answer.

    The answer is ``SD203``, and it is :func:`effective_cpu_state` of this and
    :attr:`switch_position`. Keeping the request separate from the answer is what lets
    the key switch overrule it without either one becoming a lie.
    """

    monitor_points: tuple[tuple[DeviceType, int, Unit, int], ...] | None = None
    """The registered ``0801`` list as ``(device, index, unit, words)``, or ``None``.

    ``None`` -- never registered -- is not the same as an empty list, and ``0802``
    against ``None`` is ``0xC05D``.
    """

    password_locked: bool = False
    """Whether ``1631`` has locked this port. Everything but ``1630`` then answers ``0xC201``.

    Setting this and serving every request anyway is what it used to do, which made
    ``1631`` a no-op with bookkeeping. See :data:`PASSWORD_EXEMPT_COMMANDS`.
    """


def effective_cpu_state(state: SessionState) -> int:
    """What ``SD203`` must say: the key switch first, then the last remote request.

    The switch wins because it is a physical contact and ``1001`` is a request over an
    unauthenticated socket. That asymmetry is the documented behaviour, not a choice made
    here (SH(NA)-080956ENG-M 6.9, p.131).
    """
    if state.switch_position == CpuRunState.STOP:
        return CpuRunState.STOP
    return state.remote_request


def publish_cpu_state(memory: DeviceMemory, state: SessionState) -> int:
    """Write :func:`effective_cpu_state` into ``SD203`` and return what it now holds.

    Called at the top of every :meth:`Dispatcher.handle`, because a real CPU checks its
    key switch every scan and because that is what makes a hand-written ``SD203`` a
    transient rather than a way for a test to fake the answer it is about to assert.
    """
    value = effective_cpu_state(state)
    memory.set_u16(CPU_STATUS_DEVICE, CPU_STATUS_INDEX, value)
    return value


# ----------------------------------------------------------------------------------------
# Reading a request payload
# ----------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DecodedDevice:
    """One device specification block, as the simulated CPU understood it."""

    type: DeviceType
    index: int

    def __str__(self) -> str:
        return f"{self.type.name}{self.index}"


class PayloadCursor:
    """Reads a request payload field by field, refusing anything that does not fit.

    A short payload is a **length** error and not a decode error: an FX5U-32MT/DS on
    firmware 1.065 answered a request whose ``L`` was understated by two with ``0xC061``
    and recovered the connection (measured 2026-09-06), and this is where that happens.
    Nothing here pads, and nothing reads past the end.
    """

    __slots__ = ("_codec", "_length_error", "_offset", "_payload")

    def __init__(self, payload: bytes, codec: Codec, *, length_error: int) -> None:
        self._payload = payload
        self._codec = codec
        self._offset = 0
        self._length_error = length_error

    @property
    def offset(self) -> int:
        """How many wire units have been consumed."""
        return self._offset

    @property
    def remaining(self) -> int:
        """How many wire units are left."""
        return len(self._payload) - self._offset

    def take(self, count: int) -> bytes:
        """``count`` raw wire units, or refuse with the target's length end code."""
        if count < 0 or self.remaining < count:
            raise RefusalError(
                self._length_error,
                f"the request data ran out after {self._offset} wire unit(s): "
                f"{count} more were needed and {self.remaining} remain",
            )
        chunk = self._payload[self._offset : self._offset + count]
        self._offset += count
        return chunk

    def number(self, bits: Literal[8, 16, 32]) -> int:
        """One numeric field, in this coding."""
        raw = self.take(self._codec.number_len(bits))
        try:
            return self._codec.read_number(raw, 0, bits=bits)
        except SlmpCodecError as exc:
            raise RefusalError(
                self._length_error,
                f"a {bits}-bit field at offset {self._offset - len(raw)} does not "
                f"decode in {self._codec.name}: {exc}",
            ) from exc

    def words(self, count: int) -> tuple[int, ...]:
        """``count`` word-unit data points."""
        raw = self.take(self._codec.word_data_len(count))
        try:
            return self._codec.read_words(raw, 0, count)
        except SlmpCodecError as exc:
            raise RefusalError(self._length_error, f"word data does not decode: {exc}") from exc

    def bits(self, count: int) -> tuple[bool, ...]:
        """``count`` bit-unit data points, one nibble each in binary."""
        raw = self.take(self._codec.bit_data_len(count))
        try:
            return self._codec.read_bits(raw, 0, count)
        except SlmpCodecError as exc:
            raise RefusalError(self._length_error, f"bit data does not decode: {exc}") from exc

    def finish(self) -> None:
        """Refuse a payload with bytes left over.

        Surplus request data means the frame does not say what this command says it
        says. Ignoring it is how an understated length turns into a request for a
        different span that completes normally.
        """
        if self.remaining:
            raise RefusalError(
                self._length_error,
                f"{self.remaining} wire unit(s) of request data follow the fields this "
                f"command defines",
            )


_BINARY_SHORT_CODES: Final[Mapping[int, DeviceType]] = {
    dt.code_short: dt for dt in DEVICE_TABLE.values() if dt.code_short is not None
}
_BINARY_LONG_CODES: Final[Mapping[int, DeviceType]] = {
    dt.code_long: dt for dt in DEVICE_TABLE.values()
}
_ASCII_SHORT_CODES: Final[Mapping[str, DeviceType]] = {
    dt.ascii2.rstrip("*"): dt for dt in DEVICE_TABLE.values() if dt.ascii2
}
_ASCII_LONG_CODES: Final[Mapping[str, DeviceType]] = {
    dt.ascii4.rstrip("*"): dt for dt in DEVICE_TABLE.values() if dt.ascii4
}
"""Reverse device-code tables. The generated forward table has no duplicate codes or
mnemonics in any of the four columns, and a test asserts that stays true."""


# ----------------------------------------------------------------------------------------
# The context one request is served in
# ----------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ServerContext:
    """Everything a handler is allowed to see: no socket, no clock, no transcript."""

    target: SimulatorTarget
    memory: DeviceMemory
    state: SessionState
    codec: Codec
    encoding: Encoding
    spec: SpecFormat
    unit: Unit
    command: int
    subcommand: int
    pathology: Pathology
    """The board **in force**, which is not always ``target.pathology``.

    :class:`~aslmp.testing.server.PlcSimulator` takes a ``pathology=`` argument and its
    socket layer honours it. Until 2026-09-07 the handlers read ``target.pathology``
    instead, so the three switches that live down here -- ``remote_run_lies``,
    ``remote_reset_no_response`` and ``accept_illegal_random_points`` -- silently ignored
    it: a test that turned one on the documented way got a CPU that did not misbehave,
    and its assertion passed for the wrong reason. Read this, never ``target.pathology``.
    """

    def refuse(self, name: str, detail: str) -> RefusalError:
        """A refusal carrying the end code this target declares for ``name``."""
        code = getattr(self.target.end_codes, name)
        return RefusalError(int(code), detail)

    def request_cpu_state(self, requested: int) -> int:
        """Ask the CPU for a run state and publish what it actually reached.

        Returns the state ``SD203`` now holds, which is ``requested`` only if the key
        switch permits it. A handler that called this and then reported its own argument
        back would be re-inventing the defect this exists to close.
        """
        self.state.remote_request = requested
        return publish_cpu_state(self.memory, self.state)

    def device_number_base(self, dt: DeviceType) -> int:
        """The base an ASCII device number's digits are written in, for this entry.

        Decided by the **target**, not by ``profile.notation_for``: whether ``X``/``Y``
        are octal is the single largest silent-wrong-register hazard in the protocol
        (``Y10`` sent as the digits 10 lands on the 11th output, end code ``0x0000``,
        no error anywhere), so the simulator has to be able to disagree with the client
        about it or the client's rule is untested.
        """
        octal = (
            self.target.xy_ascii_octal
            and dt.name in ("X", "Y")
            and self.encoding.value == "ascii-xy-oct"
        )
        return emit_base(dt, Notation.OCTAL_DIGITS if octal else Notation.VALUE, codec=ASCII)

    def read_device(self, cursor: PayloadCursor) -> DecodedDevice:
        """One device specification block.

        ``[number][code]`` in binary and ``[code][number]`` in ASCII: the field-order
        swap is real, and it is why this is one function rather than two
        concatenations (SH(NA)-080956ENG-M pp.35-38).
        """
        if self.codec.name == "binary":
            width = self.codec.device_number_len(self.spec)
            number = int.from_bytes(cursor.take(width), "little")
            raw_code = cursor.take(self.codec.device_code_len(self.spec))
            code = int.from_bytes(raw_code, "little")
            table = _BINARY_SHORT_CODES if self.spec is SpecFormat.SHORT else _BINARY_LONG_CODES
            dt = table.get(code)
            if dt is None:
                raise self.refuse(
                    "unknown_device_code",
                    f"device code 0x{code:02X} is in no SLMP device table. An "
                    f"FX5U-32MT/DS fw 1.065 answered 0xC05C for both 0x00 and 0xFF "
                    f"(measured 2026-09-06).",
                )
            return DecodedDevice(type=dt, index=number)
        raw_code = cursor.take(self.codec.device_code_len(self.spec))
        try:
            mnemonic = parse_ascii_device_code(raw_code)
        except SlmpCodecError as exc:
            raise self.refuse("unknown_device_code", f"device code field: {exc}") from exc
        table_ascii = _ASCII_SHORT_CODES if self.spec is SpecFormat.SHORT else _ASCII_LONG_CODES
        dt = table_ascii.get(mnemonic)
        if dt is None:
            raise self.refuse(
                "unknown_device_code",
                f"device mnemonic {mnemonic!r} is in no SLMP device table",
            )
        digits = cursor.take(self.codec.device_number_len(self.spec))
        base = self.device_number_base(dt)
        alphabet = "0123456789ABCDEF"[:base]
        text = digits.decode("ascii", "replace")
        if len(text) != len(digits) or any(character not in alphabet for character in text):
            raise self.refuse(
                "unknown_device_code",
                f"device number {digits!r} is not {len(digits)} base-{base} digit(s) "
                f"for {dt.name}. Nothing here reads a bad nibble as zero: libslmp2's "
                f"wordcodec.c:27 does, and turns a corrupt frame into a plausible 0.",
            )
        return DecodedDevice(type=dt, index=int(text, base))

    # -- memory access, with the target's end codes ---------------------------------

    def check_points(self, count: int, *, limit: int, limit_name: str) -> None:
        """Refuse a zero point count and a count over this target's ceiling."""
        if count == 0:
            raise self.refuse(
                "zero_point_count",
                "a request for zero points. On FX5U-32MT/DS fw 1.065 this returned "
                "0xC052, a point-count error rather than the address error the generic "
                "documentation predicts (measured 2026-09-06).",
            )
        if count > limit:
            raise self.refuse(
                limit_name,
                f"{count} points; this CPU serves {limit}",
            )

    def read_words(self, device: DecodedDevice, count: int) -> tuple[int, ...]:
        """Words from device memory, translating a memory refusal into an end code."""
        try:
            return self.memory.read_words(device.type.name, device.index, count)
        except SimulatorMemoryError as exc:
            raise self._as_refusal(device, exc) from exc

    def write_words(self, device: DecodedDevice, values: Sequence[int]) -> None:
        """Words into device memory."""
        try:
            self.memory.write_words(device.type.name, device.index, values)
        except SimulatorMemoryError as exc:
            raise self._as_refusal(device, exc) from exc

    def read_bits(self, device: DecodedDevice, count: int) -> tuple[bool, ...]:
        """Single bits from device memory."""
        try:
            return self.memory.read_bits(device.type.name, device.index, count)
        except SimulatorMemoryError as exc:
            raise self._as_refusal(device, exc) from exc

    def write_bits(self, device: DecodedDevice, values: Sequence[bool]) -> None:
        """Single bits into device memory."""
        try:
            self.memory.write_bits(device.type.name, device.index, values)
        except SimulatorMemoryError as exc:
            raise self._as_refusal(device, exc) from exc

    def _as_refusal(self, device: DecodedDevice, exc: SimulatorMemoryError) -> RefusalError:
        """Which end code this target answers for a memory refusal.

        The split matters: "this CPU has no ZR at all" and "D8000 is past the end of D"
        are different diagnoses, and on an FX5U-32MT/DS fw 1.065 they are different codes
        -- 0xC05C and 0xC056 (measured 2026-09-06).
        """
        if isinstance(exc, AbsentDeviceError):
            return self.refuse("device_absent", f"{device}: {exc}")
        if isinstance(exc, OutOfRangeError):
            return self.refuse("address_out_of_range", f"{device}: {exc}")
        return self.refuse("device_not_allowed_here", f"{device}: {exc}")


# ----------------------------------------------------------------------------------------
# Handlers
# ----------------------------------------------------------------------------------------

if TYPE_CHECKING:  # pragma: no cover - typing only
    Handler = Callable[[ServerContext, PayloadCursor], Outcome]


def _words_of(ctx: ServerContext, device: DecodedDevice, count: int) -> tuple[int, ...]:
    """``count`` word-access points at ``device``: registers, or 16-bit windows."""
    return ctx.read_words(device, count)


def _access_point_value(ctx: ServerContext, device: DecodedDevice, words: int) -> int:
    """One ``0403`` access point, as the 16- or 32-bit number the response carries."""
    values = ctx.read_words(device, words)
    if words == 1:
        return values[0]
    return (values[1] << 16) | values[0]


def _require_random_device(ctx: ServerContext, device: DecodedDevice) -> None:
    """The ``0403``/``1402`` device gate, with the measured exception.

    An FX5U-32MT/DS accepted a ``TS0`` word point and answered ``0x0000`` with data,
    against JY997D56001-K p.78 (measured 2026-09-06). A target that reproduces that is
    how a test proves the **client** refuses what this PLC would have allowed.
    """
    if ctx.target.accepts_random_device(
        device.type.name, random_ok=device.type.random_ok, pathology=ctx.pathology
    ):
        return
    raise ctx.refuse(
        "illegal_random_device",
        f"{device.type.name} ({device.type.long_name.lower()}) is excluded from "
        f"0x{ctx.command:04X} access points",
    )


def _handle_read_type_name(ctx: ServerContext, cursor: PayloadCursor) -> Outcome:
    """``0101``: 16 characters of model name, then the model code."""
    cursor.finish()
    return Reply(
        0x0000,
        ctx.target.model_name_field + ctx.codec.number(ctx.target.model_code, bits=16),
    )


def _handle_self_test(ctx: ServerContext, cursor: PayloadCursor) -> Outcome:
    """``0619``: echo the count and the data, byte for byte."""
    count = cursor.number(16)
    data = cursor.take(count)
    cursor.finish()
    return Reply(0x0000, ctx.codec.number(count, bits=16) + data)


def _handle_clear_error(ctx: ServerContext, cursor: PayloadCursor) -> Outcome:
    """``1617``: clear the own-station error code. No request data, no response data.

    Nothing is cleared, because this CPU model holds no error latch: no target declares
    one, nothing in the dispatcher ever sets one, and no request could read one back.
    This handler used to assign ``False`` to a ``SessionState.error_flag`` that was never
    ``True`` and that no command returned -- a bookkeeping gesture that made ``1617``
    read as modelled. Answering the command and modelling nothing is the honest pair;
    when a target grows a real error latch, this is where it gets cleared, and the field
    that holds it has to be readable through some command or it is the same defect again.
    """
    cursor.finish()
    if not ctx.target.clear_error:
        raise ctx.refuse("unsupported_command", "this CPU does not serve 0x1617")
    return Reply(0x0000)


def _handle_batch_read(ctx: ServerContext, cursor: PayloadCursor) -> Outcome:
    """``0401``: one head device, one point count, one contiguous run."""
    device = ctx.read_device(cursor)
    count = cursor.number(16)
    cursor.finish()
    if ctx.unit is Unit.BIT:
        ctx.check_points(
            count,
            limit=ctx.target.limits.batch_bit_for(ctx.encoding),
            limit_name="batch_bit_limit",
        )
        _require_bit_device(ctx, device)
        return Reply(0x0000, ctx.codec.bits(ctx.read_bits(device, count)))
    ctx.check_points(
        count,
        limit=ctx.target.limits.batch_word_for(ctx.encoding),
        limit_name="batch_word_limit",
    )
    return Reply(0x0000, ctx.codec.words(_words_of(ctx, device, count)))


def _handle_batch_write(ctx: ServerContext, cursor: PayloadCursor) -> Outcome:
    """``1401``: the same head and count, followed by the data."""
    device = ctx.read_device(cursor)
    count = cursor.number(16)
    if ctx.unit is Unit.BIT:
        ctx.check_points(
            count,
            limit=ctx.target.limits.batch_bit_for(ctx.encoding),
            limit_name="batch_bit_limit",
        )
        _require_bit_device(ctx, device)
        values = cursor.bits(count)
        cursor.finish()
        ctx.write_bits(device, values)
        return Reply(0x0000)
    ctx.check_points(
        count,
        limit=ctx.target.limits.batch_word_for(ctx.encoding),
        limit_name="batch_word_limit",
    )
    words = cursor.words(count)
    cursor.finish()
    ctx.write_words(device, words)
    return Reply(0x0000)


def _require_bit_device(ctx: ServerContext, device: DecodedDevice) -> None:
    if device.type.unit is Unit.BIT:
        return
    raise ctx.refuse(
        "device_not_allowed_here",
        f"{device.type.name} is a word device and cannot be addressed in bit units",
    )


def _read_point_specs(
    ctx: ServerContext, cursor: PayloadCursor
) -> tuple[tuple[DecodedDevice, ...], tuple[DecodedDevice, ...]]:
    """The two one-byte counts and every access point, word group then double-word."""
    word_count = cursor.number(8)
    dword_count = cursor.number(8)
    total = word_count + dword_count
    if total == 0:
        raise ctx.refuse("zero_point_count", "a Read Random with no access points")
    if total > ctx.target.limits.random_points_for(ctx.encoding):
        raise ctx.refuse(
            "random_point_limit",
            f"{total} access points; an FX5U-32MT/DS fw 1.065 answered 0xC054 at 193 "
            f"(measured 2026-09-06) and this target serves "
            f"{ctx.target.limits.random_points_for(ctx.encoding)}",
        )
    words = tuple(ctx.read_device(cursor) for _ in range(word_count))
    dwords = tuple(ctx.read_device(cursor) for _ in range(dword_count))
    for device in (*words, *dwords):
        _require_random_device(ctx, device)
    return words, dwords


def _handle_read_random(ctx: ServerContext, cursor: PayloadCursor) -> Outcome:
    """``0403``: one consistent snapshot of scattered devices."""
    words, dwords = _read_point_specs(ctx, cursor)
    cursor.finish()
    return Reply(0x0000, _random_payload(ctx, words, dwords))


def _random_payload(
    ctx: ServerContext,
    words: tuple[DecodedDevice, ...],
    dwords: tuple[DecodedDevice, ...],
) -> bytes:
    """The word section, one word per point, then the double-word section, two each."""
    out = [
        ctx.codec.number(_access_point_value(ctx, device, 1), bits=16) for device in words
    ]
    out.extend(
        ctx.codec.number(_access_point_value(ctx, device, 2), bits=32) for device in dwords
    )
    return b"".join(out)


def _handle_write_random(ctx: ServerContext, cursor: PayloadCursor) -> Outcome:
    """``1402``: scattered writes, word units or bit units."""
    if ctx.unit is Unit.BIT:
        return _handle_write_random_bits(ctx, cursor)
    word_count = cursor.number(8)
    dword_count = cursor.number(8)
    if word_count + dword_count == 0:
        raise ctx.refuse("zero_point_count", "a Write Random with no access points")
    weighted = (
        word_count * ctx.target.limits.write_random_word_weight
        + dword_count * ctx.target.limits.write_random_dword_weight
    )
    if weighted > ctx.target.limits.write_random_budget:
        raise ctx.refuse(
            "random_point_limit",
            f"word x {ctx.target.limits.write_random_word_weight} + double-word x "
            f"{ctx.target.limits.write_random_dword_weight} = {weighted}, over this "
            f"CPU's budget of {ctx.target.limits.write_random_budget}",
        )
    pending: list[tuple[DecodedDevice, tuple[int, ...]]] = []
    for width, count in ((1, word_count), (2, dword_count)):
        for _ in range(count):
            device = ctx.read_device(cursor)
            _require_random_device(ctx, device)
            value = cursor.number(16 if width == 1 else 32)
            if width == 1:
                pending.append((device, (value,)))
            else:
                pending.append((device, (value & 0xFFFF, (value >> 16) & 0xFFFF)))
    cursor.finish()
    for device, values in pending:
        ctx.write_words(device, values)
    return Reply(0x0000)


def _handle_write_random_bits(ctx: ServerContext, cursor: PayloadCursor) -> Outcome:
    """``1402`` subcommand ``0001``: set or reset scattered single bits."""
    count = cursor.number(8)
    if count == 0:
        raise ctx.refuse("zero_point_count", "a Write Random in bit units with no points")
    if count > ctx.target.limits.random_bit_points:
        raise ctx.refuse(
            "random_bit_point_limit",
            f"{count} bit access points; this CPU serves "
            f"{ctx.target.limits.random_bit_points}",
        )
    value_bits: Literal[8, 16] = 8 if ctx.spec is SpecFormat.SHORT else 16
    pending: list[tuple[DecodedDevice, bool]] = []
    for _ in range(count):
        device = ctx.read_device(cursor)
        _require_random_device(ctx, device)
        _require_bit_device(ctx, device)
        pending.append((device, bool(cursor.number(value_bits))))
    cursor.finish()
    for device, value in pending:
        ctx.write_bits(device, (value,))
    return Reply(0x0000)


def _read_block_heads(
    ctx: ServerContext, cursor: PayloadCursor
) -> tuple[tuple[DecodedDevice, int], ...]:
    """The two block counts and each block's head device and point count."""
    if not ctx.target.block_access:
        raise ctx.refuse("unsupported_command", "this CPU does not serve block access")
    word_blocks = cursor.number(8)
    bit_blocks = cursor.number(8)
    blocks = word_blocks + bit_blocks
    if blocks == 0:
        raise ctx.refuse("zero_point_count", "a block request with no blocks")
    if blocks > ctx.target.limits.max_blocks:
        raise ctx.refuse(
            "block_count_limit",
            f"{blocks} blocks; this CPU serves {ctx.target.limits.max_blocks}",
        )
    out: list[tuple[DecodedDevice, int]] = []
    for _ in range(blocks):
        device = ctx.read_device(cursor)
        points = cursor.number(16)
        if points == 0:
            raise ctx.refuse("zero_point_count", f"block at {device} asks for 0 points")
        out.append((device, points))
    return tuple(out)


def _handle_read_blocks(ctx: ServerContext, cursor: PayloadCursor) -> Outcome:
    """``0406``: several contiguous runs, each block's points as words."""
    blocks = _read_block_heads(ctx, cursor)
    cursor.finish()
    total = sum(points for _device, points in blocks)
    if total > ctx.target.limits.read_block_points:
        raise ctx.refuse(
            "batch_word_limit",
            f"{total} points over {len(blocks)} blocks; this CPU serves "
            f"{ctx.target.limits.read_block_points}",
        )
    out: list[int] = []
    for device, points in blocks:
        out.extend(_words_of(ctx, device, points))
    return Reply(0x0000, ctx.codec.words(out))


def _handle_write_blocks(ctx: ServerContext, cursor: PayloadCursor) -> Outcome:
    """``1406``: several contiguous runs, each followed by its own data."""
    if not ctx.target.block_access:
        raise ctx.refuse("unsupported_command", "this CPU does not serve block access")
    word_blocks = cursor.number(8)
    bit_blocks = cursor.number(8)
    blocks = word_blocks + bit_blocks
    if blocks == 0:
        raise ctx.refuse("zero_point_count", "a block request with no blocks")
    if blocks > ctx.target.limits.max_blocks:
        raise ctx.refuse(
            "block_count_limit",
            f"{blocks} blocks; this CPU serves {ctx.target.limits.max_blocks}",
        )
    pending: list[tuple[DecodedDevice, tuple[int, ...]]] = []
    total = 0
    for _ in range(blocks):
        device = ctx.read_device(cursor)
        points = cursor.number(16)
        if points == 0:
            raise ctx.refuse("zero_point_count", f"block at {device} asks for 0 points")
        total += points
        pending.append((device, cursor.words(points)))
    cursor.finish()
    if total > ctx.target.limits.write_block_points:
        raise ctx.refuse(
            "batch_word_limit",
            f"{total} points over {len(pending)} blocks; this CPU serves "
            f"{ctx.target.limits.write_block_points}",
        )
    for device, values in pending:
        ctx.write_words(device, values)
    return Reply(0x0000)


def _handle_register_monitor(ctx: ServerContext, cursor: PayloadCursor) -> Outcome:
    """``0801``: register a device list. RefusalError outright on an iQ-F.

    An FX5U-32MT/DS on firmware 1.065 answers ``0xC059`` -- **not** ``0xC05D`` "monitor
    not registered", which is what a reader of the generic reference expects and what
    tempts a library into emulating the command with a ``0403`` (measured 2026-09-06).
    """
    if not ctx.target.monitor:
        raise ctx.refuse(
            "unsupported_command",
            "0x0801 Monitor Registration. On FX5U-32MT/DS fw 1.065 this is 0xC059, not "
            "0xC05D, and there is no substitute command to fall back to.",
        )
    words, dwords = _read_point_specs(ctx, cursor)
    cursor.finish()
    registration = tuple(
        (device.type, device.index, Unit.WORD, span)
        for span, group in ((1, words), (2, dwords))
        for device in group
    )
    ctx.state.monitor_points = registration
    return Reply(0x0000)


def _handle_execute_monitor(ctx: ServerContext, cursor: PayloadCursor) -> Outcome:
    """``0802``: read the registered list back. No request data at all."""
    cursor.finish()
    if not ctx.target.monitor:
        raise ctx.refuse("unsupported_command", "0x0802 Execute Monitor")
    registration = ctx.state.monitor_points
    if registration is None:
        raise ctx.refuse(
            "monitor_not_registered",
            "0x0802 with no 0x0801 registration on this CPU. SLMP issues no handle, so "
            "a second client's registration replaces the first one's silently.",
        )
    words = tuple(
        DecodedDevice(dt, index) for dt, index, _unit, span in registration if span == 1
    )
    dwords = tuple(
        DecodedDevice(dt, index) for dt, index, _unit, span in registration if span == 2
    )
    return Reply(0x0000, _random_payload(ctx, words, dwords))


def _require_remote(ctx: ServerContext) -> None:
    if ctx.target.remote_control:
        return
    raise ctx.refuse("remote_control_disabled", "remote control is disabled on this CPU")


def _handle_remote_run(ctx: ServerContext, cursor: PayloadCursor) -> Outcome:
    """``1001``: mode, then the clear mode and one reserved byte.

    Whether the CPU actually runs is :func:`effective_cpu_state`'s business: with
    :attr:`SessionState.switch_position` at ``STOP`` this answers ``0x0000`` and
    ``SD203`` still reads ``STOP``, which is the manual's own sentence (p.131) rather
    than a pathology. ``remote_run_lies`` is the *other* case -- a CPU that answers
    ``0x0000`` and changes nothing for no documented reason at all.
    """
    _require_remote(ctx)
    mode = cursor.number(16)
    clear = cursor.number(8)
    cursor.number(8)
    cursor.finish()
    del mode
    if clear not in {int(value) for value in ctx.target.clear_modes}:
        raise ctx.refuse(
            "unsupported_subcommand",
            f"clear mode {clear} is not one this CPU accepts; an iQ-F's clear-mode table "
            f"has exactly one row and it is 00H",
        )
    if ctx.pathology.remote_run_lies:
        return Reply(0x0000)
    ctx.request_cpu_state(CpuRunState.RUN)
    return Reply(0x0000)


def _handle_remote_pause(ctx: ServerContext, cursor: PayloadCursor) -> Outcome:
    """``1003``: mode only."""
    _require_remote(ctx)
    cursor.number(16)
    cursor.finish()
    if ctx.pathology.remote_run_lies:
        return Reply(0x0000)
    ctx.request_cpu_state(CpuRunState.PAUSE)
    return Reply(0x0000)


def _handle_remote_stop(ctx: ServerContext, cursor: PayloadCursor) -> Outcome:
    """``1002``: the two-unit fixed field, ``00 00`` on iQ-F and ``01 00`` elsewhere."""
    _require_remote(ctx)
    _consume_fixed_field(ctx, cursor)
    if ctx.pathology.remote_run_lies:
        return Reply(0x0000)
    ctx.request_cpu_state(CpuRunState.STOP)
    return Reply(0x0000)


def _handle_remote_latch_clear(ctx: ServerContext, cursor: PayloadCursor) -> Outcome:
    """``1005``: the fixed field. Legal only from STOP."""
    _require_remote(ctx)
    _consume_fixed_field(ctx, cursor)
    if effective_cpu_state(ctx.state) != CpuRunState.STOP:
        raise RefusalError(0x4010, "latch clear needs the CPU stopped")
    return Reply(0x0000)


def _handle_remote_reset(ctx: ServerContext, cursor: PayloadCursor) -> Outcome:
    """``1006``: on success there is **no response**, and the connection goes with it."""
    if not ctx.target.remote_reset:
        raise ctx.refuse("remote_control_disabled", "remote reset is disabled on this CPU")
    _consume_fixed_field(ctx, cursor)
    if ctx.pathology.remote_reset_no_response:
        return Silence(
            "0x1006 Remote Reset completed: SH(NA)-080956ENG-M p.136 says the response "
            "is not sent back, and over TCP the connection is torn down with it.",
            close_after=True,
        )
    return Reply(0x0000, close_after=True)


def _consume_fixed_field(ctx: ServerContext, cursor: PayloadCursor) -> None:
    """The 16-bit fixed field of ``1002``/``1005``/``1006``, checked against the target."""
    value = cursor.number(16)
    cursor.finish()
    expected = int.from_bytes(ctx.target.remote_fixed_field, "little")
    if value != expected:
        raise ctx.refuse(
            "unsupported_subcommand",
            f"the fixed field is 0x{value:04X} and this CPU family writes "
            f"0x{expected:04X} ({ctx.target.remote_fixed_field.hex(' ').upper()})",
        )


def _handle_unlock_password(ctx: ServerContext, cursor: PayloadCursor) -> Outcome:
    """``1630``: the password as literal characters in both codings."""
    if not ctx.target.remote_password:
        raise ctx.refuse("unsupported_command", "0x1630 Remote Password Unlock")
    length = cursor.number(16)
    supplied = cursor.take(length).decode("ascii", "replace")
    cursor.finish()
    if supplied != ctx.target.password:
        raise ctx.refuse("password_incorrect", "the remote password does not match")
    ctx.state.password_locked = False
    return Reply(0x0000)


def _handle_lock_password(ctx: ServerContext, cursor: PayloadCursor) -> Outcome:
    """``1631``: lock the connection again."""
    if not ctx.target.remote_password:
        raise ctx.refuse("unsupported_command", "0x1631 Remote Password Lock")
    length = cursor.number(16)
    supplied = cursor.take(length).decode("ascii", "replace")
    cursor.finish()
    if supplied != ctx.target.password:
        raise ctx.refuse("password_incorrect", "the remote password does not match")
    ctx.state.password_locked = True
    return Reply(0x0000)


HANDLERS: Final[Mapping[int, Handler]] = {
    0x0101: _handle_read_type_name,
    0x0401: _handle_batch_read,
    0x0403: _handle_read_random,
    0x0406: _handle_read_blocks,
    0x0619: _handle_self_test,
    0x0801: _handle_register_monitor,
    0x0802: _handle_execute_monitor,
    0x1001: _handle_remote_run,
    0x1002: _handle_remote_stop,
    0x1003: _handle_remote_pause,
    0x1005: _handle_remote_latch_clear,
    0x1006: _handle_remote_reset,
    0x1401: _handle_batch_write,
    0x1402: _handle_write_random,
    0x1406: _handle_write_blocks,
    0x1617: _handle_clear_error,
    0x1630: _handle_unlock_password,
    0x1631: _handle_lock_password,
}
"""One entry per command an external device may send.

``tests/unit/test_simulator_dispatch.py`` asserts this covers exactly the ``"request"``
rows of :data:`aslmp.commands.registry.COMMANDS`: adding a command to the library
without teaching the simulator to serve it fails the build, and serving a command the
library does not implement means one of the two is wrong about the protocol.
"""


# ----------------------------------------------------------------------------------------
# The dispatcher
# ----------------------------------------------------------------------------------------


@dataclass(slots=True)
class Dispatcher:
    """Turns a decoded request frame into an outcome, for one simulated CPU.

    Holds the memory and the session state, so one dispatcher serves every connection to
    one simulator -- which is what a CPU is. The scenario, if there is one, gets first
    refusal on every request.
    """

    target: SimulatorTarget
    memory: DeviceMemory
    state: SessionState = field(default_factory=SessionState)
    scenario: Scenario | None = None
    pathology: Pathology | None = None
    """An override for the target's own board, or ``None`` to use the target's.

    :class:`~aslmp.testing.server.PlcSimulator` passes whatever it was given here, so a
    ``pathology=`` argument reaches the handlers and not only the socket layer.
    """

    scan_per_request: bool = False
    """Advance ``D8``/``D9`` once per served request, if the CPU is running.

    Off by default, and the default is a statement rather than caution: the bench CPU
    advances ``IO_Scan`` on a wall clock at about 1018 scans/s (the one idle scan rate
    this repository publishes; conditions in ``docs/hardware.md`` section 17) whether or
    not anybody is talking to it, so a counter that moves per request is not a model of
    that silicon --
    it is a model of a CPU that scans only when asked, which no CPU does.

    Turn it on to say "this simulated CPU is running the bench's program while it serves"
    and get the property :meth:`advance_scan` alone cannot give you: registers that move
    underneath a client, so a stale-value bug has something to fail against. Two reads of
    ``D8`` then differ, and a test that asserts an exact count has to seed and freeze it
    (leave this off) rather than assume the CPU stood still.
    """

    def __post_init__(self) -> None:
        try:
            self.publish_cpu_state()
        except SimulatorMemoryError as exc:
            raise ValueError(
                f"a simulated CPU has to be able to report its own operating status, "
                f"and this memory cannot hold {CPU_STATUS_DEVICE}{CPU_STATUS_INDEX}: "
                f"{exc}"
            ) from exc

    @property
    def board(self) -> Pathology:
        """The pathology actually in force: the override if there is one, else the target's."""
        return self.target.pathology if self.pathology is None else self.pathology

    def publish_cpu_state(self) -> int:
        """Re-derive ``SD203`` from the key switch and the last remote request."""
        return publish_cpu_state(self.memory, self.state)

    def cpu_state(self) -> int:
        """What ``SD203`` holds right now: device memory, read the way a client reads it.

        The register is re-derived at the top of every served request, so a key switch
        you have just turned reaches it when the CPU next scans -- which is also what
        silicon does, and which is why this is a plain register read rather than a call
        to :func:`effective_cpu_state`. Call :meth:`publish_cpu_state` to advance the
        scan yourself.
        """
        return self.memory.get_u16(CPU_STATUS_DEVICE, CPU_STATUS_INDEX)

    def handle(self, request: RawRequest, *, codec: Codec, encoding: Encoding) -> Outcome:
        """Serve one request. Never raises for a malformed one: it answers an end code."""
        self.publish_cpu_state()
        if self.scan_per_request:
            self.advance_scan()
        try:
            unit, spec, extension = decode_subcommand(request.subcommand)
        except SlmpCodecError:
            return Reply(self.target.end_codes.unsupported_subcommand)
        ctx = ServerContext(
            target=self.target,
            memory=self.memory,
            state=self.state,
            codec=codec,
            encoding=encoding,
            spec=spec,
            unit=unit,
            command=request.command,
            subcommand=request.subcommand,
            pathology=self.board,
        )
        scripted = None if self.scenario is None else self.scenario.take(request.command)
        if scripted is not None:
            return scripted
        if (
            self.state.password_locked
            and request.command not in PASSWORD_EXEMPT_COMMANDS
        ):
            return Reply(self.target.end_codes.password_locked)
        if extension:
            return Reply(self.target.end_codes.unsupported_subcommand)
        if not self.target.supports_spec(spec):
            return Reply(self.target.end_codes.unsupported_subcommand)
        handler = HANDLERS.get(request.command)
        if handler is None:
            return Reply(self.target.end_codes.unsupported_command)
        cursor = PayloadCursor(
            request.payload, codec, length_error=self.target.end_codes.request_length_mismatch
        )
        try:
            return handler(ctx, cursor)
        except RefusalError as refusal:
            return Reply(refusal.end_code)

    def advance_scan(
        self,
        device: str = "D",
        index: int = 8,
        *,
        step: float = BENCH_SCAN_STEP,
        wrap_above: float | None = BENCH_SCAN_WRAP,
    ) -> float:
        """Advance the free-running scan counter the bench CPU keeps at ``D8``/``D9``.

        **That register is a ``REAL``**, so this is an ``f32`` bump and not a double-word
        one: the CPU's own ST is ``IO_Scan := IO_Scan + 1.0`` with ``IF IO_Scan > 1.0E7``
        (FX5U-32MT/DS fw 1.065 at 192.168.10.250, 2026-09-07). It idles at 1018 scans/s
        with no physical I/O wired -- the one idle rate this repository publishes, with
        its conditions in ``docs/hardware.md`` section 17 -- so a client reading ``D8``
        twice gets two different numbers.

        **A stopped CPU does not scan**, so this returns the counter unchanged unless
        ``SD203`` says RUN. That sentence used to be false in both directions: nothing
        derived ``SD203`` from the remote handlers, and this method counted regardless, so
        a Remote STOP that was answered ``0x0000`` was followed by a scan counter that
        kept climbing -- the one oracle ``tests/hardware/test_remote_control.py`` trusts
        over ``SD203`` itself, and the simulator could not reproduce it either way.

        This method does not make a register move *while a client is talking to the
        CPU*; only a caller calling it does, and until 2026-09-07 the only callers were
        tests. :attr:`scan_per_request` is what buys the "registers move underneath a
        client" property, and it is opt-in for the reason given there.

        This method used to call :meth:`~aslmp.testing.memory.DeviceMemory.bump_u32`,
        which made every simulator-backed test of an ``f32`` scan counter pass against a
        ``u32`` decode of it. Use :meth:`~aslmp.testing.memory.DeviceMemory.bump_u32`
        directly for a counter a CPU really declares as an integer double word.
        """
        if effective_cpu_state(self.state) != CpuRunState.RUN:
            return self.memory.get_f32(device, index)
        return self.memory.bump_f32(device, index, step, wrap_above=wrap_above)


def registered_request_codes() -> tuple[int, ...]:
    """Every command code an external device may send, from the shared registry."""
    return tuple(
        sorted(code for code, spec in COMMANDS.items() if spec.direction == "request")
    )
