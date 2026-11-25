"""Operator-only reads of the task-quality figures (task-metrics RFC, section 7)."""

from .. import metrics
from ..action_levels import ActionLevel
from ..api_v1 import Operation, _OBJ, _STR, _NULL_STR, _INT, _array, _object
from ..ops import ErrorCode, OpError

DIFFICULTIES = ["routine", "hard", "critical"]
RESULTS = {}
OPERATIONS = (
    Operation("metrics.task_outcomes", "POST", "/api/v1/metrics/task_outcomes",
        "Read task success, first-pass, review, cost and duration figures for a window.",
        "Read task success, first-pass, review, cost and duration figures for a window. "
        "The last N days are set against the 4N days before them; a ratio shows from 20 cases with its Wilson "
        "95% interval, else insufficient. An item that cannot be computed returns an error and the others answer. "
        "coverage_since is when records begin (nothing is backfilled); missing_attempts counts Worker runs since "
        "then with no attempt.",
        _object({"days": {"type": "integer", "minimum": 1, "maximum": 90, "default": 7},
                 "project": _STR, "profile": _STR, "difficulty": {"enum": DIFFICULTIES}}, [], closed=True),
        _object({"days": _INT, "window": _OBJ, "comparison_window": _OBJ, "filters": _OBJ,
                 "coverage_since": _NULL_STR, "missing_attempts": {"type": ["integer", "null"]},
                 "tasks": _OBJ, "steps": _OBJ, "reviews": _OBJ, "contract": _OBJ,
                 "failure_handling": _OBJ}, ["days", "window", "comparison_window", "filters", "coverage_since",
                 "missing_attempts", "tasks", "steps", "reviews", "contract"]),
        risk="read", operator_only=True,
        level=ActionLevel.INSIDE),  # reads only Looper's stored attempt and outcome records
    Operation("metrics.step_attempts", "POST", "/api/v1/metrics/step_attempts",
        "Read one task's steps and attempts with what each attempt cost.",
        "Read one task's steps and attempts with what each attempt cost. Costs are split first attempt and retry, "
        "Worker, review and advisor, per billing basis; planning and spend no attempt owns are listed apart and "
        "everything adds up to the task's cost. An unknown cost is null, never 0.",
        _object({"task_id": {"type": "string", "minLength": 1}}, closed=True),
        _object({"task_id": _STR, "status": _STR, "run_no": _INT, "coverage_since": _NULL_STR,
                 "missing_attempts": _INT, "steps": _array(), "summary": _OBJ, "costs": _OBJ}),
        risk="read", operator_only=True,
        level=ActionLevel.INSIDE),  # reads only Looper's stored attempt and run records
)


async def run(server, operation, arguments, principal, headers):
    if operation.operation_id == "metrics.step_attempts":
        view = await metrics.task_attempts(arguments["task_id"])
        if view is None:
            raise OpError(ErrorCode.NOT_FOUND, f"no task {arguments['task_id']}")
        return view
    return await metrics.compute(arguments.get("days", 7), metrics.Filters(
        arguments.get("project"), arguments.get("profile"), arguments.get("difficulty")))
