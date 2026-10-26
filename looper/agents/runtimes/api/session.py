"""Resident model API sessions with process-local, neutral conversation history."""
from __future__ import annotations

import json
import logging
import os
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from ... import claude_cli, log_redaction
from ...ops import OpError
from ...run_usage import Price, Usage, run_usage
from .. import ApiProtocol, pricing
from ..cancel import TurnCancel
from .adapter import Descriptor, KEY_ENV
from .loop import (
    AgentLoop, ErrorKind, LoopEvent, LoopResult, ModelTarget, Outcome, RequestDone, RequestUsage, SessionItem,
    TextDelta, ToolCall, ToolDone, ToolOutcome, ToolSpec, TurnTools, loop_for,
)

FORBIDDEN_ENV = (
    "OPENAI_ORG_ID", "OPENAI_PROJECT_ID", "OPENAI_ADMIN_KEY", "OPENAI_CUSTOM_HEADERS",
    "ANTHROPIC_CUSTOM_HEADERS", "OPENAI_LOG", "ANTHROPIC_LOG",
)


class MemoryTranscript:
    """Neutral items and protocol metadata, retained only by this session owner."""

    def __init__(self, protocol: ApiProtocol):
        self.protocol = protocol
        self._items: list[SessionItem] = []

    async def items(self) -> list[SessionItem]:
        return list(self._items)

    async def append(self, items: list[SessionItem]) -> None:
        self._items.extend(items)


class _TurnTools:
    """One listing snapshot for both init and every request in this turn."""

    def __init__(self, tools: TurnTools, specs: list[ToolSpec]):
        self._tools, self._specs = tools, specs

    async def specs(self) -> list[ToolSpec]:
        return self._specs

    async def call(self, call: ToolCall) -> ToolOutcome:
        return await self._tools.call(call)


def _arguments(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw or '{}')
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _tokens(usage: RequestUsage) -> dict[str, int | None]:
    return {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens,
            "cache_read_input_tokens": usage.cache_read_tokens,
            "cache_creation_input_tokens": usage.cache_write_tokens}


def _error_message(result: LoopResult, model: str) -> str:
    status = result.error_status if result.error_status is not None else "no response"
    detail = result.error_message or "unknown error"
    messages = {
        ErrorKind.AUTH: f"the provider refused the API key (HTTP {status}): update it in Settings → LLM Providers",
        ErrorKind.NOT_FOUND: f"the provider has no model {model}",
        ErrorKind.RATE_LIMITED: f"the model API is not available now (HTTP {status}, after retries); try again later",
        ErrorKind.UNAVAILABLE: f"the model API is not available now (HTTP {status}, after retries); try again later",
        ErrorKind.CONTEXT_OVERFLOW: "the conversation is longer than the model's context window; send /new to start a new session",
        ErrorKind.TRUNCATED: "the reply was longer than the output limit and did not finish",
        ErrorKind.INVALID_REQUEST: f"the provider refused the request: {detail}",
        ErrorKind.PROTOCOL: f"the provider's stream could not be read: {detail}",
    }
    return log_redaction.redact(messages[result.error_kind])[:300]


class ApiSession:
    shares_bridge = False
    can_interrupt = False
    pid = None

    def __init__(self, label: str):
        self.label = label
        self._descriptor: Descriptor | None = None
        self._loop: AgentLoop | None = None
        self._totals = Usage()
        self._tools: TurnTools | None = None
        self._session: str | None = None
        self._transcripts: OrderedDict[str, MemoryTranscript] = OrderedDict()

    @property
    def started(self) -> bool:
        return self._descriptor is not None

    @property
    def command(self) -> list[str]:
        return self._descriptor.to_argv() if self._descriptor else []

    @property
    def totals(self) -> Usage:
        return self._totals

    def stale(self, signature: Any) -> bool:
        return self.started and signature != self._signature

    async def start(self, command: list[str], *, cwd: str, signature: Any,
                    env: dict[str, str] | None = None, totals: Usage | None = None,
                    autonomous: Callable[[Any], Awaitable[Any]] | None = None,
                    price: Price | None = None) -> None:
        try:
            descriptor = Descriptor.parse(command)
        except ValueError as exc:
            raise OSError(str(exc)) from exc
        key = (env or {}).get(KEY_ENV)
        if not key:
            raise OSError("the provider's API key did not reach the api runtime")
        for name in FORBIDDEN_ENV:
            if name in os.environ:
                raise OSError(f"{name} is set in looper serve's environment; the api runtime does not start with it, "
                              "because the SDK would add it to every request or log: unset it and restart the service")
        prompt = Path(descriptor.system_prompt_file).read_text(encoding="utf-8")
        log_redaction.add_secret(key)
        for name in ("openai", "httpx2"):
            logging.getLogger(name).setLevel(logging.WARNING)
        self._loop = loop_for(descriptor.protocol)
        self._target = ModelTarget(descriptor.protocol, descriptor.base_url, descriptor.model,
                                   descriptor.effort, descriptor.context_window, key)
        self._prompt = prompt
        self._signature, self._totals, self._price = signature, totals or Usage(), price
        self._session = descriptor.session
        self._descriptor = descriptor

    def set_turn_tools(self, tools: Any) -> None:
        self._tools = tools

    async def run_turn(self, text: str, on_event: Callable[[dict[str, Any]], Awaitable[None]], *,
                       tag: Any = None, stop: Callable[[], claude_cli.CapStop | None] | None = None,
                       cancel: TurnCancel | None = None) -> claude_cli.ResidentTurn:
        try:
            return await self._run_turn(text, on_event, stop=stop, cancel=cancel or TurnCancel.never())
        finally:
            self._tools = None

    async def _run_turn(self, text, on_event, *, stop, cancel):
        def failed(message, *, session_started=True):
            return claude_cli.ResidentTurn(
                {"type": "result", "subtype": "error_during_execution", "is_error": True,
                 "errors": [message], "num_turns": 0},
                session_started=session_started, usage=Usage(), prompt_sent=False)

        if self._tools is None:
            return failed("the Manager gave the api session no tools")
        sid = self._session or f"api-{uuid4().hex}"
        descriptor = self._descriptor
        transcript = self._transcripts.get(sid)
        if self._session and (transcript is None or transcript.protocol != descriptor.protocol):
            return failed(f"session {sid} could not be resumed", session_started=False)
        try:
            specs = await self._tools.specs()
        except OpError as exc:
            return failed(f"Looper's platform tools are unavailable: {exc}")
        tools = _TurnTools(self._tools, specs)
        if transcript is None:
            transcript = MemoryTranscript(descriptor.protocol)
            self._transcripts[sid] = transcript
        self._transcripts.move_to_end(sid)
        while len(self._transcripts) > 4:
            self._transcripts.popitem(last=False)
        model = descriptor.model

        async def send(event: dict[str, Any]) -> None:
            if not cancel.requested:
                await on_event(event)

        async def emit(event: LoopEvent) -> None:
            if isinstance(event, TextDelta):
                await send({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
                           "delta": {"type": "text_delta", "text": event.text}}})
            elif isinstance(event, RequestDone):
                content = []
                if event.reasoning_summary:
                    content.append({"type": "thinking", "thinking": event.reasoning_summary})
                if event.text:
                    content.append({"type": "text", "text": event.text})
                content.extend({"type": "tool_use", "id": call.call_id, "name": call.name,
                                "input": _arguments(call.arguments)} for call in event.tool_calls)
                await send({"type": "assistant", "message": {"model": model, "role": "assistant",
                            "content": content, "usage": _tokens(event.record.usage)}})
                u = event.record.usage
                await send(pricing.usage_event(model, Usage(input_tokens=u.input_tokens, output_tokens=u.output_tokens,
                           cache_read_tokens=u.cache_read_tokens, cache_creation_tokens=u.cache_write_tokens),
                           **({} if u.complete else {"usage_complete": False})))
            elif isinstance(event, ToolDone):
                await send({"type": "user", "message": {"role": "user", "content": [{"type": "tool_result",
                           "tool_use_id": event.call.call_id, "content": event.outcome.text,
                           "is_error": event.outcome.is_error}]}})

        await send({"type": "system", "subtype": "init", "session_id": sid, "model": model,
                    "tools": [s.name for s in specs]})
        result = await self._loop.run(target=self._target, system_prompt=self._prompt, user_text=text,
                    session_id=sid, transcript=transcript, tools=tools, cancel=cancel, emit=emit,
                    over_cap=lambda: stop is not None and stop() is not None, max_requests=30)
        complete = all(r.usage.complete for r in result.requests)
        tokens = {key: sum(_tokens(r.usage)[key] or 0 for r in result.requests)
                  for key in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")}
        event = {"type": "result"}
        cap = None
        if result.outcome is Outcome.ANSWERED:
            event.update(subtype="success", is_error=False, result=result.final_text)
        elif result.outcome is Outcome.MAX_REQUESTS:
            event.update(subtype="error_max_turns", is_error=True, errors=[
                "the turn made 30 model requests, the most a turn may make; it stopped there, and what its tool calls did was not undone"])
        elif result.outcome is Outcome.FAILED:
            event.update(subtype="error_during_execution", is_error=True, errors=[_error_message(result, model)])
        elif result.outcome is Outcome.CAPPED:
            cap = stop()
            event.update(cap.result_event)
        elif result.outcome is Outcome.CANCELLED:
            event.update(claude_cli.user_stop_result())
        event.update(session_id=sid, num_turns=len(result.requests), usage=tokens,
                     modelUsage={model: {"inputTokens": tokens["input_tokens"],
                                         "outputTokens": tokens["output_tokens"],
                                         "cacheReadInputTokens": tokens["cache_read_input_tokens"],
                                         "cacheCreationInputTokens": tokens["cache_creation_input_tokens"]}},
                     usage_complete=complete,
                     api={"protocol": descriptor.protocol.value, "requests": len(result.requests),
                          "retries": sum(r.retries for r in result.requests),
                          "upstream_ids": [r.upstream_id for r in result.requests if r.upstream_id][:30]})
        usage = cap.usage if cap else run_usage(event, price=self._price)
        self._totals = self._totals.plus(usage)
        self._session = sid
        return claude_cli.ResidentTurn(event, session_started=True, usage=usage, prompt_sent=True,
                                       usage_complete=complete)

    async def stop(self, *, kill: bool = False) -> None:
        try:
            if self._loop is not None:
                await self._loop.aclose()
        finally:
            self._loop = None
            self._descriptor = None

    async def interrupt(self) -> None:
        return None
