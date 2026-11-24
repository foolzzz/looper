"""Operator-only lesson settings, controls and auditable task readback."""
from .. import memory_lessons, memory_model as model
from ..action_levels import ActionLevel
from ..api_v1 import Operation, _object, _array, _BOOL, _STR, _NULL_STR, _INT, _OBJ
from ..ops import ErrorCode, OpError
from .memory import CANDIDATE, _refusal
from ...db import memory as db_memory

_VIEW = _object({"configured": _BOOL, "enabled": _BOOL, "settings": _OBJ, "problem": _NULL_STR}, closed=True)
_STATS = _object({**dict.fromkeys(("injected", "cited", "wrong_tasks", "reinforced"), _INT),
                  "last_injected_at": _NULL_STR, "last_cited_at": _NULL_STR}, closed=True)
_WEEKLY = _object({"enabled": _BOOL, "weekday": _INT, "time": _STR, "push_channel": _NULL_STR}, [], closed=True)
_SETTINGS = _object({"enabled": _BOOL, "auto_activate": _BOOL,
    "inject": _object(dict.fromkeys(("planner", "worker", "reviewer"), _BOOL), [], closed=True),
    **dict.fromkeys(("max_lessons", "max_chars", "project_cap", "valid_days", "wrong_threshold", "min_journal_entries"), _INT),
    "retro_budget_usd": {"type": "number"}, "daily_budget_usd": {"type": "number"}, "weekly": _WEEKLY}, [], closed=True)
_NULL_INT = {"type": ["integer", "null"]}
_SNAPSHOT = _object({"task_id": _STR, "candidate_id": _STR, "position": _INT,
    "line_text": _NULL_STR, "activated_at": _NULL_STR, "fingerprint": _NULL_STR,
    "state": _STR, "reason": _NULL_STR, "valid_until": _STR}, closed=True)
_ITEM = _object({"injection_id": _INT, "candidate_id": _STR, "position": _INT,
                 "cited": _BOOL, "wrong": _BOOL}, closed=True)
_INJECTION = _object({"id": _INT, "task_id": _STR, "agent_kind": _STR,
    "step_id": _NULL_STR, "record_id": _NULL_INT, "block_sha256": _STR,
    "block_chars": _INT, "omitted": _INT, "created_at": _STR, "delivery_key": _STR,
    "session_key": _NULL_STR, "delivered_at": _NULL_STR,
    "state": {"type": "string", "enum": ["prepared", "delivered", "abandoned", "uncertain"]},
    "block_text": _STR, "candidate_ids": _array(_STR), "snapshot_key": _STR, "items": _array(_ITEM)}, closed=True)
RESULTS = {}
# These operations only read or change Looper state; none exposes an agent tool or writes a vault note.
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
        "active weekly delivery target changes. Enabled projects inject lessons when their agents start.",
        _SETTINGS, _VIEW, risk="write", confirms=True, operator_only=True, level=ActionLevel.INSIDE),
    Operation("memory.task_lessons", "POST", "/api/v1/memory/task_lessons",
        "Read a task's frozen lessons and paginated injection history.",
        "Read a task's frozen lessons and paginated injection history. Includes the delivered text and reference flags.",
        _object({"task_id": _STR, "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                 "before": {"type": "integer", "minimum": 1, "maximum": 2**63 - 1}}, ["task_id"], closed=True),
        _object({"initialized": _BOOL, "snapshot": _array(_SNAPSHOT), "injections": _array(_INJECTION),
                 "more": _BOOL}, closed=True),
        risk="read", operator_only=True, level=ActionLevel.INSIDE),
    Operation("memory.revoke", "POST", "/api/v1/memory/revoke", "Revoke an active project lesson.",
        "Revoke an active project lesson. Records the operator's reason and a fingerprint tombstone atomically.",
        _object({"candidate": {"type": "string", "pattern": r"^[Mm]-\d{1,18}$"},
                 "reason": {"type": "string", "maxLength": model.NOTE_MAX_CHARS}}, ["candidate"], closed=True),
        _object({"candidate": CANDIDATE}, closed=True), risk="write", operator_only=True,
        audit_operator=False, level=ActionLevel.INSIDE),
    Operation("memory.renew", "POST", "/api/v1/memory/renew", "Renew an active project lesson's validity.",
        "Renew an active project lesson's validity. Leaves frozen task text and activation dates unchanged.",
        _object({"candidate": {"type": "string", "pattern": r"^[Mm]-\d{1,18}$"}}, closed=True),
        _object({"candidate": CANDIDATE}, closed=True), risk="write", operator_only=True,
        audit_operator=False, level=ActionLevel.INSIDE),
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
        if operation.operation_id == "memory.task_lessons":
            return await memory_lessons.task_view(**arguments)
        if operation.operation_id in {"memory.revoke", "memory.renew"}:
            return await memory_lessons.act(operation.operation_id.rsplit(".", 1)[1], **arguments)
        return await memory_lessons.update_settings(arguments)
    except db_memory.MemoryRefused as error:
        raise _refusal(error) from None
    except ValueError as error:
        raise OpError(ErrorCode.INVALID, str(error)) from None
