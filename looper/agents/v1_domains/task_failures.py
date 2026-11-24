"""Operator readback and versioned release of task engine failure holds."""
from ...db import step_failures as store, tasks
from .. import failure_handling
from ..action_levels import ActionLevel
from ..api_v1 import Operation, _array, _object, _STR, _NULL_STR, _INT, _OBJ
from ..ops import ErrorCode, OpError
from ..ops.service import _plain

_NULL_INT = {"type": ["integer", "null"]}
FAILURE = _object({
    "id": _INT, "task_id": _STR, "step_id": _NULL_STR, "step_identity": _STR, "step_key": _STR,
    "record_id": _NULL_INT, "role": {"type": "string", "enum": ["worker", "reviewer"]},
    "failure_class": {"type": "string", "enum": ["quota", "transient", "environment", "judgment"]},
    "failure_cause": _STR, "streak": _INT, "action": {"type": "string", "enum": ["hold", "planner", "breaker"]},
    "created_at": _STR, "run_id": _STR, "attempt": _NULL_STR, "epoch": _INT, "target": _OBJ, "result": _OBJ},
    closed=True)
HOLD = _object({
    "step_id": _STR, "task_id": _STR, "kind": {"type": "string", "enum": ["quota", "transient", "environment", "breaker"]},
    "cause": _STR, "provider_id": _NULL_STR, "until": _NULL_STR, "failure_id": _INT,
    "created_at": _STR, "streak": _INT, "target": _OBJ, "targets": _array(_OBJ), "result": _OBJ,
    "run_id": _STR, "role": _STR, "step_key": _STR, "reminded_at": _NULL_STR}, closed=True)
_HISTORY = _object({"items": _array(FAILURE), "next_after_id": _NULL_INT}, closed=True)
_HOLDS = _object({"holds": _array(HOLD)}, closed=True)
_RELEASE = _object({"task_id": _STR, "step_id": _STR, "failure_id": _INT,
    "action": {"type": "string", "enum": ["retry", "planner", "stop"]}, "status": _STR}, closed=True)
SCHEMAS = {"StepFailure": FAILURE, "StepFailureHold": HOLD}
RESULTS = {}


def _operation(name, summary, request, result, risk="read", confirms=False):
    return Operation(name, "POST", "/api/v1/" + name.replace(".", "/"), summary, summary,
        request, result, risk=risk, confirms=confirms, operator_only=True,
        level=ActionLevel.INSIDE)  # reads or changes only Looper's own task execution state


OPERATIONS = (
    _operation("tasks.failure_history", "Read a task's complete failure history in ascending pages.",
        _object({"task_id": {"type": "string", "minLength": 1}, "step_id": _STR,
            "after_id": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200}}, ["task_id"], closed=True), _HISTORY),
    _operation("tasks.holds", "Read a task's current engine failure holds.",
        _object({"task_id": {"type": "string", "minLength": 1}}, closed=True), _HOLDS),
    _operation("tasks.hold_release", "Release a versioned task failure hold after operator confirmation.",
        _object({"task_id": {"type": "string", "minLength": 1}, "step_id": {"type": "string", "minLength": 1},
            "failure_id": {"type": "integer", "minimum": 1},
            "action": {"type": "string", "enum": ["retry", "planner", "stop"]}}, closed=True),
        _RELEASE, "write", True),
)


def service():
    value = failure_handling.get_service()
    if value is None:
        raise OpError(ErrorCode.UNAVAILABLE, "failure handling is not ready")
    return value


def hold_view(row):
    return {key: row.get(key) for key in HOLD["properties"]}


async def _task(task_id):
    if await tasks.get_task(task_id) is None:
        raise OpError(ErrorCode.NOT_FOUND, "task not found")


async def impact(server, operation, arguments, principal, headers):
    await _task(arguments["task_id"])
    holds = await store.list_holds(arguments["task_id"])
    found = next((row for row in holds if row["step_id"] == arguments["step_id"]), None)
    if found is None:
        raise OpError(ErrorCode.NOT_FOUND, "this step has no failure hold")
    if found["failure_id"] != arguments["failure_id"]:
        raise OpError(ErrorCode.CONFLICT, "the failure hold changed; refresh before choosing an action")
    effect = {"retry": "Retry only the failed execution unit; its consecutive failure count is retained.",
              "planner": "Send this failure and its consecutive history to the Planner without running it again.",
              "stop": "Stop the entire task and clear all of its engine failure holds."}[arguments["action"]]
    return (f"Task {arguments['task_id']}, step {found.get('step_key') or found['step_id']}, "
            f"failure {found['failure_id']}: {found['kind']} ({found['cause']}). "
            f"Resume target: {found['target']}. {effect}")


async def run(server, operation, arguments, principal, headers):
    try:
        await _task(arguments["task_id"])
        if operation.operation_id == "tasks.failure_history":
            limit = arguments.get("limit", 50)
            rows = await store.list_failures(arguments["task_id"], step_id=arguments.get("step_id"),
                after_id=arguments.get("after_id", 0), limit=limit + 1)
            return _plain({"items": rows[:limit], "next_after_id": rows[limit - 1]["id"] if len(rows) > limit else None})
        if operation.operation_id == "tasks.holds":
            return _plain({"holds": [hold_view(row) for row in await store.list_holds(arguments["task_id"])]})
        result = await service().release_hold(arguments["task_id"], arguments["step_id"],
            arguments["failure_id"], arguments["action"], operator=principal.kind == "operator")
        refreshed = await tasks.get_task(arguments["task_id"])
        if refreshed is not None:
            result["status"] = refreshed["status"]
        return _plain({key: result[key] for key in _RELEASE["properties"]})
    except OpError:
        raise
    except failure_handling.FailureHandlingError as exc:
        raise OpError(ErrorCode(exc.code), str(exc)) from None
    except Exception:
        raise OpError(ErrorCode.UNAVAILABLE, "task failure database unavailable") from None
