"""Host-owned tool admission and monetary accounting for one runner trace."""
from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any, Literal, TypedDict, get_args

from ...run_usage import Price, Usage
from .. import pricing
from ..api.loop import ToolCall, ToolOutcome, TurnTools
from ..api.tools import invalid_arguments
from .protocol import ErrorCode, ProtocolError, RequestKind


class TurnLimits(TypedDict):
    run_cap: float | None
    task_cap: float | None
    task_spent: float | None


CapReason = Literal["run_cap", "task_cap", "max_requests", "budget_unverifiable"]
# Loss is host-owned; the wire protocol deliberately excludes it.
Outcome = Literal["completed", "cancelled", "capped", "max_requests", "failed", "lost"]


@dataclass(frozen=True)
class TurnSummary:
    usage: Usage
    totals: Usage
    model_usage: dict[str, Usage]
    outcome: Outcome
    cap_reason: CapReason | None
    usage_complete: bool


@dataclass
class _Request:
    kind: str
    estimate: float | None
    raw: dict[str, Any] | None = None


def _count(value: Any) -> bool:
    return type(value) is int and value >= 0


def _money(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _add(before: Usage, current: Usage) -> Usage:
    # Usage.plus intentionally tolerates partial costs; runner totals must not.
    cost = (None if before.cost_usd is None or current.cost_usd is None else
            round(before.cost_usd + current.cost_usd, 10))
    return replace(before.plus(current), cost_usd=cost)


class RunnerTurn:
    def __init__(self, *, session_id: str, trace_id: str, role: Literal["manager", "planner"],
                 tools: TurnTools, provider_id: str, model: str, price: Price | None,
                 limits: Callable[[], Awaitable[TurnLimits]], max_requests: int = 30,
                 prior_totals: Usage | None = None, prior_model_usage: Mapping[str, Usage] | None = None,
                 cancel: Callable[[str], Awaitable[None]] | None = None) -> None:
        if role not in ("manager", "planner"):
            raise ValueError("a runner turn must be Manager or Planner")
        if type(max_requests) is not int or max_requests <= 0:
            raise ValueError("max_requests must be a positive integer")
        self.session_id, self.trace_id, self.role = session_id, trace_id, role
        self._tools, self._provider, self._model, self._price = tools, provider_id, model, price
        self._limits, self._cancel = limits, cancel
        self.max_requests = max_requests
        self._prior = prior_totals
        self._prior_models = dict(prior_model_usage or {})
        self._active = True
        self._lock = asyncio.Lock()
        self._requests: dict[str, _Request] = {}
        self._work: set[asyncio.Task] = set()
        self.usage = Usage(cost_usd=0, input_tokens=0, output_tokens=0,
                           cache_read_tokens=0, cache_creation_tokens=0)
        self.usage_complete = True
        self.cap_reason: CapReason | None = None
        self._summary: TurnSummary | None = None

    def _trace(self, params: Mapping[str, Any]) -> None:
        if params.get("session_id") != self.session_id:
            raise ProtocolError(ErrorCode.SESSION_NOT_FOUND)
        if params.get("trace_id") != self.trace_id:
            raise ProtocolError(ErrorCode.TRACE_MISMATCH)

    def _reply(self, **values: Any) -> dict[str, Any]:
        return {"session_id": self.session_id, "trace_id": self.trace_id, **values}

    async def _cap(self, reason: CapReason) -> None:
        first = self.cap_reason is None
        self.cap_reason = self.cap_reason or reason
        self.stop()
        if first and self._cancel is not None:
            await self._cancel(self.cap_reason)

    async def call_tool(self, params: Mapping[str, Any]) -> dict[str, Any]:
        self._trace(params)
        if not self._active:
            raise ProtocolError(ErrorCode.NOT_RUNNING)
        arguments = params.get("arguments")
        if not isinstance(arguments, dict):
            result = invalid_arguments()
        else:
            for key in ("call_id", "name"):
                if not isinstance(params.get(key), str) or not params[key]:
                    raise ProtocolError(ErrorCode.INVALID_PARAMS)
            call = ToolCall(params["call_id"], params["name"], json.dumps(arguments, ensure_ascii=False))
            timeout = 120
            wait = arguments.get("wait_seconds") if call.name == "looper_room_inbox" else None
            if isinstance(wait, (int, float)) and not isinstance(wait, bool) and wait > 0:
                timeout = max(timeout, float(wait) + 20)
            work = asyncio.create_task(self._tools.call(call))
            self._work.add(work)
            # A timeout/cancel cannot establish whether an admitted write ran.
            # Retain it until finish/shutdown drains it, without cancelling it.
            try:
                result = await asyncio.wait_for(asyncio.shield(work), timeout=timeout)
            except TimeoutError:
                result = ToolOutcome(f"Looper did not answer the call to {call.name} within {timeout:g} s. "
                                     "It may still have run, so check its effect before calling it again.",
                                     is_error=True, ran=True)
        return self._reply(text=result.text, is_error=result.is_error, ran=result.ran)

    async def _current_limits(self) -> TurnLimits:
        values = dict(await self._limits())
        if self.role == "manager":
            values.update(task_cap=None, task_spent=0)
        return values

    def _unverifiable(self, limits: TurnLimits, estimate: float | None = 0) -> bool:
        capped = limits["run_cap"] is not None or limits["task_cap"] is not None
        return capped and (not self.usage_complete or not _money(self.usage.cost_usd)
                           or not _money(estimate)
                           or any(r.raw is None and not _money(r.estimate) for r in self._requests.values())
                           or (limits["task_cap"] is not None and not _money(limits["task_spent"])))

    def _actual_cap_reason(self, limits: TurnLimits) -> CapReason | None:
        if self._unverifiable(limits):
            return "budget_unverifiable"
        if limits["run_cap"] is not None and self.usage.cost_usd >= limits["run_cap"]:
            return "run_cap"
        if limits["task_cap"] is not None and round(limits["task_spent"] + self.usage.cost_usd, 10) >= limits["task_cap"]:
            return "task_cap"
        return None

    async def authorize(self, params: Mapping[str, Any]) -> dict[str, Any]:
        self._trace(params)
        async with self._lock:
            if not self._active:
                return self._reply(allow=False, reason="stopped")
            request_id = params.get("request_id")
            if (not isinstance(request_id, str) or not request_id or request_id in self._requests
                    or params.get("provider") != self._provider or params.get("model") != self._model
                    or params.get("kind") not in get_args(RequestKind)
                    or not _count(params.get("estimated_input_tokens"))
                    or not _count(params.get("max_output_tokens"))):
                raise ProtocolError(ErrorCode.INVALID_PARAMS)
            limits = await self._current_limits()
            if not self._active:  # Stop may arrive during the settings read.
                return self._reply(allow=False, reason="stopped")
            estimate_usage = Usage(input_tokens=params["estimated_input_tokens"],
                                   output_tokens=min(params["max_output_tokens"], 8192),
                                   cache_read_tokens=0, cache_creation_tokens=0)
            estimate = self._price(self._model, estimate_usage) if self._price else None
            estimate = estimate if _money(estimate) else None
            reserved = sum(r.estimate or 0 for r in self._requests.values() if r.raw is None)
            amount = round((self.usage.cost_usd or 0) + reserved + (estimate or 0), 10)
            reason = None
            if len(self._requests) >= self.max_requests:
                reason = "max_requests"
            elif self._unverifiable(limits, estimate):
                reason = "budget_unverifiable"
            elif limits["run_cap"] is not None and amount > limits["run_cap"]:
                reason = "run_cap"
            elif limits["task_cap"] is not None and round(limits["task_spent"] + amount, 10) > limits["task_cap"]:
                reason = "task_cap"
            if reason is None:
                self._requests[request_id] = _Request(params["kind"], estimate)
                return self._reply(allow=True)
            # Close admission under the lock; the callback can reenter Bridge.
            self._active = False
        await self._cap(reason)
        return self._reply(allow=False, reason=reason)

    async def request_end(self, params: Mapping[str, Any]) -> dict[str, Any]:
        self._trace(params)
        async with self._lock:
            request_id = params.get("request_id")
            request = self._requests.get(request_id) if isinstance(request_id, str) else None
            if (self._summary is not None or request is None or request.raw is not None
                    or params.get("model") != self._model or params.get("request_kind") != request.kind
                    or type(params.get("complete")) is not bool
                    or any(key in params and not _count(params[key])
                           for key in ("input_uncached", "output", "cache_read", "cache_write"))
                    or (params.get("reasoning") is not None and not _count(params["reasoning"]))):
                raise ProtocolError(ErrorCode.INVALID_PARAMS)
            complete = params["complete"] and all(key in params for key in
                                                  ("input_uncached", "output", "cache_read", "cache_write"))
            usage = Usage(input_tokens=params.get("input_uncached"), output_tokens=params.get("output"),
                          cache_read_tokens=params.get("cache_read"), cache_creation_tokens=params.get("cache_write"))
            cost = self._price(self._model, usage) if complete and self._price else None
            usage = replace(usage, cost_usd=cost if _money(cost) else None)
            request.raw = dict(params)
            self.usage = _add(self.usage, usage)
            self.usage_complete = self.usage_complete and complete
            limits = await self._current_limits()
            reason = self._actual_cap_reason(limits)
            if reason:
                self._active = False
        if reason:
            await self._cap(reason)
        return pricing.usage_event(self._model, usage, trace_id=self.trace_id,
                                   request_id=params["request_id"], usage_complete=complete)

    def stop(self) -> None:
        """Close admission synchronously; finish drains admitted platform work."""
        self._active = False

    async def drain_tools(self) -> None:
        """Cancel/shutdown waits for admitted writes even after their RPC timed out."""
        if self._work:
            await asyncio.shield(asyncio.gather(*self._work, return_exceptions=True))

    async def finish(self, params: Mapping[str, Any]) -> TurnSummary:
        self._trace(params)
        self.stop()
        await self.drain_tools()
        async with self._lock:
            if self._summary is not None:
                raise ProtocolError(ErrorCode.NOT_RUNNING)
            ended = sum(r.raw is not None for r in self._requests.values())
            lost = params.get("outcome") == "lost"
            # Loss counts only callbacks the bridge completed. A request_end may
            # already have recorded usage before an in-flight settings read failed.
            if (not _count(params.get("requests"))
                    or (not lost and not ended <= params["requests"] <= len(self._requests))
                    or params.get("outcome") not in get_args(Outcome)
                    or type(params.get("usage_complete")) is not bool):
                raise ProtocolError(ErrorCode.INVALID_PARAMS)
            self.usage_complete = (not lost and self.usage_complete and ended == len(self._requests)
                                   and params.get("usage_complete") is True)
            if not self.usage_complete:
                self.usage = replace(self.usage, cost_usd=None)
            # There is no runner left to cancel; loss must remain the terminal cause,
            # even under a cap or when current settings can no longer be read.
            reason = None
            if not lost and self.cap_reason is None:
                try:
                    reason = self._actual_cap_reason(await self._current_limits())
                except Exception:
                    if params.get("outcome") != "failed":
                        raise
                    # A failed settings callback must still release reservations
                    # and settle this trace if the settings remain unavailable.
                    self.usage_complete = False
                    self.usage = replace(self.usage, cost_usd=None)
            notify = reason is not None
            if notify:
                self.cap_reason = reason
            totals = self.usage if self._prior is None else _add(self._prior, self.usage)
            models = dict(self._prior_models)
            if self._requests:
                models[self._model] = (_add(models[self._model], self.usage)
                                       if self._model in models else self.usage)
            outcome = params.get("outcome", "failed")
            if self.cap_reason and not lost:
                outcome = "max_requests" if self.cap_reason == "max_requests" else "capped"
            self._summary = TurnSummary(self.usage, totals, models, outcome, self.cap_reason, self.usage_complete)
            self._requests.clear()  # Settlement releases every outstanding reservation.
        # Failed request processing already stopped admission. Final accounting
        # must not reenter the cancellation callback that may have just failed.
        if notify and self._cancel is not None and params.get("outcome") != "failed":
            await self._cancel(self.cap_reason)
        return self._summary
