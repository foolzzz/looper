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
                          "provider": _STR, "model": _STR, "effect": EFFECT, "problem": _NULL_STR,
                          "checking": _BOOL},
                         required=["number", "profile_id", "name", "runtime", "provider", "model", "effect", "problem"])
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
COMPLETION_TURNS = {"type": "string", "enum": [turns.value for turns in chat_settings.CompletionTurns]}
CHAT_SETTINGS = _object({"stop_enabled": _BOOL, "completion_turns": COMPLETION_TURNS,
                         "research_order": _BOOL, "outbound_enabled": _BOOL})
SCHEMAS = {"ManagerSessionEffect": EFFECT, "SettingSource": SOURCE,
           "ManagerSessionProfileSummary": PROFILE_SUMMARY, "ManagerProfileChoice": PROFILE_CHOICE,
           "ManagerSessionView": SESSION_VIEW, "ChatTurn": CHAT_TURN, "ChatSettings": CHAT_SETTINGS}
RESULTS = {}
_ID = {"type": "string", "minLength": 1}


def _op(name, description, request, result, risk="read", *, operator_only=False):
    return Operation(name, "POST", "/api/v1/" + name.replace(".", "/"), description, description,
                     request, result, risk=risk, confirms=risk == "high", operator_only=operator_only)


OUTBOUND_SETTINGS = _object({"outbound_enabled": _BOOL})

OPERATIONS = (
    _op("outbound_settings.get", "Read the operator's outbound channel control.",
        _object({}, closed=True), OUTBOUND_SETTINGS, operator_only=True),
    _op("outbound_settings.update", "Enable or disable the outbound gateway and durable Lark outbox.",
        _object({"outbound_enabled": {"type": ["boolean", "null"]}}, ["outbound_enabled"], closed=True),
        OUTBOUND_SETTINGS, "high", operator_only=True),
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
    _op("chat_settings.get", "Read the chat settings: stopping a reply, the Manager's completion turns, and its "
        "research order.",
        _object({}, closed=True), CHAT_SETTINGS),
    _op("chat_settings.update", "Change the chat settings the body names; null restores a built-in default.",
        _object({"stop_enabled": {"type": ["boolean", "null"]},
                 "completion_turns": {"type": ["string", "null"], "enum": [*COMPLETION_TURNS["enum"], None]},
                 "research_order": {"type": ["boolean", "null"]},
                 "outbound_enabled": {"type": ["boolean", "null"]}},
                [], closed=True), CHAT_SETTINGS, "high"),
)


async def impact(server, operation, arguments, principal, headers):
    if operation.operation_id not in ("chat_settings.update", "outbound_settings.update"):
        return None
    try:
        values = chat_settings.checked(arguments)
    except ValueError as exc:
        raise OpError(ErrorCode.INVALID, str(exc)) from None
    if "outbound_enabled" in values and principal.kind != "operator":
        raise OpError(ErrorCode.FORBIDDEN, "outbound_enabled requires operator identity")
    changes = []
    if "outbound_enabled" in values:
        value = values["outbound_enabled"]
        enabled = chat_settings.DEFAULT_OUTBOUND_ENABLED if value is None else value
        changes.append(f"Set channels.outbound.enabled to {str(enabled).lower()}? "
                       "Lark messages use a durable outbox, keyed retries and a shared rate limit when enabled.")
    if "stop_enabled" in values:
        value = values["stop_enabled"]
        enabled = chat_settings.DEFAULT_STOP_ENABLED if value is None else value
        changes.append(f"Set chat.stop_enabled to {str(enabled).lower()}"
                       f"{' (built-in default)' if value is None else ''}? "
                       "This immediately enables or disables stopping Manager replies through the dashboard, "
                       "Telegram and Lark. It does not stop a running reply by itself.")
    if "completion_turns" in values:
        value = values["completion_turns"]
        turns = chat_settings.DEFAULT_COMPLETION_TURNS.value if value is None else value
        changes.append(f"Set manager.completion_turns to {turns}{' (built-in default)' if value is None else ''}? "
                       + ("From the next task end, the Manager tells direct chats and the dashboard chat that a task "
                          "ended, in a read-only turn of its own, instead of the code notice; a group chat still "
                          "gets the code notice." if turns != "off" else
                          "From the next task end, every chat gets the code notice again."))
    if "research_order" in values:
        value = values["research_order"]
        enabled = chat_settings.DEFAULT_RESEARCH_ORDER if value is None else value
        changes.append(f"Set manager.research_order to {str(enabled).lower()}"
                       f"{' (built-in default)' if value is None else ''}? "
                       + ("From its next turn, the default Manager's instructions say where to look things up, in "
                          "order, before it asks the user; its process starts again to read them." if enabled else
                          "From its next turn, the default Manager's instructions no longer say where to look things "
                          "up; its process starts again."))
    return " ".join(changes)


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
    if name == "outbound_settings.get":
        return {"outbound_enabled": chat_settings.outbound_enabled()}
    if name == "outbound_settings.update":
        await chat_settings.update(arguments)
        return {"outbound_enabled": chat_settings.outbound_enabled()}
    if name == "chat_settings.get":
        return chat_settings.settings_view()
    if name == "chat_settings.update":
        if "outbound_enabled" in arguments and principal.kind != "operator":
            raise OpError(ErrorCode.FORBIDDEN, "outbound_enabled requires operator identity")
        try:
            return await chat_settings.update(arguments)
        except ValueError as exc:
            raise OpError(ErrorCode.INVALID, str(exc)) from None
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
