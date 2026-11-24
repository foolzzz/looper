"""Act-only operator control of engine holds, shared by the dashboard and Manager."""
from ..failures import FailureTyping, typing_mode
from . import Audience, Call, ErrorCode, OpError, RestrictedTool, Risk


async def operator_call(turn):
    from .service import operator_call as check
    return await check(turn)


async def _authorized(call):
    if await typing_mode() is not FailureTyping.ACT:
        raise OpError(ErrorCode.CONFLICT, "engine failure handling is not active")
    if not await operator_call(call.turn):
        raise OpError(ErrorCode.FORBIDDEN, "failure holds are controlled only by the operator in the console or their direct chat")


def _arguments(call):
    return {key: value for key, value in call.arguments.items() if key != "action"} | {
        "action": call.arguments["action"]}


async def _confirm(call: Call):
    from ..v1_domains import task_failures
    await _authorized(call)
    if call.arguments["action"] == "view":
        return None
    if not call.arguments.get("step_id") or not call.arguments.get("failure_id"):
        raise OpError(ErrorCode.INVALID, "step_id and failure_id from view are required")
    from types import SimpleNamespace
    return await task_failures.impact(None, None, _arguments(call), SimpleNamespace(kind="operator"), {})


async def _run(call: Call):
    from ..v1_domains import task_failures
    await _authorized(call)
    from types import SimpleNamespace
    if call.arguments["action"] == "view":
        operation = next(op for op in task_failures.OPERATIONS if op.operation_id == "tasks.holds")
        return await task_failures.run(None, operation, {"task_id": call.arguments["task_id"]},
                                       SimpleNamespace(kind="operator"), {})
    operation = next(op for op in task_failures.OPERATIONS if op.operation_id == "tasks.hold_release")
    return await task_failures.run(None, operation, _arguments(call), SimpleNamespace(kind="operator"), {})


TOOL = RestrictedTool(
    name="looper_task_hold", audience=Audience.OPERATOR,
    refusal="Failure holds are controlled only by the operator in the console or their direct chat.",
    description="View a task's engine failure holds. After the user's confirmation, retry the failed execution "
                "unit, send its failure to the Planner, or stop the whole task. Use the exact step_id and "
                "failure_id from view; stale versions are refused. A retry retains the consecutive failure count.",
    schema={"type": "object", "additionalProperties": False, "required": ["action", "task_id"], "properties": {
        "action": {"type": "string", "enum": ["view", "retry", "planner", "stop"]},
        "task_id": {"type": "string", "minLength": 1}, "step_id": {"type": "string", "minLength": 1},
        "failure_id": {"type": "integer", "minimum": 1}}},
    risk=Risk.WRITE, read_actions=frozenset({"view"}), run=_run, escalate=_confirm,
    task_id=lambda arguments: arguments.get("task_id"),
)
