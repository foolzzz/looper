"""Resident Manager/Planner sessions owned by a shared runner supervisor."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, fields
import logging
from pathlib import Path
import re
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from ... import claude_cli
from ...run_usage import Price, Usage
from .. import ApiProtocol, StopMethod
from ..api.adapter import KEY_ENV
from ..api.loop import TurnTools
from ..cancel import TurnCancel, TurnCancelled
from .bridge import ConnectionLost, RpcFailure, _value
from .protocol import ErrorCode, ProtocolError
from .supervisor import RunnerSupervisor, _joined
if TYPE_CHECKING:
    from .turn import RunnerTurn, TurnLimits

logger = logging.getLogger(__name__)
SETTLE_TIMEOUT = 5.0


@dataclass(frozen=True, repr=False)
class Descriptor:
    """Record-safe description, never an executable command or credential store."""

    kind: str
    role: Literal["manager", "planner"]
    provider_id: str
    protocol: ApiProtocol
    base_url: str
    model: str
    effort: str | None
    context_window: int | None
    system_prompt_file: str
    max_requests: int = 30
    session: str | None = None
    add_dirs: tuple[str, ...] = ()
    read_only: bool = False
    system_prompt_mode: Literal["replace", "append"] = "replace"
    reasoning: bool = False
    builtin: tuple[str, ...] = ()

    def to_argv(self) -> list[str]:
        argv = ["looper-runner"]
        for f in fields(self):
            value = getattr(self, f.name)
            if f.name in {"session", "add_dirs", "builtin"}:
                continue
            argv.extend(["--" + f.name.replace("_", "-"),
                         value.value if isinstance(value, ApiProtocol) else
                         "" if value is None else str(value)])
        for name in self.builtin:
            argv.extend(["--builtin", name])
        for directory in self.add_dirs:
            argv.extend(["--add-dir", directory])
        return argv + ["--session", self.session] if self.session else argv

    @classmethod
    def parse(cls, argv: list[str]) -> Descriptor:
        try:
            names = [f.name for f in fields(cls) if f.name not in {"session", "add_dirs", "builtin"}]
            size = 1 + 2 * len(names)
            if (not all(isinstance(v, str) for v in argv) or len(argv) < size or argv[0] != "looper-runner"
                    or argv[1:size:2] != ["--" + n.replace("_", "-") for n in names]):
                raise ValueError
            values = dict(zip(names, argv[2:size:2]))
            values["protocol"] = ApiProtocol(values["protocol"])
            values["effort"] = values["effort"] or None
            values["context_window"] = int(values["context_window"]) if values["context_window"] else None
            values["max_requests"] = int(values["max_requests"])
            if (values["role"] not in {"manager", "planner"} or values["max_requests"] <= 0
                    or values["read_only"] not in {"True", "False"}
                    or values["reasoning"] not in {"True", "False"}
                    or values["system_prompt_mode"] not in {"replace", "append"}
                    or any(not values[n] for n in ("kind", "provider_id", "base_url", "model", "system_prompt_file"))):
                raise ValueError
            values["read_only"] = values["read_only"] == "True"
            values["reasoning"] = values["reasoning"] == "True"
            builtin = []
            directories = []
            tail = argv[size:]
            while len(tail) >= 2 and tail[0] == "--builtin":
                if not re.fullmatch(r"[a-z][a-z_]*", tail[1]):
                    raise ValueError
                builtin.append(tail[1])
                tail = tail[2:]
            while len(tail) >= 2 and tail[0] == "--add-dir":
                directories.append(tail[1])
                tail = tail[2:]
            if tail and (len(tail) != 2 or tail[0] != "--session" or not tail[1]):
                raise ValueError
            return cls(**values, builtin=tuple(builtin), add_dirs=tuple(directories), session=tail[1] if tail else None)
        except (ValueError, TypeError, IndexError):
            raise ValueError("not a runner session description") from None


class RunnerSession:
    shares_bridge = False
    pid = None  # A shared process must never enter a tier's process cleanup.

    def __init__(self, label: str, supervisor: RunnerSupervisor, kind: str):
        self.label, self.supervisor, self.kind = label, supervisor, kind
        self._descriptor: Descriptor | None = None
        self._lease = None
        self._signature = None
        self._totals = Usage(cost_usd=0)
        self._models: dict[str, Usage] = {}
        self._tools: TurnTools | None = None
        self._budget: dict | None = None
        self._turn: RunnerTurn | None = None
        self._cancel: TurnCancel | None = None
        self._cancelling: asyncio.Task | None = None
        self._done: asyncio.Event | None = None
        self._closing = False
        self._resume_lost = False

    @property
    def started(self) -> bool:
        return (self._descriptor is not None and not self._closing and
                (self._resume_lost or self._lease is not None and self._lease.valid
                 and not self._lease.bridge._error))

    @property
    def can_interrupt(self) -> bool:
        return bool(self._lease and self._lease.valid and not self._lease.bridge._error
                    and self._lease.bridge.capabilities.get("cancel"))

    @property
    def runner_id(self) -> str | None:
        return self._lease.runner_id if self._lease else None

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
                    autonomous: Callable | None = None, price: Price | None = None) -> None:
        if self._descriptor is not None:
            await self.stop()
        descriptor = Descriptor.parse(command)
        if descriptor.kind != self.kind:
            raise ValueError("runner kind does not match session description")
        key = (env or {}).get(KEY_ENV)
        if not key:
            raise OSError("the provider's API key did not reach the runner session")
        prompt = Path(descriptor.system_prompt_file).read_text(encoding="utf-8")
        self._closing = False
        self._signature, self._totals = signature, totals if totals is not None else Usage(cost_usd=0)
        self._models = {}
        self._resume_lost = False
        lease = await self.supervisor.acquire(self.kind, owner=self.label)
        params = {"role": descriptor.role, "cwd": cwd,
            "add_dirs": list(descriptor.add_dirs), "env": {k: v for k, v in (env or {}).items() if k != KEY_ENV},
            "provider": {"account_id": descriptor.provider_id, "reasoning": descriptor.reasoning,
                "protocol": descriptor.protocol.value, "base_url": descriptor.base_url,
                "model": descriptor.model, "effort": descriptor.effort,
                "context_window": descriptor.context_window, "api_key": key},
            "system_prompt": {"mode": descriptor.system_prompt_mode, "text": prompt},
            "tools": {"builtin": list(descriptor.builtin), "platform": [], "read_only": descriptor.read_only},
            "limits": {"max_requests_per_turn": descriptor.max_requests,
                "tool_timeout_s": 120, "tool_output_bytes": 102400}}
        try:
            if descriptor.session is None:
                await lease.open_session(params)
            else:
                await lease.load_session({**params, "session_id": descriptor.session})
        except RpcFailure as error:
            await lease.release()
            if descriptor.session is None or error.code not in {ErrorCode.SESSION_NOT_FOUND, ErrorCode.SESSION_OWNED}:
                raise
            self._resume_lost = True
        except BaseException:
            await lease.release()
            raise
        else:
            self._lease = lease
        self._descriptor = descriptor

    def set_turn_tools(self, tools: TurnTools) -> None:
        self._tools = tools

    def set_turn_budget(self, *, provider_id: str, model: str, price: Price | None,
                        limits: Callable[[], Awaitable[TurnLimits]], max_requests: int = 30) -> None:
        self._budget = dict(provider_id=provider_id, model=model, price=price, limits=limits,
                            max_requests=min(max_requests, self._descriptor.max_requests)
                            if self._descriptor else max_requests)

    async def _cancel_turn(self) -> None:
        lease, turn = self._lease, self._turn
        if lease is None or turn is None:
            return
        try:
            async with asyncio.timeout(SETTLE_TIMEOUT):
                try:
                    await lease.bridge.cancel_turn(turn.session_id, turn.trace_id)
                except (RpcFailure, ProtocolError) as error:
                    if error.code != ErrorCode.NOT_RUNNING:
                        raise
                await lease.bridge.wait_settled(turn.session_id, turn.trace_id)
        except (TimeoutError, ConnectionLost, RpcFailure, ProtocolError):
            if not lease.bridge._error:  # A lost generation is already owned by supervisor recovery.
                await self.supervisor.invalidate(self.kind, ConnectionLost("runner cancellation did not settle"))

    def _request_cancel(self) -> None:
        if self._turn is not None:
            self._turn.stop()  # Close host admission before scheduling any I/O.
            entry = self._lease.bridge.sessions.get(self._turn.session_id)
            if entry is not None:
                entry.stopped = True
            if self._cancelling is None:
                self._cancelling = asyncio.create_task(self._cancel_turn())

    async def run_turn(self, text: str, on_event: Callable, *, tag: Any = None,
                       stop: Callable[[], claude_cli.CapStop | None] | None = None,
                       cancel: TurnCancel | None = None) -> claude_cli.ResidentTurn:
        if self._done is not None or self._closing:
            raise ProtocolError(ErrorCode.SESSION_BUSY)
        self._done, self._cancel = asyncio.Event(), cancel or TurnCancel.never()
        trace = str(uuid4())
        def unregister():
            pass
        entry = None
        outcome = "failed"
        try:
            descriptor, budget, tools = self._descriptor, self._budget, self._tools
            if descriptor is None:
                raise RuntimeError("runner session has not started")
            if self._resume_lost:
                return self._failure("runner saved session is unavailable; could not be resumed", trace)
            if budget is None or tools is None:
                return self._failure("runner turn requires host tools and budget context before prompting", trace)
            if budget["provider_id"] != descriptor.provider_id or budget["model"] != descriptor.model:
                return self._failure("runner turn budget does not match the bound provider/model", trace)
            if not self.started:
                return self._failure("runner generation was lost", trace)
            try:
                specs = await self._cancel.race(tools.specs())
            except TurnCancelled:
                outcome = "cancelled"
                return claude_cli.ResidentTurn({**claude_cli.user_stop_result(), "trace_id": trace}, session_started=False,
                    usage=Usage(cost_usd=0), stopped=StopMethod.CANCEL, prompt_sent=False)
            lease = self._lease
            bridge, sid = lease.bridge, lease.session_id
            async def capped(_):
                self._request_cancel()
            from .turn import RunnerTurn

            self._turn = RunnerTurn(session_id=sid, trace_id=trace, role=descriptor.role, tools=tools,
                prior_totals=self._totals, prior_model_usage=self._models, cancel=capped, **budget)
            async def emit(event):
                # Internal recorder events need the ordinary JSON modelUsage shape.
                if event.get("type") == "result":
                    event["modelUsage"] = {model: {"inputTokens": u.input_tokens, "outputTokens": u.output_tokens,
                        "cacheReadInputTokens": u.cache_read_tokens, "cacheCreationInputTokens": u.cache_creation_tokens,
                        "costUSD": u.cost_usd} for model, u in event["modelUsage"].items()}
                    if event["runner"]["outcome"] == "capped":
                        event[claude_cli.SOFT_CAP] = True
                    bridge.sessions[sid].result_event = event
                await _value(on_event(event))
                if stop is not None and stop() is not None:
                    self._request_cancel()
            logger.info("runner turn start runner_id=%s session_id=%s trace_id=%s", lease.runner_id, sid, trace)
            # begin_turn installs admission synchronously before writing. Cancellation
            # subscribes after it starts, and is checked before the prompt as well.
            if self._cancel.requested:
                outcome = "cancelled"
                return claude_cli.ResidentTurn({**claude_cli.user_stop_result(), "trace_id": trace}, session_started=False,
                    usage=Usage(cost_usd=0), stopped=StopMethod.CANCEL, prompt_sent=False)
            beginning = asyncio.create_task(bridge.begin_turn(sid, trace, emit, self._turn, text=text,
                tools=[{"name": s.name, "description": s.description, "inputSchema": dict(s.schema)} for s in specs]))
            unregister = self._cancel.on_request(self._request_cancel)
            try:
                await beginning
            except (RpcFailure, ProtocolError):
                entry = bridge.sessions[sid]
                failed = self._failure("runner rejected the prompt", trace)
                return claude_cli.ResidentTurn(failed.result_event, session_started=entry.session_started,
                    usage_complete=False, prompt_sent=entry.prompt_sent)
            except (ConnectionLost, TimeoutError):
                entry = bridge.sessions.get(sid)
                if entry is None or entry.trace_id != trace or entry.settled is None:
                    outcome = "lost"
                    return self._failure("runner generation was lost before prompting", trace)
                # An installed trace owns a single lost settlement, never replay.
            entry = bridge.sessions[sid]
            if entry.prompt_sent:
                self._cancel.sent()
            summary = await bridge.wait_settled(sid, trace)
            self._totals, self._models = summary.totals, summary.model_usage
            outcome = summary.outcome
            result = entry.result_event
            return claude_cli.ResidentTurn(result, session_started=entry.session_started, usage=summary.usage,
                usage_complete=summary.usage_complete, prompt_sent=entry.prompt_sent,
                stopped=StopMethod.CANCEL if self._cancel.requested else None)
        except asyncio.CancelledError:
            self._request_cancel()
            if self._cancelling is not None:
                await _joined(self._cancelling)
            await self._close_lease()
            raise
        finally:
            unregister()
            try:
                if self._turn is not None:
                    self._turn.stop()
                    await self._turn.drain_tools()
                if self._cancelling is not None:
                    await _joined(self._cancelling)
            finally:
                logger.info("runner turn end runner_id=%s session_id=%s trace_id=%s outcome=%s",
                    self.runner_id, self._lease.session_id if self._lease else None, trace, outcome)
                self._tools = self._budget = self._turn = self._cancel = self._cancelling = None
                done, self._done = self._done, None
                done.set()

    @staticmethod
    def _failure(message: str, trace: str) -> claude_cli.ResidentTurn:
        return claude_cli.ResidentTurn({"type": "result", "subtype": "error_during_execution",
            "is_error": True, "errors": [message], "trace_id": trace, "num_turns": 0},
            session_started=False, usage_complete=False, prompt_sent=False)

    async def steer(self, text: str, *, message_id: str, if_idle: str = "reject") -> dict:
        if if_idle != "reject":
            raise ProtocolError(ErrorCode.INVALID_PARAMS)
        if self._turn is None or self._closing:
            raise ProtocolError(ErrorCode.NOT_RUNNING)
        turn = self._turn
        return await self._lease.bridge.steer(turn.session_id, turn.trace_id, message_id, text)

    async def interrupt(self) -> StopMethod | None:
        if self._cancel is None:
            return None
        self._cancel.request()
        self._request_cancel()
        await self._done.wait()
        return StopMethod.CANCEL

    async def _close_lease(self) -> None:
        lease, self._lease = self._lease, None
        if lease is not None:
            closing = asyncio.create_task(lease.release())
            try:
                done, _ = await asyncio.wait({closing}, timeout=SETTLE_TIMEOUT)
                if not done and not lease.bridge._error:
                    await self.supervisor.invalidate(self.kind, ConnectionLost("runner close did not settle"))
            finally:
                await _joined(closing)
                if not lease.bridge._error and lease.session_id in lease.bridge.sessions:
                    await self.supervisor.invalidate(self.kind, ConnectionLost("runner session close was refused"))

    async def stop(self, *, kill: bool = False) -> None:
        self._closing = True
        if self._cancel is not None:
            await self.interrupt()
        try:
            await self._close_lease()
        finally:
            self._descriptor = None
            self._tools = self._budget = None

    async def try_stop_if_idle(self) -> bool:
        if self._done is not None or self._closing:
            return False
        self._closing = True
        await self.stop()
        return True
