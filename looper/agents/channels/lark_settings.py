"""The Lark (lark-cli) channel on Settings → Channels: the bot it acts as, names for open_ids, a people search to
build the sender allowlist with, the senders it refused, and the app the allowlist is bound to."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from ...db import channel_configs as db_channel_configs
from . import lark_cli
from .allowlist import SENDER_ID_FORMATS, sender_ids

logger = logging.getLogger(__name__)

CHANNEL_ID = "channel-lark"
QUERY_MAX_CHARS = 50
_FIELD = SENDER_ID_FORMATS["lark"].field


def _ids(config: Mapping[str, Any]) -> list[str]:
    try:
        return list(sender_ids("lark", config.get(_FIELD)))
    except ValueError:
        return []


async def status() -> dict[str, Any]:
    """The saved Lark channel as lark-cli sees it: ``bot`` (``name``, ``app_id``, ``open_id``, ``profile``; null
    when lark-cli cannot act as it), ``problem`` (why the channel would not start, from lark-cli or the allowlist's
    app; null otherwise), ``allowlist_app_id``, ``names`` (open_id -> name, for the allowlist and the refused
    senders) and ``rejected`` (the latest refused senders not on the allowlist: ``open_id``, ``name``,
    ``snippet``, ``count``, ``last_at``)."""
    row = await db_channel_configs.get_config(CHANNEL_ID)
    config = dict((row or {}).get("config") or {})
    problem, bot = await lark_cli.check(config)
    allowed = _ids(config)
    rejected = [r for r in await db_channel_configs.rejected_senders(CHANNEL_ID) if r["sender_id"] not in allowed]
    names: dict[str, str] = {}
    if bot is not None:
        try:
            names = await lark_cli.names([*allowed, *(r["sender_id"] for r in rejected)], bot.profile or None)
        except lark_cli.LarkCliError as exc:
            logger.info("channel-lark: could not look up the senders' names: %s", exc)
    return {
        "bot": None if bot is None else {"name": bot.bot_name, "app_id": bot.app_id, "open_id": bot.bot_open_id,
                                         "profile": bot.profile},
        "problem": problem,
        "allowlist_app_id": config.get("allowlist_app_id"),
        "names": names,
        "rejected": [{"open_id": r["sender_id"], "name": names.get(r["sender_id"]), "snippet": r["snippet"],
                      "count": r["count"], "last_at": r["last_at"]} for r in rejected],
    }


async def people(query: str) -> dict[str, Any]:
    """The people ``query`` (a name or an email) finds, to add to the allowlist: their open_ids for the saved
    profile's app. Raises ``ValueError`` for a bad query and ``LarkCliError`` when lark-cli cannot search."""
    text = query.strip()
    if not text or len(text) > QUERY_MAX_CHARS:
        raise ValueError(f"search for a name or an email of 1 to {QUERY_MAX_CHARS} characters")
    row = await db_channel_configs.get_config(CHANNEL_ID)
    profile = ((row or {}).get("config") or {}).get("profile") or None
    return {"people": await lark_cli.search_people(text, profile)}


async def bound_config(current: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
    """The Lark channel's new configuration as stored: checked (``lark_cli.lark_config``) and bound to an app.
    The server owns ``allowlist_app_id``. An unchanged allowlist keeps the app it was made for, even under another
    profile, so a profile of another app does not start until the allowlist is rebuilt; a changed one (or one not
    bound yet) takes the saved profile's app (unknown when lark-cli cannot say); an empty one has none."""
    stored = lark_cli.lark_config(config)
    before = dict(current or {})
    allowed = _ids(stored)
    if not allowed:
        stored.pop("allowlist_app_id", None)
        return stored
    if allowed == _ids(before) and before.get("allowlist_app_id"):
        stored["allowlist_app_id"] = before["allowlist_app_id"]
        return stored
    try:
        stored["allowlist_app_id"] = (await lark_cli.identity(stored["profile"] or None, fresh=True)).app_id
    except lark_cli.LarkCliError as exc:
        logger.info("channel-lark: the allowlist is saved without its app, which lark-cli could not tell: %s", exc)
        stored.pop("allowlist_app_id", None)
    return stored
