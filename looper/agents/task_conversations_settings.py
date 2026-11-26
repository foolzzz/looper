"""Operator-only work-mode setting, separate from agent-accessible chat settings."""

from ..db import system_settings as db_settings

KEY = "tasks.conversations.enabled"
_enabled = False


def enabled() -> bool:
    return _enabled


def set_enabled(value: bool) -> None:
    """Apply a value in memory; tests may set it without persisting a setting."""
    global _enabled
    _enabled = value


async def load() -> None:
    stored = await db_settings.get_settings([KEY])
    set_enabled(stored.get(KEY) == "true")


def view() -> dict[str, bool]:
    return {"enabled": enabled()}


def _stored(value: bool | None) -> str | None:
    if value is not None and not isinstance(value, bool):
        raise ValueError("enabled must be true, false or null")
    return None if value is None else ("true" if value else "false")


async def update(value: bool | None) -> dict[str, bool]:
    """Persist first: a failed write leaves the effective setting unchanged."""
    await db_settings.put_settings({KEY: _stored(value)})
    set_enabled(False if value is None else value)
    return view()
