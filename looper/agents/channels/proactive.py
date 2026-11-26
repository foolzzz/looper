"""Best-effort recording of explicitly typed proactive sends and their receipts."""
from __future__ import annotations

import asyncio
import logging

from .. import turn_metrics
from ..message import MessageEnvelope
from ..task_notice import Notice
from .base import DeliveryReceipt

logger = logging.getLogger(__name__)


async def store_body(ref: turn_metrics.ProactiveRef | None, text: str, *, channel_id: str, chat: str,
                     group: bool, dedupe_key: str) -> None:
    if ref is None:
        return
    from ...db import proactive_deliveries

    try:
        await asyncio.wait_for(proactive_deliveries.store_body({
            "dedupe_key": dedupe_key, "source": ref.source.value, "source_ref": ref.source_ref,
            "manager_id": ref.manager_id, "channel_id": channel_id, "chat": chat,
            "chat_kind": "group" if group else "direct"}, text), timeout=1)
    except Exception:
        logger.warning("could not store proactive body; sending anyway", exc_info=True)


def _reference(envelope: MessageEnvelope) -> tuple[turn_metrics.ProactiveRef | None, str]:
    raw = envelope.metadata.get("proactive")
    key = envelope.message_id
    marked = envelope.metadata.get("task_notice")
    if isinstance(marked, dict):
        key = marked.get("dedupe_key") or key
    if not isinstance(raw, dict):
        return None, key
    try:
        return turn_metrics.ProactiveRef(turn_metrics.ProactiveSource(raw["source"]), raw.get("source_ref"),
                                        raw.get("manager_id")), key
    except (KeyError, TypeError, ValueError):
        return None, key


async def store_envelope_body(channel_id: str, envelope: MessageEnvelope, text: str, *,
                              chat: str = "", group: bool = False) -> None:
    ref, key = _reference(envelope)
    notice = Notice.from_payload(envelope.payload)
    if notice is not None:
        chat, group = notice.target.id, notice.target.group
    await store_body(ref, text, channel_id=channel_id, chat=chat, group=group, dedupe_key=key)


def record_delivery(channel_id: str, envelope: MessageEnvelope, receipt: DeliveryReceipt, *,
                    chat: str = "", group: bool = False) -> None:
    ref, dedupe_key = _reference(envelope)
    if ref is None:
        return
    notice = Notice.from_payload(envelope.payload)
    if notice is not None:
        chat, group = notice.target.id, notice.target.group
    turn_metrics.record_proactive(ref, channel_id=channel_id, chat=chat, group=group,
                                  external_id=receipt.external_id, outcome=receipt.result.value,
                                  dedupe_key=dedupe_key)
