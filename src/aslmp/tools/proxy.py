"""``aslmp proxy`` -- forward SLMP unconditionally and decode a copy in flight.

A man-in-the-middle you put between somebody else's client and your PLC when you need to
know what is actually on the wire and Wireshark is not an option -- a locked-down plant
PC, a Windows box with no capture driver, a colleague's HMI you are not allowed to
modify.

**It forwards first and decodes afterwards, always, on a copy.** Bytes go out the far
side the moment they arrive, before anything here has looked at them. A proxy that
parsed before forwarding would add its own parse to the latency it is measuring and
would drop a frame it could not understand -- and every interesting frame is one nobody
understood. When a decode fails, that direction stops being annotated and says so; the
forwarding does not change.

**Two stamps per frame.** A monotonic timestamp and the delta since the previous frame
in the same direction. The request-to-response delta is the number the client sees; the
response-to-next-request delta is the *host gap*, which is where a slow control loop
hides. Both come from ``time.monotonic_ns`` and never from the wall clock: a run that
lasts a shift crosses an NTP step.

**TCP only, and that is not laziness.** Proxying UDP to an iQ-F cannot work: a UDP SLMP
connection entry on that CPU is point-to-point and GX Works3 refuses to save it without
a destination IP address (measured 2026-09-06), so the PLC would answer the proxy's
address and only the proxy's address -- and the entry would have to be reconfigured to
name the proxy host, at which point the client under test is no longer talking to the
configuration you wanted to observe.

This tool sends nothing of its own. It never injects, never rewrites, never retries.
"""

from __future__ import annotations

import argparse
import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass

from aslmp.tools import EXIT_OK
from aslmp.tools._common import ENCODINGS, FRAMES, hexdump, parse_or_exit, usage, warn
from aslmp.wire.codec import ASCII, BINARY, Codec, SlmpCodecError
from aslmp.wire.frames import FRAMES as FRAME_FORMATS
from aslmp.wire.frames import FrameFormat, FrameType
from aslmp.wire.raw import SlmpFrameError

__all__ = ["build_parser", "run"]


@dataclass(frozen=True, slots=True)
class _Endpoint:
    host: str
    port: int

    @classmethod
    def parse(cls, text: str, *, default_port: int) -> _Endpoint:
        host, _, port = text.rpartition(":")
        if not host:
            return cls(text, default_port)
        return cls(host, int(port))

    def __str__(self) -> str:
        return f"{self.host}:{self.port}"


class _Splitter:
    """Split one direction of a byte stream into complete SLMP frames, on a copy.

    Length-driven, exactly like :class:`~aslmp.wire.reader.ResponseAccumulator`, and for
    the same reason: TCP segmentation is real on this hardware (1 of 3 identical
    1931-byte reads split at the 1460-byte MSS, FX5U-32MT/DS fw 1.065), so a splitter
    that assumed one read is one frame would mis-attribute every large response.

    A frame it cannot read is terminal for annotation in that direction: there is no way
    to resynchronise a stream whose framing has been lost without guessing, and guessing
    is how a decoder invents transactions that never happened.
    """

    __slots__ = ("_buf", "_codec", "_frame", "_response", "broken")

    def __init__(self, frame: FrameFormat, codec: Codec, *, response: bool) -> None:
        self._frame = frame
        self._codec = codec
        self._response = response
        self._buf = bytearray()
        self.broken: str | None = None

    def feed(self, data: bytes) -> list[bytes]:
        """Add bytes; return every complete frame they finished."""
        if self.broken is not None:
            return []
        self._buf += data
        out: list[bytes] = []
        head = self._frame.prefix_units(self._codec)
        while len(self._buf) >= head:
            try:
                prefix = self._frame.read_prefix(
                    bytes(self._buf), self._codec, response=self._response
                )
            except (SlmpFrameError, SlmpCodecError) as exc:
                # The two families wire/ raises for a frame that is not one. Not
                # recovered from: this stream is not what --frame and --encoding say it
                # is, so annotation stops for this direction and the text is printed
                # once, verbatim. Forwarding is untouched -- it already happened.
                self.broken = f"{type(exc).__name__}: {exc}"
                return out
            total = head + prefix.declared
            if len(self._buf) < total:
                return out
            out.append(bytes(self._buf[:total]))
            del self._buf[:total]
        return out


def _describe(frame: FrameFormat, codec: Codec, data: bytes, *, response: bool) -> str:
    """One line about a frame: command, subcommand, end code, serial."""
    try:
        if response:
            parsed = frame.parse(data, codec)
            serial = "" if parsed.serial is None else f" serial 0x{parsed.serial:04X}"
            return (
                f"end 0x{parsed.end_code:04X}{serial}, "
                f"{len(parsed.payload)} payload unit(s)"
            )
        request = frame.parse_request(data, codec)
        serial = "" if request.serial is None else f" serial 0x{request.serial:04X}"
        name = _command_name(request.command)
        return (
            f"0x{request.command:04X} sub 0x{request.subcommand:04X} "
            f"({name}){serial}, timer 0x{request.monitoring_timer:04X}"
        )
    except (SlmpFrameError, SlmpCodecError, ValueError) as exc:
        # Annotation only. The bytes were forwarded before this function was called, so
        # nothing is lost by failing to describe them -- and describing them wrongly
        # would be worse than saying so.
        return f"undecodable ({type(exc).__name__}: {exc})"


def _command_name(code: int) -> str:
    from aslmp.commands.registry import COMMANDS

    spec = COMMANDS.get(code)
    return spec.name if spec is not None else "not implemented by aslmp"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aslmp proxy",
        description=(
            "Sit between an SLMP client and a PLC, forward every byte unconditionally, "
            "and print a decoded copy of each frame with monotonic timestamps."
        ),
        epilog=(
            "The PLC serves ONE TCP connection per configured entry -- a second connect "
            "succeeds and is then FINed (measured on FX5U-32MT/DS fw 1.065) -- so while "
            "this proxy holds the entry, the original client must point at the proxy "
            "and not at the PLC."
        ),
    )
    parser.add_argument(
        "--listen",
        default="127.0.0.1:5555",
        metavar="HOST:PORT",
        help="where to accept the client (default: 127.0.0.1:5555)",
    )
    parser.add_argument(
        "--target", required=True, metavar="HOST:PORT", help="the PLC's connection entry"
    )
    parser.add_argument(
        "--frame", choices=FRAMES, default="3E", help="the entry's frame format"
    )
    parser.add_argument(
        "--encoding", choices=ENCODINGS, default="binary", help="the port-wide data code"
    )
    parser.add_argument(
        "--transport",
        choices=("tcp",),
        default="tcp",
        help="tcp only; a UDP SLMP entry on iQ-F is bound to one destination IP",
    )
    parser.add_argument(
        "--bytes",
        type=int,
        default=32,
        metavar="N",
        help="how many bytes of each frame to print (default: 32; 0 for none)",
    )
    return parser


def run(argv: Sequence[str]) -> int:
    args = parse_or_exit(build_parser(), argv)
    try:
        listen = _Endpoint.parse(args.listen, default_port=5555)
        target = _Endpoint.parse(args.target, default_port=5000)
    except ValueError as exc:
        return usage(f"--listen and --target are HOST:PORT ({exc})")
    frame = FRAME_FORMATS[FrameType(args.frame)]
    codec = BINARY if args.encoding == "binary" else ASCII
    try:
        asyncio.run(_serve(listen, target, frame, codec, show=args.bytes))
    except KeyboardInterrupt:  # pragma: no cover -- needs a real signal
        warn("proxy stopped")
    return EXIT_OK


async def _serve(
    listen: _Endpoint,
    target: _Endpoint,
    frame: FrameFormat,
    codec: Codec,
    *,
    show: int,
) -> None:
    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:  # pragma: no cover -- needs two live sockets
        peer = writer.get_extra_info("peername")
        print(f"# client {peer} connected; opening {target}")
        upstream: tuple[asyncio.StreamReader, asyncio.StreamWriter] | None = None
        refusal = ""
        try:
            upstream = await asyncio.open_connection(target.host, target.port)
        except OSError as exc:
            # Recorded, not handled here: the decision belongs below, where the client
            # socket is closed and the operator is told. The usual cause on this
            # hardware is the PLC already serving its one TCP connection for this entry.
            refusal = str(exc)
        if upstream is None:
            print(f"# cannot reach {target}: {refusal}")
            writer.close()
            return
        up_reader, up_writer = upstream
        started = time.monotonic_ns()
        tasks = (
            asyncio.create_task(
                _pump(
                    reader, up_writer, frame, codec, "-->", started,
                    response=False, show=show,
                )
            ),
            asyncio.create_task(
                _pump(
                    up_reader, writer, frame, codec, "<--", started,
                    response=True, show=show,
                )
            ),
        )
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            up_writer.close()
            writer.close()
            print(f"# client {peer} closed")

    server = await asyncio.start_server(handle, listen.host, listen.port)
    print(
        f"# aslmp proxy: {listen} -> {target}, {frame.frame_type.value} / {codec.name}\n"
        f"# forwarding unconditionally; decoding a copy. Ctrl-C to stop."
    )
    async with server:
        await server.serve_forever()


async def _pump(  # pragma: no cover -- needs two live sockets
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    frame: FrameFormat,
    codec: Codec,
    arrow: str,
    started: int,
    *,
    response: bool,
    show: int,
) -> None:
    """Copy one direction, forwarding before decoding. Never blocks on a frame boundary."""
    splitter = _Splitter(frame, codec, response=response)
    previous: int | None = None
    while True:
        chunk = await reader.read(65536)
        if not chunk:
            if writer.can_write_eof():
                writer.write_eof()
            return
        writer.write(chunk)
        await writer.drain()
        at = time.monotonic_ns()
        for message in splitter.feed(chunk):
            delta = "" if previous is None else f" (+{(at - previous) / 1e6:.2f} ms)"
            previous = at
            print(
                f"{(at - started) / 1e6:9.2f} ms {arrow} {len(message):5d} B{delta}  "
                f"{_describe(frame, codec, message, response=response)}"
            )
            if show:
                print(f"{'':13}{hexdump(message, limit=show)}")
        if splitter.broken is not None:
            print(
                f"# {arrow} decoding stopped: {splitter.broken}\n"
                f"# forwarding continues untouched. If this is the first frame, --frame "
                f"or --encoding is wrong; nothing here guesses."
            )
