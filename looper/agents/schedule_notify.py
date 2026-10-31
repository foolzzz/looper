"""Schedule run messages and alerts, sent through the configured channels.

``failures`` sends failed runs, ``always`` sends every run that ran something,
and ``alerts`` sends alerts and recoveries only. ``never`` records them only
on the dashboard. Skipped runs never get a run message.

Each alert starts once per streak: consecutive failures, no success within
the stale window, no success by the daily deadline, and consecutive overlap
skips. ``alert_policy`` resolves their configurable thresholds with the
existing agent defaults. An OK run recovers the success alerts; starting a
new run recovers the overlap alert. Every message goes through the outbox,
recorded as ``sent``, ``failed`` or ``dashboard``.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

import asyncpg

from ..db import connection as db_connection, projects as db_projects, schedules as db_schedules
from . import cron
from .links import run_link, schedule_link, task_link
from .message import builtin_channel_id
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
RAN = ("completed", "failed", "stopped", "cancelled", "timed_out", "gate_failed", "succeeded", "interrupted")
STATUS_WORDS = {
    "completed": "完成", "failed": "失败", "stopped": "已停止", "cancelled": "已取消", "timed_out": "超时被停止",
    "gate_failed": "前置检查失败", "succeeded": "成功", "interrupted": "中断",
}


class Kind(str, Enum):
    RUN = "run"
    FAILURES_ALERT = "failures_alert"
    FAILURES_RECOVERED = "failures_recovered"
    STALE_ALERT = "stale_alert"
    STALE_RECOVERED = "stale_recovered"
    DEADLINE_ALERT = "deadline_alert"
    DEADLINE_RECOVERED = "deadline_recovered"
    OVERLAP_ALERT = "overlap_alert"
    OVERLAP_RECOVERED = "overlap_recovered"


@dataclass(frozen=True)
class AlertPolicy:
    failures_after: int
    stale_after: timedelta | None
    overlap_after: int


def alert_policy(schedule: Mapping[str, Any]) -> AlertPolicy:
    """Resolve NULL defaults once, preserving the existing agent alert behavior."""
    failures = schedule.get("alert_failures_after")
    stale = schedule.get("alert_stale_after_seconds")
    overlap = schedule.get("alert_overlap_after")
    return AlertPolicy(
        FAILURES_ALERT_AT if failures is None else failures,
        None if stale is None else timedelta(seconds=stale),
        (10 if schedule.get("kind") == "command" else 0) if overlap is None else overlap,
    )


def wants_run_message(notify: str, status: str) -> bool:
    if notify == "always":
        return status in RAN
    if notify == "failures":
        return status in db_schedules.FAILURES
    return False


def stale_deadline(rule: cron.CronRule, tz: Any, *, last_ok: datetime | None, first_run: datetime | None,
                   enabled_at: datetime, rule_changed_at: datetime | None = None,
                   window: timedelta | None = None) -> datetime | None:
    """When the stale alert is due: ``STALE_PERIODS`` periods of the rule, at least ``STALE_FLOOR`` and at most
    ``STALE_LIMIT``, after the first fire that could have been a success: the first fire after the latest of
    ``last_ok``, ``enabled_at`` (the schedule's creation or its last enabling) and ``rule_changed_at`` (its rule's
    or zone's last change). An explicit ``window`` replaces the period formula. None before any run."""
    if last_ok is None and first_run is None:
        return None
    since = max(moment for moment in (last_ok, enabled_at, rule_changed_at) if moment is not None)
    start = cron.next_fire(rule, tz, since)
    if start is None:
        return None
    if window is not None:
        return start + window
    following = cron.next_fire(rule, tz, start)
    period = (following - start) if following is not None else STALE_LIMIT
    return start + min(max(STALE_PERIODS * period, STALE_FLOOR), STALE_LIMIT)


def snippet(text: str | None) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= SNIPPET_CHARS else f"{flat[:SNIPPET_CHARS]}…"


def _local(moment: datetime, zone_name: str) -> str:
    return moment.astimezone(cron.zone(zone_name)).strftime("%Y-%m-%d %H:%M %Z")


def run_text(schedule: Mapping[str, Any], project_name: str, run: Mapping[str, Any], *, base: str = "") -> str:
    if schedule.get("kind") == "command":
        return command_run_text(schedule, project_name, run, base=base)
    when = _local(run.get("fired_at") or run["scheduled_for"], schedule["timezone"])
    lines = [f"定时任务「{schedule['name']}」（项目 {project_name}）{when} 的运行："
             f"{STATUS_WORDS.get(run['status'], run['status'])}"]
    if text := snippet(run.get("summary") or run.get("error")):
        lines.append(text)
    cost = f"花费 ${format_usd(float(run['cost_usd']))}" if run.get("cost_usd") is not None else "无花费"
    link = task_link(base, run["task_id"]) if run.get("task_id") else schedule_link(base, schedule)
    lines.append(f"{cost} · {link}")
    return "\n".join(lines)


def command_run_text(schedule: Mapping[str, Any], project_name: str, run: Mapping[str, Any], *,
                     base: str = "") -> str:
    from .scheduler import _fenced

    when = _local(run["scheduled_for"], schedule["timezone"])
    status = STATUS_WORDS.get(run["status"], run["status"])
    if run["status"] in db_schedules.FAILURES:
        if run.get("exit_code") is not None:
            status += f"（退出码 {run['exit_code']}）"
        elif run.get("exit_signal") is not None:
            status += f"（信号 {run['exit_signal']}）"
    lines = [f"定时任务「{schedule['name']}」（项目 {project_name}）{when} 的运行：{status}"]
    start = run.get("started_at") or run.get("fired_at") or run["scheduled_for"]
    end = run.get("finished_at") or start
    seconds = max(0, int((end - start).total_seconds()))
    if seconds < 60:
        elapsed = f"{seconds} 秒"
    elif seconds < 3600:
        elapsed = f"{seconds // 60} 分 {seconds % 60} 秒"
    else:
        elapsed = f"{seconds // 3600} 小时 {seconds % 3600 // 60} 分"
    timing = f"耗时 {elapsed}"
    if run["trigger"] == "catch_up":
        timing += f" · 补跑，代替错过的 {run['missed']} 次"
    elif run["trigger"] == "manual":
        timing += " · 手动运行"
    lines.append(timing)
    if run.get("error"):
        lines.append(run["error"][:SNIPPET_CHARS])
    if run.get("log_tail"):
        lines.append(f"日志末尾：\n{_fenced(run['log_tail'][-1500:], 1500)}")
    lines.append(run_link(base, schedule, run["id"]))
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
        if status in db_schedules.FAILURES and schedule["consecutive_failures"] >= alert_policy(schedule).failures_after:
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
        if status in db_schedules.OK and await self._switch(schedule["id"], "deadline_alert_at", None):
            await self._deliver(schedule, Kind.DEADLINE_RECOVERED,
                                recovered_text(schedule, project_name, run, Kind.DEADLINE_RECOVERED, base=self._base),
                                run_id=run_id)

    async def check_stale(self, now: datetime) -> None:
        """Start the stale alert of each enabled schedule past its deadline. A schedule whose run is in progress
        (queued, checking its gate, or its task running: within its run timeout, as the pass before this one stops
        a run past it) is not stale; neither are the firings it makes skip, nor is a schedule whose run went well
        after the pass read it (``start_stale_alert``)."""
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
                                          rule_changed_at=schedule.get("rule_changed_at"),
                                          window=alert_policy(schedule).stale_after)
                if deadline is None or now < deadline or not await db_schedules.start_stale_alert(
                        pool, schedule["id"], now, last_ok_at=schedule["last_ok_at"]):
                    continue
                project = await db_projects.get_project(schedule["project_id"])
                await self._deliver(schedule, Kind.STALE_ALERT,
                                    stale_alert_text(schedule, (project or {}).get("name") or "", deadline,
                                                     base=self._base))
            except Exception:
                logger.exception("could not check whether schedule %s has gone without a success", schedule["id"])

    async def check_deadlines(self, now: datetime) -> None:
        """Alert once when a deadline window contained a fire but no successful run."""
        pool = db_connection.get_pool()
        for schedule in await db_schedules.deadline_candidates():
            try:
                hh, mm = schedule["success_deadline"].split(":")
                rule, tz = cron.parse(f"{int(mm)} {int(hh)} * * *"), cron.zone(schedule["timezone"])
                deadline = cron.last_fire(rule, tz, now)
                if deadline is None:
                    continue
                previous = cron.last_fire(rule, tz, deadline - timedelta(microseconds=1))
                if previous is None:
                    continue
                since = max(previous, schedule["enabled_at"], schedule.get("rule_changed_at") or previous)
                if since >= deadline or not cron.fires_between(cron.parse(schedule["cron"]), tz, since, deadline,
                                                               limit=1):
                    continue
                if schedule["last_ok_at"] is not None and schedule["last_ok_at"] > previous:
                    continue
                opened = await db_schedules.open_runs(pool, schedule["id"])
                recent = [] if opened else await db_schedules.recent_runs(pool, schedule["id"], limit=1)
                if not await db_schedules.start_deadline_alert(pool, schedule["id"], now,
                                                              last_ok_at=schedule["last_ok_at"]):
                    continue
                last_ok = (f"上次成功：{_local(schedule['last_ok_at'], schedule['timezone'])}"
                           if schedule["last_ok_at"] is not None else "还没有成功过")
                lines = [f"告警：定时任务「{schedule['name']}」（项目 {schedule['project_name']}）到 "
                         f"{_local(deadline, schedule['timezone'])}（{schedule['timezone']}）仍没有成功运行（{last_ok}）。"]
                if opened:
                    run = opened[0]
                    lines.append(f"仍在运行中，自 {_local(run.get('fired_at') or run['scheduled_for'], schedule['timezone'])} 起。")
                elif recent:
                    run = recent[0]
                    last = f"最近一次：{STATUS_WORDS.get(run['status'], run['status'])}"
                    if error := snippet(run.get("error")):
                        last += f" · {error}"
                    lines.append(last)
                lines.append(schedule_link(self._base, schedule))
                await self._deliver(schedule, Kind.DEADLINE_ALERT, "\n".join(lines))
            except Exception:
                logger.exception("could not check success deadline for schedule %s", schedule["id"])

    async def overlap_alert(self, schedule_id: str, run_id: str, *, count: int | None = None,
                            running: Mapping[str, Any] | None = None) -> None:
        schedule = await db_schedules.get_schedule(schedule_id)
        if schedule is None:
            return
        project = await db_projects.get_project(schedule["project_id"])
        if count is None:
            count = schedule["consecutive_overlaps"]
            opened = await db_schedules.open_runs(db_connection.get_pool(), schedule_id)
            running = next((run for run in opened if run["status"] != db_schedules.QUEUED), None)
        text = (f"告警：定时任务「{schedule['name']}」（项目 {(project or {}).get('name') or schedule['project_id']}）"
                f"已连续 {count} 次因为上一次运行还没结束而跳过")
        if running is not None:
            text += (f"；正在跑的那次自 {_local(running.get('fired_at') or running['scheduled_for'], schedule['timezone'])} "
                     f"开始，可能卡住了。\n{run_link(self._base, schedule, running['id'])}")
        else:
            text += f"。\n{schedule_link(self._base, schedule)}"
        await self._deliver(schedule, Kind.OVERLAP_ALERT, text, run_id=run_id)

    async def overlap_recovered(self, schedule_id: str, run_id: str) -> None:
        schedule = await db_schedules.get_schedule(schedule_id)
        if schedule is None:
            return
        project = await db_projects.get_project(schedule["project_id"])
        text = (f"恢复：定时任务「{schedule['name']}」（项目 {(project or {}).get('name') or schedule['project_id']}）"
                f"又开始了新的运行。\n{schedule_link(self._base, schedule)}")
        await self._deliver(schedule, Kind.OVERLAP_RECOVERED, text, run_id=run_id)

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
                    sent, error = await self._sender(builtin_channel_id(channel), chat, text)
                except Exception as exc:  # a channel's failure is the message's, never the scheduler's
                    sent, error = False, f"{type(exc).__name__}: {exc}"
                status = "sent" if sent else "failed"
            if status == "failed":
                logger.warning("schedule %s: the %s message to %s could not be sent: %s", schedule["id"],
                               kind.value, channel, error)
        try:
            await db_schedules.insert_notification(
                db_connection.get_pool(), schedule_id=schedule["id"], run_id=run_id, kind=kind.value, channel=channel,
                chat=chat, text=text, status=status, error=error,
            )
        except asyncpg.ForeignKeyViolationError:
            # Deleted since the message was written: its outbox went with it.
            logger.info("schedule %s was deleted: its %s message is not recorded", schedule["id"], kind.value)
