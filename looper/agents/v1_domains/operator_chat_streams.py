"""Operator dashboard chat delivery and its reply and open-thread streams."""

import json

from .. import profile_registry
from ..api_v1 import Operation, _array, _object, _STR
from ..chat_targets import HTTP_409, ChatTargetError, resolve_chat_target
from ..ops import ErrorCode, OpError
from ..tiers.manager import is_new_session_command


_ID = {"type": "string", "minLength": 1}
CHAT_TARGET = {"oneOf": [
    _object({"kind": {"const": "manager"}, "id": _STR}, ["kind"], closed=True),
    _object({"kind": {"const": "planner"}, "task_id": _ID}, closed=True),
]}
SCHEMAS = {
    "OperatorChatAnswerDelta": _object({"text": _STR}, closed=True),
    "OperatorChatAnswerFinal": _object({"text": _STR,
        "response": _object({"message": _STR}, closed=True), "outcome": _STR}, closed=True),
    "OperatorChatAgentPhase": _object({"label": _STR, "activity": _STR, "tool": _STR}, ["label"], closed=True),
    "OperatorChatMessage": _object({"message_id": _STR, "role": {"const": "assistant"},
        "content": _STR, "created_at": _STR}, closed=True),
    "OperatorChatHeartbeat": _object({}, closed=True),
    # The adapter forwards other bus messages with their original payload fields.
    "OperatorChatSystemEvent": _object({"type": _STR}),
}
_EVENTS = {kind: {"data": {"$ref": "#/components/schemas/OperatorChat" + name}} for kind, name in (
    ("answer.delta", "AnswerDelta"), ("answer.final", "AnswerFinal"), ("agent.phase", "AgentPhase"),
    ("chat.message", "Message"), ("heartbeat", "Heartbeat"), ("system.event", "SystemEvent"))}
RESULTS = {}


def _stream(action, fields, required, description, kinds):
    return Operation("operator_chat." + action, "POST", "/api/v1/operator_chat/" + action,
        description, description, _object(fields, required, closed=True), {}, risk="read", operator_only=True,
        stream=True, stream_description=description + " Named events carry their original dashboard payloads; "
        "heartbeat carries {}. Errors before headers use OpError. Disconnect closes the subscription.",
        stream_events={"type": "string", "x-looper-events": {kind: _EVENTS[kind] for kind in kinds}})


OPERATIONS = (
    Operation("operator_chat.send", "POST", "/api/v1/operator_chat/send", "Send a dashboard chat message.",
        "Send a dashboard chat message. Send to a Manager (default) or a running task's Planner, saving the user "
        "message once. Planner messages use the shared durable supplement dispatch. "
        "new_session_profile_id requires /new to a Manager. "
        "Returns correlation_id for reply_events; a thread owned by another Manager returns conflict with manager_id.",
        _object({"text": {"type": "string", "minLength": 1, "maxLength": 32000}, "session_id": _STR,
            "message_id": _STR, "target": CHAT_TARGET, "new_session_profile_id": _STR,
            "language": {"enum": ["zh", "en"]}, "goal_id": _STR, "attachments": _array({"type": "object"})},
            ["text"], closed=True), _object({"correlation_id": _STR}, closed=True),
        risk="write", operator_only=True),
    _stream("reply_events", {"correlation_id": _ID}, ["correlation_id"],
        "Follow one dashboard reply until its final answer or timeout; unknown correlations end immediately.",
        ("answer.delta", "answer.final", "agent.phase", "heartbeat", "system.event")),
    _stream("thread_events", {"session_id": _ID, "manager": _STR}, ["session_id"],
        "Follow saved answers and autonomous messages in an open dashboard thread, checking its Manager owner.",
        ("chat.message", "heartbeat")),
)


async def run(server, operation, arguments, principal, headers):
    text = arguments["text"].strip()
    if not text:
        raise OpError(ErrorCode.INVALID, "text is required")
    try:
        target = await resolve_chat_target(arguments.get("target"), server._task_router, server._managers)
    except ChatTargetError as exc:
        raise OpError(ErrorCode.CONFLICT if exc.status == HTTP_409 else ErrorCode.INVALID, str(exc)) from None
    profile = arguments.get("new_session_profile_id")
    if profile is not None:
        if target.agent_type != "manager" or not is_new_session_command(text):
            raise OpError(ErrorCode.INVALID, "new_session_profile_id goes only with /new to the Manager")
        try:
            await profile_registry.check_selection(profile, ["manager"])
        except profile_registry.RegistryError as exc:
            raise OpError(ErrorCode.INVALID, str(exc)) from None
    session_id = arguments.get("session_id")
    if target.agent_type == "manager" and session_id and server._managers is not None:
        owner = await server._thread_owner_for_send(session_id, target.agent_id)
        if owner != target.agent_id:
            raise await server._foreign_thread_error(owner)
    return await server._deliver_chat(text=text, target=target, session_id=session_id,
        message_id=arguments.get("message_id"), language=arguments.get("language", "zh"),
        goal_id=arguments.get("goal_id"), attachments=arguments.get("attachments"), new_session_profile_id=profile)


async def stream(server, operation, arguments, principal, headers, writer):
    correlation_id = arguments.get("correlation_id")
    if correlation_id is None:
        session_id = arguments["session_id"]
        if owner := await server._thread_owner_elsewhere(session_id, arguments.get("manager") or "manager"):
            raise await server._foreign_thread_error(owner)
    writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-store\r\n"
                 b"Connection: close\r\nX-Accel-Buffering: no\r\n\r\n")
    events = (server._web_adapter.iter_chat_events(session_id) if correlation_id is None
              else server._web_adapter.iter_sse_events(correlation_id))
    try:
        # Enter the generator before the first await so cancellation always runs its unsubscribe finally.
        async for event in events:
            payload = json.dumps(event.get("payload", {}), ensure_ascii=False)
            writer.write(f"event: {event.get('kind', 'message')}\ndata: {payload}\n\n".encode("utf-8"))
            await writer.drain()
        await writer.drain()
    except ConnectionError:
        pass
    finally:
        await events.aclose()
        if correlation_id is not None:
            server._web_adapter.close_sse_stream(correlation_id)
