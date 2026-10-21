"""The chat's system settings (``system_settings``), edited in Settings → Chat.

``chat.stop_enabled``: whether a reply can be stopped (Esc and the Stop button in the dashboard,
``POST /api/chat/stop``, ``/stop`` on Telegram and Lark). Experimental, and off by default until the criteria of chat-stop-v2 section 10 are met.

Stop checks it on every request from synchronous code, so it is kept in memory: ``load`` reads it at startup and
``update`` refreshes it. Until loaded, it is off.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..db import system_settings as db_settings

STOP_ENABLED_KEY = "chat.stop_enabled"
DEFAULT_STOP_ENABLED = False
FIELDS = ("stop_enabled",)

STOP_DISABLED_REPLY = "停止功能暂未开放"

_stop_enabled = DEFAULT_STOP_ENABLED


def stop_enabled() -> bool:
    return _stop_enabled


def set_stop_enabled(value: bool) -> None:
    """Apply a value in memory only (``load`` and ``update`` store it; tests use this directly)."""
    global _stop_enabled
    _stop_enabled = value


async def load() -> bool:
    """Read the stored setting at startup; a missing or unreadable value is the default (off)."""
    stored = (await db_settings.get_settings([STOP_ENABLED_KEY])).get(STOP_ENABLED_KEY)
    set_stop_enabled(stored == "true")
    return _stop_enabled


def settings_view() -> dict[str, Any]:
    return {"stop_enabled": _stop_enabled}


def checked(body: Mapping[str, Any]) -> dict[str, bool | None]:
    """The settings in ``body`` (a boolean, or null for the default); ``ValueError`` for a bad one."""
    if unknown := [key for key in body if key not in FIELDS]:
        raise ValueError(f"unknown setting {unknown[0]!r}; the chat has {', '.join(FIELDS)}")
    if not body:
        raise ValueError(f"give at least one of {', '.join(FIELDS)} (null: the default)")
    value = body["stop_enabled"]
    if value is not None and not isinstance(value, bool):
        raise ValueError("stop_enabled must be true, false or null")
    return {"stop_enabled": value}


async def update(body: Mapping[str, Any]) -> dict[str, Any]:
    """Store the settings in ``body`` and apply them at once. A refused body changes nothing."""
    values = checked(body)
    value = values["stop_enabled"]
    await db_settings.put_settings({STOP_ENABLED_KEY: None if value is None else ("true" if value else "false")})
    await load()
    return settings_view()
