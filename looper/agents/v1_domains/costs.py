"""Result contracts for the existing task, project and global costs Tool."""

from ..api_v1 import _array, _object, _STR, _NULL_STR, _INT

_NUMBER = {"type": "number"}

TASK_MODEL_COST = _object({
    "model": _NULL_STR, "cost_usd": {"type": ["number", "null"]},
    **{key: _INT for key in ("runs", "running", "unpriced_runs", "interrupted_runs", "input_tokens",
                             "output_tokens", "cache_read_tokens", "cache_creation_tokens")}}, closed=True)

TASK_COST = _object({
    "cost_usd": _NUMBER, "runs": _INT, "running": _INT, "unpriced_runs": _INT,
    "equivalent_runs": _INT, "provider_price_runs": _INT,
    "by_role": _object({role: _object({"cost_usd": _NUMBER, "runs": _INT}, closed=True)
                        for role in ("planner", "worker", "reviewer")}, closed=True),
    "by_model": _array(TASK_MODEL_COST), "manager_by_model": _array(TASK_MODEL_COST)},
    ["cost_usd", "runs", "running", "unpriced_runs", "by_role", "by_model", "manager_by_model"], closed=True)

SPEND_SUMMARY = _object({
    "provider_priced_usd": _NUMBER, "equivalent_usd": _NUMBER,
    "runs": _INT, "running": _INT, "unpriced_runs": _INT}, closed=True)

_TASK = _object({"task_id": _STR, "name": _STR, "status": _STR, "cost": TASK_COST, "note": _STR}, closed=True)
_RANGE = {"since": _NULL_STR, "until": _NULL_STR, "total": SPEND_SUMMARY, "note": _STR}
_PROJECT = _object({**_RANGE, "project": _STR,
    "tasks": _array(_object({"task_id": _STR, "name": _STR, "provider_priced_usd": _NUMBER,
                             "equivalent_usd": _NUMBER, "runs": _INT}, closed=True)),
    "more_tasks": _INT}, closed=True)
_GLOBAL = _object({**_RANGE,
    "projects": _array(_object({"project": _NULL_STR, "provider_priced_usd": _NUMBER,
                                "equivalent_usd": _NUMBER, "runs": _INT, "tasks": _INT}, closed=True)),
    "note_projects": _STR}, closed=True)

RESULTS = {"costs": {"oneOf": [_TASK, _PROJECT, _GLOBAL]}}
OPERATIONS = ()
