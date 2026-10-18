"""Telegram and Lark answers in chat_messages, stored once their channel has tried to send them.

The Manager stores the user's message when it comes in and names the record of
its answer in the answer's metadata (``CHAT_RECORD``: the chat's session and the
message id); the channel's outbound loop stores the answer after sending it, as
sent, or as not delivered with why (``DeliveryFailure``) and the channel's
error. An answer without a record is not stored: one about a user gate (its
discussion keeps it) or a Telegram group's. The web channel stores its own.
"""

from __future__ import annotations

import logging
from typing import Any

from ...db import chat as db_chat
from ..message import MessageEnvelope
from .base import DeliveryFailure, DeliveryReceipt

logger = logging.getLogger(__name__)

CHAT_RECORD = "chat_record"
# What is kept of the channel's error with an answer it did not deliver.
DELIVERY_ERROR_MAX_CHARS = 500


def chat_record(session_id: str, message_id: str) -> dict[str, str]:
    """The metadata entry that has the channel store an answer under ``session_id`` as ``message_id``."""
    return {"session_id": session_id, "message_id": message_id}


async def record_reply(channel: str, envelope: MessageEnvelope, receipt: DeliveryReceipt) -> None:
    """Store the answer ``envelope`` as ``receipt`` says the channel delivered it, when the Manager named its
    record. A storage failure is logged, never raised: the channel goes on."""
    record: Any = envelope.metadata.get(CHAT_RECORD)
    text = str(envelope.payload.get("text") or "")
    if not isinstance(record, dict) or not record.get("session_id") or not record.get("message_id") or not text:
        return
    failure = None if receipt.success else receipt.failure or DeliveryFailure.SEND_FAILED
    error = None if receipt.success or not receipt.error else receipt.error[:DELIVERY_ERROR_MAX_CHARS]
    try:
        await db_chat.save_message(session_id=record["session_id"], message_id=record["message_id"],
                                   role="assistant", content=text, channel=channel, delivery_failure=failure,
                                   delivery_error=error)
    except Exception:
        logger.warning("could not store a %s answer in %s", channel, record["session_id"], exc_info=True)
