"""Full task pages and details for both API clients and the dashboard."""

from ...db import tasks
from .. import projects
from ..api_v1 import Operation, _array, _object, _STR, _NULL_STR, _INT, _BOOL, _OBJ
from ..ops.config import answered
from ..ops.service import _plain
from .costs import TASK_COST
from .task_configuration import _TASK_AGENTS
from .task_interactions import TASK_WAIT, ROOM_VIEW
from .workspaces import STEP_WORKSPACE


def _nullable(schema):
    return {**schema, "type": [schema["type"], "null"]}


_NUMBER = {"type": "number"}
_NULL_NUMBER = _nullable(_NUMBER)
_NULL_INT = _nullable(_INT)
_GROUP = {"type": "string", "enum": [group.value for group in tasks.TaskGroup]}
_TEMPLATE = _object({"key": _STR, "name": _STR, "updated_at": _STR,
    "inputs": _array(_object({"key": _STR, "label": _STR, "value": _STR})), "planner_instructions": _STR})
TASK_INFO = _object({
    **dict.fromkeys(("task_id", "requirement", "name", "goal_id", "status", "phase", "correlation_id",
                     "project_id", "project_name", "work_dir", "manager_id", "created_at"), _STR),
    **dict.fromkeys(("planner_id", "completed_at", "schedule_id", "schedule_name", "manager_name"), _NULL_STR),
    "group": _GROUP, "continues_schedule_session": _BOOL, "cost": _nullable(TASK_COST),
    "template": _nullable(_TEMPLATE)}, closed=True)
_STEP = _object({
    **dict.fromkeys(("id", "task_id", "step_key", "kind", "instruction", "status", "created_at", "updated_at"), _STR),
    **dict.fromkeys(("step_index", "retries"), _INT), "review": _BOOL,
    **dict.fromkeys(("worker_id", "reviewer_id", "role_key", "review_stage", "started_at", "completed_at"), _NULL_STR),
    "role_version": _NULL_INT, "review_position": _NULL_INT, "result": _nullable(_OBJ),
    **{key: _array(_STR) for key in ("acceptance_criteria", "target_files", "depends_on")},
    "review_roles": _array(_object({"role": _STR, "required": _BOOL, "version": _NULL_INT}))})
_STATS = _object({
    **dict.fromkeys(("runs", "running", "contract_violations", "steps", "accepted", "failed", "retries"), _INT),
    "cost_usd": _NUMBER, "contract_violation_rate": _NULL_NUMBER, "review_pass_rate": _NULL_NUMBER,
    "reviews": _object({"passed": _INT, "not_passed": _INT})})
_ROLE = _object({
    "id": _STR, "kind": _STR, "version": _INT, "role_prompt": _STR, "harness": _NULL_STR,
    "reason": _STR, "created_at": _STR, "via": _STR, "template": _NULL_STR,
    "profile": _nullable(_object({"profile_id": _STR, "name": _STR, "allowed": _BOOL})),
    "items": _array(_object({"id": _STR, "kind": _NULL_STR, "name": _NULL_STR, "description": _STR},
                             ["id", "kind", "name"])),
    "versions": _array(_object({"version": _INT, "created_at": _STR, "reason": _STR, "via": _STR,
                                "template": _NULL_STR, "sha256": _STR})),
    "steps": _array(_STR), "stats": _STATS})
_GATE = _object({
    **dict.fromkeys(("step", "step_id", "state", "topic", "findings", "resolution"), _STR),
    **dict.fromkeys(("waiting_on", "retry_at", "opened_at", "first_reply_at", "closed_at"), _NULL_STR),
    # GateSummary.view() and the persisted Planner._plan_change() projection.
    "summary": _nullable(_object({key: _array(_STR) for key in ("decisions", "assumptions", "open")})),
    "plan_change": _nullable(_object({key: _array(_STR) for key in ("added", "dropped", "stopped")})),
    "agenda": _array(_object({"id": _STR, "question": _STR, "status": _STR, "answer": _NULL_STR,
                              "options": _array(_STR)},
                             ["id", "question", "status"])),
    "ended_by_user": _BOOL, "depends_on": _array(_STR),
    **dict.fromkeys(("turns", "not_delivered", "reminders"), _INT),
    "channel_posts": _nullable(_object({"channel": _STR, "target": _STR, "sent": _INT,
                                        "failed": _INT, "last_error": _NULL_STR}))})
_COMMAND = _object({"what": _STR, "command": _STR})
_WORKTREE = _object({"branch": _STR, "base_commit": _STR, "path": _STR, "repository": _STR,
    "links": _array(_STR), "state": _STR, "result_commit": _NULL_STR, "removed_at": _NULL_STR,
    "commands": _array(_COMMAND)})
_ROOM = _object({key: ROOM_VIEW["properties"][key] for key in ("enabled", "counts", "by_step")}
    | {"consults": _object({key: value for key, value in
        ROOM_VIEW["properties"]["consults"]["properties"].items() if key != "list"})})
_PROFILE = _object({"profile_id": _STR, "name": _STR, "model_id": _STR, "effort": _NULL_STR,
    "model": _object({"model_id": _STR, "name": _STR, "protocol": _STR, "provider_id": _STR}),
    **dict.fromkeys(("runtime_id", "runtime_name", "runtime_type", "provider_id", "provider_name"), _STR),
    "builtin": _BOOL, "provider": _object(dict.fromkeys(("provider_id", "name", "vendor_type", "auth"), _STR)),
    "agent_teams": _BOOL, "custom_prompt": _NULL_STR, "roles": _array(_STR), "used_by": _OBJ,
    "cost": _OBJ, "warnings": _array(_STR)})
TASK_DETAIL = _object({
    **TASK_INFO["properties"], "title": _STR, "updated_at": _STR,
    "attempt_ended_at": _NULL_STR,
    "config": _OBJ, "origin_channel_id": _STR,
    **dict.fromkeys(("result_summary", "error", "fail_kind"), _NULL_STR),
    "task_budget_usd": _NULL_NUMBER, "own_budget_usd": _NULL_NUMBER,
    "client": _nullable(_object({"id": _STR, "name": _STR}, closed=True)),
    "profile_overrides": {"type": "object", "additionalProperties": _PROFILE},
    "agents": _nullable(_TASK_AGENTS), "roles": _array(_ROLE),
    "builtin_role_stats": _object({"worker": _STATS, "reviewer": _STATS}),
    "gates": _array(_GATE), "waits": _array(TASK_WAIT),
    "task_workspace": _object({"workspace_dir": _STR, "mode": _STR, "base": _STR,
                                "worktree": _nullable(_WORKTREE)}),
    "step_workspaces": _nullable(_object({
        "start": _nullable(_object({"branch": _STR, "commit": _STR})),
        "workspaces": _array(STEP_WORKSPACE), "undo": _array(_COMMAND)})),
    "room": _nullable(_ROOM),
    "requirement_versions": _array(_object({"version": _INT, "text": _STR, "via": _STR,
                                            "created_at": _STR, "delivered_at": _NULL_STR})),
    "supplements": _array(_object({"text": _STR, "via": _STR, "created_at": _STR, "delivered_at": _NULL_STR})),
    "manager_actions": _array(_object({"created_at": _STR, "channel": _STR, "tool": _STR,
        "arguments": _OBJ, "risk": _STR, "status": _STR, "confirmation_id": _NULL_STR,
        "manager_id": _STR, "client_id": _NULL_STR, "client_name": _NULL_STR})),
    "steps": _array(_STEP),
    "journal": _array(_object({"id": _INT, "step_id": _STR, "step_key": _STR, "kind": _STR,
        "summary": _STR, "learnings": _array(_STR), "created_at": _STR})),
    "progress": _array(_object(dict.fromkeys(("done", "now", "next", "risks", "estimate", "at"), _STR)))},
    closed=True)
TASK_DETAIL["properties"]["run_roles"] = {"type": "object", "additionalProperties": _object({
    "id": _STR, "kind": _STR, "version": _INT})}
SCHEMAS = {"TaskInfo": TASK_INFO, "TaskDetail": TASK_DETAIL}
RESULTS = {}
OPERATIONS = (
    Operation("tasks.page", "POST", "/api/v1/tasks/page", "Read a full task list page.",
        "Read a full task list page. Includes filters, status counts and an older-page cursor.",
        _object({"project_id": _STR, "schedule_id": _STR, "group": _GROUP,
            "status": {"type": "string", "enum": list(tasks.STATUS_GROUPS)}, "before": _STR,
            "limit": {"type": "integer", "minimum": 1, "maximum": projects.TASKS_PAGE_MAX}, "query": _STR},
            [], closed=True),
        _object({"tasks": _array(TASK_INFO), "counts": {"type": "object", "additionalProperties": _INT},
                 "next_before": _NULL_STR}), risk="read"),
    Operation("tasks.detail", "POST", "/api/v1/tasks/detail", "Read a task's full detail.",
        "Read a task's full detail. Includes configuration, history, workspaces and cost.",
        _object({"task_id": {"type": "string", "minLength": 1}}, closed=True), TASK_DETAIL, risk="read"),
)


async def run(server, operation, arguments, principal, headers):
    if operation.operation_id == "tasks.detail":
        return _plain(await answered(projects.task_detail(arguments["task_id"])))
    filters = {**arguments}
    if "group" in filters:
        filters["group"] = tasks.TaskGroup(filters["group"])
    return _plain(await answered(projects.list_tasks_page(**filters)))
