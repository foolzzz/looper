"""One Worker, Reviewer or consult run on a shared runner session."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
import logging
from typing import Any
from uuid import uuid4

from ...run_usage import Price
from ..api.loop import ToolCall, ToolOutcome, ToolSpec, TurnTools
from ..channel import DeliveryResult, RunStartError
from .bridge import ConnectionLost, RpcFailure
from .protocol import ErrorCode, ProtocolError
from .session import Descriptor, RunnerSession
from .supervisor import RunnerUnavailable
from .turn import TurnLimits

logger = logging.getLogger(__name__)


class NoTools:
    async def specs(self) -> list[ToolSpec]:
        return []

    async def call(self, call: ToolCall) -> ToolOutcome:
        return ToolOutcome(f"Looper has no platform tool {call.name} for this run", is_error=True, ran=False)


class RunnerChannel:
    send_timeout = 3.0
    pid = None

    def __init__(self, session: RunnerSession, descriptor: Descriptor, *, cwd: str,
                 env: Mapping[str, str], tools: TurnTools | None, provider_id: str, model: str,
                 price: Price | None, limits: Callable[[], Awaitable[TurnLimits]]):
        self.session, self.descriptor = session, descriptor
        self._cwd, self._env = cwd, dict(env)
        self._tools = tools if tools is not None else NoTools()
        self._budget = dict(provider_id=provider_id, model=model, price=price, limits=limits,
                            max_requests=descriptor.max_requests)
        # Database writes by the consumer must not block the shared bridge's update worker.
        self._events: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._ready = asyncio.Event()
        self._launched = False
        self._input_closed = self._result_seen = False
        self._turn: asyncio.Task | None = None
        self._ending: asyncio.Task | None = None
        self._steers: set[asyncio.Task] = set()
        self._sent: dict[str, str] = {}
        self._dropped: list[str] = []

    @property
    def command(self) -> list[str]:
        return self.descriptor.to_argv()

    @property
    def channel(self) -> RunnerChannel:
        return self

    async def launch(self) -> None:
        try:
            await self.session.start(self.command, cwd=self._cwd, signature=None, env=dict(self._env))
        except (RpcFailure, ProtocolError, ConnectionLost, OSError, ValueError) as error:
            code = getattr(error, "code", None)
            if code == ErrorCode.CAPACITY:
                reason = (f"the {self.session.kind} runner has no free session (CAPACITY); "
                          "a session frees when a Manager, Planner or run ends")
            elif isinstance(error, RunnerUnavailable):
                message = str(error)
                # Supervisor messages are record-safe; also redact the bound key defensively.
                from ..api.adapter import KEY_ENV
                key = self._env.get(KEY_ENV)
                if key:
                    message = message.replace(key, "[redacted]")
                reason = f"the {self.session.kind} runner is unavailable: {message}"[:500]
            elif code == ErrorCode.UNSUPPORTED:
                reason = f"the {self.session.kind} runner cannot fork a saved session"
            else:
                name = ErrorCode(code).name if code in ErrorCode._value2member_map_ else type(error).__name__
                reason = f"the {self.session.kind} runner could not open the run's session ({name})"
            raise RunStartError(reason) from None
        self._launched = True

    async def start(self, prompt: str) -> None:
        if not self._launched:
            raise RuntimeError("the run has not launched")
        if self._turn is not None or self._input_closed:
            raise RuntimeError("the run has already started or ended")
        self.session.set_turn_tools(self._tools)
        self.session.set_turn_budget(**self._budget)
        self._turn = asyncio.create_task(self._run(prompt))

    async def _run(self, prompt: str) -> None:
        try:
            turn = await self.session.run_turn(prompt, self._on_event)
            if not self._result_seen:
                self._on_event(turn.result_event)
        except Exception as error:
            self._on_event({"type": "result", "subtype": "error_during_execution", "is_error": True,
                            "errors": [f"the {self.session.kind} runner run failed ({type(error).__name__})"], "num_turns": 0})
        finally:
            self._events.put_nowait(None)
            self._ready.set()

    def _on_event(self, event: dict[str, Any]) -> None:
        if event.get("type") == "system":
            subtype = event.get("subtype")
            if subtype == "init":
                self._ready.set()
            elif subtype in {"steer_applied", "steer_dropped"}:
                text = self._sent.pop(event.get("runner", {}).get("message_id"), None)
                if subtype == "steer_dropped" and text is not None:
                    self._dropped.append(text)
        elif event.get("type") == "result":
            self._result_seen = True
            self.end_input()
        self._events.put_nowait(event)

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        while (event := await self._events.get()) is not None:
            yield event

    async def wait_input_ready(self) -> None:
        if self._turn is None:
            raise RuntimeError("the run has not started")
        await self._ready.wait()
        if self._result_seen or self._input_closed:
            raise RuntimeError("the run has ended")

    async def send(self, text: str, *, uuid: str | None = None) -> DeliveryResult:
        if self._input_closed or self._turn is None or self._turn.done() or not self.session.started:
            return DeliveryResult.REJECTED
        message_id = uuid or "steer-" + uuid4().hex
        self._sent[message_id] = text
        task = asyncio.create_task(self.session.steer(text, message_id=message_id))
        self._steers.add(task)
        def finished(task):
            self._steers.discard(task)
            if task.cancelled() or task.exception() is not None:
                self._sent.pop(message_id, None)
        task.add_done_callback(finished)
        # Cancelling a pending bridge request poisons the shared connection. Retain
        # ownership after a timeout (or caller cancellation) and consume its reply.
        done, _ = await asyncio.wait({task}, timeout=self.send_timeout)
        if not done:
            return DeliveryResult.UNKNOWN
        try:
            task.result()
        except Exception as error:
            code = getattr(error, "code", None)
            if code in {ErrorCode.NOT_RUNNING, ErrorCode.TRACE_MISMATCH}:
                self._sent.pop(message_id, None)
                return DeliveryResult.REJECTED
            if code == ErrorCode.UNSUPPORTED:
                raise RuntimeError(f"the {self.session.kind} runner cannot steer this run") from None
            return DeliveryResult.UNKNOWN
        return DeliveryResult.ACCEPTED

    def end_input(self) -> None:
        self._input_closed = True

    async def _end(self) -> None:
        self.end_input()
        try:
            await self.session.stop()
        except Exception:
            logger.warning("could not stop pi runner run", exc_info=True)
        finally:
            tasks = [*self._steers, *([self._turn] if self._turn is not None else [])]
            await asyncio.gather(*tasks, return_exceptions=True)

    async def end(self) -> None:
        if self._ending is None:
            self.end_input()
            self._ending = asyncio.create_task(self._end())
        await asyncio.shield(self._ending)

    async def wait(self) -> int:
        if self._turn is not None:
            await self._turn
        await self.session.stop()
        return 0

    async def error_output(self) -> str:
        return ""

    def dropped_input(self) -> list[str]:
        return list(self._dropped)
