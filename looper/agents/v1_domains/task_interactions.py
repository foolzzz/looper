"""Task-page interactions, reusing the gate, wait, role and room cores."""

from .. import role_api, room, wait_service, waits
from ..api_v1 import Operation, _array, _object, _STR, _NULL_STR, _INT, _BOOL
from ..message import channel_address
from ..ops import API_CHANNEL, ErrorCode, OpError
from ..ops.service import _plain
from ..projects import RequestError

ORDER = 7


def _enum(values, *, nullable=False):
    return {"type": ["string", "null"] if nullable else "string",
            "enum": [v.value for v in values] + ([None] if nullable else [])}


TASK_WAIT = _object({
    "step": _STR, "step_id": _STR, "state": _enum(waits.WaitState),
    "outcome": _enum(waits.WaitOutcome, nullable=True), "topic": _STR,
    "check": {"oneOf": [
        {"type": "null"},
        _object({"type": {"const": "github_pr"}, "repo": _STR, "number": _INT,
                 "ready_when": _enum(waits.ReadyWhen)}, closed=True),
        _object({"type": {"const": "command"}, "command": _STR}, closed=True)]},
    "every_seconds": {"type": ["integer", "null"]},
    **dict.fromkeys(("opened_at", "due_at", "deadline_at", "next_check_at", "last_checked_at", "closed_at"), _NULL_STR),
    "checks": _INT, "last_result": _enum(waits.CheckResult, nullable=True),
    **dict.fromkeys(("last_output", "last_error", "resolution"), _STR),
    "notice": {"type": ["string", "null"], "enum": ["sent", "dashboard", "failed", None]},
    "depends_on": _array(_STR)})
_WAIT_RESULT = _object({"task_id": _STR, "wait": TASK_WAIT})

_COUNTS = _object({"messages": _INT, "by_kind": {"type": "object", "additionalProperties": _INT},
                   "questions": _INT, "unanswered": _INT})
_SEAT = _object({"seat": _STR, "step": _STR, "presence": _enum(room.Presence),
                 "role": _STR, "kind": _enum(room.MemberKind), "step_status": _STR, "since": _STR},
                ["seat", "step", "presence", "role", "kind", "step_status"])
_MESSAGE = _object({
    "id": _INT, "from": _STR, "to": {"oneOf": [{"const": room.EVERYONE}, _array(_STR)]},
    "kind": _enum(room.MessageKind), "body": _STR, "reply_to": _INT, "answers_ids": _array(_INT),
    "files": _array(_STR), "commits": _array(_STR), "reason": _enum(room.Reason),
    "via": {"type": "string", "enum": ["planner_reply", "looper", "consult"]},
    "at": _STR, "cc_planner": _BOOL, "answered": _BOOL},
    ["id", "from", "to", "kind", "body", "cc_planner", "answered"])
_CONSULTS = _object({
    "total": _INT, "answered": _INT, "by_reason": {"type": "object", "additionalProperties": _INT},
    "cost_usd": {"type": "number"}, "list": _array(_object({
        "seat": _STR, "questions": _array(_INT),
        "state": {"type": "string", "enum": ["queued", "running", "answered", "fallback", "failed"]},
        "reason": _enum(room.FallbackReason, nullable=True),
        "read_only": {"type": ["string", "null"], "enum": ["runtime", "snapshot", None]},
        "denied_writes": _INT, "cost_usd": {"type": ["number", "null"]}, "at": _NULL_STR}))})
ROOM_VIEW = _object({
    "enabled": _BOOL, "counts": _COUNTS,
    "by_step": {"type": "object", "additionalProperties": _object({
        "sent": _INT, "received": _INT, "unanswered": _INT})},
    "seats": _array(_SEAT), "messages": _array(_MESSAGE), "more": _BOOL, "consults": _CONSULTS})

RESULTS = {"tasks.room": _object({
    "task_id": _STR, "enabled": _BOOL, "counts": _COUNTS,
    "seats": _array(_object({"seat": _STR, "step": _STR, "presence": _STR})),
    "messages": _array(_MESSAGE), "more": _BOOL})}


def _op(name, request, result, risk):
    description = name.replace(".", " ").replace("_", " ").capitalize() + "."
    return Operation(name, "POST", "/api/v1/" + name.replace(".", "/"), description, description,
                     request, result, risk=risk)


OPERATIONS = (
    *(_op("waits." + action, _object({"task_id": _STR, "step": _STR}, closed=True), _WAIT_RESULT, "write")
      for action in ("resume", "cancel")),
    _op("tasks.role_update", _object({
        "task_id": _STR, "key": _STR, "role_prompt": _STR, "harness": _NULL_STR, "profile_id": _NULL_STR,
        "items": _array(_STR)}, ["task_id", "key", "role_prompt", "harness", "profile_id"], closed=True),
        _object({"task_id": _STR, "id": _STR, "kind": {"type": "string", "enum": ["work", "review"]},
                 "version": _INT, "via": {"const": "user"}, "changed": _array(_STR)}), "write"),
    _op("tasks.room_view", _object({
        "task_id": _STR, "kind": _enum(room.MessageKind), "from": _STR, "unanswered": _BOOL,
        "after": {"type": "integer", "minimum": 0},
        "limit": {"type": "integer", "minimum": 1, "maximum": room.TASK_VIEW_LIMIT}},
        ["task_id"], closed=True), ROOM_VIEW, "read"),
)


async def run(server, operation, arguments, principal, headers):
    task_id = arguments["task_id"]
    # Identity comes from authentication, never from request fields. A role's
    # via=user is the core's explicit-human-edit contract, separate from identity.
    if operation.risk != "read":
        source = (server._web_adapter._address if principal.kind == "operator" else
                  channel_address(API_CHANNEL, principal.client_id))
    try:
        if operation.operation_id == "tasks.room_view":
            pool = server._worker_pool
            result = await room.task_view(
                task_id, runs=pool.room_runs(task_id) if pool is not None else None,
                kind=arguments.get("kind"), sender=arguments.get("from"),
                unanswered=arguments.get("unanswered", False), after=arguments.get("after", 0),
                limit=arguments.get("limit", room.TASK_VIEW_LIMIT))
            if result is None:
                raise OpError(ErrorCode.NOT_FOUND, "task not found")
        elif operation.operation_id == "tasks.role_update":
            result = await role_api.edit_task_role(
                task_id, arguments["key"], {k: v for k, v in arguments.items() if k not in ("task_id", "key")},
                task_router=server._task_router, bus=server._supervisor.bus, source=source)
        else:
            request = wait_service.resume if operation.operation_id == "waits.resume" else wait_service.cancel
            result = await request(task_id, arguments["step"],
                                   has_planner=server._task_router.has_planner(task_id),
                                   bus=server._supervisor.bus, source=source)
        return _plain(result)
    except RequestError as exc:
        code = {"404 Not Found": ErrorCode.NOT_FOUND, "409 Conflict": ErrorCode.CONFLICT}.get(
            exc.status, ErrorCode.INVALID)
        raise OpError(code, str(exc)) from None
