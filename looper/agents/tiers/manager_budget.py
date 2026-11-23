"""Manager session-budget I/O; the policy lives in ``agents.session_budget``.

Only enabled turns retain their session lock through channel delivery and maintenance. A rotation closes the
resident now; the next turn reopens the old session for the existing handoff/switch flow before starting fresh.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from dataclasses import dataclass
from functools import lru_cache, wraps
from typing import TYPE_CHECKING, Any

from ...db import agent_sessions, connection
from .. import scheduler_settings, session_budget as policy, session_switch
from ..claude_cli import ClaudeRunRecorder
from ..run_usage import compaction
from ..runtimes import Capability
from ..runtimes.cancel import TurnCancel
from ..runtimes.pricing import RunSpend, pricer
from ..session_switch import SwitchReason
from .manager_session import MAIN_SESSION, ManagerSession
from .turn_token import TurnToken

if TYPE_CHECKING:
    from ..agent_settings import Launch
    from .manager import ManagerAgent, Prepared

logger = logging.getLogger(__name__)
MAINTENANCE_SECONDS = 120


@dataclass
class BudgetTurn:
    token: TurnToken
    usd: float
    spend: RunSpend | None
    recorder: ClaudeRunRecorder | None = None
    switch: session_switch.SessionSwitch | None = None
    over_cap: bool = False


@dataclass
class _ReplyScope:
    budget: BudgetTurn | None = None


# Completion turns run in a child task. Share this holder with that child, but never with another reply.
_reply_scope: contextvars.ContextVar[_ReplyScope | None] = contextvars.ContextVar("manager_budget_reply", default=None)


@lru_cache(maxsize=64)
def _no_price(profile: str, model: str) -> None:
    logger.warning("Manager profile %s model %s: no price; the per-turn session budget stops on unpriced usage", profile, model)


async def prepare(session: ManagerSession, token: TurnToken, launch: Launch) -> dict[str, Any]:
    """An enabled turn's recorder options; an unset/unreadable switch leaves its constructor unchanged."""
    session.budget = None
    try:
        usd = await policy.turn_budget_usd()
    except Exception:
        logger.warning("cannot read the Manager session budget; leaving this turn unchanged", exc_info=True)
        return {}
    if usd is None:
        return {}
    price = launch.price or pricer(launch.settings.resolved_model, catalog=launch.settings.pricing_catalog)
    cap = min(usd, launch.max_budget_usd) if launch.max_budget_usd is not None else usd
    spend = RunSpend(price, launch.model, cap)
    if not price.model_price(launch.model).complete:
        _no_price(launch.profile.id, launch.model)
    session.budget = BudgetTurn(token, usd, spend, switch=pending(session))
    if (scope := _reply_scope.get()) is not None:
        scope.budget = session.budget
    return {"spend": spend} if spend is not None else {}


def holds_lock(session: ManagerSession, token: TurnToken) -> bool:
    return session.budget is not None and session.budget.token is token and not token.abandoned


def after_reply(method: Any) -> Any:
    """Release an enabled turn's retained lock only after its reply and best-effort maintenance."""
    @wraps(method)
    async def wrapped(manager: ManagerAgent, *args: Any, **kwargs: Any) -> Any:
        session = manager._session_now()
        scope = _ReplyScope()
        context = _reply_scope.set(scope)
        delivered = False
        try:
            result = await method(manager, *args, **kwargs)
            delivered = True
            return result
        finally:
            try:
                budget = scope.budget
                if budget is not None and session.budget is budget and session.lock_owner is budget.token:
                    try:
                        if delivered and not budget.token.cancel.requested and not budget.token.abandoned:
                            await manager._bound_session(session)
                    finally:
                        try:
                            if budget.over_cap:
                                await session.claude.stop()
                        finally:
                            if session.lock_owner is budget.token:
                                session.lock_owner = None
                                session.turn_lock.release()
            finally:
                _reply_scope.reset(context)
    return wrapped


def pending(session: ManagerSession) -> session_switch.SessionSwitch | None:
    switch = session.pending_switch
    return switch if switch is not None and switch.reason is SwitchReason.SESSION_BOUND else None


async def reopen(manager: ManagerAgent, prepared: Prepared, token: TurnToken) -> None:
    """A stopped bound session must resume once to write its handoff; failure still permits a new session."""
    session = prepared.session
    process = session.process
    if prepared.switch.reason is not SwitchReason.SESSION_BOUND or session.claude.started or process is None:
        return
    try:
        command = await asyncio.to_thread(manager._claude_command,
                                         process.system_prompt.write_file(manager._session_root()), process.launch)
        try:
            await token.step("resident_admission", manager._router.admit(session, prepared.envelope))
            await token.keep("bound_handoff_start", session.claude.start(
                command, cwd=str(manager._session_work_dir()), signature=process.signature,
                env=manager._session_environment(process.launch), totals=session.session_totals, price=process.launch.price),
                30, undo=lambda: session.claude.stop(kill=True))
        finally:
            await manager._router.release(session)
    except (OSError, TimeoutError):
        logger.warning("could not reopen the bounded Manager session for its handoff", exc_info=True)


async def annotate(session: ManagerSession, recorder: ClaudeRunRecorder) -> None:
    budget = session.budget
    if budget is None:
        return
    budget.recorder = recorder
    if budget.switch is not None:
        switch = budget.switch
        await recorder.annotate({"session_bound": policy.bound_metadata(
            policy.Outcome.ROTATED, policy.Decision(policy.Action.ROTATE, switch.bound_reason, switch.before_tokens),
            switch.after_tokens)})


def cap_reply(session: ManagerSession, exc: Exception) -> str | None:
    from .manager import OverCap

    return policy.over_cap_reply(session.budget.usd) if session.budget is not None and isinstance(exc, OverCap) else None


async def completion_reply(manager: ManagerAgent, token: TurnToken, prepared: Prepared) -> str:
    """A completion turn stopped at the same cap delivers the fixed reply through its normal channel path."""
    from .manager import OverCap

    try:
        return await manager._run_prepared(token, prepared)
    except OverCap:
        if prepared.session.budget is None:
            raise
        return policy.over_cap_reply(prepared.session.budget.usd)


async def bound(manager: ManagerAgent, session: ManagerSession) -> None:
    """Measure this turn, then compact or queue a rotation. No action from a stale/unknown measurement."""
    budget = session.budget
    if budget is None or budget.recorder is None or budget.recorder.record_id is None:
        return
    try:
        pool = connection.get_pool()
        row = await pool.fetchrow("SELECT context_tokens, cost_usd FROM llm_records WHERE id=$1",
                                  budget.recorder.record_id)
        if row is None:
            return
        context, cost = row["context_tokens"], row["cost_usd"]
        if session.key == MAIN_SESSION:
            await agent_sessions.save_measurements(manager.agent_id, context_tokens=context, last_turn_cost_usd=cost)
        else:
            await pool.execute("UPDATE manager_sessions SET context_tokens=$3, last_turn_cost_usd=$4 "
                               "WHERE manager_id=$1 AND session_key=$2", manager.agent_id, session.key, context, cost)
        if context is None:
            logger.debug("Manager %s session %s: this turn's context is unknown; keeping it", manager.agent_id, session.key)
            return
        max_tokens = await scheduler_settings.session_context_max_tokens()
        decision = policy.decide(context, cost, budget_usd=budget.usd, max_tokens=max_tokens,
                                 can_compact=session.claude.started and session.process is not None
                                 and Capability.HEADLESS_COMPACTION in session.claude_runtime.capabilities)
        if decision.action is policy.Action.KEEP:
            return
        after = None
        if decision.action is policy.Action.COMPACT:
            compacted = await _compact(manager, session, decision, max_tokens)
            after = compacted.post_tokens
            outcome = policy.after_compaction(compacted, max_tokens=max_tokens)
            if outcome is policy.Outcome.COMPACTED:
                return
        session.pending_switch = session_switch.session_bound(
            decision.reason, context, after, max_tokens=max_tokens, cost_usd=cost, budget_usd=budget.usd,
            runtime=session.session_runtime, profile=session.session_profile, provider=session.session_provider)
        await session.claude.stop()
    except Exception:
        logger.warning("Manager %s session %s: session budget maintenance failed", manager.agent_id, session.key,
                       exc_info=True)


async def _compact(manager: ManagerAgent, session: ManagerSession, decision: policy.Decision,
                   max_tokens: int) -> policy.Compaction:
    process = session.process
    recorder = ClaudeRunRecorder(agent_id=manager.agent_id, task_id=None, command=process.command,
                                cwd=str(manager._session_work_dir()), input_text="/compact", session_id=session.session_id,
                                system_prompt=process.system_prompt, launch=process.launch, pid_of=lambda: session.claude.pid)
    boundary = None
    finished = False
    async with recorder:
        await recorder.annotate({"maintenance": "session_bound"})

        async def on_event(event: dict[str, Any]) -> None:
            nonlocal boundary
            await recorder.record(event)
            if (found := compaction(event)) is not None:
                boundary = found
                manager._context_changed("a session budget compaction")

        try:
            turn = await asyncio.wait_for(session.claude.run_turn("/compact", on_event, cancel=TurnCancel()),
                                          MAINTENANCE_SECONDS)
            finished = (turn.result_event is not None and turn.result_event.get("subtype") == "success"
                        and not turn.result_event.get("is_error"))
            await recorder.finish(output_text=None, result_event=turn.result_event, usage=turn.usage,
                                  error=None if finished and boundary is not None else "headless compaction not confirmed")
            session.session_totals = session.claude.totals
            await manager._save_session_state()
        except Exception as exc:
            logger.warning("Manager headless compaction failed (%s); rotating", type(exc).__name__)
            await recorder.finish(output_text=None, result_event=None, error=f"compaction failed: {type(exc).__name__}")
        result = policy.Compaction(finished, boundary is not None, (boundary or {}).get("post_tokens"))
        outcome = policy.after_compaction(result, max_tokens=max_tokens)
        # Failed compaction belongs to this maintenance record too; the next turn records the rotation itself.
        if outcome is policy.Outcome.COMPACTED:
            await recorder.annotate({"session_bound": policy.bound_metadata(outcome, decision, result.post_tokens)})
        return result


async def recent_results(manager: ManagerAgent, session: ManagerSession) -> list[str]:
    """Ended tasks are resume data, owned by this Manager; routed slots never inherit another chat's results."""
    if pending(session) is None:
        return []
    try:
        # Group and DM resume briefs use only their own chat excerpt; never copy main's task history there.
        if session.key != MAIN_SESSION:
            return []
        rows = await connection.get_pool().fetch(
            "SELECT title, status, result_summary FROM tasks WHERE manager_id=$1 "
            "AND status IN ('completed','failed','cancelled') ORDER BY completed_at DESC NULLS LAST, created_at DESC LIMIT 3",
            manager.agent_id)
        return ["", "Recent task results (stored task data):", *[
            f"- {row['title'] or '(untitled)'} ({row['status']}): {(row['result_summary'] or '')[:200]}" for row in rows]]
    except Exception:
        logger.warning("cannot read recent task results for the Manager's resume brief", exc_info=True)
        return []
