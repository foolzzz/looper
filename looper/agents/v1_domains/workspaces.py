"""Task deletion and workspace management through the existing dashboard cores."""

from ...db import step_workspaces as db_workspaces, tasks
from .. import projects, step_workspaces
from ..api_v1 import Operation, _array, _object, _STR, _NULL_STR, _INT, _BOOL
from ..ops import ErrorCode, OpError
from ..ops.config import answered
from .task_create import WORKSPACE_CHECK, WORKSPACE_MODE

STEP_WORKSPACE = _object({
    "id": _INT, "step_id": _STR, "step_key": _STR, "branch": _STR, "target_branch": _STR,
    "base_commit": _STR, "checkpoint": _BOOL, "result_commit": _NULL_STR, "merge_commit": _NULL_STR,
    "state": {"type": "string", "enum": ["active", "merging", "merged", "unmerged", "discarded"]},
    "merge_error": _NULL_STR, "merge_files": _array(_STR), "detail": _STR, "links": _array(_STR),
    "path": _STR, "removed_at": _NULL_STR, "discardable": _BOOL},
    ["id", "step_id", "branch", "target_branch", "base_commit", "checkpoint", "result_commit", "merge_commit",
     "state", "merge_error", "merge_files", "detail", "links", "path", "removed_at", "discardable"])
_TASK_ID = {"type": "string", "minLength": 1, "pattern": r"\S"}
RESULTS = {}
OPERATIONS = (
    Operation("tasks.delete", "POST", "/api/v1/tasks/delete", "Delete a stopped task and its workspaces.",
              "Delete a stopped task and its workspaces. Remove step worktrees, task checkout, run sessions and database rows; "
              "keep the task branch. Clients confirm the impact in the same session.",
              _object({"task_id": _TASK_ID}, closed=True), _object({"deleted": _STR}), risk="high", confirms=True),
    Operation("workspaces.check", "POST", "/api/v1/workspaces/check", "Check a task's workspace before creation.",
              "Check a task's workspace before creation. Use the directory or project default with mode, base validation and warnings.",
              _object({"dir": _STR, "project_id": _STR, "mode": WORKSPACE_MODE,
                       "base": {"type": "string", "maxLength": projects.BASE_MAX_CHARS}}, [], closed=True),
              WORKSPACE_CHECK, risk="read"),
    Operation("workspaces.discard", "POST", "/api/v1/workspaces/discard", "Discard a stopped task's step worktree.",
              "Discard a stopped task's step worktree. Remove its directory and branch while holding the task lock; "
              "clients confirm the impact in the same session.",
              _object({"task_id": _TASK_ID, "workspace_id": {"type": "integer", "minimum": 1,
                       "maximum": 10**18 - 1}}, closed=True), _object({"workspace": STEP_WORKSPACE}),
              risk="high", confirms=True),
)


async def impact(server, operation, arguments, principal, headers):
    task_id = arguments["task_id"].strip() if operation.operation_id == "tasks.delete" else arguments["task_id"]
    task = await tasks.get_task(task_id)
    if task is None:
        message = "task not found" if operation.operation_id == "tasks.delete" else "no such step worktree in this task"
        raise OpError(ErrorCode.NOT_FOUND, message)
    if operation.operation_id == "tasks.delete":
        if task["status"] in tasks.ACTIVE_STATUSES:
            raise OpError(ErrorCode.INVALID, "stop the task before deleting")
        return (f"Delete task {task_id}, its step worktrees, task checkout, run sessions and database records. "
                "The task branch is kept; discarded step branches are removed.")
    row = await db_workspaces.get(arguments["workspace_id"])
    if row is None or row["task_id"] != task_id:
        raise OpError(ErrorCode.NOT_FOUND, "no such step worktree in this task")
    if task["status"] in tasks.ACTIVE_STATUSES or server._task_router.has_planner(task_id):
        raise OpError(ErrorCode.CONFLICT, "stop the task before discarding its step worktrees")
    workspace = step_workspaces.StepWorkspace.from_row(row)
    if not workspace.discardable:
        why = "gone" if workspace.removed_at is not None else workspace.state.value
        raise OpError(ErrorCode.CONFLICT, f"step workspace {workspace.id} is {why}; it cannot be discarded")
    return f"Discard task {task_id}'s step worktree: delete directory {workspace.path} and branch {workspace.branch}."


async def run(server, operation, arguments, principal, headers):
    if operation.operation_id == "workspaces.check":
        return await answered(projects.check_workspace(arguments))
    if operation.operation_id == "tasks.delete":
        return await answered(server._delete_task(arguments["task_id"]))
    return await answered(server._discard_workspace(arguments["task_id"], arguments["workspace_id"]))
