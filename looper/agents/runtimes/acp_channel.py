"""One ACP v1 Reviewer session over stdio, without vendor extensions.

Wire fields follow https://agentclientprotocol.com/protocol/v1/schema.
UsageUpdate.used/size measure context, not billable input/output tokens. The
published v1 schema has no billable token totals: cost must remain unknown.
"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import AsyncIterator, Mapping
from enum import Enum, auto
from pathlib import Path
from typing import Any

from ..claude_cli import iter_lines
from .channel import DeliveryResult

PROTOCOL_VERSION = 1
INIT_TIMEOUT_SEC = 30.0
SESSION_TIMEOUT_SEC = 60.0

# ACP ToolKind, not option labels or vendor tool names. Execute is deliberately
# absent: a terminal command cannot be proved read-only from its display text.
READ_ONLY_PERMISSION_KINDS = frozenset({"read", "search"})


class _Phase(Enum):
    NEW = auto()
    INITIALIZING = auto()
    CREATING = auto()
    RUNNING = auto()
    FINISHED = auto()


class AcpChannel:
    """The single stdout owner handles setup, notifications and agent requests."""

    send_timeout = 0.0

    def __init__(self, process: asyncio.subprocess.Process, *, cwd: str, prompt_file: Path | None,
                 model: str | None) -> None:
        self._process, self._cwd = process, cwd
        self._prompt_file, self._model = prompt_file, model
        self._phase = _Phase.NEW
        self._session: str | None = None
        self._deadline: float | None = None
        self._expected_id = 0
        self._prompt: list[dict[str, str]] = []
        self._startup_error: str | None = None
        self._text: list[str] = []
        self._tools: set[str] = set()
        self._denials: list[dict[str, str]] = []
        self._runtime_usd: float | None = None
        self._cancel_sent = False

    async def _write(self, message: dict[str, Any]) -> None:
        stdin = self._process.stdin
        if stdin is None or stdin.is_closing():
            raise BrokenPipeError("ACP stdin is closed")
        stdin.write((json.dumps({"jsonrpc": "2.0", **message}, ensure_ascii=False) + "\n").encode("utf-8"))
        await stdin.drain()

    async def _request(self, method: str, params: dict[str, Any]) -> None:
        self._expected_id += 1
        await self._write({"id": self._expected_id, "method": method, "params": params})

    async def start(self, prompt: str) -> None:
        if self._phase is not _Phase.NEW:
            raise RuntimeError("ACP Reviewer accepts exactly one prompt")
        self._phase = _Phase.INITIALIZING
        try:
            if self._prompt_file is None:
                raise OSError("no prompt_file was supplied")
            # read_text() normalizes newlines; the contract must reach ACP verbatim.
            contract = self._prompt_file.read_bytes().decode("utf-8")
            self._prompt = [{"type": "text", "text": contract}, {"type": "text", "text": prompt}]
        except (OSError, UnicodeError) as exc:
            self._startup_error = f"ACP prompt_file is unreadable: {exc}"
            return
        self._deadline = asyncio.get_running_loop().time() + INIT_TIMEOUT_SEC
        try:
            await self._request("initialize", {"protocolVersion": PROTOCOL_VERSION,
                "clientCapabilities": {}, "clientInfo": {"name": "looper", "version": "1"}})
        except (OSError, RuntimeError) as exc:
            self._startup_error = f"ACP initialize could not be sent: {exc}"

    def _result(self, *, error: str | None = None) -> dict[str, Any]:
        event: dict[str, Any] = {"type": "result", "subtype": "error" if error else "success",
            "is_error": error is not None, "result": "".join(self._text), "session_id": self._session,
            "usage_complete": False}
        # v1 currently documents context size and dollars, but no billable token
        # totals. Do not manufacture zero tokens or price vendor _meta fields.
        if self._runtime_usd is not None:
            event["runtime_reported_usd"] = self._runtime_usd
        if self._denials:
            event["permission_denials"] = self._denials
        if error:
            event["errors"] = [error]
        return event

    def _cancel(self) -> None:
        """Queue cancellation before EOF; process termination belongs to ProcessRun."""
        stdin = self._process.stdin
        if self._session and not self._cancel_sent and stdin is not None and not stdin.is_closing():
            self._cancel_sent = True
            try:
                stdin.write((json.dumps({"jsonrpc": "2.0", "method": "session/cancel",
                    "params": {"sessionId": self._session}}) + "\n").encode("utf-8"))
            except (OSError, RuntimeError):
                pass

    async def _permission(self, message: Mapping[str, Any]) -> None:
        params = message.get("params")
        params = params if isinstance(params, Mapping) else {}
        tool = params.get("toolCall")
        tool = tool if isinstance(tool, Mapping) else {}
        options = params.get("options")
        options = options if isinstance(options, list) else []
        tool_kind = tool.get("kind")
        allowed = (params.get("sessionId") == self._session and isinstance(tool_kind, str)
                   and tool_kind in READ_ONLY_PERMISSION_KINDS)
        wanted = "allow_once" if allowed else "reject_once"
        chosen = next((option["optionId"] for option in options if isinstance(option, Mapping)
                       and option.get("kind") == wanted and isinstance(option.get("optionId"), str)), None)
        outcome = ({"outcome": "selected", "optionId": chosen} if chosen is not None else {"outcome": "cancelled"})
        if not allowed or chosen is None:
            self._denials.append({"tool": str(tool.get("kind") or "unknown"),
                                  "input": json.dumps(tool.get("rawInput"), ensure_ascii=False)[:500]})
        await self._write({"id": message["id"], "result": {"outcome": outcome}})

    def _assistant(self, content: list[dict[str, Any]]) -> dict[str, Any]:
        return {"type": "assistant", "session_id": self._session,
                "message": {"role": "assistant", "model": self._model, "content": content}}

    def _updates(self, update: Mapping[str, Any]) -> list[dict[str, Any]]:
        kind = update.get("sessionUpdate")
        if not isinstance(kind, str):
            return []
        if kind in {"agent_message_chunk", "agent_thought_chunk"}:
            content = update.get("content")
            text = content.get("text") if isinstance(content, Mapping) and content.get("type") == "text" else None
            if isinstance(text, str) and text:
                field = "text" if kind == "agent_message_chunk" else "thinking"
                if field == "text":
                    self._text.append(text)
                return [self._assistant([{"type": field, field: text}])]
        elif kind in {"tool_call", "tool_call_update"}:
            ident = update.get("toolCallId")
            if not isinstance(ident, str) or not ident:
                return []
            events = []
            if ident not in self._tools:
                self._tools.add(ident)
                raw = update.get("rawInput")
                events.append(self._assistant([{"type": "tool_use", "id": ident,
                    "name": str(update.get("title") or update.get("kind") or "ACP tool"),
                    "input": raw if isinstance(raw, dict) else {"value": raw}}]))
            if update.get("status") in {"completed", "failed"}:
                parts = []
                for block in update.get("content") or []:
                    content = block.get("content") if isinstance(block, Mapping) else None
                    if isinstance(content, Mapping) and content.get("type") == "text" and isinstance(content.get("text"), str):
                        parts.append(content["text"])
                if not parts and "rawOutput" in update:
                    parts.append(json.dumps(update["rawOutput"], ensure_ascii=False))
                events.append({"type": "user", "session_id": self._session, "message": {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": ident, "content": "\n".join(parts),
                     "is_error": update.get("status") == "failed"}]}})
            return events
        elif kind == "usage_update":
            cost = update.get("cost")
            amount = cost.get("amount") if isinstance(cost, Mapping) else None
            if (isinstance(cost, Mapping) and cost.get("currency") == "USD"
                    and isinstance(amount, (int, float)) and not isinstance(amount, bool)
                    and math.isfinite(amount) and amount >= 0):
                self._runtime_usd = float(amount)
        return []

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        assert self._process.stdout is not None
        lines = iter_lines(self._process.stdout)
        succeeded = False
        try:
            if self._startup_error:
                raise RuntimeError(self._startup_error)
            if self._phase is _Phase.NEW:
                raise RuntimeError("ACP Reviewer has not started")
            while self._phase is not _Phase.FINISHED:
                try:
                    async with asyncio.timeout_at(self._deadline):
                        line = await anext(lines)
                except StopAsyncIteration:
                    raise RuntimeError("ACP process exited before its session/prompt result") from None
                try:
                    message = json.loads(line)
                except (ValueError, UnicodeError):
                    raise RuntimeError("ACP stdout is not valid JSON-RPC") from None
                if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                    raise RuntimeError("ACP stdout is not a JSON-RPC 2.0 object")
                method, ident = message.get("method"), message.get("id")
                if method is not None:
                    if not isinstance(method, str):
                        raise RuntimeError("ACP method is not a string")
                    if ident is not None:
                        if method == "session/request_permission":
                            await self._permission(message)
                        else:
                            writing = method == "fs/write_text_file" or str(method).startswith("terminal/")
                            self._denials.append({"tool": str(method), "input": "unsupported client request"})
                            await self._write({"id": ident, "error": {"code": -32000 if writing else -32601,
                                "message": "ACP Reviewer client request denied"}})
                    elif method == "session/update":
                        params = message.get("params")
                        if isinstance(params, Mapping) and params.get("sessionId") == self._session:
                            update = params.get("update")
                            if isinstance(update, Mapping):
                                for event in self._updates(update):
                                    yield event
                    continue
                if type(ident) is not int or ident != self._expected_id:
                    continue
                response = message.get("result")
                if "error" in message or not isinstance(response, Mapping):
                    raise RuntimeError("ACP request failed or returned an invalid response")
                if self._phase is _Phase.INITIALIZING:
                    if type(response.get("protocolVersion")) is not int or response["protocolVersion"] != PROTOCOL_VERSION:
                        raise RuntimeError("unsupported ACP protocolVersion: expected 1")
                    self._phase = _Phase.CREATING
                    self._deadline = asyncio.get_running_loop().time() + SESSION_TIMEOUT_SEC
                    await self._request("session/new", {"cwd": self._cwd, "mcpServers": []})
                elif self._phase is _Phase.CREATING:
                    session = response.get("sessionId")
                    if not isinstance(session, str) or not session:
                        raise RuntimeError("ACP session/new returned no session id")
                    self._session = session
                    self._phase, self._deadline = _Phase.RUNNING, None
                    yield {"type": "system", "subtype": "init", "session_id": session,
                           "runtime": "acp", "model": self._model, "cwd": self._cwd}
                    await self._request("session/prompt", {"sessionId": session, "prompt": self._prompt})
                else:
                    if response.get("stopReason") != "end_turn":
                        raise RuntimeError("ACP session/prompt did not end with stopReason end_turn (v1 required)")
                    if not "".join(self._text).strip():
                        raise RuntimeError("ACP end_turn contained no final text")
                    succeeded = True
                    self._phase = _Phase.FINISHED
                    yield self._result()
        except TimeoutError:
            method = "initialize" if self._phase is _Phase.INITIALIZING else "session/new"
            yield self._result(error=f"ACP {method} timed out")
        except (OSError, RuntimeError, ValueError) as exc:
            yield self._result(error=str(exc))
        finally:
            if not succeeded:
                self._cancel()
            self.end_input()
            await lines.aclose()

    async def wait_input_ready(self) -> None:
        raise RuntimeError("ACP Reviewer steering is not supported")

    async def send(self, text: str, *, uuid: str | None = None) -> DeliveryResult:
        raise RuntimeError("ACP Reviewer steering is not supported")

    def end_input(self) -> None:
        if self._phase is not _Phase.FINISHED:
            self._cancel()
        self._phase = _Phase.FINISHED
        stdin = self._process.stdin
        if stdin is not None and not stdin.is_closing():
            stdin.close()
