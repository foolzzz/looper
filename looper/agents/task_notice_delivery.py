"""Delivery policy for code task-end notices; completion turns and other chat notices keep their own send points."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from ..db import chat as db_chat, proactive_deliveries as db_deliveries, tasks as db_tasks
from . import chat_settings, proactive_delivery as delivery
from .channels.base import DeliveryReceipt
from .message import MessageEnvelope, MessageType, channel_thread_id
from .task_notice import Notice, TargetKind

logger = logging.getLogger(__name__)
_NOTICE = "task_notice"


def metadata(task_id: str | None, message_id: str) -> dict[str, Any]:
    """Mark a task-end notice with its original status id, retained by completion fallbacks. Off: no change to
    the journaled envelope, not even its metadata."""
    if not chat_settings.delivery_enabled() or not task_id:
        return {}
    return {_NOTICE: {"task_id": task_id, "dedupe_key": f"task_notice:{message_id}"}}


async def send(envelope: MessageEnvelope, post: Callable[[MessageEnvelope], Awaitable[DeliveryReceipt]]) -> DeliveryReceipt:
    """Decide immediately before the channel sends; mark sent only on its successful receipt. Off or an unmarked
    notice: the existing adapter runs unchanged. A failed decision keeps the existing send behavior."""
    marked = envelope.metadata.get(_NOTICE)
    if not chat_settings.delivery_enabled() or envelope.message_type is not MessageType.CHAT_NOTICE or not marked:
        return await post(envelope)
    notice = Notice.from_payload(envelope.payload)
    if notice is None:
        return await post(envelope)  # the adapter reports its existing input error
    key = marked["dedupe_key"]
    decided = None
    try:
        chat = notice.target.id
        if notice.channel.is_lark and notice.target.kind is TargetKind.USER:
            chat = await db_tasks.direct_chat_of(notice.channel_id, chat)
        session = None if chat is None else channel_thread_id(notice.channel_id, chat, "manager", "manager")
        last_reply = await db_chat.last_user_message_at(session) if session else None
        decided = await delivery.decide(
            delivery.Delivery(delivery.DeliveryKind.TASK_NOTICE, delivery.Urgency.NORMAL,
                              delivery.ChatKind.GROUP if notice.target.group else delivery.ChatKind.DIRECT),
            dedupe_key=key, channel_id=notice.channel_id, chat=notice.target.id,
            last_user_message_at=last_reply, task_id=marked["task_id"])
    except Exception:
        logger.exception("task notice %s: decision failed; sending as with the layer off", key)
    if decided is not None and decided.outcome is not delivery.Outcome.SEND:
        return DeliveryReceipt(notice.channel.value, True)
    receipt = await post(envelope)
    if decided is not None and receipt.success:
        try:
            await db_deliveries.record_receipt(key, "sent", receipt.external_id)
        except Exception:
            logger.exception("task notice %s: could not record the channel's receipt", key)
    return receipt
