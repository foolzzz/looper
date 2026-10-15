"""Schedules over HTTP: validation, the views and the preview (scheduled-tasks RFC §5, §9, §10).

A schedule belongs to a project and fires a full task of it on a time rule
(``looper/agents/cron.py``); the scheduler (``looper/agents/scheduler.py``)
does the firing. A new schedule is disabled. Enabling one, or changing the
rule of an enabled one, sets its next fire time from now, so nothing missed
before is run. Two fires of a rule are at least ``min_interval_seconds``
apart, checked over the next 24 hours when it is saved: 5 minutes, or 1 minute
for a schedule with a gate command, whose run ends at its gate when there is
nothing new (``skipped_gate``: no task, no cost). Removing the gate of a
schedule that fires more often is refused.
``LOOPER_SCHEDULE_MIN_INTERVAL_SECONDS``, read when the server starts, lowers
both for tests and validation only.

S2 adds a gate command (run before each run), a daily spend cap, and whom to
tell (``notify`` and a ``notify_target``: a chat that a channel's sender
allowlist in Settings → Channels already allows; none means the dashboard
only). Each schedule's view carries what its runs cost today and its active
alerts; ``schedule_overview`` is the dashboard's summary.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from enum import Enum
from typing import Any

import asyncpg

from .. import env
from ..db import channel_configs as db_channel_configs, connection as db_connection, llm_records as db_llm_records
from ..db import projects as db_projects, schedules as db_schedules
from . import cron
from .agent_settings import AgentType, budget_usd, load_concurrency
from .channels.allowlist import SENDER_ID_FORMATS, sender_ids
from .projects import HTTP_400, HTTP_404, HTTP_409, TASK_PROFILE_ROLES, RequestError, _profile_overrides
from .scheduler import GATE_TIMEOUT_DEFAULT_SECONDS, GATE_TIMEOUT_MAX_SECONDS
from .system_prompt import TASK_REQUIREMENT_LIMIT
from .tiers.task import resolve_work_dir

NAME_MAX_CHARS = 80
# A run's task requirement is the schedule's and the run context the scheduler appends (``scheduler.run_context``,
# fitted into the room left); the schedule's own leaves that much room under the task limit.
RUN_CONTEXT_RESERVE_CHARS = 2000
REQUIREMENT_MAX_CHARS = TASK_REQUIREMENT_LIMIT - RUN_CONTEXT_RESERVE_CHARS
RUN_TIMEOUT_MAX_SECONDS = 30 * 86400
PREVIEW_COUNT = 5
PREVIEW_MAX = 20
RUNS_DEFAULT = 50
RUNS_MAX = 200
GATE_COMMAND_MAX_CHARS = 2000
MAX_PARALLEL_RUNS_LIMIT = 10

MIN_INTERVAL_ENV = "LOOPER_SCHEDULE_MIN_INTERVAL_SECONDS"
DEFAULT_MIN_INTERVAL_SECONDS = 300
# A gated schedule may fire every minute: a run with nothing new ends at its gate, without a task.
MIN_INTERVAL_GATED_SECONDS = 60
_min_interval = DEFAULT_MIN_INTERVAL_SECONDS

NO_WORK_DIR = "a work directory is required: set one on the schedule or on its project"


class Overlap(str, Enum):
    SKIP = "skip"  # a firing while the previous run's task is in progress is skipped
    QUEUE = "queue"  # it waits for that task to end; at most one waits
    PARALLEL = "parallel"  # it starts beside the runs in progress, up to max_parallel_runs (reentrant)


class CatchUp(str, Enum):
    ONCE = "once"  # missed fires run once, told how many were missed
    SKIP = "skip"  # missed fires are skipped unless the latest is within the grace period


class SkipRows(str, Enum):
    """Whether a list of runs shows the routine skips (``ROUTINE_SKIPS``)."""

    SHOW = "show"
    HIDE = "hide"


# Runs that only say there was nothing to do: the gate found nothing new, or the previous run was still going.
ROUTINE_SKIPS = ("skipped_gate", "skipped_overlap")


class Notify(str, Enum):
    FAILURES = "failures"  # failed runs, the alerts and their recoveries
    ALWAYS = "always"  # every run that ran something, the alerts and their recoveries
    NEVER = "never"  # nothing is sent; the dashboard still shows the alerts


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


def min_interval_seconds(*, gated: bool) -> int:
    """How close two fires of a schedule may be: a minute with a gate command, else ``DEFAULT_MIN_INTERVAL_SECONDS``
    (both lowered by ``LOOPER_SCHEDULE_MIN_INTERVAL_SECONDS``)."""
    return min(MIN_INTERVAL_GATED_SECONDS, _min_interval) if gated else _min_interval


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _seconds_text(seconds: int) -> str:
    if seconds % 60 == 0:
        minutes = seconds // 60
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    return f"{seconds} second{'s' if seconds != 1 else ''}"


def checked_rule(rule_text: Any, zone_name: Any, *, now: datetime, gated: bool = False) -> tuple[cron.CronRule, Any]:
    """The rule and zone, checked: valid, firing at all, and never closer than the minimum interval of a schedule
    with a gate command (``gated``) or without one over the next 24 hours. Raises ``ValueError`` saying why not."""
    if not isinstance(rule_text, str) or not rule_text.strip():
        raise ValueError("cron is required: a 5-field rule such as '0 9 * * 1-5'")
    rule = cron.parse(rule_text)
    tz = cron.zone(zone_name)
    if cron.next_fire(rule, tz, now) is None:
        raise ValueError(f"the rule {rule.text!r} never fires")
    least = min_interval_seconds(gated=gated)
    if close := cron.too_close(rule, tz, now, least):
        first, second = (moment.astimezone(tz).strftime("%Y-%m-%d %H:%M") for moment in close)
        gap = int((close[1] - close[0]).total_seconds())
        gated_least = min_interval_seconds(gated=True)
        hint = (f" (a schedule with a gate command may fire every {_seconds_text(gated_least)})"
                if not gated and gated_least < least else "")
        raise ValueError(f"the rule {rule.text!r} fires {_seconds_text(gap)} apart ({first} and {second}, "
                         f"{zone_name}); fires must be at least {_seconds_text(least)} apart{hint}")
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
        "task_title": row.get("task_title"),
        "summary": row.get("summary"), "cost_usd": cost_usd, "error": row.get("error"),
        "finished_at": row.get("finished_at"), "gate_output": row.get("gate_output"),
        "carried_from": row.get("carried_from"),
        # A skip that repeated counts every firing it stands for, from scheduled_for to finished_at.
        "firings": row.get("firings") or 1,
    }


async def _run_views(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    running = [row["task_id"] for row in rows if row.get("task_id") and row.get("cost_usd") is None]
    costs = await db_llm_records.task_costs(running) if running else {}
    return [run_view(row, costs.get(row.get("task_id") or "")) for row in rows]


def schedule_view(row: Mapping[str, Any], last_run: dict[str, Any] | None = None, *,
                  spent_today: float = 0.0) -> dict[str, Any]:
    target = {"channel": row["notify_channel"], "chat": row["notify_chat"]} if row.get("notify_channel") else None
    return {
        "schedule_id": row["id"], "project_id": row["project_id"], "project_name": row["project_name"],
        "name": row["name"], "cron": row["cron"],
        "timezone": row["timezone"], "description": describe(row["cron"], row["timezone"]),
        "requirement": row["requirement"], "work_dir": row["work_dir"], "overlap": row["overlap"],
        "max_parallel_runs": row["max_parallel_runs"],
        "catch_up": row["catch_up"], "run_timeout_seconds": row["run_timeout_seconds"],
        "run_budget_usd": row["run_budget_usd"], "profile_overrides": dict(row.get("profile_overrides") or {}),
        "enabled": row["enabled"], "next_fire_at": row["next_fire_at"], "last_fired_at": row["last_fired_at"],
        "last_success_at": row["last_success_at"], "consecutive_failures": row["consecutive_failures"],
        "created_via": row["created_via"], "created_at": row["created_at"], "updated_at": row["updated_at"],
        "gate_command": row.get("gate_command"), "gate_timeout_seconds": row.get("gate_timeout_seconds"),
        "daily_budget_usd": row.get("daily_budget_usd"), "notify": row.get("notify") or Notify.FAILURES.value,
        "notify_target": target, "spent_today_usd": round(spent_today, 6),
        "alerts": {"failures_since": row.get("failures_alert_at"), "stale_since": row.get("stale_alert_at")},
        "last_run": last_run,
    }


def local_midnight(now: datetime | None = None) -> datetime:
    """The start of today in the server's zone, where a schedule's day (its daily budget) starts."""
    zone = cron.zone(cron.local_zone_name())
    local = (now or _utcnow()).astimezone(zone)
    return datetime.combine(local.date(), datetime.min.time(), tzinfo=zone)


async def _schedule_answer(schedule_id: str) -> dict[str, Any]:
    row = await _existing(schedule_id)
    latest = (await db_schedules.latest_runs([schedule_id])).get(schedule_id)
    last_run = (await _run_views([latest]))[0] if latest else None
    spent = await db_schedules.spent_since(db_connection.get_pool(), schedule_id, local_midnight())
    return {"schedule": schedule_view(row, last_run, spent_today=spent)}


async def notify_targets() -> list[dict[str, Any]]:
    """The chats a schedule may tell: the senders each channel's allowlist (Settings → Channels) already lets in."""
    targets = []
    for row in await db_channel_configs.list_configs():
        channel = row["channel_type"]
        if channel not in SENDER_ID_FORMATS:
            continue
        try:
            chats = sender_ids(channel, (row.get("config") or {}).get(SENDER_ID_FORMATS[channel].field))
        except ValueError:
            continue  # an invalid stored list: the channel does not start either
        label = "Telegram" if channel == "telegram" else "Lark"
        targets += [{"channel": channel, "chat": chat, "label": f"{label} · {chat}",
                     "channel_enabled": bool(row.get("enabled"))} for chat in chats]
    return targets


async def limits() -> dict[str, Any]:
    return {
        "name_max_chars": NAME_MAX_CHARS, "requirement_max_chars": REQUIREMENT_MAX_CHARS,
        "min_interval_seconds": min_interval_seconds(gated=False),
        "min_interval_gated_seconds": min_interval_seconds(gated=True), "default_timezone": cron.local_zone_name(),
        "run_timeout_max_seconds": RUN_TIMEOUT_MAX_SECONDS, "roles": list(TASK_PROFILE_ROLES),
        "gate_command_max_chars": GATE_COMMAND_MAX_CHARS, "gate_timeout_default_seconds": GATE_TIMEOUT_DEFAULT_SECONDS,
        "gate_timeout_max_seconds": GATE_TIMEOUT_MAX_SECONDS, "max_parallel_runs_limit": MAX_PARALLEL_RUNS_LIMIT,
        "notify_targets": await notify_targets(),
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


def _max_parallel_runs(raw: Any) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or not 1 <= raw <= MAX_PARALLEL_RUNS_LIMIT:
        raise ValueError(f"max_parallel_runs must be a whole number from 1 to {MAX_PARALLEL_RUNS_LIMIT}: how many "
                         "runs of a parallel schedule may be in progress at once")
    return raw


def _budget(field: str) -> Any:
    return lambda raw: None if raw is None else budget_usd(raw, field=field)


def _gate_command(raw: Any) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError("gate_command must be a string: a shell command, or empty for no gate")
    text = raw.strip()
    if len(text) > GATE_COMMAND_MAX_CHARS:
        raise ValueError(f"a gate command has at most {GATE_COMMAND_MAX_CHARS} characters, got {len(text)}")
    return text or None


def _gate_timeout(raw: Any) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int) or not 1 <= raw <= GATE_TIMEOUT_MAX_SECONDS:
        raise ValueError(f"gate_timeout_seconds must be a whole number of seconds from 1 to "
                         f"{GATE_TIMEOUT_MAX_SECONDS}, or null for {GATE_TIMEOUT_DEFAULT_SECONDS}")
    return raw


_PARSERS = {
    "name": _name, "requirement": _requirement, "work_dir": _work_dir,
    "overlap": lambda raw: _choice("overlap", raw, Overlap), "max_parallel_runs": _max_parallel_runs,
    "catch_up": lambda raw: _choice("catch_up", raw, CatchUp),
    "run_timeout_seconds": _timeout, "run_budget_usd": _budget("run_budget_usd"),
    "gate_command": _gate_command, "gate_timeout_seconds": _gate_timeout,
    "daily_budget_usd": _budget("daily_budget_usd"), "notify": lambda raw: _choice("notify", raw, Notify),
}
_ACCEPTED = (*(name for name in db_schedules.FIELDS if name not in ("notify_channel", "notify_chat")),
             "notify_target", "profile_overrides")


async def _notify_target(raw: Any) -> dict[str, str | None]:
    """``notify_target`` as the stored columns: one of ``notify_targets()``, or none (null)."""
    if raw is None:
        return {"notify_channel": None, "notify_chat": None}
    if not isinstance(raw, dict) or not isinstance(raw.get("channel"), str) or not isinstance(raw.get("chat"), str):
        raise RequestError(HTTP_400, 'notify_target must be {"channel": "telegram" | "lark", "chat": "<id>"} or '
                                     "null for the dashboard only")
    allowed = {(target["channel"], target["chat"]) for target in await notify_targets()}
    if (raw["channel"], raw["chat"]) not in allowed:
        raise RequestError(HTTP_400, f"{raw['channel']} chat {raw['chat']!r} is not one the channel allows: add the "
                                     "sender in Settings → Channels first")
    return {"notify_channel": raw["channel"], "notify_chat": raw["chat"]}


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
    if "notify_target" in data:
        fields.update(await _notify_target(data["notify_target"]))
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


def _check_rule(rule_text: Any, zone_name: Any, now: datetime, *, gated: bool,
                removes_gate: bool = False) -> cron.CronRule:
    try:
        return checked_rule(rule_text, zone_name, now=now, gated=gated)[0]
    except ValueError as exc:
        reason = f"without its gate command, {exc}" if removes_gate else str(exc)
        raise RequestError(HTTP_400, reason) from None


# --- Requests ---


async def _schedule_views(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``schedule_view`` of each row, with its newest run and what it spent today."""
    ids = [row["id"] for row in rows]
    latest = await db_schedules.latest_runs(ids)
    views = dict(zip(latest, await _run_views(list(latest.values()))))
    spent = await db_schedules.spent_since_each(db_connection.get_pool(), ids, local_midnight())
    return [schedule_view(row, views.get(row["id"]), spent_today=spent[row["id"]]) for row in rows]


async def list_schedules(project_id: str) -> dict[str, Any]:
    """The project's schedules, and the limits a schedule of it is checked against: the shared ones, and the
    project's Planner limit (``planner_limit``), past which a parallel schedule's runs wait."""
    await _existing_project(project_id)
    return {"schedules": await _schedule_views(await db_schedules.list_schedules(project_id)),
            "limits": {**await limits(), "planner_limit": await load_concurrency(AgentType.PLANNER, project_id)}}


async def list_all_schedules() -> dict[str, Any]:
    """Every project's schedules, by project name and then name, the disabled ones too (the Schedules page)."""
    return {"schedules": await _schedule_views(await db_schedules.list_schedules())}


async def create_schedule(project_id: str, data: dict[str, Any], *, created_via: str = "dashboard",
                          now: datetime | None = None) -> dict[str, Any]:
    project = await _existing_project(project_id)
    fields, profiles = await _fields(data, update=False)
    _check_rule(fields["cron"], fields["timezone"], now or _utcnow(), gated=bool(fields.get("gate_command")))
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
    return await _with_warnings(await _schedule_answer(schedule_id))


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
            gated = bool(fields.get("gate_command", row["gate_command"]))
            rule = _check_rule(rule_text, zone_name, now, gated=gated,
                               removes_gate=bool(row["gate_command"]) and not gated)
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
            if enabled and not row["enabled"]:
                fields["enabled_at"] = now  # the no-success alert counts from here (``stale_deadline``)
            if rule.text != row["cron"] or zone_name != row["timezone"]:
                fields["rule_changed_at"] = now  # so does it from a new rule or zone
            await db_schedules.update_schedule(conn, schedule_id, fields, profiles=profiles)
    except asyncpg.UniqueViolationError:
        raise _name_taken(fields["name"]) from None
    except asyncpg.ForeignKeyViolationError:
        raise RequestError(HTTP_400, "an Agent Profile the schedule chose was removed meanwhile; choose again") \
            from None
    return await _with_warnings(await _schedule_answer(schedule_id))


async def _with_warnings(answer: dict[str, Any]) -> dict[str, Any]:
    """A saved schedule's answer with what deserves a look (``warnings``): a parallel schedule allowed more runs at
    once than its project's Planner limit, whose runs past it wait as pending while their run timeout counts."""
    schedule = answer["schedule"]
    warnings = []
    if schedule["overlap"] == Overlap.PARALLEL.value:
        limit = await load_concurrency(AgentType.PLANNER, schedule["project_id"])
        if schedule["max_parallel_runs"] > limit:
            warnings.append(f"max_parallel_runs is {schedule['max_parallel_runs']}, but its project runs at most "
                            f"{limit} tasks at once (the Planner limit): the runs past it wait as pending, and their "
                            "run timeout counts while they wait")
    return {**answer, "warnings": warnings}


async def delete_schedule(schedule_id: str) -> dict[str, Any]:
    """Delete a schedule and its run history; the tasks it created stay."""
    if not await db_schedules.delete_schedule(schedule_id):
        raise RequestError(HTTP_404, f"schedule {schedule_id!r} not found")
    return {"deleted": schedule_id}


async def list_runs(schedule_id: str, query: Mapping[str, str]) -> dict[str, Any]:
    """A page of the schedule's runs, newest first: ``limit`` of them, older than the run ``before`` when given,
    without the routine skips when ``skips`` is ``hide``. ``next_before`` continues the list (None at its end), and
    ``routine_skips`` counts those skips over the schedule's whole history, shown or not."""
    raw = query.get("limit") or str(RUNS_DEFAULT)
    if not raw.isdigit() or not 1 <= int(raw) <= RUNS_MAX:
        raise RequestError(HTTP_400, f"limit must be a whole number from 1 to {RUNS_MAX}")
    try:
        skips = _choice("skips", query.get("skips") or SkipRows.SHOW.value, SkipRows)
    except ValueError as exc:
        raise RequestError(HTTP_400, str(exc)) from None
    await _existing(schedule_id)
    before = query.get("before") or None
    if before is not None:
        run = await db_schedules.get_run(before)
        if run is None or run["schedule_id"] != schedule_id:
            raise RequestError(HTTP_400, f"before must be a run of schedule {schedule_id}")
    limit = int(raw)
    rows = await db_schedules.list_runs(schedule_id, limit=limit + 1, before=before,
                                        without=ROUTINE_SKIPS if skips == SkipRows.HIDE.value else ())
    page = rows[:limit]
    return {"runs": await _run_views(page),
            "next_before": page[-1]["id"] if len(rows) > limit else None,
            "routine_skips": await db_schedules.run_counts(schedule_id, ROUTINE_SKIPS)}


def preview(data: Mapping[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Whether a rule and zone are valid, for a schedule with a gate command when ``gated`` is true, the rule in
    words and its next fire times; an invalid one is answered with ``valid`` false and the reason."""
    rule_text, zone_name, count = data.get("cron"), data.get("timezone"), data.get("count", PREVIEW_COUNT)
    gated = data.get("gated", False)
    if not isinstance(gated, bool):
        raise RequestError(HTTP_400, "gated must be true or false: whether the schedule has a gate command")
    if not isinstance(rule_text, str):
        raise RequestError(HTTP_400, "cron must be a string")
    if zone_name in (None, ""):
        zone_name = cron.local_zone_name()
    elif not isinstance(zone_name, str):
        raise RequestError(HTTP_400, "timezone must be a string: an IANA name")
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= PREVIEW_MAX:
        raise RequestError(HTTP_400, f"count must be a whole number from 1 to {PREVIEW_MAX}")
    now = now or _utcnow()
    answer: dict[str, Any] = {"cron": rule_text, "timezone": zone_name,
                              "min_interval_seconds": min_interval_seconds(gated=gated)}
    try:
        rule, tz = checked_rule(rule_text, zone_name, now=now, gated=gated)
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


async def list_notifications(schedule_id: str, query: Mapping[str, str]) -> dict[str, Any]:
    """The schedule's messages, sent or not, newest first."""
    raw = query.get("limit") or str(RUNS_DEFAULT)
    if not raw.isdigit() or not 1 <= int(raw) <= RUNS_MAX:
        raise RequestError(HTTP_400, f"limit must be a whole number from 1 to {RUNS_MAX}")
    await _existing(schedule_id)
    rows = await db_schedules.list_notifications(schedule_id, limit=int(raw))
    return {"notifications": [
        {"notification_id": row["id"], "kind": row["kind"], "run_id": row["run_id"], "channel": row["channel"],
         "chat": row["chat"], "text": row["text"], "status": row["status"], "error": row["error"],
         "created_at": row["created_at"]} for row in rows]}


async def schedule_overview(since: datetime) -> dict[str, Any]:
    """The dashboard's schedules: how many there are, the next 5 fires, the failing ones, and the runs skipped
    since ``since`` by reason."""
    summary = await db_schedules.overview(since)
    return {
        "total": summary["total"], "enabled": summary["enabled"],
        "next": [{"schedule_id": row["id"], "name": row["name"], "project_id": row["project_id"],
                  "project_name": row["project_name"], "next_fire_at": row["next_fire_at"],
                  "timezone": row["timezone"]} for row in summary["upcoming"]],
        "failing": [{"schedule_id": row["id"], "name": row["name"], "project_id": row["project_id"],
                     "project_name": row["project_name"], "timezone": row["timezone"],
                     "consecutive_failures": row["consecutive_failures"],
                     "failures_since": row["failures_alert_at"], "stale_since": row["stale_alert_at"],
                     "last_success_at": row["last_success_at"], "last_status": row["last_status"]}
                    for row in summary["failing"]],
        "skipped_today": {status: summary["skipped"].get(status, 0)
                          for status in ("skipped_gate", "skipped_overlap", "skipped_missed", "skipped_budget")},
    }
