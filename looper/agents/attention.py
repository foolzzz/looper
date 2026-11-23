"""What needs the operator ("needs you"): one read-only summary, gathered by code from what Looper already keeps.
It calls no model and stores nothing.

Off until the system setting ``attention.enabled`` is ``true`` (no row is off). While it is off, ``attention.summary``
(``POST /api/v1/attention/summary``) answers ``conflict``, the dashboard shows no page for it, and ``/todo`` is an
ordinary message: no prompt, tool list or command line changes either way.

The sections, in this order (``KINDS``):

- ``gates``: user gates waiting for the operator's answer (``gate_service.list_gates``, ``waiting_on=user``);
- ``confirmations``: a Manager's operation waiting for the operator's word in a chat
  (``card_actions.pending_confirmations``: durable card rows while cards are on, otherwise Manager memory);
- ``schedules``: failing schedules, as the dashboard's Schedules card lists them (``db.schedules.failing``);
- ``logins``: a runtime login an Agent Profile uses, or the Claude Code CLI or login, failing, as the HTTP server's
  status checks report them now (``use_status_checks``);
- ``intents``: intents waiting to be built or rejected, while intents are on;
- ``ledger``: the ledger's own open items that wait for the user (a to-do, a decision to confirm, anything overdue),
  while the ledger is on;
- ``deliveries``: messages of the last ``DELIVERY_WINDOW`` that may not have arrived (``uncertain``: never sent
  again, so the operator checks), and persisted dropped Lark catch-up windows;
- ``cost``: pending spend holds, while the cost guard enforces limits.

A section is ``ok`` (read; possibly empty), ``error`` (its source could not be read: never shown as empty) or
``unknown`` (this caller cannot read it, e.g. ``logins`` without a status reader). A feature that is off leaves its
section out. Each section lists at most ``SHOWN`` items and counts them all.

``/todo`` in a chat answers the same summary in code, only in the operator's own turns (``operator_identity``).
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any
from urllib.parse import urlencode

from ..db import attention as store, ledger as db_ledger, schedules as db_schedules, tasks as db_tasks
from ..db import system_settings as db_settings
from . import card_actions, gate_routing, gate_service, leads_model, operator_identity
from .message import MessageEnvelope, is_lark, parse_channel_id

logger = logging.getLogger(__name__)

ENABLED_KEY = "attention.enabled"
KINDS = ("gates", "confirmations", "schedules", "logins", "intents", "ledger", "deliveries", "cost")
SHOWN = 20
DELIVERY_WINDOW = timedelta(hours=72)
# The checks the dashboard's banner shows as a login that fails (web/src/components/claude-check-banner.tsx).
LOGIN_CHECKS = ("codex_login", "coco_login", "grok_login", "cursor_login", "ttadk")

StatusChecks = Callable[[], Awaitable[Sequence[Mapping[str, Any]]]]
CatchupChecks = Callable[[], Sequence[Mapping[str, Any]]]


class NotRead(Enum):
    """Why a reader gave no items: its feature is off (the section is left out), or this caller cannot read it
    (the section is ``unknown``)."""

    OFF = "off"
    UNKNOWN = "unknown"


Read = tuple[list[dict[str, Any]], int] | NotRead
_status_checks: StatusChecks | None = None
_catchup_checks: CatchupChecks | None = None


async def enabled() -> bool:
    """Whether the summary is on (``attention.enabled`` is ``true``)."""
    return (await db_settings.get_settings([ENABLED_KEY])).get(ENABLED_KEY) == "true"


def use_status_checks(read: StatusChecks | None, *, owner: StatusChecks | None = None,
                      catchup: CatchupChecks | None = None) -> None:
    """Where ``logins`` is read from: the HTTP server's startup checks with the logins read now, set while it runs
    (``AgentHTTPServer.start``). None: ``logins`` is ``unknown``; with ``owner``, only if ``owner`` is the reader
    (a server that stops clears its own, never a newer one's). ``catchup`` reads only the registry's cached windows."""
    global _status_checks, _catchup_checks
    if owner is None or _status_checks == owner:
        _status_checks = read
        _catchup_checks = catchup


def _item(key: str, title: str, *, context: str = "", since: datetime | None = None, href: str | None = None,
          ref: str | None = None, **facts: Any) -> dict[str, Any]:
    """One thing that waits: what it is (``title``), where it belongs (``context``: a task, a project), since when,
    where the dashboard shows it (``href``), what a chat calls it (``ref``) and the kind's own ``facts``."""
    return {"key": key, "title": title, "context": context, "since": since, "href": href, "ref": ref, "facts": facts}


def _task_name(row: Mapping[str, Any], task_id: str) -> str:
    return db_tasks.task_name(title=row.get("title"), goal_id=row.get("goal_id"), requirement=row.get("requirement"),
                              task_id=task_id)


def _gate_item(row: Mapping[str, Any]) -> dict[str, Any]:
    return _item(f"gate:{row['gate']['step_id']}", row["gate"]["topic"],
                 context=row["task"]["name"], since=row["gate"]["opened_at"],
                 href=f"#tasks/{row['task']['task_id']}?gate={row['gate']['step']}",
                 step=row["gate"]["step"], questions=row["agenda_open"])


async def _gates(_: Any) -> Read:
    listed = await gate_service.list_gates({"state": "open", "waiting_on": "user", "limit": str(SHOWN)})
    return [_gate_item(row) for row in listed["gates"]], listed["total"]


async def _confirmation_items(managers: Any) -> list[dict[str, Any]] | None:
    waiting = await card_actions.pending_confirmations(managers)
    if waiting is None:
        return None
    now = datetime.now(timezone.utc)
    items = []
    names = {agent.agent_id: getattr(agent, "name", None) or agent.agent_id
             for agent in managers.agents()} if managers is not None else {}
    for row in waiting:
        manager_id = row["manager_id"]
        items.append(_item(
            f"confirmation:{row['confirmation_id']}", row["impact"] or row["tool"],
            context=names.get(manager_id, manager_id), since=now - timedelta(seconds=row["waited_seconds"]),
            href=_chat_href(manager_id, row["chat"]) if row["channel"] == "web" else None,
            ref=row["confirmation_id"], tool=row["tool"], channel=row["channel"],
            expires_in_seconds=row["expires_in_seconds"]))
    items.sort(key=lambda item: item["since"])
    return items


async def _confirmations(managers: Any) -> Read:
    items = await _confirmation_items(managers)
    if items is None:
        return NotRead.UNKNOWN
    return items[:SHOWN], len(items)


def _chat_href(manager_id: str, conversation: str | None) -> str:
    """The dashboard chat with ``manager_id`` open on the web conversation that asked (the Chat page's query: the
    default Manager and no conversation are left out)."""
    query = urlencode({key: value for key, value in (("target", manager_id if manager_id != "manager" else None),
                                                     ("conversation", conversation)) if value})
    return f"#chat?{query}" if query else "#chat"


_SCHEDULE_ALERTS = (("failures", "failures_alert_at"), ("stale", "stale_alert_at"),
                    ("deadline", "deadline_alert_at"), ("overlap", "overlap_alert_at"))


def _schedule_items(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    from .unanswered_pushes import PauseReason

    items = []
    for row in rows:
        alerts = [name for name, column in _SCHEDULE_ALERTS if row[column] is not None]
        moments = [row[column] for _, column in _SCHEDULE_ALERTS if row[column] is not None]
        if row.get("pause_reason") == PauseReason.UNANSWERED:
            alerts.append(PauseReason.UNANSWERED.value)
            if row.get("paused_at") is not None:
                moments.append(row["paused_at"])
        since = min(moments) if moments else None
        items.append(_item(f"schedule:{row['id']}", row["name"], context=row["project_name"], since=since,
                           href=f"#schedules/{row['id']}", failures=row["consecutive_failures"], alerts=alerts,
                           last_status=row["last_status"], last_ok_at=row.get("last_ok_at")))
    return items


async def _schedules(_: Any) -> Read:
    items = _schedule_items(await db_schedules.failing())
    return items[:SHOWN], len(items)


def _login_failing(checks: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [check for check in checks
            if (str(check.get("id", "")).startswith("claude_") or check.get("id") in LOGIN_CHECKS)
            and check.get("status") == "fail"]


def _login_items(checks: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [_item(f"login:{check['id']}", str(check.get("label") or check["id"]), href="#settings/runtimes",
                  detail=str(check.get("detail") or ""), fix=str(check.get("fix") or ""))
            for check in _login_failing(checks)]


async def _logins(_: Any) -> Read:
    if _status_checks is None:
        return NotRead.UNKNOWN
    items = _login_items(await _status_checks())
    return items[:SHOWN], len(items)


def _intent_item(row: Mapping[str, Any]) -> dict[str, Any]:
    return _item(f"intent:{row['id']}", row["title"], context=row["type_name"], since=row["created_at"],
                 href="#intents", ref=leads_model.format_intent_id(row["id"]), priority=row["priority"],
                 confidence=row["confidence"], expires_at=row["expires_at"])


async def _intents(_: Any) -> Read:
    if not await leads_model.intents_enabled():
        return NotRead.OFF
    rows, count = await store.pending_intents(SHOWN)
    return [_intent_item(row) for row in rows], count


def _ledger_item(row: Mapping[str, Any]) -> dict[str, Any]:
    return _item(f"ledger:{row['id']}", row["title"], since=row["created_at"], ref=db_ledger.format_id(row["id"]),
                 kind=row["kind"], due_at=row["due_at"], overdue=row["overdue"])


async def _ledger(_: Any) -> Read:
    if not await db_ledger.enabled():
        return NotRead.OFF
    rows, count = await store.ledger_waiting(SHOWN)
    return [_ledger_item(row) for row in rows], count


def _channel_type(channel: str | None) -> str | None:
    """``lark`` for ``channel-lark`` or ``channel-lark-2``; a channel type stays as it is."""
    parsed = parse_channel_id(channel)
    return parsed.channel_type if parsed is not None else channel


_DELIVERY_HREF = {"subscription": "#schedules/{ref}", "intent": "#intents", "task_end": "#tasks/{ref}"}


def _delivery_item(row: Mapping[str, Any]) -> dict[str, Any]:
    title = _task_name(row, row["ref"]) if row["source"] == "task_end" else row["title"]
    if row.get("reason") == "final_notice_retry_exhausted":
        title += "（最终状态通知重试已耗尽）"
    return _item(f"delivery:{row['source']}:{row['key']}",
                 title, since=row["at"],
                 href=_DELIVERY_HREF[row["source"]].format(ref=row["ref"]),
                 ref=leads_model.format_intent_id(int(row["ref"])) if row["source"] == "intent" else None,
                 source=row["source"], channel=_channel_type(row["channel"]),
                 **({"reason": row["reason"]} if row.get("reason") else {}))


async def _deliveries(_: Any) -> Read:
    rows, count = await store.uncertain_deliveries(datetime.now(timezone.utc) - DELIVERY_WINDOW, SHOWN)
    catchup = await _catchup_items()
    return [*catchup, *(_delivery_item(row) for row in rows)][:SHOWN], count + len(catchup)


async def _catchup_items() -> list[dict[str, Any]]:
    if _catchup_checks is None:
        return []
    return [_item(check["id"], str(check["detail"]), context=str(check["label"]), href="#settings/channels",
                  source="catchup", channel="lark", state="dropped", fix=str(check["fix"]))
            for check in _catchup_checks()
            if str(check.get("id", "")).startswith("lark_catchup:") and check.get("status") == "fail"]


def _cost_item(row: Mapping[str, Any]) -> dict[str, Any]:
    if row.get("reason") == "unanswered":
        name = _plain(row.get("schedule_name") or row["scope_ref"])
        return _item(f"cost:{row['id']}", f"『{name}』{row['pushes']} 条推送没人回", context=row["scope_ref"],
                     since=row["created_at"], href="#settings/cost", ref=f"C-{row['id']}",
                     reason="unanswered", pushes=row["pushes"])
    label = "任务花费" if row["scope"] == "task" else "全局当天花费"
    return _item(f"cost:{row['id']}", f"{label}等你决定", context=row["scope_ref"], since=row["created_at"],
                 href="#settings/cost", ref=f"C-{row['id']}", currency=row["currency"],
                 limit_usd=row["limit_usd"], spent_usd=row["spent_usd"])


async def _cost(_: Any) -> Read:
    from . import cost_holds as service

    if not service.needs_service():
        return NotRead.OFF
    from ..db import cost_holds

    rows = await cost_holds.list_pending()
    return [_cost_item(row) for row in rows[:SHOWN]], len(rows)


async def _earliest_cost(_: Any) -> dict[str, Any] | None:
    read = await _cost(None)
    return _oldest(read[0]) if isinstance(read, tuple) else None


def _oldest(items: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """The earliest item by ``since``; the first when none is dated (logins have no clock)."""
    dated = [item for item in items if isinstance(item.get("since"), datetime)]
    if dated:
        return dict(min(dated, key=lambda item: item["since"]))
    return dict(items[0]) if items else None


async def _earliest_gates(_: Any) -> dict[str, Any] | None:
    from ..db import user_gates as db_user_gates

    # The list page is newest-opened first and capped. The card's earliest is the oldest user-waiting gate.
    rows = await db_user_gates.list_gates(state="open")
    waiting = [gate_service._listed_view(row) for row in rows]
    waiting = [row for row in waiting if row["gate"]["waiting_on"] == "user"]
    return _oldest([_gate_item(row) for row in waiting])


async def _earliest_confirmations(managers: Any) -> dict[str, Any] | None:
    items = await _confirmation_items(managers)
    return _oldest(items) if items is not None else None


async def _earliest_schedules(_: Any) -> dict[str, Any] | None:
    return _oldest(_schedule_items(await db_schedules.failing()))


async def _earliest_logins(_: Any) -> dict[str, Any] | None:
    if _status_checks is None:
        return None
    return _oldest(_login_items(await _status_checks()))


async def _earliest_intents(_: Any) -> dict[str, Any] | None:
    if not await leads_model.intents_enabled():
        return None
    row = await store.earliest_pending_intent()
    return None if row is None else _intent_item(row)


async def _earliest_ledger(_: Any) -> dict[str, Any] | None:
    if not await db_ledger.enabled():
        return None
    row = await store.earliest_ledger_item()
    return None if row is None else _ledger_item(row)


async def _earliest_deliveries(_: Any) -> dict[str, Any] | None:
    row = await store.earliest_uncertain_delivery(datetime.now(timezone.utc) - DELIVERY_WINDOW)
    return _oldest([*([] if row is None else [_delivery_item(row)]), *await _catchup_items()])


_READERS = {"gates": _gates, "confirmations": _confirmations, "schedules": _schedules, "logins": _logins,
            "intents": _intents, "ledger": _ledger, "deliveries": _deliveries, "cost": _cost}
_EARLIEST = {"gates": _earliest_gates, "confirmations": _earliest_confirmations, "schedules": _earliest_schedules,
             "logins": _earliest_logins, "intents": _earliest_intents, "ledger": _earliest_ledger,
             "deliveries": _earliest_deliveries, "cost": _earliest_cost}


async def earliest(kind: str, *, managers: Any = None) -> dict[str, Any] | None:
    """The oldest item of one kind, from its own read.

    ``collect`` keeps at most ``SHOWN`` items and several kinds are ordered by urgency, so the oldest can be
    missing from that list. This does not change the list ``/todo`` shows.
    """
    reader = _EARLIEST.get(kind)
    if reader is None:
        return None
    return await reader(managers)


async def collect(*, managers: Any = None) -> dict[str, Any]:
    """The summary now: every section in ``KINDS`` order (a feature that is off leaves its own out) and ``total``,
    the count of what the readable sections hold. Confirmations use durable cards when enabled, supplemented
    by the Manager fleet's memory; without either source the section is ``unknown``."""
    sections = []
    for kind in KINDS:
        try:
            read = await _READERS[kind](managers)
        except Exception as exc:
            logger.exception("needs-you: could not read %s", kind)
            sections.append({"kind": kind, "state": "error", "count": 0, "items": [],
                             "error": f"{type(exc).__name__}: {exc}"[:300]})
            continue
        if read is NotRead.OFF:
            continue
        if read is NotRead.UNKNOWN:
            sections.append({"kind": kind, "state": "unknown", "count": 0, "items": [], "error": None})
            continue
        items, count = read
        sections.append({"kind": kind, "state": "ok", "count": count, "items": items, "error": None})
    return {"read_at": datetime.now(timezone.utc), "total": sum(s["count"] for s in sections if s["state"] == "ok"),
            "sections": sections}


# --- /todo: the summary in a chat, written by code -------------------------------------------------------------

# "/todo" and Telegram's "/todo@bot".
_COMMAND = re.compile(r"^/todo(?:@\w+)?$", re.IGNORECASE)
FORBIDDEN = "「需要你处理」只给你本人看：在你和机器人的私聊或控制台里发 /todo。"
NOTHING = "现在没有需要你处理的事。"
_TITLES = {"gates": "等你回复的关卡", "confirmations": "等你确认的操作", "schedules": "失败的定时任务",
           "logins": "登录失效", "intents": "待处理的意图", "ledger": "账本里等你的事",
           "deliveries": "可能没送到的消息", "cost": "花费等你决定"}
_CHANNELS = {"web": "控制台聊天", "lark": "飞书", "lark_bot": "飞书", "telegram": "Telegram"}
_ALERTS = {"failures": "连续失败告警", "stale": "太久没成功", "deadline": "没在截止时间前成功", "overlap": "运行重叠",
           "unanswered": "没人回，已自动暂停"}
_LEDGER_KINDS = {"pending": "待办", "decision": "待确认的决定", "work_item": "在做的事"}
_DELIVERY_SOURCES = {"subscription": "订阅推送", "intent": "意图消息", "task_end": "任务结束通知", "catchup": "飞书消息补收失败"}
_HINTS = {"gates": "发 /gates 看编号，再用 /gate N <回答> 回答。",
          "confirmations": "在提问的那个聊天里回复「确认」，或回复别的放弃。",
          "cost": "回复「继续 {ref} <新上限美元>」提高上限并继续，或「停 {ref}」。",
          "intents": "回复「建 {ref}」建任务，「驳回 {ref}」驳回。"}


def is_command(text: str) -> bool:
    return _COMMAND.match(text.strip()) is not None


def _ago(moment: Any, now: datetime) -> str:
    if not isinstance(moment, datetime):
        return ""
    seconds = max(0.0, (now - moment).total_seconds())
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟"
    return f"{int(seconds // 3600)} 小时" if seconds < 172_800 else f"{int(seconds // 86_400)} 天"


# The most characters of a title or its context a chat line keeps.
TEXT_MAX_CHARS = 120


def _plain(text: Any) -> str:
    """Text others wrote (a gate's topic, an intent's or a ledger item's title, a task, project or schedule name) as
    one plain line: control and format characters dropped, whitespace and line breaks merged, at most
    ``TEXT_MAX_CHARS``. The /todo reply is stored in the chat's history, which a new Manager session reads back."""
    line = " ".join("".join(ch for ch in str(text or "") if unicodedata.category(ch) not in ("Cc", "Cf")
                            or ch in "\n\t").split())
    return line if len(line) <= TEXT_MAX_CHARS else line[:TEXT_MAX_CHARS - 1] + "…"


def _line(kind: str, item: Mapping[str, Any], now: datetime) -> str:
    facts = item["facts"]
    title, context, waited = _plain(item["title"]), _plain(item["context"]), _ago(item["since"], now)
    if kind == "gates":
        return f"- 【{context}】{facts['step']} {title}" + (f"（已等 {waited}）" if waited else "")
    if kind == "confirmations":
        where = _CHANNELS.get(facts["channel"] or "", facts["channel"] or "聊天")
        return f"- {title}（{where}，{max(1, facts['expires_in_seconds'] // 60)} 分钟内有效）"
    if kind == "schedules":
        alerts = "、".join(_ALERTS[name] for name in facts["alerts"])
        failures = f"连续失败 {facts['failures']} 次" if facts["failures"] else ""
        return f"- {title}（{context}；{'，'.join(part for part in (failures, alerts) if part) or '失败'}）"
    if kind == "logins":
        return f"- {title}：{facts['detail']}" + (f"。修复：{facts['fix']}" if facts["fix"] else "")
    if kind == "intents":
        return f"- {item['ref']} {title}（{context}" + ("，紧急" if facts["priority"] == "urgent" else "") + "）"
    if kind == "cost":
        if facts.get("reason") == "unanswered":
            return f"- {item['ref']} {title}"
        money = "等价花费" if facts["currency"] == "equivalent_usd" else "真钱"
        return f"- {item['ref']} {title}（{context}；{money} ${facts['spent_usd']:.2f} / 上限 ${facts['limit_usd']:.2f}）"
    if kind == "ledger":
        label = "已过期" if facts["overdue"] else _LEDGER_KINDS[facts["kind"]]
        return f"- {item['ref']} {title}（{label}）"
    if kind == "deliveries" and facts["source"] == "catchup":
        return f"- {_DELIVERY_SOURCES['catchup']}：{title}。{_plain(facts['fix'])}"
    where = _CHANNELS.get(facts["channel"] or "", facts["channel"] or "")
    return (f"- {_DELIVERY_SOURCES[facts['source']]}：{title}" + (f"（{item['ref']}）" if item["ref"] else "")
            + (f"，{where}" if where else "") + (f"，{waited}前" if waited else ""))


def summary_text(summary: Mapping[str, Any]) -> str:
    """The summary as a chat message: each section with something in it, or that could not be read. It says
    nothing waits only when every section was read and is empty."""
    now = datetime.now(timezone.utc)
    parts, unread = [], []
    for section in summary["sections"]:
        kind = section["kind"]
        if section["state"] == "error":
            unread.append(f"{_TITLES[kind]}：没能读取（{section['error']}）")
        elif section["state"] == "unknown":
            unread.append(f"{_TITLES[kind]}：这里读不到")
        elif section["state"] == "ok" and section["count"]:
            lines = [f"{_TITLES[kind]} {section['count']}：", *(_line(kind, item, now) for item in section["items"])]
            if section["count"] > len(section["items"]):
                lines.append(f"- 另有 {section['count'] - len(section['items'])} 项，见控制台「Needs you」页")
            if kind == "cost" and section["items"][0]["facts"].get("reason") == "unanswered":
                hint = f"回复「继续 {section['items'][0]['ref']}」继续推，或「停 {section['items'][0]['ref']}」暂停。"
            else:
                hint = _HINTS[kind].format(ref=section["items"][0]["ref"]) if kind in _HINTS else ""
            parts.append("\n".join(lines) + (f"\n\n{hint}" if hint else ""))
    if not parts and not unread:
        return NOTHING
    head = (f"需要你处理（{summary['total']} 项）：" if parts
            else "读到的来源里没有需要你处理的事，但下面这些没读到，不能说没有：")
    return "\n\n".join([head, *parts, *unread])


async def command_reply(envelope: MessageEnvelope, *, managers: Any = None) -> str:
    """The reply to ``/todo``: the summary in the operator's own turn, a refusal in any other."""
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
    return summary_text(await collect(managers=managers))
