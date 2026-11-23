"""Operator-only channel outage threshold, cached for synchronous health checks."""

from __future__ import annotations

import logging

from ...db import system_settings as db_settings

logger = logging.getLogger(__name__)
KEY = "channels.down_alert_minutes"
DEFAULT_MINUTES = 10
_minutes = DEFAULT_MINUTES


def checked(value: int | None) -> int:
    if value is None:
        return DEFAULT_MINUTES
    if type(value) is not int or value < 1:
        raise ValueError("down_alert_minutes must be a positive whole number of minutes or null")
    return value


def minutes() -> int:
    return _minutes


def view() -> dict[str, int]:
    return {"down_alert_minutes": minutes()}


async def load() -> int:
    """Read at startup; invalid stored values retain the built-in ten-minute threshold."""
    global _minutes
    stored = (await db_settings.get_settings([KEY])).get(KEY)
    try:
        _minutes = DEFAULT_MINUTES if stored is None else checked(int(stored))
    except (ValueError, TypeError):
        logger.warning("the stored %s is invalid; using %d minutes", KEY, DEFAULT_MINUTES)
        _minutes = DEFAULT_MINUTES
    return _minutes


async def update(value: int | None) -> dict[str, int]:
    """Persist before changing memory, so a refused or failed write leaves the threshold intact."""
    global _minutes
    effective = checked(value)
    await db_settings.put_settings({KEY: None if value is None else str(effective)})
    _minutes = effective
    return view()
