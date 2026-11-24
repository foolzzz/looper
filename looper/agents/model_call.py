"""One tool-less model call with a typed reply, using the role's subscription-login runtime.

The two record recipes preserve the existing child and processor execution contracts. They do not select a
different runtime or change its launch: the role's Agent Profile still does that. ``Backend`` is the seam for a
future implementation; there is no backend setting or direct-API implementation.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Generic, Protocol, TypeVar

from .agent_settings import AgentSettings, AgentType
from .spawn import ChildFailure, ChildRun, SpawnParent

if TYPE_CHECKING:
    from .processor import ProcessorRun
    from .failures import Failure

T = TypeVar("T")


class CallFailure(str, Enum):
    TIMEOUT = "timeout"
    PARSE = "parse"
    RUNTIME = "runtime"
    RATE_LIMITED = "rate_limited"
    UNAVAILABLE = "unavailable"
    BUDGET = "budget"
    REFUSED = "refused"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class Execution:
    """The run before parsing. A parse failure must not rewrite its record or its advisor round."""

    output: str | None
    record_id: int | None = None
    cost_usd: float | None = None
    failure: CallFailure | None = None
    reason: str = ""
    models: tuple[str, ...] = ()
    profile: Mapping[str, Any] | None = None
    engine_failure: Failure | None = None

    def round(self) -> dict[str, Any]:
        return {"record_id": self.record_id, "cost_usd": self.cost_usd, "models": list(self.models),
                "failure": self.failure.value if self.failure is not None else None}


@dataclass(frozen=True)
class ChildCall:
    """A child run's existing launch, bounds and record ownership; optional lifecycle callbacks precede parsing."""

    child: ChildRun
    parent: SpawnParent
    settings: AgentSettings
    cap_usd: float
    runtime_root: Path
    claude_bin: str = "claude"
    on_record: Callable[[int], Awaitable[None]] | None = None
    on_result: Callable[[Execution], Awaitable[None]] | None = None


@dataclass(frozen=True)
class ProcessorCall:
    """A private processor-style run, including its input reference, settings scope and event filtering."""

    owner: str
    instructions: str
    input_text: str
    input_ref: Mapping[str, Any]
    quiet_word: str
    budget_usd: float
    timeout_s: float
    runtime_root: Path
    claude_bin: str = "claude"
    metadata: Mapping[str, Any] | None = None
    contract: str | None = None
    role: AgentType = AgentType.LEADS_CHECK
    project_id: str | None = None
    schedule_id: str | None = None


Request = ChildCall | ProcessorCall


class Backend(Protocol):
    """Execution seam retained for a future direct-API backend alongside the subscription CLI backend."""

    async def run(self, request: Request) -> Execution: ...


_CHILD_FAILURES = {failure: (CallFailure.RUNTIME if failure is ChildFailure.START_FAILURE else CallFailure(failure.value))
                   for failure in ChildFailure}


class SubscriptionBackend:
    """The existing one-shot CLI paths, with their original record and failure policies."""

    async def run(self, request: Request) -> Execution:
        if isinstance(request, ChildCall):
            from .spawn import run_child

            done = await run_child(request.child, parent=request.parent, settings=request.settings,
                                   cap_usd=request.cap_usd, runtime_root=request.runtime_root,
                                   claude_bin=request.claude_bin, on_record=request.on_record)
            return Execution(done.output, done.record_id, done.cost_usd,
                             _CHILD_FAILURES[done.failure] if done.failure is not None else None,
                             done.detail, done.models)
        from .processor import ProcessorFailure, run

        scope = {name: value for name in ("project_id", "schedule_id")
                 if (value := getattr(request, name)) is not None}
        done = await run(owner=request.owner, instructions=request.instructions, input_text=request.input_text,
                         input_ref=request.input_ref, quiet_word=request.quiet_word, budget_usd=request.budget_usd,
                         timeout_s=request.timeout_s, runtime_root=request.runtime_root, claude_bin=request.claude_bin,
                         metadata=request.metadata, contract=request.contract, **scope,
                         **({"role": request.role} if request.role is not AgentType.PROCESSOR else {}))
        failures = {ProcessorFailure.REFUSED: CallFailure.UNAVAILABLE, ProcessorFailure.TIMEOUT: CallFailure.TIMEOUT,
                    ProcessorFailure.BUDGET: CallFailure.BUDGET, ProcessorFailure.FAILED: CallFailure.RUNTIME,
                    ProcessorFailure.BLOCKED: CallFailure.BLOCKED}
        return Execution(done.reply, done.record_id, done.cost_usd,
                         failures[done.failure] if done.failure is not None else None, done.detail, profile=done.profile,
                         engine_failure=done.engine_failure)


@dataclass(frozen=True, kw_only=True)
class _CallMetadata:
    execution: Execution
    latency_ms: int = 0
    model: str | None = None

    @property
    def record_id(self) -> int | None:
        return self.execution.record_id

    @property
    def cost_usd(self) -> float | None:
        return self.execution.cost_usd


@dataclass(frozen=True)
class CallOk(_CallMetadata, Generic[T]):
    """A completed execution and its parsed reply."""

    value: T


@dataclass(frozen=True)
class CallFailed(_CallMetadata):
    """An execution or parse failure; a parser's original exception is retained in ``error``."""

    failure: CallFailure
    reason: str
    error: Exception | None = None


CallResult = CallOk[T] | CallFailed


def processor_result(result: CallResult[str]) -> ProcessorRun:
    """Keep the processor owners' result and retry contract around a call whose reply is the output itself."""
    from .processor import ProcessorFailure, ProcessorRun

    failures = {CallFailure.UNAVAILABLE: ProcessorFailure.REFUSED, CallFailure.REFUSED: ProcessorFailure.REFUSED,
                CallFailure.TIMEOUT: ProcessorFailure.TIMEOUT, CallFailure.BUDGET: ProcessorFailure.BUDGET,
                CallFailure.BLOCKED: ProcessorFailure.BLOCKED}
    done = result.execution
    return ProcessorRun(result.value if isinstance(result, CallOk) else None, done.record_id, done.cost_usd,
                        failures.get(result.failure, ProcessorFailure.FAILED) if isinstance(result, CallFailed) else None,
                        done.reason, done.profile, done.engine_failure)


async def call(request: Request, *, parse: Callable[[str], T], backend: Backend | None = None) -> CallResult[T]:
    """Run once within the recipe's deadline and spend cap, then parse its reply. Execution failures never parse;
    parse failures retain the execution's record and spend. Cancellation and recorder/callback errors propagate to
    the owner, as on the original paths. A parser may return a typed domain rejection (the leads' ``Checked``).
    """
    started = time.monotonic()
    done = await (backend if backend is not None else SubscriptionBackend()).run(request)
    latency_ms = int((time.monotonic() - started) * 1000)
    model = done.models[0] if done.models else (
        request.settings.profile.model if isinstance(request, ChildCall)
        else ((done.profile or {}).get("model") or {}).get("name"))
    if isinstance(request, ChildCall) and request.on_result is not None:
        await request.on_result(done)
    if done.failure is not None:
        return CallFailed(execution=done, failure=done.failure, reason=done.reason, latency_ms=latency_ms, model=model)
    try:
        value = parse(done.output or "")
    except Exception as exc:
        return CallFailed(execution=done, failure=CallFailure.PARSE, reason=str(exc), error=exc,
                          latency_ms=latency_ms, model=model)
    return CallOk(execution=done, value=value, latency_ms=latency_ms, model=model)
