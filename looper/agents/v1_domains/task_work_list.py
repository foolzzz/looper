"""Operator-only work-list reads and activity watermarks."""

from ...db import work_list
from ..action_levels import ActionLevel
from ..api_v1 import Operation, _array, _object, _STR, _NULL_STR, _INT, _BOOL
from ..ops import ErrorCode, OpError
from ..ops.service import _plain

_ROW = _object({
    **dict.fromkeys(("task_id", "title", "requirement_head", "project_id", "project_name", "status", "group",
                     "created_at", "last_activity_at"), _STR),
    **dict.fromkeys(("run_no", "last_seq", "waiting_gates"), _INT),
    **dict.fromkeys(("needs_you", "unseen"), _BOOL), "ended_at": _NULL_STR}, closed=True)
_SCHEDULE = _object({
    **dict.fromkeys(("schedule_id", "name", "project_id", "project_name"), _STR),
    **dict.fromkeys(("last_success_at", "last_task_id", "last_status", "last_ended_at"), _NULL_STR),
    "enabled": _BOOL, "running": _BOOL, "consecutive_failures": _INT}, closed=True)
_PAGE = _object({"items": _array(_ROW), "counts": {"type": "object", "additionalProperties": _INT},
                 "next_cursor": _NULL_STR}, closed=True)
_SCHEDULE_PAGE = _object({"schedules": _array(_SCHEDULE), "next_cursor": _NULL_STR}, closed=True)
_RESULT = {**_object({**_PAGE["properties"], **_SCHEDULE_PAGE["properties"]}, ["next_cursor"], closed=True),
           "oneOf": [_PAGE, _SCHEDULE_PAGE]}
RESULTS = {}
OPERATIONS = (
    Operation("tasks.work_list", "POST", "/api/v1/tasks/work_list", "Read a work-list section.",
        "Read a work-list section. Keyset pages include activity, waiting gates and unseen progress; "
        "scheduled tasks are folded into one row per schedule. Operator-only.",
        _object({"section": {"type": "string", "enum": ["open", "ended", "schedules"]},
            "project_id": _STR, "statuses": {"type": "array", "items": {"type": "string", "minLength": 1},
                                               "minItems": 1, "maxItems": 10},
            "cursor": {"type": "string", "maxLength": 4096},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50}},
            ["section"], closed=True), _RESULT,
        risk="read", operator_only=True, level=ActionLevel.INSIDE),  # reads only Looper's stored task state
    Operation("tasks.mark_seen", "POST", "/api/v1/tasks/mark_seen", "Record seen task activity.",
        "Record seen task activity. The operator's activity watermark only moves forward.",
        _object({"task_id": {"type": "string", "minLength": 1},
                 "seq": {"type": "integer", "minimum": 0, "maximum": 9223372036854775807}}, closed=True),
        _object({"task_id": _STR, "seen_activity_seq": _INT}, closed=True),
        risk="write", operator_only=True, level=ActionLevel.INSIDE),  # changes only the operator's seen watermark
)


async def run(server, operation, arguments, principal, headers):
    if operation.operation_id == "tasks.mark_seen":
        seq = await work_list.mark_seen(principal, arguments["task_id"], arguments["seq"])
        if seq is None:
            raise OpError(ErrorCode.NOT_FOUND, "Task not found.", field="task_id")
        return {"task_id": arguments["task_id"], "seen_activity_seq": seq}
    arguments = dict(arguments)
    section = arguments.pop("section")
    if section == "schedules" and "statuses" in arguments:
        raise OpError(ErrorCode.INVALID, "Statuses apply only to open and ended sections.", field="statuses")
    try:
        if section == "schedules":
            result = await work_list.schedule_section(**arguments)
        else:
            read = work_list.open_section if section == "open" else work_list.ended_section
            result = await read(principal, **arguments)
    except ValueError as exc:
        raise OpError(ErrorCode.INVALID, str(exc), field="cursor") from exc
    return _plain(result)
