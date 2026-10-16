"""Telling people about a schedule's runs (scheduled-tasks RFC §8, S2).

A schedule's ``notify`` decides which runs get a message: ``failures`` (the
default) the failed ones (``failed``, ``timed_out``, ``gate_failed``),
``always`` every run that ran something (a task's outcome, or a failed gate),
``alerts`` and ``never`` none. Runs that were skipped never do. Two alerts go
out once per streak, each with one recovery message:

- **failures**: ``consecutive_failures`` reaches ``FAILURES_ALERT_AT`` (3);
  it recovers with the next ok run (``db.schedules.OK``): a completed run,
  or a gate that found nothing to do;
- **stale**: no run completed, and no gate found nothing to do, for 3
  periods of the rule, at least 15 minutes and at most 24 hours, counted
  from the first fire after the latest of that run, the schedule's last
  enabling and its rule's last change (``stale_deadline``); checked on every
  scheduler pass while the schedule is enabled and no run of it is in
  progress, it recovers the same way.

Alerts and recoveries are sent under every ``notify`` but ``never``, which
sends none; ``alerts`` sends nothing else. Every message is recorded in ``schedule_notifications`` (the outbox):
``sent``, ``failed`` with the reason, or ``dashboard`` when the schedule names
no chat or ``notify`` is ``never``. The text is code-written: the schedule,
the run's status, a summary snippet, its cost and the link to the task (``links``).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

from ..db import connection as db_connection, projects as db_projects, schedules as db_schedules
from . import cron
from .links import schedule_link, task_link
from .project_execution import format_usd

logger = logging.getLogger(__name__)

# (channel type, chat, text) -> whether it was sent, and why not.
Sender = Callable[[str, str, str], Awaitable[tuple[bool, str | None]]]

FAILURES_ALERT_AT = 3
STALE_LIMIT = timedelta(hours=24)
STALE_PERIODS = 3
# The shortest no-success window: three fires of the shortest rule without a gate (5 minutes apart). A gated
# schedule may fire every minute, where three periods would be three minutes: one slow gate or a couple of busy
# minutes, not a schedule gone quiet.
STALE_FLOOR = STALE_PERIODS * timedelta(minutes=5)
SNIPPET_CHARS = 300

# The final statuses of a run that ran something (a task, or a gate that failed).
RAN = ("completed", "failed", "stopped", "cancelled", "timed_out", "gate_failed")
STATUS_WORDS = {
    "completed": "完成", "failed": "失败", "stopped": "已停止", "cancelled": "已取消", "timed_out": "超时被停止",
    "gate_failed": "前置检查失败",
}


class Kind(str, Enum):
    RUN = "run"
    FAILURES_ALERT = "failures_alert"
    FAILURES_RECOVERED = "failures_recovered"
    STALE_ALERT = "stale_alert"
    STALE_RECOVERED = "stale_recovered"


def wants_run_message(notify: str, status: str) -> bool:
    if notify == "always":
        return status in RAN
    if notify == "failures":
        return status in db_schedules.FAILURES
    return False


def stale_deadline(rule: cron.CronRule, tz: Any, *, last_ok: datetime | None, first_run: datetime | None,
                   enabled_at: datetime, rule_changed_at: datetime | None = None) -> datetime | None:
    """When the stale alert is due: ``STALE_PERIODS`` periods of the rule, at least ``STALE_FLOOR`` and at most
    ``STALE_LIMIT``, after the first fire that could have been a success: the first fire after the latest of
    ``last_ok``, ``enabled_at`` (the schedule's creation or its last enabling) and ``rule_changed_at`` (its rule's
    or zone's last change). None before any run."""
    if last_ok is None and first_run is None:
        return None
    since = max(moment for moment in (last_ok, enabled_at, rule_changed_at) if moment is not None)
    start = cron.next_fire(rule, tz, since)
    if start is None:
        return None
    following = cron.next_fire(rule, tz, start)
    period = (following - start) if following is not None else STALE_LIMIT
    return start + min(max(STALE_PERIODS * period, STALE_FLOOR), STALE_LIMIT)


def snippet(text: str | None) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= SNIPPET_CHARS else f"{flat[:SNIPPET_CHARS]}…"


def _local(moment: datetime, zone_name: str) -> str:
    return moment.astimezone(cron.zone(zone_name)).strftime("%Y-%m-%d %H:%M %Z")


def run_text(schedule: Mapping[str, Any], project_name: str, run: Mapping[str, Any], *, base: str = "") -> str:
    when = _local(run.get("fired_at") or run["scheduled_for"], schedule["timezone"])
    lines = [f"定时任务「{schedule['name']}」（项目 {project_name}）{when} 的运行："
             f"{STATUS_WORDS.get(run['status'], run['status'])}"]
    if text := snippet(run.get("summary") or run.get("error")):
        lines.append(text)
    cost = f"花费 ${format_usd(float(run['cost_usd']))}" if run.get("cost_usd") is not None else "无花费"
    link = task_link(base, run["task_id"]) if run.get("task_id") else schedule_link(base, schedule)
    lines.append(f"{cost} · {link}")
    return "\n".join(lines)


def failures_alert_text(schedule: Mapping[str, Any], project_name: str, run: Mapping[str, Any] | None, *,
                        base: str = "") -> str:
    last = ""
    if run is not None:
        last = f"最近一次：{STATUS_WORDS.get(run['status'], run['status'])}"
        if text := snippet(run.get("summary") or run.get("error")):
            last += f" · {text}"
        last += "\n"
    return (f"告警：定时任务「{schedule['name']}」（项目 {project_name}）已连续失败 "
            f"{schedule['consecutive_failures']} 次。\n{last}{schedule_link(base, schedule)}")


def stale_alert_text(schedule: Mapping[str, Any], project_name: str, deadline: datetime, *, base: str = "") -> str:
    since = (f"上次成功：{_local(schedule['last_ok_at'], schedule['timezone'])}" if schedule.get("last_ok_at")
             else "还没有成功过")
    return (f"告警：定时任务「{schedule['name']}」（项目 {project_name}）到 {_local(deadline, schedule['timezone'])} "
            f"仍没有成功运行（{since}）。\n{schedule_link(base, schedule)}")


def recovered_text(schedule: Mapping[str, Any], project_name: str, run: Mapping[str, Any], kind: Kind, *,
                   base: str = "") -> str:
    what = "不再连续失败" if kind is Kind.FAILURES_RECOVERED else "又有成功的运行"
    when = _local(run.get("finished_at") or run["scheduled_for"], schedule["timezone"])
    return (f"恢复：定时任务「{schedule['name']}」（项目 {project_name}）{what}（{when}）。\n"
            f"{schedule_link(base, schedule)}")


class Notifier:
    """Sends a schedule's messages through ``sender`` (the server's channels; None: record them only), their links
    starting with ``dashboard_url`` (``links``; empty: relative)."""

    def __init__(self, *, sender: Sender | None, clock: Callable[[], datetime], dashboard_url: str = "") -> None:
        self._sender = sender
        self._clock = clock
        self._base = dashboard_url

    async def run_ended(self, run_id: str) -> None:
        """A run reached its final status: its message, then the failures alert or the recoveries it causes."""
        run = await db_schedules.get_run(run_id)
        if run is None:
            return
        schedule = await db_schedules.get_schedule(run["schedule_id"])
        if schedule is None:
            return
        project = await db_projects.get_project(schedule["project_id"])
        project_name = (project or {}).get("name") or schedule["project_id"]
        status = run["status"]
        if wants_run_message(schedule["notify"], status):
            await self._deliver(schedule, Kind.RUN, run_text(schedule, project_name, run, base=self._base),
                                run_id=run_id)
        now = self._clock()
        if status in db_schedules.FAILURES and schedule["consecutive_failures"] >= FAILURES_ALERT_AT:
            if await self._switch(schedule["id"], "failures_alert_at", now):
                await self._deliver(schedule, Kind.FAILURES_ALERT,
                                    failures_alert_text(schedule, project_name, run, base=self._base), run_id=run_id)
        if status in db_schedules.OK and await self._switch(schedule["id"], "failures_alert_at", None):
            await self._deliver(schedule, Kind.FAILURES_RECOVERED,
                                recovered_text(schedule, project_name, run, Kind.FAILURES_RECOVERED, base=self._base),
                                run_id=run_id)
        if status in db_schedules.OK and await self._switch(schedule["id"], "stale_alert_at", None):
            await self._deliver(schedule, Kind.STALE_RECOVERED,
                                recovered_text(schedule, project_name, run, Kind.STALE_RECOVERED, base=self._base),
                                run_id=run_id)

    async def check_stale(self, now: datetime) -> None:
        """Start the stale alert of each enabled schedule past its deadline. A schedule whose run is in progress
        (queued, checking its gate, or its task running: within its run timeout, as the pass before this one stops
        a run past it) is not stale; neither are the firings it makes skip."""
        pool = db_connection.get_pool()
        for schedule in await db_schedules.enabled_schedules():
            if schedule["stale_alert_at"] is not None:
                continue
            try:
                if await db_schedules.open_runs(pool, schedule["id"]):
                    continue
                rule, tz = cron.parse(schedule["cron"]), cron.zone(schedule["timezone"])
                first_run = await db_schedules.first_run_at(pool, schedule["id"])
                deadline = stale_deadline(rule, tz, last_ok=schedule["last_ok_at"], first_run=first_run,
                                          enabled_at=schedule["enabled_at"],
                                          rule_changed_at=schedule.get("rule_changed_at"))
                if deadline is None or now < deadline or not await self._switch(schedule["id"], "stale_alert_at", now):
                    continue
                project = await db_projects.get_project(schedule["project_id"])
                await self._deliver(schedule, Kind.STALE_ALERT,
                                    stale_alert_text(schedule, (project or {}).get("name") or "", deadline,
                                                     base=self._base))
            except Exception:
                logger.exception("could not check whether schedule %s has gone without a success", schedule["id"])

    @staticmethod
    async def _switch(schedule_id: str, column: str, at: datetime | None) -> bool:
        return await db_schedules.set_alert(db_connection.get_pool(), schedule_id, column, at)

    async def _deliver(self, schedule: Mapping[str, Any], kind: Kind, text: str, *, run_id: str | None = None) -> None:
        channel, chat = schedule.get("notify_channel"), schedule.get("notify_chat")
        status, error = "dashboard", None
        if channel and chat and schedule["notify"] != "never":
            if self._sender is None:
                status, error = "failed", "this server cannot send messages"
            else:
                try:
                    sent, error = await self._sender(channel, chat, text)
                except Exception as exc:  # a channel's failure is the message's, never the scheduler's
                    sent, error = False, f"{type(exc).__name__}: {exc}"
                status = "sent" if sent else "failed"
            if status == "failed":
                logger.warning("schedule %s: the %s message to %s could not be sent: %s", schedule["id"],
                               kind.value, channel, error)
        await db_schedules.insert_notification(
            db_connection.get_pool(), schedule_id=schedule["id"], run_id=run_id, kind=kind.value, channel=channel,
            chat=chat, text=text, status=status, error=error,
        )
