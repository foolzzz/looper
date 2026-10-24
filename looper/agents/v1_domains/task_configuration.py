"""Task configuration, project placement, agent choices and budget continuation."""

from .. import projects
from ..agent_settings import CONCURRENCY_MAX, TASK_LIMIT_ROLES
from ..api_v1 import Operation, _array, _object, _STR, _NULL_STR, _INT, _BOOL, _OBJ
from ..message import TASK_ROUTER_ADDRESS, MessageEnvelope, MessageType, RestartMode
from ..ops.config import answered

ORDER = 3

_LIMIT = {"type": "integer", "minimum": 1, "maximum": CONCURRENCY_MAX}
_NULL_LIMIT = {**_LIMIT, "type": ["integer", "null"]}
_MODE = {"type": "string", "enum": ["append", "replace"]}
_ID = {"type": "string", "minLength": 1}
_VALUE = _object({"value": _INT, "source": _STR})
_PROFILE = _object({"profile_id": _STR, "name": _STR, "model": _STR, "effort": _NULL_STR,
                    "runtime": _STR, "provider": _STR})
_ROLE = {"profile": _object({"value": _PROFILE, "source": _STR}), "runtime_type": _STR,
         "system_prompt_mode": _object({"value": _MODE, "source": _STR})}
_CONCURRENCY = _object({"value": _INT, "source": _STR, "task": {"type": ["integer", "null"]},
                        "default": _VALUE})
_TASK_AGENTS = _object({"concurrency_max": _INT, "editable": _BOOL, "roles": _object({
    "planner": _object(_ROLE), "worker": _object({**_ROLE, "concurrency": _CONCURRENCY}),
    "reviewer": _object({**_ROLE, "concurrency": _CONCURRENCY})})})

# Existing Manager operation: only its result changes, not its request or behavior.
RESULTS = {"concurrency": _object({"task_id": _STR, "changes": _array(_object({
    "role": {"type": "string", "enum": [role.value for role in TASK_LIMIT_ROLES]},
    "before": _VALUE, "after": _VALUE, "task_limit": {"type": ["integer", "null"]},
    "default": _VALUE})), "note": _STR})}


def _operation(action, description, request, result, *, risk="write"):
    return Operation(f"tasks.{action}", "POST", f"/api/v1/tasks/{action}", description, description,
                     request, result, risk=risk, confirms=risk == "high")


OPERATIONS = (
    _operation("configure", "Edit an unstarted task's configuration.",
               _object({"task_id": _ID, "requirement": _STR, "work_dir": _STR, "config": _OBJ},
                       ["task_id"], closed=True),
               _object({"task_id": _STR, "requirement": _STR, "work_dir": _STR, "status": _STR, "config": _OBJ})),
    _operation("move", "Move a task to another project, including its live placement.",
               _object({"task_id": _ID, "project_id": _ID}, closed=True),
               _object({"task_id": _STR, "project_id": _STR, "project_name": _STR})),
    _operation("agents", "Change or clear a task's profile, concurrency and prompt mode choices.",
               _object({"task_id": _ID,
                        "profiles": _object({role: _NULL_STR for role in projects.TASK_PROFILE_ROLES}, [], closed=True),
                        "concurrency": _object({role.value: _NULL_LIMIT for role in TASK_LIMIT_ROLES}, [], closed=True),
                        "system_prompt_modes": _object({role: {"type": ["string", "null"],
                            "enum": ["append", "replace", None]} for role in projects.TASK_PROFILE_ROLES}, [], closed=True)},
                       ["task_id"], closed=True),
               _object({"task_id": _STR, "agents": _TASK_AGENTS, "note": _STR}, ["task_id", "agents"])),
    _operation("continue", "Raise a budget-failed task's cap and continue its saved plan.",
               _object({"task_id": _ID, "task_budget_usd": {"type": "number", "exclusiveMinimum": 0}}, closed=True),
               _object({"task_id": _STR, "project_id": _STR, "task_budget_usd": {"type": "number"},
                        "spent_usd": {"type": "number"}, "continued": _STR}), risk="high"),
)


async def impact(server, operation, arguments, principal, headers):
    if operation.operation_id != "tasks.continue":
        return None
    task, result = await answered(projects.check_cap_to_continue(arguments["task_id"], arguments))
    # Server-owned context survives ClientOps confirmation; the closed request cannot supply it.
    arguments["_cap_scope"] = (task["project_id"], task.get("budget_usd") is not None)
    scope = ("this task's own spend cap" if task.get("budget_usd") is not None else
             f"project {result['project_id']}'s per-task spend cap, which also affects its other tasks")
    return f"Set {scope} to ${result['task_budget_usd']:g} and continue task {result['task_id']} from its saved plan."


async def run(server, operation, arguments, principal, headers):
    task_id = arguments["task_id"]
    data = {key: value for key, value in arguments.items() if key not in ("task_id", "_cap_scope")}
    if operation.operation_id == "tasks.configure":
        return await answered(server._task_config_result(task_id, data))
    if operation.operation_id == "tasks.move":
        return await answered(projects.move_task(task_id, data, server._task_router))
    if operation.operation_id == "tasks.agents":
        result = await answered(projects.set_task_agents(task_id, data))
        if data.get("concurrency"):
            await server._recheck_concurrency()
        return result
    result = await answered(projects.raise_cap_to_continue(task_id, data, expected_scope=arguments.get("_cap_scope")))
    await server._supervisor.bus.publish(MessageEnvelope.create(
        source=server._web_adapter._address, target=TASK_ROUTER_ADDRESS, message_type=MessageType.TASK_RESTART,
        payload={"task_id": task_id, "mode": RestartMode.AT_CAP.value}))
    return {**result, "continued": task_id}
