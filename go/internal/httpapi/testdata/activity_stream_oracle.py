"""Drive the real activity SSE handler over a socket.

The process imports looper.agents.http_server.AgentHTTPServer and
looper.agents.broadcast.Broadcaster, listens on 127.0.0.1:0, and answers one
JSON command per line on stdin. Each command opens a TCP client to that
server, so the bytes are what the reference handler writes.

list_activity is replaced with an in-process fake so the cases do not need a
database. The handler still calls it, pages 500 rows, and turns failures into
an empty replay. asyncio.wait_for is wrapped only when the timeout is the
handler's 20.0 seconds: the wrapper records that value and completes when the
command fires it. A queue item that is already ready is returned even if the
timeout is also ready, which is what asyncio.wait_for does when Queue.get can
finish immediately.

A take returns only when the HTTP head (the first read) and an exact number of
complete SSE frames are buffered. Silence is not completion: a partial frame
stays in the buffer until it is finished or the bounded timeout fires. drop
waits until the server transport has observed the loss, and the handler task
is still the thing that unsubscribes, in its finally, after the next write
fails. play resets rows and the clock only after that play's handler and pump
tasks have finished.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import socket
import struct
import sys
import traceback
from types import SimpleNamespace

from looper.agents.broadcast import Broadcaster
from looper.agents.http_server import AgentHTTPServer
import looper.db.activity as activity

TOKEN = "loopgo-activity-oracle"
_ORIG_WAIT_FOR = asyncio.wait_for
# Bounds for events that should already be true. They are not idle delays.
FRAME_TIMEOUT = 8.0
HANDLER_TIMEOUT = 8.0
DISCONNECT_TIMEOUT = 3.0
SUBS_TIMEOUT = 3.0


class Rows:
    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.once = False
        self.once_done = False
        self.fail_after: int | None = None
        self.calls: list[dict] = []


class Clock:
    def __init__(self) -> None:
        self.timeouts: list[float] = []
        self.entries = 0
        self.blocked = 0
        self.pings: list[asyncio.Future] = []
        self.hold_once = False
        self.release: asyncio.Future | None = None
        self.held = asyncio.Event()
        self.cv = asyncio.Condition()

    def reset(self, hold: bool) -> None:
        self.timeouts.clear()
        self.entries = 0
        self.blocked = 0
        self.pings.clear()
        self.hold_once = hold
        self.release = None
        self.held.clear()

    async def wait_for_20(self, aw):
        self.timeouts.append(20.0)
        fut = asyncio.ensure_future(aw)
        loop = asyncio.get_running_loop()
        ping = loop.create_future()
        release = None
        async with self.cv:
            self.pings.append(ping)
            self.blocked += 1
            self.entries += 1
            if self.hold_once:
                self.hold_once = False
                release = loop.create_future()
                self.release = release
                self.held.set()
            self.cv.notify_all()
        try:
            if release is not None:
                await release
            done, _pending = await asyncio.wait({fut, ping}, return_when=asyncio.FIRST_COMPLETED)
            if fut in done:
                return fut.result()
            fut.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await fut
            raise asyncio.TimeoutError
        finally:
            async with self.cv:
                self.blocked = max(0, self.blocked - 1)
                self.pings = [item for item in self.pings if item is not ping]
                self.cv.notify_all()

    async def wait_fresh(self, n: int, previous: int, timeout: float = 3.0) -> int:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        async with self.cv:
            while self.entries <= previous or self.blocked < n:
                left = deadline - loop.time()
                if left <= 0:
                    raise TimeoutError(
                        f"arm entries={self.entries} blocked={self.blocked} previous={previous} need={n}"
                    )
                try:
                    await _ORIG_WAIT_FOR(self.cv.wait(), left)
                except asyncio.TimeoutError as exc:
                    raise TimeoutError(
                        f"arm entries={self.entries} blocked={self.blocked} previous={previous} need={n}"
                    ) from exc
            return self.entries

    async def wait_entered(self, timeout: float = 3.0) -> None:
        await _ORIG_WAIT_FOR(self.held.wait(), timeout)

    def fire(self) -> None:
        for ping in self.pings:
            if not ping.done():
                ping.set_result(True)
                return
        raise RuntimeError("clock has no waiter")

    def release_hold(self) -> None:
        if self.release is None or self.release.done():
            raise RuntimeError("clock is not held")
        self.release.set_result(True)


def frame_end(data: bytearray, start: int, frames: int) -> int | None:
    """End index of exactly `frames` SSE frames beginning at start.

    A frame ends at ``\\n\\n``. A short buffer or a trailing partial frame
    returns None so the caller keeps waiting. Extra complete frames stay in
    the buffer, after the returned index.
    """
    i = start
    for _ in range(frames):
        mark = data.find(b"\n\n", i)
        if mark < 0:
            return None
        i = mark + 2
    return i


def complete_frames(data: bytearray, start: int) -> int:
    n = 0
    i = start
    while True:
        mark = data.find(b"\n\n", i)
        if mark < 0:
            return n
        n += 1
        i = mark + 2


class Buf:
    def __init__(self) -> None:
        self.data = bytearray()
        self.eof = False
        self.headers_pending = True
        self.cv = asyncio.Condition()

    async def add(self, chunk: bytes) -> None:
        async with self.cv:
            if chunk:
                self.data += chunk
            else:
                self.eof = True
            self.cv.notify_all()

    def _end(self, frames: int) -> int | None:
        if frames < 0:
            raise RuntimeError(f"frame count {frames}")
        i = 0
        if self.headers_pending:
            mark = self.data.find(b"\r\n\r\n")
            if mark < 0:
                return None
            i = mark + 4
        return frame_end(self.data, i, frames)

    async def take(self, frames: int, timeout: float = FRAME_TIMEOUT) -> bytes:
        if frames < 0:
            raise RuntimeError(f"frame count {frames}")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        async with self.cv:
            while True:
                end = self._end(frames)
                if end is not None:
                    out = bytes(self.data[:end])
                    del self.data[:end]
                    self.headers_pending = False
                    return out
                if self.eof:
                    raise TimeoutError(self._short(frames, "stream closed"))
                left = deadline - loop.time()
                if left <= 0:
                    raise TimeoutError(self._short(frames, "timed out"))
                try:
                    await _ORIG_WAIT_FOR(self.cv.wait(), left)
                except asyncio.TimeoutError:
                    continue

    def _short(self, frames: int, why: str) -> str:
        start = 0
        if self.headers_pending:
            mark = self.data.find(b"\r\n\r\n")
            if mark < 0:
                return f"{why}: want {frames} frames, headers incomplete, {len(self.data)} bytes"
            start = mark + 4
        seen = complete_frames(self.data, start)
        tail = bytes(self.data[start:])[:80]
        return f"{why}: want {frames} complete frames, saw {seen}, tail {tail!r}"

    async def until_eof(self, timeout: float = FRAME_TIMEOUT) -> bytes:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        async with self.cv:
            while not self.eof:
                left = deadline - loop.time()
                if left <= 0:
                    raise TimeoutError("stream did not close")
                try:
                    await _ORIG_WAIT_FOR(self.cv.wait(), left)
                except asyncio.TimeoutError as exc:
                    raise TimeoutError("stream did not close") from exc
            out = bytes(self.data)
            self.data.clear()
            return out


def abort_writer(writer: asyncio.StreamWriter) -> None:
    # TransportSocket hides close(). Linger 0 plus abort sends RST, so the
    # handler's next write fails. A plain FIN often does not.
    sock = writer.get_extra_info("socket")
    if sock is not None:
        with contextlib.suppress(OSError):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    transport = writer.transport
    if transport is None:
        writer.close()
        return
    if not transport.is_closing():
        transport.abort()


def endpoint(addr) -> tuple:
    if not addr:
        raise RuntimeError("missing socket address")
    return (str(addr[0]), int(addr[1]))


def pairs_to_dict(pairs: list) -> dict:
    obj = {}
    for key, value in pairs:
        obj[key] = value
    return obj


class Handlers:
    """Server connection tasks, keyed by the client socket's own address."""

    def __init__(self) -> None:
        self.live: dict[tuple, dict] = {}
        self.pending: set[tuple] = set()

    def expect(self, peer: tuple) -> None:
        if peer not in self.live:
            self.pending.add(peer)

    def started(self, peer: tuple, task: asyncio.Task, writer: asyncio.StreamWriter) -> None:
        self.pending.discard(peer)
        self.live[peer] = {"task": task, "writer": writer}

        def done(finished: asyncio.Task, peer: tuple = peer, task: asyncio.Task = task) -> None:
            cur = self.live.get(peer)
            if cur is not None and cur["task"] is task:
                self.live.pop(peer, None)
            if not finished.cancelled():
                finished.exception()

        task.add_done_callback(done)

    def writer_for(self, client: asyncio.StreamWriter) -> asyncio.StreamWriter:
        peer = endpoint(client.get_extra_info("sockname"))
        info = self.live.get(peer)
        if info is None:
            raise RuntimeError(f"no server handler for {peer}; live {list(self.live)}")
        return info["writer"]


class Session:
    def __init__(
        self, server: AgentHTTPServer, clock: Clock, rows: Rows, port: int, handlers: Handlers,
    ) -> None:
        self.server = server
        self.clock = clock
        self.rows = rows
        self.port = port
        self.handlers = handlers
        self.clients: list[dict] = []
        self.pumps: set[asyncio.Task] = set()

    def count(self, task: str) -> int:
        return len(self.server._broadcaster._subs.get(task, ()))

    def _kick_clock(self) -> None:
        if self.clock.release is not None and not self.clock.release.done():
            self.clock.release.set_result(True)
        for ping in list(self.clock.pings):
            if not ping.done():
                ping.set_result(True)

    def _abort_server(self, client: dict) -> None:
        try:
            server_writer = self.handlers.writer_for(client["writer"])
        except RuntimeError:
            return
        transport = server_writer.transport
        if transport is not None and not transport.is_closing():
            transport.abort()

    async def _await_idle(self, clients: list[dict]) -> None:
        # clock.blocked == 0 is also true while a handler is still replaying
        # or draining. Wait for the tasks themselves.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + HANDLER_TIMEOUT
        while True:
            for client in clients:
                self._abort_server(client)
            self._kick_clock()
            pending_handlers = [
                info["task"] for info in self.handlers.live.values() if not info["task"].done()
            ]
            pending_pumps = [pump for pump in self.pumps if not pump.done()]
            if not pending_handlers and not pending_pumps and not self.handlers.pending:
                break
            left = deadline - loop.time()
            if left <= 0:
                raise TimeoutError(
                    f"handlers {len(pending_handlers)} pending {len(self.handlers.pending)} "
                    f"pumps {len(pending_pumps)}"
                )
            waitable = pending_handlers + pending_pumps
            if waitable:
                await asyncio.wait(waitable, timeout=min(left, 0.05))
            else:
                await _ORIG_WAIT_FOR(asyncio.sleep(0.001), left)
        leftover = list(self.server._broadcaster._subs)
        if leftover:
            raise TimeoutError(f"subscribers still attached: {leftover}")

    async def settle(self) -> None:
        clients = list(self.clients)
        self.clients.clear()
        for client in clients:
            abort_writer(client["writer"])
        await self._await_idle(clients)

    async def play(self, cmd: dict) -> dict:
        await self.settle()
        self.clock.reset(bool(cmd.get("hold")))
        self.rows.rows = [pairs_to_dict(pairs) for pairs in cmd.get("rows") or []]
        self.rows.once = bool(cmd.get("once"))
        self.rows.once_done = False
        self.rows.fail_after = cmd.get("fail_after")
        self.rows.calls = []
        parts: list[bytes] = []
        marks: list[int] = []
        seen = 0
        err: Exception | None = None
        try:
            await self._open(cmd)
            for step in cmd.get("steps") or []:
                seen = await self._step(cmd, step, seen, parts, marks)
        except Exception as exc:
            err = exc
        task = cmd.get("task") or ""
        result = {
            "parts": [base64.b64encode(part).decode("ascii") for part in parts],
            "calls": list(self.rows.calls),
            "timeouts": list(self.clock.timeouts),
            "subs": self.count(task),
            "marks": marks,
        }
        try:
            await self.settle()
        except Exception as settle_err:
            if err is None:
                raise
            raise settle_err from err
        if err is not None:
            raise err
        return result

    async def _open(self, cmd: dict) -> None:
        n = int(cmd.get("clients") or 1)
        task = cmd["task"]
        query = cmd.get("query") or ""
        target = f"/api/tasks/{task}/activity/stream"
        if query:
            target += "?" + query
        raw = (
            f"GET {target} HTTP/1.1\r\n"
            f"Host: 127.0.0.1\r\n"
            f"Authorization: Bearer {TOKEN}\r\n"
            f"\r\n"
        ).encode("utf-8")
        for _ in range(n):
            reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
            self.handlers.expect(endpoint(writer.get_extra_info("sockname")))
            buf = Buf()
            pump = asyncio.create_task(self._pump(reader, buf))
            self.pumps.add(pump)

            def forget(done: asyncio.Task, pumps: set[asyncio.Task] = self.pumps) -> None:
                pumps.discard(done)
                if not done.cancelled():
                    done.exception()

            pump.add_done_callback(forget)
            writer.write(raw)
            await writer.drain()
            self.clients.append({"writer": writer, "buf": buf, "pump": pump})

    async def _pump(self, reader: asyncio.StreamReader, buf: Buf) -> None:
        try:
            while True:
                chunk = await reader.read(1 << 16)
                await buf.add(chunk)
                if not chunk:
                    return
        except Exception:
            await buf.add(b"")

    async def _wait_lost(self, client: dict) -> None:
        server_writer = self.handlers.writer_for(client["writer"])
        loop = asyncio.get_running_loop()
        deadline = loop.time() + DISCONNECT_TIMEOUT
        while True:
            transport = server_writer.transport
            if transport is None or transport.is_closing():
                return
            left = deadline - loop.time()
            if left <= 0:
                raise TimeoutError("server transport did not observe the disconnect")
            await _ORIG_WAIT_FOR(asyncio.sleep(0.001), left)

    async def _wait_subs(self, task: str, want: int) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + SUBS_TIMEOUT
        while self.count(task) != want:
            left = deadline - loop.time()
            if left <= 0:
                raise TimeoutError(f"subs {self.count(task)} want {want}")
            closing = [
                info["task"]
                for info in self.handlers.live.values()
                if not info["task"].done() and _writer_closing(info["writer"])
            ]
            # Fewer subscribers means a dropped handler has to finish. Its
            # finally unsubscribes. Waiting on that task is the event; a
            # later poll would not write again.
            if self.count(task) > want and closing:
                await asyncio.wait(closing, timeout=left, return_when=asyncio.FIRST_COMPLETED)
                continue
            await _ORIG_WAIT_FOR(asyncio.sleep(0.005), left)

    async def _step(self, cmd: dict, step: dict, seen: int, parts: list[bytes], marks: list[int]) -> int:
        kind = step["do"]
        client = self.clients[int(step.get("client") or 0)]
        task = cmd["task"]
        if kind == "arm":
            return await self.clock.wait_fresh(int(step.get("n") or 1), seen)
        if kind == "entered":
            await self.clock.wait_entered()
            return self.clock.entries
        if kind == "take":
            if "frames" not in step:
                raise RuntimeError("take requires frames")
            parts.append(await client["buf"].take(int(step["frames"])))
            return seen
        if kind == "eof":
            parts.append(await client["buf"].until_eof())
            return seen
        if kind == "publish":
            self.server._broadcaster.publish(task, pairs_to_dict(step["event"]))
            return seen
        if kind == "fire":
            self.clock.fire()
            return seen
        if kind == "release":
            self.clock.release_hold()
            return seen
        if kind == "close":
            self.server._broadcaster.close_task(task)
            return seen
        if kind == "drop":
            # The client RST can still lose the race to the next write. Abort
            # the server transport as well, then wait until that transport is
            # closing. The handler stays parked on its queue, so the
            # subscriber remains until the next write fails and finally
            # unsubscribes.
            abort_writer(client["writer"])
            self._abort_server(client)
            await self._wait_lost(client)
            return seen
        if kind == "subs":
            want = step.get("n")
            if want is None:
                marks.append(self.count(task))
                return seen
            await self._wait_subs(task, int(want))
            marks.append(int(want))
            return seen
        raise RuntimeError(f"unknown step {kind}")


def _writer_closing(writer: asyncio.StreamWriter) -> bool:
    transport = writer.transport
    return transport is None or transport.is_closing()


async def list_activity(task_id: str, *, since_seq: int = 0, limit: int = 100, pool=None):
    rows.calls.append({"task": task_id, "since": str(since_seq), "limit": int(limit)})
    if rows.fail_after is not None and len(rows.calls) == rows.fail_after + 1:
        raise RuntimeError("db down")
    if rows.once:
        if rows.once_done:
            return []
        rows.once_done = True
        return [dict(row) for row in rows.rows]
    out = []
    for row in rows.rows:
        if row.get("seq", 0) > since_seq:
            out.append(dict(row))
        if len(out) >= limit:
            break
    return out


async def clock_wait_for(aw, timeout=None):
    if timeout != 20.0:
        return await _ORIG_WAIT_FOR(aw, timeout)
    return await clock.wait_for_20(aw)


rows = Rows()
clock = Clock()


async def amain() -> None:
    activity.list_activity = list_activity
    asyncio.wait_for = clock_wait_for
    server = None
    port = 0
    handlers = Handlers()
    # Above the Go test servers (18340-18439) and above 18000.
    for candidate in range(18440, 18640):
        attempt = AgentHTTPServer(
            host="127.0.0.1",
            port=candidate,
            operator_token=TOKEN,
            web_adapter=SimpleNamespace(),
            task_router=SimpleNamespace(),
            supervisor=SimpleNamespace(),
            broadcaster=Broadcaster(),
        )
        original = attempt._handle_connection

        async def tracked(reader, writer, original=original):
            task = asyncio.current_task()
            if task is None:
                raise RuntimeError("connection handler is not a task")
            handlers.started(endpoint(writer.get_extra_info("peername")), task, writer)
            await original(reader, writer)

        attempt._handle_connection = tracked
        try:
            await attempt.start()
        except OSError:
            continue
        server = attempt
        port = candidate
        break
    if server is None:
        raise RuntimeError("no port above 18440")
    session = Session(server, clock, rows, port, handlers)
    sys.stdout.write(json.dumps({"ready": True, "port": port}) + "\n")
    sys.stdout.flush()
    loop = asyncio.get_running_loop()
    # One play sends every history row on a single line. 501 events exceed
    # StreamReader's 64KiB default and kill the oracle before the handler runs.
    reader = asyncio.StreamReader(limit=32 * 1024 * 1024)
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)
    while True:
        line = await reader.readline()
        if not line:
            break
        try:
            cmd = json.loads(line)
            if cmd.get("op") != "play":
                raise RuntimeError(f"unknown op {cmd.get('op')!r}")
            result = await session.play(cmd)
            result["ok"] = True
        except Exception:
            result = {"ok": False, "error": traceback.format_exc()}
        sys.stdout.write(json.dumps(result) + "\n")
        sys.stdout.flush()
    await server.stop()


if __name__ == "__main__":
    asyncio.run(amain())
