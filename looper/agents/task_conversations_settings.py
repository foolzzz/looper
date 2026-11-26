"""Operator-only work-mode setting, separate from agent-accessible chat settings."""

from ..db import system_settings as db_settings

KEY = "tasks.conversations.enabled"
MAX_RESIDENT_KEY = "tasks.managers.max_resident"
DEFAULT_MAX_RESIDENT = 6
_enabled = False
_max_resident = DEFAULT_MAX_RESIDENT
_UNSET = object()


def enabled() -> bool:
    return _enabled


def set_enabled(value: bool) -> None:
    """Apply a value in memory; tests may set it without persisting a setting."""
    global _enabled
    _enabled = value


async def load() -> None:
    global _max_resident
    stored = await db_settings.get_settings([KEY, MAX_RESIDENT_KEY])
    set_enabled(stored.get(KEY) == "true")
    _max_resident = int(stored.get(MAX_RESIDENT_KEY, DEFAULT_MAX_RESIDENT))
    _checked_max_resident(_max_resident)


def max_resident() -> int:
    return _max_resident


def view() -> dict[str, bool | int]:
    return {"enabled": enabled(), "max_resident": max_resident()}


def _stored(value: bool | None) -> str | None:
    if value is not None and not isinstance(value, bool):
        raise ValueError("enabled must be true, false or null")
    return None if value is None else ("true" if value else "false")


def _checked_max_resident(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not int or not 1 <= value <= 20:
        raise ValueError("max_resident must be an integer from 1 to 20 or null")
    return str(value)


async def update(value: bool | None | object = _UNSET, *, max_resident: int | None | object = _UNSET
                 ) -> dict[str, bool | int]:
    """Persist first: a failed write leaves the effective setting unchanged."""
    global _max_resident
    fields = {}
    if value is not _UNSET:
        fields[KEY] = _stored(value)
    if max_resident is not _UNSET:
        fields[MAX_RESIDENT_KEY] = _checked_max_resident(max_resident)
    await db_settings.put_settings(fields)
    if value is not _UNSET:
        set_enabled(False if value is None else value)
    if max_resident is not _UNSET:
        _max_resident = DEFAULT_MAX_RESIDENT if max_resident is None else max_resident
    return view()
