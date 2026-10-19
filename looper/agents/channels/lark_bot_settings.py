"""The bot-token Lark channel's credentials, sender names and app-bound allowlist in Settings."""

from __future__ import annotations

import hashlib
import logging
import re
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from ...db import channel_configs as db_channel_configs
from .allowlist import sender_ids

if TYPE_CHECKING:
    from .lark_api import BotInfo

logger = logging.getLogger(__name__)

CHANNEL_ID = "channel-lark_bot"
QUERY_MAX_CHARS = 50
IDENTITY_TTL_SECONDS = 30.0
_APP_ID = re.compile(r"[A-Za-z0-9_]{4,64}")
_bots: dict[tuple[str, str, str], tuple[float, BotInfo]] = {}


def stored_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """The saved configuration, with its app id and platform domain checked and normalized."""
    from .lark_api import BASE_URLS

    stored = dict(config)
    app_id = stored.get("app_id", "")
    if not isinstance(app_id, str):
        raise ValueError("app_id must be a string")
    app_id = app_id.strip()
    if app_id and not _APP_ID.fullmatch(app_id):
        raise ValueError("app_id must be 4 to 64 letters, digits or underscores")
    domain = stored.get("domain", "feishu")
    if not isinstance(domain, str) or domain not in BASE_URLS:
        raise ValueError(f"domain must be one of {', '.join(BASE_URLS)}")
    stored.update(app_id=app_id, domain=domain)
    return stored


def _ids(config: Mapping[str, Any]) -> list[str]:
    try:
        return list(sender_ids("lark_bot", config.get("allowed_open_ids")))
    except ValueError:
        return []


def forget() -> None:
    """Forget cached bot identities after credentials change."""
    _bots.clear()


async def check(config: Mapping[str, Any], app_secret: str | None, *, fresh: bool = False,
                ) -> tuple[str | None, BotInfo | None]:
    """Why the channel cannot run, and the bot its credentials identify when known."""
    app_id = config.get("app_id", "")
    if not app_id:
        return "no app_id is set", None
    if not app_secret:
        return "no app secret is set", None

    from .lark_api import BASE_URLS, LarkApi, LarkApiError

    domain = config.get("domain", "feishu")
    key = (app_id, domain, hashlib.sha256(app_secret.encode()).hexdigest())
    now = time.monotonic()
    cached = _bots.get(key)
    if not fresh and cached is not None and now - cached[0] < IDENTITY_TTL_SECONDS:
        bot = cached[1]
    else:
        api = LarkApi(app_id, app_secret, base_url=BASE_URLS[domain])
        try:
            bot = await api.bot_info()
        except LarkApiError as exc:
            return f"the platform rejected the app credentials: {exc}", None
        finally:
            await api.aclose()
        _bots[key] = (time.monotonic(), bot)
    built_for = config.get("allowlist_app_id")
    if built_for and built_for != app_id:
        return (f"the sender allowlist was made for app {built_for}; app_id is app {app_id}; open_ids differ per "
                "app, so rebuild the allowlist for this app"), bot
    return None, bot


async def status() -> dict[str, Any]:
    """The saved bot, its startup problem, allowlist names and latest refused senders."""
    from .lark_api import BASE_URLS, LarkApi, LarkApiError

    row = await db_channel_configs.get_config(CHANNEL_ID)
    config = dict((row or {}).get("config") or {})
    secret = await db_channel_configs.get_bot_token(CHANNEL_ID)
    problem, bot = await check(config, secret)
    allowed = _ids(config)
    rejected = [r for r in await db_channel_configs.rejected_senders(CHANNEL_ID) if r["sender_id"] not in allowed]
    names: dict[str, str] = {}
    if bot is not None:
        api = LarkApi(config["app_id"], secret, base_url=BASE_URLS[config.get("domain", "feishu")])
        try:
            names = await api.names([*allowed, *(r["sender_id"] for r in rejected)])
        except LarkApiError as exc:
            logger.info("channel-lark_bot: could not look up the senders' names: %s", exc)
        finally:
            await api.aclose()
    return {
        "bot": None if bot is None else {"name": bot.app_name, "app_id": config["app_id"],
                                         "open_id": bot.open_id, "profile": ""},
        "problem": problem,
        "allowlist_app_id": config.get("allowlist_app_id"),
        "names": names,
        "rejected": [{"open_id": r["sender_id"], "name": names.get(r["sender_id"]), "count": r["count"],
                      "last_at": r["last_at"], "chat_id": r.get("chat_id")} for r in rejected],
    }


async def people(query: str) -> dict[str, Any]:
    """People matching a name or email, with whether the platform returned the complete result."""
    text = query.strip()
    if not text or len(text) > QUERY_MAX_CHARS:
        raise ValueError(f"search for a name or an email of 1 to {QUERY_MAX_CHARS} characters")

    from .lark_api import BASE_URLS, LarkApi, LarkApiError

    row = await db_channel_configs.get_config(CHANNEL_ID)
    config = dict((row or {}).get("config") or {})
    secret = await db_channel_configs.get_bot_token(CHANNEL_ID)
    if not config.get("app_id"):
        raise LarkApiError("no app_id is set")
    if not secret:
        raise LarkApiError("no app secret is set")
    api = LarkApi(config["app_id"], secret, base_url=BASE_URLS[config.get("domain", "feishu")])
    try:
        found, complete = await api.search_people(text)
    finally:
        await api.aclose()
    return {"people": found, "complete": complete}


async def bound_config(current: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
    """Bind a changed allowlist to its app; an unchanged one keeps its original app binding."""
    stored = stored_config(config)
    allowed = _ids(stored)
    if not allowed:
        stored.pop("allowlist_app_id", None)
    elif allowed == _ids(current) and current.get("allowlist_app_id"):
        stored["allowlist_app_id"] = current["allowlist_app_id"]
    else:
        stored["allowlist_app_id"] = stored["app_id"]
    return stored
