"""Operator-owned network recovery policy; no row means recovery is disabled."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..db import system_settings

logger = logging.getLogger(__name__)
ENABLED_KEY = "engine.network_pause.enabled"
MAX_PAUSE_KEY = "engine.network_pause.max_pause_seconds"
DEFAULT_MAX_PAUSE_SECONDS = 7200
MAX_PAUSE_SECONDS = 86400


@dataclass(frozen=True)
class NetworkPauseSettings:
    enabled: bool = False
    max_pause_seconds: int = DEFAULT_MAX_PAUSE_SECONDS


async def load(*, pool=None, strict=False) -> NetworkPauseSettings:
    try:
        stored = await system_settings.get_settings([ENABLED_KEY, MAX_PAUSE_KEY], pool=pool)
    except Exception:
        if strict:
            raise
        logger.warning("Could not read network pause settings; recovery is disabled", exc_info=True)
        return NetworkPauseSettings()
    maximum = DEFAULT_MAX_PAUSE_SECONDS
    if MAX_PAUSE_KEY in stored:
        try:
            maximum = int(stored[MAX_PAUSE_KEY])
            if not 1 <= maximum <= MAX_PAUSE_SECONDS:
                raise ValueError
        except ValueError:
            logger.warning("Invalid %s; using the built-in duration", MAX_PAUSE_KEY)
            maximum = DEFAULT_MAX_PAUSE_SECONDS
    return NetworkPauseSettings(stored.get(ENABLED_KEY) == "true", maximum)


def checked(values: Mapping[str, Any]) -> dict[str, str | None]:
    if not values or set(values) - {"enabled", "max_pause_seconds"}:
        raise ValueError("provide enabled or max_pause_seconds; unknown network pause setting")
    result = {}
    if "enabled" in values:
        value = values["enabled"]
        if value is not None and type(value) is not bool:
            raise ValueError("enabled must be true, false or null")
        result[ENABLED_KEY] = None if value is None else str(value).lower()
    if "max_pause_seconds" in values:
        value = values["max_pause_seconds"]
        if value is not None and (type(value) is not int or not 1 <= value <= MAX_PAUSE_SECONDS):
            raise ValueError(f"max_pause_seconds must be an integer from 1 to {MAX_PAUSE_SECONDS}, or null")
        result[MAX_PAUSE_KEY] = None if value is None else str(value)
    return result


async def update(values: Mapping[str, Any], *, pool=None) -> NetworkPauseSettings:
    await system_settings.put_settings(checked(values), pool=pool)
    return await load(pool=pool)
