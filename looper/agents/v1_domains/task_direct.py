"""Operator completion of a task that is in direct mode."""

from .. import projects, task_conversations_settings
from ..action_levels import ActionLevel
from ..api_v1 import STATUS, Operation, _BOOL, _NULL_STR, _STR, _object
from ..ops import ErrorCode, OpError

_SUMMARY = "Complete a task that is in direct mode (not running in the background)."
RESULTS = {}
OPERATIONS = (
    Operation(
        "tasks.done", "POST", "/api/v1/tasks/done", _SUMMARY,
        _SUMMARY + " Work mode must be enabled. A task the background is working on is refused. "
        "A worktree task's uncommitted work is committed on its branch.",
        _object({"task_id": {"type": "string", "minLength": 1}}, closed=True),
        _object({"task_id": _STR, "status": _STR, "changed": _BOOL, "warning": _NULL_STR}),
        risk="write", operator_only=True,
        level=ActionLevel.INSIDE),  # writes only Looper's own task row; a worktree finish commits like tasks.delete
)


async def run(server, operation, arguments, principal, headers):
    if not task_conversations_settings.enabled():
        raise OpError(ErrorCode.CONFLICT, "work mode is off (tasks.conversations.enabled)")
    try:
        done = await server._task_router.complete_direct(arguments["task_id"])
    except projects.RequestError as exc:
        code = next((code for code, status in STATUS.items() if status == exc.status), ErrorCode.INVALID)
        raise OpError(code, str(exc)) from None
    return {"task_id": arguments["task_id"], "status": done.status, "changed": done.changed,
            "warning": done.warning}
