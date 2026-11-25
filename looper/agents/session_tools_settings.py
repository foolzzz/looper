"""Operator-only opt-in for the tools of routed chat sessions."""

from ..db import system_settings as db_settings

KEY = "manager.session_tools.enabled"
_enabled = False


def enabled() -> bool:
    return _enabled


def set_enabled(value: bool) -> None:
    global _enabled
    _enabled = value


async def load() -> None:
    stored = await db_settings.get_settings([KEY])
    set_enabled(stored.get(KEY) == "true")


def view() -> dict[str, bool]:
    return {"enabled": enabled()}


async def update(value: bool | None) -> dict[str, bool]:
    """Persist first; failure leaves the effective setting unchanged."""
    if value is not None and not isinstance(value, bool):
        raise ValueError("enabled must be true, false or null")
    await db_settings.put_settings({KEY: None if value is None else str(value).lower()})
    set_enabled(False if value is None else value)
    return view()
