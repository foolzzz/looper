"""Operator conversation history and dashboard target projections.

Database read failures now return unavailable instead of an empty history, and
save failures return unavailable instead of claiming persistence succeeded.
Client-owned ``chat.*`` operations and channel delivery are unchanged.
"""

import base64
import hashlib
import json

from cryptography.fernet import Fernet, InvalidToken

from ...db import chat, managers as db_managers, task_chat
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
                                        "last_role", "last_content"), _STR), "message_count": _INT,
    "client": {"oneOf": [_object({"id": _STR, "name": _STR}, closed=True), {"type": "null"}]}}, closed=True)
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
    _op("messages", {"session_id": _ID, "manager": _STR, "before": _STR,
        "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 200}},
        _object({"messages": _array(DB_CHAT_MESSAGE), "has_more": _BOOL, "next_before": _NULL_STR,
                 "turn_state": {"enum": ["idle", "running", "interrupted"]}},
                ["messages", "has_more", "next_before"], closed=True),
        ["session_id"]),
    _op("save_message", {"session_id": _ID, "message_id": _ID, "role": _ID, "content": _STR},
        _object({"ok": _BOOL}), risk="write"),
)
RESULTS = {}


def _cursor_codec(server):
    # The operator credential survives restarts. Domain separation keeps cursor encryption independent of auth.
    if server._operator_token:
        key = hashlib.sha256(("operator-chat-cursor-v1:" + server._operator_token).encode()).digest()
        return Fernet(base64.urlsafe_b64encode(key))
    # In-process servers without an operator credential still need an unpredictable key.
    if not hasattr(server, "_history_cursor_codec"):
        server._history_cursor_codec = Fernet(Fernet.generate_key())
    return server._history_cursor_codec


def _read_cursor(server, cursor, session_id):
    try:
        if len(cursor) > 4096 or not cursor.startswith("v1."):
            raise ValueError
        data = json.loads(_cursor_codec(server).decrypt(cursor[3:].encode()))
        if (not isinstance(data, dict) or set(data) != {"id", "session"}
                or type(data["id"]) is not int or not 0 < data["id"] < 2**63
                or not isinstance(data["session"], str)):
            raise ValueError
    except (InvalidToken, ValueError, UnicodeError):
        raise OpError(ErrorCode.INVALID_CURSOR, "invalid history cursor") from None
    if data["session"] != session_id:
        raise OpError(ErrorCode.CURSOR_SESSION_MISMATCH, "history cursor belongs to another session")
    return data["id"]


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
    # A task conversation belongs to its task. The operation is already operator-only.
    task_thread = action == "messages" and bool(session_id) and session_id.startswith(chat.TASK_THREAD_PREFIX)
    if session_id and not task_thread and (owner := await server._thread_owner_elsewhere(session_id, manager_id)):
        raise await server._foreign_thread_error(owner)
    if action == "sessions":
        sessions = await chat.list_sessions(session_id=session_id, manager_id=manager_id)
        for session in sessions:
            client_id, client_name = session.pop("client_id"), session.pop("client_name")
            session["client"] = {"id": client_id, "name": client_name} if client_id is not None else None
        return _plain({"sessions": sessions})
    before = _read_cursor(server, arguments["before"], session_id) if "before" in arguments else None
    messages, has_more, next_id = await chat.older_messages(session_id, before=before, limit=arguments.get("limit", 200))
    next_before = ("v1." + _cursor_codec(server).encrypt(json.dumps({"id": next_id, "session": session_id}).encode()).decode()
                   if next_id is not None else None)
    result = {"messages": messages, "has_more": has_more, "next_before": next_before}
    if task_thread:
        result["turn_state"] = await task_chat.turn_state(session_id.removeprefix(chat.TASK_THREAD_PREFIX))
    return _plain(result)
