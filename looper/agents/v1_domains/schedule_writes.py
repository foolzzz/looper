"""Schedule administration through the shared services and scheduler."""

from ...db import connection as db_connection, projects as db_projects, schedules as db_schedules
from .. import schedules
from ..agent_settings import BUDGET_MAX_USD, CONCURRENCY_MAX
from ..api_v1 import Operation, _array, _object, _STR, _NULL_STR, _BOOL
from ..ops import ErrorCode, OpError
from ..ops.config import answered
from ..ops.schedules import CommandEdit, _command_edit, _raises_a_cap, _shown
from ..ops.service import _plain
from ..schedule_templates import MissingDirectory, effective_schedule
from .schedule_reads import SCHEDULE, RUN, LISTED_SCHEDULE, LISTED_RUN, _enum, _nullable

ORDER = 12

EDIT_EFFECT = {
    CommandEdit.ENABLED: ("It is enabled: its later runs execute the changed command unattended, "
                          "without an agent or model spend."),
    CommandEdit.QUEUED: ("It is disabled but has a queued run, which executes the changed command when it starts, "
                         "without an agent or model spend."),
}

_BUDGET = _nullable({"type": "number", "exclusiveMinimum": 0, "maximum": BUDGET_MAX_USD})
_FIELDS = {
    "kind": _enum("agent", "command"),
    "name": {"type": "string", "maxLength": schedules.NAME_MAX_CHARS}, "cron": _STR,
    "requirement": _STR, "timezone": _STR, "work_dir": _STR,
    "command": _nullable({"type": "string", "maxLength": schedules.GATE_COMMAND_MAX_CHARS}),
    "env": _nullable({"type": "object", "additionalProperties": {"type": "string", "maxLength": 4096},
                      "maxProperties": 64}),
    "env_file": _nullable({"type": "string", "maxLength": 1024}),
    "success_deadline": _nullable({"type": "string", "pattern": r"^([01][0-9]|2[0-3]):[0-5][0-9]$"}),
    "alert_failures_after": _nullable({"type": "integer", "minimum": 1, "maximum": 1000}),
    "alert_stale_after_seconds": _nullable({"type": "integer", "minimum": 300, "maximum": 2592000}),
    "alert_overlap_after": _nullable({"type": "integer", "minimum": 0, "maximum": 10000}),
    "template_key": _nullable({"type": "string", "pattern": "^[a-z][a-z0-9_-]{0,31}$"}),
    "template_inputs": {"type": "object", "additionalProperties": {"type": "string", "maxLength": 2000}},
    "overlap": _enum("skip", "queue", "parallel"), "catch_up": _enum("once", "skip"),
    "max_parallel_runs": {"type": "integer", "minimum": 1, "maximum": schedules.MAX_PARALLEL_RUNS_LIMIT},
    "run_timeout_seconds": {"anyOf": [
        _nullable({"type": "integer", "minimum": 1, "maximum": schedules.RUN_TIMEOUT_MAX_SECONDS}),
        {"const": "default"}]},
    "run_budget_usd": _BUDGET, "daily_budget_usd": _BUDGET, "gate_command": _NULL_STR,
    "gate_timeout_seconds": _nullable({"type": "integer", "minimum": 1,
                                        "maximum": schedules.GATE_TIMEOUT_MAX_SECONDS}),
    "notify": _enum("failures", "alerts", "always", "never"),
    "user_gates": _enum("off", "on"),
    "notify_target": _nullable(_object({"channel": _STR, "chat": _STR}, closed=True)),
    "profile_overrides": _nullable(_object(dict.fromkeys(("planner", "worker", "reviewer"), _NULL_STR),
                                            [], closed=True)),
    "task_limits": _nullable(_object(dict.fromkeys(("worker", "reviewer"), _nullable({
        "type": "integer", "minimum": 1, "maximum": CONCURRENCY_MAX})), [], closed=True)),
}
SCHEDULE_CREATE = _object({**_FIELDS,
                           "manager_id": {"type": "string", "minLength": 1}}, ["name", "cron"], closed=True)
SCHEDULE_PATCH = {**_object({**_FIELDS, "enabled": _BOOL}, [], closed=True),
                  "anyOf": [{"required": [key]} for key in (*_FIELDS, "enabled")]}
SCHEMAS = {"ScheduleCreate": SCHEDULE_CREATE, "SchedulePatch": SCHEDULE_PATCH}
RESULTS = {
    "schedules.create": _object({"schedule": LISTED_SCHEDULE, "enabled": _BOOL}),
    "schedules.update": _object({"schedule": LISTED_SCHEDULE, "changed": _array(_STR)}),
    **{f"schedules.{action}": _object({"schedule": LISTED_SCHEDULE, "note": _STR}, ["schedule"])
       for action in ("enable", "disable")},
    "schedules.run": _object({"schedule": _STR, "project": _STR, "run": LISTED_RUN, "note": _STR}),
    "schedules.delete": _object({"deleted": _STR, "schedule_id": _STR, "project": _STR, "note": _STR}),
    "schedules.reset_planner_session": _object({"schedule": _STR, "schedule_id": _STR,
                                               "project": _STR, "cleared": _BOOL, "note": _STR}),
}


def _op(action, fields, result, *, risk="write"):
    description = f"Change schedules: {action.replace('_', ' ')}."
    return Operation(f"schedule_admin.{action}", "POST", f"/api/v1/schedule_admin/{action}",
                     description, description, _object(fields, closed=True), result,
                     risk=risk, confirms=action != "create")


_ID = {"schedule_id": _STR}
OPERATIONS = (
    _op("create", {"project_id": _STR, "fields": SCHEDULE_CREATE}, _object({"schedule": SCHEDULE})),
    _op("update", {**_ID, "fields": SCHEDULE_PATCH}, _object({"schedule": SCHEDULE})),
    _op("delete", _ID, _object({"deleted": _STR}), risk="high"),
    _op("reset_planner_session", _ID, _object({"cleared": _BOOL}), risk="high"),
    _op("run", _ID, _object({"run": RUN}), risk="high"),
)


async def impact(server, operation, arguments, principal, headers):
    action = operation.operation_id.removeprefix("schedule_admin.")
    if action == "create":
        return None
    if action == "run" and server._scheduler is None:
        raise OpError(ErrorCode.CONFLICT, "the scheduler is not running in this server")
    row = await answered(schedules._existing(arguments["schedule_id"]))
    project = await db_projects.get_project(row["project_id"])
    if project is None:
        raise OpError(ErrorCode.NOT_FOUND, "the schedule's project no longer exists")
    project_dir = project["work_dir"]

    def directory(view) -> str:
        return view["work_dir"] or f"the project directory ({project_dir or 'none set'})"

    if action == "run":
        # Bind the displayed read; the closed request cannot supply this server-owned version.
        arguments["_schedule_version"] = db_schedules.configuration_version(row, project_work_dir=project_dir)
    subject = f"schedule {row['name']!r} in project {row['project_name']!r}"
    if action == "delete":
        if row["kind"] == "command":
            runs = await db_schedules.open_runs(db_connection.get_pool(), row["id"])
            if schedules.blocks_delete(row, runs):
                raise OpError(ErrorCode.CONFLICT, schedules.RUNNING_COMMAND_DELETE)
            queued = " The queued run will not execute." if any(run["status"] == db_schedules.QUEUED for run in runs) else ""
            return f"Delete {subject}, its runs and notifications.{queued} It has no tasks."
        return f"Delete {subject}, its runs and notifications. Its created tasks stay."
    if action == "run":
        if row["kind"] == "command":
            return (f"Run {subject}'s shell command once now, without an agent or model spend: "
                    f"{_shown('command', row, {})} in {directory(row)}. Its next fire stays.")
        return f"Run {subject} once now through its overlap check, spending within its caps. Its next fire stays."
    if action == "reset_planner_session":
        return (f"Reset the stored Planner session of {subject}. The next run starts a new session; "
                "an active run keeps its session and cannot write it back.")
    data = arguments["fields"]
    if not data:
        raise OpError(ErrorCode.INVALID, "give at least one field to change")
    if "kind" in data and data["kind"] != row.get("kind", "agent"):
        raise OpError(ErrorCode.INVALID, "a schedule's kind cannot change; create a new schedule")
    # Normalize with the same validators as execution before deciding whether a cap rises.
    command = row.get("kind", "agent") == "command"
    fields, profiles, _ = await answered(schedules._fields(data, update=True, resolve_profiles=not command))
    gated = bool(fields.get("gate_command", row["gate_command"])) or command
    try:
        if command:
            schedules._check_kind_fields("command", {**row, **fields,
                "profile_overrides": profiles if "profile_overrides" in data else row.get("profile_overrides"),
                **{key: data[key] for key in ("template_inputs", "task_limits") if key in data}})
        schedules._check_rule(fields.get("cron", row["cron"]), fields.get("timezone", row["timezone"]),
                              schedules._utcnow(), gated=gated,
                              removes_gate=not command and bool(row["gate_command"]) and not gated)
    except schedules.RequestError as exc:
        raise OpError(ErrorCode.INVALID, str(exc)) from None
    arguments["_schedule_version"] = db_schedules.configuration_version(
        row, project_work_dir=project_dir, changes=fields)
    edit = await _command_edit(row, fields)
    if edit is not None:
        try:
            await effective_schedule(None, {**row, **fields}, enabled=data.get("enabled", row["enabled"]))
        except ValueError as exc:
            raise OpError(ErrorCode.INVALID if isinstance(exc, MissingDirectory) else ErrorCode.CONFLICT, str(exc)) from None
    raises = _raises_a_cap(row, fields, project["task_budget_usd"] if project else None)
    if edit is not None and not (data.get("enabled") and not row["enabled"]):
        lines = []
        for key in edit[1]:
            if key == "command":
                lines.append(f"command: {_shown('command', row, {})} → {_shown('command', {**row, **fields}, {})}")
            elif key == "env":
                old, new = row["env"] or {}, fields["env"] or {}
                line = f"env keys: {', '.join(sorted(old)) or 'none'} → {', '.join(sorted(new)) or 'none'}"
                changed = sorted(k for k in old.keys() & new.keys() if old[k] != new[k])
                if changed:
                    line += f" (values changed: {', '.join(changed)})"
                lines.append(line)
            elif key == "env_file":
                lines.append(f"env_file: {row['env_file'] or 'none'} → {fields['env_file'] or 'none'}")
            elif key == "work_dir":
                lines.append(f"directory: {directory(row)} → {directory({**row, **fields})}")
        if "work_dir" not in edit[1]:
            lines.append(f"directory: {directory({**row, **fields})}")
        lines.extend(f"{key}: {row[key]!r} → {data[key]!r}" for key in
                     ("enabled", "run_budget_usd", "daily_budget_usd") if key in data and data[key] != row[key])
        return f"Update {subject}: {'; '.join(lines)}. {EDIT_EFFECT[edit[0]]}"
    if (data.get("enabled") and not row["enabled"]) or raises:
        changes = ", ".join(f"{key}: {row[key]!r} → {data[key]!r}" for key in
                            ("enabled", "run_budget_usd", "daily_budget_usd") if key in data)
        effect = (f"Enabling runs its shell command {_shown('command', {**row, **fields}, {})} "
                  f"in {directory({**row, **fields})} unattended at each fire, without an agent or model spend." if command else
                  "Enabling starts unattended scheduled tasks; raised or removed caps allow more spending.")
        return f"Update {subject}: {changes}. {effect}"
    return None


async def run(server, operation, arguments, principal, headers):
    action = operation.operation_id.removeprefix("schedule_admin.")
    if action == "create":
        result = await answered(schedules.create_schedule(arguments["project_id"], arguments["fields"],
                                                          runtime_root=server._runtime_root))
    elif action == "update":
        result = await answered(schedules.update_schedule(arguments["schedule_id"], arguments["fields"],
                                                          runtime_root=server._runtime_root,
                                                          expected_version=arguments.get("_schedule_version")))
    elif action == "run":
        if server._scheduler is None:
            raise OpError(ErrorCode.CONFLICT, "the scheduler is not running in this server")
        result = await answered(schedules.run_now(arguments["schedule_id"], server._scheduler,
                                                  runtime_root=server._runtime_root,
                                                  expected_version=arguments.get("_schedule_version")))
    else:
        service = schedules.delete_schedule if action == "delete" else schedules.reset_planner_session
        result = await answered(service(arguments["schedule_id"]))
    return _plain(result)
