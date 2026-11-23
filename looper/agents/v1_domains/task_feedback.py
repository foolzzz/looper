"""The operator's verdict on a completed task's delivery. Only the dashboard and the CLI write it (D21): no Manager
tool, and neither the verdict nor its note reaches an agent or the action trail."""

from ...db import task_feedback, tasks
from ..action_levels import ActionLevel
from ..api_v1 import Operation, _NULL_STR, _STR, _object
from ..ops import ErrorCode, OpError

_VIEW = _object({"task_id": _STR, "verdict": {"enum": [*task_feedback.VERDICTS, None]},
                 "note": _NULL_STR, "created_at": _NULL_STR, "updated_at": _NULL_STR}, closed=True)
_TASK = {"type": "string", "minLength": 1}
RESULTS = {}
OPERATIONS = (
    Operation("task_feedback.get", "POST", "/api/v1/task_feedback/get",
        "Read the delivery verdict marked on a task.",
        "Read the delivery verdict marked on a task. The verdict is null when none is marked.",
        _object({"task_id": _TASK}, closed=True), _VIEW, risk="read", operator_only=True,
        audit_operator=False, level=ActionLevel.INSIDE),  # reads one stored row
    Operation("task_feedback.set", "POST", "/api/v1/task_feedback/set",
        "Mark a completed task's delivery as problematic, or withdraw it with a null verdict.",
        "Mark a completed task's delivery as problematic, or withdraw it with a null verdict. "
        "The verdict is defect, incomplete, wrong_approach or other, with an optional note of up to 500 characters. "
        "Only completed tasks take one. It feeds the quality figures and is shown to the operator only.",
        _object({"task_id": _TASK,
                 "verdict": {"enum": [*task_feedback.VERDICTS, None]},
                 "note": {"type": ["string", "null"], "maxLength": task_feedback.NOTE_MAX_CHARS}},
                ["task_id", "verdict"], closed=True),
        _VIEW, risk="write", operator_only=True, audit_operator=False,
        level=ActionLevel.INSIDE),  # writes one row of Looper's own database
)


def _view(task_id, row):
    if row is None:
        return {"task_id": task_id, "verdict": None, "note": None, "created_at": None, "updated_at": None}
    return {"task_id": task_id, "verdict": row["verdict"], "note": row["note"],
            "created_at": row["created_at"].isoformat(), "updated_at": row["updated_at"].isoformat()}


async def run(server, operation, arguments, principal, headers):
    task_id = arguments["task_id"]
    task = await tasks.get_task(task_id)
    if task is None:
        raise OpError(ErrorCode.NOT_FOUND, f"no task {task_id}")
    if operation.operation_id == "task_feedback.get":
        return _view(task_id, await task_feedback.get(task_id))
    verdict = arguments["verdict"]
    if verdict is None:
        await task_feedback.withdraw(task_id)
        return _view(task_id, None)
    if task["status"] != "completed":
        raise OpError(ErrorCode.CONFLICT, f"task {task_id} is {task['status']}: only a completed task takes a verdict")
    return _view(task_id, await task_feedback.put(task_id, verdict, arguments.get("note") or None))
