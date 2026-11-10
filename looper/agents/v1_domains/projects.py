"""Full project administration and typed results for the existing compact operations."""

from ...db import projects as db_projects
from .. import projects as core
from ..api_v1 import Operation, _array, _object, _STR, _NULL_STR, _INT, _BOOL, _summary
from ..ops import Call, ErrorCode, OpError, Turn
from ..ops.config import _deleting_project, answered, directory_change_impact, project_named
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
    **dict.fromkeys(("planner_roles", "wait_commands", "user_gates", "agent_room", "operator_notes", "vault",
                    "dependency_conditions"),
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
MOVED = _object({"project": PROJECT, "old_dir": _STR, "new_dir": _STR, "link_old": _BOOL,
    "repairs": _array(_object({"repository": _STR, "ok": _BOOL, "output": _STR})),
    "notes": _object({"moved": _BOOL, "from": _STR, "to": _STR, "reason": _STR}, required=("moved",))})
MOVE_PREVIEW = _object({"old_dir": _STR, "new_dir": _STR, "impact": _STR, "move_version": _STR})
MOVE_REQUEST = {"project": _STR, "to": _STR, "link_old": _BOOL}
SCHEMAS = {"Project": PROJECT, "ProjectExecution": _object(EXECUTION), "ProjectPatch": PATCH}
RESULTS = {
    # The compact Manager list omits the root when unset to preserve its model-visible result.
    "projects.list": _object({"projects": _array(_object({"project": _STR, "work_dir": _NULL_STR,
        "tasks": _INT, "active_tasks": _INT, "schedules": _INT, "background": _NULL_STR})),
        "projects_root": _STR}, ["projects"]),
    **dict.fromkeys(("projects.get", "projects.create", "projects.update"), PROJECT_RESULT),
    "projects.delete": DELETED,
    "projects.move": MOVED,
}


def _op(action, request, result, risk):
    description = f"{action.capitalize()} projects." if action == "list" else f"{action.capitalize()} a project."
    if action == "delete":
        description = ("Delete a project with its tasks and schedules, stopping its running tasks first "
                       "(the user confirms first).")
    if action == "move_dir":
        description = ("Move a project's directory and files, rewriting schedule directories and preserving the old "
                       "path as a symbolic link by default; refuses active work and asks clients to confirm first.")
    if action == "preview_move_dir":
        description = "Preview a project's directory move and its confirmation impact without moving files."
    return Operation(f"project_admin.{action}", "POST", f"/api/v1/project_admin/{action}",
                     _summary(description), description, request, result, risk=risk, confirms=risk == "high" or action == "update")


OPERATIONS = (
    _op("list", _object({}, closed=True), _object({"projects": _array(PROJECT), "projects_root": _STR}), "read"),
    _op("create", _object(PATCH_FIELDS, ["name"], closed=True), PROJECT_RESULT, "write"),
    _op("update", _object({"project_id": _STR, "fields": PATCH}, closed=True), PROJECT_RESULT, "write"),
    _op("delete", _object({"project_id": _STR}, closed=True), DELETED, "high"),
    _op("preview_move_dir", _object(MOVE_REQUEST, ["project"], closed=True), MOVE_PREVIEW, "read"),
    _op("move_dir", _object({**MOVE_REQUEST, "move_version": _STR}, ["project"], closed=True), MOVED, "high"),
)


async def _move_project(arguments):
    if arguments.get("_move_project_id"):
        return await answered(core._existing_project(arguments["_move_project_id"]))
    project = await db_projects.get_project(arguments["project"])
    return project if project is not None else await project_named(arguments["project"])


async def impact(server, operation, arguments, principal, headers):
    if operation.operation_id == "project_admin.move_dir":
        project = await _move_project(arguments)
        move = await core.preview_project_move(project, arguments)
        arguments["_move_project_id"] = project["id"]
        arguments["_move_version"] = (move.version if principal.kind == "client" else
                                      arguments.get("move_version", move.version))
        return core.project_move_impact(project, move)
    if operation.operation_id == "project_admin.update":
        if "work_dir" not in arguments["fields"]:
            return None
        project = await answered(core._existing_project(arguments["project_id"]))
        change = await answered(core.directory_change(project, arguments["fields"]["work_dir"]))
        if not change.schedules:
            return None
        # Bind the shown read; the closed request cannot supply this server-owned version.
        arguments["_directory_version"] = change.version
        return directory_change_impact(project, change, arguments["fields"])
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
    if action == "preview_move_dir":
        project = await _move_project(arguments)
        move = await core.preview_project_move(project, arguments)
        return {"old_dir": move.old, "new_dir": move.new, "impact": core.project_move_impact(project, move),
                "move_version": move.version}
    if action == "move_dir":
        project = await _move_project(arguments)
        return await core.move_project_dir(project["id"], arguments,
            expected_move_version=arguments.get("_move_version", arguments.get("move_version")))
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
    # A confirmed directory update must reach the version check even if another writer set the same target.
    if arguments.get("_directory_version") is None and (
            not changes or all(before[key] == value for key, value in changes.items())):
        raise OpError(ErrorCode.INVALID, "give at least one changed project field")
    return {"project": await answered(core.update_project(
        project_id, fields, expected_directory_version=arguments.get("_directory_version")))}
