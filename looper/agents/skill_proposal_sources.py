"""Planner closing replies and the Manager's proposal-only tool, with durable feedback."""
from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from ..db import activity
from ..db.connection import get_pool, transaction
from . import skill_proposals as proposals, skill_secrets
from .project_skills import ProjectSkillError, skill_name

logger = logging.getLogger(__name__)


def _safe_name(item: Mapping[str, Any]) -> str:
    try:
        name = skill_name(item.get("name"))
        return "(draft)" if skill_secrets.find(name) else name
    except ProjectSkillError:
        return "(draft)"


async def _refused(project_id: str, item: Mapping[str, Any], result: proposals.ProposalResult,
                   via: proposals.Via, record_id: int | None = None) -> None:
    # Keep the typed rule and its explanation, never the refused content.
    async with transaction() as conn:
        await conn.execute(
            "INSERT INTO skill_proposal_events "
            "(project_id, name, action, actor_kind, via, refusal, note, record_id) "
            "VALUES ($1, $2, 'refuse', 'agent', $3, $4, $5, $6)",
            project_id, _safe_name(item), via.value, result.refusal.value, result.detail[:500], record_id)


async def from_planner(task_id: str, project_id: str, items: Sequence[Any], record_id: int | None) -> None:
    """Store each optional closing draft independently; a refusal or storage failure never fails the task."""
    if not items or not project_id:
        return
    for draft in items:
        try:
            item = draft.to_dict() if hasattr(draft, "to_dict") else dict(draft)
            evidence = item.get("evidence")
            if evidence is None:
                steps = await get_pool().fetch(
                    "SELECT step_key FROM task_steps WHERE task_id = $1 ORDER BY step_index LIMIT $2",
                    task_id, proposals.EVIDENCE_MAX)
                evidence = [{"task": task_id, "step": row["step_key"]} for row in steps]
                evidence = evidence or [{"task": task_id}]
            result = await proposals.propose(project_id, item, proposer=f"planner-{task_id}",
                                             evidence=evidence, via=proposals.Via.PLANNER)
            if result.proposal is None:
                await _refused(project_id, item, result, proposals.Via.PLANNER, record_id)
                summary = f"Skill proposal {_safe_name(item)} refused: {result.refusal.value} — {result.detail}"
            else:
                proposal = result.proposal
                async with transaction() as conn:
                    await conn.execute(
                        "UPDATE skill_proposal_events SET record_id = $2 WHERE proposal_id = $1 AND action = 'propose'",
                        proposal["proposal_id"], record_id)
                if any(entry["task"] == task_id for entry in evidence):
                    continue  # propose already wrote the creation activity for its evidence tasks.
                summary = f"Skill proposal p-{proposal['proposal_id']} created for {proposal['name']!r}"
            await activity.insert_activity(task_id=task_id, worker=f"planner-{task_id}", kind="phase", summary=summary[:500])
        except Exception:
            logger.warning("task %s: could not store a skill proposal", task_id, exc_info=True)


async def manager_propose(project_id: str, item: Mapping[str, Any], *, proposer: str) -> proposals.ProposalResult:
    """Write only a proposal, with the same checks as a Planner draft."""
    result = await proposals.propose(project_id, item, proposer=proposer, via=proposals.Via.MANAGER)
    if result.proposal is None:
        await _refused(project_id, item, result, proposals.Via.MANAGER)
    return result


async def _feedback(project_id: str) -> list[str]:
    """Claim project feedback atomically: concurrent Planners cannot tell the same event twice."""
    if not project_id:
        return []
    async with transaction() as conn:
        rows = await conn.fetch(
            "WITH selected AS (SELECT e.id FROM skill_proposal_events e WHERE e.told_at IS NULL AND "
            "((e.project_id = $1 AND e.action = 'refuse') OR "
            "(e.proposal_id IN (SELECT id FROM skill_proposals WHERE project_id = $1) "
            "AND e.action IN ('confirm', 'reject', 'expire', 'supersede'))) "
            "ORDER BY e.id FOR UPDATE SKIP LOCKED), "
            "delivered AS (UPDATE skill_proposal_events e SET told_at = now() FROM selected s "
            "WHERE e.id = s.id RETURNING e.*) "
            "SELECT e.*, COALESCE(p.name, e.name) AS skill_name FROM delivered e "
            "LEFT JOIN skill_proposals p ON p.id = e.proposal_id ORDER BY e.id", project_id)
    verbs = {"confirm": "confirmed", "reject": "rejected", "expire": "expired", "supersede": "superseded",
             "refuse": "refused"}
    return [f"skill proposal " + (f"p-{row['proposal_id']} " if row["proposal_id"] is not None else "")
            + f"{row['skill_name']}: {verbs[row['action']]}"
            + (f" — {row['refusal']}: {row['note']}" if row["refusal"] else f" — {row['note']}" if row["note"] else "")
            for row in rows]


async def final_summary(planner: Any, plan: Any, reply: Any) -> str:
    if reply.skill_proposals:
        await from_planner(planner._task_id, plan.project_id, reply.skill_proposals, planner._last_record_id)
    return reply.final.markdown()


async def planner_note(project_id: str) -> list[str]:
    try:
        return await _feedback(project_id)
    except Exception:
        logger.warning("project %s: could not read skill proposal feedback", project_id, exc_info=True)
        return []


async def plan_message(planner: Any, kind: Any, message: str) -> str:
    """Add project feedback only to an opted-in PLAN turn, leaving other messages byte-identical."""
    from .tiers.planner_protocol import TurnKind

    if kind is not TurnKind.PLAN or not getattr(planner, "_skill_proposals", False):
        return message
    notes = await planner_note(planner._plan.project_id)
    return message + "\n\n" + "\n".join(notes) if notes else message
