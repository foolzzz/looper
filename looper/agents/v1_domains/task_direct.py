"""Operator completion and task Manager profile selection."""

from .. import projects, task_conversations_settings
from ..action_levels import ActionLevel
from ..api_v1 import STATUS, Operation, _BOOL, _NULL_STR, _STR, _object
from ..ops import ErrorCode, OpError
from ..profile_registry import RegistryError
from ..runtimes import RuntimeUnsupported
from ...db import tasks

_SUMMARY = "Complete a task that is in direct mode (not running in the background)."
_PROFILE_SUMMARY = "Select or clear this task Manager's Claude Code profile."
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
    Operation(
        "tasks.task_manager_profile_set", "POST", "/api/v1/tasks/task_manager_profile_set", _PROFILE_SUMMARY,
        _PROFILE_SUMMARY + " Work mode must be enabled. An active turn returns 409. "
        "An idle process stops and the next turn resumes its saved session with the selected profile. "
        "Null restores the task Manager role default.",
        _object({"task_id": {"type": "string", "minLength": 1},
                 "profile_id": {"type": ["string", "null"], "minLength": 1}}, closed=True),
        _object({"task_id": _STR, "profile_id": _NULL_STR}, closed=True),
        risk="write", operator_only=True,
        level=ActionLevel.INSIDE),  # updates only a task's own Looper profile override
)


async def run(server, operation, arguments, principal, headers):
    if not task_conversations_settings.enabled():
        raise OpError(ErrorCode.CONFLICT, "work mode is off (tasks.conversations.enabled)")
    try:
        if operation.operation_id == "tasks.task_manager_profile_set":
            from ..task_manager_settings import set_task_manager_profile
            if await tasks.get_task(arguments["task_id"]) is None:
                raise OpError(ErrorCode.NOT_FOUND, "task not found")
            await set_task_manager_profile(arguments["task_id"], arguments["profile_id"])
            return {"task_id": arguments["task_id"], "profile_id": arguments["profile_id"]}
        done = await server._task_router.complete_direct(arguments["task_id"])
    except projects.RequestError as exc:
        code = next((code for code, status in STATUS.items() if status == exc.status), ErrorCode.INVALID)
        raise OpError(code, str(exc)) from None
    except (RegistryError, RuntimeUnsupported) as exc:
        raise OpError(ErrorCode.INVALID, str(exc)) from None
    return {"task_id": arguments["task_id"], "status": done.status, "changed": done.changed,
            "warning": done.warning}
