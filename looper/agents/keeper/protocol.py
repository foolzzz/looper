"""Line-delimited JSON-RPC and byte limits shared by keeper peers.

Notifications share one ordered channel: stream/data, stream/eof, child/exited,
controller/replaced and keeper/closing. The client resolves responses in its
reader and dispatches notifications in wire order on a separate task. Its queue
holds at most 64 notifications (at most 4 MiB of decoded data); a full queue
backpressures the reader. Connection loss fails pending RPCs immediately, then
drains queued callbacks before calling on_closed once.

After child exit, delivery is bounded to 10 seconds. Before closing, the keeper
queues keeper/closing with {stdout_offset, stderr_offset}, the final byte lengths
of stdout.log and stderr.log, even if stream/eof could not be delivered. The
client calls on_closing(stdout_offset, stderr_offset) in notification order.
Callers compare these lengths with their own consumed offsets and read any
missing suffix from the log files. These are not the reader's prefetched offsets.
The final frame uses the existing bounded socket flush: a peer that remains
unreadable through that flush, or a broken connection, can still lose it. Without
stream/eof, callers must check final offsets or fall back to the logs if the
closing frame is also missing.
"""

import asyncio
import base64
import binascii
import json
import re
from enum import IntEnum, StrEnum

PROTOCOL_VERSIONS = ("1.0",)
MAX_FRAME_BYTES = 4 * 1024 * 1024
MAX_STREAM_BYTES = 65536
MAX_STDIN_BYTES = 1024 * 1024


class ErrorCode(IntEnum):
    PARSE = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL = -32603
    VERSION_MISMATCH = -32010
    STALE_CONTROLLER = -32011
    NOT_INITIALIZED = -32012
    OFFSET_MISMATCH = -32013
    CHILD_EXITED = -32014
    FRAME_TOO_LARGE = -32015
    NOT_CONTROLLER = -32016


class Stream(StrEnum):
    STDOUT = "stdout"
    STDERR = "stderr"


class KeeperSignal(StrEnum):
    TERM = "TERM"
    INT = "INT"
    KILL = "KILL"


class KeeperError(Exception):
    def __init__(self, code, message, data=None):
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


def negotiate(offered) -> str | None:
    common = [v for v in offered if isinstance(v, str) and re.fullmatch(r"[0-9]+\.[0-9]+", v)
              and v in PROTOCOL_VERSIONS]
    return max(common, key=lambda v: tuple(map(int, v.split("."))), default=None)


def encode(value) -> bytes:
    frame = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode() + b"\n"
    if len(frame) > MAX_FRAME_BYTES:
        raise KeeperError(ErrorCode.FRAME_TOO_LARGE, "frame too large")
    return frame


async def read_frame(reader):
    try:
        frame = await reader.readuntil(b"\n")
    except asyncio.LimitOverrunError:
        raise KeeperError(ErrorCode.FRAME_TOO_LARGE, "frame too large") from None
    except asyncio.IncompleteReadError as exc:
        if not exc.partial:
            raise EOFError from None
        raise KeeperError(ErrorCode.PARSE, "unterminated frame") from None
    if len(frame) > MAX_FRAME_BYTES:
        raise KeeperError(ErrorCode.FRAME_TOO_LARGE, "frame too large")
    try:
        return json.loads(frame.decode("utf-8"), parse_constant=_invalid_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise KeeperError(ErrorCode.PARSE, "invalid JSON") from None


def _invalid_constant(value):
    raise ValueError(value)


def decode_data(value, limit):
    try:
        if not isinstance(value, str) or len(value) > 4 * ((limit + 2) // 3):
            raise ValueError()
        data = base64.b64decode(value, validate=True)
        if len(data) > limit:
            raise ValueError()
        return data
    except (ValueError, binascii.Error):
        raise KeeperError(ErrorCode.INVALID_PARAMS, "invalid or oversized base64 data") from None
