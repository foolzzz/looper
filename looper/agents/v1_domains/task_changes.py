"""Operator-only task file metadata, without patches or workspace preparation."""

from .. import task_changes
from ..action_levels import ActionLevel
from ..api_v1 import Operation, _array, _object, _STR, _NULL_STR, _BOOL
from ..ops.config import answered


RESULTS = {}
OPERATIONS = (
    Operation("tasks.changes", "POST", "/api/v1/tasks/changes", "Read a task's changed files.",
        "Read a task's changed files. Task branches include net changes since their base; in-place workspaces "
        "include only uncommitted changes. At most 200 entries, with an explicit truncation flag.",
        _object({"task_id": {**_STR, "minLength": 1},
                 "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 100}},
                ["task_id"], closed=True),
        _object({"task_id": _STR, "kind": {**_STR, "enum": ["ok", "unavailable"]},
                 "scope": {**_NULL_STR, "enum": ["task_branch", "working_tree", None]},
                 "files": {**_array(_object({"path": _STR,
                     "status": {**_STR, "enum": ["added", "modified", "deleted", "renamed", "copied",
                                                 "type_changed", "conflicted", "untracked", "unknown"]},
                     "old_path": _NULL_STR}, closed=True)), "maxItems": 200},
                 "truncated": _BOOL, "reason": {**_NULL_STR, "enum": ["not_git", "workspace_missing",
                     "branch_missing", "branch_changed", "git_error", None]}}, closed=True),
        risk="read", operator_only=True,
        # Reads the task's local repository by running read-only git commands.
        level=ActionLevel.MACHINE),
)


async def run(server, operation, arguments, principal, headers):
    return await answered(task_changes.read(arguments["task_id"], limit=arguments.get("limit", 100)))
