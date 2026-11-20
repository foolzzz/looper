"""A task's end as a completion event, and the Manager's completion turn that may tell it (manager completion
ledger RFC, C1 and C2).

An end that has a chat to tell is written to ``manager_events`` with the code notice it would send (``fallback``).
With ``manager.completion_turns`` off (the default) every event is ``skipped`` (``off``): the notice goes out as it
always did, and the row only counts it. With it ``dm_only``, the end of a task asked for in a direct chat or the
dashboard chat, whose own Manager handles the report, is ``pending``: the Manager is woken (``COMPLETION_DUE``),
claims the chat's pending events and runs a read-only ``[COMPLETION]`` turn; its reply, with each task's link added
by code, replaces the notice. Whatever goes wrong (the turn fails, runs past ``TURN_SECONDS``, is stopped, replies
nothing, or the reply cannot be handed to the channel; the Manager or the switch was turned off), the events fall
back and their notices go out exactly as recorded; a Manager that starts does the same for the events a restart
left ``running`` (``recover``). A group chat's end is ``skipped`` (``group_chat``), and so is
one whose report reached another Manager (``manager_off``). A schedule's run message is recorded ``skipped``
(``off``) whatever the switch says: its turn is not built yet.

A replayed message finds its row: it is not sent again, unless the first attempt never handed the notice to the
channel. When the database cannot be written, the notice is sent anyway.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..db import manager_events as store, tasks as db_tasks
from . import chat_settings
from .links import task_link
from .message import AgentAddress, MessageEnvelope, MessageType, channel_address, parse_channel_id
from .task_notice import Notice, NotifyChannel, lead, link_line

logger = logging.getLogger(__name__)

WEB_CHANNEL_ID = "channel-web"
LEAD_CHARS = 600
# A notice never waits longer than this for the event table (a lock wait or an exhausted pool).
DB_TIMEOUT_SECONDS = 3.0
# A completion turn's reply goes out only within this long from the claim; else the notices do.
TURN_SECONDS = 120.0
# A turn past its deadline gets this long to end once stopped, and again once cancelled.
END_SECONDS = 10.0
# A wake-up whose events could not be read is queued again after this long.
RETRY_SECONDS = 5.0


@dataclass(frozen=True)
class Target:
    """The chat completion events wait in: its Manager, the bot (or ``channel-web``) and the chat."""

    manager_id: str
    channel_id: str
    chat: str

    def payload(self) -> dict[str, str]:
        return {"manager_id": self.manager_id, "channel_id": self.channel_id, "chat": self.chat}

    @classmethod
    def from_payload(cls, raw: Any) -> Target | None:
        if not isinstance(raw, Mapping):
            return None
        values = [raw.get(key) for key in ("manager_id", "channel_id", "chat")]
        if not all(isinstance(value, str) and value for value in values):
            return None
        return cls(*values)

    @classmethod
    def of(cls, row: Mapping[str, Any]) -> Target:
        return cls(row["manager_id"], row["channel_id"], row["chat"])

    @property
    def key(self) -> tuple[str, str, str]:
        return self.manager_id, self.channel_id, self.chat


def target_kind(notice: Notice) -> str:
    """Where a Telegram or Lark notice goes: a group chat, or one person (a direct message, or a private chat)."""
    return "group" if notice.target.group else "dm"


def facts(envelope: MessageEnvelope, *, name: str = "", chat_id: str | None = None) -> dict[str, Any]:
    """The ``completion_v1`` facts of an end: what the task is called, how it ended, the start of its result, where
    a worktree task's work is (``branch``), and the chat it was asked in (``chat_id``: a Lark direct chat's id, which
    its notice names by the person instead)."""
    payload = envelope.payload
    return {"version": "completion_v1", "task_id": payload.get("task_id"), "name": name,
            "status": payload.get("status"), "fail_kind": payload.get("fail_kind"),
            "result": {"lead": lead(payload.get("summary"))[:LEAD_CHARS],
                       "delivery_summary": (payload.get("delivery_summary") or "")[:LEAD_CHARS],
                       "branch": payload.get("branch_line") or None},
            "chat_id": chat_id}


async def _initial_state(manager_id: str, envelope: MessageEnvelope, target: str, *,
                         available: bool) -> tuple[str, str | None]:
    """Whether an end waits for the Manager's completion turn (``pending``), or is told by its notice at once
    (``skipped``, and why). Nothing is read while the switch is off."""
    if chat_settings.completion_turns() is chat_settings.CompletionTurns.OFF:
        return "skipped", "off"
    if target == "group":
        return "skipped", "group_chat"
    task_id = envelope.payload.get("task_id")
    if not available or envelope.metadata.get("handed_over_from"):
        return "skipped", "manager_off"
    # A report the task's own Manager could not take comes to the default Manager: the chat is not this one's.
    if (await db_tasks.manager_of([task_id])).get(task_id) != manager_id:
        return "skipped", "manager_off"
    return "pending", None


async def _record(manager_id: str, task_id: str | None, *, decide: Any = None,
                  **fields: Any) -> store.Recorded | None:
    """The event, or None when there is none to read (no task to name, or the database failed): the caller then
    sends the notice as before. ``decide`` answers its state and reason (``skipped``, ``off`` without one)."""
    if not task_id:
        return None

    async def write() -> store.Recorded:
        state, reason = await decide() if decide is not None else ("skipped", "off")
        return await store.record(manager_id=manager_id, task_id=task_id, state=state, reason=reason, **fields)

    try:
        recorded = await asyncio.wait_for(write(), DB_TIMEOUT_SECONDS)
    except Exception:  # includes the timeout
        logger.exception("manager_event not recorded task=%s: sending the notice anyway", task_id)
        return None
    logger.info("manager_event recorded id=%s manager=%s task=%s target=%s state=%s reason=%s inserted=%s",
                recorded.row["id"], manager_id, task_id, fields["target_kind"], recorded.row["state"],
                recorded.row["reason"], recorded.inserted)
    return recorded


async def record_notice_end(manager_id: str, envelope: MessageEnvelope, notice: Notice, *,
                            available: bool = True) -> store.Recorded | None:
    """A Telegram or Lark task's end, to be sent as ``notice`` (a ``CHAT_NOTICE``). ``available``: whether this
    Manager can take a completion turn (it runs, and is not turned off)."""
    kind = target_kind(notice)
    route = envelope.metadata.get("reply_route")
    chat_id = route.get("chat_id") if isinstance(route, dict) and isinstance(route.get("chat_id"), str) else None
    return await _record(
        manager_id, envelope.payload.get("task_id"), origin="chat", target_kind=kind,
        channel_id=notice.channel_id, chat=notice.target.id, dedupe_key=envelope.message_id,
        decide=lambda: _initial_state(manager_id, envelope, kind, available=available),
        payload=facts(envelope, name=envelope.metadata.get("task_title") or "", chat_id=chat_id),
        fallback=[{"via": "notice", "notice": notice.payload()}])


async def record_web_end(manager_id: str, envelope: MessageEnvelope, thread: str, text: str, *,
                         available: bool = True) -> store.Recorded | None:
    """A dashboard chat task's end, to be told in the chat session ``thread`` as ``text``."""
    return await _record(
        manager_id, envelope.payload.get("task_id"), origin="chat", target_kind="web", channel_id=WEB_CHANNEL_ID,
        chat=thread, session_key=thread, dedupe_key=envelope.message_id,
        decide=lambda: _initial_state(manager_id, envelope, "web", available=available),
        payload=facts(envelope, name=envelope.metadata.get("task_title") or "", chat_id=thread),
        fallback=[{"via": "web_thread", "thread": thread, "text": text, "correlation_id": envelope.correlation_id,
                   "origin_channel_id": envelope.metadata.get("origin_channel_id")}])


async def _schedule_state(target: str) -> tuple[str, str | None]:
    """A schedule run's end waits for the default Manager's completion turn when its message goes to a direct
    chat; to a chat bound to a Manager (a group) it is sent at once."""
    if chat_settings.completion_turns() is chat_settings.CompletionTurns.OFF:
        return "skipped", "off"
    if target != "dm":
        return "skipped", "group_chat"
    return "pending", None


async def record_schedule_run(manager_id: str, *, task_id: str | None, run_id: str, target: str, channel_id: str,
                              chat: str, text: str, name: str, status: str,
                              summary: str | None = None) -> store.Recorded | None:
    """A schedule run's end, whose message ``text`` goes to a chat; ``summary`` is its task's result."""
    return await _record(
        manager_id, task_id, origin="schedule", schedule_run_id=run_id, target_kind=target, channel_id=channel_id,
        chat=chat, dedupe_key=f"schedule_run:{run_id}", decide=lambda: _schedule_state(target),
        payload={"version": "completion_v1", "task_id": task_id, "name": name, "status": status,
                 "schedule_run_id": run_id, "result": {"lead": lead(summary)[:LEAD_CHARS]}},
        fallback=[{"via": "schedule", "run_id": run_id, "kind": "run", "text": text}])


async def mark_sent(recorded: store.Recorded | None) -> None:
    """The notice was handed to the channel."""
    if recorded is None:
        return
    try:
        await asyncio.wait_for(store.mark_sent(recorded.row["id"]), DB_TIMEOUT_SECONDS)
    except Exception:
        logger.exception("manager_event %s: could not record that its notice was sent", recorded.row["id"])


def waits_for_turn(recorded: store.Recorded | None) -> bool:
    """Whether the event waits for the Manager's completion turn: its notice is not sent now."""
    return recorded is not None and recorded.row["state"] == "pending"


# Called when an event waits for a turn: the completion service looks at once instead of at its next tick.
_poke: Any = None
# Sends a schedule run's message (the code notice, or the Manager's reply) through the scheduler and records it in
# the schedule's outbox: (run id, text, event id) -> its outbox status (``sent``, ``failed``, ``dashboard``).
_schedule_delivery: Any = None


def set_poke(poke: Any) -> None:
    global _poke
    _poke = poke


def set_schedule_delivery(deliver: Any) -> None:
    global _schedule_delivery
    _schedule_delivery = deliver


def poke() -> None:
    """An event waits for its turn: wake the completion service (none runs here: its next start finds it)."""
    if _poke is not None:
        _poke()


async def _deliver_schedule(run_id: str, text: str, event_id: int, *, send: bool = True) -> None:
    """Through the scheduler: send ``text`` as the run's message and record it in the schedule's outbox, or (not
    ``send``: the chat got it in a message the Manager sent for other tasks too) only record it as sent."""
    if _schedule_delivery is None:
        raise RuntimeError("no scheduler sends schedule messages here")
    if await _schedule_delivery(run_id, text, event_id, send=send) == "failed":
        raise RuntimeError(f"schedule run {run_id}: its message was not handed to a channel")


def wake(source: AgentAddress, target: Target) -> MessageEnvelope:
    """A ``COMPLETION_DUE`` for the chat ``target``, from and to its Manager ``source``."""
    return MessageEnvelope.create(source=source, target=source, message_type=MessageType.COMPLETION_DUE,
                                  payload=target.payload(), correlation_id=f"completion-{uuid.uuid4().hex[:12]}")


async def recover(bus: Any, source: AgentAddress) -> None:
    """A Manager starts (``source``): the events its earlier turns left ``running`` fall back (``restart``), and
    every fallen-back event whose notice was never sent sends it. Done before its message loop, so a replayed
    wake-up finds them settled; its pending events wait for the completion service."""
    unsent = await asyncio.wait_for(store.recover(source.agent_id), DB_TIMEOUT_SECONDS)
    for row in unsent:
        logger.info("completion fallback id=%s reason=%s", row["id"], row["reason"])
    await send_fallbacks(bus, source, unsent)


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


# --- The [COMPLETION] message (model-visible) ---

CONTRACT = """Write one message to this chat, in the user's language:
- First, one sentence per task: what it delivered, or why it failed.
- Then only what the user needs from here: the key outputs the result names (a pull request, a file, a branch to merge), and the decisions the facts above leave to the user (a branch to merge, a failure to retry or drop). Do not make up decisions; when there are none, say nothing about them.
- At most two next steps, each as a question ("要我…吗？"). In this turn you cannot act: your tools only read, and a change answers needs_user_turn. Never say you started, created or changed anything.
- Looper adds each task's link after your message: do not write links, and do not repeat the whole result.
- At most 8 lines. No headings, no tables, no account of your tool calls.
Read with your tools (looper_tasks get, looper_costs) only when the facts above are not enough."""


def _duration(start: Any, end: Any) -> str | None:
    if not (isinstance(start, datetime) and isinstance(end, datetime)):
        return None
    minutes = max(0, round((end - start).total_seconds() / 60))
    if minutes < 1:
        return "under a minute"
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if minutes else f"{hours} h"


async def task_facts(task_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
    """What the message says of each task besides the event's facts: its project, how long it ran and what it cost;
    an empty entry for a task that cannot be read (the message then leaves those out)."""
    from ..db import llm_records as db_llm_records, projects as db_projects

    found: dict[str, dict[str, Any]] = {task_id: {} for task_id in task_ids}
    try:
        costs = await db_llm_records.task_costs(list(task_ids))
        for task_id in task_ids:
            task = await db_tasks.get_task(task_id)
            if task is None:
                continue
            project = await db_projects.get_project(task["project_id"]) if task.get("project_id") else None
            end = task.get("completed_at") or task.get("attempt_ended_at") or task.get("updated_at")
            cost = (costs.get(task_id) or {}).get("cost_usd")
            found[task_id] = {"project": (project or {}).get("name") or task.get("project_id"),
                              "duration": _duration(task.get("created_at"), end),
                              "cost_usd": float(cost) if cost is not None else None}
    except Exception:
        logger.warning("completion turn: could not read the tasks' projects, times and costs", exc_info=True)
    return found


def _indented(text: str) -> str:
    return "\n   ".join(line for line in text.strip().splitlines() if line.strip())


def completion_message(rows: Sequence[Mapping[str, Any]], *, channel_line: str,
                       more: Mapping[str, Mapping[str, Any]]) -> str:
    """The ``[COMPLETION]`` message: the ended tasks' facts (``more`` from ``task_facts``), then the contract."""
    lines = ["[COMPLETION] Looper reports tasks that ended. This is not a message from the user, and nobody asked "
             "you anything.", channel_line, f"Ended tasks ({len(rows)}):"]
    for number, row in enumerate(rows, 1):
        payload = _json(row["payload"])
        task_id, extra = row["task_id"], more.get(row["task_id"]) or {}
        name = " ".join(str(payload.get("name") or "").split()) or task_id
        how = "completed" if payload.get("status") == "completed" else "failed"
        if payload.get("fail_kind") == db_tasks.FailKind.TASK_BUDGET.value:
            how += " at its spend cap"
        spent = [part for part in (
            f"in {extra['duration']}" if extra.get("duration") else None,
            f"${extra['cost_usd']:.2f}" if extra.get("cost_usd") is not None else None) if part]
        where = f" (project {extra['project']})" if extra.get("project") else ""
        if row.get("origin") == "schedule":
            label, asked = task_id, f'a run of the schedule "{name}", whose messages come to this chat.'
        else:
            label, asked = f'{task_id} "{name}"', "asked for in this chat."
        lines.append(f'{number}. {label}{where}: {how}{" " + ", ".join(spent) if spent else ""}; {asked}')
        result = payload.get("result") or {}
        if result.get("lead"):
            lines.append(f"   Result (the start of its summary): {_indented(result['lead'])}")
        if result.get("delivery_summary"):
            lines.append(f"   Delivery: {_indented(result['delivery_summary'])}")
        if result.get("branch"):
            lines.append(f"   Branch: {_indented(result['branch'])}")
    return "\n".join([*lines, "", CONTRACT])


# --- What goes to the chat ---

def link_block(rows: Sequence[Mapping[str, Any]], *, base: str) -> str:
    """What code adds after the Manager's reply: each task's link (named when there are several), and where a
    worktree task's work is; as the chat's notice writes them (Lark and the dashboard: a Markdown link)."""
    lines = []
    for row in rows:
        payload = _json(row["payload"])
        parsed = parse_channel_id(row["channel_id"])
        channel = NotifyChannel(parsed.channel_type) if parsed is not None else NotifyChannel.LARK
        link = link_line(task_link(base, row["task_id"]), channel)
        name = " ".join(str(payload.get("name") or "").split())
        lines.append(f"「{name or row['task_id']}」{link}" if len(rows) > 1 else link)
        if branch := (payload.get("result") or {}).get("branch"):
            lines.append(branch)
    return "\n".join(lines)


async def send_reply(bus: Any, source: AgentAddress, rows: Sequence[Mapping[str, Any]], text: str, *,
                     metadata: Mapping[str, Any] | None = None) -> None:
    """Hand the completion turn's reply to the chat its events name, the way their notice would go: into the web
    chat session, or as a ``CHAT_NOTICE`` to the person (or chat) the notice names, with its text replaced."""
    first = _json(rows[0]["fallback"])[0]
    if first["via"] == "schedule":
        await _deliver_schedule(first["run_id"], text, rows[0]["id"])
        await _record_schedule_replies(rows[1:], text)
        return
    if first["via"] == "web_thread":
        envelope = MessageEnvelope.create(
            source=source, target=channel_address("web"), message_type=MessageType.AGENT_REPLY,
            payload={"text": text, "chat_session_id": first["thread"]}, correlation_id=first.get("correlation_id") or "",
            metadata={"origin_channel_id": first.get("origin_channel_id")})
    elif first["via"] == "notice":
        notice = Notice.from_payload(first["notice"])
        if notice is None:
            raise ValueError(f"manager event {rows[0]['id']}: its notice cannot be read")
        envelope = MessageEnvelope.create(
            source=source, target=channel_address(notice.channel.value, notice.channel_id),
            message_type=MessageType.CHAT_NOTICE, payload={**notice.payload(), "text": text},
            correlation_id=f"completion-{rows[0]['id']}",
            metadata={"origin_channel_id": notice.channel_id, **(metadata or {})})
    else:
        raise ValueError(f"manager event {rows[0]['id']}: a {first['via']} event has no completion turn")
    await bus.publish(envelope)
    await _record_schedule_replies(rows[1:], text)


async def _record_schedule_replies(rows: Sequence[Mapping[str, Any]], text: str) -> None:
    """The schedule runs among a batch's other events: their outbox records the reply that told them (logged when
    it cannot: the reply is out)."""
    for row in rows:
        item = _json(row["fallback"])[0]
        if item["via"] != "schedule":
            continue
        try:
            await _deliver_schedule(item["run_id"], text, row["id"], send=False)
        except Exception:
            logger.exception("manager_event %s: its schedule outbox could not record the reply", row["id"])


async def send_fallbacks(bus: Any, source: AgentAddress, rows: Sequence[Mapping[str, Any]]) -> None:
    """Send each event's code notice as it was recorded, and note it was handed to the channel. One that cannot be
    sent is logged; the others still go."""
    for row in rows:
        try:
            for item in _json(row["fallback"]):
                if item["via"] == "notice":
                    notice = item["notice"]
                    await bus.publish(MessageEnvelope.create(
                        source=source, target=channel_address(notice["channel"], notice["channel_id"]),
                        message_type=MessageType.CHAT_NOTICE, payload=notice,
                        correlation_id=f"completion-{row['id']}",
                        metadata={"origin_channel_id": notice["channel_id"]}))
                elif item["via"] == "web_thread":
                    await bus.publish(MessageEnvelope.create(
                        source=source, target=channel_address("web"), message_type=MessageType.AGENT_REPLY,
                        payload={"text": item["text"], "chat_session_id": item["thread"]},
                        correlation_id=item.get("correlation_id") or "",
                        metadata={"origin_channel_id": item.get("origin_channel_id")}))
                elif item["via"] == "schedule":
                    await _deliver_schedule(item["run_id"], item["text"], row["id"])
                else:
                    raise ValueError(f"manager event {row['id']}: an unknown notice ({item['via']})")
        except Exception:
            logger.exception("manager_event %s: its notice could not be sent", row["id"])
            continue
        try:
            await asyncio.wait_for(store.mark_sent(row["id"]), DB_TIMEOUT_SECONDS)
        except Exception:
            logger.exception("manager_event %s: could not record that its notice was sent", row["id"])


async def fall_back(bus: Any, source: AgentAddress, *, reason: str, batch: uuid.UUID | None = None,
                    target: Target | None = None, states: Sequence[str] = ("pending",),
                    record_id: int | None = None) -> list[dict[str, Any]]:
    """Move the events (of ``batch``, or the chat ``target``'s in ``states``) to ``fallback`` and send their
    notices; the events moved."""
    rows = await store.fall_back(reason=reason, batch=batch, target=target.key if target is not None else None,
                                 states=("running",) if batch is not None else states, record_id=record_id)
    for row in rows:
        logger.info("completion fallback id=%s reason=%s", row["id"], reason)
    await send_fallbacks(bus, source, rows)
    return rows
