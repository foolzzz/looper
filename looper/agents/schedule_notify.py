"""Schedule run messages and alerts, sent through the configured channels.

``failures`` sends failed runs, ``always`` sends every run that ran something,
and ``alerts`` sends alerts and recoveries only. ``never`` records them only
on the dashboard. Skipped runs never get a run message.

Each alert starts once per streak: consecutive failures, no success within
the stale window, no success by the daily deadline, consecutive overlap
skips, and an extraction subscription's second check failing in consecutive
runs that otherwise succeeded (its items stored unchecked). ``alert_policy`` resolves their configurable thresholds with the
existing agent defaults. An OK run recovers the success alerts; starting a
new run recovers the overlap alert. Every message goes through the outbox,
recorded as ``sent``, ``failed`` or ``dashboard``.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

import asyncpg

from ..db import (connection as db_connection, managers as db_managers, projects as db_projects,
                  schedules as db_schedules, tasks as db_tasks)
from . import turn_metrics
from . import completion_events, cron, scheduler_settings
from .links import run_link, schedule_link, task_link
from .channels.base import PostOutcome
from .message import builtin_channel_id, parse_channel_id
from .project_execution import format_usd

logger = logging.getLogger(__name__)

# (channel type, chat, text) -> whether it was sent, and why not.
Sender = Callable[..., Awaitable[tuple[bool, str | None]]]

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
    # An extraction subscription's second check (``check_items``) failing run after run, and done again.
    CHECK_ALERT = "check_alert"
    CHECK_RECOVERED = "check_recovered"
    # A subscription's runs skipping their window (``skip_reason``) run after run, and one using its window again.
    SKIP_ALERT = "skip_alert"
    SKIP_RECOVERED = "skip_recovered"


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


async def _held_back(schedule: Mapping[str, Any], run_id: str | None) -> bool:
    """Whether a subscription's notice stays on the dashboard: its target is not confirmed for the configuration it
    has now, or the run it is about did not start with that target confirmed for it (``subscription_runs``' pin)."""
    if schedule.get("kind") != "subscription":
        return False
    from ..db import subscriptions as db_subs
    from . import subscriptions

    sub = schedule.get("subscription") or {}
    version = sub.get("target_confirmed_version")
    if not sub.get("target_confirmed_at") or not version or version != await subscriptions.config_version(schedule):
        return True
    stream = await db_subs.get_run(run_id) if run_id else None
    return stream is not None and (
        stream["config_version"] != version
        or (stream["target_channel"], stream["target_chat"]) != (schedule.get("notify_channel"), schedule.get("notify_chat")))


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


def _bounded_lines(text: str, limit: int) -> str:
    """Keep complete lines, including room for the truncation marker."""
    lines = text.strip().splitlines()
    if len(text.strip()) <= limit:
        return "\n".join(lines)
    kept: list[str] = []
    size = 1  # The ellipsis, plus a newline for every retained line.
    for line in lines:
        if size + len(line) + 1 > limit:
            break
        kept.append(line)
        size += len(line) + 1
    return "\n".join([*kept, "…"])


def _report_summary(text: str) -> str:
    lines = [line for line in text.splitlines() if line.strip()]
    summary = "\n".join(lines[:12])
    if len(lines) > 12 or len(summary) > 1200:
        return summary[:1199] + "…"
    return summary


def _section(text: str, title: str) -> str | None:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.strip() == f"## {title}":
            end = next((i for i in range(index + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
            return "\n".join(lines[index + 1:end]).strip()
    return None


def _deliverable_text(schedule: Mapping[str, Any], run: Mapping[str, Any]) -> str:
    summary = run.get("summary") or ""
    if run["status"] in db_schedules.FAILURES:
        reason = _section(summary, "Why it ended")
        if reason is None:
            reason = run.get("error") or "暂无错误详情。"
        reason = re.sub(r"(?m)^ {0,3}#{1,6}\s+", "", reason)
        return f"今天的「{schedule['name']}」没能按时生成。\n原因：\n{_bounded_lines(reason, 800)}"
    text = _section(summary, "Delivered")
    if text is None:
        lines = summary.strip().splitlines()
        while lines and (not lines[0].strip() or lines[0].startswith("## ")):
            lines.pop(0)
        text = "\n".join(lines)
    return _bounded_lines(text, 4000)


def _local(moment: datetime, zone_name: str) -> str:
    return moment.astimezone(cron.zone(zone_name)).strftime("%Y-%m-%d %H:%M %Z")


def run_text(schedule: Mapping[str, Any], project_name: str, run: Mapping[str, Any], *, base: str = "") -> str:
    if schedule.get("kind") == "subscription":
        # What it made or why it failed can carry what the messages said: the notice says only that it failed.
        when = _local(run.get("fired_at") or run["scheduled_for"], schedule["timezone"])
        return (f"订阅「{schedule['name']}」（项目 {project_name}）{when} 的运行："
                f"{STATUS_WORDS.get(run['status'], run['status'])}，详情见 Dashboard。\n{run_link(base, schedule, run['id'])}")
    if schedule.get("kind") == "command":
        return command_run_text(schedule, project_name, run, base=base)
    if schedule.get("notify_format") == "deliverable":
        return _deliverable_text(schedule, run)
    when = _local(run.get("fired_at") or run["scheduled_for"], schedule["timezone"])
    lines = [f"定时任务「{schedule['name']}」（项目 {project_name}）{when} 的运行："
             f"{STATUS_WORDS.get(run['status'], run['status'])}"]
    if text := _report_summary(run.get("summary") or run.get("error") or ""):
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
                        base: str = "", context_max_tokens: int = scheduler_settings.SESSION_CONTEXT_MAX_TOKENS) -> str:
    last = ""
    if run is not None:
        last = f"最近一次：{STATUS_WORDS.get(run['status'], run['status'])}"
        if (text := snippet(run.get("summary") or run.get("error"))) and schedule.get("kind") != "subscription":
            last += f" · {text}"
        last += "\n"
        if run.get("failure_kind") == "task_budget":
            last += "原因：已达到任务预算上限。\n"
            context = run.get("planner_context_tokens")
            if context is None:
                context = schedule.get("planner_session_context_tokens")
            if context is not None and context > context_max_tokens:
                last += f"Planner 会话上下文为 {context} tokens，超过 {context_max_tokens} tokens 的上限。\n"
    return (f"告警：定时任务「{schedule['name']}」（项目 {project_name}）已连续失败 "
            f"{schedule['consecutive_failures']} 次。\n{last}{schedule_link(base, schedule)}")


def check_alert_text(schedule: Mapping[str, Any], project_name: str, failures: int, *, base: str = "") -> str:
    return (f"告警：订阅「{schedule['name']}」（项目 {project_name}）的第二道检查已连续 {failures} 次没有完成，"
            f"这几次整理出的条目都未经复核入库。\n{schedule_link(base, schedule)}")


def check_recovered_text(schedule: Mapping[str, Any], project_name: str, run: Mapping[str, Any], *,
                         base: str = "") -> str:
    when = _local(run.get("finished_at") or run["scheduled_for"], schedule["timezone"])
    return (f"恢复：订阅「{schedule['name']}」（项目 {project_name}）的第二道检查又完成了复核（{when}）。\n"
            f"{schedule_link(base, schedule)}")


SKIP_WORDS = {"blocked_by_hook": "提示词被本机 Claude Code 的 hook 拦下", "invalid_reply": "处理器的回复两次都读不了"}


def skip_alert_text(schedule: Mapping[str, Any], project_name: str, skips: int, reason: str, *,
                    base: str = "") -> str:
    return (f"告警：订阅「{schedule['name']}」（项目 {project_name}）已连续 {skips} 次跳过了它的消息窗口"
            f"（最近一次：{SKIP_WORDS.get(reason, reason)}），这些消息没有整理。\n{schedule_link(base, schedule)}")


def skip_recovered_text(schedule: Mapping[str, Any], project_name: str, run: Mapping[str, Any], *,
                        base: str = "") -> str:
    when = _local(run.get("finished_at") or run["scheduled_for"], schedule["timezone"])
    return (f"恢复：订阅「{schedule['name']}」（项目 {project_name}）又整理了它的消息窗口（{when}）。\n"
            f"{schedule_link(base, schedule)}")


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

    def __init__(self, *, sender: Sender | None, clock: Callable[[], datetime], dashboard_url: str = "",
                 poster: Any = None) -> None:
        self._sender = sender
        self._clock = clock
        self._base = dashboard_url
        # The channels' ``post`` (typed outcome): what a completion event's run message goes through.
        self._poster = poster

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
        # A subscription's successful run already pushed (or kept) its output: the message is the output itself.
        if wants_run_message(schedule["notify"], status) and not (
                schedule["kind"] == "subscription" and status in db_schedules.SUCCESS):
            text = run_text(schedule, project_name, run, base=self._base)
            recorded = await self._record_end(schedule, run, text)
            if completion_events.waits_for_turn(recorded):
                completion_events.poke()  # the default Manager's completion turn tells it, or its fallback does
            elif recorded is None or recorded.send_fallback:
                if await self._deliver(schedule, Kind.RUN, text, run_id=run_id) != "failed":
                    await completion_events.mark_sent(recorded)
        now = self._clock()
        if status in db_schedules.FAILURES and schedule["consecutive_failures"] >= alert_policy(schedule).failures_after:
            if await self._switch(schedule["id"], "failures_alert_at", now):
                await self._deliver(schedule, Kind.FAILURES_ALERT,
                                    failures_alert_text(schedule, project_name, run, base=self._base,
                                                        context_max_tokens=await scheduler_settings.session_context_max_tokens()),
                                    run_id=run_id)
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
        if schedule["kind"] == "subscription":
            await self._check_streak(schedule, project_name, run, now)
            await self._skip_streak(schedule, project_name, run, now)

    async def _skip_streak(self, schedule: Mapping[str, Any], project_name: str, run: Mapping[str, Any],
                           now: datetime) -> None:
        """A subscription's runs that skipped their window: after as many in a row as the schedule's failure alert
        waits for (3 by default; a run that did not complete neither ends nor adds to them), alert once; the next
        completed run that did not skip recovers."""
        from ..db import subscriptions as db_subs

        pool = db_connection.get_pool()
        stream = await db_subs.get_run(run["id"], db=pool)
        if stream is None or run["status"] != "completed":
            return
        if stream["outcome"] == "skipped":
            threshold = alert_policy(schedule).failures_after
            skips = await db_subs.skip_streak(pool, schedule["id"], limit=threshold)
            if skips >= threshold and await db_subs.set_skip_alert(pool, schedule["id"], now):
                await self._deliver(schedule, Kind.SKIP_ALERT,
                                    skip_alert_text(schedule, project_name, skips, stream["skip_reason"],
                                                    base=self._base), run_id=run["id"])
        elif await db_subs.set_skip_alert(pool, schedule["id"], None):
            await self._deliver(schedule, Kind.SKIP_RECOVERED,
                                skip_recovered_text(schedule, project_name, run, base=self._base), run_id=run["id"])

    async def _check_streak(self, schedule: Mapping[str, Any], project_name: str, run: Mapping[str, Any],
                            now: datetime) -> None:
        """An extraction run's second check: after as many failed checks in a row as the schedule's failure alert
        waits for (3 by default; skipped checks neither end nor add to them), alert once; a check done again
        recovers. The run itself succeeded either way: its items were stored, unchecked."""
        from ..db import subscriptions as db_subs

        pool = db_connection.get_pool()
        stream = await db_subs.get_run(run["id"], db=pool)
        check = (stream or {}).get("check_status")
        if check == "failed":
            threshold = alert_policy(schedule).failures_after
            failures = await db_subs.check_failures(pool, schedule["id"], limit=threshold)
            if failures >= threshold and await db_subs.set_check_alert(pool, schedule["id"], now):
                await self._deliver(schedule, Kind.CHECK_ALERT,
                                    check_alert_text(schedule, project_name, failures, base=self._base),
                                    run_id=run["id"])
        elif check == "checked" and await db_subs.set_check_alert(pool, schedule["id"], None):
            await self._deliver(schedule, Kind.CHECK_RECOVERED,
                                check_recovered_text(schedule, project_name, run, base=self._base), run_id=run["id"])

    @staticmethod
    async def _record_end(schedule: Mapping[str, Any], run: Mapping[str, Any], text: str):
        """The completion event of a run that ended its task and whose message goes to a chat; None for any other
        run (its message is sent as before)."""
        from .schedules import notify_target_kind

        channel, chat = schedule.get("notify_channel"), schedule.get("notify_chat")
        if not (channel and chat and run.get("task_id") and run["status"] in db_tasks.PLANNER_END_STATUSES):
            return None
        try:
            target = await notify_target_kind(channel, chat)
        except Exception:
            logger.exception("schedule %s: could not tell who its notification target is", schedule["id"])
            return None
        if target is None:
            return None
        return await completion_events.record_schedule_run(
            db_managers.DEFAULT_MANAGER_ID, task_id=run["task_id"], run_id=run["id"], target=target,
            channel_id=builtin_channel_id(channel), chat=chat, text=text, name=schedule["name"], status=run["status"])

    async def post_run_message(self, run_id: str, channel_id: str, chat: str, text: str, manager_event_id: int, *,
                               send: bool = True, reply: bool = False) -> tuple[PostOutcome, str | None]:
        """A run's message told by its completion event ``manager_event_id`` (the Manager's reply, or the code notice
        it falls back to), sent to the chat the event recorded (``channel_id``, ``chat``: never one the schedule
        names later) through the channels' ``post``, and recorded in the outbox, the row naming the event. The caller
        wrote the event ``sending`` first. Not ``send``: the chat got it in a reply the Manager sent for other tasks
        too, so it is only recorded, as sent, once. ``reply``: the Manager's reply (its own idempotency key, so a
        refused reply never holds back the notice that follows it).

        Answers what happened: ``SENT``; ``REJECTED`` (it never reached a channel: the run cannot be read, the chat is
        no longer an allowed target, no channel can post; or the channel refused it before any part went) or
        ``UNKNOWN`` (the channel raised or did not answer, or a part went and a later one failed: never sent again).
        A message that went but whose outbox row could not be written is ``SENT``: the row is written once more."""
        from .schedules import notify_targets
        from .subscription_runs import push_target

        parsed = parse_channel_id(channel_id)
        try:
            run = await db_schedules.get_run(run_id)
            schedule = await db_schedules.get_schedule(run["schedule_id"]) if run is not None else None
        except Exception as exc:
            return PostOutcome.REJECTED, f"the run could not be read ({type(exc).__name__})"
        if schedule is None or parsed is None:
            return PostOutcome.REJECTED, "the run, its schedule or its channel is gone"
        channel = parsed.channel_type
        if not send:
            await self._record_once(schedule, run_id, channel, chat, text, "sent", None, manager_event_id)
            return PostOutcome.SENT, None
        try:
            allowed = any((t["channel"], t["chat"]) == (channel, chat) for t in await notify_targets())
        except Exception as exc:
            return PostOutcome.REJECTED, f"the allowed chats could not be read ({type(exc).__name__})"
        if not allowed:
            outcome, error = PostOutcome.REJECTED, "the chat is no longer an allowed notification target"
        elif self._poster is None:
            outcome, error = PostOutcome.REJECTED, "this server cannot send messages"
        else:
            try:
                with turn_metrics.proactive_send(
                        turn_metrics.ProactiveSource.SCHEDULE, run_id, schedule.get("manager_id")):
                    receipt = await self._poster(channel_id, push_target(channel, chat), text,
                                                 idempotency_key=f"looper-{'reply' if reply else 'notice'}-"
                                                                 f"{manager_event_id}")
            except Exception as exc:  # in the channel's hands: it may have gone
                outcome, error = PostOutcome.UNKNOWN, f"{type(exc).__name__}: the channel did not say"
            else:
                outcome, error = receipt.result, receipt.error
                if outcome is PostOutcome.REJECTED and receipt.external_ids:  # a part went: never sent again
                    outcome, error = PostOutcome.UNKNOWN, f"part of it was sent: {receipt.error}"
        if outcome is not PostOutcome.SENT:
            logger.warning("schedule %s: run %s's message to %s is %s", schedule["id"], run_id, channel,
                           outcome.value)
        await self._record_once(schedule, run_id, channel, chat, text,
                                "sent" if outcome is PostOutcome.SENT else "failed",
                                None if outcome is PostOutcome.SENT else error, manager_event_id)
        return outcome, error

    @staticmethod
    async def _record_once(schedule: Mapping[str, Any], run_id: str, channel: str, chat: str, text: str, status: str,
                           error: str | None, manager_event_id: int) -> None:
        """The outbox row of an event's message, once per event and status; a write that fails is tried once more and
        then logged (the message is what it is: never sent again for its row)."""
        for attempt in range(2):
            try:
                await db_schedules.insert_notification(
                    db_connection.get_pool(), schedule_id=schedule["id"], run_id=run_id, kind=Kind.RUN.value,
                    channel=channel, chat=chat, text=text, status=status, error=error,
                    manager_event_id=manager_event_id, once=True)
                return
            except asyncpg.ForeignKeyViolationError:
                return  # deleted since: its outbox went with it
            except Exception:
                if attempt:
                    logger.exception("schedule %s: run %s's outbox row was not written", schedule["id"], run_id)

    async def run_target_ok(self, run_id: str, channel_id: str, chat: str) -> bool:
        """Whether the run's schedule still sends its messages to ``chat`` through ``channel_id`` (the target its
        completion event recorded), and may (the chat is still an allowed target)."""
        from .schedules import notify_targets

        run = await db_schedules.get_run(run_id)
        schedule = await db_schedules.get_schedule(run["schedule_id"]) if run is not None else None
        if schedule is None or schedule["notify"] == "never":
            return False
        channel, target = schedule.get("notify_channel"), schedule.get("notify_chat")
        if not (channel and target and builtin_channel_id(channel) == channel_id and target == chat):
            return False
        return any((t["channel"], t["chat"]) == (channel, chat) for t in await notify_targets())

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
                    if (error := snippet(run.get("error"))) and schedule.get("kind") != "subscription":
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

    async def _deliver(self, schedule: Mapping[str, Any], kind: Kind, text: str, *, run_id: str | None = None) -> str:
        """Record ``sent``/``failed`` only after calling the sender; otherwise keep it on the dashboard."""
        from .schedules import notify_targets

        if schedule.get("kind") == "subscription":
            # Read afresh: the target, its confirmation and whether it notifies may have changed since the caller
            # read the schedule (this run's end, an alert's check), and a notice goes only where it may now.
            schedule = await db_schedules.get_schedule(schedule["id"]) or schedule
        channel, chat = schedule.get("notify_channel"), schedule.get("notify_chat")
        status, error = "dashboard", None
        dashboard_alert = kind is not Kind.RUN and schedule.get("notify_format") == "deliverable"
        # A subscription's notices (failures and alerts too) go only where its pushes may: through the same check.
        unconfirmed = await _held_back(schedule, run_id)
        if channel and chat and schedule["notify"] != "never" and not dashboard_alert and not unconfirmed:
            if not any((t["channel"], t["chat"]) == (channel, chat) for t in await notify_targets()):
                error = ("the notification target is unavailable: "
                         "its allowlist entry or chat binding was removed")
            elif self._sender is None:
                error = "this server cannot send messages"
            else:
                try:
                    from .subscription_runs import push_target

                    # Keep the scheduler's existing notification target convention.
                    with turn_metrics.proactive_send(turn_metrics.ProactiveSource.SCHEDULE, run_id,
                                                     schedule.get("manager_id"), group=push_target(channel, chat).group):
                        sent, error = await self._sender(builtin_channel_id(channel), chat, text)
                except Exception as exc:  # a channel's failure is the message's, never the scheduler's
                    sent, error = False, f"{type(exc).__name__}: {exc}"
                status = "sent" if sent else "failed"
            if status == "failed" or error:
                logger.warning("schedule %s: the %s message to %s could not be sent", schedule["id"], kind.value, channel)
        try:
            await db_schedules.insert_notification(
                db_connection.get_pool(), schedule_id=schedule["id"], run_id=run_id, kind=kind.value, channel=channel,
                chat=chat, text=text, status=status, error=error,
            )
        except asyncpg.ForeignKeyViolationError:
            # Deleted since the message was written: its outbox went with it.
            logger.info("schedule %s was deleted: its %s message is not recorded", schedule["id"], kind.value)
        return status
