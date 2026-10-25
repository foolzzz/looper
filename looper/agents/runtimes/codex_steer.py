"""A Worker's Codex app-server run, translated through the exec event adapter."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator, Mapping
from typing import Any

from ..claude_cli import iter_lines
from .codex import CodexStream

logger = logging.getLogger(__name__)
_REQUEST_TIMEOUT_SEC = 4.0


def exec_item(item: Mapping[str, Any]) -> dict[str, Any] | None:
    """An app-server item in the shape ``CodexStream`` already reads from exec."""
    kind = item.get("type", "")
    if kind == "userMessage":
        return None
    converted = {**item, "type": re.sub(r"(?<!^)(?=[A-Z])", "_", str(kind)).lower()}
    if kind == "agentMessage":
        converted.pop("phase", None)
    elif kind == "reasoning":
        parts = [text for key in ("summary", "content") for part in item.get(key) or []
                 if (text := part.get("text") if isinstance(part, Mapping) else part)]
        converted = {"id": item.get("id"), "type": "reasoning", "text": "\n".join(parts)}
    elif kind == "commandExecution":
        converted = {"id": item.get("id"), "type": "command_execution", "command": item.get("command"),
                     "aggregated_output": item.get("aggregatedOutput"), "exit_code": item.get("exitCode"),
                     "status": item.get("status")}
    elif kind == "fileChange":
        converted["changes"] = [{"path": change.get("path"), "kind": change.get("kind", {}).get("type")}
                                for change in item.get("changes") or []]
    elif kind == "plan":
        converted = {"id": item.get("id"), "type": "todo_list",
                     "items": [{"text": item.get("text", ""), "completed": False}]}
    if "status" in converted:
        converted["status"] = {"inProgress": "in_progress", "declined": "failed"}.get(
            converted["status"], converted["status"])
    return converted


def exec_events(method: str, params: Mapping[str, Any], usage: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """Notifications that exec reports too; deltas and other protocol bookkeeping are ignored."""
    if method == "turn/started":
        return [{"type": "turn.started"}]
    if method in ("item/started", "item/completed"):
        raw_item = params.get("item")
        if not isinstance(raw_item, Mapping):
            return []
        item = exec_item(raw_item)
        return [{"type": method.replace("/", "."), "item": item}] if item is not None else []
    if method == "turn/completed":
        turn = params.get("turn")
        if not isinstance(turn, Mapping):
            return []
        if turn.get("status") == "completed":
            totals = usage or {}
            return [{"type": "turn.completed", "usage": {
                "input_tokens": totals.get("inputTokens", 0),
                "cached_input_tokens": totals.get("cachedInputTokens", 0),
                "cache_write_input_tokens": totals.get("cacheWriteInputTokens", 0),
                "output_tokens": totals.get("outputTokens", 0),
            }}]
        message = "the turn was interrupted" if turn.get("status") == "interrupted" \
            else (turn["error"].get("message") if isinstance(turn.get("error"), Mapping) else None) or "the turn failed"
        return [{"type": "turn.failed", "error": {"message": message}}]
    if method == "error":
        error = params.get("error")
        return [{"type": "error", "message": error.get("message") if isinstance(error, Mapping) else None}]
    return []


class CodexChannel:
    """One app-server thread and turn; the reader drives the handshake and resolves steering replies."""

    def __init__(self, process: asyncio.subprocess.Process, *, model: str, effort: str, cwd: str,
                 keep_session: bool) -> None:
        self._process = process
        self._model = model
        self._effort = effort
        self._cwd = cwd
        self._keep_session = keep_session
        self._stream = CodexStream(model=model)
        self._lock = asyncio.Lock()
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._next_id = 0
        self._handshake: tuple[str, int] | None = None
        self._prompt = ""
        self._thread_id: str | None = None
        self._turn_id: str | None = None
        self._turn_ready = asyncio.Event()
        self._ended = False

    async def _write(self, message: dict[str, Any]) -> None:
        assert self._process.stdin is not None
        self._process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
        await self._process.stdin.drain()

    async def _request(self, method: str, params: dict[str, Any]) -> tuple[int, asyncio.Future[dict[str, Any]]]:
        """Called with the write lock held, including the request's registration."""
        self._next_id += 1
        request_id = self._next_id
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._write({"id": request_id, "method": method, "params": params})
        except BaseException:
            self._pending.pop(request_id, None)
            future.cancel()
            raise
        return request_id, future

    async def _handshake_request(self, method: str, params: dict[str, Any]) -> None:
        async with self._lock:
            request_id, _ = await self._request(method, params)
            self._handshake = method, request_id

    async def start(self, prompt: str) -> None:
        self._prompt = prompt
        await self._handshake_request("initialize", {"clientInfo": {"name": "looper", "title": None, "version": "0"}})

    def _failed(self, message: str) -> dict[str, Any]:
        self._ended = True
        self._turn_ready.set()
        return {"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "",
                "errors": [f"Codex app-server: {message}"]}

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        assert self._process.stdout is not None
        usage: Mapping[str, Any] = {}
        try:
            async for line in iter_lines(self._process.stdout):
                try:
                    message = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    logger.debug("ignoring a non-JSON Codex app-server line (%d bytes)", len(line))
                    continue
                if not isinstance(message, dict):
                    logger.debug("ignoring a Codex app-server line that is not an object")
                    continue
                request_id = message.get("id")
                if "id" in message and (isinstance(request_id, bool) or not isinstance(request_id, (int, str))):
                    logger.debug("ignoring a Codex app-server envelope with a bad id (%s)", type(request_id).__name__)
                    continue
                params = message.get("params")
                result = message.get("result")
                if ("params" in message and not isinstance(params, dict)) or (
                        "result" in message and not isinstance(result, dict)):
                    logger.debug("ignoring a Codex app-server envelope whose params or result is not an object "
                                 "(%d keys)", len(message))
                    continue
                if "method" in message:
                    method = message["method"]
                    if not isinstance(method, str):
                        logger.debug("ignoring a Codex app-server envelope with a bad method")
                        continue
                    if "id" in message:
                        async with self._lock:
                            await self._write({"id": message["id"], "error": {
                                "code": -32601, "message": "Looper runs Codex without interactive requests"}})
                        yield {"type": "system", "subtype": "codex_server_request", "method": method}
                        continue
                    params = params or {}
                    if method == "thread/tokenUsage/updated":
                        token_usage = params.get("tokenUsage")
                        total = token_usage.get("total") if isinstance(token_usage, dict) else None
                        usage = total if isinstance(total, dict) else {}
                    if method == "turn/completed":
                        self._turn_id = None
                    try:
                        translated = [event for raw in exec_events(method, params, usage)
                                      for event in self._stream.translate(raw)]
                    except (AttributeError, TypeError, ValueError) as exc:
                        logger.debug("ignoring a Codex app-server notification that does not fit its shape (%s)",
                                     type(exc).__name__)
                        continue
                    for event in translated:
                        yield event
                    continue
                future = self._pending.pop(request_id, None)
                if future is None or future.done():
                    continue
                future.set_result(message)
                if self._handshake is None or self._handshake[1] != request_id:
                    continue
                method = self._handshake[0]
                if "error" in message:
                    error = message["error"]
                    yield self._failed(str((error.get("message") if isinstance(error, dict) else None)
                                           or "request failed"))
                    return
                result = result or {}
                if method == "initialize":
                    async with self._lock:
                        await self._write({"method": "initialized"})
                    await self._handshake_request("thread/start", {
                        "cwd": self._cwd, "model": self._model, "approvalPolicy": "never",
                        "sandbox": "danger-full-access", "ephemeral": not self._keep_session,
                    })
                elif method == "thread/start":
                    thread = result.get("thread")
                    self._thread_id = thread.get("id") if isinstance(thread, dict) else None
                    if not self._thread_id:
                        yield self._failed("thread/start returned no thread id")
                        return
                    for event in self._stream.translate({"type": "thread.started", "thread_id": self._thread_id}):
                        yield event
                    await self._handshake_request("turn/start", {
                        "threadId": self._thread_id, "input": [{"type": "text", "text": self._prompt}],
                        **({"effort": self._effort} if self._effort else {}),
                    })
                elif method == "turn/start":
                    turn = result.get("turn")
                    self._turn_id = turn.get("id") if isinstance(turn, dict) else None
                    if not self._turn_id:
                        yield self._failed("turn/start returned no turn id")
                        return
                    self._handshake = None
                    self._turn_ready.set()
            if not self._turn_ready.is_set():
                yield self._failed("connection closed before the turn started")
        except (BrokenPipeError, ConnectionResetError) as exc:
            if not self._turn_ready.is_set():
                yield self._failed(str(exc))
        finally:
            self._ended = True
            self._turn_id = None
            self._turn_ready.set()
            for request_id, future in self._pending.items():
                if not future.done():
                    future.set_exception(ConnectionResetError("Codex app-server closed its output"))
                    if self._handshake is not None and self._handshake[1] == request_id:
                        future.exception()  # The handshake is driven here, not awaited by a caller.
            self._pending.clear()

    def _check_open(self) -> None:
        assert self._process.stdin is not None
        if self._ended or self._process.stdin.is_closing():
            raise RuntimeError("the Codex run has ended")

    @property
    def send_timeout(self) -> float:
        return _REQUEST_TIMEOUT_SEC

    async def wait_input_ready(self) -> None:
        self._check_open()
        await self._turn_ready.wait()
        self._check_open()
        if self._turn_id is None:
            raise RuntimeError("no active turn")

    async def send(self, text: str, *, uuid: str | None = None) -> None:
        try:
            await asyncio.wait_for(self._steer(text), self.send_timeout)
        except TimeoutError:
            raise RuntimeError("Codex app-server did not accept the steer in time") from None

    async def _steer(self, text: str) -> None:
        """Readiness, lock, write and acknowledgement under the caller's one deadline."""
        await self.wait_input_ready()
        async with self._lock:
            self._check_open()
            if self._turn_id is None:
                raise RuntimeError("no active turn")
            request_id, future = await self._request("turn/steer", {
                "threadId": self._thread_id, "expectedTurnId": self._turn_id,
                "input": [{"type": "text", "text": text}],
            })
        try:
            response = await future
        finally:
            self._pending.pop(request_id, None)
        if "error" in response:
            error = response["error"]
            raise RuntimeError(str((error.get("message") if isinstance(error, dict) else None)
                                   or "Codex app-server rejected the steer"))

    def end_input(self) -> None:
        assert self._process.stdin is not None
        if not self._process.stdin.is_closing():
            self._process.stdin.close()
