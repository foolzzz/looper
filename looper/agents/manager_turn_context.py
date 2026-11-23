"""Bridge trusted channel identity and live settings into a Manager user turn.

The switch is checked before any identity lookup. Background turns keep their
existing channel label and permissions; only user turns receive a speaker.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from . import chat_settings, turn_context
from .channels.allowlist import OPERATOR_FIELD, allowlist_from_config
from .message import BOT_CHANNEL_TYPES, MessageEnvelope
from .ops import Turn
from .turn_context import TurnContext
from ..db import channel_configs

logger = logging.getLogger(__name__)


async def prepare(envelope: MessageEnvelope | None, turn: Turn | None) -> TurnContext | None:
    """Resolve identity afresh for this bot, failing closed when its config cannot be read."""
    if not chat_settings.turn_context_enabled() or turn is None or turn.autonomous or envelope is None:
        return None
    operator_id, allowlist = None, frozenset()
    if turn.channel in BOT_CHANNEL_TYPES:
        try:
            row = await channel_configs.get_config(turn.channel_id)
            config = (row or {}).get("config") or {}
            raw_id = config.get(OPERATOR_FIELD)
            operator_id = raw_id.strip() if isinstance(raw_id, str) and raw_id.strip() else None
            allowlist = allowlist_from_config(turn.channel_id, turn.channel, config).ids
        except Exception:
            # An unavailable/malformed identity must never acquire operator permissions.
            operator_id, allowlist = None, frozenset()
            logger.warning("could not read turn context bot identity; authorization requires the operator",
                           exc_info=True)
        turn.operator_id_missing = operator_id is None
    context = turn_context.classify(
        envelope, operator_id=operator_id, allowlist=allowlist, now=datetime.now(timezone.utc),
        zone=turn_context.zone(), language=chat_settings.reply_language(), persona_address=None)
    turn.speaker = context.speaker
    return context
