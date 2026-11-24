"""Operator-only lesson foundation, automatically exposed by the generated CLI."""
from .. import memory_lessons, memory_model as model
from ..action_levels import ActionLevel
from ..api_v1 import Operation, _object, _array, _BOOL, _STR, _NULL_STR, _INT, _OBJ
from ..ops import ErrorCode, OpError
from .memory import CANDIDATE

_VIEW = _object({"configured": _BOOL, "enabled": _BOOL, "settings": _OBJ, "problem": _NULL_STR}, closed=True)
_STATS = _object({**dict.fromkeys(("injected", "cited", "wrong_tasks", "reinforced"), _INT),
                  "last_injected_at": _NULL_STR, "last_cited_at": _NULL_STR}, closed=True)
_WEEKLY = _object({"enabled": _BOOL, "weekday": _INT, "time": _STR, "push_channel": _NULL_STR}, [], closed=True)
_SETTINGS = _object({"enabled": _BOOL, "auto_activate": _BOOL,
    "inject": _object(dict.fromkeys(("planner", "worker", "reviewer"), _BOOL), [], closed=True),
    **dict.fromkeys(("max_lessons", "max_chars", "project_cap", "valid_days", "wrong_threshold", "min_journal_entries"), _INT),
    "retro_budget_usd": {"type": "number"}, "daily_budget_usd": {"type": "number"}, "weekly": _WEEKLY}, [], closed=True)
RESULTS = {}
# All three operations stay inside Looper: reading rows or replacing a system setting, with no agent tool.
OPERATIONS = (
    Operation("memory.lessons_list", "POST", "/api/v1/memory/lessons_list", "List project lessons, newest first.",
        "List project lessons, newest first. Reads only Looper lessons; before continues from an M-id.",
        _object({"project": _STR, "state": {"type": "string", "enum": ["all", *sorted(state.value for state in model.LOOPER_STATES)]},
                 "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                 "before": {"type": "string", "pattern": r"^[Mm]-\d{1,18}$"}}, [], closed=True),
        _object({"lessons": _array(_object({"candidate": CANDIDATE, "stats": _STATS}, closed=True)),
                 "more": _BOOL}, closed=True), risk="read", operator_only=True, level=ActionLevel.INSIDE),
    Operation("memory_lessons.settings_get", "POST", "/api/v1/memory_lessons/settings_get",
        "Read lesson settings and any configuration problem.",
        "Read lesson settings and any configuration problem. Missing settings are disabled by default.",
        _object({}, closed=True), _VIEW, risk="read", operator_only=True, level=ActionLevel.INSIDE),
    Operation("memory_lessons.settings_put", "POST", "/api/v1/memory_lessons/settings_put",
        "Replace lesson settings, filling omitted fields with defaults.",
        "Replace lesson settings, filling omitted fields with defaults. Confirms automatic activation and "
        "active weekly delivery target changes; this foundation starts no processing.",
        _SETTINGS, _VIEW, risk="write", confirms=True, operator_only=True, level=ActionLevel.INSIDE),
)


async def impact(server, operation, arguments, principal, headers):
    if operation.operation_id == "memory_lessons.settings_put":
        try:
            return await memory_lessons.confirmation_impact(arguments)
        except ValueError as error:
            raise OpError(ErrorCode.INVALID, str(error)) from None
    return None


async def run(server, operation, arguments, principal, headers):
    try:
        if operation.operation_id == "memory.lessons_list":
            return await memory_lessons.list_view(**arguments)
        if operation.operation_id == "memory_lessons.settings_get":
            return await memory_lessons.settings_view()
        return await memory_lessons.update_settings(arguments)
    except ValueError as error:
        raise OpError(ErrorCode.INVALID, str(error)) from None
