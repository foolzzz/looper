"""Full project administration and typed results for the existing compact operations."""

from .. import projects as core
from ..api_v1 import Operation, _array, _object, _STR, _NULL_STR, _INT
from ..ops import Call, ErrorCode, OpError, Turn
from ..ops.config import _deleting_project, answered
from ..project_execution import FIELDS, ROOM_LIMITS


def _choice(*values):
    return {"type": ["string", "null"], "enum": [*values, None]}


# Enumerate the stored execution columns once; input compatibility is derived below.
EXECUTION = {
    "task_budget_usd": {"type": ["number", "null"]},
    "room_consult_budget_usd": {"type": ["number", "null"]},
    **dict.fromkeys(("supervise_interval_seconds", "max_step_retries", "role_text_max_chars",
                     "gate_reminder_hours", *ROOM_LIMITS), {"type": ["integer", "null"]}),
    "task_budget_action": _choice("wrap_up", "fail"), "review_policy": _choice("planner", "all"),
    "step_workspaces": _choice("shared", "worktree"), "task_workspaces": _choice("in_place", "worktree"),
    **dict.fromkeys(("planner_roles", "wait_commands", "user_gates", "agent_room", "operator_notes", "vault"),
                    _choice("off", "on")),
    "workspace_links": {"type": ["array", "null"], "items": _STR},
    "distill_lessons": {"type": "string", "enum": ["off", "draft", "write"]},
}
assert set(EXECUTION) == set(FIELDS)
PROJECT = _object({"project_id": _STR, "name": _STR, "description": _STR, "work_dir": _STR,
    "background": _STR, "created_at": _STR, "updated_at": _STR,
    "task_count": _INT, "active_count": _INT, "schedule_count": _INT, **EXECUTION})
INPUT_EXECUTION = {}
for field, schema in EXECUTION.items():
    if field in ("task_budget_usd", "room_consult_budget_usd"):
        schema = {"type": ["number", "string", "null"]}
    elif field == "distill_lessons":
        schema = {"type": ["string", "boolean", "null"],
                  "enum": ["off", "draft", "write", "", True, False, None],
                  "description": "Lessons mode; true maps to write, false, null and empty string map to off."}
    elif "enum" in schema or field == "workspace_links":
        schema = {"anyOf": [schema, {"const": ""}]}
    INPUT_EXECUTION[field] = schema
PATCH_FIELDS = {"name": _STR, "description": _STR, "work_dir": _STR, "background": _STR, **INPUT_EXECUTION}
PATCH = {**_object(PATCH_FIELDS, [], closed=True), "minProperties": 1}
PROJECT_RESULT = _object({"project": PROJECT})
DELETED = _object({"deleted": _STR})
SCHEMAS = {"Project": PROJECT, "ProjectExecution": _object(EXECUTION), "ProjectPatch": PATCH}
RESULTS = {
    "projects.list": _object({"projects": _array(_object({"project": _STR, "work_dir": _NULL_STR,
        "tasks": _INT, "active_tasks": _INT, "schedules": _INT, "background": _NULL_STR}))}),
    **dict.fromkeys(("projects.get", "projects.create", "projects.update"), PROJECT_RESULT),
    "projects.delete": DELETED,
}


def _op(action, request, result, risk):
    description = f"{action.capitalize()} projects." if action == "list" else f"{action.capitalize()} a project."
    if action == "delete":
        description = ("Delete a project with its tasks and schedules, stopping its running tasks first "
                       "(the user confirms first).")
    return Operation(f"project_admin.{action}", "POST", f"/api/v1/project_admin/{action}",
                     description, description, request, result, risk=risk, confirms=risk == "high")


OPERATIONS = (
    _op("list", _object({}, closed=True), _object({"projects": _array(PROJECT)}), "read"),
    _op("create", _object(PATCH_FIELDS, ["name"], closed=True), PROJECT_RESULT, "write"),
    _op("update", _object({"project_id": _STR, "fields": PATCH}, closed=True), PROJECT_RESULT, "write"),
    _op("delete", _object({"project_id": _STR}, closed=True), DELETED, "high"),
)


async def impact(server, operation, arguments, principal, headers):
    if operation.operation_id != "project_admin.delete":
        return None
    project = await answered(core.deletable_project(arguments["project_id"]))
    # Use the compact operation's impact and occupancy checks without changing its Tool contract.
    return await _deleting_project(Call({"action": "delete", "project": project["name"]}, Turn(None, None), None))


def _input_fields(fields):
    fields = dict(fields)
    # Only budgets accept numeric strings; the core retains range/precision validation.
    for field in ("task_budget_usd", "room_consult_budget_usd"):
        if isinstance(fields.get(field), str) and fields[field] != "":
            try:
                fields[field] = float(fields[field])
            except ValueError:
                raise OpError(ErrorCode.INVALID, f"{field} must be a number of US dollars") from None
    return fields


async def run(server, operation, arguments, principal, headers):
    action = operation.operation_id.rsplit(".", 1)[1]
    if action == "list":
        return await core.list_projects()
    if action == "create":
        return {"project": await answered(core.create_project(_input_fields(arguments)))}
    project_id = arguments["project_id"]
    if action == "delete":
        return await answered(core.delete_project(
            project_id, server._supervisor.bus, server._web_adapter._address, server._runtime_root,
            task_router=server._task_router))
    fields = _input_fields(arguments["fields"])
    try:
        normalized = core._project_fields(fields, update=True)
    except core.RequestError as exc:
        raise OpError(ErrorCode.INVALID, str(exc)) from None
    changes = {key: value for key, value in normalized.items() if key != "execution"}
    changes.update(normalized.get("execution", {}))
    before = core.project_view(await answered(core._existing_project(project_id)))
    if not changes or all(before[key] == value for key, value in changes.items()):
        raise OpError(ErrorCode.INVALID, "give at least one changed project field")
    return {"project": await answered(core.update_project(project_id, fields))}
