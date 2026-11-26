"""Operator completion and task Manager profile selection."""

from .. import projects, task_conversations_settings, task_manager_settings
from ..action_levels import ActionLevel
from ..api_v1 import STATUS, Operation, _BOOL, _NULL_STR, _STR, _array, _object
from ..ops import ErrorCode, OpError
from ...db import tasks

_SUMMARY = "Complete a task that is in direct mode (not running in the background)."
_PROFILE_SUMMARY = "Select or clear this task Manager's Claude Code profile."
PROFILE_VIEW = _object({
    "task_id": _STR, "override_profile_id": _NULL_STR,
    "effective": _object({"profile_id": _STR, "model": _STR, "runtime": _STR,
                          "provider": _STR, "effort": _NULL_STR}, closed=True),
    "source": {"type": "string", "enum": ["task", "role", "default"]},
    "choices": _array(_object({"profile_id": _STR, "label": _STR, "model": _STR, "runtime": _STR}, closed=True))},
    closed=True)
RESULTS = {}
OPERATIONS = (
    Operation(
        "tasks.task_manager_profile_get", "POST", "/api/v1/tasks/task_manager_profile_get",
        "Read this task Manager's effective profile and choices.",
        "Read this task Manager's effective profile and choices. Work mode must be enabled. "
        "Sources are task, role or default; a Manager's own profile is never inherited.",
        _object({"task_id": {"type": "string", "minLength": 1}}, closed=True), PROFILE_VIEW,
        risk="read", operator_only=True,
        level=ActionLevel.INSIDE),  # reads only Looper's task settings and registered profiles
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
        PROFILE_VIEW,
        risk="write", operator_only=True,
        level=ActionLevel.INSIDE),  # updates only a task's own Looper profile override
)


async def run(server, operation, arguments, principal, headers):
    if not task_conversations_settings.enabled():
        raise OpError(ErrorCode.CONFLICT, "work mode is off (tasks.conversations.enabled)")
    try:
        if operation.operation_id in {"tasks.task_manager_profile_get", "tasks.task_manager_profile_set"}:
            task = await tasks.get_task(arguments["task_id"])
            if task is None:
                raise OpError(ErrorCode.NOT_FOUND, "task not found")
            if operation.operation_id == "tasks.task_manager_profile_set":
                await task_manager_settings.set_task_manager_profile(
                    task["id"], arguments["profile_id"],
                    publish=getattr(server._web_adapter, "publish_task_event", None))
            return await task_manager_settings.task_manager_profile_get(task)
        done = await server._task_router.complete_direct(arguments["task_id"])
    except projects.RequestError as exc:
        code = next((code for code, status in STATUS.items() if status == exc.status), ErrorCode.INVALID)
        raise OpError(code, str(exc)) from None
    return {"task_id": arguments["task_id"], "status": done.status, "changed": done.changed,
            "warning": done.warning}
