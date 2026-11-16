"""A Worker's ACP turn and conservative receipts for Grok's interjection extension.

An interjection notification acknowledges queuing. Only model output starting after a
subsequent tool result confirms consumption; a turn ending sooner leaves it unread.
"""

from __future__ import annotations

import asyncio
import json
import os
import selectors
import signal
import subprocess
import time
import uuid as uuids
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any

from ..claude_cli import iter_lines
from .channel import DeliveryResult, RoomSteerWindow, delivery_attempt
from .grok_acp import CANCEL_GRACE_SEC, INIT_TIMEOUT_SEC, PROTOCOL_VERSION, SETUP_TIMEOUT_SEC, AcpTurn

INTERJECT = "_x.ai/interject"
INTERJECTION = "_x.ai/session/interjection"
_SEND_TIMEOUT_SEC = 4.0


def probe_interject(executable: str, *, home: str, env: dict[str, str], timeout: float) -> bool:
    """No session or model call: only a missing-session error proves the hidden method exists."""
    requests = [
        {"id": 1, "method": "initialize", "params": {"protocolVersion": PROTOCOL_VERSION,
         "clientCapabilities": {}, "clientInfo": {"name": "looper", "version": "1"}}},
        {"id": 2, "method": INTERJECT,
         "params": {"sessionId": "looper-probe-" + uuids.uuid4().hex, "text": "probe"}},
    ]
    process = None
    try:
        process = subprocess.Popen([executable, "agent", "--always-approve", "stdio"],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   cwd=home, env=env, start_new_session=True)
        assert process.stdin is not None and process.stdout is not None
        deadline, buffered = time.monotonic() + timeout, b""
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            for request in requests:
                process.stdin.write((json.dumps({"jsonrpc": "2.0", **request}) + "\n").encode())
                process.stdin.flush()
                while True:
                    if time.monotonic() >= deadline:
                        return False
                    if b"\n" not in buffered:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0 or not selector.select(remaining):
                            return False
                        chunk = os.read(process.stdout.fileno(), 65536)
                        if not chunk:
                            return False
                        buffered += chunk
                        continue
                    line, buffered = buffered.split(b"\n", 1)
                    try:
                        message = json.loads(line)
                    except (ValueError, UnicodeDecodeError):
                        continue
                    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or message.get("id") != request["id"]:
                        continue
                    if request["method"] == "initialize":
                        result = message.get("result")
                        if ("error" in message or not isinstance(result, dict)
                                or type(result.get("protocolVersion")) is not int
                                or result["protocolVersion"] != PROTOCOL_VERSION):
                            return False
                        break
                    error = message.get("error")
                    return ("result" not in message and isinstance(error, dict) and error.get("code") == -32602
                            and any("session not found" in str(error.get(field, "")).lower()
                                    for field in ("message", "data")))
    except OSError:
        return False
    finally:
        if process is not None:
            # ACP is a resident process: completion of the probe does not imply EOF.
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.communicate(timeout=1)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate()
    return False


def _interject_queued(response: Mapping[str, Any]) -> bool:
    """Accept Grok 1.0.77's nested status and the flat JSON-RPC result shape."""
    result = response.get("result")
    return isinstance(result, Mapping) and (
        result.get("status") == "queued"
        or isinstance(nested := result.get("result"), Mapping) and nested.get("status") == "queued")


@dataclass
class _Interjection:
    text: str
    accepted: bool = False
    notified_after: int | None = None
    observed: bool = False


class _Phase(Enum):
    NEW = auto()
    INITIALIZING = auto()
    CREATING = auto()
    RUNNING = auto()
    FINISHED = auto()


class GrokChannel:
    """One process, one turn. ``events`` owns stdout and preserves notification order."""

    def __init__(self, process: asyncio.subprocess.Process, *, model: str | None, cwd: str,
                 max_turns: int = 0) -> None:
        self._process, self._model, self._cwd = process, model, cwd
        self._max_turns = max_turns
        self._lock = asyncio.Lock()
        self._ready = asyncio.Event()
        self._phase = _Phase.NEW
        self._prompt = ""
        self._next_id = 0
        self._setup_id: int | None = None
        self._prompt_id: int | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._interjections: dict[str, _Interjection] = {}
        self._request_delivery: dict[int, str] = {}
        self._session: str | None = None
        self._turn: AcpTurn | None = None
        self._deadline: float | None = None
        self._startup_error: str | None = None
        self._stop: str | None = None
        self._began = 0.0
        self._tool_results = 0
        self._model_open = False
        self._response_start = 0
        self._last_response_start: int | None = None
        self._room_window = RoomSteerWindow()
        self._inbox: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    async def _write(self, method: str, params: dict[str, Any], *, request: bool = True) -> int:
        assert self._process.stdin is not None
        self._next_id += 1
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": params}
        if request:
            message["id"] = self._next_id
        self._process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode())
        await self._process.stdin.drain()
        return self._next_id

    async def start(self, prompt: str) -> None:
        if self._phase is not _Phase.NEW:
            raise RuntimeError("the Grok ACP run has already started")
        self._phase, self._prompt = _Phase.INITIALIZING, prompt
        self._deadline = asyncio.get_running_loop().time() + INIT_TIMEOUT_SEC
        try:
            async with asyncio.timeout_at(self._deadline), self._lock:
                self._setup_id = await self._write("initialize", {"protocolVersion": PROTOCOL_VERSION,
                    "clientCapabilities": {}, "clientInfo": {"name": "looper", "version": "1"}})
        except TimeoutError:
            self._startup_error = "initialize write timed out"

    def _receipts(self) -> list[dict[str, Any]]:
        events = []
        for ident, delivery in list(self._interjections.items()):
            if delivery.accepted and delivery.observed:
                events.append({"type": "user", "isReplay": True, "uuid": ident, "session_id": self._session,
                               "message": {"role": "user", "content": delivery.text}})
                del self._interjections[ident]
        return events

    def _observe(self, message: Mapping[str, Any]) -> None:
        params = message["params"]
        if message.get("method") == INTERJECTION:
            ident = params.get("interjectionId")
            delivery = self._interjections.get(ident) if isinstance(ident, str) else None
            if delivery is not None and delivery.notified_after is None:
                delivery.notified_after = self._tool_results
            return
        update = params.get("update")
        if not isinstance(update, Mapping):
            return
        kind = update.get("sessionUpdate")
        if kind == "tool_call_update" and update.get("status") in ("completed", "failed"):
            self._tool_results += 1
        content = update.get("content")
        output = (kind in ("agent_message_chunk", "agent_thought_chunk") and isinstance(content, Mapping)
                  and isinstance(content.get("text"), str) and bool(content["text"]))
        completed = message.get("method") == "_x.ai/session_notification" and kind == "response_completed"
        if output or completed:
            if not self._model_open:
                self._response_start = self._tool_results
            if output:
                self._observe_output(self._response_start)
            if completed:
                # Usage alone is not model output. For a tool-only response, its
                # first tool_call is output; later calls keep this response's start.
                self._last_response_start = self._response_start
            self._model_open = not completed
        elif kind == "tool_call" and self._last_response_start is not None:
            self._observe_output(self._last_response_start)

    def _observe_output(self, started_after: int) -> None:
        for delivery in self._interjections.values():
            if delivery.notified_after is not None and started_after > delivery.notified_after:
                delivery.observed = True

    async def _read(self) -> None:
        """Drain while the Worker handles an event and awaits a steer ACK.

        Notifications stay ordered in the inbox. RPC acceptance must be resolved
        independently: room delivery sends from inside the event consumer itself.
        """
        assert self._process.stdout is not None
        lines = iter_lines(self._process.stdout)
        try:
            async for raw in lines:
                try:
                    message = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                    continue
                params = message.get("params")
                if (message.get("method") == "session/update" and isinstance(params, Mapping)
                        and params.get("sessionId") == self._session):
                    update = params.get("update")
                    if isinstance(update, Mapping) and isinstance(call_id := update.get("toolCallId"), str):
                        if update.get("sessionUpdate") == "tool_call":
                            self._room_window.tool_started(call_id)
                        elif (update.get("sessionUpdate") == "tool_call_update"
                              and update.get("status") in ("completed", "failed")):
                            self._room_window.tool_finished(call_id)
                ident = message.get("id")
                if (message.get("method") is None and isinstance(ident, int) and not isinstance(ident, bool)
                        and ("result" in message) != ("error" in message) and ident in self._pending):
                    delivery_id = self._request_delivery.get(ident)
                    delivery = self._interjections.get(delivery_id) if delivery_id else None
                    if delivery is not None:
                        delivery.accepted = _interject_queued(message)
                        if "error" in message:
                            self._interjections.pop(delivery_id, None)
                    future = self._pending[ident]
                    if not future.done():
                        future.set_result(message)
                self._inbox.put_nowait(message)
        finally:
            self._inbox.put_nowait(None)
            self._fail_pending()
            await lines.aclose()

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        reader = asyncio.create_task(self._read())
        try:
            if self._startup_error:
                raise RuntimeError(self._startup_error)
            while self._phase is not _Phase.FINISHED:
                if self._deadline is not None and asyncio.get_running_loop().time() >= self._deadline:
                    raise TimeoutError
                async with asyncio.timeout_at(self._deadline):
                    message = await self._inbox.get()
                if message is None:
                    raise RuntimeError("the process ended before its prompt result")
                method, ident = message.get("method"), message.get("id")
                if method is None:
                    if type(ident) is not int or ("result" in message) == ("error" in message):
                        continue
                    if ident == self._setup_id:
                        result = message.get("result")
                        if not isinstance(result, dict) or "error" in message:
                            raise RuntimeError("ACP setup was rejected")
                        async with self._lock:
                            if self._phase is _Phase.INITIALIZING:
                                if type(result.get("protocolVersion")) is not int or result["protocolVersion"] != PROTOCOL_VERSION:
                                    raise RuntimeError("unsupported ACP protocol version")
                                self._phase = _Phase.CREATING
                                self._deadline = asyncio.get_running_loop().time() + SETUP_TIMEOUT_SEC
                                self._setup_id = await self._write("session/new", {"cwd": self._cwd, "mcpServers": []})
                            else:
                                session = result.get("sessionId")
                                if not isinstance(session, str) or not session:
                                    raise RuntimeError("session/new returned no session id")
                                self._session = session
                                self._turn = AcpTurn(model=self._model, session_id=session)
                                self._began = time.monotonic()
                                self._prompt_id = await self._write("session/prompt", {"sessionId": session,
                                    "prompt": [{"type": "text", "text": self._prompt}]})
                                self._setup_id, self._deadline = None, None
                                self._phase = _Phase.RUNNING
                                self._ready.set()
                        if self._turn is not None:
                            yield self._turn.init(cwd=self._cwd)
                    elif ident == self._prompt_id and self._turn is not None:
                        events = self._turn.finish(message, duration_ms=int((time.monotonic() - self._began) * 1000),
                                                   stop=self._stop)
                        self.end_input()
                        for event in events:
                            yield event
                    else:
                        for event in self._receipts():
                            yield event
                    continue
                if ident is not None:
                    assert self._process.stdin is not None
                    reply = ({"result": {"outcome": {"outcome": "cancelled"}}}
                             if method == "session/request_permission" else
                             {"error": {"code": -32601, "message": "unsupported client method"}})
                    async with self._lock:
                        self._process.stdin.write((json.dumps({"jsonrpc": "2.0", "id": ident, **reply}) + "\n").encode())
                        await self._process.stdin.drain()
                    continue
                params = message.get("params")
                if (method not in ("session/update", "_x.ai/session_notification", INTERJECTION)
                        or not isinstance(params, dict) or params.get("sessionId") != self._session
                        or self._turn is None or self._stop is not None):
                    continue
                self._observe(message)
                for event in self._receipts():
                    yield event
                for event in self._turn.feed(message):
                    yield event
                if self._max_turns and self._turn.responses > self._max_turns:
                    self._stop = "max_turns"
                    self._deadline = asyncio.get_running_loop().time() + CANCEL_GRACE_SEC
                    async with self._lock:
                        await self._write("session/cancel", {"sessionId": self._session}, request=False)
        except (TimeoutError, RuntimeError, BrokenPipeError, ConnectionResetError) as exc:
            turn = self._turn or AcpTurn(model=self._model, session_id=self._session or "")
            for event in turn.finish({"error": {"message": f"Grok ACP: {str(exc) or 'acknowledgement timed out'}"}},
                                     duration_ms=0, stop=self._stop):
                yield event
        finally:
            self.end_input()
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)

    @property
    def send_timeout(self) -> float:
        return _SEND_TIMEOUT_SEC

    async def wait_input_ready(self) -> None:
        if self._phase is _Phase.NEW:
            raise RuntimeError("Grok cannot steer before the run has started")
        await self._ready.wait()
        if self._phase is not _Phase.RUNNING or self._stop is not None:
            raise RuntimeError("the Grok run has ended")

    async def send(self, text: str, *, uuid: str | None = None) -> DeliveryResult:
        ident: int | None = None
        try:
            async with delivery_attempt(self.send_timeout) as attempt:
                await self.wait_input_ready()
                async with self._lock:
                    if self._phase is not _Phase.RUNNING or self._stop is not None:
                        return DeliveryResult.REJECTED
                    self._room_window.check(uuid)
                    delivery_id = uuid or str(uuids.uuid4())
                    # Register before drain: stdout may acknowledge while the write is still pending.
                    ident = self._next_id + 1
                    future = asyncio.get_running_loop().create_future()
                    self._pending[ident] = future
                    self._request_delivery[ident] = delivery_id
                    self._interjections[delivery_id] = _Interjection(text)
                    attempt.writing = True
                    await self._write(INTERJECT, {"sessionId": self._session, "text": text,
                                                  "interjectionId": delivery_id})
                response = await future
            if attempt.result is not DeliveryResult.ACCEPTED:
                return attempt.result
            if "error" in response:
                return DeliveryResult.REJECTED
            return DeliveryResult.ACCEPTED if _interject_queued(response) else DeliveryResult.UNKNOWN
        finally:
            if ident is not None:
                self._request_delivery.pop(ident, None)
                if (future := self._pending.pop(ident, None)) is not None:
                    future.cancel()

    def _fail_pending(self) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ConnectionResetError("the Grok run has ended"))
                future.exception()

    def dropped_input(self) -> list[str]:
        """Recover unread Planner input; room deliveries keep their unconfirmed cursor."""
        return [delivery.text for ident, delivery in self._interjections.items()
                if delivery.accepted and not delivery.observed and not ident.startswith("looper-reminder-")]

    def end_input(self) -> None:
        self._phase = _Phase.FINISHED
        self._ready.set()
        self._fail_pending()
        assert self._process.stdin is not None
        if not self._process.stdin.is_closing():
            self._process.stdin.close()
