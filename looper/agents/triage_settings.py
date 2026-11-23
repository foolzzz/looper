"""Operator-only event triage switch. Execution is delivered separately from this foundation."""

from ..db import system_settings

KEY = "subscriptions.triage.enabled"
TURNED_OFF = "subscriptions.triage.enabled is off"
NOT_IMPLEMENTED = "triage execution is not available yet"


async def enabled(db=None) -> bool:
    """An absent or invalid stored switch is off; no runtime cache or startup writes."""
    return (await system_settings.get_settings([KEY], pool=db)).get(KEY) == "true"


async def view() -> dict[str, bool]:
    return {"enabled": await enabled()}


async def update(value: bool | None) -> dict[str, bool]:
    if value is not None and not isinstance(value, bool):
        raise ValueError("enabled must be true, false or null")
    await system_settings.put_settings({KEY: None if value is None else str(value).lower()})
    return await view()
