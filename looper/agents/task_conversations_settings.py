"""Operator-only work-mode setting, separate from agent-accessible chat settings."""

from ..db import system_settings as db_settings

KEY = "tasks.conversations.enabled"
MANAGER_ANSWERS_KEY = "tasks.conversations.manager_answers"
MAX_RESIDENT_KEY = "tasks.managers.max_resident"
DEFAULT_MAX_RESIDENT = 6
_enabled = False
_manager_answers = False
_max_resident = DEFAULT_MAX_RESIDENT
_UNSET = object()


def enabled() -> bool:
    return _enabled


def set_enabled(value: bool) -> None:
    """Apply a value in memory; tests may set it without persisting a setting."""
    global _enabled
    _enabled = value


def manager_answers() -> bool:
    """Whether the task Manager answers task messages. Independent of ``enabled``; only the string ``true`` is on."""
    return _manager_answers


def set_manager_answers(value: bool) -> None:
    global _manager_answers
    _manager_answers = value


async def load() -> None:
    global _max_resident
    stored = await db_settings.get_settings([KEY, MANAGER_ANSWERS_KEY, MAX_RESIDENT_KEY])
    set_enabled(stored.get(KEY) == "true")
    set_manager_answers(stored.get(MANAGER_ANSWERS_KEY) == "true")
    _max_resident = int(stored.get(MAX_RESIDENT_KEY, DEFAULT_MAX_RESIDENT))
    _checked_max_resident(_max_resident)


def max_resident() -> int:
    return _max_resident


def view() -> dict[str, bool | int]:
    return {"enabled": enabled(), "manager_answers": manager_answers(), "max_resident": max_resident()}


def _stored(value: bool | None, name: str = "enabled") -> str | None:
    if value is not None and not isinstance(value, bool):
        raise ValueError(f"{name} must be true, false or null")
    return None if value is None else ("true" if value else "false")


def _checked_max_resident(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not int or not 1 <= value <= 20:
        raise ValueError("max_resident must be an integer from 1 to 20 or null")
    return str(value)


async def update(value: bool | None | object = _UNSET, *, max_resident: int | None | object = _UNSET,
                 manager_answers: bool | None | object = _UNSET) -> dict[str, bool | int]:
    """Persist first: a failed write leaves the effective setting unchanged."""
    global _max_resident
    fields = {}
    if value is not _UNSET:
        fields[KEY] = _stored(value)
    if manager_answers is not _UNSET:
        fields[MANAGER_ANSWERS_KEY] = _stored(manager_answers, "manager_answers")
    if max_resident is not _UNSET:
        fields[MAX_RESIDENT_KEY] = _checked_max_resident(max_resident)
    await db_settings.put_settings(fields)
    if value is not _UNSET:
        set_enabled(False if value is None else value)
    if manager_answers is not _UNSET:
        set_manager_answers(False if manager_answers is None else manager_answers)
    if max_resident is not _UNSET:
        _max_resident = DEFAULT_MAX_RESIDENT if max_resident is None else max_resident
    return view()
