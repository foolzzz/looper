"""The action receipts switch: system setting ``manager.action_receipts.enabled`` (assistant phase 3 RFC, 6.7).

Only ``true`` is on; no row, or any other value, is off. Until ``chat_settings.load`` reads the row the switch is off,
and a failed read stays off. Read in memory, because ``ManagerOps.begin_turn`` is synchronous.
"""

from __future__ import annotations

ENABLED_KEY = "manager.action_receipts.enabled"

_enabled = False
_generation = 0  # counts the times the switch was turned off: what was queued before is stale


def enabled() -> bool:
    return _enabled


def generation() -> int:
    return _generation


def set_enabled(value: bool) -> None:
    """Apply a value in memory only (``apply_stored`` and ``update`` use it; tests set it directly)."""
    global _enabled, _generation
    if _enabled and not value:
        _generation += 1
    _enabled = value


def apply_stored(value: str | None) -> None:
    set_enabled(value == "true")


def view() -> dict[str, bool]:
    return {"enabled": _enabled}


async def update(value: bool | None) -> dict[str, bool]:
    """Persist the operator's choice. Null deletes the row and restores off."""
    if value is not None and not isinstance(value, bool):
        raise ValueError("enabled must be true, false or null")
    from ..db import system_settings as db_settings

    await db_settings.put_settings({ENABLED_KEY: None if value is None else ("true" if value else "false")})
    set_enabled(False if value is None else value)
    return view()
