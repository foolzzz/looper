"""What makes two channel rows the same bot (RFC §9.1)."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from ... import env

BOT_TYPES = ("telegram", "lark", "lark_bot")
# The types whose rows can be the same bot as each other's: a lark-cli profile and a bot token channel can name one
# Lark app.
SAME_BOT_TYPES = {"telegram": ("telegram",), "lark": ("lark", "lark_bot"), "lark_bot": ("lark", "lark_bot")}

_TELEGRAM_BOT_ID = re.compile(r"^(\d+):")
# The built-in Lark rows, which today run side by side even when they act as one app: the app key does not make them
# the same bot as each other, only as an added bot.
_BUILTIN_LARK_IDS = {"channel-lark": "lark", "channel-lark_bot": "lark_bot"}


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


def bot_keys(channel_type: str, config: Mapping[str, Any], secret: str | None, *, env_token: str = "",
             app_id: str | None = None, channel_id: str | None = None) -> set[str]:
    """Every key that makes two rows the same bot: ``bot_key``'s, and for the Lark types the app the row acts as
    (``app_id``, from ``lark_app_id``), so a lark-cli profile and a bot token row of one app, or two names of one
    profile (the active profile and its own name), are the same bot.

    The app key has one form per built-in Lark row (``channel_id``): an added bot carries both, a built-in row only
    its own, so ``channel-lark`` and ``channel-lark_bot`` of one app are not the same bot (RFC §9.1: each runs)."""
    keys = {key for key in (bot_key(channel_type, config, secret, env_token=env_token),) if key is not None}
    if channel_type in ("lark", "lark_bot") and app_id:
        builtin = _BUILTIN_LARK_IDS.get(channel_id or "")
        keys.update(f"lark_app:{app_id}:{name}" for name in _BUILTIN_LARK_IDS.values() if builtin in (None, name))
    return keys


async def lark_app_id(channel_type: str, config: Mapping[str, Any]) -> str | None:
    """The Lark app a row acts as: a bot token row's app_id; a lark-cli row's profile's app, as lark-cli tells it now
    (None when it cannot say: such a row does not start)."""
    if channel_type == "lark_bot":
        return config.get("app_id") or None
    if channel_type != "lark":
        return None
    from . import lark_cli

    try:
        return (await lark_cli.identity(config.get("profile") or None, fresh=True)).app_id or None
    except lark_cli.LarkCliError:
        return None


def builtin_env_token(config: Mapping[str, Any]) -> str:
    """The Telegram token the built-in row without a stored token runs, in main's order: env[config.bot_token_env
    or LOOPER_TELEGRAM_BOT_TOKEN], else (unset or empty) env[LOOPER_TELEGRAM_BOT_TOKEN]. Serve's start and every
    same-bot check (the API, the config import, serve's ``clashes``) use this, so the token checked is the one run."""
    return (env.get(config.get("bot_token_env") or "LOOPER_TELEGRAM_BOT_TOKEN", "")
            or env.get("LOOPER_TELEGRAM_BOT_TOKEN", ""))


def clashes(rows: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """For rows in order ({channel_id, channel_type, config, secret, and optionally env_token and app_id}), each
    later row with a key (``bot_keys``) an earlier row has: {later channel_id: earlier channel_id}. A later row adds
    no keys of its own: it does not run."""
    first: dict[str, str] = {}
    out: dict[str, str] = {}
    for row in rows:
        keys = bot_keys(row["channel_type"], row.get("config") or {}, row.get("secret"),
                        env_token=row.get("env_token") or "", app_id=row.get("app_id"), channel_id=row["channel_id"])
        earlier = next((first[key] for key in sorted(keys) if key in first), None)
        if earlier is not None:
            out[row["channel_id"]] = earlier
            continue
        for key in keys:
            first[key] = row["channel_id"]
    return out
