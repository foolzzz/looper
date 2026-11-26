"""Full dashboard projections beside the unchanged compact task Tools."""

from .. import overview, projects
from ..run_usage import CostStatus
from ..api_v1 import Operation, STATUS, _array, _object, _STR, _NULL_STR, _INT, _BOOL
from ..ops import ErrorCode, OpError
from ..system_prompt import TASK_REQUIREMENT_LIMIT
from ...db import task_changes

ORDER = 2

_NUM = {"type": "number"}
_LOAD = _object({"running": _INT, "waiting": _INT})
_PLANNER = _object({"agent_id": _STR, "task_id": _STR, "project_id": _NULL_STR,
                    "project_name": _NULL_STR, "requirement": _STR, "name": _STR})
_TASK_ROLE = _object({"id": _STR, "kind": {"type": "string", "enum": ["work", "review"]}, "version": _INT})
_RUN = _object({
    "agent_id": _STR, "role": {"type": "string", "enum": ["worker", "reviewer"]}, "task_id": _STR,
    "project_id": _NULL_STR, "project_name": _NULL_STR, "task_name": _STR, "step": _STR, "instruction": _STR,
    "state": {"type": "string", "enum": ["running", "waiting"]}, "elapsed_seconds": _NUM,
    "task_role": _TASK_ROLE},
    ["agent_id", "role", "task_id", "task_name", "project_id", "project_name", "step", "instruction", "state", "elapsed_seconds"])
_RATE = {kind: _INT for kind in ("total", "input", "output", "cache_read", "cache_creation")}
_PROCESS = _object({
    "agent_id": _STR, "role": {"type": "string", "enum": ["manager", "planner", "worker", "reviewer", "consult",
                                                      "processor"]},
    "name": _NULL_STR, "task_id": _NULL_STR, "task_name": _NULL_STR, "project_id": _NULL_STR, "project_name": _NULL_STR,
    "step": _NULL_STR, "instruction": _NULL_STR, "task_role": _TASK_ROLE, "runtime": _NULL_STR, "model": _NULL_STR,
    "pid": {"type": ["integer", "null"]}, "state": {"type": "string", "enum": ["running", "waiting"]},
    "elapsed_seconds": _NUM, "tpm": _object(_RATE) | {"type": ["object", "null"]}},
    ["agent_id", "role", "name", "task_id", "task_name", "project_id", "project_name", "step", "instruction",
     "runtime", "model", "pid", "state", "elapsed_seconds", "tpm"])
_SCHEDULE = {"schedule_id": _STR, "name": _STR, "project_id": _STR, "project_name": _STR}
_OVERVIEW = _object({
    "version": _STR,
    "tasks": _object({**{group: _INT for group in (
        "needs_you", "in_progress", "ready", "attention", "completed", "cancelled", "other", "total")},
        "other_statuses": {"type": "object", "additionalProperties": _INT}}),
    "agents": _object({
        "manager": _object({"agent_id": _STR, "state": {"type": "string", "enum": ["busy", "idle"]}}) | {"type": ["object", "null"]},
        "managers": _array(_object({"agent_id": _STR, "name": _STR, "state": {"type": "string", "enum": ["busy", "idle", "stopped"]}})),
        "planners": _array(_PLANNER), "workers": _array(_RUN), "reviewers": _array(_RUN)},
        ["manager", "planners", "workers", "reviewers"]),
    "processes": _array(_PROCESS),
    "tpm": _object({**_RATE, "window_seconds": _INT}),
    "projects": _array(_object({"project_id": _STR, "name": _STR,
        "tasks": _object({"running": _INT, "ready": _INT}), "workers": _LOAD, "reviewers": _LOAD})),
    "idle_projects": _INT,
    "spend_today": _object({"since": _STR, "provider_priced_usd": _NUM, "equivalent_usd": _NUM,
                            "runs": _INT, "running": _INT, "unpriced_runs": _INT,
                            "cost_status": {"type": "string", "enum": [state.value for state in CostStatus]}},
                           ["since", "provider_priced_usd", "equivalent_usd", "runs", "running", "unpriced_runs"]),
    "health": _object({"unhealthy": _array(_object({"name": _STR,
        "state": {"type": "string", "enum": ["restarting", "crash_looping"]}, "up": _BOOL, "restart_count": _INT,
        "last_crash_at": _NULL_STR, "next_restart_at": _NULL_STR}))}),
    "schedules": _object({"total": _INT, "enabled": _INT,
        "next": _array(_object({**_SCHEDULE, "next_fire_at": _STR, "timezone": _STR})),
        "failing": _array(_object({**_SCHEDULE, "timezone": _STR, "consecutive_failures": _INT,
            "failures_since": _NULL_STR, "stale_since": _NULL_STR, "deadline_since": _NULL_STR,
            "overlap_since": _NULL_STR, "last_success_at": _NULL_STR,
            "last_status": {"type": ["string", "null"], "enum": [None, "queued", "gating", "running", "completed",
                "succeeded", "failed", "stopped", "cancelled", "timed_out", "interrupted", "skipped_overlap",
                "skipped_missed", "skipped_gate", "gate_failed", "skipped_budget", "skipped_busy"]}})),
        "skipped_today": _object({reason: _INT for reason in (
            "skipped_gate", "skipped_overlap", "skipped_missed", "skipped_budget", "skipped_busy")})})})

# Existing Tool result contracts belong here; their model-facing schemas stay untouched.
RESULTS = {
    **{f"task_control.{action}": _object({"task_id": _STR, flag: _BOOL, **({"note": _STR} if note else {})})
       for action, flag, note in (("start", "started", False), ("stop", "stopped", False),
                                 ("restart", "restarted", True), ("start_over", "started_over", True),
                                 ("cancel", "cancelled", False))},
    "task_update": _object({"task_id": _STR, "title": _NULL_STR,
        "requirement_version": {"type": ["integer", "null"]}, "changed": _array(_STR),
        "profiles_note": _STR, "requirement_note": _STR}, ["task_id", "changed"]),
}
OPERATIONS = (
    Operation("system.overview", "POST", "/api/v1/system/overview", "Read the full dashboard overview.",
              "Read the full dashboard overview.", _object({}, closed=True), _OVERVIEW, risk="read"),
    Operation("tasks.requirement_update", "POST", "/api/v1/tasks/requirement_update", "Edit a task's requirement.",
              "Edit a task's requirement. Keep its version and notify its Planner once. "
              "Optional expected_version refuses a stale edit with requirement_changed (409) and current_version.",
              _object({"task_id": {"type": "string", "minLength": 1},
                       "requirement": {"type": "string", "minLength": 1, "maxLength": TASK_REQUIREMENT_LIMIT},
                       "expected_version": _INT}, ["task_id", "requirement"], closed=True),
              _object({"task_id": _STR, "version": _INT, "requirement": _STR}), risk="write", confirms=True),
)


async def run(server, operation, arguments, principal, headers):
    if operation.operation_id == "system.overview":
        from ..http_server import _serialize

        result = await overview.build_overview(task_router=server._task_router, worker_pool=server._worker_pool,
                                               manager=server._manager, supervisor=server._supervisor,
                                               managers=server._managers)
        return _serialize(result)
    try:
        return await projects.edit_requirement(
            arguments["task_id"], arguments, task_router=server._task_router, bus=server._supervisor.bus,
            source=server._web_adapter._address, via=f"client:{principal.client_name or 'operator'}")
    except task_changes.RequirementChanged as exc:
        raise OpError(ErrorCode.REQUIREMENT_CHANGED, str(exc),
                      details={"current_version": exc.current_version}) from None
    except projects.RequestError as exc:
        code = next((code for code, status in STATUS.items() if status == exc.status), ErrorCode.INVALID)
        raise OpError(code, str(exc)) from None
