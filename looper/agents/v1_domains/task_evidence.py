"""Operator-only acceptance evidence readback, including current check definitions."""

import json

from ...db import step_check_runs, tasks
from ...db.connection import get_pool
from ..action_levels import ActionLevel
from ..api_v1 import Operation, _array, _object, _STR, _NULL_STR, _INT, _BOOL
from ..ops import ErrorCode, OpError
from ..ops.service import _plain
from .task_views import _STEP

_NULL_INT = {"type": ["integer", "null"]}
_ARTIFACT = _object({"name": _STR, "mime": _STR, "bytes": _INT, "sha256": _STR}, closed=True)
_RUN = _object({
    **dict.fromkeys(("id", "attempt", "run", "criterion", "output_bytes"), _INT),
    **dict.fromkeys(("task_id", "step_id", "check_id", "output_tail", "started_at"), _STR),
    **dict.fromkeys(("head_sha", "content_sha", "output_sha256", "finished_at"), _NULL_STR),
    **dict.fromkeys(("exit_code", "duration_ms"), _NULL_INT),
    "type": {"type": "string", "enum": ["command", "artifact"]},
    "state": {"type": "string", "enum": [state.value for state in step_check_runs.CheckState]},
    "error_kind": {"type": ["string", "null"],
                   "enum": [None, *(kind.value for kind in step_check_runs.ErrorKind)]},
    "required": _BOOL, "guarded": _BOOL, "artifacts": _array(_ARTIFACT), "protected_changed": _array(_STR),
}, closed=True)
RESULTS = {}
OPERATIONS = (
    Operation("task_evidence.get", "POST", "/api/v1/task_evidence/get",
        "Read a task's acceptance checks and measured evidence.",
        "Read a task's acceptance checks and measured evidence. Task is its id; optional step is its id or plan label. "
        "Returns definitions and all attempts, including retained history when checks are off. "
        "Artifact metadata contains no storage paths or file contents. Operator only because output can be sensitive.",
        _object({"task": {**_STR, "minLength": 1}, "step": {**_STR, "minLength": 1}}, ["task"], closed=True),
        _object({"task_id": _STR, "steps": _array(_object({
            "step_id": _STR, "step": _STR, "checks": _STEP["properties"]["checks"], "runs": _array(_RUN),
        }, closed=True))}, closed=True), risk="read", operator_only=True,
        level=ActionLevel.INSIDE),  # reads only Looper's stored definitions and measured evidence
)


async def run(server, operation, arguments, principal, headers):
    task_id = arguments["task"]
    pool = get_pool()
    if await tasks.get_task(task_id, pool=pool) is None:
        raise OpError(ErrorCode.NOT_FOUND, "task not found")
    selector = arguments.get("step")
    steps = await pool.fetch(
        "SELECT id, step_key, checks FROM task_steps WHERE task_id=$1 "
        "AND ($2::text IS NULL OR id=$2 OR step_key=$2) ORDER BY step_index", task_id, selector)
    if selector is not None and not steps:
        raise OpError(ErrorCode.NOT_FOUND, "step not found")
    rows = await step_check_runs.list_runs(task_id, steps[0]["id"] if selector is not None and len(steps) == 1 else None)
    result = []
    for step in steps:
        runs = [row for row in rows if row["step_id"] == step["id"]]
        for row in runs:
            # Never expose future internal storage fields through this read operation.
            row["artifacts"] = [{key: item[key] for key in ("name", "mime", "bytes", "sha256")}
                                for item in row["artifacts"]]
        result.append({"step_id": step["id"], "step": step["step_key"],
                       "checks": json.loads(step["checks"]) if isinstance(step["checks"], str) else step["checks"],
                       "runs": runs})
    return _plain({"task_id": task_id, "steps": result})
