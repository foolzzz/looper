"""The operator's switch of the Manager's session search (``manager.session_search.enabled``; assistant search RFC,
section 2): read it, and set it. Turning it on is refused while group chats share the Manager's session."""

from ..action_levels import ActionLevel
from ..api_v1 import Operation, _BOOL, _STR, _object
from ..ops import session_search

VIEW = _object({"enabled": _BOOL, "available": _BOOL, "in_effect": _BOOL, "unavailable_reason": _STR},
               ["enabled", "available", "in_effect"], closed=True)
RESULTS = {}
OPERATIONS = (
    Operation("session_search.get", "POST", "/api/v1/session_search/get",
              "Read the session search setting.",
              "Read the session search setting. It says whether the Manager's session search is stored on, whether "
              "it can take effect now (only once group chats run in a session of their own) and whether it does.",
              _object({}, closed=True), VIEW, operator_only=True,
              level=ActionLevel.INSIDE),  # reads Looper's own session search setting
    Operation("session_search.update", "POST", "/api/v1/session_search/update",
              "Set the session search setting.",
              "Set the session search setting. Null restores the default (off). Turning it on is refused "
              "(conflict) while group chats share the Manager's session.",
              _object({"enabled": {"type": ["boolean", "null"]}}, ["enabled"], closed=True), VIEW,
              risk="high", confirms=True, operator_only=True,
              level=ActionLevel.INSIDE),  # changes only Looper's own session search setting
)


async def impact(server, operation, arguments, principal, headers):
    if operation.operation_id != "session_search.update":
        return None
    value = arguments["enabled"]
    if value is True and session_search.group_sessions() is not session_search.GroupSessions.OWN:
        from ..ops import ErrorCode, OpError

        raise OpError(ErrorCode.CONFLICT, session_search.NOT_ISOLATED)
    state = "on" if value else "off"
    return (f"Turn the Manager's session search {state}{' (the default)' if value is None else ''}? "
            + ("It searches your own earlier conversations with it, in your own turns only."
               if value else "The Manager no longer searches your earlier conversations."))


async def run(server, operation, arguments, principal, headers):
    if operation.operation_id == "session_search.get":
        return await session_search.view()
    return await session_search.update(arguments["enabled"])
