"""Scenario Manager definitions and task templates, using the existing service invariants."""

from .. import managers_api
from ..api_v1 import Operation, _array, _object, _STR, _NULL_STR, _INT, _BOOL
from ..ops.config import answered
from ..ops.service import _plain

ORDER = 9


def _enum(*values):
    return {"type": "string", "enum": list(values)}


_ID = {"type": "string", "minLength": 1}
_INPUT_FIELDS = {"key": _STR, "label": _STR, "kind": _enum("text", "url"),
                 "required": _BOOL, "pattern": _STR, "hint": _STR}
_PROFILES = _object({role: _STR for role in ("planner", "worker", "reviewer")}, [], closed=True)
_TEMPLATE_FIELDS = {
    "name": _STR, "description": _STR, "project_id": _NULL_STR, "work_dir": _STR,
    "workspace_mode": {"type": ["string", "null"], "enum": ["worktree", "in_place", None]},
    "requirement": _STR, "planner_instructions": _STR, "profiles": _PROFILES,
    "role_templates": _array(_STR), "inputs": _array(_object(_INPUT_FIELDS)),
}
TASK_TEMPLATE = _object({"key": _STR, **_TEMPLATE_FIELDS, "updated_at": _STR})
_MANAGER_FIELDS = {
    "name": _STR, "description": _STR, "system_prompt": _NULL_STR, "profile_id": _NULL_STR,
    "tools": _enum("all", "tasks"), "task_scope": _enum("all", "own"),
    "task_policy": _enum("free", "templates_only"), "enabled": _BOOL,
}
_VIEW_FIELDS = {
    "id": _STR, **_MANAGER_FIELDS, "builtin": _BOOL, "profile_name": _NULL_STR,
    "templates": _array(TASK_TEMPLATE), "revision": _INT, "created_at": _STR, "updated_at": _STR,
}
MANAGER_VIEW = _object({**_VIEW_FIELDS,
    "state": _object({"running": _BOOL, "busy": _BOOL, "healthy": _BOOL, "sync": _enum("ok", "pending"),
                      "sync_error": _STR}, ["running", "busy", "healthy", "sync"]),
    "spend": _object({"today_usd": {"type": "number"}, "last_7d_usd": {"type": "number"}, "runs_today": _INT}),
}, list(_VIEW_FIELDS))
MANAGERS = _object({"managers": _array(MANAGER_VIEW), "max_enabled": _INT})
_PATCH = {**_object(_MANAGER_FIELDS, [], closed=True),
          "anyOf": [{"required": [field]} for field in _MANAGER_FIELDS]}
# The service supplies omitted template/input defaults, just as the dashboard does today.
_TEMPLATE_REQUEST = {**_TEMPLATE_FIELDS, "inputs": _array(_object(_INPUT_FIELDS, ["key", "label"], closed=True))}


def _op(name, summary, request, result, *, risk="high"):
    return Operation(name, "POST", "/api/v1/" + name.replace(".", "/"), summary, summary,
                     request, result, risk=risk, confirms=risk == "high")


OPERATIONS = (
    _op("managers.list", "List Managers with task templates, live synchronization state and spend.",
        _object({}, closed=True), MANAGERS, risk="read"),
    _op("managers.create", "Create a scenario Manager definition.",
        _object({"key": _STR, **_MANAGER_FIELDS}, ["key", "name", "system_prompt"], closed=True),
        _object({"manager": MANAGER_VIEW})),
    _op("managers.update", "Change the given fields of a Manager definition.",
        _object({"manager_id": _ID, "fields": _PATCH}, closed=True), _object({"manager": MANAGER_VIEW})),
    _op("managers.delete", "Retire a scenario Manager without tasks or bots.",
        _object({"manager_id": _ID}, closed=True), MANAGERS),
    _op("manager_templates.put", "Create or replace a Manager's task template.",
        _object({"manager_id": _ID, "key": _STR, **_TEMPLATE_REQUEST},
                ["manager_id", "key", "name"], closed=True), MANAGERS),
    _op("manager_templates.delete", "Delete a Manager's task template.",
        _object({"manager_id": _ID, "key": _STR}, closed=True), MANAGERS),
)
RESULTS = {}


async def impact(server, operation, arguments, principal, headers):
    name = operation.operation_id
    if name == "managers.create":
        return (f"Create Manager manager-{arguments['key']} with the supplied definition? "
                f"enabled={arguments.get('enabled', True)}, tools={arguments.get('tools', 'tasks')}, "
                f"task_scope={arguments.get('task_scope', 'own')}, task_policy={arguments.get('task_policy', 'free')}. "
                "When enabled it can receive messages using the supplied prompt and profile.")
    manager_id = arguments["manager_id"]
    if name == "managers.update":
        fields = arguments["fields"]
        changes = ", ".join(f"{key}={fields[key]}" if key in ("tools", "task_scope", "task_policy", "enabled", "profile_id")
                            else key for key in sorted(fields))
        return (f"Change Manager {manager_id} fields: {changes}? "
                "The supplied definition, tool/task permissions and enabled state apply to its runtime; "
                "existing tasks and threads keep their attribution.")
    if name == "managers.delete":
        return (f"Retire Manager {manager_id}, stop its runtime and remove all its task templates? "
                "Its key stays reserved and existing threads keep their owner; tasks or bots block deletion.")
    key = arguments["key"]
    action = "Replace or create" if name == "manager_templates.put" else "Delete"
    return (f"{action} task template {key!r} on Manager {manager_id}? "
            "This changes its future task creation; tasks already created retain their saved configuration.")


async def run(server, operation, arguments, principal, headers):
    name = operation.operation_id
    if name == "managers.list":
        return _plain(await answered(managers_api.managers_view(
            extra=server._managers.state if server._managers else None)))
    if name == "managers.create":
        return await answered(managers_api.create_manager(arguments))
    manager_id = arguments["manager_id"]
    if name == "managers.update":
        return await answered(managers_api.update_manager(manager_id, arguments["fields"]))
    if name == "managers.delete":
        return await answered(managers_api.delete_manager(manager_id))
    if name == "manager_templates.put":
        return await answered(managers_api.put_template(manager_id, arguments["key"],
                              {k: v for k, v in arguments.items() if k not in ("manager_id", "key")}))
    return await answered(managers_api.delete_template(manager_id, arguments["key"]))
