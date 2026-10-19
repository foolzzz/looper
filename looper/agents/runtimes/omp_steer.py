"""One OMP run over its line-delimited rpc protocol, with acknowledged steering."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from ..claude_cli import iter_lines
from .omp import OmpStream

logger = logging.getLogger(__name__)
_STEER_TIMEOUT_SEC = 4.0
_DROPPED = frozenset({"ready", "prompt_result", "session_settled", "available_commands_update",
                      "extension_ui_request"})


class OmpChannel:
    """The Worker's first prompt and further messages share one rpc process until EOF."""

    def __init__(self, process: asyncio.subprocess.Process, *, model: str | None, cwd: str) -> None:
        self._process = process
        self._stream = OmpStream(model=model)
        self._cwd = cwd
        self._lock = asyncio.Lock()
        self._next_id = 0
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._startup: dict[str, str] = {}
        self._prompt_acknowledged = False
        self._ended = False
        self._result_seen = False

    async def _write(self, command: str, **params: Any) -> tuple[str, asyncio.Future[dict[str, Any]]]:
        assert self._process.stdin is not None
        self._next_id += 1
        request_id = str(self._next_id)
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            self._process.stdin.write((json.dumps({"id": request_id, "type": command, **params}) + "\n").encode())
            await self._process.stdin.drain()
        except BaseException:
            self._pending.pop(request_id, None)
            future.cancel()
            raise
        return request_id, future

    async def start(self, prompt: str) -> None:
        async with self._lock:
            for command, params in (("set_interrupt_mode", {"mode": "wait"}), ("get_state", {}),
                                    ("prompt", {"message": prompt})):
                request_id, _ = await self._write(command, **params)
                self._startup[request_id] = command

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        assert self._process.stdout is not None
        try:
            async for line in iter_lines(self._process.stdout):
                try:
                    raw = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    logger.debug("OMP rpc: skipping non-JSON line: %r", line)
                    continue
                if not isinstance(raw, dict):
                    logger.debug("OMP rpc: skipping non-object line: %r", line)
                    continue
                if raw.get("type") == "response":
                    request_id = raw.get("id")
                    future = self._pending.pop(request_id, None) if isinstance(request_id, str) else None
                    if future is None:
                        if raw.get("command") == "parse" and not raw.get("success"):
                            logger.debug("OMP rpc: parse failed: %s", raw.get("error"))
                        continue
                    if not future.done():
                        future.set_result(raw)
                    command = self._startup.pop(request_id, None)
                    if command is not None:
                        if not raw.get("success"):
                            yield self._failure(str(raw.get("error") or f"{command} failed"))
                            return
                        if command == "get_state":
                            data = raw.get("data") or {}
                            for event in self._stream.translate({"type": "session", "id": data.get("sessionId"),
                                                                 "cwd": self._cwd}):
                                yield event
                        elif command == "prompt":
                            self._prompt_acknowledged = True
                    continue
                if raw.get("type") in _DROPPED:
                    continue
                for event in self._stream.translate(raw):
                    if event.get("type") == "result":
                        self._result_seen = True
                    yield event
            if not self._prompt_acknowledged:
                yield self._failure("the process ended before acknowledging the prompt")
        finally:
            self._ended = True
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(ConnectionResetError("the OMP rpc connection ended"))
                    # Startup requests have no waiter; send still observes the exception when awaited.
                    future.exception()
            self._pending.clear()

    def _failure(self, message: str) -> dict[str, Any]:
        self._result_seen = True
        return {"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "",
                "errors": [f"OMP rpc: {message}"]}

    async def send(self, text: str, *, uuid: str | None = None) -> None:
        assert self._process.stdin is not None
        async with self._lock:
            if self._ended or self._result_seen or self._process.stdin.is_closing():
                raise RuntimeError("the OMP run has ended")
            request_id, future = await self._write("steer", message=text)
        try:
            response = await asyncio.wait_for(future, _STEER_TIMEOUT_SEC)
        except TimeoutError:
            raise RuntimeError("OMP did not answer the steer") from None
        finally:
            self._pending.pop(request_id, None)
        if not response.get("success"):
            raise RuntimeError(response.get("error") or "OMP rejected the steer")

    def end_input(self) -> None:
        assert self._process.stdin is not None
        if not self._process.stdin.is_closing():
            self._process.stdin.close()
