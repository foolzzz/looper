"""Task-session measurements and maintenance, inside the pool's existing turn lease."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from ...db import agent_sessions, connection, llm_records
from .. import scheduler_settings, session_budget as policy
from ..claude_cli import ClaudeRunRecorder
from ..run_usage import Usage, compaction
from ..runtimes import Capability

logger = logging.getLogger(__name__)
MAINTENANCE_SECONDS = 120


async def _measure(agent: Any, record_id: int | None, *, maintenance: bool) -> tuple[int | None, float | None]:
    row = None
    if record_id is not None:
        try:
            row = await connection.get_pool().fetchrow(
                "SELECT context_tokens, cost_usd FROM llm_records WHERE id=$1", record_id)
        except Exception:
            logger.warning("cannot read task Manager %s turn measurements", agent.task_id, exc_info=True)
    context, cost = (row["context_tokens"], row["cost_usd"]) if row is not None else (None, None)
    await agent_sessions.save_measurements(f"task-manager-{agent.task_id}", context_tokens=context,
                                           last_turn_cost_usd=cost)
    if record_id is not None:
        await llm_records.add_metadata(record_id, {"task_manager": {"mode": agent.mode, "maintenance": maintenance}})
    return context, cost


async def maintain(agent: Any, request: Any, reopened: Any, launch: Any, prompt: Any,
                   record_id: int | None) -> None:
    """Only this record supplies measurements; maintenance cannot overlap another task turn."""
    try:
        context, cost = await _measure(agent, record_id, maintenance=False)
        if request.cancel.requested:
            return
        maximum = await scheduler_settings.session_context_max_tokens()
        decision = policy.decide(context, cost, budget_usd=launch.max_budget_usd, max_tokens=maximum,
            can_compact=agent.resident.started and Capability.HEADLESS_COMPACTION in launch.runtime.capabilities)
        if decision.action is policy.Action.KEEP:
            return
        after = None
        bound_record = record_id
        if decision.action is policy.Action.COMPACT:
            compacted, bound_record = await _compact(agent, request, reopened, launch, prompt, decision, maximum)
            after = compacted.post_tokens
            if policy.after_compaction(compacted, max_tokens=maximum) is policy.Outcome.COMPACTED:
                return
        await agent.resident.stop(kill=True)
        agent.session_id, agent.totals = None, Usage()
        agent.context_rotation_pending = True
        await agent_sessions.save_session(f"task-manager-{agent.task_id}", None, {}, runtime_type="claude-code",
            profile_id=launch.profile.id, provider_id=launch.profile.provider.id, pending_handoff="context_bound")
        if bound_record is not None:
            await llm_records.add_metadata(bound_record, {
                "session_bound": policy.bound_metadata(policy.Outcome.ROTATED, decision, after)})
    except Exception:
        logger.warning("task Manager %s context maintenance failed", agent.task_id, exc_info=True)


async def _compact(agent: Any, request: Any, reopened: Any, launch: Any, prompt: Any,
                   decision: policy.Decision, maximum: int) -> tuple[policy.Compaction, int | None]:
    boundary = None
    finished = False
    recorder = ClaudeRunRecorder(agent_id=f"task-manager-{agent.task_id}", task_id=agent.task_id,
        command=agent.resident.command, cwd=reopened.work_dir, input_text="/compact", session_id=agent.session_id,
        system_prompt=prompt, launch=launch, pid_of=lambda: agent.resident.pid)
    async with recorder:
        await recorder.annotate({"maintenance": "session_bound"})

        async def on_event(event: dict[str, Any]) -> None:
            nonlocal boundary
            await recorder.record(event)
            if (found := compaction(event)) is not None:
                boundary = found

        try:
            result = await asyncio.wait_for(agent.resident.run_turn("/compact", on_event, tag=request,
                stop=recorder.cap_stop, cancel=request.cancel), MAINTENANCE_SECONDS)
            finished = (result.result_event is not None and result.result_event.get("subtype") == "success"
                        and not result.result_event.get("is_error") and not result.stopped)
            await recorder.finish(output_text=None, result_event=result.result_event, usage=result.usage,
                error=None if finished and boundary is not None else "headless compaction not confirmed")
            if result.stopped:
                from .task_manager import TaskManagerEvent
                await request.sink.event(TaskManagerEvent.STOPPED, {"method": result.stopped.value})
        except Exception as exc:
            logger.warning("task Manager %s compaction failed (%s)", agent.task_id, type(exc).__name__)
            await recorder.finish(output_text=None, result_event=None, error=f"compaction failed: {type(exc).__name__}")
        agent.totals = agent.resident.totals
        await agent_sessions.save_session(f"task-manager-{agent.task_id}", agent.session_id, agent.totals.to_dict(),
            runtime_type="claude-code", profile_id=launch.profile.id, provider_id=launch.profile.provider.id)
        await _measure(agent, recorder.record_id, maintenance=True)
        post = (boundary or {}).get("post_tokens")
        # A task session is retained only after a positively measured reduction below the bound.
        measured = policy.Compaction(finished and post is not None, boundary is not None, post)
        outcome = policy.after_compaction(measured, max_tokens=maximum)
        await recorder.annotate({"task_manager_compaction": {"reason": decision.reason.value, "result": outcome.value},
                                "session_bound": policy.bound_metadata(outcome, decision, post)})
        return measured, recorder.record_id
