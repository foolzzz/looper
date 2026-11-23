"""The status card: a typed snapshot of what Looper is doing, assembled by code.

Off until the system setting ``status_card.enabled`` is ``true`` (no row, or anything other than ``true``, is off).
While it is off, ``status_card.get`` answers conflict and ``/status`` stays an ordinary message. The snapshot
itself does not check the switch, so a test can build one either way. A section is ``ok``, ``error`` (its source
could not be read: the text never says that source is empty) or ``unknown`` (this caller cannot read it). A
feature that is off leaves its section out. There is no recent-receipts section and no per-lane queue count.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum, StrEnum
from typing import Any, Literal, NotRequired, TypedDict, cast
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

ENABLED_KEY = "status_card.enabled"
FORBIDDEN = "状态只在你本人的私聊和控制台里看。"
_FAILURE_CHARS = 300
_LABELS = {"manager": "管家", "tasks": "任务", "schedules": "定时任务", "waiting": "等你", "reminders": "提醒"}
_WAITING = {"gates": "关卡", "confirmations": "确认", "schedules": "失败的定时任务", "logins": "登录",
            "intents": "意图", "ledger": "待办", "deliveries": "可能没送到"}
_BACKGROUND = {"completion": "收尾", "own": "自己发起"}

_enabled = False

# A reply marked with this is shown on the live stream and stored in no chat a later session reads.
EPHEMERAL_CHAT = "ephemeral_chat"


class SessionState(StrEnum):
    """What one Manager session is doing."""

    IDLE = "idle"
    ANSWERING = "answering"
    BACKGROUND = "background"


SectionState = Literal["ok", "error", "unknown"]


class _ErrorSection(TypedDict):
    name: str
    state: Literal["error"]
    error: str


class _UnknownSection(TypedDict):
    name: str
    state: Literal["unknown"]
    error: None


class _SessionView(TypedDict):
    key: str
    state: Literal["idle", "answering", "background"]
    origin: str | None
    channel: str | None
    chat: str | None
    group: bool
    client_id: str | None
    started_at: str | None
    elapsed_seconds: int | None
    queued: int


class _ManagerRow(TypedDict):
    manager_id: str
    name: str
    queued: int
    sessions: list[_SessionView]


class _ManagerSection(TypedDict):
    name: Literal["manager"]
    state: SectionState
    error: str | None
    managers: NotRequired[list[_ManagerRow]]


class _TaskRow(TypedDict):
    task_id: str
    name: str
    project_id: str | None
    project_name: str | None
    workers_running: int
    workers_waiting: int
    reviewers_running: int
    reviewers_waiting: int
    planner: bool


class _TasksSection(TypedDict):
    name: Literal["tasks"]
    state: SectionState
    error: str | None
    count: NotRequired[int]
    shown: NotRequired[list[_TaskRow]]
    more: NotRequired[int]


class _ScheduleNext(TypedDict):
    schedule_id: str
    name: str
    project_name: str | None
    next_fire_at: str | None
    timezone: str | None


class _ScheduleRunning(TypedDict):
    run_id: str
    schedule_id: str
    name: str
    project_name: str | None
    fired_at: str | None
    timezone: str | None


class _SchedulesSection(TypedDict):
    name: Literal["schedules"]
    state: SectionState
    error: str | None
    next: NotRequired[list[_ScheduleNext]]
    running: NotRequired[list[_ScheduleRunning]]


class _Earliest(TypedDict):
    key: str | None
    title: str
    since: str | None


class _WaitingKind(TypedDict):
    kind: str
    state: SectionState
    count: int
    earliest: _Earliest | None
    error: str | None


class _WaitingSection(TypedDict):
    name: Literal["waiting"]
    state: SectionState
    error: str | None
    kinds: NotRequired[list[_WaitingKind]]


class _ReminderNext(TypedDict):
    due_at: str | None
    text: str
    timezone: str | None


class _RemindersSection(TypedDict):
    name: Literal["reminders"]
    state: SectionState
    error: str | None
    next: NotRequired[_ReminderNext | None]


_Section = (_ManagerSection | _TasksSection | _SchedulesSection | _WaitingSection | _RemindersSection
            | _ErrorSection | _UnknownSection)


class StatusSnapshot(TypedDict):
    """The card as code returns it: section states are ``ok``, ``error`` or ``unknown``."""

    read_at: str
    text: str
    sections: list[_Section]


def enabled() -> bool:
    """Whether the card is on. Until ``load`` reads the row, it is off; a failed read stays off."""
    return _enabled


def apply_stored(value: str | None) -> None:
    """Apply a stored ``status_card.enabled`` value. Only ``true`` is on."""
    set_enabled(value == "true")


def set_enabled(value: bool) -> None:
    """Apply a value in memory only (``load`` and ``update`` store it; tests use this directly)."""
    global _enabled
    _enabled = value


def ephemeral_status(text: str, *, target_type: str) -> bool:
    """Whether ``text`` is an enabled ``/status`` addressed to a Manager.

    That reply is built in code. The dashboard must keep both the command and the card out of every store a later
    session or a resume brief reads. Off, and any other target, it stays an ordinary message.
    """
    from .chat_commands import is_status

    return target_type == "manager" and is_status(text) and enabled()


def view() -> dict[str, bool]:
    return {"enabled": enabled()}


async def update(value: bool | None) -> dict[str, bool]:
    """Persist the operator's choice. Null deletes the row and restores off."""
    if value is not None and not isinstance(value, bool):
        raise ValueError("enabled must be true, false or null")
    from ..db import system_settings as db_settings

    await db_settings.put_settings({ENABLED_KEY: None if value is None else ("true" if value else "false")})
    set_enabled(False if value is None else value)
    return view()


@dataclass(eq=False)
class SessionActivity:
    """One Manager session as the card reads it: idle, answering the operator, or a background turn."""

    key: str
    state: SessionState
    origin: str | None
    channel: str | None
    chat: str | None
    group: bool
    client_id: str | None
    started_at: datetime | None
    queued: int

    def __post_init__(self) -> None:
        if not isinstance(self.state, SessionState):
            self.state = SessionState(self.state)


class SoloFleet:
    """The one Manager that is answering, when the process has no fleet object."""

    def __init__(self, agent: Any) -> None:
        self._agent = agent

    def agents(self) -> list[Any]:
        return [self._agent]


def session_activity(agent: Any) -> list[SessionActivity]:
    """What each of ``agent``'s sessions is doing, and how many messages wait for it.

    With session routing off, the main session's count also includes the bus queue and a message the loop has
    taken that is not the running turn. Those are not lanes. With routing on, only each session's own queue counts.
    """
    from . import session_routing
    from .tiers.manager_session import MAIN_SESSION

    routing = session_routing.routes(agent.agent_id)
    sessions = sorted(agent._sessions.values(), key=lambda session: (session.key != MAIN_SESSION, session.key))
    found = []
    for session in sessions:
        token = session.running_token
        locked = session.turn_lock.locked()
        origin = _origin(token)
        if token is None and not locked:
            state, origin = SessionState.IDLE, None
            channel = chat = client_id = None
            group = False
            started = None
        else:
            state = SessionState.ANSWERING if origin in (None, "user") else SessionState.BACKGROUND
            channel, chat, group, client_id = _session_place(token)
            started = token.received_at if token is not None else None
        queued = agent._router.queued_count(session)
        if not routing and session.key == MAIN_SESSION:
            pending = agent._bus.pending_count(agent.agent_id) if agent._bus is not None else 0
            handling = agent._handling
            extra = 1 if handling is not None and (token is None or handling.message_id != token.message_id) else 0
            queued += pending + extra
        found.append(SessionActivity(session.key, state, origin, channel, chat, group, client_id, started, queued))
    return found


def _origin(token: Any) -> str | None:
    turn = getattr(token, "ops_turn", None) if token is not None else None
    origin = getattr(turn, "origin", None)
    return origin.value if origin is not None else None


def _session_place(token: Any) -> tuple[str | None, str | None, bool, str | None]:
    channel = getattr(token, "channel", None) or None
    chat = getattr(token, "chat", None) or None
    group, client_id = False, None
    envelope = getattr(token, "envelope", None)
    if envelope is not None:
        from .tiers.manager import _message_client, _message_origin

        origin = _message_origin(envelope)
        channel = origin.get("channel") or channel
        chat = origin.get("chat") or chat
        group = bool(origin.get("group"))
        client_id, _name = _message_client(envelope)
    if channel == "":
        channel = None
    return channel, chat, group, client_id


async def snapshot(*, fleet: Any, task_router: Any, worker_pool: Any, now: datetime,
                   source: str = "snapshot") -> StatusSnapshot:
    """The card now. ``source`` is only the log line (``snapshot``, ``status`` or ``get``)."""
    started = time.perf_counter()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    sections = [await _manager_section(fleet, now), await _tasks_section(task_router, worker_pool)]
    sections.append(await _schedules_section(now))
    waiting = await _waiting_section(fleet)
    if waiting is not None:
        sections.append(waiting)
    reminders = await _reminders_section()
    if reminders is not None:
        sections.append(reminders)
    text = _render(now, sections)
    failed = ",".join(section["name"] for section in sections if section["state"] == "error") or "-"
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    logger.info("status_card source=%s elapsed_ms=%d failed=%s", source, elapsed_ms, failed)
    return cast(StatusSnapshot, {"read_at": now.isoformat(), "text": text, "sections": _jsonable(sections)})


async def command_reply(envelope: Any, *, fleet: Any, task_router: Any, worker_pool: Any, now: datetime) -> str:
    """The reply to ``/status``: the card in the operator's own turn, a refusal in any other. A refusal builds nothing."""
    from . import gate_routing, operator_identity
    from .message import is_lark

    channel = envelope.source.channel or envelope.source.agent_id or ""
    context = envelope.payload.get("channel_context")
    context = context if isinstance(context, dict) else {}
    group = gate_routing._group(channel, context) if is_lark(channel) or channel == "telegram" else False
    sender = context.get("sender_id") or context.get("user_id")
    client = context.get("client")
    operator = await operator_identity.operator_sender_id(envelope.source.agent_id) if channel != "web" else None
    if not operator_identity.is_operator(channel=channel, group=group, sender=str(sender) if sender else None,
                                        client_id=client.get("id") if isinstance(client, dict) else None,
                                        operator_id=operator):
        return FORBIDDEN
    card = await snapshot(fleet=fleet, task_router=task_router, worker_pool=worker_pool, now=now, source="status")
    return card["text"]


def _failure(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}".replace("\n", " ").replace("\r", " ")
    return text[:_FAILURE_CHARS]


def _error(name: str, exc: BaseException) -> dict[str, Any]:
    logger.exception("status_card: could not read %s", name)
    return {"name": name, "state": "error", "error": _failure(exc)}


def _unknown(name: str) -> dict[str, Any]:
    return {"name": name, "state": "unknown", "error": None}


async def _manager_section(fleet: Any, now: datetime) -> dict[str, Any]:
    if fleet is None:
        return _unknown("manager")
    try:
        agents = list(fleet.agents())
        rows = [(agent, list(agent.activity())) for agent in agents]
    except Exception as exc:
        return _error("manager", exc)
    managers = []
    for agent, activity in rows:
        sessions = [_session_payload(item, now) for item in activity]
        name = _plain(getattr(agent, "name", "") or "") or getattr(agent, "agent_id", "")
        managers.append({"manager_id": getattr(agent, "agent_id", ""), "name": name,
                         "queued": sum(item.queued for item in activity), "sessions": sessions})
    return {"name": "manager", "state": "ok", "error": None, "managers": managers}


def _session_payload(item: SessionActivity, now: datetime) -> dict[str, Any]:
    elapsed = None
    if item.started_at is not None:
        elapsed = max(0, int((now - item.started_at).total_seconds()))
    return {"key": item.key, "state": item.state, "origin": item.origin, "channel": item.channel,
            "chat": item.chat, "group": item.group, "client_id": item.client_id,
            "started_at": item.started_at, "elapsed_seconds": elapsed, "queued": item.queued}


async def _tasks_section(task_router: Any, worker_pool: Any) -> dict[str, Any]:
    if task_router is None:
        return _unknown("tasks")
    try:
        from ..db import projects as db_projects
        from ..db import tasks as db_tasks

        grouped: dict[str, dict[str, Any]] = {}
        for planner in task_router.running_planners():
            row = _task_row(grouped, planner["task_id"])
            row["planner"] = True
            row["cached_name"] = planner.get("name") or planner["task_id"]
            if planner.get("project_id"):
                row["project_id"] = planner["project_id"]
        for run in (() if worker_pool is None else worker_pool.runs()):
            row = _task_row(grouped, run["task_id"])
            _count_run(row, run.get("role"), run.get("state"))
            if not row["project_id"] and run.get("project_id"):
                row["project_id"] = run["project_id"]
        ids = list(grouped)
        found = await db_tasks.names_and_projects_of(ids) if ids else {}
        project_ids = []
        for task_id, row in grouped.items():
            info = found.get(task_id) or {}
            if info.get("project_id"):
                row["project_id"] = info["project_id"]
            row["name"] = info.get("name") or row["cached_name"] or task_id
            if row["project_id"]:
                project_ids.append(row["project_id"])
        names = await db_projects.names_of(set(project_ids)) if project_ids else {}
        shown_rows = []
        for row in grouped.values():
            raw = names.get(row["project_id"]) if row["project_id"] else None
            shown_rows.append({
                "task_id": row["task_id"], "name": _plain(row["name"]) or row["task_id"],
                "project_id": row["project_id"], "project_name": _plain(raw) if raw else None,
                "workers_running": row["workers_running"], "workers_waiting": row["workers_waiting"],
                "reviewers_running": row["reviewers_running"], "reviewers_waiting": row["reviewers_waiting"],
                "planner": row["planner"]})
        shown_rows.sort(key=lambda row: (not _task_active(row), row["name"].lower(), row["task_id"]))
    except Exception as exc:
        return _error("tasks", exc)
    shown, more = shown_rows[:5], shown_rows[5:]
    return {"name": "tasks", "state": "ok", "error": None, "count": len(shown_rows), "shown": shown, "more": len(more)}


def _task_row(grouped: dict[str, dict[str, Any]], task_id: str) -> dict[str, Any]:
    return grouped.setdefault(task_id, {
        "task_id": task_id, "cached_name": None, "project_id": None, "planner": False,
        "workers_running": 0, "workers_waiting": 0, "reviewers_running": 0, "reviewers_waiting": 0})


def _count_run(row: dict[str, Any], role: str | None, state: str | None) -> None:
    if role == "worker" and state == "running":
        row["workers_running"] += 1
    elif role == "worker" and state == "waiting":
        row["workers_waiting"] += 1
    elif role == "reviewer" and state == "running":
        row["reviewers_running"] += 1
    elif role == "reviewer" and state == "waiting":
        row["reviewers_waiting"] += 1


def _task_active(row: Mapping[str, Any]) -> bool:
    return bool(row["workers_running"] or row["reviewers_running"] or row["planner"])


async def _schedules_section(now: datetime) -> dict[str, Any]:
    try:
        from ..db import schedules as db_schedules

        overview = await db_schedules.overview(now)
        running_rows = await db_schedules.named_running()
    except Exception as exc:
        return _error("schedules", exc)
    upcoming = []
    for row in overview.get("upcoming") or []:
        upcoming.append({"schedule_id": row["id"], "name": _plain(row.get("name")),
                         "project_name": _plain(row.get("project_name")) or None,
                         "next_fire_at": row.get("next_fire_at"), "timezone": row.get("timezone")})
        if len(upcoming) == 3:
            break
    running = [{"run_id": row["id"], "schedule_id": row["schedule_id"], "name": _plain(row.get("name")),
                "project_name": _plain(row.get("project_name")) or None, "fired_at": row.get("fired_at"),
                "timezone": row.get("timezone")} for row in running_rows]
    return {"name": "schedules", "state": "ok", "error": None, "next": upcoming, "running": running}


async def _waiting_section(fleet: Any) -> dict[str, Any] | None:
    from . import attention

    try:
        on = await attention.enabled()
    except Exception as exc:
        return _error("waiting", exc)
    if not on:
        return None
    managers = fleet if fleet is not None and callable(getattr(fleet, "ops", None)) else None
    try:
        summary = await attention.collect(managers=managers)
    except Exception as exc:
        return _error("waiting", exc)
    kinds = []
    for section in summary.get("sections") or []:
        earliest = None
        # The shown list is capped and often urgency-ordered, so the oldest item has its own read.
        if section.get("state") == "ok" and section.get("count"):
            try:
                earliest = _earliest_view(await attention.earliest(section["kind"], managers=managers))
            except Exception:
                logger.warning("status_card: could not read the earliest %s", section.get("kind"), exc_info=True)
        kinds.append({"kind": section["kind"], "state": section["state"], "count": section.get("count") or 0,
                      "earliest": earliest, "error": section.get("error")})
    return {"name": "waiting", "state": "ok", "error": None, "kinds": kinds}


def _earliest_view(item: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not item:
        return None
    since = item.get("since")
    return {"key": item.get("key"), "title": _plain(item.get("title")),
            "since": since if isinstance(since, datetime) else None}


async def _reminders_section() -> dict[str, Any] | None:
    from . import reminders

    try:
        on = await reminders.enabled()
    except Exception as exc:
        return _error("reminders", exc)
    if not on:
        return None
    try:
        from ..db import reminders as db_reminders

        row = await db_reminders.next_pending()
    except Exception as exc:
        return _error("reminders", exc)
    nxt = None
    if row is not None:
        nxt = {"due_at": row.get("due_at"), "text": _plain(row.get("text")), "timezone": row.get("timezone")}
    return {"name": "reminders", "state": "ok", "error": None, "next": nxt}


def _plain(text: Any) -> str:
    from . import attention

    return attention._plain(text)


def _render(now: datetime, sections: list[dict[str, Any]]) -> str:
    lines = [_header(now)]
    for section in sections:
        lines.append(_section_line(section, now))
    return "\n".join(lines)


def _header(now: datetime) -> str:
    from . import chat_settings

    name = chat_settings.timezone()
    zone: Any
    if not name:
        zone = now.astimezone().tzinfo
    else:
        try:
            zone = ZoneInfo(name)
        except Exception:
            logger.warning("status_card: header timezone %s is not usable", name)
            zone = now.astimezone().tzinfo
    return f"截至 {now.astimezone(zone).strftime('%m-%d %H:%M')}"


def _section_line(section: Mapping[str, Any], now: datetime) -> str:
    label = _LABELS[section["name"]]
    if section["state"] == "error":
        return f"{label}：没能读取（{section['error']}）"
    if section["state"] == "unknown":
        return f"{label}：这里读不到"
    if section["name"] == "manager":
        return _manager_text(section)
    if section["name"] == "tasks":
        return _tasks_text(section)
    if section["name"] == "schedules":
        return _schedules_text(section, now)
    if section["name"] == "waiting":
        return _waiting_text(section)
    return _reminders_text(section, now)


def _manager_text(section: Mapping[str, Any]) -> str:
    managers = section["managers"]
    if not managers:
        return "管家：没有在运行的"
    lines = []
    single = len(managers) == 1
    for manager in managers:
        label = "管家" if single else (manager["name"] or manager["manager_id"])
        phrases = [_session_phrase(session) for session in manager["sessions"] if session["state"] != "idle"]
        body = "；".join(phrases) if phrases else "空闲"
        lines.append(f"{label}：{body}；排队 {manager['queued']} 条")
    return "\n".join(lines)


def _session_phrase(session: Mapping[str, Any]) -> str:
    duration = _duration(session.get("elapsed_seconds"))
    if session["state"] == "background":
        kind = _BACKGROUND.get(session.get("origin") or "", "自己发起")
        return f"在跑后台回合（{kind}，{duration}）"
    return f"在回答你（{_where(session)}，{duration}）"


def _where(session: Mapping[str, Any]) -> str:
    channel, group = session.get("channel"), bool(session.get("group"))
    if channel == "web":
        return "API 客户端" if session.get("client_id") else "控制台"
    if channel == "telegram":
        return "Telegram 群" if group else "Telegram"
    if channel in ("lark", "lark_bot"):
        return "飞书群" if group else "飞书"
    return "聊天"


def _duration(seconds: int | None) -> str:
    if seconds is None or seconds < 60:
        return "不到 1 分钟"
    if seconds < 3600:
        return f"{seconds // 60} 分钟"
    if seconds < 86400:
        return f"{seconds // 3600} 小时"
    return f"{seconds // 86400} 天"


def _tasks_text(section: Mapping[str, Any]) -> str:
    if not section["count"]:
        return "任务：没有在跑的"
    body = "、".join(_task_phrase(row) for row in section["shown"])
    extra = f"，另有 {section['more']} 个" if section["more"] else ""
    return f"任务：{section['count']} 个在跑：{body}{extra}"


def _task_phrase(row: Mapping[str, Any]) -> str:
    detail = _task_detail(row)
    project = row.get("project_name")
    name = row["name"]
    if project and detail:
        return f"{name}（{project}，{detail}）"
    if project:
        return f"{name}（{project}）"
    if detail:
        return f"{name}（{detail}）"
    return name


def _task_detail(row: Mapping[str, Any]) -> str:
    parts = []
    if row["workers_running"]:
        parts.append(f"Worker {row['workers_running']} 个在跑")
    if row["workers_waiting"]:
        parts.append(f"{row['workers_waiting']} 个等位置")
    if row["reviewers_running"]:
        if row["workers_running"]:
            parts.append(f"Reviewer {row['reviewers_running']} 个在跑")
        elif row["reviewers_running"] == 1:
            parts.append("等评审")
        else:
            parts.append(f"Reviewer {row['reviewers_running']} 个在跑")
    if row["reviewers_waiting"]:
        parts.append(f"Reviewer {row['reviewers_waiting']} 个等位置")
    if row["planner"] and not parts:
        parts.append("Planner 在跑")
    return "，".join(parts)


def _schedules_text(section: Mapping[str, Any], now: datetime) -> str:
    running, upcoming = section["running"], section["next"]
    if not running and not upcoming:
        return "定时任务：没有待发的"
    parts = []
    if running:
        parts.append("正在跑 " + "、".join(row["name"] for row in running))
    if upcoming:
        parts.append("下一个 " + "、".join(f"{_clock(row['next_fire_at'], row.get('timezone'), now)} {row['name']}"
                                         for row in upcoming))
    return "定时任务：" + "；".join(parts)


def _waiting_text(section: Mapping[str, Any]) -> str:
    parts = []
    hint = False
    for kind in section["kinds"]:
        label = _WAITING.get(kind["kind"], kind["kind"])
        if kind["state"] == "error":
            parts.append(f"{label}没能读取")
        elif kind["state"] == "unknown":
            parts.append(f"{label}这里读不到")
        elif kind["count"]:
            parts.append(f"{label} {kind['count']}")
            hint = True
    if not parts:
        return "等你：没有"
    text = "等你：" + " · ".join(parts)
    return text + "（发 /todo 看详情）" if hint else text


def _reminders_text(section: Mapping[str, Any], now: datetime) -> str:
    nxt = section["next"]
    if not nxt:
        return "提醒：没有待发的"
    return f"提醒：下一条 {_clock(nxt['due_at'], nxt.get('timezone'), now)} {nxt['text']}"


def _clock(moment: Any, zone_name: str | None, now: datetime) -> str:
    if not isinstance(moment, datetime):
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    zone = _zone(zone_name)
    local = moment.astimezone(zone)
    if local.date() == now.astimezone(zone).date():
        return local.strftime("%H:%M")
    return local.strftime("%m-%d %H:%M")


def _zone(name: str | None) -> Any:
    if not name:
        return timezone.utc
    try:
        return ZoneInfo(name)
    except Exception:
        logger.warning("status_card: timezone %s is not usable", name)
        return timezone.utc


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value
