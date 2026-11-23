"""Operator-only event triage settings: the switch, which gates subscription creation and execution, and how many
triage turns the Manager takes a day."""

from ..db import system_settings

KEY = "subscriptions.triage.enabled"
MAX_TURNS_KEY = "subscriptions.triage.max_turns_per_day"
TURNED_OFF = "subscriptions.triage.enabled is off"
DEFAULT_MAX_TURNS = 12
MIN_MAX_TURNS, MAX_MAX_TURNS = 1, 100
UNSET = object()


async def enabled(db=None) -> bool:
    """An absent or invalid stored switch is off; no runtime cache or startup writes."""
    return (await system_settings.get_settings([KEY], pool=db)).get(KEY) == "true"


async def max_turns_per_day(db=None) -> int:
    """The triage turns the Manager takes on one local day; the 13th (by default) goes to the brief. An absent or
    invalid stored value is the default."""
    stored = (await system_settings.get_settings([MAX_TURNS_KEY], pool=db)).get(MAX_TURNS_KEY)
    try:
        value = int(stored) if stored is not None and stored.isdecimal() else None
    except ValueError:
        value = None
    return value if value is not None and MIN_MAX_TURNS <= value <= MAX_MAX_TURNS else DEFAULT_MAX_TURNS


async def view() -> dict[str, bool | int]:
    return {"enabled": await enabled(), "max_turns_per_day": await max_turns_per_day()}


async def update(value: bool | None | object = UNSET, *, max_turns: int | None | object = UNSET) -> dict[str, bool | int]:
    """Store the settings given (null restores a default); the others stay as they are."""
    if value is not UNSET and value is not None and not isinstance(value, bool):
        raise ValueError("enabled must be true, false or null")
    if max_turns is not UNSET and max_turns is not None and (
            isinstance(max_turns, bool) or not isinstance(max_turns, int)
            or not MIN_MAX_TURNS <= max_turns <= MAX_MAX_TURNS):
        raise ValueError(f"max_turns_per_day must be a whole number from {MIN_MAX_TURNS} to {MAX_MAX_TURNS}, or null")
    stored: dict[str, str | None] = {}
    if value is not UNSET:
        stored[KEY] = None if value is None else str(value).lower()
    if max_turns is not UNSET:
        stored[MAX_TURNS_KEY] = None if max_turns is None else str(max_turns)
    if stored:
        await system_settings.put_settings(stored)
    return await view()
