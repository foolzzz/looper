"""Task deletion and workspace management, beside the unchanged Manager services."""

from ...db import step_workspaces, tasks
from ..api_v1 import Operation, _array, _object, _STR, _NULL_STR, _INT, _BOOL
from ..ops import ErrorCode, OpError
from ..ops.config import answered
from .task_create import WORKSPACE_CHECK, WORKSPACE_MODE

_TASK_ID = {"type": "string", "minLength": 1, "pattern": r"\S"}
STEP_WORKSPACE = _object({
    "id": _INT, "step_id": _STR, "step_key": _STR, "branch": _STR, "target_branch": _STR,
    "base_commit": _STR, "checkpoint": _BOOL, "result_commit": _NULL_STR, "merge_commit": _NULL_STR,
    "state": {"type": "string", "enum": ["active", "merging", "merged", "unmerged", "discarded"]},
    "merge_error": _NULL_STR, "merge_files": _array(_STR), "detail": _STR, "links": _array(_STR),
    "path": _STR, "removed_at": _NULL_STR, "discardable": _BOOL},
    ["id", "step_id", "branch", "target_branch", "base_commit", "checkpoint", "result_commit", "merge_commit",
     "state", "merge_error", "merge_files", "detail", "links", "path", "removed_at", "discardable"])
SCHEMAS = {"StepWorkspace": STEP_WORKSPACE}
RESULTS = {}
OPERATIONS = (
    Operation("tasks.delete", "POST", "/api/v1/tasks/delete", "Delete a stopped task and its workspaces.",
              "Delete a stopped task and its workspaces. The task's result branch stays.",
              _object({"task_id": _TASK_ID}, closed=True), _object({"deleted": _STR}), risk="high", confirms=True),
    Operation("workspaces.check", "POST", "/api/v1/workspaces/check", "Check a task workspace before creation.",
              "Check a task workspace before creation. Use the project's mode, base and warnings.",
              _object({"dir": _STR, "project_id": _STR, "mode": WORKSPACE_MODE, "base": _STR}, [], closed=True),
              WORKSPACE_CHECK, risk="read"),
    Operation("workspaces.discard", "POST", "/api/v1/workspaces/discard", "Discard a stopped task's step workspace.",
              "Discard a stopped task's step workspace. Delete its directory and branch.",
              _object({"task_id": _TASK_ID,
                       "workspace_id": {"type": "integer", "minimum": 1, "maximum": 10**18 - 1}}, closed=True),
              _object({"workspace": STEP_WORKSPACE}), risk="high", confirms=True),
)


async def impact(server, operation, arguments, principal, headers):
    task_id = arguments["task_id"].strip() if operation.operation_id == "tasks.delete" else arguments["task_id"]
    task = await tasks.get_task(task_id)
    if task is None:
        raise OpError(ErrorCode.NOT_FOUND, "task not found")
    if operation.operation_id == "tasks.delete":
        return (f"Delete task {task_id} ({task['status']}), its step worktrees, task checkout, "
                "saved sessions and database records. The task's result branch stays.")
    workspace = await step_workspaces.get(arguments["workspace_id"])
    if workspace is None or workspace["task_id"] != task_id:
        raise OpError(ErrorCode.NOT_FOUND, "no such step worktree in this task")
    return (f"Discard workspace {workspace['id']} of task {task_id}: "
            f"delete directory {workspace['path']} and branch {workspace['branch']}.")


async def run(server, operation, arguments, principal, headers):
    if operation.operation_id == "tasks.delete":
        return await answered(server._delete_task(arguments["task_id"]))
    if operation.operation_id == "workspaces.discard":
        return await answered(server._discard_workspace(arguments["task_id"], arguments["workspace_id"]))
    from .. import projects

    return await answered(projects.check_workspace(arguments))
