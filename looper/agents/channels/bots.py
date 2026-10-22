"""What makes two channel rows the same bot (RFC §9.1)."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from ... import env

BOT_TYPES = ("telegram", "lark", "lark_bot")

_TELEGRAM_BOT_ID = re.compile(r"^(\d+):")


def bot_key(channel_type: str, config: Mapping[str, Any], secret: str | None, *, env_token: str = "") -> str | None:
    """What makes two rows the same bot (RFC §9.1): Telegram, the bot id that starts its token ("<bot id>:<secret>",
    digits before the colon; the built-in row with no stored token uses ``env_token``); lark-cli, its profile ("" is
    lark-cli's active profile, the built-in row's default); the Lark bot token channel, its app_id. None when the
    row names none yet (a Telegram row without a token, a Lark bot row without app_id): it cannot clash.

    Never returns the secret: a Telegram key carries only the bot id."""
    if channel_type == "telegram":
        match = _TELEGRAM_BOT_ID.match(secret or env_token or "")
        return f"telegram:{match.group(1)}" if match else None
    if channel_type == "lark":
        return f"lark:{config.get('profile') or ''}"
    if channel_type == "lark_bot":
        app_id = config.get("app_id") or ""
        return f"lark_bot:{app_id}" if app_id else None
    return None


def builtin_env_token(config: Mapping[str, Any]) -> str:
    """The Telegram token the built-in row falls back to: env[config.bot_token_env or LOOPER_TELEGRAM_BOT_TOKEN]."""
    return env.get(config.get("bot_token_env") or "LOOPER_TELEGRAM_BOT_TOKEN", "")


def clashes(rows: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """For rows of one type in order ({channel_id, channel_type, config, secret}), each later row whose key an
    earlier row has: {later channel_id: earlier channel_id}."""
    first: dict[str, str] = {}
    out: dict[str, str] = {}
    for row in rows:
        key = bot_key(row["channel_type"], row.get("config") or {}, row.get("secret"),
                      env_token=row.get("env_token") or "")
        if key is None:
            continue
        if key in first:
            out[row["channel_id"]] = first[key]
        else:
            first[key] = row["channel_id"]
    return out
