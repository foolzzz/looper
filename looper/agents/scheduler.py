"""The scheduler in ``looper serve``: it fires due schedules (scheduled-tasks RFC §6, D5–D7).

Every ``TICK_SECONDS`` (and once at start, which is how fires missed while the
server was down or the computer slept are handled), one pass:

1. Runs whose task ended get their final status, summary and cost (the
   TaskRouter also reports each task end, ``task_ended``, so this pass only
   catches what that missed, such as a task a server start stopped). A run
   past its schedule's ``run_timeout_seconds`` is recorded ``timed_out`` and
   its task stopped. A queued run whose previous task has ended starts.
2. Each enabled schedule whose ``next_fire_at`` has come is handled in a
   transaction that locks its row (``FOR UPDATE SKIP LOCKED``), so two servers
   on one database fire it once. The fires due since ``next_fire_at`` decide
   (``decide``): one on time fires as ``scheduled``; missed ones fire once as
   ``catch_up`` (``missed`` says how many) or, with catch-up ``skip``, fire
   only when the latest was within ``GRACE`` and are recorded
   ``skipped_missed`` otherwise. ``last_fired_at`` becomes the fire time
   handled and ``next_fire_at`` the next one after now.

A firing while one of the schedule's tasks is still active is recorded
``skipped_overlap``, or, with overlap ``queue``, waits as the one ``queued``
run. Otherwise it creates the task (the schedule's requirement plus the
code-written run context, ``run_context``; its profile overrides; its own
spend cap) and starts it at once with ``TASK_START``: enabling the schedule was
the approval (D6), and the TaskRouter's start path applies the project's
concurrency limit. "Run now" is the same firing with ``trigger`` manual,
leaving ``next_fire_at`` alone.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Protocol

import asyncpg

from ..db import agent_setting_layers as db_layers, connection as db_connection, llm_records as db_llm_records
from ..db import projects as db_projects, schedules as db_schedules, tasks as db_tasks
from . import cron
from .message import TASK_ROUTER_ADDRESS, AgentAddress, MessageEnvelope, MessageType

logger = logging.getLogger(__name__)

SCHEDULER_ADDRESS = AgentAddress(agent_type="scheduler", agent_id="scheduler")
TICK_SECONDS = 15.0
# How late the latest of several missed fires may be for catch-up "skip" to still run it.
GRACE = timedelta(minutes=2)
# The most missed fires counted; more are reported as at least this many.
MISSED_COUNT_LIMIT = 10_000
CONTEXT_RUNS = 3
SUMMARY_CHARS = 300
# A run's task still ready this long after it fired was created but never started (the server stopped in between).
START_RETRY_AFTER = timedelta(minutes=1)

# A task's final status, as its run's.
_OUTCOMES = {"completed": "completed", "failed": "failed", "stopped": "stopped", "cancelled": "cancelled"}
# A schedule's task that still counts as in progress: queued, running, or created and about to start.
_BUSY_STATUSES = (*db_tasks.ACTIVE_STATUSES, db_tasks.READY)


class Trigger(str, Enum):
    SCHEDULED = "scheduled"
    CATCH_UP = "catch_up"
    MANUAL = "manual"


class Action(str, Enum):
    NONE = "none"  # nothing is due
    FIRE = "fire"
    SKIP = "skip"  # missed fires skipped (catch-up "skip", the latest too long ago)


@dataclass(frozen=True)
class Firing:
    trigger: Trigger
    scheduled_for: datetime
    missed: int = 0  # fires missed before this one
    missed_since: datetime | None = None  # the first of them


@dataclass(frozen=True)
class Decision:
    action: Action
    firing: Firing | None = None


def decide(due: list[datetime], *, latest: datetime | None, now: datetime, catch_up: str) -> Decision:
    """What a schedule does with the fires due since its ``next_fire_at`` (``due``, oldest first; ``latest`` the
    newest of all of them, which ``due`` may not reach when it was cut at ``MISSED_COUNT_LIMIT``).

    One fire within ``GRACE`` is on time. Anything else was missed: catch-up ``once`` fires once for all of
    them; ``skip`` fires on time for the latest when it is within ``GRACE`` and skips them all otherwise.
    """
    if not due or latest is None:
        return Decision(Action.NONE)
    on_time = now - latest <= GRACE
    if len(due) == 1 and on_time:
        return Decision(Action.FIRE, Firing(Trigger.SCHEDULED, latest))
    if catch_up == "once":
        return Decision(Action.FIRE, Firing(Trigger.CATCH_UP, latest, missed=len(due), missed_since=due[0]))
    if on_time:
        return Decision(Action.FIRE, Firing(Trigger.SCHEDULED, latest, missed=len(due) - 1, missed_since=due[0]))
    return Decision(Action.SKIP, Firing(Trigger.SCHEDULED, latest, missed=len(due), missed_since=due[0]))


# --- The run context (model-visible) ---

_STATUS_WORDS = {
    "completed": "完成", "failed": "失败", "stopped": "已停止", "cancelled": "已取消", "timed_out": "超时被停止",
    "skipped_overlap": "跳过（上一次运行还没结束）", "skipped_missed": "跳过（错过了触发时刻）",
}
_TRIGGER_WORDS = {Trigger.SCHEDULED: "按时触发", Trigger.CATCH_UP: "补跑", Trigger.MANUAL: "手动「立即运行」"}


def _local(moment: datetime, tz: Any) -> str:
    return moment.astimezone(tz).strftime("%Y-%m-%d %H:%M %Z")


def _snippet(text: str | None) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= SUMMARY_CHARS else f"{flat[:SUMMARY_CHARS]}…"


def run_context(schedule: Mapping[str, Any], firing: Firing, recent: list[Mapping[str, Any]], *,
                description: str) -> str:
    """The section the scheduler appends to a run's requirement: which schedule and firing this is, the fires it
    stands in for, the latest runs' outcomes, and that nobody is there to answer."""
    tz = cron.zone(schedule["timezone"])
    lines = [
        "## 定时运行上下文",
        "",
        f"本节由 Looper 生成：这是定时任务「{schedule['name']}」的一次运行。",
        f"- 时间规则：`{schedule['cron']}`（{description}）",
        f"- 本次{'触发' if firing.trigger is Trigger.MANUAL else '应触发'}的时刻：{_local(firing.scheduled_for, tz)}"
        f"（{schedule['timezone']}）",
        f"- 触发方式：{_TRIGGER_WORDS[firing.trigger]}",
    ]
    if firing.missed and firing.missed_since is not None:
        count = f"{firing.missed} 次" if firing.missed < MISSED_COUNT_LIMIT else f"至少 {firing.missed} 次"
        since = _local(firing.missed_since, tz)
        if firing.trigger is Trigger.CATCH_UP:
            lines.append(f"- 补跑：服务停止或电脑休眠期间，自 {since} 起漏掉了 {count}触发，这一次运行代替它们；"
                         "需要回补的内容请按这段时间处理。")
        else:
            lines.append(f"- 漏跑：自 {since} 起有 {count}触发因错过时刻被跳过（补跑策略为跳过）。")
    if recent:
        lines.append(f"- 最近 {len(recent)} 次运行（新的在前）：")
        for run in recent:
            moment = run.get("fired_at") or run["scheduled_for"]
            status = _STATUS_WORDS.get(run["status"], run["status"])
            how = "" if run["trigger"] == Trigger.SCHEDULED.value else f"（{_TRIGGER_WORDS[Trigger(run['trigger'])]}）"
            text = _snippet(run.get("summary") or run.get("error"))
            lines.append(f"  - {_local(moment, tz)}{how} · {status}" + (f" · {text}" if text else ""))
    else:
        lines.append("- 最近的运行：没有，这是第一次。")
    lines.append("- 这是无人值守的运行：不要等待用户回答；需要做假设时选择合理的做法，并把假设写进结果。")
    return "\n".join(lines)


# --- The scheduler ---


class _Bus(Protocol):
    async def publish(self, envelope: MessageEnvelope) -> None: ...


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Scheduler:
    """Fires due schedules every ``tick_seconds``; started and stopped with the server."""

    def __init__(self, *, bus: _Bus, clock: Callable[[], datetime] = _utcnow,
                 tick_seconds: float = TICK_SECONDS) -> None:
        self._bus = bus
        self._clock = clock
        self._tick_seconds = tick_seconds
        self._loop_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._loop_task = asyncio.create_task(self._loop())
        logger.info("scheduler started: checks the schedules every %g s", self._tick_seconds)

    async def stop(self) -> None:
        if self._loop_task is not None:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
            self._loop_task = None

    def is_running(self) -> bool:
        return self._loop_task is not None and not self._loop_task.done()

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("scheduler pass failed; trying again in %g s", self._tick_seconds)
            await asyncio.sleep(self._tick_seconds)

    async def tick(self) -> None:
        """One pass: finish ended runs, stop timed-out ones, start queued ones, fire due schedules."""
        now = self._clock()
        await self._settle_running(now)
        for schedule_id in await db_schedules.schedules_with_queued_runs():
            await self._start_queued(schedule_id)
        await self._fire_due(now)

    # --- Firing ---

    async def _fire_due(self, now: datetime) -> None:
        handled: list[str] = []
        while True:
            started: str | None = None
            schedule: dict[str, Any] | None = None
            try:
                async with db_connection.transaction() as conn:
                    schedule = await db_schedules.lock_next_due(conn, now, skip=handled)
                    if schedule is None:
                        return
                    handled.append(schedule["id"])
                    started = await self._fire_schedule(conn, schedule, now)
            except Exception:
                if schedule is None:
                    raise  # the database itself failed: the next pass tries again
                # Rolled back: this schedule is due again next pass; the others go on now.
                logger.exception("could not fire schedule %s", schedule["id"])
                continue
            if started:
                await self._start_task(started)

    async def _fire_schedule(self, conn: asyncpg.Connection, schedule: dict[str, Any], now: datetime) -> str | None:
        """Handle a due schedule whose row ``conn`` holds; the task to start, if one was created."""
        try:
            rule, tz = cron.parse(schedule["cron"]), cron.zone(schedule["timezone"])
        except ValueError as exc:
            # Saved rules are checked; only a rule this version no longer reads gets here. Stop it, and say why.
            logger.error("schedule %s disabled: its rule %r in %s cannot be read: %s", schedule["id"],
                         schedule["cron"], schedule["timezone"], exc)
            await db_schedules.update_schedule(conn, schedule["id"], {"enabled": False, "next_fire_at": None})
            return None
        due = cron.fires_between(rule, tz, schedule["next_fire_at"] - timedelta(microseconds=1), now,
                                 limit=MISSED_COUNT_LIMIT)
        latest = cron.last_fire(rule, tz, now) if len(due) == MISSED_COUNT_LIMIT else (due[-1] if due else None)
        decision = decide(due, latest=latest, now=now, catch_up=schedule["catch_up"])
        next_at = cron.next_fire(rule, tz, now)
        if decision.action is Action.NONE or decision.firing is None:
            await db_schedules.update_schedule(conn, schedule["id"], {"next_fire_at": next_at})
            return None
        firing = decision.firing
        await db_schedules.set_fired(conn, schedule["id"], last_fired_at=firing.scheduled_for, next_fire_at=next_at)
        if decision.action is Action.SKIP:
            await db_schedules.insert_run(
                conn, schedule_id=schedule["id"], scheduled_for=firing.scheduled_for, trigger=firing.trigger.value,
                missed=firing.missed, status="skipped_missed", finished_at=now, created_at=now,
                error=f"{firing.missed} fires were missed since {_local(firing.missed_since or now, tz)}; the latest, "
                      f"at {_local(firing.scheduled_for, tz)}, was more than {int(GRACE.total_seconds() // 60)} "
                      "minutes ago (catch-up: skip)",
            )
            return None
        _, task_id = await self._fire(conn, schedule, firing, now)
        return task_id

    async def run_now(self, schedule_id: str) -> str | None:
        """Fire a schedule now, enabled or not (``trigger`` manual); the run's id, None for an unknown schedule.
        Its ``next_fire_at`` stays."""
        now = self._clock()
        async with db_connection.transaction() as conn:
            schedule = await db_schedules.lock_schedule(conn, schedule_id)
            if schedule is None:
                return None
            run_id, task_id = await self._fire(conn, schedule, Firing(Trigger.MANUAL, now), now)
        if task_id:
            await self._start_task(task_id)
        return run_id

    async def _busy_task(self, conn: asyncpg.Connection, schedule_id: str) -> str | None:
        """A task of the schedule still in progress (queued, running, or created and about to start), if any."""
        return await conn.fetchval(
            "SELECT id FROM tasks WHERE schedule_id = $1 AND status = ANY($2::text[]) ORDER BY created_at LIMIT 1",
            schedule_id, list(_BUSY_STATUSES),
        )

    async def _fire(self, conn: asyncpg.Connection, schedule: dict[str, Any], firing: Firing,
                    now: datetime) -> tuple[str, str | None]:
        """One firing, the schedule's row held: a run with a new task, a queued run or a skipped one. Returns the
        run's id and the task to start (None unless one was created)."""
        common = {"schedule_id": schedule["id"], "scheduled_for": firing.scheduled_for,
                  "trigger": firing.trigger.value, "missed": firing.missed, "created_at": now}
        open_runs = await db_schedules.open_runs(conn, schedule["id"])
        queued = next((run for run in open_runs if run["status"] == db_schedules.QUEUED), None)
        busy = await self._busy_task(conn, schedule["id"])
        if busy or queued:
            if schedule["overlap"] == "queue" and queued is None:
                return await db_schedules.insert_run(conn, status=db_schedules.QUEUED, **common), None
            reason = (f"a run is already waiting for the previous run's task ({busy})" if queued
                      else f"the previous run's task ({busy}) is still in progress")
            return await db_schedules.insert_run(conn, status="skipped_overlap", fired_at=now, finished_at=now,
                                                 error=reason, **common), None
        task_id, error = await self._create_task(conn, schedule, firing)
        if task_id is None:
            run_id = await db_schedules.insert_run(conn, status="failed", fired_at=now, finished_at=now, error=error,
                                                   **common)
            await conn.execute("UPDATE schedules SET consecutive_failures = consecutive_failures + 1 WHERE id = $1",
                               schedule["id"])
            return run_id, None
        run_id = await db_schedules.insert_run(conn, status=db_schedules.RUNNING, fired_at=now, task_id=task_id,
                                               **common)
        logger.info("schedule %s fired (%s): task %s", schedule["id"], firing.trigger.value, task_id)
        return run_id, task_id

    async def _create_task(self, conn: asyncpg.Connection, schedule: dict[str, Any],
                           firing: Firing) -> tuple[str | None, str | None]:
        """Create the run's task, ready to start; its id, or None and why it could not be created."""
        project = await db_projects.get_project(schedule["project_id"], pool=conn)
        work_dir = schedule["work_dir"] or (project or {}).get("work_dir") or ""
        if not work_dir:
            return None, "a work directory is required: set one on the schedule or on its project"
        recent = await db_schedules.recent_runs(conn, schedule["id"], limit=CONTEXT_RUNS)
        description = cron.describe(cron.parse(schedule["cron"]), schedule["timezone"])
        requirement = f"{schedule['requirement']}\n\n{run_context(schedule, firing, recent, description=description)}"
        task_id = f"task-{uuid.uuid4().hex[:12]}"
        await db_tasks.create_task(
            task_id=task_id, requirement=requirement, project_id=schedule["project_id"], work_dir=work_dir,
            status=db_tasks.READY, schedule_id=schedule["id"], budget_usd=schedule["run_budget_usd"], pool=conn,
        )
        if schedule["profile_overrides"]:
            await db_layers.set_profile_overrides(conn, task_id, schedule["profile_overrides"])
        return task_id, None

    async def _start_task(self, task_id: str) -> None:
        """Start the task through the TaskRouter's start path, which applies the project's concurrency limit."""
        await self._bus.publish(MessageEnvelope.create(
            source=SCHEDULER_ADDRESS, target=TASK_ROUTER_ADDRESS, message_type=MessageType.TASK_START,
            payload={"task_id": task_id},
        ))

    async def _start_queued(self, schedule_id: str) -> None:
        """Start the schedule's queued run when none of its tasks is in progress any more."""
        task_id: str | None = None
        now = self._clock()
        async with db_connection.transaction() as conn:
            schedule = await db_schedules.lock_schedule(conn, schedule_id)
            if schedule is None:
                return
            queued = [run for run in await db_schedules.open_runs(conn, schedule_id)
                      if run["status"] == db_schedules.QUEUED]
            if not queued or await self._busy_task(conn, schedule_id):
                return
            run = queued[0]
            firing = Firing(Trigger(run["trigger"]), run["scheduled_for"], missed=run["missed"])
            task_id, error = await self._create_task(conn, schedule, firing)
            await db_schedules.start_queued_run(conn, run["id"], task_id=task_id, fired_at=now, error=error)
            if task_id is None:
                await conn.execute(
                    "UPDATE schedules SET consecutive_failures = consecutive_failures + 1 WHERE id = $1", schedule_id)
        if task_id:
            logger.info("schedule %s: the queued run starts now (task %s)", schedule_id, task_id)
            await self._start_task(task_id)

    # --- Ending ---

    async def task_ended(self, task_id: str, status: str) -> None:
        """The TaskRouter's report that a task ended: finish its run, then start the schedule's queued run."""
        try:
            task = await db_tasks.get_task(task_id)
            if task is None or not task.get("schedule_id"):
                return
            if run := await db_schedules.running_run_of_task(task_id):
                await self._finish(run, task, status, self._clock())
            await self._start_queued(task["schedule_id"])
        except Exception:
            logger.exception("could not record the end of scheduled task %s", task_id)

    async def _finish(self, run: Mapping[str, Any], task: Mapping[str, Any] | None, status: str | None,
                      now: datetime) -> None:
        outcome = _OUTCOMES.get(status or "")
        if outcome is None:
            return
        task = task or {}
        cost = (await db_llm_records.task_costs([run["task_id"]]))[run["task_id"]]["cost_usd"]
        summary = task.get("result_summary") if outcome == "completed" else task.get("error")
        error = task.get("error") if outcome == "failed" else None
        async with db_connection.transaction() as conn:
            if await db_schedules.finish_run(conn, run["id"], status=outcome, at=now, summary=summary,
                                             cost_usd=float(cost), error=error):
                logger.info("schedule %s: run %s %s (task %s)", run["schedule_id"], run["id"], outcome,
                            run["task_id"])

    async def _settle_running(self, now: datetime) -> None:
        """Finish the running runs whose task ended or went away, stop those past their timeout, and start the
        tasks a server stop left created but never started."""
        for run in await db_schedules.running_runs():
            try:
                await self._settle(run, now)
            except Exception:
                logger.exception("could not check schedule run %s", run["id"])

    async def _settle(self, run: dict[str, Any], now: datetime) -> None:
        task_status = run["task_status"]
        if run["task_id"] is None or task_status is None:
            async with db_connection.transaction() as conn:
                await db_schedules.finish_run(conn, run["id"], status="stopped", at=now,
                                              error="its task was deleted")
            return
        if task_status in _OUTCOMES:
            task = {"result_summary": run["result_summary"], "error": run["task_error"]}
            await self._finish(run, task, task_status, now)
            return
        timeout = run["run_timeout_seconds"]
        if timeout and run["fired_at"] and now - run["fired_at"] >= timedelta(seconds=timeout):
            cost = (await db_llm_records.task_costs([run["task_id"]]))[run["task_id"]]["cost_usd"]
            async with db_connection.transaction() as conn:
                finished = await db_schedules.finish_run(
                    conn, run["id"], status="timed_out", at=now, cost_usd=float(cost),
                    error=f"stopped after the run timeout of {timeout} s", summary=None,
                )
            if finished:
                logger.warning("schedule %s: run %s timed out after %d s; stopping task %s", run["schedule_id"],
                               run["id"], timeout, run["task_id"])
                await self._bus.publish(MessageEnvelope.create(
                    source=SCHEDULER_ADDRESS, target=TASK_ROUTER_ADDRESS, message_type=MessageType.TASK_STOP,
                    payload={"task_id": run["task_id"]},
                ))
            return
        if task_status == db_tasks.READY and run["fired_at"] and now - run["fired_at"] >= START_RETRY_AFTER:
            logger.info("schedule %s: task %s was created but never started; starting it", run["schedule_id"],
                        run["task_id"])
            await self._start_task(run["task_id"])
