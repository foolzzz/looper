"""The scheduler's two system settings (``system_settings``), edited in Settings → Scheduler.

``scheduler.min_interval_seconds``: how close two fires of a schedule may be (a minute with a gate command, else
this many seconds). ``scheduler.run_timeout_seconds``: the run timeout of a schedule that follows the default.
With no stored minimum interval, ``LOOPER_SCHEDULE_MIN_INTERVAL_SECONDS`` (or its ``LOOPX_`` name) still applies
for one more version, with a warning that names the variable; a stored value wins over it.

The minimum interval is read by synchronous validation, so it is kept in memory: ``load`` reads it at startup and
``update`` refreshes it. The run timeout is read when a run starts (``effective_run_timeout``).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from enum import Enum
from typing import Any

from .. import env
from ..db import system_settings as db_settings

logger = logging.getLogger(__name__)

MIN_INTERVAL_KEY = "scheduler.min_interval_seconds"
RUN_TIMEOUT_KEY = "scheduler.run_timeout_seconds"
MIN_INTERVAL_ENV = "LOOPER_SCHEDULE_MIN_INTERVAL_SECONDS"
DEFAULT_MIN_INTERVAL_SECONDS = 300
# A gated schedule may fire every minute: a run with nothing new ends at its gate, without a task.
MIN_INTERVAL_GATED_SECONDS = 60
DEFAULT_RUN_TIMEOUT_SECONDS: int | None = None  # no limit, as a schedule without a timeout always had
RUN_TIMEOUT_MAX_SECONDS = 30 * 86400
FIELDS = ("min_interval_seconds", "run_timeout_seconds")

_min_interval = DEFAULT_MIN_INTERVAL_SECONDS
_min_source: str | None = None  # "setting", "env"; None: the built-in
_warned_env = False


class Source(str, Enum):
    SETTING = "setting"
    ENV = "env"  # the deprecated environment variable
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


def min_interval_seconds(*, gated: bool) -> int:
    """How close two fires of a schedule may be: a minute with a gate command, else the minimum interval."""
    return min(MIN_INTERVAL_GATED_SECONDS, _min_interval) if gated else _min_interval


def _env_name() -> str:
    legacy = env.counterpart(MIN_INTERVAL_ENV)
    return MIN_INTERVAL_ENV if MIN_INTERVAL_ENV in os.environ or not legacy else legacy


async def load() -> int:
    """Read the minimum interval at startup: the stored setting, else the deprecated variable (warned about once,
    without its value), else the built-in. Raises ``ValueError`` for a variable that is not a whole number of
    seconds from 1, as the server refuses to start with it."""
    global _min_interval, _min_source, _warned_env
    stored = (await db_settings.get_settings([MIN_INTERVAL_KEY])).get(MIN_INTERVAL_KEY)
    if stored is not None:
        try:
            _min_interval, _min_source = _seconds(int(stored), MIN_INTERVAL_KEY), Source.SETTING.value
            return _min_interval
        except ValueError:
            logger.warning("the stored %s is not a whole number of seconds from 1; ignoring it", MIN_INTERVAL_KEY)
    raw = env.get(MIN_INTERVAL_ENV)
    if raw is None or raw.strip() == "":
        _min_interval, _min_source = DEFAULT_MIN_INTERVAL_SECONDS, None
        return _min_interval
    try:
        value = _seconds(int(raw), MIN_INTERVAL_ENV)
    except ValueError:
        raise ValueError(f"{MIN_INTERVAL_ENV} must be a whole number of seconds from 1; got {raw!r}") from None
    if not _warned_env:
        _warned_env = True
        logger.warning("%s is deprecated and will be ignored in the next version: set the minimum interval in "
                       "Settings → Scheduler instead", _env_name())
    _min_interval, _min_source = value, Source.ENV.value
    return _min_interval


async def _stored_run_timeout() -> int | None:
    stored = (await db_settings.get_settings([RUN_TIMEOUT_KEY])).get(RUN_TIMEOUT_KEY)
    if stored is None:
        return None
    try:
        return _seconds(int(stored), RUN_TIMEOUT_KEY, high=RUN_TIMEOUT_MAX_SECONDS)
    except ValueError:
        logger.warning("the stored %s is not a whole number of seconds from 1 to %d; using no limit",
                       RUN_TIMEOUT_KEY, RUN_TIMEOUT_MAX_SECONDS)
        return None


async def default_run_timeout() -> int | None:
    """The run timeout of a schedule that follows the default (None: no limit)."""
    return await _stored_run_timeout()


async def effective_run_timeout(mode: str, seconds: int | None) -> int | None:
    """A schedule's timeout for a run that starts now (None: no limit)."""
    if mode == RunTimeoutMode.INHERIT.value:
        return await default_run_timeout()
    return seconds if mode == RunTimeoutMode.SECONDS.value else None


async def settings_view() -> dict[str, Any]:
    """Each setting's effective value and where it comes from (``setting``, ``env``, ``built-in``)."""
    run_timeout = await _stored_run_timeout()
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
        values[name] = None if raw is None else _seconds(raw, name, high=high)
    return values


_KEYS = {"min_interval_seconds": MIN_INTERVAL_KEY, "run_timeout_seconds": RUN_TIMEOUT_KEY}


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
