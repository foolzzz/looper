"""Full dashboard task creation through the shared task core; no Manager Tool changes."""

from ...db import agent_setting_layers, tasks
from .. import projects
from ..api_v1 import Operation, _array, _object, _STR, _NULL_STR, _INT, _BOOL
from ..ops import ErrorCode, OpError
from ..ops.config import answered
from ..project_execution import ROOM_LIMITS, ProjectExecution

WORKSPACE_MODE = {"type": "string", "enum": ["in_place", "worktree"]}
WORKSPACE_WARNING = _object({
    "kind": {"type": "string", "enum": ["not_git", "uncommitted", "shared", "large_checkout"]},
    "text": _STR, "bytes": _INT, "files": _array(_STR),
    "tasks": _array(_object({"task_id": _STR, "name": _STR, "status": _STR, "mode": WORKSPACE_MODE}))},
    ["kind", "text"])
WORKSPACE_CHECK = _object({
    "workspace": _STR, "mode": WORKSPACE_MODE, "requested_mode": WORKSPACE_MODE,
    "default_mode": WORKSPACE_MODE,
    "git": {**_object({"repository": _STR, "branch": _NULL_STR, "head": _STR, "base_commit": _STR},
                       ["repository", "branch", "head"]), "type": ["object", "null"]},
    "warnings": _array(WORKSPACE_WARNING),
    "notes": _object({"found": _BOOL, "files": _array(_STR), "omitted": _array(_STR), "error": _STR},
                     ["found", "files", "omitted"]), "notes_on": _BOOL},
    ["workspace", "mode", "requested_mode", "git", "warnings", "notes"])


def _choice(*values):
    return {"type": ["string", "null"], "enum": [*values, "", None]}


def _inherit(kind, **fields):
    return {"anyOf": [{"type": [kind, "null"], **fields}, {"const": ""}]}


# The shared core accepts the project's execution fields as well as its descriptive fields.
NEW_PROJECT_FIELDS = _object({
    "name": {"type": "string", "minLength": 1, "pattern": r"\S"},
    "description": _STR, "work_dir": _STR,
    "background": _STR,
    "task_budget_usd": _inherit("number"),
    "task_budget_action": _choice("fail", "wrap_up"), "review_policy": _choice("planner", "all"),
    "distill_lessons": {"type": ["string", "boolean", "null"],
                        "enum": ["off", "draft", "write", "", True, False, None]},
    "step_workspaces": _choice("shared", "worktree"), "task_workspaces": _choice("in_place", "worktree"),
    "workspace_links": _inherit("array", items=_STR),
    **{field: _choice("on", "off") for field in (
        "operator_notes", "vault", "planner_roles", "wait_commands", "user_gates", "agent_room")},
    **{field: _inherit("integer") for field in (
        "supervise_interval_seconds", "max_step_retries", "role_text_max_chars", "gate_reminder_hours", *ROOM_LIMITS)},
    "room_consult_budget_usd": _inherit("number")}, ["name"], closed=True)
REQUEST = {**_object({
    "requirement": {"type": "string", "minLength": 1, "maxLength": 32_000, "pattern": r"\S"},
    "project_id": {"type": "string", "minLength": 1}, "project": NEW_PROJECT_FIELDS,
    "work_dir": _STR, "config": {"type": "object"}, "goal_id": _STR,
    "profile_overrides": _object({role: _STR
                                  for role in projects.TASK_PROFILE_ROLES}, [], closed=True),
    "workspace_mode": WORKSPACE_MODE, "base": {"type": "string", "maxLength": projects.BASE_MAX_CHARS}},
    ["requirement"], closed=True), "oneOf": [{"required": ["project_id"]}, {"required": ["project"]}]}
RESULTS = {}
OPERATIONS = (Operation(
    "tasks.create", "POST", "/api/v1/tasks/create", "Create a ready task in an existing or new project.",
    "Create a ready task in an existing or new project. Full configuration is preserved; "
    "a client creating a new project must confirm in the same session.",
    REQUEST, _object({"task_id": _STR, "status": _STR, "created_at": _STR,
                      "project_id": _STR, "workspace": WORKSPACE_CHECK}), risk="write", confirms=True),)


async def impact(server, operation, arguments, principal, headers):
    return await answered(_impact(arguments))


async def _impact(arguments):
    if "project" not in arguments:
        return None
    # Validate without creating rows before registering a client's confirmation.
    fields = projects._project_fields(arguments["project"], update=False)
    await answered(projects._profile_overrides(arguments.get("profile_overrides")))
    default_dir = fields.get("work_dir", "")
    work_dir = projects._task_work_dir(arguments.get("work_dir"), default_dir)
    await answered(projects.task_workspace(
        arguments, work_dir=work_dir, named=bool(arguments.get("work_dir", "").strip()), project_dir=default_dir,
        project_mode=ProjectExecution.from_row(fields.get("execution")).task_mode))
    return f"新建项目「{fields['name']}」（默认目录 {default_dir or '无'}），并在里面创建任务（工作目录 {work_dir}）。"


async def run(server, operation, arguments, principal, headers):
    chosen = await answered(projects._profile_overrides(arguments.get("profile_overrides")))
    result = await answered(projects.create_task_core(
        arguments, profiles=chosen, task_fields={"manager_id": "manager", "client_id": principal.client_id}))
    stored = await tasks.get_task(result["task_id"])
    profiles = await agent_setting_layers.profile_overrides(result["task_id"])
    expected = {"requirement": arguments["requirement"].strip(), "config": arguments.get("config", {}),
                "goal_id": arguments.get("goal_id", ""), "project_id": result["project_id"],
                "workspace_dir": result["work_dir"], "workspace_mode": result["workspace"]["requested_mode"],
                "workspace_base": arguments.get("base", "").strip(), "manager_id": "manager",
                "client_id": principal.client_id}
    if (stored is None or any(stored[key] != value for key, value in expected.items())
            or {role: profile["id"] for role, profile in profiles.items()} != chosen):
        raise OpError(ErrorCode.UNAVAILABLE, "the created task could not be verified; refresh tasks before retrying")
    return {"task_id": stored["id"], "status": stored["status"], "created_at": stored["created_at"],
            "project_id": stored["project_id"], "workspace": result["workspace"]}
