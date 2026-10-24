"""Operator conversation history and dashboard target projections.

Database read failures now return unavailable instead of an empty history, and
save failures return unavailable instead of claiming persistence succeeded.
Client-owned ``chat.*`` operations and channel delivery are unchanged.
"""

from ...db import chat, managers as db_managers
from ..api_v1 import Operation, _array, _object, _STR, _NULL_STR, _INT, _BOOL
from ..chat_targets import list_chat_targets
from ..ops import ErrorCode, OpError
from ..ops.service import _plain


_ID = {"type": "string", "minLength": 1}
_ROLE = _object({"id": _STR, "kind": {"enum": ["work", "review"]}, "version": _INT}, closed=True)
MANAGER_TARGET = _object({"kind": {"const": "manager"}, "id": _STR, "name": _STR,
                          "can_send": {"const": True}}, ["kind", "id", "can_send"], closed=True)
PLANNER_TARGET = _object({"kind": {"const": "planner"}, "id": _STR, "task_id": _STR,
                          "requirement": _STR, "name": _STR, "status": _STR, "can_send": _BOOL}, closed=True)
_STEP_FIELDS = {"id": _STR, "task_id": _STR, "status": _STR,
                "step_index": {"type": ["integer", "null"]}, "instruction": _STR,
                "started_at": _STR, "can_send": {"const": False}, "task_role": _ROLE}
CHAT_TARGET_INFO = {"oneOf": [MANAGER_TARGET, PLANNER_TARGET, *(
    _object({"kind": {"const": kind}, **_STEP_FIELDS},
            ["kind", *(key for key in _STEP_FIELDS if key != "task_role")], closed=True)
    for kind in ("worker", "reviewer"))]}
CHAT_SESSION = _object({**dict.fromkeys(("session_id", "channel", "first_message_at", "last_message_at",
                                        "last_role", "last_content"), _STR), "message_count": _INT}, closed=True)
DB_CHAT_MESSAGE = _object({**dict.fromkeys(("message_id", "role", "content", "created_at", "channel"), _STR),
    "delivery_failure": {"enum": ["no_chat", "send_failed", None]}, "delivery_error": _NULL_STR},
    ["message_id", "role", "content", "created_at"], closed=True)


def _op(action, fields, result, required=None, risk="read"):
    description = f"Operator chat: {action.replace('_', ' ')}."
    return Operation(f"operator_chat.{action}", "POST", f"/api/v1/operator_chat/{action}",
                     description, description, _object(fields, required, closed=True), result,
                     risk=risk, operator_only=True)


OPERATIONS = (
    _op("targets", {}, _object({"targets": _array(CHAT_TARGET_INFO)})),
    _op("sessions", {"manager": _STR, "session_id": _STR}, _object({"sessions": _array(CHAT_SESSION)}), []),
    _op("messages", {"session_id": _ID, "manager": _STR}, _object({"messages": _array(DB_CHAT_MESSAGE)}),
        ["session_id"]),
    _op("save_message", {"session_id": _ID, "message_id": _ID, "role": _ID, "content": _STR},
        _object({"ok": _BOOL}), risk="write"),
)
RESULTS = {}


async def run(server, operation, arguments, principal, headers):
    action = operation.operation_id.removeprefix("operator_chat.")
    if action == "targets":
        managers = None if server._managers is None else [
            {"id": agent.agent_id, "name": agent.name} for agent in server._managers.agents()]
        return _plain({"targets": await list_chat_targets(server._task_router, managers=managers)})
    if action == "save_message":
        await chat.save_message(**arguments)
        return {"ok": True}
    manager_id = arguments.get("manager") or "manager"
    session_id = arguments.get("session_id") or None
    if action == "sessions" and manager_id != "manager" and await db_managers.get_manager(manager_id) is None:
        raise OpError(ErrorCode.NOT_FOUND, f"no Manager '{manager_id}'")
    if session_id and (owner := await server._thread_owner_elsewhere(session_id, manager_id)):
        raise await server._foreign_thread_error(owner)
    if action == "sessions":
        return _plain({"sessions": await chat.list_sessions(session_id=session_id, manager_id=manager_id)})
    return _plain({"messages": await chat.list_messages(session_id)})
