"""Recording a task's end as a completion event (manager completion ledger RFC, C1).

An end that has a chat to tell is written to ``manager_events`` with the code notice it would send (``fallback``).
The completion turn that may one day replace that notice is not built yet: every event is ``skipped`` (``off``),
so the notice goes out as it always did, and the row only counts it. A replayed message finds its row: it is not
sent again, unless the first attempt never handed the notice to the channel. When the database cannot be
written, the notice is sent anyway.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..db import manager_events as store
from .message import MessageEnvelope
from .task_notice import Notice, lead

logger = logging.getLogger(__name__)

WEB_CHANNEL_ID = "channel-web"
LEAD_CHARS = 600
# A notice never waits longer than this for the event table (a lock wait or an exhausted pool).
DB_TIMEOUT_SECONDS = 3.0


def target_kind(notice: Notice) -> str:
    """Where a Telegram or Lark notice goes: a group chat, or one person (a direct message, or a private chat)."""
    return "group" if notice.target.group else "dm"


def facts(envelope: MessageEnvelope, *, name: str = "") -> dict[str, Any]:
    """The ``completion_v1`` facts of an end: what the task is called, how it ended and the start of its result."""
    payload = envelope.payload
    return {"version": "completion_v1", "task_id": payload.get("task_id"), "name": name,
            "status": payload.get("status"), "fail_kind": payload.get("fail_kind"),
            "result": {"lead": lead(payload.get("summary"))[:LEAD_CHARS],
                       "delivery_summary": (payload.get("delivery_summary") or "")[:LEAD_CHARS]}}


async def _record(manager_id: str, task_id: str | None, **fields: Any) -> store.Recorded | None:
    """The event, or None when there is none to read (no task to name, or the database failed): the caller then
    sends the notice as before."""
    if not task_id:
        return None
    try:
        recorded = await asyncio.wait_for(
            store.record(manager_id=manager_id, task_id=task_id, state="skipped", reason="off", **fields),
            DB_TIMEOUT_SECONDS)
    except Exception:  # includes the timeout
        logger.exception("manager_event not recorded task=%s: sending the notice anyway", task_id)
        return None
    logger.info("manager_event recorded id=%s manager=%s task=%s target=%s state=%s reason=%s inserted=%s",
                recorded.row["id"], manager_id, task_id, fields["target_kind"], recorded.row["state"],
                recorded.row["reason"], recorded.inserted)
    return recorded


async def record_notice_end(manager_id: str, envelope: MessageEnvelope, notice: Notice) -> store.Recorded | None:
    """A Telegram or Lark task's end, to be sent as ``notice`` (a ``CHAT_NOTICE``)."""
    return await _record(
        manager_id, envelope.payload.get("task_id"), origin="chat", target_kind=target_kind(notice),
        channel_id=notice.channel_id, chat=notice.target.id, dedupe_key=envelope.message_id,
        payload=facts(envelope, name=envelope.metadata.get("task_title") or ""),
        fallback=[{"via": "notice", "notice": notice.payload()}])


async def record_web_end(manager_id: str, envelope: MessageEnvelope, thread: str, text: str) -> store.Recorded | None:
    """A dashboard chat task's end, to be told in the chat session ``thread`` as ``text``."""
    return await _record(
        manager_id, envelope.payload.get("task_id"), origin="chat", target_kind="web", channel_id=WEB_CHANNEL_ID,
        chat=thread, session_key=thread, dedupe_key=envelope.message_id,
        payload=facts(envelope, name=envelope.metadata.get("task_title") or ""),
        fallback=[{"via": "web_thread", "thread": thread, "text": text, "correlation_id": envelope.correlation_id,
                   "origin_channel_id": envelope.metadata.get("origin_channel_id")}])


async def record_schedule_run(manager_id: str, *, task_id: str | None, run_id: str, target: str, channel_id: str,
                              chat: str, text: str, name: str, status: str) -> store.Recorded | None:
    """A schedule run's end, whose message ``text`` goes to a chat."""
    return await _record(
        manager_id, task_id, origin="schedule", schedule_run_id=run_id, target_kind=target, channel_id=channel_id,
        chat=chat, dedupe_key=f"schedule_run:{run_id}",
        payload={"version": "completion_v1", "task_id": task_id, "name": name, "status": status,
                 "schedule_run_id": run_id},
        fallback=[{"via": "schedule", "run_id": run_id, "kind": "run", "text": text}])


async def mark_sent(recorded: store.Recorded | None) -> None:
    """The notice was handed to the channel."""
    if recorded is None:
        return
    try:
        await asyncio.wait_for(store.mark_sent(recorded.row["id"]), DB_TIMEOUT_SECONDS)
    except Exception:
        logger.exception("manager_event %s: could not record that its notice was sent", recorded.row["id"])
