"""Schedules over HTTP: validation, the views and the preview (scheduled-tasks RFC §5, §9, §10).

A schedule belongs to a project and fires a full task of it on a time rule
(``looper/agents/cron.py``); the scheduler (``looper/agents/scheduler.py``)
does the firing. A new schedule is disabled. Enabling one, or changing the
rule of an enabled one, sets its next fire time from now, so nothing missed
before is run. Two fires of a rule are at least ``min_interval_seconds()``
apart (5 minutes), checked over the next 24 hours when it is saved;
``LOOPER_SCHEDULE_MIN_INTERVAL_SECONDS``, read when the server starts, lowers
it for tests and validation only.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from enum import Enum
from typing import Any

import asyncpg

from .. import env
from ..db import connection as db_connection, llm_records as db_llm_records, projects as db_projects
from ..db import schedules as db_schedules
from . import cron
from .agent_settings import budget_usd
from .projects import HTTP_400, HTTP_404, HTTP_409, TASK_PROFILE_ROLES, RequestError, _profile_overrides
from .tiers.task import resolve_work_dir

NAME_MAX_CHARS = 80
# The task requirement in a Planner's prompt is clipped at 8,000 characters (system_prompt.TASK_REQUIREMENT_LIMIT);
# this leaves room for the run context the scheduler appends.
REQUIREMENT_MAX_CHARS = 6000
RUN_TIMEOUT_MAX_SECONDS = 30 * 86400
PREVIEW_COUNT = 5
PREVIEW_MAX = 20
RUNS_DEFAULT = 50
RUNS_MAX = 200

MIN_INTERVAL_ENV = "LOOPER_SCHEDULE_MIN_INTERVAL_SECONDS"
DEFAULT_MIN_INTERVAL_SECONDS = 300
_min_interval = DEFAULT_MIN_INTERVAL_SECONDS

NO_WORK_DIR = "a work directory is required: set one on the schedule or on its project"


class Overlap(str, Enum):
    SKIP = "skip"  # a firing while the previous run's task is in progress is skipped
    QUEUE = "queue"  # it waits for that task to end; at most one waits


class CatchUp(str, Enum):
    ONCE = "once"  # missed fires run once, told how many were missed
    SKIP = "skip"  # missed fires are skipped unless the latest is within the grace period


def configure_min_interval() -> int:
    """Read ``LOOPER_SCHEDULE_MIN_INTERVAL_SECONDS`` (for tests and validation only) at startup; raises
    ``ValueError`` for a value that is not a whole number of seconds from 1."""
    global _min_interval
    raw = env.get(MIN_INTERVAL_ENV)
    if raw is None or raw.strip() == "":
        _min_interval = DEFAULT_MIN_INTERVAL_SECONDS
    else:
        try:
            value = int(raw)
        except ValueError:
            value = 0
        if value < 1:
            raise ValueError(f"{MIN_INTERVAL_ENV} must be a whole number of seconds from 1; got {raw!r}")
        _min_interval = value
    return _min_interval


def min_interval_seconds() -> int:
    return _min_interval


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _seconds_text(seconds: int) -> str:
    if seconds % 60 == 0:
        minutes = seconds // 60
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    return f"{seconds} second{'s' if seconds != 1 else ''}"


def checked_rule(rule_text: Any, zone_name: Any, *, now: datetime) -> tuple[cron.CronRule, Any]:
    """The rule and zone, checked: valid, firing at all, and never closer than the minimum interval over the next
    24 hours. Raises ``ValueError`` saying why not."""
    if not isinstance(rule_text, str) or not rule_text.strip():
        raise ValueError("cron is required: a 5-field rule such as '0 9 * * 1-5'")
    rule = cron.parse(rule_text)
    tz = cron.zone(zone_name)
    if cron.next_fire(rule, tz, now) is None:
        raise ValueError(f"the rule {rule.text!r} never fires")
    if close := cron.too_close(rule, tz, now, _min_interval):
        first, second = (moment.astimezone(tz).strftime("%Y-%m-%d %H:%M") for moment in close)
        gap = int((close[1] - close[0]).total_seconds())
        raise ValueError(f"the rule {rule.text!r} fires {_seconds_text(gap)} apart ({first} and {second}, "
                         f"{zone_name}); fires must be at least {_seconds_text(_min_interval)} apart")
    return rule, tz


def describe(rule_text: str, zone_name: str) -> str:
    try:
        return cron.describe(cron.parse(rule_text), zone_name)
    except ValueError:
        return f"{rule_text} ({zone_name})"


# --- Views ---


def run_view(row: Mapping[str, Any], cost: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """A run; a running one's cost is its task's so far (``cost``)."""
    cost_usd = row.get("cost_usd")
    if cost_usd is None and cost is not None:
        cost_usd = cost["cost_usd"]
    return {
        "run_id": row["id"], "schedule_id": row["schedule_id"], "scheduled_for": row["scheduled_for"],
        "fired_at": row.get("fired_at"), "trigger": row["trigger"], "missed": row["missed"],
        "status": row["status"], "task_id": row.get("task_id"), "task_status": row.get("task_status"),
        "summary": row.get("summary"), "cost_usd": cost_usd, "error": row.get("error"),
        "finished_at": row.get("finished_at"),
    }


async def _run_views(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    running = [row["task_id"] for row in rows if row.get("task_id") and row.get("cost_usd") is None]
    costs = await db_llm_records.task_costs(running) if running else {}
    return [run_view(row, costs.get(row.get("task_id") or "")) for row in rows]


def schedule_view(row: Mapping[str, Any], last_run: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "schedule_id": row["id"], "project_id": row["project_id"], "name": row["name"], "cron": row["cron"],
        "timezone": row["timezone"], "description": describe(row["cron"], row["timezone"]),
        "requirement": row["requirement"], "work_dir": row["work_dir"], "overlap": row["overlap"],
        "catch_up": row["catch_up"], "run_timeout_seconds": row["run_timeout_seconds"],
        "run_budget_usd": row["run_budget_usd"], "profile_overrides": dict(row.get("profile_overrides") or {}),
        "enabled": row["enabled"], "next_fire_at": row["next_fire_at"], "last_fired_at": row["last_fired_at"],
        "last_success_at": row["last_success_at"], "consecutive_failures": row["consecutive_failures"],
        "created_via": row["created_via"], "created_at": row["created_at"], "updated_at": row["updated_at"],
        "last_run": last_run,
    }


async def _schedule_answer(schedule_id: str) -> dict[str, Any]:
    row = await _existing(schedule_id)
    latest = (await db_schedules.latest_runs([schedule_id])).get(schedule_id)
    last_run = (await _run_views([latest]))[0] if latest else None
    return {"schedule": schedule_view(row, last_run)}


def limits() -> dict[str, Any]:
    return {
        "name_max_chars": NAME_MAX_CHARS, "requirement_max_chars": REQUIREMENT_MAX_CHARS,
        "min_interval_seconds": _min_interval, "default_timezone": cron.local_zone_name(),
        "run_timeout_max_seconds": RUN_TIMEOUT_MAX_SECONDS, "roles": list(TASK_PROFILE_ROLES),
    }


# --- Validation ---


def _name(raw: Any) -> str:
    name = raw.strip() if isinstance(raw, str) else ""
    if not name:
        raise ValueError("a schedule name is required")
    if len(name) > NAME_MAX_CHARS:
        raise ValueError(f"a schedule name has at most {NAME_MAX_CHARS} characters")
    return name


def _requirement(raw: Any) -> str:
    text = raw.strip() if isinstance(raw, str) else ""
    if not text:
        raise ValueError("requirement is required: what each run's task should do")
    if len(text) > REQUIREMENT_MAX_CHARS:
        raise ValueError(f"a schedule's requirement has at most {REQUIREMENT_MAX_CHARS} characters, got {len(text)}")
    return text


def _work_dir(raw: Any) -> str:
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise ValueError("work_dir must be a string: an absolute directory, or empty for the project's default")
    return resolve_work_dir(raw) if raw.strip() else ""


def _choice(field: str, raw: Any, kind: type[Enum]) -> str:
    try:
        return kind(raw).value
    except ValueError:
        raise ValueError(f"{field} must be one of {', '.join(str(m.value) for m in kind)}") from None


def _timeout(raw: Any) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int) or not 1 <= raw <= RUN_TIMEOUT_MAX_SECONDS:
        raise ValueError(f"run_timeout_seconds must be a whole number of seconds from 1 to "
                         f"{RUN_TIMEOUT_MAX_SECONDS}, or null for no limit")
    return raw


def _budget(raw: Any) -> float | None:
    return None if raw is None else budget_usd(raw, field="run_budget_usd")


_PARSERS = {
    "name": _name, "requirement": _requirement, "work_dir": _work_dir,
    "overlap": lambda raw: _choice("overlap", raw, Overlap),
    "catch_up": lambda raw: _choice("catch_up", raw, CatchUp),
    "run_timeout_seconds": _timeout, "run_budget_usd": _budget,
}
_ACCEPTED = (*db_schedules.FIELDS, "profile_overrides")


async def _fields(data: Mapping[str, Any], *, update: bool) -> tuple[dict[str, Any], dict[str, str] | None]:
    """The schedule fields in ``data``, validated, and the profile overrides when given; ``enabled`` is the
    caller's. A new schedule needs a name, a rule and a requirement."""
    accepted = (*_ACCEPTED, "enabled") if update else _ACCEPTED
    if "enabled" in data and not update:
        raise RequestError(HTTP_400, "a new schedule is created disabled; enable it with PUT /api/schedules/<id> "
                                     '{"enabled": true}')
    if unknown := [key for key in data if key not in accepted]:
        raise RequestError(HTTP_400, f"unknown field {unknown[0]!r}; a schedule has {', '.join(accepted)}")
    fields: dict[str, Any] = {}
    try:
        for name, parse in _PARSERS.items():
            if name in data or (not update and name in ("name", "requirement")):
                fields[name] = parse(data.get(name))
        if "cron" in data or not update:
            fields["cron"] = data.get("cron")
        if "timezone" in data or not update:
            zone_name = data.get("timezone")
            fields["timezone"] = zone_name if zone_name not in (None, "") else cron.local_zone_name()
    except ValueError as exc:
        raise RequestError(HTTP_400, str(exc)) from None
    profiles = await _profile_overrides(data["profile_overrides"]) if "profile_overrides" in data else None
    return fields, profiles


async def _existing_project(project_id: str) -> dict[str, Any]:
    project = await db_projects.get_project(project_id)
    if project is None:
        raise RequestError(HTTP_404, f"project {project_id!r} not found")
    return project


async def _existing(schedule_id: str) -> dict[str, Any]:
    row = await db_schedules.get_schedule(schedule_id)
    if row is None:
        raise RequestError(HTTP_404, f"schedule {schedule_id!r} not found")
    return row


def _name_taken(name: str) -> RequestError:
    return RequestError(HTTP_409, f"this project already has a schedule named {name!r}")


def _check_rule(rule_text: Any, zone_name: Any, now: datetime) -> cron.CronRule:
    try:
        return checked_rule(rule_text, zone_name, now=now)[0]
    except ValueError as exc:
        raise RequestError(HTTP_400, str(exc)) from None


# --- Requests ---


async def list_schedules(project_id: str) -> dict[str, Any]:
    await _existing_project(project_id)
    rows = await db_schedules.list_schedules(project_id)
    latest = await db_schedules.latest_runs([row["id"] for row in rows])
    views = dict(zip(latest, await _run_views(list(latest.values()))))
    return {"schedules": [schedule_view(row, views.get(row["id"])) for row in rows], "limits": limits()}


async def create_schedule(project_id: str, data: dict[str, Any], *, created_via: str = "dashboard",
                          now: datetime | None = None) -> dict[str, Any]:
    project = await _existing_project(project_id)
    fields, profiles = await _fields(data, update=False)
    _check_rule(fields["cron"], fields["timezone"], now or _utcnow())
    fields["cron"] = cron.parse(fields["cron"]).text
    if not fields.get("work_dir") and not project["work_dir"]:
        raise RequestError(HTTP_400, NO_WORK_DIR)
    try:
        async with db_connection.transaction() as conn:
            schedule_id = await db_schedules.create_schedule(conn, project_id=project_id, fields=fields,
                                                             profiles=profiles or {}, created_via=created_via)
    except asyncpg.UniqueViolationError:
        raise _name_taken(fields["name"]) from None
    except asyncpg.ForeignKeyViolationError:  # the project or a profile was removed meanwhile
        raise RequestError(HTTP_400, "the project or an Agent Profile the schedule chose was removed meanwhile") \
            from None
    return await _schedule_answer(schedule_id)


async def get_schedule(schedule_id: str) -> dict[str, Any]:
    return await _schedule_answer(schedule_id)


async def update_schedule(schedule_id: str, data: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Change the fields given. Enabling sets the next fire time from now (nothing missed before runs), as does a
    new rule or zone on an enabled schedule; disabling clears it."""
    if not data:
        raise RequestError(HTTP_400, "give at least one field to change")
    now = now or _utcnow()
    fields, profiles = await _fields(data, update=True)
    enabled = data.get("enabled")
    if "enabled" in data and not isinstance(enabled, bool):
        raise RequestError(HTTP_400, "enabled must be true or false")
    try:
        async with db_connection.transaction() as conn:
            row = await db_schedules.lock_schedule(conn, schedule_id)
            if row is None:
                raise RequestError(HTTP_404, f"schedule {schedule_id!r} not found")
            rule_text, zone_name = fields.get("cron", row["cron"]), fields.get("timezone", row["timezone"])
            rule = _check_rule(rule_text, zone_name, now)
            if "cron" in fields:
                fields["cron"] = rule.text
            work_dir = fields.get("work_dir", row["work_dir"])
            if not work_dir:
                project = await db_projects.get_project(row["project_id"], pool=conn)
                if not (project and project["work_dir"]):
                    raise RequestError(HTTP_400, NO_WORK_DIR)
            will_run = row["enabled"] if enabled is None else enabled
            if not will_run:
                fields["next_fire_at"] = None
            elif (enabled and not row["enabled"]) or "cron" in fields or "timezone" in fields:
                fields["next_fire_at"] = cron.next_fire(rule, cron.zone(zone_name), now)
            if enabled is not None:
                fields["enabled"] = enabled
            await db_schedules.update_schedule(conn, schedule_id, fields, profiles=profiles)
    except asyncpg.UniqueViolationError:
        raise _name_taken(fields["name"]) from None
    except asyncpg.ForeignKeyViolationError:
        raise RequestError(HTTP_400, "an Agent Profile the schedule chose was removed meanwhile; choose again") \
            from None
    return await _schedule_answer(schedule_id)


async def delete_schedule(schedule_id: str) -> dict[str, Any]:
    """Delete a schedule and its run history; the tasks it created stay."""
    if not await db_schedules.delete_schedule(schedule_id):
        raise RequestError(HTTP_404, f"schedule {schedule_id!r} not found")
    return {"deleted": schedule_id}


async def list_runs(schedule_id: str, query: Mapping[str, str]) -> dict[str, Any]:
    raw = query.get("limit") or str(RUNS_DEFAULT)
    if not raw.isdigit() or not 1 <= int(raw) <= RUNS_MAX:
        raise RequestError(HTTP_400, f"limit must be a whole number from 1 to {RUNS_MAX}")
    await _existing(schedule_id)
    return {"runs": await _run_views(await db_schedules.list_runs(schedule_id, limit=int(raw)))}


def preview(data: Mapping[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Whether a rule and zone are valid, the rule in words and its next fire times; an invalid one is answered
    with ``valid`` false and the reason."""
    rule_text, zone_name, count = data.get("cron"), data.get("timezone"), data.get("count", PREVIEW_COUNT)
    if not isinstance(rule_text, str):
        raise RequestError(HTTP_400, "cron must be a string")
    if zone_name in (None, ""):
        zone_name = cron.local_zone_name()
    elif not isinstance(zone_name, str):
        raise RequestError(HTTP_400, "timezone must be a string: an IANA name")
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= PREVIEW_MAX:
        raise RequestError(HTTP_400, f"count must be a whole number from 1 to {PREVIEW_MAX}")
    now = now or _utcnow()
    answer: dict[str, Any] = {"cron": rule_text, "timezone": zone_name, "min_interval_seconds": _min_interval}
    try:
        rule, tz = checked_rule(rule_text, zone_name, now=now)
    except ValueError as exc:
        return {**answer, "valid": False, "description": None, "next": [], "error": str(exc)}
    return {**answer, "valid": True, "cron": rule.text, "description": cron.describe(rule, zone_name),
            "next": cron.next_fires(rule, tz, now, count), "error": None}


async def run_now(schedule_id: str, scheduler: Any) -> dict[str, Any]:
    """Fire the schedule now (``trigger`` manual), through the same overlap check; its next fire time stays."""
    run_id = await scheduler.run_now(schedule_id)
    if run_id is None:
        raise RequestError(HTTP_404, f"schedule {schedule_id!r} not found")
    row = await db_schedules.get_run(run_id)
    return {"run": (await _run_views([row]))[0]}
