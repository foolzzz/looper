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


class Buf:
    def __init__(self) -> None:
        self.data = bytearray()
        self.eof = False
        self.cv = asyncio.Condition()

    async def add(self, chunk: bytes) -> None:
        async with self.cv:
            if chunk:
                self.data += chunk
            else:
                self.eof = True
            self.cv.notify_all()

    async def take(self, quiet: float = 0.05) -> bytes:
        async with self.cv:
            while True:
                size = len(self.data)
                try:
                    await _ORIG_WAIT_FOR(self.cv.wait(), quiet)
                except asyncio.TimeoutError:
                    if len(self.data) == size:
                        out = bytes(self.data)
                        self.data.clear()
                        return out

    async def until_eof(self, timeout: float = 3.0) -> bytes:
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
    if transport is not None:
        transport.abort()
        return
    writer.close()


def pairs_to_dict(pairs: list) -> dict:
    obj = {}
    for key, value in pairs:
        obj[key] = value
    return obj


class Session:
    def __init__(self, server: AgentHTTPServer, clock: Clock, rows: Rows, port: int) -> None:
        self.server = server
        self.clock = clock
        self.rows = rows
        self.port = port
        self.clients: list[dict] = []

    def count(self, task: str) -> int:
        return len(self.server._broadcaster._subs.get(task, ()))

    async def settle(self) -> None:
        had = bool(self.clients)
        for client in self.clients:
            with contextlib.suppress(Exception):
                client["writer"].close()
        self.clients.clear()
        if not had and self.clock.blocked == 0:
            return
        for _ in range(100):
            if self.clock.release is not None and not self.clock.release.done():
                self.clock.release.set_result(True)
            for ping in list(self.clock.pings):
                if not ping.done():
                    ping.set_result(True)
            await asyncio.sleep(0.01)
            if self.clock.blocked == 0:
                await asyncio.sleep(0.02)
                if self.clock.blocked == 0:
                    return
        raise TimeoutError(f"handlers still blocked: {self.clock.blocked}")

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
        await self.settle()
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
            buf = Buf()
            asyncio.create_task(self._pump(reader, buf))
            writer.write(raw)
            await writer.drain()
            self.clients.append({"writer": writer, "buf": buf})

    async def _pump(self, reader: asyncio.StreamReader, buf: Buf) -> None:
        try:
            while True:
                chunk = await reader.read(1 << 16)
                await buf.add(chunk)
                if not chunk:
                    return
        except Exception:
            await buf.add(b"")

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
            parts.append(await client["buf"].take())
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
            # A FIN often lets the next small write succeed, so the handler
            # stays subscribed. RST makes that write fail. The reference
            # handler then unsubscribes in finally; it does not unsubscribe
            # on the close itself.
            abort_writer(client["writer"])
            return seen
        if kind == "subs":
            want = step.get("n")
            if want is None:
                marks.append(self.count(task))
                return seen
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 2.0
            while self.count(task) != int(want):
                if loop.time() > deadline:
                    raise TimeoutError(f"subs {self.count(task)} want {want}")
                await asyncio.sleep(0.01)
            marks.append(int(want))
            return seen
        raise RuntimeError(f"unknown step {kind}")


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
        try:
            await attempt.start()
        except OSError:
            continue
        server = attempt
        port = candidate
        break
    if server is None:
        raise RuntimeError("no port above 18440")
    session = Session(server, clock, rows, port)
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
