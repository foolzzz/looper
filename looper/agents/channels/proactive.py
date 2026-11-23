"""Record only explicitly typed proactive messages, after their channel has delivered them."""
from __future__ import annotations

from .. import chat_settings, turn_metrics
from ..message import MessageEnvelope
from ..task_notice import Notice
from .base import DeliveryReceipt


def record_delivery(channel_id: str, envelope: MessageEnvelope, receipt: DeliveryReceipt, *,
                    chat: str = "", group: bool = False) -> None:
    if not chat_settings.metrics_enabled():
        return
    raw = envelope.metadata.get("proactive")
    if not isinstance(raw, dict):
        return
    try:
        ref = turn_metrics.ProactiveRef(turn_metrics.ProactiveSource(raw["source"]), raw.get("source_ref"),
                                       raw.get("manager_id"))
    except (KeyError, TypeError, ValueError):
        return
    notice = Notice.from_payload(envelope.payload)
    dedupe_key = envelope.message_id
    if notice is not None:
        chat, group = notice.target.id, notice.target.group
        marked = envelope.metadata.get("task_notice")
        if isinstance(marked, dict):
            dedupe_key = marked.get("dedupe_key") or dedupe_key
    turn_metrics.record_proactive(ref, channel_id=channel_id, chat=chat, group=group,
                                  external_id=receipt.external_id, outcome=receipt.result.value,
                                  dedupe_key=dedupe_key)
