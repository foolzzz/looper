"""The browser/computer-use switches: system settings ``browser.*``, all off by default.

Only the lowercase string ``"true"`` is on; no row, or any other value, is off. Until ``chat_settings.load`` reads
the rows the switches are off. A failed read is logged, forces every switch off, and still propagates, so startup
does not continue on an unreadable settings table. The values are kept in memory. This module owns the settings
only; no tool, prompt or process consumes them yet.

``browser.retention_days`` is the audit/screenshot retention horizon; a missing or invalid row falls back to
:data:`DEFAULT_RETENTION_DAYS`.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger(__name__)

ENABLED_KEY = "browser.enabled"
USER_CHROME_KEY = "browser.user_chrome"
DESKTOP_KEY = "browser.desktop"
RETENTION_DAYS_KEY = "browser.retention_days"
DEFAULT_RETENTION_DAYS = 7

_FIELDS = ("enabled", "user_chrome", "desktop", "retention_days")
_BOOL_FIELDS = ("enabled", "user_chrome", "desktop")
_KEY_FOR_FIELD = {
    "enabled": ENABLED_KEY,
    "user_chrome": USER_CHROME_KEY,
    "desktop": DESKTOP_KEY,
    "retention_days": RETENTION_DAYS_KEY,
}

_enabled = False
_user_chrome = False
_desktop = False
_retention_days = DEFAULT_RETENTION_DAYS
_generation = 0  # counts the times the master switch was turned off: state queued before that is stale


def enabled() -> bool:
    return _enabled


def user_chrome_enabled() -> bool:
    return _user_chrome


def desktop_enabled() -> bool:
    return _desktop


def retention_days() -> int:
    return _retention_days


def generation() -> int:
    return _generation


def set_enabled(value: bool) -> None:
    """Apply the master switch in memory only (``apply_stored`` and ``update`` use it; tests set it directly)."""
    global _enabled, _generation
    if _enabled and not value:
        _generation += 1
    _enabled = value


def _stored_switch(value: str | None) -> bool:
    """Only the exact string ``"true"`` is on; a missing row or any other value is off."""
    return value == "true"


def _parse_retention_days(raw: str | None) -> int:
    """A positive base-10 integer. Surrounding whitespace is ignored; anything else is the default."""
    if raw is None:
        return DEFAULT_RETENTION_DAYS
    try:
        parsed = int(raw.strip(), 10)
    except (ValueError, TypeError):
        parsed = None
    if parsed is None or parsed <= 0:
        logger.warning(
            "Ignoring invalid %s %r; using the default %d",
            RETENTION_DAYS_KEY,
            raw,
            DEFAULT_RETENTION_DAYS,
        )
        return DEFAULT_RETENTION_DAYS
    return parsed


def apply_stored(stored: Mapping[str, str]) -> None:
    """Apply the rows read from ``system_settings``; absent keys and unknown values stay off/default."""
    global _user_chrome, _desktop, _retention_days
    set_enabled(_stored_switch(stored.get(ENABLED_KEY)))
    _user_chrome = _stored_switch(stored.get(USER_CHROME_KEY))
    _desktop = _stored_switch(stored.get(DESKTOP_KEY))
    _retention_days = _parse_retention_days(stored.get(RETENTION_DAYS_KEY))


def note_unreadable(exc: BaseException) -> None:
    """A failed read leaves every switch off. The caller still raises ``exc``."""
    logger.warning("Could not read browser settings; leaving them off", exc_info=exc)
    apply_stored({})


def view() -> dict[str, Any]:
    return {
        "enabled": _enabled,
        "user_chrome": _user_chrome,
        "desktop": _desktop,
        "retention_days": _retention_days,
    }


async def update(changes: Mapping[str, Any]) -> dict[str, Any]:
    """Persist the operator's changes and apply them in memory. Null deletes that key's row (its built-in default).

    The whole input is validated before anything is written, so a rejected change leaves both the rows and the
    in-memory state untouched.
    """
    if not changes:
        raise ValueError(
            f"give at least one of {', '.join(_FIELDS)} (null: the default)"
        )
    unknown = [key for key in changes if key not in _KEY_FOR_FIELD]
    if unknown:
        raise ValueError(
            f"unknown setting {unknown[0]!r}; the browser settings are {', '.join(_FIELDS)}"
        )
    for field in _BOOL_FIELDS:
        if (
            field in changes
            and changes[field] is not None
            and not isinstance(changes[field], bool)
        ):
            raise ValueError(f"{field} must be true, false or null")
    if "retention_days" in changes:
        retention = changes["retention_days"]
        if retention is not None and (type(retention) is not int or retention <= 0):
            raise ValueError("retention_days must be a positive integer or null")

    from ...db import system_settings as db_settings

    rows: dict[str, str | None] = {}
    for field, value in changes.items():
        if field == "retention_days":
            rows[_KEY_FOR_FIELD[field]] = None if value is None else str(value)
        else:
            rows[_KEY_FOR_FIELD[field]] = (
                None if value is None else ("true" if value else "false")
            )
    await db_settings.put_settings(rows)

    global _user_chrome, _desktop, _retention_days
    if "enabled" in changes:
        set_enabled(False if changes["enabled"] is None else changes["enabled"])
    if "user_chrome" in changes:
        _user_chrome = (
            False if changes["user_chrome"] is None else changes["user_chrome"]
        )
    if "desktop" in changes:
        _desktop = False if changes["desktop"] is None else changes["desktop"]
    if "retention_days" in changes:
        _retention_days = (
            DEFAULT_RETENTION_DAYS
            if changes["retention_days"] is None
            else changes["retention_days"]
        )
    return view()
