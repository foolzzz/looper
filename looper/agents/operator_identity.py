"""Who the operator is in a chat (message-leads-intents RFC, section 5; completion-ledger RFC, section 4.3).

Each Lark or Telegram bot may name its operator's sender id in its configuration (``channel_configs.config`` key
``operator_sender_id``; Lark's open_ids differ per app, so it is per bot). A turn is the operator's own when it comes
from the dashboard's web chat without an API client, or from a direct chat whose sender is that id. Code decides it
from the turn's typed fields; no model can claim it. Setting and checking the field is the channel configuration's.
"""

from __future__ import annotations

OPERATOR_KEY = "operator_sender_id"


async def operator_sender_id(channel_id: str) -> str | None:
    """The operator's sender id on bot ``channel_id``: its configuration's ``operator_sender_id``, or None when it is
    unset, empty or the bot is unknown."""
    from ..db import channel_configs

    row = await channel_configs.get_config(channel_id)
    value = ((row or {}).get("config") or {}).get(OPERATOR_KEY)
    return value.strip() if isinstance(value, str) and value.strip() else None


def is_operator(*, channel: str | None, group: bool, sender: str | None, client_id: str | None,
                operator_id: str | None) -> bool:
    """Whether a turn is the operator's own: the web chat without an API client, or a direct (not group) chat whose
    sender equals ``operator_id`` (which must be set)."""
    if channel == "web":
        return client_id is None
    return not group and operator_id is not None and sender is not None and sender == operator_id
