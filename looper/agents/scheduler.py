"""The scheduler in ``looper serve``: it fires due schedules (scheduled-tasks RFC §6, D5–D7).

Every ``TICK_SECONDS`` (and once at start, which is how fires missed while the
server was down or the computer slept are handled), one pass:

1. Runs whose task ended get their final status, summary and cost (the
   TaskRouter also reports each task end, ``task_ended``, so this pass only
   catches what that missed, such as a task a server start stopped). A run
   past the timeout it started with (``schedule_runs.run_timeout_seconds``) is
   recorded ``timed_out`` and its task stopped. A queued run whose previous task has ended starts.
2. Each enabled schedule whose ``next_fire_at`` has come is handled in a
   transaction that locks its row (``FOR NO KEY UPDATE SKIP LOCKED``), so when
   keeper is off, concurrent servers fire it once. With keeper on, one database
   runs one control plane. A foreign key check on the row (a
   message about its last run being recorded) does not hold it up. The fires
   due since ``next_fire_at`` decide
   (``decide``): one on time fires as ``scheduled``; missed ones fire once as
   ``catch_up`` (``missed`` says how many) or, with catch-up ``skip``, fire
   only when the latest was within ``GRACE`` and are recorded
   ``skipped_missed`` otherwise. ``last_fired_at`` becomes the fire time
   handled and ``next_fire_at`` the next one after now.

A firing while one of the schedule's tasks is still active is recorded
``skipped_overlap``, or, with overlap ``queue``, waits as the one ``queued``
run. With overlap ``parallel`` (reentrant) it starts beside the runs in
progress, up to ``max_parallel_runs`` at once, but never while a run of the
schedule checks its gate: only tasks overlap, so a gate can claim what it hands
to its run (``gate_env`` tells it which run). Otherwise it creates the task (the schedule's requirement plus the
code-written run context, ``run_context``; its profile overrides and task
limits; its own spend cap) and starts it at once with ``TASK_START``: enabling
the schedule was the approval (D6). "Run now" is the same firing with
``trigger`` manual, leaving ``next_fire_at`` alone.

Before the task (S2): a schedule with a ``daily_budget_usd`` whose runs fired
since the server's local midnight cost that much records ``skipped_budget``.
A schedule with a ``gate_command`` records the run ``gating`` and runs the
gate outside the transaction (``schedule_gate``): a failure or timeout is
``gate_failed`` (a failure); nothing printed is ``skipped_gate``, unless the
previous run that ran something did not finish its input; else the task is
created with the gate's output in its context. The gate output of each run
that failed, timed out, was stopped or cancelled, or whose gate failed is
handed on to the next run, labelled 「上一轮未完成」, once (``carried_on``): all
of them when several runs of a parallel schedule ended so before it, oldest
first, the oldest left out (and counted) when they do not fit. Each run that ends is reported to the ``Notifier``, which sends its
message and the alerts (``schedule_notify``); each pass checks the stale
alert. Gates and messages run in the background, so a slow one holds up
neither the pass nor the TaskRouter.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack, asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

import asyncpg

from . import housekeeping

from ..db import agent_setting_layers as db_layers, connection as db_connection, llm_records as db_llm_records
from ..db import schedules as db_schedules, tasks as db_tasks
from . import cron
from .command_runs import BACKSTOP_SLACK, CommandRuns, CommandSpec, Finished, Lost, Observation, Running, Starting, Unknown
from .drain import DrainState
from .message import TASK_ROUTER_ADDRESS, AgentAddress, MessageEnvelope, MessageType
from .schedule_gate import GateResult, run_gate
from .schedule_notify import Notifier, Sender, alert_policy
from .scheduler_settings import effective_run_timeout
from .system_prompt import TASK_REQUIREMENT_LIMIT

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
GATE_TIMEOUT_DEFAULT_SECONDS = 60
GATE_TIMEOUT_MAX_SECONDS = 600
# The least room the gate outputs get in a run's context, however long the requirement.
GATE_CONTEXT_MIN_CHARS = 600
# The least room an earlier run's unfinished input gets there; those that do not fit are left out, oldest first.
CARRIED_MIN_CHARS = 300

# A task's final status, as its run's.
_OUTCOMES = {"completed": "completed", "failed": "failed", "stopped": "stopped", "cancelled": "cancelled"}
# A schedule's task that still counts as in progress: queued, running, or created and about to start.
_BUSY_STATUSES = (*db_tasks.ACTIVE_STATUSES, db_tasks.READY)


@dataclass
class _ProjectFirings:
    active: int = 0
    deleting: bool = False
    idle: asyncio.Event = field(default_factory=asyncio.Event)


# Shared by the deletion entry points and every Scheduler in this server.
_project_firings: dict[str, _ProjectFirings] = {}


@contextmanager
def _project_firing(project_id: str) -> Iterator[bool]:
    state = _project_firings.setdefault(project_id, _ProjectFirings())
    if state.deleting:
        yield False
        return
    state.active += 1
    state.idle.clear()
    try:
        yield True
    finally:
        state.active -= 1
        if not state.active:
            state.idle.set()
            if not state.deleting:
                _project_firings.pop(project_id, None)


@asynccontextmanager
async def quiesce_project(project_id: str) -> AsyncIterator[None]:
    """Refuse new firings and Router starts, then drain admitted work before cleanup.

    The caller serializes project deletions. Gates may keep running outside transactions; their completion
    must enter the barrier again before it can create a task. A failed or cancelled delete releases the barrier.
    """
    state = _project_firings.setdefault(project_id, _ProjectFirings())
    state.deleting = True
    try:
        if state.active:
            await state.idle.wait()
        yield
    finally:
        state.deleting = False
        if not state.active:
            _project_firings.pop(project_id, None)


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
    "gate_failed": "前置检查失败", "skipped_gate": "跳过（前置检查没有输出）", "skipped_budget": "跳过（已到每日花费上限）",
}
_TRIGGER_WORDS = {Trigger.SCHEDULED: "按时触发", Trigger.CATCH_UP: "补跑", Trigger.MANUAL: "手动「立即运行」"}


def _local(moment: datetime, tz: Any) -> str:
    return moment.astimezone(tz).strftime("%Y-%m-%d %H:%M %Z")


def gate_env(schedule: Mapping[str, Any], run_id: str, firing: Firing, timeout: int | None) -> dict[str, str]:
    """The schedule, firing, missed fires and timeout passed to gates and scheduled commands."""
    return {"LOOPER_SCHEDULE_ID": str(schedule["id"]), "LOOPER_SCHEDULE_RUN_ID": run_id,
            "LOOPER_SCHEDULE_SCHEDULED_FOR": firing.scheduled_for.isoformat(),
            "LOOPER_SCHEDULE_RUN_TIMEOUT_SECONDS": "" if timeout is None else str(timeout),
            "LOOPER_SCHEDULE_TRIGGER": firing.trigger.value, "LOOPER_SCHEDULE_MISSED": str(firing.missed),
            "LOOPER_SCHEDULE_MISSED_SINCE": firing.missed_since.isoformat() if firing.missed_since else ""}


def run_title(schedule: Mapping[str, Any], firing: Firing) -> str | None:
    """A run's task title: the schedule's name and the fire time in its zone (``alarm-triage · 10-15 12:30``), the
    name clipped so the time fits ``db.tasks.task_title``'s limit."""
    when = f" · {firing.scheduled_for.astimezone(cron.zone(schedule['timezone'])):%m-%d %H:%M}"
    name = " ".join(str(schedule["name"]).split())
    return db_tasks.task_title(f"{name[:db_tasks.TITLE_MAX_CHARS - len(when)]}{when}")


def _snippet(text: str | None) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= SUMMARY_CHARS else f"{flat[:SUMMARY_CHARS]}…"


def continuing_run_preface(task_id: str, previous: Mapping[str, Any] | None) -> str:
    """The user-message boundary between runs sharing one Planner conversation."""
    lines = [f"这是这个定时任务的一次新运行，任务编号是 {task_id}。上面这段对话都是更早的运行；那些运行的任务编号、步骤、计划和关卡都不属于这个任务。"
             "这个任务的需求和前置检查输出在后面（这一轮的 Task 一节）。做这次的需求；更早的运行只当作记忆。"]
    if previous is not None:
        status = previous["status"]
        lines.append(f"上一轮：{_STATUS_WORDS.get(status, status)} · {_snippet(previous.get('summary') or previous.get('error'))}")
    lines.append("不要沿用更早一次运行的步骤编号；这个任务的步骤用新的编号。")
    return "\n\n".join(lines)


@dataclass(frozen=True)
class Carried:
    """The unfinished input of an earlier run, handed to this one."""

    run_id: str
    status: str
    at: datetime
    gate_output: str


def _fenced(text: str, room: int) -> str:
    """``text`` in a code fence longer than any backtick run in it, clipped to ``room`` characters."""
    if len(text) > room:
        text = f"{text[:room]}\n[截断：还有 {len(text) - room} 个字符，完整内容见这次运行的记录]"
    fence = "`" * max(3, max((len(run) for run in re.findall(r"`+", text)), default=0) + 1)
    return f"{fence}\n{text}\n{fence}"


def run_context(schedule: Mapping[str, Any], firing: Firing, recent: list[Mapping[str, Any]], *,
                description: str, gate_output: str | None = None, carried: Sequence[Carried] = (),
                room: int | None = None) -> str:
    """The section the scheduler appends to a run's requirement: which schedule and firing this is, the fires it
    stands in for, what the gate printed, the unfinished input of the runs that handed it on (``carried``, oldest
    first, one block each), the latest runs' outcomes, and whether its schedule allows user gates. The gate outputs
    share ``room`` characters (at least ``GATE_CONTEXT_MIN_CHARS``), the current one two thirds of it when there are
    earlier ones; those that do not fit at ``CARRIED_MIN_CHARS`` each are left out, the oldest first, and
    counted."""
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
    room = max(GATE_CONTEXT_MIN_CHARS, room if room is not None else TASK_REQUIREMENT_LIMIT)
    own_room = room * 2 // 3 if carried else room
    if gate_output:
        lines.append(f"- 前置检查（`{schedule.get('gate_command') or ''}`）的输出就是这一轮的输入：只处理这些，"
                     "不要再自己运行这条命令或读它的来源去找别的条目：")
        lines.append(_fenced(gate_output, own_room))
    if carried:
        carried_room = room - own_room if gate_output else room
        kept = carried[-max(1, carried_room // CARRIED_MIN_CHARS):]
        if omitted := len(carried) - len(kept):
            lines.append(f"- 另有 {omitted} 次更早的运行也没有完成，这里放不下它们的前置检查输出，见各自的运行记录。")
        for one in kept:
            lines.append(f"- 上一轮未完成：{_local(one.at, tz)} 的运行{_STATUS_WORDS.get(one.status, one.status)}，"
                         "它的前置检查输出如下；这一轮请一并处理还没完成的部分：")
            lines.append(_fenced(one.gate_output, carried_room // len(kept)))
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
    if schedule.get("user_gates") == "on":
        lines.append("- 这次运行允许用户关卡：项目也允许时，可通过关卡等待用户决定；等待计入运行超时。")
    else:
        lines.append("- 这是无人值守的运行：不要等待用户回答；需要做假设时选择合理的做法，并把假设写进结果。")
    return "\n".join(lines)


def run_requirement(schedule: Mapping[str, Any], firing: Firing, recent: list[Mapping[str, Any]], *,
                    description: str, gate_output: str | None = None, carried: Sequence[Carried] = ()) -> str:
    """A run's task requirement: the schedule's, then its run context, the gate outputs fitted into the room the
    task limit (``TASK_REQUIREMENT_LIMIT``) leaves after the rest. The lines around the outputs (the gate's, each
    earlier run's label, the clipping notes) are measured once and the outputs shrunk by what they took, so the
    requirement fits whenever the schedule's own requirement leaves ``schedules.RUN_CONTEXT_RESERVE_CHARS``."""
    head = f"{schedule['requirement']}\n\n"
    bare = run_context(schedule, firing, recent, description=description)
    room = TASK_REQUIREMENT_LIMIT - len(head) - len(bare)
    context = ""
    for _ in range(3):
        context = run_context(schedule, firing, recent, description=description, gate_output=gate_output,
                              carried=carried, room=room)
        excess = len(head) + len(context) - TASK_REQUIREMENT_LIMIT
        if excess <= 0:
            break
        room -= excess + 50  # a shorter output's clipping note can grow by a digit
    return head + context


# --- The scheduler ---


class _Bus(Protocol):
    async def publish(self, envelope: MessageEnvelope) -> None: ...


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class _Launched:
    """After commit: start a task, gate or command, or notify that the run ended."""

    run_id: str
    task_id: str | None = None
    gate: Firing | None = None
    ended: bool = False
    fingerprint: str | None = None
    gate_enabled: bool | None = None
    command: CommandSpec | None = None
    overlap_alert: bool = False
    overlap_recovered: bool = False
    overlap_count: int = 0
    overlap_run: Mapping[str, Any] | None = None


class _DrainBegan(Exception):
    """A drain began while an agent firing's transaction was open: the firing rolls back and is done after the drain."""


class Scheduler:
    """Fires due schedules every ``tick_seconds``; started and stopped with the server. ``sender`` sends the
    schedules' messages (None: they are recorded only), their links starting with ``dashboard_url`` (``links``);
    ``day_zone`` is the zone whose midnight starts a day for the daily budget (the server's own by default)."""

    def __init__(self, *, bus: _Bus, clock: Callable[[], datetime] = _utcnow, tick_seconds: float = TICK_SECONDS,
                 sender: Sender | None = None, day_zone: str | None = None, dashboard_url: str = "",
                 runtime_root: Path | None = None, drain: DrainState | None = None) -> None:
        self._bus = bus
        self._clock = clock
        self._tick_seconds = tick_seconds
        self._loop_task: asyncio.Task[None] | None = None
        self._last_tick_at: datetime | None = None
        self._last_ok_tick_at: datetime | None = None
        self._notifier = Notifier(sender=sender, clock=clock, dashboard_url=dashboard_url)
        self._day_zone = cron.zone(day_zone or cron.local_zone_name())
        self._commands = CommandRuns(runtime_root) if runtime_root is not None else None
        self._runtime_root = runtime_root
        self._housekeeping_at: datetime | None = None
        self._adopted: set[str] = set()
        # Gates and messages under way; stopping cancels them.
        self._background: set[asyncio.Task[Any]] = set()
        self._drain = drain
        self._starting = 0
        self._gates = 0

    def drain_waiting(self) -> dict[str, int]:
        return {"gates": self._gates, "starting": self._starting}

    @contextmanager
    def _admit_start(self, kind: str = "agent") -> Iterator[bool]:
        """Count admission through task publication, including awaits in database transactions."""
        if kind == "command":
            yield True
            return
        if self._drain is not None and self._drain.draining:
            yield False
            return
        self._starting += 1
        try:
            yield True
        finally:
            self._starting -= 1

    def _agent_admission_closed(self, kind: str) -> bool:
        return kind != "command" and self._drain is not None and self._drain.draining

    def _check_still_admitted(self, schedule: Mapping[str, Any]) -> None:
        """Call as the last statement inside an agent firing's transaction."""
        if self._agent_admission_closed(schedule["kind"]):
            raise _DrainBegan(schedule["id"])

    def _defer_start(self, schedule: Mapping[str, Any], launched: _Launched) -> None:
        """The commit landed after drain.begin: the task stays ready, and _settle's start retry starts it once
        the service serves again. A gate was admitted with its run and still finishes during the drain."""
        if launched.task_id and self._agent_admission_closed(schedule["kind"]):
            logger.info("schedule %s: a drain began as run %s committed; task %s starts after the drain",
                        schedule["id"], launched.run_id, launched.task_id)
            launched.task_id = None

    async def start(self) -> None:
        if self._commands is not None:
            await asyncio.to_thread(self._commands.prepare)
        # A gate cannot outlive the server that ran it.
        async with db_connection.transaction() as conn:
            if count := await db_schedules.fail_gating_runs(conn, self._clock()):
                logger.warning("%d schedule run(s) were checking their gate when the server stopped: gate_failed",
                               count)
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
        for job in list(self._background):
            job.cancel()
        await asyncio.gather(*self._background, return_exceptions=True)

    def is_running(self) -> bool:
        return self._loop_task is not None and not self._loop_task.done()

    def health(self) -> dict[str, Any]:
        """Report the loop's latest completed pass without accessing the database."""
        return {
            "running": self.is_running(),
            "tick_seconds": self._tick_seconds,
            "last_tick_at": self._last_tick_at.isoformat() if self._last_tick_at else None,
            "last_ok_tick_at": self._last_ok_tick_at.isoformat() if self._last_ok_tick_at else None,
            "last_ok_tick_age_seconds": (
                (self._clock() - self._last_ok_tick_at).total_seconds() if self._last_ok_tick_at else None
            ),
            "command_runs": {"running": 0},
        }

    async def idle(self) -> None:
        """Wait for the gates and messages under way, and those they start."""
        while self._background:
            await asyncio.gather(*list(self._background), return_exceptions=True)

    def _spawn(self, job: Any, *, gate: bool = False) -> None:
        task = asyncio.ensure_future(job)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        if gate:
            # Count before the task first executes: a drain can start in that scheduling gap.
            self._gates += 1
            task.add_done_callback(lambda _: self._gate_done())

    def _gate_done(self) -> None:
        self._gates -= 1

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
                self._last_tick_at = self._last_ok_tick_at = self._clock()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._last_tick_at = self._clock()
                logger.exception("scheduler pass failed; trying again in %g s", self._tick_seconds)
            await asyncio.sleep(self._tick_seconds)

    async def tick(self) -> None:
        """One pass: finish ended runs, stop timed-out ones, start queued ones, fire due schedules, and start the
        stale alerts that are due."""
        now = self._clock()
        await self._settle_running(now)
        kinds = ("command",) if self._drain is not None and self._drain.draining else None
        for schedule_id in await db_schedules.schedules_with_queued_runs(kinds=kinds):
            await self._start_queued(schedule_id)
        await self._fire_due(now, kinds=kinds)
        await self._notifier.check_stale(now)
        await self._notifier.check_deadlines(now)
        if self._runtime_root is not None and (self._housekeeping_at is None or now >= self._housekeeping_at):
            self._spawn(self._housekeep(now))
            self._housekeeping_at = now + housekeeping.INTERVAL

    async def _housekeep(self, now: datetime) -> None:
        try:
            assert self._runtime_root is not None
            await housekeeping.run_once(self._runtime_root, now,
                                        current_runner=self._commands._runner if self._commands is not None else None,
                                        commands=self._commands)
        except Exception:
            logger.exception("scheduler housekeeping failed; trying again on the next housekeeping pass")

    # --- Firing ---

    async def _fire_due(self, now: datetime, *, kinds: Sequence[str] | None = None) -> None:
        handled: list[str] = []
        while True:
            with ExitStack() as firing_guard:
                launched: _Launched | None = None
                schedule: dict[str, Any] | None = None
                try:
                    async with db_connection.transaction() as conn:
                        # Until the kind is known, count a lookup that may admit agent work. Still look up
                        # commands when agent admission is closed; release this count before kind admission.
                        with self._admit_start():
                            schedule = await db_schedules.lock_next_due(conn, now, skip=handled, kinds=kinds)
                        if schedule is None or not firing_guard.enter_context(self._admit_start(schedule["kind"])):
                            return
                        handled.append(schedule["id"])
                        if not firing_guard.enter_context(_project_firing(schedule["project_id"])):
                            continue
                        launched = await self._fire_schedule(conn, schedule, now)
                        self._check_still_admitted(schedule)
                except _DrainBegan:
                    logger.info("schedule %s: a drain began while it fired; rolled back, it stays due", schedule["id"])
                    continue
                except Exception:
                    if schedule is None:
                        raise  # the database itself failed: the next pass tries again
                    # Rolled back: this schedule is due again next pass; the others go on now.
                    logger.exception("could not fire schedule %s", schedule["id"])
                    continue
                if launched is not None:
                    self._defer_start(schedule, launched)
                    await self._follow(schedule, launched)

    async def _fire_schedule(self, conn: asyncpg.Connection, schedule: dict[str, Any],
                             now: datetime) -> _Launched | None:
        """Handle a due schedule whose row ``conn`` holds."""
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
            await self._skip(
                conn, "skipped_missed", now,
                {"schedule_id": schedule["id"], "scheduled_for": firing.scheduled_for,
                 "trigger": firing.trigger.value, "missed": firing.missed, "created_at": now},
                kind=schedule["kind"],
                error=f"{firing.missed} fires were missed since {_local(firing.missed_since or now, tz)}; the latest, "
                      f"at {_local(firing.scheduled_for, tz)}, was more than {int(GRACE.total_seconds() // 60)} "
                      "minutes ago (catch-up: skip)",
            )
            return None
        return await self._fire(conn, schedule, firing, now)

    async def run_now(self, schedule_id: str, *, expected_version: str | None = None) -> str | None:
        """Fire a schedule now, enabled or not (``trigger`` manual); None if unknown or admission is refused.
        Its ``next_fire_at`` stays. A gate runs in the background: the run is ``gating`` until it ends."""
        now = self._clock()
        with ExitStack() as firing_guard:
            try:
                async with db_connection.transaction() as conn:
                    schedule = await db_schedules.lock_schedule(conn, schedule_id)
                    if schedule is not None:
                        db_schedules.check_configuration_version(schedule, expected_version)
                    if (schedule is None or not firing_guard.enter_context(self._admit_start(schedule["kind"]))
                            or not firing_guard.enter_context(_project_firing(schedule["project_id"]))):
                        return None
                    launched = await self._fire(conn, schedule, Firing(Trigger.MANUAL, now), now)
                    self._check_still_admitted(schedule)
            except _DrainBegan:
                return None
            self._defer_start(schedule, launched)
            await self._follow(schedule, launched)
            return launched.run_id

    async def _follow(self, schedule: Mapping[str, Any], launched: _Launched) -> None:
        """After the firing's transaction: start its task, run its gate, report its end."""
        if launched.task_id:
            await self._start_task(launched.task_id, schedule["project_id"])
        if launched.command is not None:
            await self._start_runner(schedule, launched.command)
        if launched.gate is not None:
            self._spawn(self._gate(schedule["id"], launched.run_id, launched.gate, launched.fingerprint,
                                   launched.gate_enabled), gate=True)
        if launched.ended:
            self._spawn(self._notifier.run_ended(launched.run_id))
        if launched.overlap_alert:
            self._spawn(self._notifier.overlap_alert(schedule["id"], launched.run_id,
                                                    count=launched.overlap_count, running=launched.overlap_run))
        if launched.overlap_recovered:
            self._spawn(self._notifier.overlap_recovered(schedule["id"], launched.run_id))

    async def _busy_task(self, conn: asyncpg.Connection, schedule_id: str) -> str | None:
        """A task of the schedule still in progress (queued, running, or created and about to start), if any."""
        return await conn.fetchval(
            "SELECT id FROM tasks WHERE schedule_id = $1 AND status = ANY($2::text[]) ORDER BY created_at LIMIT 1",
            schedule_id, list(_BUSY_STATUSES),
        )

    async def _fire(self, conn: asyncpg.Connection, schedule: dict[str, Any], firing: Firing,
                    now: datetime) -> _Launched:
        """One firing, the schedule's row held: after the overlap check, a queued or skipped run, or the run
        launched (``_launch``)."""
        common = {"schedule_id": schedule["id"], "scheduled_for": firing.scheduled_for,
                  "trigger": firing.trigger.value, "missed": firing.missed,
                  "missed_since": firing.missed_since, "created_at": now}
        command = schedule["kind"] == "command"
        if not command and await db_schedules.owner_error(conn, schedule["manager_id"], template_key=schedule.get("template_key")):
            return await self._create_run(conn, schedule, firing, now, common=common)
        open_runs = await db_schedules.open_runs(conn, schedule["id"])
        queued = next((run for run in open_runs if run["status"] == db_schedules.QUEUED), None)
        gating = next((run for run in open_runs if run["status"] == db_schedules.GATING), None)
        if schedule["overlap"] == "parallel":
            # Runs overlap in their tasks only: a gate never runs beside another, up to max_parallel_runs at once.
            running = sum(run["status"] == db_schedules.RUNNING for run in open_runs)
            if gating is None and running < schedule["max_parallel_runs"]:
                return await self._launch(conn, schedule, firing, now, common=common)
            reason = ("the previous run is still checking its gate" if gating
                      else f"{running} runs are in progress, the most this schedule runs at once (max_parallel_runs)")
            return await self._skip_overlap(conn, schedule, now, common, reason, open_runs)
        busy = (next((run["id"] for run in open_runs if run["status"] == db_schedules.RUNNING), None)
                if command else await self._busy_task(conn, schedule["id"]))
        if busy or queued or gating:
            if schedule["overlap"] == "queue" and queued is None:
                return _Launched(await db_schedules.insert_run(conn, status=db_schedules.QUEUED, **common))
            reason = ("a run is already waiting for the previous command run" if queued and command
                      else "a run is already waiting for the previous run's task" if queued
                      else "the previous run is still checking its gate" if gating
                      else f"the previous command run ({busy}) is still in progress" if command
                      else f"the previous run's task ({busy}) is still in progress")
            return await self._skip_overlap(conn, schedule, now, common, reason, open_runs)
        return await self._launch(conn, schedule, firing, now, common=common)

    async def _skip_overlap(self, conn: asyncpg.Connection, schedule: Mapping[str, Any], now: datetime,
                            common: Mapping[str, Any], reason: str, open_runs: Sequence[Mapping[str, Any]]) -> _Launched:
        run_id = await self._skip(conn, "skipped_overlap", now, common, kind=schedule["kind"],
                                  fired_at=now, error=reason)
        count = await db_schedules.add_overlap(conn, schedule["id"])
        threshold = alert_policy(schedule).overlap_after
        alert = (threshold > 0 and count >= threshold
                 and await db_schedules.set_alert(conn, schedule["id"], "overlap_alert_at", now))
        # Keep the alert's evidence even if the next run resets the streak before delivery.
        running = next((run for run in open_runs if run["status"] != db_schedules.QUEUED), None)
        return _Launched(run_id, overlap_alert=alert, overlap_count=count, overlap_run=running)

    async def _skip(self, conn: asyncpg.Connection, status: str, now: datetime, common: Mapping[str, Any],
                    *, kind: str = "agent", **fields: Any) -> str:
        """Record every command skip; merge repeated agent skips on today's latest run."""
        merged = (await db_schedules.count_skip(conn, common["schedule_id"], status=status, at=now,
                                                since=self.day_start(now)) if kind == "agent" else None)
        return merged or await db_schedules.insert_run(conn, status=status, finished_at=now, **common, **fields)

    async def _put(self, conn: asyncpg.Connection, run_id: str | None, common: Mapping[str, Any] | None,
                   **fields: Any) -> str:
        """Record the run: a new one from ``common``, or the queued or gating run ``run_id`` updated."""
        if run_id is None:
            assert common is not None
            return await db_schedules.insert_run(conn, **common, **fields)
        await db_schedules.update_run(conn, run_id, **fields)
        return run_id

    def day_start(self, now: datetime) -> datetime:
        """The start of ``now``'s day in the server's zone: the daily budget counts the runs fired since."""
        local = now.astimezone(self._day_zone)
        return datetime.combine(local.date(), datetime.min.time(), tzinfo=self._day_zone)

    async def _launch(self, conn: asyncpg.Connection, schedule: dict[str, Any], firing: Firing, now: datetime, *,
                      common: Mapping[str, Any] | None = None, run_id: str | None = None) -> _Launched:
        """A firing past the overlap check: skipped at the daily budget, gating, or its task created."""
        if schedule["kind"] == "command":
            return await self._start_command(conn, schedule, firing, now, common=common, run_id=run_id)
        if await db_schedules.owner_error(conn, schedule["manager_id"], template_key=schedule.get("template_key")):
            return await self._create_run(conn, schedule, firing, now, common=common, run_id=run_id)
        budget = schedule["daily_budget_usd"]
        if budget is not None:
            spent = await db_schedules.spent_since(conn, schedule["id"], self.day_start(now))
            if spent >= budget:
                error = f"today's runs cost ${spent:.4f}, the daily budget is ${budget:g}"
                if run_id is None:
                    assert common is not None
                    return _Launched(await self._skip(conn, "skipped_budget", now, common, fired_at=now, error=error))
                return _Launched(await self._put(conn, run_id, None, status="skipped_budget", fired_at=now,
                                                 finished_at=now, error=error))
        # The run's timeout is fixed as it starts; changing the schedule or the default later leaves it be.
        timeout = {"run_timeout_seconds": await effective_run_timeout(schedule["run_timeout_mode"],
                                                                      schedule["run_timeout_seconds"], conn)}
        if schedule["gate_command"]:
            from .schedule_templates import effective_schedule, fingerprint
            try:
                effective = await effective_schedule(conn, schedule)
            except ValueError:
                return await self._create_run(conn, schedule, firing, now, common=common, run_id=run_id)
            import json
            expected = fingerprint(schedule, effective)
            run_id = await self._put(conn, run_id, common, status=db_schedules.GATING, fired_at=now, **timeout)
            await db_schedules.update_run(conn, run_id, execution_snapshot=json.loads(expected))
            recovered = await db_schedules.reset_overlaps(conn, schedule["id"])
            return _Launched(run_id, gate=firing, fingerprint=expected, gate_enabled=schedule["enabled"],
                             overlap_recovered=recovered)
        return await self._create_run(conn, schedule, firing, now, common=common, run_id=run_id, started=timeout)

    async def _start_command(self, conn: asyncpg.Connection, schedule: Mapping[str, Any], firing: Firing,
                             now: datetime, *, common: Mapping[str, Any] | None = None,
                             run_id: str | None = None) -> _Launched:
        from .schedule_templates import effective_schedule

        try:
            if self._commands is None:
                raise ValueError("this server cannot run commands (no runtime root)")
            effective = await effective_schedule(conn, schedule)
        except ValueError as exc:
            run_id = await self._put(conn, run_id, common, status="failed", fired_at=now,
                                     finished_at=now, error=str(exc))
            await db_schedules.count_outcome(conn, schedule["id"], "failed", now)
            return _Launched(run_id, ended=True)
        timeout = schedule["run_timeout_seconds"] if schedule["run_timeout_mode"] == "seconds" else None
        run_id = run_id or db_schedules.new_run_id()
        log = CommandRuns.log_path(schedule["id"], run_id)
        if common is not None:
            await db_schedules.insert_run(conn, run_id=run_id, status=db_schedules.RUNNING, fired_at=now,
                                          run_timeout_seconds=timeout, log_path=log, **common)
        else:
            await db_schedules.update_run(conn, run_id, status=db_schedules.RUNNING, fired_at=now,
                                          run_timeout_seconds=timeout, log_path=log)
        recovered = await db_schedules.reset_overlaps(conn, schedule["id"])
        return _Launched(run_id, overlap_recovered=recovered, command=CommandSpec(
            run_id, schedule["id"], schedule["command"], effective.work_dir, schedule["env"] or {},
            schedule["env_file"], timeout, gate_env(schedule, run_id, firing, timeout),
        ))

    async def _start_runner(self, schedule: Mapping[str, Any], spec: CommandSpec) -> None:
        assert self._commands is not None
        try:
            pid, started = await asyncio.to_thread(self._commands.spawn, spec)
        except (OSError, RuntimeError) as exc:
            async with db_connection.transaction() as conn:
                finished = await db_schedules.finish_command_run(
                    conn, spec.run_id, status="failed", at=self._clock(),
                    error=f"the runner could not start: {exc}",
                )
            if finished:
                self._spawn(self._notifier.run_ended(spec.run_id))
            logger.exception("command runner could not start schedule=%s run=%s", schedule["id"], spec.run_id)
            return
        await db_schedules.update_run(db_connection.get_pool(), spec.run_id, runner_pid=pid, runner_started=started)
        logger.info("command run started schedule=%s run=%s pid=%s", schedule["id"], spec.run_id, pid)

    async def _create_run(self, conn: asyncpg.Connection, schedule: dict[str, Any], firing: Firing, now: datetime, *,
                          common: Mapping[str, Any] | None = None, run_id: str | None = None,
                          gate_output: str | None = None, carried: list[Carried] | None = None,
                          fires_now: bool = True, started: Mapping[str, Any] | None = None,
                          expected_fingerprint: str | None = None) -> _Launched:
        """Create the run's task (or fail the run, saying why) and record the run running; ``fires_now`` sets its
        ``fired_at`` (a gating run's was set when its gate started); ``started`` is what a run with no gate records
        as it starts (its timeout)."""
        from .schedule_templates import ConfigurationChanged
        try:
            # Savepoint: a role/profile insert failure must not leave a partial task in the firing transaction.
            async with conn.transaction():
                if carried is None:
                    carried = await self._carried(conn, schedule, before=run_id)
                task_id, error = await self._create_task(conn, schedule, firing, run_id=run_id,
                                                       gate_output=gate_output, carried=carried,
                                                       expected_fingerprint=expected_fingerprint)
        except ConfigurationChanged as exc:
            # Output and its original snapshot were saved outside the task creation savepoint.
            task_id, error = None, str(exc)
        except Exception as exc:
            logger.warning("schedule %s could not create its task: %s", schedule["id"], exc)
            task_id, error = None, str(exc)
        fired = {"fired_at": now} if fires_now else {}
        if task_id is None:
            run_id = await self._put(conn, run_id, common, status="failed", finished_at=now, error=error, **fired)
            await db_schedules.count_outcome(conn, schedule["id"], "failed", now)
            return _Launched(run_id, ended=True)
        # Every earlier run's unfinished input goes to this run, and to no later one; it names the latest of them.
        run_id = await self._put(conn, run_id, common, status=db_schedules.RUNNING, task_id=task_id,
                                 carried_from=carried[-1].run_id if carried else None, **fired, **(started or {}))
        await db_schedules.hand_on(conn, [one.run_id for one in carried])
        logger.info("schedule %s fired (%s): task %s", schedule["id"], firing.trigger.value, task_id)
        recovered = await db_schedules.reset_overlaps(conn, schedule["id"])
        return _Launched(run_id, task_id=task_id, overlap_recovered=recovered)

    async def _work_dir(self, conn: asyncpg.Connection | None, schedule: Mapping[str, Any]) -> str:
        from .schedule_templates import effective_schedule
        return (await effective_schedule(conn, schedule)).work_dir

    async def _create_task(self, conn: asyncpg.Connection, schedule: dict[str, Any], firing: Firing, *,
                           run_id: str | None, gate_output: str | None,
                           carried: list[Carried], expected_fingerprint: str | None = None) -> tuple[str | None, str | None]:
        """Resolve all mutable dependencies in this firing transaction, then store one complete task."""
        from .schedule_templates import effective_schedule, fingerprint, ConfigurationChanged
        from ..db import task_roles as db_roles
        from .tiers.journal import JournalKind, SUMMARY_MAX_CHARS, clip

        try:
            effective = await effective_schedule(conn, schedule)
        except ValueError as exc:
            if expected_fingerprint is not None:
                raise ConfigurationChanged(str(exc)) from None
            raise
        if expected_fingerprint is not None and fingerprint(schedule, effective) != expected_fingerprint:
            raise ConfigurationChanged("schedule/template configuration changed while its gate ran")
        recent = await db_schedules.recent_runs(conn, schedule["id"], limit=CONTEXT_RUNS)
        description = cron.describe(cron.parse(schedule["cron"]), schedule["timezone"])
        requirement = run_requirement({**schedule, "requirement": effective.requirement}, firing, recent,
                                      description=description, gate_output=gate_output, carried=carried)
        task_id = f"task-{uuid.uuid4().hex[:12]}"
        snapshot = None
        if effective.template:
            from dataclasses import asdict
            snapshot = effective.template.snapshot(effective.values, effective.roles)
            snapshot["inputs"] = [{**asdict(item), "value": effective.values.get(item.key, "")}
                                  for item in effective.template.inputs]
        await db_tasks.create_task(
            task_id=task_id, requirement=requirement, project_id=schedule["project_id"],
            work_dir=effective.work_dir, status=db_tasks.READY, schedule_id=schedule["id"],
            budget_usd=schedule["run_budget_usd"], title=run_title(schedule, firing), pool=conn,
            workspace_dir=effective.work_dir, workspace_mode=effective.workspace_mode,
            manager_id=schedule["manager_id"],
            template=snapshot,
        )
        if effective.profiles:
            await db_layers.set_profile_overrides(conn, task_id, effective.profiles)
        if schedule["task_limits"]:
            await db_layers.set_task_limits(conn, task_id, schedule["task_limits"])
        for role in effective.roles:
            name = effective.template.name
            await db_roles.add_first_version(conn, task_id, role["role_key"], {
                **role, "reason": f"from template {name}", "via": "template"},
                {"step_id": "", "kind": JournalKind.ROLE_DEFINED.value,
                 "summary": clip(f"Role {role['role_key']} v1 from the task template {name}", SUMMARY_MAX_CHARS),
                 "learnings": []})
        return task_id, None

    @staticmethod
    async def _carried(conn: asyncpg.Connection, schedule: Mapping[str, Any], *, before: str | None,
                       expected_fingerprint: str | None = None) -> list[Carried]:
        """The unfinished input of every run that ended without finishing it and has not handed it on yet, oldest
        first: each goes to the next run only, all of them when several runs of a parallel schedule failed before
        it."""
        import json
        from .schedule_templates import effective_schedule, fingerprint
        runs = await db_schedules.unhanded_runs(conn, schedule["id"], besides=before)
        current = (json.loads(expected_fingerprint or fingerprint(schedule, await effective_schedule(conn, schedule)))
                   if runs else None)
        return [Carried(run["id"], run["status"], run.get("fired_at") or run["scheduled_for"],
                        run["gate_output"].strip())
                for run in runs if run.get("execution_snapshot") is None or run["execution_snapshot"] == current]

    async def _gate(self, schedule_id: str, run_id: str, firing: Firing, expected: str | None = None,
                    enabled: bool | None = None) -> None:
        """Run a gating run's gate, then record what it decided: gate_failed, skipped_gate, or the task."""
        try:
            from .schedule_templates import effective_schedule, fingerprint
            async with db_connection.transaction() as conn:
                # This read must not hide a due schedule from SKIP LOCKED ticks: they still need to record
                # overlap skips while the gate starts. Completion locks the row and rechecks the fingerprint.
                schedule = await db_schedules.get_schedule(schedule_id, pool=conn)
                if schedule is None:
                    return
                try:
                    if enabled is not None and schedule["enabled"] != enabled:
                        raise ValueError("schedule enabled state changed before its gate started")
                    enabled = schedule["enabled"]
                    effective = await effective_schedule(conn, schedule)
                    current = fingerprint(schedule, effective)
                    if expected is not None and expected != current:
                        raise ValueError("schedule/template configuration changed before its gate started")
                    expected = current
                    configuration_error = None
                except ValueError as exc:
                    configuration_error = str(exc)
                started = await db_schedules.get_run(run_id, pool=conn)
            timeout = schedule["gate_timeout_seconds"] or GATE_TIMEOUT_DEFAULT_SECONDS
            if configuration_error:
                result = GateResult(None, "", "", error=configuration_error)
            else:
                result = await run_gate(schedule["gate_command"], effective.work_dir, timeout,
                                        label=f"gate of {run_id}", env=gate_env(schedule, run_id, firing,
                                        started and started["run_timeout_seconds"]))
            now = self._clock()
            launched: _Launched | None = None
            with ExitStack() as firing_guard:
                async with db_connection.transaction() as conn:
                    schedule = await db_schedules.lock_schedule(conn, schedule_id)
                    run = await db_schedules.get_run(run_id, pool=conn)
                    if schedule is None or run is None or run["status"] != db_schedules.GATING:
                        return
                    allowed = firing_guard.enter_context(_project_firing(schedule["project_id"]))
                    output = result.stdout.strip()
                    await db_schedules.update_run(conn, run_id, gate_output=output or None)
                    if not allowed:
                        await db_schedules.update_run(conn, run_id, status="gate_failed", finished_at=now,
                                                      error="project delete refused the gate's task start")
                        await db_schedules.count_outcome(conn, schedule_id, "gate_failed", now)
                        launched = _Launched(run_id, ended=True)
                    else:
                        try:
                            if schedule["enabled"] != enabled:
                                raise ValueError("schedule enabled state changed while its gate ran")
                            current = await effective_schedule(conn, schedule)
                            if configuration_error or fingerprint(schedule, current) != expected:
                                raise ValueError(configuration_error or "schedule/template configuration changed while its gate ran")
                        except ValueError as exc:
                            await db_schedules.update_run(conn, run_id, status="failed", finished_at=now, error=str(exc))
                            await db_schedules.count_outcome(conn, schedule_id, "failed", now)
                            launched = _Launched(run_id, ended=True)
                    if launched is not None:
                        pass
                    elif not result.ok:
                        await db_schedules.update_run(conn, run_id, status="gate_failed", finished_at=now,
                                                      error=result.failure(timeout), gate_output=output or None)
                        await db_schedules.count_outcome(conn, schedule_id, "gate_failed", now)
                        launched = _Launched(run_id, ended=True)
                    else:
                        carried = await self._carried(conn, schedule, before=run_id, expected_fingerprint=expected)
                        if not output and not carried:
                            # Nothing new: counted on the latest run when that is a skipped_gate of today too.
                            merged = await db_schedules.count_skip(conn, schedule_id, status="skipped_gate", at=now,
                                                                   since=self.day_start(now), besides=run_id)
                            if merged:
                                await db_schedules.delete_run(conn, run_id)
                            else:
                                await db_schedules.update_run(conn, run_id, status="skipped_gate", finished_at=now)
                            await db_schedules.count_outcome(conn, schedule_id, "skipped_gate", now)
                            launched = _Launched(merged or run_id, ended=True)
                        else:
                            launched = await self._create_run(conn, schedule, firing, now, run_id=run_id,
                                                              gate_output=output or None, carried=carried,
                                                              fires_now=False, expected_fingerprint=expected)
                logger.info("schedule %s: gate of run %s %s", schedule_id, run_id,
                            "started a task" if launched.task_id else "ended the run")
                await self._follow(schedule, launched)
                if launched.task_id is None:
                    await self._start_queued(schedule_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("could not finish the gate of schedule run %s", run_id)

    async def _start_task(self, task_id: str, project_id: str) -> None:
        """Publish through the deletion barrier, including recovery of tasks created before a server stop."""
        self._starting += 1
        try:
            with _project_firing(project_id) as allowed:
                # TASK_START stays admitted during drain so Planners can persist results and decisions;
                # their bus queues are lost at restart. Their dispatched runs wait at the Worker slot.
                # A retry can carry a snapshot read before deletion finished and released the barrier.
                if not allowed or await db_tasks.get_task(task_id) is None:
                    return
                await self._bus.publish(MessageEnvelope.create(
                    source=SCHEDULER_ADDRESS, target=TASK_ROUTER_ADDRESS, message_type=MessageType.TASK_START,
                    payload={"task_id": task_id},
                ))
        finally:
            self._starting -= 1

    async def _start_queued(self, schedule_id: str) -> None:
        """Start the schedule's queued run when none of its tasks or gates is in progress any more."""
        now = self._clock()
        with ExitStack() as firing_guard:
            try:
                async with db_connection.transaction() as conn:
                    schedule = await db_schedules.lock_schedule(conn, schedule_id)
                    if (schedule is None or not firing_guard.enter_context(self._admit_start(schedule["kind"]))
                            or not firing_guard.enter_context(_project_firing(schedule["project_id"]))):
                        return
                    runs = await db_schedules.open_runs(conn, schedule_id)
                    queued = [run for run in runs if run["status"] == db_schedules.QUEUED]
                    if not queued or any(run["status"] == db_schedules.GATING for run in runs):
                        return
                    busy = (any(run["status"] == db_schedules.RUNNING for run in runs)
                            if schedule["kind"] == "command" else await self._busy_task(conn, schedule_id))
                    if busy:
                        return
                    run = queued[0]
                    firing = Firing(Trigger(run["trigger"]), run["scheduled_for"], missed=run["missed"],
                                    missed_since=run["missed_since"])
                    launched = await self._launch(conn, schedule, firing, now, run_id=run["id"])
                    self._check_still_admitted(schedule)
            except _DrainBegan:
                return
            self._defer_start(schedule, launched)
            logger.info("schedule %s: the queued run %s starts now", schedule_id, launched.run_id)
            await self._follow(schedule, launched)

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
            finished = await db_schedules.finish_run(conn, run["id"], status=outcome, at=now, summary=summary,
                                                     cost_usd=float(cost), error=error)
        if finished:
            logger.info("schedule %s: run %s %s (task %s)", run["schedule_id"], run["id"], outcome, run["task_id"])
            self._spawn(self._notifier.run_ended(run["id"]))

    async def _settle_running(self, now: datetime) -> None:
        """Observe commands, finish ended tasks, enforce timeouts and recover task starts."""
        if self._commands is not None:
            self._commands.reap()
        rows = await db_schedules.running_runs()
        commands = [run for run in rows if run["kind"] == "command"]
        observations = (await asyncio.to_thread(self._commands.inspect, commands, now)
                        if self._commands is not None else
                        {run["id"]: Lost("this server cannot run commands") for run in commands})
        for run in rows:
            try:
                if run["kind"] == "command":
                    await self._settle_command(run, observations[run["id"]], now)
                else:
                    await self._settle(run, now)
            except Exception:
                logger.exception("could not check schedule run %s", run["id"])

    async def _settle_command(self, run: Mapping[str, Any], obs: Observation, now: datetime) -> None:
        run_id = run["id"]
        fields: dict[str, Any] = {}
        if isinstance(obs, (Starting, Unknown)):
            return
        if isinstance(obs, Running):
            assert self._commands is not None
            if run_id not in self._commands._owned and run_id not in self._adopted:
                logger.info("command run adopted run=%s pid=%s", run_id, obs.runner_pid)
                self._adopted.add(run_id)
            # Recover the identity even when shutdown cancelled the original spawn's database write.
            if obs.runner_pid and (not run["runner_pid"] or not run["runner_started"] or not run["started_at"]):
                await db_schedules.update_run(db_connection.get_pool(), run_id,
                                              runner_pid=obs.runner_pid, runner_started=obs.runner_started,
                                              started_at=obs.started_at)
            timeout = run["run_timeout_seconds"]
            if timeout is None or obs.started_at is None \
                    or now <= obs.started_at + timedelta(seconds=timeout) + BACKSTOP_SLACK:
                return
            await asyncio.to_thread(self._commands.end_group, obs.child)
            if obs.runner_pid:
                await asyncio.to_thread(self._commands.kill_runner, obs.runner_pid, obs.runner_started)
            status, at = "timed_out", now
            error = "the runner did not end the run at its timeout; Looper killed its process group"
            fields["started_at"] = obs.started_at
        elif isinstance(obs, Finished):
            status, at, error = obs.status, obs.ended_at, obs.error
            # PostgreSQL TEXT rejects NUL; keep raw output in the log file and exit record.
            fields = {"started_at": obs.started_at, "exit_code": obs.exit_code, "exit_signal": obs.signal,
                      "log_bytes": obs.log_bytes, "log_truncated": obs.log_truncated,
                      "log_tail": obs.tail.replace("\0", "")}
        else:
            assert isinstance(obs, Lost)
            if self._commands is not None:
                await asyncio.to_thread(self._commands.end_group, obs.child)
            status, at, error = "interrupted", now, obs.reason
        async with db_connection.transaction() as conn:
            finished = await db_schedules.finish_command_run(conn, run_id, status=status, at=at, error=error, **fields)
        if not finished:
            return
        if isinstance(obs, Finished):
            seconds = (obs.ended_at - obs.started_at).total_seconds() if obs.started_at else 0.0
            logger.info("command run finished run=%s status=%s exit=%s seconds=%.1f log_bytes=%d truncated=%s",
                        run_id, status, obs.exit_code, seconds, obs.log_bytes, obs.log_truncated)
        elif isinstance(obs, Running):
            logger.warning("command run killed by backstop run=%s", run_id)
        else:
            logger.warning("command run interrupted run=%s reason=%s", run_id, error)
        if self._commands is not None:
            await asyncio.to_thread(self._commands.release, run_id)
        self._adopted.discard(run_id)
        self._spawn(self._notifier.run_ended(run_id))
        await self._start_queued(run["schedule_id"])

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
                self._spawn(self._notifier.run_ended(run["id"]))
            return
        if (task_status == db_tasks.READY and run["fired_at"] and now - run["fired_at"] >= START_RETRY_AFTER
                and not self._agent_admission_closed(run["kind"])):
            logger.info("schedule %s: task %s was created but never started; starting it", run["schedule_id"],
                        run["task_id"])
            await self._start_task(run["task_id"], run["project_id"])
