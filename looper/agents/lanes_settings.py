"""Operator-only Manager lanes setting, kept separate from agent-accessible chat settings."""

from typing import Any

from ..db import system_settings as db_settings

KEY = "manager.lanes.enabled"
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


async def portable(conn: Any) -> dict[str, bool]:
    """Only an explicit, valid setting travels; absence keeps the destination's setting."""
    if not await conn.fetchval("SELECT to_regclass('system_settings') IS NOT NULL"):
        return {}
    stored = await db_settings.get_settings([KEY], pool=conn)
    value = stored.get(KEY)
    return {"enabled": value == "true"} if value in ("true", "false") else {}


async def import_into(importer: Any, entry: Any) -> None:
    """Validate before writing, within the configuration importer's transaction."""
    from .config_transfer import Action

    try:
        if not isinstance(entry, dict) or set(entry) != {"enabled"}:
            raise ValueError("manager_lanes must contain only enabled")
        value = _stored(entry["enabled"])
    except ValueError as exc:
        importer.record("settings.manager_lanes", "enabled", Action.SKIPPED, reason=str(exc))
        return
    current = await db_settings.get_settings([KEY], pool=importer.conn)
    action = importer.outcome("settings.manager_lanes", "enabled", current.get(KEY),
                              ("enabled",) if current.get(KEY) != value else ())
    if importer.apply and action in (Action.ADDED, Action.CHANGED):
        if value is None:
            await importer.conn.execute("DELETE FROM system_settings WHERE key = $1", KEY)
        else:
            await importer.conn.execute(
                "INSERT INTO system_settings (key, value) VALUES ($1, $2) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()", KEY, value)
