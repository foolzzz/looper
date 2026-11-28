"""Operator-only operations of the task Manager's ref reports (``task_manager_refs``)."""

from ...db import task_manager_refs as store
from ...db import tasks
from .. import task_manager_refs
from ..action_levels import ActionLevel
from ..api_v1 import Operation, _INT, _NULL_STR, _STR, _object
from ..ops import ErrorCode, OpError

_BRANCH_SUMMARY = "Record the branch checked out in a task's directory as its task branch."
_RESOLVE_SUMMARY = "Mark a task Manager ref report handled."
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
    Operation(
        "tasks.task_manager_ref_report_resolve", "POST", "/api/v1/tasks/task_manager_ref_report_resolve",
        _RESOLVE_SUMMARY,
        _RESOLVE_SUMMARY + " The report leaves \"needs you\"; nothing in git changes and nothing is restored. "
        "Resolving a handled report again keeps its first time.",
        _object({"report_id": {"type": "integer", "minimum": 1}}, closed=True),
        _object({"report_id": _INT, "resolved_at": _STR}, closed=True),
        risk="write", operator_only=True,
        level=ActionLevel.INSIDE),  # writes only Looper's own report row
)


async def run(server, operation, arguments, principal, headers):
    if operation.operation_id == "tasks.task_manager_ref_report_resolve":
        resolved = await store.resolve_report(arguments["report_id"], "operator")
        if resolved is None:
            raise OpError(ErrorCode.NOT_FOUND, "ref report not found")
        return {"report_id": arguments["report_id"], "resolved_at": resolved.isoformat()}
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
    try:
        problem = await task_manager_refs.branch_problem(work_dir, branch)
    except Exception as exc:
        raise OpError(ErrorCode.CONFLICT, f"cannot check the branch name: {exc}") from None
    if problem is not None:
        raise OpError(ErrorCode.CONFLICT, f"{task_manager_refs.display(branch)}: {problem}")
    return {"task_id": task["id"], "branch": branch, "previous": await store.set_branch(task["id"], branch)}
