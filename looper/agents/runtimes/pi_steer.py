"""One Pi RPC run: correlate setup and prompt responses while draining stdout."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from enum import Enum, auto
from typing import Any

from ..claude_cli import iter_lines
from .pi import PiStream

_SETUP_TIMEOUT_SEC = 10.0
_PROMPT_TIMEOUT_SEC = 10.0
_STEER_TIMEOUT_SEC = 4.0


class _Phase(Enum):
    NEW = auto()
    SETUP = auto()
    PROMPT = auto()
    RUNNING = auto()
    FINISHED = auto()


class PiChannel:
    """Worker startup never waits for a response: ``events`` owns the stdout reader."""

    def __init__(self, process: asyncio.subprocess.Process, *, model: str | None, cwd: str) -> None:
        self._process = process
        self._stream = PiStream(model=model)
        self._cwd = cwd
        self._lock = asyncio.Lock()
        self._ready = asyncio.Event()
        self._next_id = 0
        self._pending: dict[str, str] = {}
        self._steers: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._phase = _Phase.NEW
        self._prompt = ""
        self._deadline: float | None = None
        self._startup_error: str | None = None
        self._early_result: dict[str, Any] | None = None

    async def _write(self, command: str, **params: Any) -> tuple[str, asyncio.Future[dict[str, Any]] | None]:
        assert self._process.stdin is not None
        self._next_id += 1
        request_id = str(self._next_id)
        self._pending[request_id] = command
        future: asyncio.Future[dict[str, Any]] | None = None
        if command == "steer":
            future = self._steers[request_id] = asyncio.get_running_loop().create_future()
        try:
            self._process.stdin.write((json.dumps({"id": request_id, "type": command, **params}) + "\n").encode())
            await self._process.stdin.drain()
        except BaseException:
            self._pending.pop(request_id, None)
            if (future := self._steers.pop(request_id, None)) is not None:
                future.cancel()
            raise
        return request_id, future

    async def start(self, prompt: str) -> None:
        if self._phase is not _Phase.NEW:
            raise RuntimeError("the Pi RPC run has already started")
        self._prompt = prompt
        self._phase = _Phase.SETUP
        self._deadline = asyncio.get_running_loop().time() + _SETUP_TIMEOUT_SEC
        try:
            async with asyncio.timeout_at(self._deadline), self._lock:
                await self._write("get_state")
                await self._write("set_steering_mode", mode="one-at-a-time")
        except TimeoutError:
            self._startup_error = "setup timed out"
        except (BrokenPipeError, ConnectionResetError):
            self._startup_error = "the process ended during setup"

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        assert self._process.stdout is not None
        lines = iter_lines(self._process.stdout)
        try:
            if self._startup_error is not None:
                yield self._failure(self._startup_error)
                return
            while True:
                try:
                    # An absolute deadline also expires when diagnostics keep stdout busy.
                    if self._deadline is not None and asyncio.get_running_loop().time() >= self._deadline:
                        raise TimeoutError
                    async with asyncio.timeout_at(self._deadline):
                        line = await anext(lines)
                except TimeoutError:
                    yield self._failure(f"{self._phase.name.lower()} acknowledgement timed out")
                    return
                except StopAsyncIteration:
                    if self._phase is not _Phase.FINISHED:
                        yield self._failure("the process ended before " + (
                            "agent_settled" if self._phase is _Phase.RUNNING else
                            f"{self._phase.name.lower()} acknowledgement"))
                    return
                if self._phase is _Phase.FINISHED:
                    continue  # Keep draining until stdin closure makes the RPC process exit.
                try:
                    raw = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    raw = None
                if not isinstance(raw, dict) or not isinstance(raw.get("type"), str) or not raw["type"]:
                    yield self._failure("invalid JSON RPC record")
                    return
                kind = raw["type"]
                if kind == "extension_error":
                    yield self._failure("extension_error during the run")
                    return
                if kind == "response":
                    request_id = raw.get("id")
                    command = self._pending.pop(request_id, None) if isinstance(request_id, str) else None
                    if command is None:
                        if raw.get("command") == "parse" and raw.get("success") is False:
                            yield self._failure("Pi could not parse an RPC command")
                            return
                        continue
                    if command == "steer":
                        future = self._steers.get(request_id)
                        if future is not None and not future.done():
                            future.set_result(raw)
                        continue
                    if raw.get("command") != command or raw.get("success") is not True:
                        yield self._failure(f"{command} rejected or returned an invalid acknowledgement")
                        return
                    if command == "get_state":
                        data = raw.get("data")
                        session_id = data.get("sessionId") if isinstance(data, dict) else None
                        if not isinstance(session_id, str) or not session_id.strip():
                            yield self._failure("get_state returned no valid sessionId")
                            return
                        for event in self._stream.translate({"type": "session", "id": session_id, "cwd": self._cwd}):
                            yield event
                    if self._phase is _Phase.SETUP and not self._pending:
                        self._phase = _Phase.PROMPT
                        self._deadline = asyncio.get_running_loop().time() + _PROMPT_TIMEOUT_SEC
                        async with asyncio.timeout_at(self._deadline), self._lock:
                            await self._write("prompt", message=self._prompt)
                    elif command == "prompt":
                        data = raw.get("data")
                        disposition = data.get("disposition") if isinstance(data, dict) else None
                        if disposition != "started":
                            shown = disposition if disposition in ("handled", "queued") else "invalid"
                            yield self._failure(f"prompt disposition {shown}; expected started")
                            return
                        self._phase = _Phase.RUNNING
                        self._deadline = None
                        self._ready.set()
                        if self._early_result is not None:
                            async with self._lock:
                                self._finish()
                            yield self._early_result
                    continue
                for event in self._stream.translate(raw):
                    if event.get("type") == "result":
                        if self._phase is _Phase.PROMPT:
                            # Fast completion may precede its ACK. Acceptance still decides
                            # whether this is our run; rejection must never leak a success.
                            self._early_result = event
                            continue
                        if self._phase is not _Phase.RUNNING:
                            yield self._failure("agent_settled before the prompt was sent")
                            return
                        async with self._lock:
                            self._finish()
                    yield event
        except TimeoutError:
            yield self._failure("prompt write timed out")
        except (BrokenPipeError, ConnectionResetError):
            yield self._failure("the process ended while sending the prompt")
        finally:
            async with self._lock:
                self._finish()
            await lines.aclose()

    def _finish(self) -> None:
        self._phase = _Phase.FINISHED
        self._deadline = None
        self._ready.set()
        self._pending.clear()
        for future in self._steers.values():
            if not future.done():
                future.set_exception(ConnectionResetError("the Pi run has ended"))
                # A write may still be draining and get cancelled before awaiting its ACK.
                future.exception()
        self._steers.clear()

    def _failure(self, message: str) -> dict[str, Any]:
        self._finish()
        return self._stream.failure(f"Pi RPC: {message}")

    async def send(self, text: str, *, uuid: str | None = None) -> None:
        """Only a queued ACK means acceptance; neither timeout nor rejection is retried."""
        assert self._process.stdin is not None
        request_id: str | None = None
        try:
            # One deadline covers lock contention, pipe backpressure and the response.
            async with asyncio.timeout(_STEER_TIMEOUT_SEC):
                if self._phase is _Phase.NEW:
                    raise RuntimeError("Pi cannot steer before the run has started")
                # Worker registers inputs just after start(), before events() has read setup ACKs.
                await self._ready.wait()
                async with self._lock:
                    if self._phase is _Phase.FINISHED or self._process.stdin.is_closing():
                        raise RuntimeError("the Pi run has ended")
                    request_id, future = await self._write("steer", message=text)
                    assert future is not None
                response = await future
        except TimeoutError:
            raise RuntimeError("Pi did not accept the steer in time; acceptance is unknown") from None
        finally:
            if request_id is not None:
                self._pending.pop(request_id, None)
                if (pending := self._steers.pop(request_id, None)) is not None:
                    pending.cancel()
        if response.get("command") != "steer" or response.get("success") is not True:
            raise RuntimeError("Pi rejected the steer or returned an invalid acknowledgement")
        data = response.get("data")
        disposition = data.get("disposition") if isinstance(data, dict) else None
        if disposition != "queued":
            if disposition == "handled":
                raise RuntimeError("Pi handled the steer; it was not queued")
            raise RuntimeError("Pi returned an invalid steer disposition; expected queued")

    def end_input(self) -> None:
        assert self._process.stdin is not None
        self._finish()
        if not self._process.stdin.is_closing():
            self._process.stdin.close()
