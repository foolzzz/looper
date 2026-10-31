"""Full schedule read projections and the existing compact Tool results."""

from ...db import schedules as db_schedules
from .. import schedules
from ..api_v1 import Operation, _array, _object, _STR, _NULL_STR, _INT, _BOOL
from ..ops.config import answered
from ..ops.service import _plain

ORDER = 11


def _enum(*values):
    return {"type": "string", "enum": list(values)}


def _nullable(schema):
    return {"anyOf": [schema, {"type": "null"}]}


_NULL_INT = {"type": ["integer", "null"]}
_NULL_NUMBER = {"type": ["number", "null"]}
_RUN_STATUS = _enum(*dict.fromkeys((*db_schedules.OPEN_STATUSES, *db_schedules.SUCCESS,
                                   *db_schedules.FAILURES, *db_schedules.SKIPPED, "stopped", "cancelled")))
RUN = _object({
    **dict.fromkeys(("run_id", "schedule_id", "manager_id", "scheduled_for", "trigger", "status"), _STR),
    "missed": _INT, "firings": _INT, "kind": _enum("agent", "command"),
    **dict.fromkeys(("fired_at", "task_id", "task_status", "task_title", "summary", "error", "finished_at",
                     "gate_output", "carried_from", "started_at", "log_tail"), _NULL_STR),
    "cost_usd": _NULL_NUMBER, "exit_code": _NULL_INT, "exit_signal": _NULL_INT,
    "duration_seconds": _NULL_NUMBER,
    "log": _nullable(_object({"path": _STR, "bytes": _NULL_INT, "truncated": _BOOL}))})
_SCHEDULE_FIELDS = {
    **dict.fromkeys(("schedule_id", "project_id", "project_name", "manager_id", "name", "cron", "timezone", "description",
                     "requirement", "work_dir", "created_via", "created_at", "updated_at"), _STR),
    "manager_name": _NULL_STR, "template_key": _NULL_STR, "template_name": _NULL_STR,
    "kind": _enum("agent", "command"), "command": _NULL_STR,
    "env": {"type": "object", "additionalProperties": _STR},
    "env_file": _NULL_STR, "success_deadline": _NULL_STR,
    "alert_policy": _object({
        "failures_after": _object({"value": _INT, "default": _BOOL, "source": _enum("default", "schedule")}),
        "stale_after_seconds": _object({"value": _INT, "default": _BOOL, "source": _enum("default", "schedule")}),
        "overlap_after": _object({"value": _INT, "default": _BOOL, "source": _enum("default", "schedule")})}),
    "template_inputs": {"type": "object", "additionalProperties": _STR},
    "enabled": _BOOL, "overlap": _enum("skip", "queue", "parallel"), "catch_up": _enum("once", "skip"),
    "notify": _enum("failures", "alerts", "always", "never"),
    "user_gates": _enum("off", "on"),
    "max_parallel_runs": _INT, "consecutive_failures": _INT,
    **dict.fromkeys(("next_fire_at", "last_fired_at", "last_success_at"), _NULL_STR),
    "run_timeout_seconds": {"anyOf": [_NULL_INT, {"const": "default"}]},
    "run_timeout_effective_seconds": _NULL_INT, "run_budget_usd": _NULL_NUMBER,
    "daily_budget_usd": _NULL_NUMBER, "spent_today_usd": {"type": "number"},
    "gate_command": _NULL_STR, "gate_timeout_seconds": _NULL_INT,
    "profile_overrides": _object(dict.fromkeys(("planner", "worker", "reviewer"), _STR), [], closed=True),
    "task_limits": _object({"worker": _NULL_INT, "reviewer": _NULL_INT}),
    "notify_target": _nullable(_object({"channel": _STR, "chat": _STR})),
    "alerts": _object(dict.fromkeys(("failures_since", "stale_since", "deadline_since", "overlap_since"), _NULL_STR)),
    "planner_session": _object({"active": _BOOL, **dict.fromkeys(
        ("runtime", "profile", "last_task_id", "last_task_title", "updated_at"), _NULL_STR)})}
SCHEDULE_WITHOUT_LAST_RUN = _object({key: value for key, value in _SCHEDULE_FIELDS.items()
                                    if key not in {"manager_id", "manager_name"}})
SCHEDULE = _object({**_SCHEDULE_FIELDS, "last_run": _nullable(RUN)})
SCHEDULE_LIMITS = _object({
    **dict.fromkeys(("name_max_chars", "requirement_max_chars", "min_interval_seconds",
                     "min_interval_gated_seconds", "run_timeout_max_seconds", "gate_command_max_chars",
                     "gate_timeout_default_seconds", "gate_timeout_max_seconds", "max_parallel_runs_limit",
                     "concurrency_max"), _INT),
    "run_timeout_default_seconds": _NULL_INT, "default_timezone": _STR, "roles": _array(_STR),
    "notify_targets": _array(_object({"channel": _STR, "chat": _STR, "label": _STR, "channel_enabled": _BOOL}))})
PREVIEW = _object({"cron": _STR, "timezone": _STR, "min_interval_seconds": _INT, "valid": _BOOL,
                   "description": _NULL_STR, "next": _array(_STR), "error": _NULL_STR})
ROUTINE_SKIPS = _object({"skipped_gate": _INT, "skipped_overlap": _INT})
_RUN_STATS = _object({**dict.fromkeys(("runs", "succeeded", "failed"), _INT),
                      **dict.fromkeys(("success_rate", "avg_seconds", "max_seconds"), _NULL_NUMBER)})
RUNS_PAGE = _object({"runs": _array(RUN), "next_before": _NULL_STR, "routine_skips": ROUTINE_SKIPS,
                     "stats": _object({"day": _RUN_STATS, "week": _RUN_STATS})})
RUN_LOG = _object({"run_id": _STR, "schedule_id": _STR, "size": _INT, "offset": _INT,
                   "next_offset": _NULL_INT, "data_base64": _STR, "complete": _BOOL, "truncated": _BOOL})
NOTIFICATION = _object({
    **dict.fromkeys(("notification_id", "text", "created_at"), _STR),
    **dict.fromkeys(("run_id", "channel", "chat", "error"), _NULL_STR),
    "kind": _enum("run", "failures_alert", "failures_recovered", "stale_alert", "stale_recovered",
                  "deadline_alert", "deadline_recovered", "overlap_alert", "overlap_recovered"),
    "status": _enum("sent", "failed", "dashboard")})
LISTED_RUN = _object({
    **dict.fromkeys(("run_id", "status", "trigger"), _STR),
    **dict.fromkeys(("at", "task_id", "summary"), _NULL_STR), "cost_usd": _NULL_NUMBER, "firings": _INT,
    "exit_code": _NULL_INT, "kind": _enum("agent", "command")},
    ["run_id", "status", "trigger", "at", "task_id", "summary", "cost_usd", "exit_code", "kind"])
LISTED_SCHEDULE = _object({
    **dict.fromkeys(("schedule_id", "name", "project", "cron", "timezone", "description"), _STR),
    "user_gates": _enum("off", "on"), "kind": _enum("agent", "command"),
    "enabled": _BOOL, "next_fire_at": _NULL_STR, "consecutive_failures": _INT, "last_run": _nullable(LISTED_RUN)})
RESULTS = {
    "schedules.list": _object({"schedules": _array(LISTED_SCHEDULE)}),
    "schedules.get": _object({"schedule": SCHEDULE_WITHOUT_LAST_RUN, "latest_runs": _array(LISTED_RUN),
                               "routine_skips": ROUTINE_SKIPS})}


def _op(action, fields, result, required=None):
    description = f"Read schedules: {action.replace('_', ' ')}."
    return Operation(f"schedule_admin.{action}", "POST", f"/api/v1/schedule_admin/{action}",
                     description, description, _object(fields, required, closed=True), result, risk="read")


_ID = {"schedule_id": _STR}
_LIMIT = {"type": "integer", "minimum": 1, "maximum": 200}
OPERATIONS = (
    _op("list", {}, _object({"schedules": _array(SCHEDULE)})),
    _op("project_list", {"project_id": _STR}, _object({"schedules": _array(SCHEDULE), "limits": SCHEDULE_LIMITS})),
    _op("get", _ID, _object({"schedule": SCHEDULE})),
    _op("preview", {"cron": _STR, "timezone": _STR, "count": {**_LIMIT, "maximum": 20}, "gated": _BOOL},
        PREVIEW, ["cron"]),
    _op("runs", {**_ID, "limit": _LIMIT, "before": _NULL_STR, "skips": _enum("show", "hide"), "status": _RUN_STATUS},
        RUNS_PAGE, ["schedule_id"]),
    _op("run_log", {"run_id": _STR, "offset": {"type": "integer", "minimum": 0},
                    "limit_bytes": {"type": "integer", "minimum": 1, "maximum": 4 * 1024 * 1024},
                    "tail_lines": {"type": "integer", "minimum": 1, "maximum": 5000}}, RUN_LOG, ["run_id"]),
    _op("notifications", {**_ID, "limit": _LIMIT}, _object({"notifications": _array(NOTIFICATION)}), ["schedule_id"]),
)


async def run(server, operation, arguments, principal, headers):
    action = operation.operation_id.removeprefix("schedule_admin.")
    if action == "preview":
        return _plain(schedules.preview(arguments))
    if action == "list":
        result = await answered(schedules.list_all_schedules(runtime_root=server._runtime_root))
    elif action == "project_list":
        result = await answered(schedules.list_schedules(arguments["project_id"], runtime_root=server._runtime_root))
    elif action == "get":
        result = await answered(schedules.get_schedule(arguments["schedule_id"], runtime_root=server._runtime_root))
    elif action == "run_log":
        result = await answered(schedules.run_log(arguments["run_id"], arguments, server._runtime_root))
    else:
        # The shared service accepts query-string values; v1 validates JSON types before adapting them.
        query = {key: str(value) for key, value in arguments.items() if key != "schedule_id" and value is not None}
        reader = schedules.list_runs if action == "runs" else schedules.list_notifications
        result = await answered(reader(arguments["schedule_id"], query,
                                       **({"runtime_root": server._runtime_root} if action == "runs" else {})))
    return _plain(result)
