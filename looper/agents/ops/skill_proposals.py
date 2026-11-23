"""The operator's Manager may draft a project skill; only the dashboard can confirm it."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from ...db import connection
from ..project_skills import ProjectSkillNotFound
from . import Audience, Call, ErrorCode, OpError, RestrictedTool, Risk
from .config import project_named

logger = logging.getLogger(__name__)
TOOL_NAME = "looper_skill_propose"


async def enabled(project_ids: Sequence[str] | None = None) -> bool:
    """No opted-in project means no tool, including before the proposal migration exists."""
    try:
        return bool(await connection.get_pool().fetchval(
            "SELECT EXISTS (SELECT 1 FROM projects WHERE skill_proposals = 'on' "
            "AND ($1::text[] IS NULL OR id = ANY($1::text[])))",
            list(project_ids) if project_ids is not None else None))
    except Exception:
        logger.debug("could not read project skill proposal switches", exc_info=True)
        return False


async def _propose(call: Call) -> dict[str, Any]:
    from ..skill_proposal_sources import manager_propose

    project = await project_named(call.arguments["project"], manager_id=call.scope.tasks.manager_id,
                                  project_ids=call.scope.tasks.project_ids)
    item = {key: value for key, value in call.arguments.items() if key != "project"}
    try:
        result = await manager_propose(project["id"], item, proposer=call.manager_id)
    except ProjectSkillNotFound as exc:
        raise OpError(ErrorCode.NOT_FOUND, str(exc)) from None
    if result.refusal is not None:
        return {"refusal": result.refusal.value, "detail": result.detail}
    proposal = result.proposal
    assert proposal is not None
    return {"proposal": {"proposal_id": proposal["proposal_id"], "name": proposal["name"],
                         "status": proposal["status"]},
            "message": "The proposal waits for confirmation by the operator on the dashboard; "
                       "it has no effect on any run until then."}


TOOL = RestrictedTool(
    name=TOOL_NAME, risk=Risk.WRITE, audience=Audience.OPERATOR, run=_propose,
    refusal="only the operator can propose project skills, in their own direct chat or on the dashboard",
    description="Propose creating or updating a project skill for the operator to review on the dashboard. "
                "Give complete content and why a procedure repeated and will hold for later tasks. "
                "Cite evidence with an existing task id and optional step key in this project; without it the "
                "proposal is refused. Update also names skill. Leave out credentials, task-only details and "
                "one-time paths. This stores only a proposal: it waits for dashboard confirmation and has no "
                "effect until then. You cannot confirm a proposal or directly change or delete a skill.",
    schema={"type": "object", "additionalProperties": False,
            "required": ["project", "action", "name", "description", "content", "why"],
            "properties": {
                "project": {"type": "string", "minLength": 1, "description": "The project's name."},
                "action": {"type": "string", "enum": ["create", "update"]},
                "name": {"type": "string", "minLength": 1, "maxLength": 64},
                "description": {"type": "string", "minLength": 1, "maxLength": 1024},
                "content": {"type": "string", "minLength": 1, "maxLength": 65536},
                "why": {"type": "string", "minLength": 1, "maxLength": 500},
                "skill": {"type": "string", "minLength": 1, "maxLength": 64},
                "evidence": {"type": "array", "items": {"type": "object", "required": ["task"],
                    "additionalProperties": False, "properties": {
                        "task": {"type": "string", "minLength": 1},
                        "step": {"type": "string", "minLength": 1}}}},
            }},
)
