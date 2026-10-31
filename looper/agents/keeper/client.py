"""Async keeper control client; connection loss never retries a write."""

import asyncio
import base64
import inspect
import logging

from .protocol import (MAX_FRAME_BYTES, MAX_STREAM_BYTES, PROTOCOL_VERSIONS, KeeperError,
                       Stream, decode_data, encode, read_frame)

logger = logging.getLogger(__name__)


class KeeperGone(ConnectionError):
    """The peer disappeared, or its stream could no longer be consumed safely."""


class KeeperClient:
    def __init__(self):
        self.on_data = lambda stream, offset, data: None
        self.on_eof = lambda stream, offset: None
        self.on_exited = lambda exit_code, signal: None
        self.on_replaced = lambda epoch: None
        self.on_closing = lambda stdout_offset, stderr_offset: None
        self.on_closed = lambda: None
        self._writer = self._reader_task = self._dispatcher_task = None
        self._pending, self._offsets = {}, {}
        self._next_id = 0

    async def connect(self, socket_path, *, timeout=10):
        await self.close()
        reader, self._writer = await asyncio.wait_for(
            asyncio.open_unix_connection(socket_path, limit=MAX_FRAME_BYTES), timeout)
        self._offsets.clear()
        # At most 64 queued frames, each carrying at most MAX_STREAM_BYTES of data.
        notifications = asyncio.Queue(maxsize=64)
        available, finished = asyncio.Event(), asyncio.Event()
        self._dispatcher_task = asyncio.create_task(self._dispatch(notifications, available, finished))
        self._reader_task = asyncio.create_task(self._read(reader, notifications, available))
        writer = self._writer
        # A task cancelled before it starts never executes its coroutine's finally.
        self._reader_task.add_done_callback(lambda _: self._disconnected(writer, available, finished))

    async def initialize(self, *, epoch, looper_version, timeout=10) -> dict:
        return await self._request("initialize", {"protocol_versions": PROTOCOL_VERSIONS,
                                   "controller": {"epoch": epoch, "looper_version": looper_version}}, timeout)

    async def subscribe(self, stream, from_offset, *, timeout=10):
        stream = Stream(stream)
        new = stream not in self._offsets
        if new:
            self._offsets[stream] = from_offset
        try:
            await self._request("stream/subscribe", {"stream": stream, "from_offset": from_offset}, timeout)
        except BaseException:
            if new:
                self._offsets.pop(stream, None)
            raise

    async def stdin_write(self, data, expect_offset, *, timeout=10) -> int:
        result = await self._request("stdin/write", {"data": base64.b64encode(data).decode("ascii"),
                                                   "expect_offset": expect_offset}, timeout)
        return result["offset"]

    async def stdin_close(self, *, timeout=10):
        await self._request("stdin/close", {}, timeout)

    async def signal(self, sig, *, timeout=10):
        await self._request("child/signal", {"signal": sig}, timeout)

    async def end(self, *, timeout=15) -> tuple[int | None, int | None]:
        # child/end deliberately uses proc_tree's fixed grace, with no grace_seconds parameter.
        result = await self._request("child/end", {}, timeout)
        return result["exit_code"], result["signal"]

    async def _request(self, method, params, timeout):
        writer = self._writer
        if writer is None or writer.is_closing():
            raise KeeperGone("keeper connection is closed")
        self._next_id += 1
        request_id = self._next_id
        frame = encode({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            async with asyncio.timeout(timeout):
                writer.write(frame)
                await writer.drain()
                return await future
        except (ConnectionError, OSError) as exc:
            await self.close()
            # close() has failed every future; retrieve this one's exception too.
            if future.done() and not future.cancelled():
                future.exception()
            raise KeeperGone("keeper connection dropped") from exc
        finally:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()

    async def _callback(self, callback, *args):
        result = callback(*args)
        if inspect.isawaitable(result):
            await result

    async def _dispatch(self, notifications, available, finished):
        try:
            while True:
                await available.wait()
                available.clear()
                while not notifications.empty():
                    callback, args = notifications.get_nowait()
                    try:
                        await self._callback(callback, *args)
                    except (Exception, asyncio.CancelledError):
                        logger.exception("keeper notification callback failed")
                        await self.close()
                        if asyncio.current_task().cancelling():
                            raise
                if finished.is_set():
                    break
        finally:
            await self._callback(self.on_closed)

    async def _read(self, reader, notifications, available):
        try:
            while True:
                message = await read_frame(reader)
                if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                    raise ValueError("invalid keeper message")
                if "id" in message:
                    future = self._pending.get(message["id"])
                    if future is not None and not future.done():
                        if "error" in message:
                            error = message["error"]
                            future.set_exception(KeeperError(error["code"], error["message"], error.get("data")))
                        else:
                            future.set_result(message["result"])
                    continue
                method, params = message["method"], message["params"]
                if method in ("stream/data", "stream/eof"):
                    stream, offset = Stream(params["stream"]), params["offset"]
                    if type(offset) is not int or self._offsets.get(stream) != offset:
                        raise ValueError("noncontiguous keeper stream")
                    if method == "stream/data":
                        data = decode_data(params["data"], MAX_STREAM_BYTES)
                        self._offsets[stream] += len(data)
                        callback, args = self.on_data, (stream, offset, data)
                    else:
                        callback, args = self.on_eof, (stream, offset)
                elif method == "child/exited":
                    callback, args = self.on_exited, (params["exit_code"], params["signal"])
                elif method == "controller/replaced":
                    callback, args = self.on_replaced, (params["epoch"],)
                elif method == "keeper/closing":
                    args = (params["stdout_offset"], params["stderr_offset"])
                    if any(type(offset) is not int or offset < 0 for offset in args):
                        raise ValueError("invalid final keeper offsets")
                    callback = self.on_closing
                else:
                    continue
                await notifications.put((callback, args))
                available.set()
        except (EOFError, KeeperError, ValueError, KeyError, TypeError, ConnectionError, OSError):
            pass

    def _disconnected(self, writer, available, finished):
        writer.close()
        if self._writer is writer:
            self._writer = None
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(KeeperGone("keeper connection dropped"))
        # Wake the dispatcher without needing space for a sentinel in a full queue.
        finished.set()
        available.set()

    async def close(self):
        task, self._reader_task = self._reader_task, None
        dispatcher, self._dispatcher_task = self._dispatcher_task, None
        writer = self._writer
        if writer is not None:
            writer.close()
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if dispatcher is not None and dispatcher is not asyncio.current_task():
            await asyncio.gather(dispatcher, return_exceptions=True)
        if writer is not None:
            try:
                await asyncio.wait_for(writer.wait_closed(), 1)
            except (TimeoutError, ConnectionError, OSError):
                writer.transport.abort()
