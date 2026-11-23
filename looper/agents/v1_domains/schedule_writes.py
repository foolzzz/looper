"""Schedule administration through the shared services and scheduler."""

from datetime import datetime, timezone

from ...db import connection as db_connection, projects as db_projects, schedules as db_schedules
from ...db import subscriptions as db_subs
from .. import schedules, subscriptions
from ..agent_settings import BUDGET_MAX_USD, CONCURRENCY_MAX
from ..action_levels import ActionLevel, schedule_level, schedule_reach
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

_SUBSCRIPTION = _object({
    "window": _enum("cursor", "lookback"), "delivery_policy": _enum("per_run", "daily"),
    "source_id": _STR, "chat_kinds": _array(_enum("p2p", "group", "topic", "unknown")),
    "sender_kinds": _array(_enum("user", "app")), "include_chats": _array(_STR), "exclude_chats": _array(_STR),
    "exclude_mentions_me": _BOOL, "include_history": _BOOL,
    "max_age_minutes": {"type": "integer", "minimum": 5, "maximum": 10080},
    "max_events": {"type": "integer", "minimum": 1, "maximum": 2000},
    "text_max_chars": {"type": "integer", "minimum": 20, "maximum": 4000},
    "quiet_word": {"type": "string", "minLength": 1, "maxLength": 40},
    "output_header": {"type": "string", "maxLength": subscriptions.HEADER_MAX_CHARS},
    "output_kind": _enum(*subscriptions.OUTPUT_KINDS), "render": _enum("lines_v1", "lines_v2"),
    "check_items": _BOOL,
    "rules": _array(_object({
        "name": _STR, "label": _STR, "chat_kinds": _array(_enum("p2p", "group", "topic", "unknown")),
        "include_chats": _array(_STR), "exclude_chats": _array(_STR), "sender_kinds": _array(_enum("user", "app")),
        "exclude_self": _BOOL, "mentions_me": _enum("any", "only", "exclude"), "text_match": _STR,
        "never_quiet": _BOOL, "areas": {"type": "object", "additionalProperties": _STR}}, ["name"], closed=True))},
    [], closed=True)
_BUDGET = _nullable({"type": "number", "exclusiveMinimum": 0, "maximum": BUDGET_MAX_USD})
_FIELDS = {
    "kind": _enum("agent", "command", "subscription"), "subscription": _SUBSCRIPTION,
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
    "notify_format": _enum("report", "deliverable"),
    "user_gates": _enum("off", "on"),
    "active_hours": _nullable({"type": "string",
                               "pattern": r"^([01][0-9]|2[0-3]):[0-5][0-9]-([01][0-9]|2[0-3]):[0-5][0-9]$"}),
    "idle_only": _nullable({"type": "boolean"}),
    "notify_target": _nullable(_object({"channel": _STR, "chat": _STR}, closed=True)),
    "profile_overrides": _nullable(_object(dict.fromkeys(("planner", "worker", "reviewer", "processor"), _NULL_STR),
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


async def _level(arguments):
    """Where the schedule reaches (``action_levels.schedule_level``; an API client's call has no chat of its own): a
    new one by its fields, a stored one by what it has with the change merged; one that is not there is
    ``unknown``."""
    fields = arguments.get("fields") if isinstance(arguments.get("fields"), dict) else {}
    if "schedule_id" in arguments:
        row = await db_schedules.get_schedule(arguments["schedule_id"])
        if row is None:
            return ActionLevel.UNKNOWN
        fields = {**schedule_reach(row), **fields}
    return await schedule_level(fields)


def _op(action, fields, result, *, risk="write", required=None):
    description = f"Change schedules: {action.replace('_', ' ')}."
    return Operation(f"schedule_admin.{action}", "POST", f"/api/v1/schedule_admin/{action}",
                     description, description, _object(fields, required, closed=True), result,
                     risk=risk, confirms=action != "create",
                     # running a schedule, or confirming its push target, reaches where it does (``_level``)
                     level_of={"create": _level, "update": _level, "run": _level, "confirm_target": _level}.get(action))


_ID = {"schedule_id": _STR}
OPERATIONS = (
    _op("create", {"project_id": _STR, "fields": SCHEDULE_CREATE}, _object({"schedule": SCHEDULE})),
    _op("update", {**_ID, "fields": SCHEDULE_PATCH}, _object({"schedule": SCHEDULE})),
    _op("delete", _ID, _object({"deleted": _STR}), risk="high"),
    _op("reset_planner_session", _ID, _object({"cleared": _BOOL}), risk="high"),
    _op("run", _ID, _object({"run": RUN}), risk="high"),
    # A subscription's push target, shown to the operator and confirmed as it is now; and its cursor, moved to just
    # before the first event its source ingested after a time (the switch from an outside collector).
    _op("confirm_target", {**_ID, "config_version": _STR}, _object({"schedule": SCHEDULE}), risk="high",
        required=["schedule_id"]),
    _op("reset_cursor", {**_ID, "after": {"type": "string", "format": "date-time"}},
        _object({"schedule": SCHEDULE, "cursor": _object({
            "cursor_seq": {"type": "integer"}, "committed_seq": {"type": "integer"},
            "events_after": {"type": "integer"}})}), risk="high"),
)


def _after(value: str) -> datetime:
    """A time with its offset, as ``reset_cursor`` takes it."""
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise OpError(ErrorCode.INVALID, "after must be an ISO 8601 time with its offset, such as "
                                         "2026-11-19T08:00:00-08:00") from None
    if moment.tzinfo is None:
        raise OpError(ErrorCode.INVALID, "after needs its offset (such as -08:00 or Z): events are stored in UTC")
    return moment.astimezone(timezone.utc)


async def _subscription_impact(action, row, arguments, project_dir, subject, principal):
    """What the operator is shown before they act on a subscription schedule; None when no confirmation is needed
    (a change that touches neither its target, its rule, its zone nor whether it runs)."""
    now = datetime.now(timezone.utc)
    if action == "delete":
        return (f"Delete {subject}, its runs and notifications. The events it read stay in the stream; "
                "what it pushed stays in the chat.")
    if action == "reset_planner_session":
        raise OpError(ErrorCode.INVALID, "a subscription has no Planner session")
    if action == "run":
        arguments["_schedule_version"] = db_schedules.configuration_version(row, project_work_dir=project_dir)
        pushes = "pushes it to its confirmed target" if row["subscription"]["target_confirmed_at"] else \
            "keeps it on the dashboard (its push target is not confirmed)"
        sub = row["subscription"]
        window = (f"events from the last {sub['max_age_minutes']} minutes, ignoring its cursor"
                  if sub["window"] == "lookback" else "the events after its cursor")
        daily = " It skips if this local date already has an attempt." if sub["delivery_policy"] == "daily" else ""
        if subscriptions.is_extraction(sub):
            pushes = (f"stores what it finds as {subscriptions.output_kind(sub)} (only once it is confirmed; until "
                      "then the run is skipped)")
        return (f"Run {subject} once now: it reads {window}, runs its processor within its caps and "
                f"{pushes}. Its next fire stays.{daily}")
    if action == "confirm_target":
        if not row["notify_channel"] and not subscriptions.is_extraction(row["subscription"]):
            raise OpError(ErrorCode.INVALID, "the subscription has no notify_target to confirm")
        try:
            await schedules.check_private_target(row["subscription"], row["notify_channel"], row["notify_chat"])
        except schedules.RequestError as exc:
            raise OpError(ErrorCode.INVALID, str(exc)) from None
        arguments["_schedule_version"] = db_schedules.configuration_version(row, project_work_dir=project_dir)
        shown = await subscriptions.config_version(row)
        if arguments.get("config_version") not in (None, shown):
            raise OpError(ErrorCode.CONFLICT, "the subscription changed since config_version was read: read it again")
        # A client is shown what the schedule is now; confirming confirms that.
        if principal.kind == "client":
            arguments["_config_version"] = shown
        return await subscriptions.confirmation_text(row, now=now, action="确认推送目标：")
    if action == "reset_cursor":
        if row["subscription"]["window"] == "lookback":
            raise OpError(ErrorCode.INVALID, "a lookback subscription does not use a cursor")
        if await db_schedules.open_runs(db_connection.get_pool(), row["id"]):
            raise OpError(ErrorCode.CONFLICT, "a run of the subscription is in progress: reset its cursor when it ends")
        moment = _after(arguments.get("after"))
        found = await db_subs.cursor_after(db_connection.get_pool(), row["id"], moment)
        return (f"Reset the cursor of {subject} to just after the events ingested before {moment.isoformat()}: "
                f"it moves from {row['subscription']['cursor_seq']} to {found['cursor_seq']}, so its next run reads "
                f"the {found['events_after']} events stored since (up to {found['committed_seq']}), then the "
                "subscription's own filter and window limits apply.")
    data = arguments["fields"]
    if not data:
        raise OpError(ErrorCode.INVALID, "give at least one field to change")
    if "kind" in data and data["kind"] != "subscription":
        raise OpError(ErrorCode.INVALID, "a schedule's kind cannot change; create a new schedule")
    try:
        stream = await schedules._stream_fields(data, update=True)
        rest = {key: value for key, value in data.items() if key != "subscription"}
        fields, profiles, _ = (await schedules._fields(rest, update=True, resolve_profiles=False)
                               if rest else ({}, None, None))
        schedules._check_kind_fields("subscription", {**row, **fields, **(
            {"profile_overrides": profiles} if "profile_overrides" in rest else {})})
        schedules._check_rule(fields.get("cron", row["cron"]), fields.get("timezone", row["timezone"]), now,
                              gated=True)
        if schedules.touches_target(row, fields, stream):  # disabling and renaming are always possible
            await schedules.check_private_target({**row["subscription"], **(stream or {})},
                                                 fields.get("notify_channel", row["notify_channel"]),
                                                 fields.get("notify_chat", row["notify_chat"]))
    except schedules.RequestError as exc:
        raise OpError(ErrorCode.INVALID, str(exc)) from None
    asked = {**fields, **({"enabled": data["enabled"]} if "enabled" in data else {})}
    if subscriptions.is_extraction(row["subscription"]):  # its chats and source are confirmed too
        asked.update(stream or {})
    enabling = bool(data.get("enabled")) and not row["enabled"]
    if not (enabling or subscriptions.needs_confirmation(row, asked, stream=stream)):
        return None
    if enabling:
        try:
            await schedules._check_processor_runs(row["id"])
        except schedules.RequestError as exc:
            raise OpError(ErrorCode.CONFLICT, str(exc)) from None
    if "profile_overrides" in rest:
        raise OpError(ErrorCode.INVALID, "change the processor's Agent Profile in a request of its own: what you "
                                         "confirm is the profile that runs")
    arguments["_schedule_version"] = db_schedules.configuration_version(
        row, project_work_dir=project_dir, changes=fields)
    after = {**row, **fields, "enabled": data.get("enabled", row["enabled"]),
             "subscription": {**row["subscription"], **(stream or {})}}
    # Only a client that was asked and confirmed has the operator's word, and only for what it was shown: the
    # dashboard's own request is not asked, and its operator confirms the target after (``confirm_target``).
    arguments["_confirm_version"] = (await subscriptions.config_version(after)
                                     if principal.kind == "client" and (after.get("notify_channel") or
                                                                        subscriptions.is_extraction(after["subscription"]))
                                     else None)
    return await subscriptions.confirmation_text(after, now=now, action=f"{'启用并更新' if enabling else '更新'}")


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
    if row["kind"] == "subscription":
        return await _subscription_impact(action, row, arguments, project_dir, subject, principal)
    if action in ("confirm_target", "reset_cursor"):
        raise OpError(ErrorCode.INVALID, f"{action} applies to subscriptions only")
    if action == "delete":
        if row["kind"] == "command":
            runs = await db_schedules.open_runs(db_connection.get_pool(), row["id"])
            if schedules.blocks_delete(row, runs):
                raise OpError(ErrorCode.CONFLICT, schedules.running_command_delete(runs))
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
    if "subscription" in data:
        raise OpError(ErrorCode.INVALID, "subscription applies to schedules of kind subscription only")
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
                                                          expected_version=arguments.get("_schedule_version"),
                                                          confirm_version=arguments.get("_confirm_version")))
    elif action == "confirm_target":
        version = arguments.get("config_version") or arguments.get("_config_version")
        if not version:
            raise OpError(ErrorCode.INVALID, "config_version is required: read it from the schedule's subscription "
                                             "(schedule_admin.get) and confirm what it showed")
        result = await answered(schedules.confirm_target(arguments["schedule_id"], config_version=version,
                                                         runtime_root=server._runtime_root,
                                                         expected_version=arguments.get("_schedule_version")))
    elif action == "reset_cursor":
        result = await answered(schedules.reset_cursor(arguments["schedule_id"], _after(arguments.get("after")),
                                                       runtime_root=server._runtime_root))
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
