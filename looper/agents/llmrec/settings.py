"""Request retention settings, read afresh by each housekeeping pass."""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from typing import Any

from ...db import system_settings

logger = logging.getLogger(__name__)
RETENTION_KEY = "llmrec.retention_days"
MAX_TOTAL_KEY = "llmrec.max_total_mb"
DEFAULT_RETENTION_DAYS = 7
DEFAULT_MAX_TOTAL_MB = 2048
RETENTION_RANGE = (1, 365)
MAX_TOTAL_RANGE = (102.4, 102400)
FIELDS = ("retention_days", "max_total_mb")
_KEYS = dict(zip(FIELDS, (RETENTION_KEY, MAX_TOTAL_KEY)))


def checked(body: Mapping[str, Any]) -> dict[str, int | float | None]:
    values = {}
    for name, value in body.items():
        if name not in FIELDS:
            raise ValueError(f"unknown request recording setting {name!r}")
        low, high = RETENTION_RANGE if name == "retention_days" else MAX_TOTAL_RANGE
        valid_type = type(value) is int if name == "retention_days" else type(value) in (int, float)
        if value is not None and (not valid_type or not low <= value <= high or not math.isfinite(value)):
            unit = "a whole number of days" if name == "retention_days" else "MiB"
            raise ValueError(f"{name} must be {unit} from {low} to {high}")
        values[name] = value
    return values


async def settings_view(conn: Any = None) -> dict[str, Any]:
    stored = await system_settings.get_settings(list(_KEYS.values()), pool=conn)
    result = {}
    for name, built_in, span in (("retention_days", DEFAULT_RETENTION_DAYS, RETENTION_RANGE),
                                  ("max_total_mb", DEFAULT_MAX_TOTAL_MB, MAX_TOTAL_RANGE)):
        raw = stored.get(_KEYS[name])
        value = None
        if raw is not None:
            try:
                value = checked({name: int(raw) if name == "retention_days" else float(raw)})[name]
            except (ValueError, TypeError):
                logger.warning("invalid stored %s; using built-in value", _KEYS[name])
        result[name] = {"value": built_in if value is None else value,
                        "source": "built-in" if value is None else "setting", "built_in": built_in,
                        "minimum": span[0], "maximum": span[1]}
    return result


async def retention_days(conn: Any = None) -> int:
    return (await settings_view(conn))["retention_days"]["value"]


async def max_total_mb(conn: Any = None) -> int | float:
    return (await settings_view(conn))["max_total_mb"]["value"]
