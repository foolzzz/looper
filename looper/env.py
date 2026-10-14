"""Looper's environment variables: ``LOOPER_*``, with the ``LOOPX_*`` names from before the rename as a fallback.

The product was called LoopX and its variables ``LOOPX_*``. For one release,
``get("LOOPER_X")`` reads ``LOOPX_X`` when ``LOOPER_X`` is not set, and logs a
deprecation warning the first time it does so for that name. A ``LOOPX_*``
name that a stored setting still names (a Telegram channel's
``bot_token_env``) reads ``LOOPER_*`` the same way, so renaming the variable
does not break the setting.
"""

from __future__ import annotations

import logging
import os

PREFIX = "LOOPER_"
LEGACY_PREFIX = "LOOPX_"  # before the rename; read as a fallback for one release

logger = logging.getLogger(__name__)
_warned: set[str] = set()


def counterpart(name: str) -> str | None:
    """The same variable under the other prefix (``LOOPER_X`` and ``LOOPX_X``); None for any other name."""
    if name.startswith(PREFIX):
        return LEGACY_PREFIX + name[len(PREFIX):]
    if name.startswith(LEGACY_PREFIX):
        return PREFIX + name[len(LEGACY_PREFIX):]
    return None


def get(name: str, default: str | None = None) -> str | None:
    """``os.environ[name]``, else its counterpart's value (a deprecated ``LOOPX_*`` one warned about once), else
    ``default``. A variable set to an empty string counts as set."""
    value = os.environ.get(name)
    if value is not None:
        return value
    other = counterpart(name)
    value = os.environ.get(other) if other else None
    if value is None:
        return default
    if other.startswith(LEGACY_PREFIX) and other not in _warned:
        _warned.add(other)
        logger.warning("%s is deprecated: rename it to %s (the old name is read until the next release)", other, name)
    return value
