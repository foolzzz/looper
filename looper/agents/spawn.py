"""Runs another run starts (the advisor RFC, section 7): ``run_child`` runs one, ``spawn`` records it as a spawn too.

A child run is one fresh process that cannot change anything: an advisor's (``SpawnKind.ADVISOR``) has no tool at
all (``Access.ADVISOR``), its system prompt replaced, in an empty private directory under the system's temporary
directory (``looper-advisor/<run_id>``, a path with no user name in it, since the runtime may send its working
directory with its requests), removed afterwards. It is built on what the room consult and the processor already
do:

- **Bounds.** ``single_run.run_once`` with one deadline over writing the prompt, reading the reply and waiting for
  exit, the process tree ended however it ends; the per-run cap (``--max-budget-usd`` for a runtime whose own figure
  is the run's cost), then the run's recorded cost (else its runtime's own figure) checked against it. A cap the run
  reached, a cost over it or a cost still unknown gives no output; so does a cost at provider prices whose cache
  writes do not say how long they last (``run_usage.cache_split_known``), since it is not exact. Since that cap is soft (the request that crosses
  it is already paid, and its answer dropped), a run with less than ``MIN_RUN_USD`` left does not start.
- **Refusals.** A run that cannot start says why before any process starts (``ChildFailure.UNAVAILABLE``): its
  settings or Agent Profile cannot run, its runtime has no mode without tools or cannot replace its system prompt,
  its model at provider prices has no full price, or it would go through the local gateway (which records requests).
  A model that refused to answer (``stop_reason`` ``refusal``), or a result another model answered, is
  ``ChildFailure.REFUSED``.
- **Its record** (``llm_records``): agent id ``<kind>-<12 hex>``, the parent's task and step, ``run_kind`` the
  kind's and ``parent_record_id`` the run that started it, so its cost is the task's.

``spawn`` is the one-shot entry point (M4's tools call it): it opens an ``agent_spawns`` row in ``asking``, runs one
child and finishes the row. A consult of the advisor, which first checks its request and may run more than one
round, moves its row itself (``db.agent_spawns``) and calls ``run_child`` for each round.
"""

from __future__ import annotations

import json
import logging
import shutil
import tempfile
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any

from ..db import agent_spawns
from ..db.agent_spawns import SpawnState
from .agent_settings import Access, AgentSettings, AgentType, Launch
from .system_prompt import SystemPrompt

logger = logging.getLogger(__name__)

# The least a child run may have left to start: its cap is soft, so a run started on less pays for a whole request
# and keeps nothing (measured with Claude Code 2.1.328: a 0.01 USD cap, a 0.056 USD request, its answer dropped).
MIN_RUN_USD = 0.10
OUTPUT_MAX_CHARS = 20_000


class SpawnKind(str, Enum):
    ADVISOR = "advisor"


# Each kind's access level, its runs' kind in llm_records and the directory under the system's temporary directory
# its runs work in.
_KINDS: Mapping[SpawnKind, tuple[Access, str, str]] = {
    SpawnKind.ADVISOR: (Access.ADVISOR, "advisor", "looper-advisor"),
}


class ChildFailure(str, Enum):
    """Why a child run gave no output."""

    UNAVAILABLE = "unavailable"  # it could not start: its settings, runtime, price or route (``detail``)
    RUNTIME = "runtime"  # its process did not start, or ended without a result or in error
    TIMEOUT = "timeout"
    BUDGET = "budget"  # it reached its cap, cost more, its cost is unknown, or too little was left to start it
    REFUSED = "refused"  # its model refused, or another model answered


@dataclass(frozen=True)
class SpawnParent:
    """Where a child run belongs: its task, the step it is about and the run (``llm_records.id``) that started it."""

    task_id: str
    step_id: str | None = None
    record_id: int | None = None


@dataclass(frozen=True)
class ChildRun:
    """One child run: its kind, its system prompt (which replaces the runtime's), its one user turn and its bounds;
    ``role`` is the role the run is for, when it has one (the dashboard lists such a run as running)."""

    kind: SpawnKind
    system_prompt: SystemPrompt
    prompt: str
    timeout_s: float
    max_turns: int = 2
    role: AgentType | None = None
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    # Its kind of run in llm_records, when not its spawn kind's (a judge's run for an advisor's consult).
    run_kind: str | None = None
    # Whether its record keeps none of its text: its input only as a reference (its digest and length), its stream
    # only as typed events (``processor.typed_events``), no output and no process output in its errors. A judge's
    # run is private: what it reads may be a request it sends back.
    private: bool = False

    @property
    def record_kind(self) -> str:
        return self.run_kind or _KINDS[self.kind][1]

    @property
    def agent_id(self) -> str:
        return f"{self.record_kind}-{self.run_id}"


@dataclass(frozen=True)
class ChildResult:
    """What a child run gave: its output (its final message), or why none (``failure``, ``detail``); its record,
    cost and the models that answered."""

    output: str | None
    record_id: int | None = None
    cost_usd: float | None = None
    failure: ChildFailure | None = None
    detail: str = ""
    models: tuple[str, ...] = ()

    def round(self) -> dict[str, Any]:
        """The run as one round of a spawn's ``result.rounds``: typed fields only, never its output."""
        return {"record_id": self.record_id, "cost_usd": self.cost_usd, "models": list(self.models),
                "failure": self.failure.value if self.failure is not None else None}


@dataclass(frozen=True)
class SpawnOutcome:
    """A spawn's row once finished, and its child run."""

    row: dict[str, Any] | None
    child: ChildResult


def _directory(kind: SpawnKind, run_id: str) -> Path:
    return Path(tempfile.gettempdir()) / _KINDS[kind][2] / run_id


def _problem(settings: AgentSettings, launch: Launch) -> str | None:
    """Why ``launch`` cannot run as a child: its settings, its runtime's modes, its model's price or its route."""
    from .llmrec.gateway import translated
    from .profile_registry import cost_basis
    from .runtimes import ADAPTERS, Capability

    if settings.error:
        return settings.error
    if settings.profile_error:
        return settings.profile_error
    adapter = ADAPTERS.get(settings.profile.runtime.type)
    if adapter is None or Capability.NO_TOOLS not in adapter.capabilities:
        return (f"Agent Profile {settings.profile.name!r} cannot run a spawned run: its runtime "
                f"{settings.profile.runtime.type!r} has no mode without tools")
    if not adapter.can_replace_system_prompt:
        return (f"Agent Profile {settings.profile.name!r} cannot run a spawned run: its runtime "
                f"{settings.profile.runtime.type!r} cannot replace its system prompt")
    if translated(launch):
        return (f"Agent Profile {settings.profile.name!r} runs through the local gateway, which records its "
                "requests; a spawned run runs without request recording")
    basis = cost_basis(launch.profile)
    if basis["source"] == "provider_price" and launch.model in basis["unpriced"]:
        return f"model {launch.model!r} has no full price with its provider, so no spend cap can hold"
    return None


def _refusal(result: Mapping[str, Any], model: str) -> str | None:
    """Why ``result`` is not the answer of ``model``: the model refused, or another model answered."""
    from .runtimes.pricing import CLAUDE_CODE_ALIASES, base_model

    if result.get("stop_reason") == "refusal":
        return "its model refused to answer"
    usage = result.get("modelUsage")
    if isinstance(usage, Mapping) and usage:
        expected = base_model(CLAUDE_CODE_ALIASES.get(base_model(model), model))  # an alias, as the model it runs
        others = sorted(name for name in usage if base_model(str(name)) != expected)
        if others:
            return f"another model answered ({', '.join(others)})"
    return None


async def run_child(child: ChildRun, *, parent: SpawnParent, settings: AgentSettings, cap_usd: float,
                    runtime_root: Path, claude_bin: str = "claude") -> ChildResult:
    """Run ``child`` once with ``settings``, spending at most ``cap_usd`` (lowered by the layers' per-run cap), as the
    module's notes say; any reason it gives no output is in the result."""
    from . import single_run
    from ..db import llm_records
    from .claude_cli import ClaudeRunRecorder
    from .processor import run_cost, typed_events
    from .run_usage import cache_split_known
    from .runtimes import CostSource, RuntimeUnsupported
    from .system_prompt import sha256_text

    access, run_kind = _KINDS[child.kind][0], child.record_kind
    cap = min(cap_usd, settings.max_budget_usd or cap_usd)
    if cap < MIN_RUN_USD:
        return ChildResult(None, failure=ChildFailure.BUDGET,
                           detail=f"{cap:.2f} USD is left, less than the {MIN_RUN_USD:.2f} USD a run needs to start")
    launch = Launch(replace(settings, max_budget_usd=cap), role=child.role, access=access)
    if problem := _problem(settings, launch):
        return ChildResult(None, failure=ChildFailure.UNAVAILABLE, detail=problem)
    directory = _directory(child.kind, child.run_id)
    directory.mkdir(mode=0o700, parents=True)
    try:
        prompt_file = child.system_prompt.write_file(runtime_root)
        try:
            command = launch.runtime.run_command(launch.executable(claude_bin), cwd=directory, prompt_file=prompt_file,
                                                 launch=launch, max_turns=child.max_turns)
        except RuntimeUnsupported as exc:
            return ChildResult(None, failure=ChildFailure.UNAVAILABLE, detail=str(exc))
        input_text = child.prompt
        if child.private:
            input_text = json.dumps({"sha256": sha256_text(child.prompt), "chars": len(child.prompt)},
                                    separators=(",", ":"), sort_keys=True)
        recorder = ClaudeRunRecorder(agent_id=child.agent_id, task_id=parent.task_id, command=command,
                                     cwd=str(directory), input_text=input_text, step_id=parent.step_id,
                                     system_prompt=child.system_prompt, launch=launch, whole_process=True,
                                     run_kind=run_kind, parent_record_id=parent.record_id,
                                     keep=typed_events if child.private else None, keep_output=not child.private)
        await recorder.start()
        try:
            finished = await single_run.run_once(
                recorder, command, cwd=str(directory), prompt=child.prompt, launch=launch, timeout=child.timeout_s,
                what=f"the {child.kind.value} run", label=f"{child.kind.value} {child.agent_id}", quiet=child.private)
        except single_run.StartFailed as exc:
            return ChildResult(None, recorder.record_id, failure=ChildFailure.RUNTIME,
                               detail=f"its runtime does not start: {exc}")
    finally:
        shutil.rmtree(directory, ignore_errors=True)
    result, record_id = finished.result, recorder.record_id
    recorded: float | None = None
    try:
        row = await llm_records.get_record(record_id) if record_id is not None else None
        recorded = float(row["cost_usd"]) if row and row.get("cost_usd") is not None else None
    except Exception:
        logger.warning("cannot read the cost of %s run %s", child.kind.value, record_id, exc_info=True)
    cost = run_cost(recorded, result, launch.cost_source)
    models = tuple(sorted(str(name) for name in ((result or {}).get("modelUsage") or {})))
    failed: tuple[ChildFailure, str] | None = None
    if finished.timed_out:
        failed = ChildFailure.TIMEOUT, f"no output within {child.timeout_s:g} s"
    elif result is None:
        failed = ChildFailure.RUNTIME, "its run ended without a result"
    elif (refused := _refusal(result, launch.model)) is not None:
        failed = ChildFailure.REFUSED, refused
    elif launch.runtime.budget_error(result) is not None:
        failed = ChildFailure.BUDGET, f"it reached its spend cap of {cap:g} USD"
    elif (why := single_run.unsuccessful(result)) is not None:
        failed = ChildFailure.RUNTIME, why
    elif launch.cost_source is CostSource.PROVIDER_PRICE and not cache_split_known(result):
        failed = ChildFailure.BUDGET, ("its cache writes do not say how long they last, so its cost at its "
                                       f"provider's prices is not exact and its cap of {cap:g} USD cannot be checked")
    elif cost is None:
        failed = ChildFailure.BUDGET, f"its cost is unknown, so its cap of {cap:g} USD cannot be checked"
    elif cost > cap:
        failed = ChildFailure.BUDGET, f"it cost {cost:.4f} USD, over its cap of {cap:g} USD"
    if failed is not None:
        return ChildResult(None, record_id, cost, failure=failed[0], detail=failed[1], models=models)
    return ChildResult(str(result.get("result") or ""), record_id, cost, models=models)


async def spawn(kind: SpawnKind, parent: SpawnParent, brief: Mapping[str, Any], inputs: Mapping[str, Any],
                profile: AgentSettings, budget: float, *, child: ChildRun, runtime_root: Path,
                claude_bin: str = "claude") -> SpawnOutcome:
    """Open a spawn of ``kind`` for ``parent`` (``brief``: why and what for; ``inputs``: what it is given), run
    ``child`` once with ``profile`` within ``budget`` and finish the spawn: ``answered`` with its output, else
    ``over_budget`` or ``failed`` with why."""
    if child.kind is not kind:
        raise ValueError(f"a {kind.value} spawn cannot run a {child.kind.value} run")
    row = await agent_spawns.create(kind=kind.value, task_id=parent.task_id, step_id=parent.step_id,
                                    parent_record_id=parent.record_id, brief=brief, inputs=inputs,
                                    budget_usd=budget, state=SpawnState.ASKING)
    result = ChildResult(None, failure=ChildFailure.RUNTIME, detail="its run was stopped")
    try:
        result = await run_child(child, parent=parent, settings=profile, cap_usd=budget, runtime_root=runtime_root,
                                 claude_bin=claude_bin)
    finally:
        state = (SpawnState.ANSWERED if result.failure is None else
                 SpawnState.OVER_BUDGET if result.failure is ChildFailure.BUDGET else SpawnState.FAILED)
        outcome: dict[str, Any] = {"failure": result.failure.value if result.failure is not None else None}
        if result.output is not None:
            outcome["output"] = result.output[:OUTPUT_MAX_CHARS]
        finished = await agent_spawns.move(
            row["id"], state, expect=SpawnState.ASKING, child_record_id=result.record_id,
            spent_usd=result.cost_usd or 0.0, result=outcome, append={"rounds": result.round()},
            detail=result.detail or None)
    return SpawnOutcome(finished, result)
