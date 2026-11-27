"""Operator-only operation on the task branch a direct session records (``task_manager_refs``)."""

from ...db import task_manager_refs as store
from ...db import tasks
from .. import task_manager_refs
from ..action_levels import ActionLevel
from ..api_v1 import Operation, _NULL_STR, _STR, _object
from ..ops import ErrorCode, OpError

_BRANCH_SUMMARY = "Record the branch checked out in a task's directory as its task branch."
RESULTS = {}
OPERATIONS = (
    Operation(
        "tasks.task_manager_branch_set", "POST", "/api/v1/tasks/task_manager_branch_set", _BRANCH_SUMMARY,
        _BRANCH_SUMMARY + " A direct session refuses its next turn while HEAD is not on this branch; use this "
        "after you moved the checkout on purpose. A detached HEAD, a worktree task and a task without a directory "
        "are refused. Returns the new and the previous branch.",
        _object({"task_id": {"type": "string", "minLength": 1}}, closed=True),
        _object({"task_id": _STR, "branch": _STR, "previous": _NULL_STR}, closed=True),
        risk="write", operator_only=True,
        level=ActionLevel.INSIDE),  # reads the checkout's HEAD through git_safe and writes only Looper's own row
)


async def run(server, operation, arguments, principal, headers):
    task = await tasks.get_task(arguments["task_id"])
    if task is None:
        raise OpError(ErrorCode.NOT_FOUND, "task not found")
    work_dir = str(task.get("work_dir") or "")
    if task.get("workspace_mode") == "worktree":
        raise OpError(ErrorCode.CONFLICT, "a worktree task has no in-place task branch to record")
    if not work_dir:
        raise OpError(ErrorCode.CONFLICT, "the task has no directory")
    try:
        head = (await task_manager_refs.snapshot(work_dir)).head
    except Exception as exc:  # git missing or timed out, or a repository git_safe refuses (a wrapped Refused)
        raise OpError(ErrorCode.CONFLICT, f"cannot read the task directory's HEAD: {exc}") from None
    if head is None:
        raise OpError(ErrorCode.CONFLICT, "HEAD is detached: check a branch out first")
    branch = head.removeprefix(task_manager_refs.HEADS)
    return {"task_id": task["id"], "branch": branch, "previous": await store.set_branch(task["id"], branch)}
