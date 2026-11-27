"""Bridge trusted channel identity and live settings into a Manager user turn.

Only user turns receive a speaker, and they receive it whatever the switch says; the switch only decides
whether the turn context is rendered. Background turns keep their existing channel label and permissions.
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


async def identify(envelope: MessageEnvelope | None, turn: Turn | None) -> TurnContext | None:
    """Classify the speaker of a user turn and set ``turn.speaker``, whatever the switch or the session routing.

    Authorization (high-risk calls, confirmations) depends on it, so it never waits for the turn-context switch.
    Identity is resolved afresh for this bot, failing closed to a member when its config cannot be read."""
    if turn is None or turn.autonomous or envelope is None:
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
    try:
        context = turn_context.classify(
            envelope, operator_id=operator_id, allowlist=allowlist, now=datetime.now(timezone.utc),
            zone=turn_context.zone(), language=chat_settings.reply_language(), persona_address=None)
    except ValueError:
        # A channel the classifier does not know: no speaker, which a bot channel's authorization refuses.
        logger.warning("could not classify the speaker of a turn on channel %r", turn.channel)
        return None
    turn.speaker = context.speaker
    return context


async def prepare(envelope: MessageEnvelope | None, turn: Turn | None) -> TurnContext | None:
    """The turn context to render: only with the switch on (the speaker is set either way, see ``identify``)."""
    context = await identify(envelope, turn)
    return context if chat_settings.turn_context_enabled() else None
