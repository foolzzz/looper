"""Manager session profiles and operator chat controls, backed by the existing services."""

from .. import chat_settings, profile_registry
from ..api_v1 import Operation, _array, _object, _STR, _NULL_STR, _INT, _BOOL
from ..ops import ErrorCode, OpError


def _nullable(schema):
    return {**schema, "type": ["object", "null"]}


EFFECT = {"type": "string", "enum": ["current", "resumes", "new_session", "starts"]}
SOURCE = {"type": "string", "enum": ["builtin", "system", "role", "project", "role_project", "session",
                                       "profile", "task", "planner_task", "planner_step"]}
PROFILE_SUMMARY = _object({"profile_id": _STR, "name": _STR, "runtime": _STR, "provider": _STR,
                           "model": _STR, "effort": _NULL_STR})
PROFILE_CHOICE = _object({"number": _INT, "profile_id": _STR, "name": _STR, "runtime": _STR,
                          "provider": _STR, "model": _STR, "effect": EFFECT, "problem": _NULL_STR})
SESSION_VIEW = _object({
    "session": _object({"active": _BOOL, "runtime": _NULL_STR, "profile_id": _NULL_STR}),
    "selected": _nullable(_object({"profile_id": _STR, "name": _STR})),
    "effective": _object({"profile": _nullable(PROFILE_SUMMARY), "source": SOURCE}),
    "problem": _NULL_STR,
    "follow_settings": _nullable(_object({"name": _STR, "effect": EFFECT})),
    "choices": _array(PROFILE_CHOICE),
})
CHAT_TURN = _object({"message_id": _STR, "client_message_id": _NULL_STR, "channel": _STR,
                     "started_at": _STR, "state": _STR, "method": _NULL_STR,
                     "stopping": _BOOL, "can_interrupt": _BOOL})
CHAT_SETTINGS = _object({"stop_enabled": _BOOL})
SCHEMAS = {"ManagerSessionEffect": EFFECT, "SettingSource": SOURCE,
           "ManagerSessionProfileSummary": PROFILE_SUMMARY, "ManagerProfileChoice": PROFILE_CHOICE,
           "ManagerSessionView": SESSION_VIEW, "ChatTurn": CHAT_TURN, "ChatSettings": CHAT_SETTINGS}
RESULTS = {}
_ID = {"type": "string", "minLength": 1}


def _op(name, description, request, result, risk="read", *, operator_only=False):
    return Operation(name, "POST", "/api/v1/" + name.replace(".", "/"), description, description,
                     request, result, risk=risk, confirms=risk == "high", operator_only=operator_only)


OPERATIONS = (
    _op("manager_session.get", "Read a Manager's session and available Agent Profiles.",
        _object({"manager": _STR}, [], closed=True), SESSION_VIEW),
    _op("manager_session.select_profile", "Choose a Manager session's profile; null follows Settings.",
        _object({"manager": _STR, "profile_id": _NULL_STR}, ["profile_id"], closed=True),
        _object({**SESSION_VIEW["properties"], "effect": EFFECT, "text": _STR}), "write"),
    _op("operator_chat.turn", "Read the live Manager turn answering a dashboard conversation.",
        _object({"session_id": _ID}, closed=True), _object({"turn": _nullable(CHAT_TURN)}), operator_only=True),
    _op("operator_chat.stop", "Stop the Manager reply to this message.",
        _object({"message_id": _ID}, closed=True),
        _object({"state": {"type": "string", "enum": ["stopping", "already_stopping"]},
                 "phase": _NULL_STR, "method": _NULL_STR}), "write", operator_only=True),
    _op("chat_settings.get", "Read the experimental chat stop setting.",
        _object({}, closed=True), CHAT_SETTINGS),
    _op("chat_settings.update", "Change the experimental chat stop setting; null restores its built-in default.",
        _object({"stop_enabled": {"type": ["boolean", "null"]}}, closed=True), CHAT_SETTINGS, "high"),
)


async def impact(server, operation, arguments, principal, headers):
    if operation.operation_id != "chat_settings.update":
        return None
    value = arguments["stop_enabled"]
    enabled = chat_settings.DEFAULT_STOP_ENABLED if value is None else value
    return (f"Set chat.stop_enabled to {str(enabled).lower()}"
            f"{' (built-in default)' if value is None else ''}? "
            "This immediately enables or disables stopping Manager replies through the dashboard, "
            "Telegram and Lark. It does not stop a running reply by itself.")


def _manager(server, manager_id):
    agent = server._manager_for(manager_id)
    if server._managers is None and manager_id not in (None, "", "manager"):
        agent = None
    if agent is None:
        if server._managers is not None or manager_id not in (None, "", "manager"):
            raise OpError(ErrorCode.NOT_FOUND, f"no running Manager {manager_id or 'manager'!r}")
        raise OpError(ErrorCode.UNAVAILABLE, "the Manager is not running")
    return agent


def _stop_state(agent, message_id):
    current = agent.turn_for(message_id)
    phase = current.stop.phase if current is not None and current.stop is not None else None
    phase = phase if phase is not None else (current.phase if current is not None else None)
    method = current.method if current is not None else None
    return {"phase": getattr(phase, "value", phase), "method": getattr(method, "value", method)}


async def run(server, operation, arguments, principal, headers):
    name = operation.operation_id
    if name == "chat_settings.get":
        return chat_settings.settings_view()
    if name == "chat_settings.update":
        return await chat_settings.update(arguments)
    if name.startswith("manager_session."):
        agent = _manager(server, arguments.get("manager"))
        if name == "manager_session.get":
            return await agent.session_view()
        # The caller cannot supply provenance, or impersonate the dashboard or a Manager.
        origin = {"channel": "web", "chat": None, "sender": "dashboard", "group": False}
        via = "api"
        if principal.kind == "client":
            origin = {"channel": "api", "chat": None, "sender": principal.client_id, "group": False,
                      "client": {"id": principal.client_id, "name": principal.client_name}}
            via = f"client:{principal.client_name or principal.client_id}"
        try:
            result = await agent.select_session_profile(arguments["profile_id"], origin=origin, via=via,
                                                        input_text="POST /api/v1/manager_session/select_profile")
        except (ValueError, profile_registry.RegistryError) as exc:
            raise OpError(ErrorCode.INVALID, str(exc)) from None
        return {**await agent.session_view(), "effect": result.effect.value, "text": result.text}
    if name == "operator_chat.turn":
        session_id = arguments["session_id"]
        if server._managers is None:
            agent = _manager(server, None)
        else:
            from ...db import managers as db_managers

            agent = server._managers.get(await db_managers.thread_owner(session_id) or "manager")
        current = None if agent is None else agent.current_turn_for("web", session_id)
        return {"turn": None if current is None else {
            "message_id": current.message_id, "client_message_id": current.client_message_id,
            "channel": current.channel, "started_at": current.received_at.isoformat(), "state": current.phase.value,
            "method": getattr(current.method, "value", current.method), "stopping": current.stop is not None,
            "can_interrupt": agent.can_interrupt}}
    if not chat_settings.stop_enabled():
        raise OpError(ErrorCode.CONFLICT, "Stopping a reply is off (Settings → Chat; experimental).",
                      details={"state": "disabled"})
    message_id = arguments["message_id"]
    agent = server._stop_agent(message_id)
    if agent is None:
        raise OpError(ErrorCode.UNAVAILABLE, "the Manager is not running")
    result = await agent.stop_turn(message_id, via="web")
    details = {"state": result.value, **_stop_state(agent, message_id)}
    if result.value in ("stopping", "already_stopping"):
        return details
    errors = {"not_running": "No turn is running.",
              "other_message": "The running turn answers a different message.",
              "cannot_interrupt": "This runtime cannot interrupt a reply."}
    raise OpError(ErrorCode.CONFLICT, errors[result.value], details=details)
