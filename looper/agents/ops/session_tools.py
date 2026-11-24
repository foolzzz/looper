"""Task tools whose complete input and output boundary is one authenticated chat."""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, fields
from enum import Enum
from typing import Any

from ...db.connection import get_pool
from ...db import projects as db_projects, tasks as db_tasks
from ..operator_identity import operator_sender_id
from .. import task_notice
from . import Call, ErrorCode, OpError, Risk, Tool, Turn
from . import tasks


@dataclass(frozen=True)
class ChatTaskSummary:
    task_id: str
    title: str
    status: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ChatTaskDetail(ChatTaskSummary):
    completed_at: str | None
    steps_done: int
    steps_total: int
    open_gates: int
    result: str | None


@dataclass(frozen=True)
class ChatWriteOutcome:
    task_id: str
    title: str
    status: str


@dataclass(frozen=True)
class ChatControlOutcome:
    task_id: str
    status: str


@dataclass(frozen=True)
class ChatInstructOutcome:
    task_id: str
    delivered: bool


class Initiator(str, Enum):
    OPERATOR = "operator"
    OTHER = "other"
    NO_OPERATOR_ID = "no_operator_id"
    NONE = "none"


async def initiator(turn: Turn) -> Initiator:
    if turn.autonomous or not turn.sender:
        return Initiator.NONE
    try:
        operator = await operator_sender_id(turn.channel_id)
    except Exception:
        return Initiator.NO_OPERATOR_ID
    if not operator:
        return Initiator.NO_OPERATOR_ID
    return Initiator.OPERATOR if turn.sender == operator else Initiator.OTHER


# Never expose an implementation error's text: it may contain operator configuration.
SESSION_ERRORS: dict[ErrorCode, str] = {
    ErrorCode.NOT_FOUND: "no such task in this chat",
    ErrorCode.INVALID: "invalid arguments for this chat tool",
    ErrorCode.FORBIDDEN: "this session has no access to that tool",
    ErrorCode.NEEDS_USER_TURN: "this action needs a user's message in this chat",
    ErrorCode.CONFLICT: "the task's current state does not allow this action",
    ErrorCode.STOPPED: "this turn was stopped; nothing ran",
    ErrorCode.EXPIRED: "this turn has ended; nothing ran",
    ErrorCode.NOT_THIS_TURN: "this call was not made in the running turn; it did not run",
    ErrorCode.UNAVAILABLE: "this chat tool is unavailable; try again later",
    ErrorCode.BLOCKED: "this action was blocked; nothing ran",
    ErrorCode.INTERNAL: "this chat tool is unavailable; try again later",
    ErrorCode.RATE_LIMITED: "this chat tool is temporarily rate limited",
    ErrorCode.UNAUTHORIZED: "this call is not authorized",
}
HIGH_REFUSAL = "请在你自己的私聊里做"


async def _owned_task(conn: Any, task_id: str, session_key: str, manager_id: str) -> Mapping[str, Any]:
    row = await conn.fetchrow("SELECT * FROM tasks WHERE id=$1 AND origin_session=$2 AND manager_id=$3",
                              task_id, session_key, manager_id)
    if row is None:
        raise OpError(ErrorCode.NOT_FOUND, SESSION_ERRORS[ErrorCode.NOT_FOUND])
    return row


def _key(call: Call) -> str:
    return call.confirmation["session_key"]


def _summary(row: Mapping[str, Any]) -> dict[str, Any]:
    return {"task_id": row["id"], "title": row["title"] or "", "status": row["status"],
            "created_at": str(row["created_at"]), "updated_at": str(row["updated_at"])}


async def _read(call: Call) -> dict[str, Any]:
    pool, args = get_pool(), call.arguments
    if args["action"] == "list":
        statuses, include = db_tasks._group_filter(db_tasks.TaskGroup(args["group"]) if args.get("group") else None)
        rows = await pool.fetch(
            "SELECT id,title,status,created_at,updated_at FROM tasks WHERE origin_session=$1 AND manager_id=$2 "
            "AND ($3::text[] IS NULL OR (status=ANY($3))=$4) ORDER BY created_at DESC,id DESC LIMIT $5",
            _key(call), call.manager_id, statuses, include, args.get("limit") or 10)
        return {"tasks": [_summary(row) for row in rows]}
    row = await _owned_task(pool, args.get("task_id") or "", _key(call), call.manager_id)
    counts = await pool.fetchrow(
        "SELECT (SELECT count(*) FROM task_steps WHERE task_id=$1 AND status='verified') AS steps_done, "
        "(SELECT count(*) FROM task_steps WHERE task_id=$1) AS steps_total, "
        "(SELECT count(*) FROM task_user_gates WHERE task_id=$1 AND state='open') AS open_gates", row["id"])
    result = None
    if row["status"] in ("completed", "failed", "cancelled", "stopped"):
        channel = task_notice.notify_channel(call.turn.channel_id)
        if channel is not None:
            router = getattr(call.platform, "task_router", None)
            result = task_notice.notice_text(row["id"], row["title"] or row["id"], row["status"],
                row["error"] if row["status"] == "failed" else row["result_summary"], channel=channel, group=call.turn.group,
                dashboard_url=getattr(router, "_dashboard_url", ""))
            # Completion events retain the exact code notice, including evidence and
            # delivery details. Read only a notice addressed to this same chat.
            recorded = await pool.fetchval(
                "SELECT fallback FROM manager_events WHERE task_id=$1 AND manager_id=$2 "
                "AND channel_id=$3 AND target_kind=$4 AND (chat=$5 OR payload->>'chat_id'=$5) "
                "ORDER BY id DESC LIMIT 1", row["id"], call.manager_id, call.turn.channel_id,
                "group" if call.turn.group else "dm", call.turn.chat)
            for item in json.loads(recorded) if isinstance(recorded, str) else (recorded or []):
                notice = item.get("notice") if item.get("via") == "notice" else None
                if isinstance(notice, Mapping) and isinstance(notice.get("text"), str):
                    result = notice["text"]
                    break
    return {**_summary(row), "completed_at": str(row["completed_at"]) if row["completed_at"] else None,
            **dict(counts), **({"result": result} if result is not None else {})}


async def _create(call: Call) -> dict[str, Any]:
    # Reject a missing project before the shared implementation can propose creating one.
    if await db_projects.find_project_by_name(call.arguments["project"].strip()) is None:
        raise OpError(ErrorCode.NOT_FOUND, SESSION_ERRORS[ErrorCode.NOT_FOUND])
    result = await tasks._create(call, origin_session=_key(call))
    return {**result, "title": result.get("name", "")}


async def _control(call: Call) -> dict[str, Any]:
    await _owned_task(get_pool(), call.arguments["task_id"], _key(call), call.manager_id)
    await tasks._control(call)
    row = await _owned_task(get_pool(), call.arguments["task_id"], _key(call), call.manager_id)
    return {"task_id": row["id"], "status": row["status"]}


async def _instruct(call: Call) -> dict[str, Any]:
    await _owned_task(get_pool(), call.arguments["task_id"], _key(call), call.manager_id)
    await tasks._instruct(call, from_operator=True)
    return {"task_id": call.arguments["task_id"], "delivered": True}


def project(name: str, arguments: Mapping[str, Any], value: Mapping[str, Any]) -> dict[str, Any]:
    """Serialize only declared view fields, including when an internal result grows."""
    from ..session_visibility import SESSION_TOOL_RULES

    views = SESSION_TOOL_RULES[name].view
    if name == "looper_chat_tasks" and arguments.get("action") == "list":
        allowed = {f.name for f in fields(ChatTaskSummary)}
        return {"tasks": [{k: v for k, v in row.items() if k in allowed} for row in value["tasks"]]}
    view = ChatTaskDetail if name == "looper_chat_tasks" else views[0]
    allowed = {f.name for f in fields(view)}
    return {k: v for k, v in value.items() if k in allowed}


def _schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


_TEXT = {"type": "string", "minLength": 1}
_ID = {"type": "string"}  # Malformed ids follow the same not_found path as unknown ids.
TOOLS = (
    Tool("looper_chat_tasks", "List or get only tasks belonging to this chat. Details omit private configuration and progress.",
         _schema({"action": {"enum": ["list", "get"]}, "task_id": _ID,
                  "group": {"enum": [g.value for g in db_tasks.TaskGroup]},
                  "limit": {"type": "integer", "minimum": 1, "maximum": 20}}, ["action"]), Risk.READ, _read,
         task_id=lambda a: a.get("task_id")),
    Tool("looper_chat_task_create", "Create a ready task in this chat using an existing project. Only the operator can write.",
         _schema({"title": {**_TEXT, "maxLength": 200}, "requirement": {**_TEXT, "maxLength": tasks.TASK_REQUIREMENT_LIMIT},
                  "project": _TEXT}, ["title", "requirement", "project"]), Risk.WRITE, _create),
    Tool("looper_chat_task_control", "Control this chat's task. Only the operator can start, stop or restart; other actions require their own chat.",
         _schema({"task_id": _ID, "action": {"enum": ["start", "stop", "restart", "start_over", "cancel"]}},
                 ["task_id", "action"]), Risk.WRITE, _control, escalate=tasks._confirming,
         task_id=lambda a: a.get("task_id")),
    Tool("looper_chat_task_instruct", "Send instructions to this chat's running task. Only the operator can write.",
         _schema({"task_id": _ID, "text": {**_TEXT, "maxLength": tasks.TASK_REQUIREMENT_LIMIT}}, ["task_id", "text"]),
         Risk.WRITE, _instruct, task_id=lambda a: a.get("task_id")),
)
