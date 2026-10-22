"""Add, change and remove a Manager's bots over the HTTP API (RFC §9.1): the checks and the writes behind
``POST /api/channel-configs``, ``PUT`` and ``DELETE /api/channel-configs/<id>``. The routes live in
``looper.agents.http_server``; a refusal is a ``projects.RequestError`` it answers with.

A bot's token (the Telegram bot token or the Lark app secret) is registered with ``log_redaction`` as soon as it is
read, and no error text is built from the request body."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import asyncpg

from ...db import channel_configs as db_channel_configs
from ...db.channel_configs import BUILTIN_CHANNEL_IDS
from .. import log_redaction, projects
from ..profile_registry import SECRET_MASK
from .allowlist import AllowlistError, channel_view, normalized_config
from .bots import BOT_TYPES, SAME_BOT_TYPES, bot_keys, builtin_env_token, lark_app_id

HTTP_500 = "500 Internal Server Error"
TYPE_WORDS = {"telegram": "Telegram", "lark": "Lark (lark-cli)", "lark_bot": "Lark (bot token)"}
DISPLAY_NAME_MAX = 60
TOKEN_MAX = 4096
_POST_KEYS = {"channel_type", "manager_id", "display_name", "enabled", "config", "bot_token"}
_LOCK = "SELECT pg_advisory_xact_lock(hashtext('looper.channel_bots'))"


def _bad(message: str) -> projects.RequestError:
    return projects.RequestError(projects.HTTP_400, message)


def _check_token(token: Any) -> str:
    if not isinstance(token, str) or not token or len(token) > TOKEN_MAX:
        raise _bad(f"bot_token must be a non-empty string (<={TOKEN_MAX} chars)")
    return token


async def _bound(channel_type: str, current: Mapping[str, Any], config: Any) -> dict[str, Any]:
    """``config`` as stored: allowlist checked, and for the Lark types bound to its app. ``AllowlistError`` for a
    bad one."""
    stored = normalized_config(channel_type, config)
    try:
        if channel_type == "lark":
            from . import lark_settings
            return await lark_settings.bound_config(current, stored)
        if channel_type == "lark_bot":
            from . import lark_bot_settings
            return await lark_bot_settings.bound_config(current, stored)
    except ValueError as exc:
        raise AllowlistError(str(exc)) from None
    return stored


async def _clash(conn: asyncpg.Connection, channel_type: str, config: Mapping[str, Any], secret: str | None, *,
                 env_token: str = "", exclude: str | None = None) -> None:
    """Under the bots lock: ``RequestError`` 409 when a bot other than ``exclude`` is the same bot as the one
    described (``bots.bot_keys``: for Lark, the app each row acts as, across both Lark types). Never names a
    token."""
    await conn.execute(_LOCK)
    keys = bot_keys(channel_type, config, secret, env_token=env_token,
                    app_id=await lark_app_id(channel_type, config))
    if not keys:
        return
    for other_type in SAME_BOT_TYPES[channel_type]:
        for row in await db_channel_configs.bot_keys(other_type, conn=conn):
            if row["channel_id"] == exclude:
                continue
            stored, other = row["secret"], row["config"] or {}
            env = builtin_env_token(other) if (
                other_type == "telegram" and row["channel_id"] in BUILTIN_CHANNEL_IDS and not stored) else ""
            if keys & bot_keys(other_type, other, stored, env_token=env,
                               app_id=await lark_app_id(other_type, other)):
                name = await conn.fetchval(
                    "SELECT display_name FROM channel_configs WHERE channel_id = $1", row["channel_id"])
                raise projects.RequestError(
                    projects.HTTP_409,
                    f"this bot is already added as {name or row['channel_id']} ({row['channel_id']})")


async def add_bot(data: dict[str, Any]) -> dict[str, Any]:
    """Add a bot for a Manager: the new ``channel_configs`` row as ``channel_view`` shows it."""
    token = data.get("bot_token")
    if isinstance(token, str):
        log_redaction.add_secret(token)
    unknown = set(data) - _POST_KEYS
    if unknown:
        raise _bad(f"unknown keys: {', '.join(sorted(map(str, unknown)))}")
    channel_type = data.get("channel_type")
    if channel_type not in BOT_TYPES:
        raise _bad(f"channel_type must be one of {', '.join(BOT_TYPES)}")
    manager_id = data.get("manager_id")
    if not isinstance(manager_id, str) or not manager_id:
        raise _bad("manager_id must be the id of a Manager")
    enabled = data.get("enabled", False)
    if not isinstance(enabled, bool):
        raise _bad("enabled must be true or false")
    display_name = data.get("display_name")
    if display_name is not None:
        if not isinstance(display_name, str) or len(display_name.strip()) > DISPLAY_NAME_MAX:
            raise _bad(f"display_name must be a string of at most {DISPLAY_NAME_MAX} characters")
        display_name = display_name.strip() or None
    try:
        config = await _bound(channel_type, {}, data.get("config", {}))
    except AllowlistError as exc:
        raise _bad(str(exc)) from None

    if channel_type == "telegram":
        if not isinstance(token, str) or not token or len(token) > TOKEN_MAX:
            raise _bad("a new Telegram bot needs its token: paste it from BotFather")
        if config.get("bot_token_env"):
            raise _bad("a new bot's token is entered here, not read from the environment")
    elif channel_type == "lark":
        if not config.get("profile"):
            raise _bad("name the lark-cli profile of this bot: the active profile belongs to the built-in Lark "
                       "channel")
        if token is not None:
            raise _bad("a lark-cli bot has no token: its credentials are in its lark-cli profile")
    else:
        if not config.get("app_id"):
            raise _bad("a new Lark bot needs its app_id")
        if not isinstance(token, str) or not token or len(token) > TOKEN_MAX:
            raise _bad("a new Lark bot needs its app secret")

    try:
        from ...db.connection import get_pool
        async with get_pool().acquire() as conn, conn.transaction():
            await _clash(conn, channel_type, config, token)
            manager_name = await conn.fetchval(
                "SELECT name FROM managers WHERE id = $1 AND deleted_at IS NULL", manager_id)
            if manager_name is None:
                raise LookupError(f"Manager {manager_id} not found")
            row = await db_channel_configs.create_bot(
                channel_type, manager_id,
                display_name=display_name or f"{TYPE_WORDS[channel_type]} · {manager_name}"[:DISPLAY_NAME_MAX],
                enabled=enabled, config=config, secret=token, conn=conn)
    except LookupError as exc:
        raise projects.RequestError(projects.HTTP_404, str(exc)) from None
    except projects.RequestError:
        raise
    except Exception as exc:
        raise projects.RequestError(HTTP_500, str(exc)) from None
    return channel_view(row)


async def update_bot(channel_id: str, data: dict[str, Any]) -> dict[str, Any]:
    """Change a channel row (display name, enabled, config, token); a bot's Manager never changes."""
    # The token is a secret kept out of config: SECRET_MASK (or absent) keeps the stored token, "" / null removes
    # it, anything else stores it. It is never returned.
    token = data.pop("bot_token", SECRET_MASK)
    if isinstance(token, str):
        log_redaction.add_secret(token)
    if token != SECRET_MASK and token is not None and (not isinstance(token, str) or len(token) > TOKEN_MAX):
        raise _bad("bot_token must be a string (<=4096 chars) or null")
    removing = token is None or token == ""
    changing_token = token != SECRET_MASK and not removing

    try:
        current = await db_channel_configs.get_config(channel_id)
        if current is None:
            raise projects.RequestError(projects.HTTP_404, f"channel '{channel_id}' not found")
        kind = str(current.get("channel_type") or "")
        if "manager_id" in data:
            if data.pop("manager_id") != current.get("manager_id"):
                raise _bad("a bot's Manager cannot change: add a new bot for the other Manager and remove this one")
        before = current.get("config") or {}
        config = before
        if "config" in data:
            # Validated before anything is written: a rejected request must not change the stored token or config.
            config = await _bound(kind, before, data["config"])
            if kind == "telegram" and channel_id not in BUILTIN_CHANNEL_IDS and config.get("bot_token_env"):
                raise _bad("a bot's token is entered here, not read from the environment")
            data = {**data, "config": config}
        # What says which bot a row is: its token (removing the built-in Telegram row's falls back to the
        # environment's, which bot_token_env names), its lark-cli profile, its app_id.
        identity_changed = token != SECRET_MASK or any(
            config.get(key) != before.get(key) for key in ("profile", "app_id", "bot_token_env"))

        from ...db.connection import get_pool
        async with get_pool().acquire() as conn, conn.transaction():
            if identity_changed:
                await conn.execute(_LOCK)
                if removing:
                    secret = None
                elif changing_token:
                    secret = token
                else:
                    secret = await db_channel_configs.get_bot_token(channel_id, pool=conn)
                env = builtin_env_token(config) if (
                    kind == "telegram" and channel_id in BUILTIN_CHANNEL_IDS and not secret) else ""
                await _clash(conn, kind, config, secret, env_token=env, exclude=channel_id)
            if token != SECRET_MASK:
                await db_channel_configs.set_bot_token(channel_id, None if removing else token, pool=conn)
            updated = await db_channel_configs.set_config(channel_id, data, pool=conn)
    except AllowlistError as exc:
        raise _bad(str(exc)) from None
    except projects.RequestError:
        raise
    except Exception as exc:
        raise projects.RequestError(HTTP_500, str(exc)) from None
    if updated is None:
        raise projects.RequestError(projects.HTTP_404, f"channel '{channel_id}' not found")
    return channel_view(updated)


async def remove_bot(channel_id: str) -> dict[str, Any]:
    """Remove an added bot; the adapter keeps running until the restart."""
    if channel_id in BUILTIN_CHANNEL_IDS:
        raise _bad(f"the built-in {channel_id.removeprefix('channel-')} channel cannot be removed: turn it off "
                   "instead")
    try:
        result = await db_channel_configs.delete_bot(channel_id)
    except Exception as exc:
        raise projects.RequestError(HTTP_500, str(exc)) from None
    if result == "missing":
        raise projects.RequestError(projects.HTTP_404, f"channel '{channel_id}' not found")
    return {"deleted": channel_id, "restart": True}
