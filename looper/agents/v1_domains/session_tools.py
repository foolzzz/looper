"""Operator session-tool controls and explicit, audited assignment of legacy tasks."""

import re

from .. import session_tools_settings
from ..action_levels import ActionLevel
from ..api_v1 import Operation, _BOOL, _STR, _object
from ..ops import ErrorCode, OpError
from ...db import manager_actions as db_actions
from ...db.connection import get_pool

SESSION_PATTERN = r"^(group|dm):channel-[a-z_]+(-[1-9][0-9]*)?:.+$"
VIEW = _object({"enabled": _BOOL}, closed=True)
ASSIGNMENT = _object({"task_id": _STR, "origin_session": _STR}, closed=True)
RESULTS = {}
OPERATIONS = (
    Operation("session_tools.get", "POST", "/api/v1/session_tools/get",
              "Read the routed chat tools setting.",
              "Read the routed chat tools setting. It is disabled by default.",
              _object({}, closed=True), VIEW, operator_only=True,
              level=ActionLevel.INSIDE),  # reads only Looper's session-tool setting
    Operation("session_tools.update", "POST", "/api/v1/session_tools/update",
              "Set the routed chat tools setting.",
              "Set the routed chat tools setting. Null restores off. Session routing must also be enabled.",
              _object({"enabled": {"type": ["boolean", "null"]}}, closed=True), VIEW,
              risk="high", confirms=True, operator_only=True,
              level=ActionLevel.INSIDE),  # changes only Looper's session-tool setting
    Operation("manager_sessions.assign_task", "POST", "/api/v1/manager_sessions/assign_task",
              "Assign an unassigned legacy task to a chat session once.",
              "Assign an unassigned legacy task to a chat session once. Acknowledge that its title and completion "
              "notice may become visible to everyone in that chat. Existing assignments cannot change.",
              _object({"task_id": {"type": "string", "minLength": 1},
                       "session": {"type": "string", "pattern": SESSION_PATTERN},
                       "acknowledge": {"type": "boolean", "const": True}}, closed=True), ASSIGNMENT,
              risk="high", confirms=True, operator_only=True, audit_operator=False,
              level=ActionLevel.INSIDE),  # changes task ownership within Looper, with an atomic audit
)


async def impact(server, operation, arguments, principal, headers):
    if operation.operation_id == "session_tools.update":
        return ("Set manager.session_tools.enabled to " + str(bool(arguments["enabled"])).lower() +
                "? Routed chats gain their own task reads and operator writes; other senders' writes are refused.")
    if operation.operation_id == "manager_sessions.assign_task":
        return "Expose this legacy task's title and completion notice to the selected chat? This cannot be undone."
    return None


async def run(server, operation, arguments, principal, headers):
    if principal.kind != "operator":
        raise OpError(ErrorCode.FORBIDDEN, "this control requires operator identity")
    if operation.operation_id == "session_tools.get":
        return session_tools_settings.view()
    if operation.operation_id == "session_tools.update":
        return await session_tools_settings.update(arguments["enabled"])
    session = arguments["session"]
    if arguments.get("acknowledge") is not True or re.fullmatch(SESSION_PATTERN, session) is None:
        raise OpError(ErrorCode.INVALID, "acknowledge must be true and session must name a group or direct chat")
    async with get_pool().acquire() as conn, conn.transaction():
        if not await conn.fetchval("SELECT EXISTS(SELECT 1 FROM manager_sessions "
                                   "WHERE manager_id='manager' AND session_key=$1)", session):
            raise OpError(ErrorCode.NOT_FOUND, "no such Manager session")
        task = await conn.fetchrow("UPDATE tasks SET origin_session=$2,updated_at=now() "
                                  "WHERE id=$1 AND manager_id='manager' AND origin_session IS NULL "
                                  "RETURNING id", arguments["task_id"], session)
        if task is None:
            raise OpError(ErrorCode.CONFLICT, "task is missing, belongs to another Manager, or is already assigned")
        result = {"task_id": task["id"], "origin_session": session}
        await db_actions.record(channel="api", chat=None, message_id=None, tool=operation.operation_id,
                                arguments=arguments, risk="high", status="ok", result=result,
                                task_id=task["id"], manager_id="manager", pool=conn,
                                sender_kind="operator")
        return result
