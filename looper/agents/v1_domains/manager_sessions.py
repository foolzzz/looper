"""The operator's list of the default Manager's sessions, and resetting one of them.

``main`` lives in ``agent_sessions``. Each group and each other sender's direct chat lives in
``manager_sessions``. Reset does what ``/new`` does in that session (clear the session id and stop
the process once the turn lock is free) and does not post into the chat.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .. import session_routing
from ..action_levels import ActionLevel
from ..api_v1 import Operation, _array, _object, _STR, _BOOL
from ..managers_api import DEFAULT_MANAGER_ID
from ..ops import ErrorCode, OpError
from ..session_routing import MAIN_KEY, SessionKey, SessionKind
from ...db import manager_sessions as db_manager_sessions
from ...db.connection import get_pool
from .chat_controls import _manager

_IDENTIFIER = re.compile(r"^(?:ou|on|oc|cli)_[0-9A-Za-z]+$")
_NULL_NUM = {"type": ["number", "null"]}
_SESSION = _object({
    "key": _STR, "kind": {"type": "string", "enum": ["main", "group", "dm"]}, "name": _STR,
    "resident": _BOOL, "last_active_at": {"type": ["string", "null"]}, "context_tokens": {"type": ["integer", "null"]},
    "cost_usd": _NULL_NUM,
})
VIEW = _object({"routing_enabled": _BOOL, "sessions": _array(_SESSION)})
RESULTS = {}
_RESET = "重置这个会话？管家会忘掉这里之前的对话，下一条消息开一个新会话。"
OPERATIONS = (
    Operation("manager_sessions.list", "POST", "/api/v1/manager_sessions/list",
              "List the default Manager's sessions.",
              "List the default Manager's sessions. Each row is main, or one group or other sender's direct chat, "
              "with its name, whether a process is running, its last activity and its cost.",
              _object({}, closed=True), VIEW, risk="read", operator_only=True,
              level=ActionLevel.INSIDE),  # reads the default Manager's own session rows; sends nothing
    Operation("manager_sessions.reset", "POST", "/api/v1/manager_sessions/reset",
              "Reset one Manager session.",
              "Reset one Manager session. Clears its session id and stops its process once idle, the same as /new "
              "in that session. The next message there starts a new session.",
              _object({"key": {"type": "string", "minLength": 1}}, closed=True), _SESSION,
              risk="write", confirms=True, operator_only=True,
              level=ActionLevel.INSIDE),  # clears one Looper session and stops its process; sends nothing
)


def display_name(kind: str, chat: str | None, *candidates: str | None) -> str:
    """A stored chat name, or the chat id's last 6 characters. Never a raw open_id. Main has no chat."""
    if kind == "main":
        return "主会话"
    raw = {chat}
    for candidate in candidates:
        text = (candidate or "").strip()
        if not text or text in raw or _IDENTIFIER.fullmatch(text):
            continue
        return text
    return (chat or "")[-6:] or "会话"


def parse_key(text: str) -> SessionKey:
    """``main``, or ``kind:channel_id:chat`` (the chat may contain colons). Anything else is refused."""
    if "~" in text:
        raise ValueError("session key must be main or kind:channel:chat")
    if text == SessionKind.MAIN.value:
        return MAIN_KEY
    kind, _, rest = text.partition(":")
    channel, separator, chat = rest.partition(":")
    if kind not in (SessionKind.GROUP.value, SessionKind.DM.value) or not separator or not channel or not chat:
        raise ValueError("session key must be main or kind:channel:chat")
    return SessionKey(SessionKind(kind), channel, chat)


def _cost(totals: Any) -> float | None:
    if isinstance(totals, str):
        try:
            totals = json.loads(totals)
        except ValueError:
            return None
    if not isinstance(totals, dict):
        return None
    value = totals.get("cost_usd")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _resident(agent: Any, key: str) -> bool:
    session = agent._sessions.get(key)
    return bool(session is not None and session.claude.started)


def _view(agent: Any, *, key: str, kind: str, name: str, last_active_at: Any, totals: Any) -> dict[str, Any]:
    return {"key": key, "kind": kind, "name": name, "resident": _resident(agent, key),
            "last_active_at": last_active_at, "context_tokens": None,  # filled in by 1.4
            "cost_usd": _cost(totals)}


async def _seen_names(rows: list[dict[str, Any]]) -> dict[tuple[str, str], str | None]:
    channels = list({row["channel_id"] for row in rows})
    if not channels:
        return {}
    found = await get_pool().fetch(
        "SELECT channel_id, chat_id, name FROM channel_seen_chats WHERE channel_id = ANY($1::text[])", channels)
    return {(row["channel_id"], row["chat_id"]): row["name"] for row in found}


async def _main_row(agent: Any) -> dict[str, Any]:
    saved = await get_pool().fetchrow(
        "SELECT session_totals, updated_at FROM agent_sessions WHERE agent_id = $1", agent.agent_id)
    totals = None if saved is None else saved["session_totals"]
    updated = None if saved is None else saved["updated_at"]
    return _view(agent, key=SessionKind.MAIN.value, kind="main", name=display_name("main", None),
                 last_active_at=updated, totals=totals)


async def _rows(agent: Any) -> list[dict[str, Any]]:
    stored = await db_manager_sessions.list(agent.agent_id)
    names = await _seen_names(stored)
    rows = [await _main_row(agent)]
    for row in stored:
        rows.append(_view(agent, key=row["session_key"], kind=row["kind"],
                          name=display_name(row["kind"], row["chat"], names.get((row["channel_id"], row["chat"]))),
                          last_active_at=row["last_active_at"], totals=row["session_totals"]))
    return rows


def _default(server: Any) -> Any:
    agent = _manager(server, DEFAULT_MANAGER_ID)
    if agent.agent_id != DEFAULT_MANAGER_ID:
        raise OpError(ErrorCode.UNAVAILABLE, "the default Manager is not running")
    return agent


async def impact(server, operation, arguments, principal, headers):
    if operation.operation_id != "manager_sessions.reset":
        return None
    return _RESET


async def run(server, operation, arguments, principal, headers):
    agent = _default(server)
    if operation.operation_id == "manager_sessions.list":
        return {"routing_enabled": session_routing.routing_enabled(), "sessions": await _rows(agent)}
    try:
        key = parse_key(arguments["key"])
    except ValueError as exc:
        raise OpError(ErrorCode.INVALID, str(exc)) from None
    if key.kind is not SessionKind.MAIN and await db_manager_sessions.get(agent.agent_id, key.text) is None:
        raise OpError(ErrorCode.NOT_FOUND, "no such Manager session")
    await agent.reset_named_session(key)
    rows = await _rows(agent)
    found = next((row for row in rows if row["key"] == key.text), None)
    if found is None:
        raise OpError(ErrorCode.NOT_FOUND, "no such Manager session")
    return found
