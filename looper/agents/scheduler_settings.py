"""The scheduler's system settings (``system_settings``).

``scheduler.min_interval_seconds``: how close two fires of a schedule may be (a minute with a gate command, else
this many seconds). ``scheduler.run_timeout_seconds``: the run timeout of a schedule that follows the default.
With no stored minimum interval, the built-in default is 300 seconds.

The minimum interval is read by synchronous validation, so it is kept in memory: ``load`` reads it at startup and
``update`` refreshes it. The run timeout is read when a run starts (``effective_run_timeout``).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from enum import Enum
from typing import Any

from ..db import system_settings as db_settings

logger = logging.getLogger(__name__)

MIN_INTERVAL_KEY = "scheduler.min_interval_seconds"
RUN_TIMEOUT_KEY = "scheduler.run_timeout_seconds"
RUN_RETENTION_KEY = "scheduler.run_retention_days"
DEFAULT_RUN_RETENTION_DAYS = 30
RUN_RETENTION_MAX_DAYS = 3650
DEFAULT_MIN_INTERVAL_SECONDS = 300
# A gated schedule may fire every minute: a run with nothing new ends at its gate, without a task.
MIN_INTERVAL_GATED_SECONDS = 60
DEFAULT_RUN_TIMEOUT_SECONDS: int | None = None  # no limit, as a schedule without a timeout always had
RUN_TIMEOUT_MAX_SECONDS = 30 * 86400
FIELDS = ("min_interval_seconds", "run_timeout_seconds", "run_retention_days")

_min_interval = DEFAULT_MIN_INTERVAL_SECONDS
_min_source: str | None = None  # "setting"; None: the built-in


class Source(str, Enum):
    SETTING = "setting"
    DEFAULT = "built-in"


class RunTimeoutMode(str, Enum):
    """What a schedule's run timeout is: the system default, its own number, or no limit."""

    INHERIT = "inherit"
    SECONDS = "seconds"
    NONE = "none"


def _seconds(raw: Any, name: str, *, high: int | None = None) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1 or (high is not None and raw > high):
        span = f"from 1 to {high}" if high else "from 1"
        raise ValueError(f"{name} must be a whole number of seconds {span}")
    return raw


def min_interval_seconds(*, gated: bool, base: int | None = None) -> int:
    """How close two fires of a schedule may be: a minute with a gate command, else the minimum interval (``base``
    stands in for the one in effect, to check against a value about to be saved)."""
    least = _min_interval if base is None else base
    return min(MIN_INTERVAL_GATED_SECONDS, least) if gated else least


async def load() -> int:
    """Read the minimum interval at startup: the stored setting, else the built-in."""
    global _min_interval, _min_source
    stored = (await db_settings.get_settings([MIN_INTERVAL_KEY])).get(MIN_INTERVAL_KEY)
    if stored is not None:
        try:
            _min_interval, _min_source = _seconds(int(stored), MIN_INTERVAL_KEY), Source.SETTING.value
            return _min_interval
        except ValueError:
            logger.warning("the stored %s is not a whole number of seconds from 1; ignoring it", MIN_INTERVAL_KEY)
    _min_interval, _min_source = DEFAULT_MIN_INTERVAL_SECONDS, None
    return _min_interval


async def _stored_run_timeout(conn: Any = None) -> int | None:
    stored = (await db_settings.get_settings([RUN_TIMEOUT_KEY], pool=conn)).get(RUN_TIMEOUT_KEY)
    if stored is None:
        return None
    try:
        return _seconds(int(stored), RUN_TIMEOUT_KEY, high=RUN_TIMEOUT_MAX_SECONDS)
    except ValueError:
        logger.warning("the stored %s is not a whole number of seconds from 1 to %d; using no limit",
                       RUN_TIMEOUT_KEY, RUN_TIMEOUT_MAX_SECONDS)
        return None


async def default_run_timeout(conn: Any = None) -> int | None:
    """The run timeout of a schedule that follows the default (None: no limit). Inside a transaction, read on its
    ``conn``: a second connection from the pool could wait forever for one."""
    return await _stored_run_timeout(conn)


def _days(raw: Any) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or not 1 <= raw <= RUN_RETENTION_MAX_DAYS:
        raise ValueError(f"run_retention_days must be a whole number of days from 1 to {RUN_RETENTION_MAX_DAYS}")
    return raw


async def _stored_run_retention(conn: Any = None) -> int | None:
    stored = (await db_settings.get_settings([RUN_RETENTION_KEY], pool=conn)).get(RUN_RETENTION_KEY)
    if stored is None:
        return None
    try:
        return _days(int(stored))
    except (ValueError, TypeError):
        logger.warning("the stored %s is not a whole number of days from 1 to %d; using %d",
                       RUN_RETENTION_KEY, RUN_RETENTION_MAX_DAYS, DEFAULT_RUN_RETENTION_DAYS)
        return None


async def run_retention_days(conn: Any = None) -> int:
    """Read retention on the caller's transaction connection when one is supplied."""
    stored = await _stored_run_retention(conn)
    return DEFAULT_RUN_RETENTION_DAYS if stored is None else stored


async def effective_run_timeout(mode: str, seconds: int | None, conn: Any = None) -> int | None:
    """A schedule's timeout for a run that starts now (None: no limit); ``conn`` as ``default_run_timeout``."""
    if mode == RunTimeoutMode.INHERIT.value:
        return await default_run_timeout(conn)
    return seconds if mode == RunTimeoutMode.SECONDS.value else None


async def settings_view() -> dict[str, Any]:
    """Each setting's effective value and where it comes from (``setting``, ``built-in``)."""
    run_timeout = await _stored_run_timeout()
    retention = await _stored_run_retention()
    return {
        "min_interval_seconds": {
            "value": _min_interval, "source": _min_source or Source.DEFAULT.value,
            "built_in": DEFAULT_MIN_INTERVAL_SECONDS, "minimum": 1,
            "gated_value": min_interval_seconds(gated=True),
        },
        "run_timeout_seconds": {
            "value": run_timeout,
            "source": Source.SETTING.value if run_timeout else Source.DEFAULT.value,
            "built_in": DEFAULT_RUN_TIMEOUT_SECONDS, "minimum": 1, "maximum": RUN_TIMEOUT_MAX_SECONDS,
        },
        "run_retention_days": {
            "value": DEFAULT_RUN_RETENTION_DAYS if retention is None else retention,
            "source": Source.DEFAULT.value if retention is None else Source.SETTING.value,
            "built_in": DEFAULT_RUN_RETENTION_DAYS, "minimum": 1, "maximum": RUN_RETENTION_MAX_DAYS,
        },
    }


def checked(body: Mapping[str, Any]) -> dict[str, int | None]:
    """The settings in ``body`` (a number, or null to go back to the built-in); ``ValueError`` for a bad one."""
    if unknown := [key for key in body if key not in FIELDS]:
        raise ValueError(f"unknown setting {unknown[0]!r}; the scheduler has {', '.join(FIELDS)}")
    if not body:
        raise ValueError(f"give at least one of {', '.join(FIELDS)} (null: the built-in)")
    values: dict[str, int | None] = {}
    for name, raw in body.items():
        high = RUN_TIMEOUT_MAX_SECONDS if name == "run_timeout_seconds" else None
        values[name] = None if raw is None else (_days(raw) if name == "run_retention_days"
                                                else _seconds(raw, name, high=high))
    return values


_KEYS = {"min_interval_seconds": MIN_INTERVAL_KEY, "run_timeout_seconds": RUN_TIMEOUT_KEY,
         "run_retention_days": RUN_RETENTION_KEY}


async def store(name: str, value: int, *, conn: Any) -> None:
    """Write one checked setting on ``conn`` (a configuration import's transaction); ``load`` applies it."""
    await conn.execute(
        "INSERT INTO system_settings (key, value) VALUES ($1, $2) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()", _KEYS[name], str(value))


async def update(body: Mapping[str, Any]) -> dict[str, Any]:
    """Store the settings in ``body`` and apply them: a minimum interval to every later save, a run timeout to the
    runs that start after. A refused body changes nothing."""
    values = checked(body)
    await db_settings.put_settings({_KEYS[name]: None if value is None else str(value)
                                    for name, value in values.items()})
    if "min_interval_seconds" in values:
        await load()
    return await settings_view()
