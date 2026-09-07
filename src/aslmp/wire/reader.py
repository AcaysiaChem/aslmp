"""``ResponseAccumulator`` — the TCP-segmentation defence, with no socket in sight.

Layer 0. **stdlib only.** Importing this module must not pull ``socket``, ``ssl``,
``asyncio``, ``selectors``, ``threading`` or ``logging`` into ``sys.modules``
(``tests/unit/test_layering.py`` proves it in a subprocess).

TCP is a stream. An SLMP response is a fixed prefix, a 16-bit length field inside it, and
exactly that many more units. Nothing else delimits it: there is no terminator and no
checksum. So the only correct read is *length-driven* -- read the prefix with a loop,
take ``L``, read exactly ``L`` more with a loop -- and Mitsubishi says so, in
SH(NA)-080956ENG-M chapter 6 p.205, as a troubleshooting item: *"If the response message
is shorter than expected, take action to receive the remaining data."*

**This is not theoretical.** A 1931-byte response (a 960-word batch read) was captured
three times on FX5U-32MT/DS fw 1.065, 2026-09-06: twice it arrived as one chunk, and once
as ``1460`` then ``471`` bytes 3.0 ms apart -- the Ethernet MSS boundary. Whether you see
the split depends on host scheduling, so a single ``recv()`` passes two runs in three and
fails in production. (``pymcprotocol`` 0.3.0 ``type3e.py:148`` is one unbounded
``recv(4096)`` with no reassembly and no length check; ``pymcprotocol`` also zero-fills a
short response, turning ``[111, 222, 333, 444]`` into ``[111, 222, 0, 0]``.)

That trial-3 split is also why the receive timestamp belongs on the **last** chunk:
stamping the first reports 11.0 ms for a transaction that took 14.0 ms.

.. rubric:: Why this class is pure

It holds a ``bytearray``, an integer and a frame format. No socket, no event loop, no
clock. Every segmentation behaviour a real network can produce is therefore reproducible
in CI as a list of ``bytes`` -- ``tests/unit/test_reader.py`` feeds every golden response
one unit at a time, split exactly at 1460, and in 200 seeded random chunkings, and
asserts an identical :class:`~aslmp.wire.raw.RawResponse` every time. A reassembler that
needed a socket to test would be tested by nobody.

It satisfies ``aslmp.transport.base.Reassembler`` structurally -- ``bytes_needed`` and
``feed`` -- so the transport can drive the read without ever learning what a frame is.
The dependency runs upward; the knowledge runs downward.

.. rubric:: What it refuses

* **Trailing data.** Feeding more than :attr:`bytes_needed` raises. On UDP a datagram
  must be exactly one message; on TCP a surplus is the measured coalescing corruption
  arriving as somebody else's answer. Neither is buffered "just in case", because the
  only thing to do with an unattributable response is not to return it.
* **Taking early.** :meth:`take` on an incomplete buffer raises rather than returning a
  short or zero-filled frame. An empty buffer is not end code ``0x0000``.
* **Taking twice.** One accumulator, one exchange, like the transaction token it travels
  with.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aslmp.wire.raw import (
    RawResponse,
    SlmpIncompleteFrameError,
    SlmpTrailingDataError,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from aslmp.wire.codec import Codec
    from aslmp.wire.frames import FrameFormat, FramePrefix

__all__ = ["ResponseAccumulator"]


class ResponseAccumulator:
    """Incremental, length-driven, pure. Feed it bytes; ask it for a frame.

    ::

        acc = ResponseAccumulator(THREE_E, BINARY)
        while acc.bytes_needed:
            acc.feed(await recv(acc.bytes_needed))
        response = acc.take()

    ``expect_serial`` is the 4E serial this transaction sent. It is compared byte for
    byte in :meth:`take`, and a mismatch raises
    :class:`~aslmp.wire.raw.SlmpSerialMismatchError` -- the only in-band defence against
    the measured coalescing corruption, and the reason a 4E connection can detect what a
    3E connection cannot.
    """

    __slots__ = ("_buf", "_codec", "_expect_serial", "_frame", "_prefix", "_taken")

    def __init__(
        self,
        frame: FrameFormat,
        codec: Codec,
        *,
        expect_serial: int | None = None,
    ) -> None:
        if expect_serial is not None and not frame.carries_serial:
            raise ValueError(
                f"expect_serial=0x{expect_serial:04X} was supplied for a "
                f"{frame.frame_type.value} frame, which has no serial No. field. 3E "
                f"offers no in-band correlation at all; that is a property of the frame "
                f"format, not something a caller can ask for."
            )
        self._frame = frame
        self._codec = codec
        self._expect_serial = expect_serial
        self._buf = bytearray()
        self._prefix: FramePrefix | None = None
        self._taken = False

    # -- state -------------------------------------------------------------------------

    @property
    def frame(self) -> FrameFormat:
        """The frame format this accumulator was built for."""
        return self._frame

    @property
    def bytes_needed(self) -> int:
        """Wire units still wanted: the rest of the prefix, then the rest of the body.

        Zero exactly when a whole frame is buffered (or has already been taken). A
        transport loops ``while acc.bytes_needed:`` and asks the socket for at most this
        many units, which is what stops it reading into the next message.
        """
        if self._taken:
            return 0
        if self._prefix is None:
            return self._frame.prefix_units(self._codec) - len(self._buf)
        return self._total - len(self._buf)

    @property
    def _total(self) -> int:
        """Units in the whole frame once ``L`` is known. Only valid after the prefix."""
        if self._prefix is None:  # pragma: no cover - guarded by every caller
            raise SlmpIncompleteFrameError(
                "the frame length is not known until the fixed prefix has arrived"
            )
        return self._frame.prefix_units(self._codec) + self._prefix.declared

    @property
    def complete(self) -> bool:
        """``True`` when a whole frame is buffered and not yet taken."""
        return not self._taken and self._prefix is not None and self.bytes_needed == 0

    @property
    def declared_length(self) -> int | None:
        """``L`` as this frame declares it, or ``None`` until the prefix has arrived."""
        return None if self._prefix is None else self._prefix.declared

    @property
    def buffered(self) -> bytes:
        """What has arrived so far. For a diagnostic, never for a decode."""
        return bytes(self._buf)

    # -- feeding -----------------------------------------------------------------------

    def feed(self, data: bytes, /) -> None:
        """Append received units. Raises rather than buffering somebody else's message.

        A whole datagram may be fed in one call -- UDP delivers one message at a time and
        should not have to slice it -- or the frame may arrive one unit at a time. What
        is refused is a buffer longer than ``prefix + L``, which is only knowable once
        the prefix is in.

        The prefix is decoded the moment it is complete, so a foreign subheader, an
        unsolicited Ondemand frame or an impossible ``L`` is named at the earliest unit
        that can prove it -- rather than after waiting for a further ``L`` units, which
        is exactly what an overstated length looks like from here.
        """
        if self._taken:
            raise SlmpTrailingDataError(
                f"{len(data)} further wire unit(s) arrived after the response frame was "
                f"taken. One accumulator serves one exchange; a second response on the "
                f"same buffer is the measured TCP coalescing corruption and it is never "
                f"attributed to a transaction that has already been answered."
            )
        self._buf += data
        if self._prefix is None and len(self._buf) >= self._frame.prefix_units(self._codec):
            self._prefix = self._frame.read_prefix(
                bytes(self._buf), self._codec, response=True
            )
        if self._prefix is not None and len(self._buf) > self._total:
            raise SlmpTrailingDataError(
                f"{len(self._buf)} wire unit(s) have arrived but this frame is "
                f"{self._total}{self._so_far()}. The surplus {len(self._buf) - self._total} "
                f"unit(s) are somebody's message and this library will not guess whose: "
                f"read at most bytes_needed units at a time, and on UDP require exactly "
                f"one datagram per SLMP message."
            )

    def _so_far(self) -> str:
        """`` (L = 0x000B, 20 of 20 units)`` — context for a refusal message."""
        if self._prefix is None:
            return f" (still reading the fixed prefix, {len(self._buf)} unit(s) in)"
        return (
            f" (L = 0x{self._prefix.declared:04X}, {len(self._buf)} of "
            f"{self._total} unit(s) in)"
        )

    # -- taking ------------------------------------------------------------------------

    def take(self) -> RawResponse:
        """The parsed frame. Raises unless a whole one is buffered, and burns itself.

        Never returns a partial frame, a zero-filled frame or a stale one. The three
        failures this replaces are all real: ``pymcprotocol`` zero-fills a truncated
        response, ``pymelsec`` returns ``[]`` from an error path, and ``Esmool`` leaves
        an unread tail on the socket so that the *next* transaction reads the previous
        one's bytes as fresh data.
        """
        if self._taken:
            raise SlmpIncompleteFrameError(
                "this response frame has already been taken. An accumulator serves one "
                "exchange, like the transaction token it travels with; returning the "
                "same frame twice is how a stale reading reaches a control loop."
            )
        if not self.complete:
            raise SlmpIncompleteFrameError(
                f"{self.bytes_needed} more wire unit(s) are needed before this is a "
                f"frame{self._so_far()}. An empty or partial buffer is not end code "
                f"0x0000: SH(NA)-080956ENG-M chapter 6 p.205 says to take action to "
                f"receive the remaining data, and this library never invents it."
            )
        response = self._frame.parse(
            bytes(self._buf), self._codec, expect_serial=self._expect_serial
        )
        self._taken = True
        return response

    def __repr__(self) -> str:
        state = "taken" if self._taken else f"{self.bytes_needed} unit(s) wanted"
        return (
            f"ResponseAccumulator({self._frame!r}, {self._codec!r}, "
            f"expect_serial={self._expect_serial!r}) [{state}]"
        )
