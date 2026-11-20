"""Enabled Lark and Telegram bots whose configuration has no operator id.

Settings uses this when a switch turns the ledger on (assistant layer RFC, section 5.7; completion-ledger RFC,
section 4.3). In those bots' direct chats a decision is recorded as relayed until Settings → Channels has
"My ID". The channel configuration already checks the id; this only notices that ``operator_sender_id`` is
absent or blank.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..operator_identity import OPERATOR_KEY

_BOT_TYPES = frozenset({"telegram", "lark", "lark_bot"})


@dataclass(frozen=True)
class BotRef:
    """One enabled bot that has no operator id."""

    channel_id: str
    channel_type: str
    name: str


async def missing() -> list[BotRef]:
    """Enabled ``telegram``, ``lark`` and ``lark_bot`` configurations with no ``operator_sender_id``."""
    from ...db import channel_configs

    found: list[BotRef] = []
    for row in await channel_configs.list_configs():
        if not row.get("enabled") or row.get("channel_type") not in _BOT_TYPES:
            continue
        config = row.get("config")
        value = config.get(OPERATOR_KEY) if isinstance(config, dict) else None
        if isinstance(value, str) and value.strip():
            continue
        label = row.get("display_name")
        name = label.strip() if isinstance(label, str) and label.strip() else row["channel_id"]
        found.append(BotRef(row["channel_id"], row["channel_type"], name))
    return found
