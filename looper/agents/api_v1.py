"""``/api/v1``: the one API local agents and scripts use (RFC ``looper-client-skill`` §5.2).

This module holds what every answer shares: who is calling (``Principal``), the typed errors and their HTTP status,
and ``whoami``; and the operations: the ops tools of an API client, one operation per tool action, and the few
declared by hand, with the OpenAPI document generated from them (RFC §5.1–§5.3).
"""

from __future__ import annotations

import copy
import functools
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import looper

from .ops import ErrorCode, Tool

API_VERSION = 1

CHAT_STREAM_SECONDS = 1800  # how long a chat.send stream waits for the Manager's answer
CONVERSATION_PREFIX = "cli-"  # the id of a conversation an API client opened

OK = "200 OK"
ACCEPTED = "202 Accepted"

STATUS: dict[ErrorCode, str] = {
    ErrorCode.INVALID: "400 Bad Request",
    ErrorCode.UNAUTHORIZED: "401 Unauthorized",
    ErrorCode.FORBIDDEN: "403 Forbidden",
    ErrorCode.NOT_FOUND: "404 Not Found",
    ErrorCode.CONFLICT: "409 Conflict",
    ErrorCode.EXPIRED: "410 Gone",
    ErrorCode.RATE_LIMITED: "429 Too Many Requests",
    ErrorCode.UNAVAILABLE: "503 Service Unavailable",
}
_DEFAULT_STATUS = "400 Bad Request"


@dataclass(frozen=True)
class Principal:
    """Who calls: the operator (their token), or an API client (``lpc_`` token)."""

    kind: Literal["client", "operator"]
    client_id: str | None = None
    client_name: str | None = None

    @property
    def key(self) -> str:
        return f"client:{self.client_id}" if self.kind == "client" else "operator"


OPERATOR = Principal("operator")


def error(code: ErrorCode, message: str, *, status: str | None = None) -> tuple[str, dict[str, Any]]:
    return status or STATUS.get(code, _DEFAULT_STATUS), {"status": "error", "error": {"code": code.value, "message": message}}


def answer_status(answer: Mapping[str, Any]) -> str:
    """The HTTP status of an operation's answer (``ok``, ``needs_confirmation`` or ``error``)."""
    status = answer.get("status")
    if status == "ok":
        return OK
    if status == "needs_confirmation":
        return ACCEPTED
    return STATUS.get(ErrorCode(answer["error"]["code"]), _DEFAULT_STATUS)


def whoami(principal: Principal) -> dict[str, Any]:
    client = ({"id": principal.client_id, "name": principal.client_name} if principal.kind == "client" else None)
    return {"status": "ok", "result": {
        "principal": principal.kind, "client": client, "looper_version": looper.__version__,
        "api_version": API_VERSION}}


# --- the operations ---

PREFIX = "/api/v1"
SESSION_PATTERN = "^[A-Za-z0-9._:-]{1,64}$"
UNTYPED: dict[str, Any] = {"type": "object", "x-looper-result-typed": False}
_CONFIRMATION_NOTE = " Some calls answer needs_confirmation first; POST /api/v1/confirm runs them."


@dataclass(frozen=True)
class Operation:
    operation_id: str  # "tasks.list", "task_create", "chat.send"
    method: str  # "POST" or "GET"
    path: str  # "/api/v1/ops/tasks/list"
    summary: str  # the description's first sentence, at most 120 characters
    description: str
    request: Mapping[str, Any] | None  # JSON Schema of the body; None for GET
    result: Mapping[str, Any]  # JSON Schema of OpOk.result
    tool: str | None = None
    action: str | None = None
    risk: str | None = None  # "read" | "write" | "high"
    hand: bool = False  # loopercli has a hand-written command for it (x-looper-cli-hand)
    stream: bool = False  # answers text/event-stream
    confirms: bool = False  # a call may answer needs_confirmation (a write, or a tool that escalates)
    operator_only: bool = False  # credential management never accepts an API client


def _summary(description: str) -> str:
    first = description.split(". ", 1)[0].strip()
    return first if len(first) <= 120 else first[:119].rstrip() + "…"


_STR: dict[str, Any] = {"type": "string"}
_NULL_STR: dict[str, Any] = {"type": ["string", "null"]}
_INT: dict[str, Any] = {"type": "integer"}
_BOOL: dict[str, Any] = {"type": "boolean"}
_OBJ: dict[str, Any] = {"type": "object"}
_NULL_OBJ: dict[str, Any] = {"type": ["object", "null"]}


def _array(items: Mapping[str, Any] = _OBJ) -> dict[str, Any]:
    return {"type": "array", "items": dict(items)}


def _object(properties: Mapping[str, Any], required: Sequence[str] | None = None, *,
            closed: bool = False) -> dict[str, Any]:
    """An object schema: ``required`` defaults to every property; extra keys are allowed unless ``closed``."""
    schema: dict[str, Any] = {"type": "object", "properties": dict(properties),
                              "additionalProperties": not closed}
    names = list(properties) if required is None else list(required)
    if names:
        schema["required"] = names
    return schema


_LISTED = {"task_id": _STR, "name": _STR, "status": _STR, "group": _STR, "project": _NULL_STR,
           "created_at": _NULL_STR}
_TASK_ID_ONLY = {"task_id": _STR}
_GATE_ANSWER = {"task_id": _STR, "gate": _OBJ, "message": _OBJ, "delivered": _STR}
_PLAN = _object({
    "applied": _BOOL, "counts": _OBJ,
    "changes": _array(_object({"section": _STR, "key": _STR, "action": _STR,
                               "fields": _array(_STR), "reason": _STR, "code": _STR},
                              ["section", "key", "action"])),
    "need_secret": _array(_object({"section": _STR, "key": _STR, "names": _array(_STR),
                                   "target": _object({"kind": _STR, "id": _STR})},
                                  ["section", "key", "names"])),
    "notes": _array(_STR)})

RESULTS: dict[str, dict[str, Any]] = {
    "auth.set_password": _object({"user": _STR, "password_set": _BOOL}),
    "auth.clear_password": _object({"removed": _BOOL}),
    "system.check": _object({"checks": _array(_object({
        "id": _STR, "label": _STR, "status": {"type": "string", "enum": ["ok", "warn", "fail", "skip"]},
        "detail": _STR, "fix": _STR}))}),
    "config.export": _object({"document": _object({
        "format": _STR, "looper_version": _STR, "exported_at": _STR, "secrets": _STR,
        "runtimes": _array(), "providers": _array(), "models": _array(), "profiles": _array(),
        "settings": _OBJ, "channels": _array(), "catalog": _array(), "installs": _array(),
        "projects": _array()}, ["format"])}),
    "config.import_preview": _object({"import_id": _STR, "bytes": _INT, "file": _OBJ,
                                      "expires_at": _STR, "plan": _PLAN}),
    "config.import_apply": _object({"import_id": _STR, "plan": _PLAN}),
    "config.import_cancel": _object({"deleted": _STR}),
    "config_export": _object({"path": _STR, "format": _STR, "bytes": _INT, "counts": _OBJ,
                               "secrets_left_out": _INT, "path_documents_to_repoint": _INT, "note": _STR}),
    "tasks.list": _object({"tasks": _array(_object(_LISTED)), "more": _BOOL}),
    "tasks.get": _object({
        **_LISTED, "phase": _NULL_STR, "work_dir": _NULL_STR, "requirement": _NULL_STR, "steps": _array(),
        "waits": _array(), "roles": _array(), "supplements": _array(), "latest_progress": _NULL_STR,
        "result": _NULL_STR, "cost_usd": {"type": ["number", "null"]}, "concurrency": _OBJ, "gates": _array(),
    }, [*_LISTED, "phase", "work_dir", "requirement", "steps", "waits", "supplements", "latest_progress", "result",
        "cost_usd", "concurrency"]),
    "task_pipeline": _object({
        **_TASK_ID_ONLY, "name": _STR, "status": _STR, "group": _STR, "phase": _NULL_STR, "steps": _array(),
        "gates": _array(), "waits": _array()}),
    "task_create": _object({
        **_TASK_ID_ONLY, "name": _STR, "status": _STR, "project": _STR, "project_created": _BOOL, "work_dir": _STR,
        "workspace_mode": _STR}),
    "task_control.start": _object({**_TASK_ID_ONLY, "started": _BOOL}),
    "task_control.stop": _object({**_TASK_ID_ONLY, "stopped": _BOOL}),
    "task_control.restart": _object({**_TASK_ID_ONLY, "restarted": _BOOL, "note": _STR}),
    "task_control.start_over": _object({**_TASK_ID_ONLY, "started_over": _BOOL, "note": _STR}),
    "task_control.cancel": _object({**_TASK_ID_ONLY, "cancelled": _BOOL}),
    "task_update": _object({
        **_TASK_ID_ONLY, "title": _NULL_STR, "requirement_version": {"type": ["integer", "null"]},
        "changed": _array(_STR), "profiles_note": _STR, "requirement_note": _STR}, ["task_id", "changed"]),
    "task_instruct": _object({**_TASK_ID_ONLY, "supplement_id": {"type": ["integer", "string"]}, "note": _STR}),
    "gates.list": _object({"gates": _array(), "total": _INT, "counts": _OBJ, "limit": _INT, "offset": _INT}),
    "gates.messages": _object({**_TASK_ID_ONLY, "gate": _OBJ, "messages": _array()}),
    "gates.reply": _object(_GATE_ANSWER),
    "gates.end": _object(_GATE_ANSWER),
    "agent_profiles.list": _object({
        "profiles": _array(), "runtimes": _array(), "runtime_types": _array(), "models": _array()}),
    "overview": _object({
        "tasks": _OBJ, "running_tasks": _array(), "step_runs": _array(), "projects": _array(),
        "waiting_gates": _INT, "spend_today": _OBJ, "unhealthy": _array()}),
    # The hand operations (the chat ones are card 5's to serve).
    "chat.send": {"type": "string"},
    "chat.messages": _object({
        "conversation_id": _STR, "manager_id": _STR,
        "messages": _array(_object({"message_id": _STR, "role": _STR, "content": _STR, "created_at": _STR})),
        "next_cursor": _NULL_STR, "has_more": _BOOL}),
    "chat.conversations": _object({"conversations": _array(_object({
        "conversation_id": _STR, "manager_id": _STR, "created_at": _STR, "last_message_at": _NULL_STR,
        "last_content": _NULL_STR}))}),
    "confirm": {"type": "object", "description": "The result of the operation that was confirmed.",
                "additionalProperties": True},
    "openapi": {"type": "object", "description": "The OpenAPI document itself, not wrapped in OpOk."},
    "whoami": _object({
        "principal": {"type": "string", "enum": ["client", "operator"]},
        "client": _object({"id": _STR, "name": _STR}) | {"type": ["object", "null"]},
        "looper_version": _STR, "api_version": _INT}),
}


def tool_operations(tools: Sequence[Tool]) -> list[Operation]:
    """One operation per action of a tool that has an ``action``, otherwise one per tool."""
    operations: list[Operation] = []
    for tool in tools:
        base = tool.name.removeprefix("looper_")
        description = tool.description + (_CONFIRMATION_NOTE if tool.escalate is not None else "")
        actions = (tool.schema["properties"]["action"].get("enum")
                   if "action" in (tool.schema.get("required") or ()) else None)
        if not actions:
            operations.append(Operation(
                operation_id=base, method="POST", path=f"{PREFIX}/ops/{base}", summary=_summary(description),
                description=description, request=copy.deepcopy(dict(tool.schema)),
                result=RESULTS.get(base, UNTYPED), tool=tool.name, risk=tool.risk.value,
                confirms=tool.risk.value != "read" or tool.escalate is not None))
            continue
        for action in actions:
            request = copy.deepcopy(dict(tool.schema))
            request["properties"].pop("action", None)
            required = [name for name in request.get("required") or () if name != "action"]
            if required:
                request["required"] = required
            else:
                request.pop("required", None)
            risk = "read" if action in tool.read_actions else tool.risk.value
            operation_id = f"{base}.{action}"
            operations.append(Operation(
                operation_id=operation_id, method="POST", path=f"{PREFIX}/ops/{base}/{action}",
                summary=_summary(description), description=description, request=request,
                result=RESULTS.get(operation_id, UNTYPED), tool=tool.name, action=action, risk=risk,
                confirms=risk != "read" or tool.escalate is not None))
    return operations


def _hand(operation_id: str, method: str, path: str, description: str, request: Mapping[str, Any] | None, *,
          stream: bool = False, hand: bool = True, operator_only: bool = False) -> Operation:
    return Operation(operation_id=operation_id, method=method, path=f"{PREFIX}{path}",
                     summary=_summary(description), description=description, request=request,
                     result=RESULTS.get(operation_id, UNTYPED), hand=hand, stream=stream, operator_only=operator_only)


HAND_OPERATIONS: tuple[Operation, ...] = (
    _hand("chat.send", "POST", "/chat/send",
          "Say something to the Manager and read its answer as it comes. The answer is a stream of server-sent "
          "events; the conversation is the client's own.",
          {"type": "object", "additionalProperties": False, "required": ["text"], "properties": {
              "text": {"type": "string", "minLength": 1, "maxLength": 32_000},
              "conversation_id": _STR, "manager_id": _STR, "language": {"type": "string", "enum": ["zh", "en"]}}},
          stream=True),
    _hand("chat.messages", "POST", "/chat/messages",
          "Read a conversation of this client after a cursor, oldest first.",
          {"type": "object", "additionalProperties": False, "required": ["conversation_id"], "properties": {
              "conversation_id": _STR, "after": {"type": "string", "minLength": 1},
              "limit": {"type": "integer", "minimum": 1, "maximum": 200}}}),
    _hand("chat.conversations", "POST", "/chat/conversations",
          "List this client's conversations, the most recently active first.",
          {"type": "object", "additionalProperties": False, "properties": {}}),
    _hand("confirm", "POST", "/confirm",
          "Run the operation this client was asked to confirm in this session.",
          {"type": "object", "additionalProperties": False, "required": ["confirmation_id"],
           "properties": {"confirmation_id": {"type": "string", "minLength": 1}}}),
    _hand("openapi", "GET", "/openapi.json", "This document.", None),
)
WHOAMI = _hand("whoami", "GET", "/whoami", "Who the token calls as, and the Looper version.", None, hand=False)

ONLINE_OPERATIONS: tuple[Operation, ...] = (
    _hand("auth.set_password", "POST", "/auth/set_password",
          "Set the operator password and end existing password sessions. Operator identity only.",
          _object({"user": {"type": "string", "pattern": "^[A-Za-z0-9._-]{1,64}$"},
                   "password": {"type": "string", "minLength": 8, "writeOnly": True}}, ["password"], closed=True),
          hand=False, operator_only=True),
    _hand("auth.clear_password", "POST", "/auth/clear_password",
          "Clear the operator password and end existing password sessions. Operator identity only.",
          _object({}, closed=True), hand=False, operator_only=True),
    _hand("system.check", "POST", "/system/check", "Run the doctor's self checks, including pg_dump.",
          _object({}, closed=True), hand=False),
    _hand("config.export", "POST", "/config/export", "Export the configuration as a portable document.",
          _object({"include_projects": _BOOL, "include_secrets": _BOOL}, [], closed=True), hand=False),
    _hand("config.import_preview", "POST", "/config/import_preview",
          "Stage a configuration document and preview its import without writing configuration.",
          _object({"document": _OBJ}, closed=True), hand=False),
    _hand("config.import_cancel", "POST", "/config/import_cancel",
          "Cancel this caller's staged configuration import and remove its file.",
          _object({"import_id": {"type": "string", "minLength": 1}}, closed=True), hand=False),
    Operation("config.import_apply", "POST", f"{PREFIX}/config/import_apply", "Apply a staged configuration import.",
              "Apply a staged configuration import. Requires confirmation in this client and session.",
              _object({"import_id": {"type": "string", "minLength": 1}}, closed=True),
              RESULTS["config.import_apply"], risk="high", confirms=True),
)


def operations(tools: Sequence[Tool] | None = None) -> list[Operation]:
    if tools is None:
        from .ops.clients import CLIENT_TOOLS

        tools = CLIENT_TOOLS
    return [*tool_operations(tools), *HAND_OPERATIONS, WHOAMI, *ONLINE_OPERATIONS]


_STREAM_EVENTS = ("Events, in order: accepted {conversation_id, message_id, manager_id}; delta {text} and status "
                  "{label}, any number; final {text, outcome}; heartbeat every 15 seconds without an event; timeout "
                  "{message} when the stream ran out of time (the reply is still written to the conversation).")


def _responses(operation: Operation) -> dict[str, Any]:
    ok: dict[str, Any]
    if operation.operation_id == "openapi":
        ok = {"description": "The OpenAPI document.", "content": {"application/json": {"schema": _OBJ}}}
    elif operation.stream:
        ok = {"description": _STREAM_EVENTS, "content": {"text/event-stream": {"schema": {"type": "string"}}}}
    else:
        ok = {"description": "ok", "content": {"application/json": {"schema": {"allOf": [
            {"$ref": "#/components/schemas/OpOk"}, {"type": "object", "properties": {"result": dict(operation.result)}}]}}}}
    responses: dict[str, Any] = {"200": ok}
    if operation.confirms:
        responses["202"] = {"description": "needs_confirmation: POST /confirm with the confirmation_id",
                            "content": {"application/json": {"schema": {
                                "$ref": "#/components/schemas/OpNeedsConfirmation"}}}}
    responses["default"] = {"description": "error", "content": {"application/json": {"schema": {
        "$ref": "#/components/schemas/OpError"}}}}
    return responses


def _path_item(operation: Operation) -> dict[str, Any]:
    item: dict[str, Any] = {"operationId": operation.operation_id, "summary": operation.summary,
                            "description": operation.description}
    for key, value in (("x-looper-tool", operation.tool), ("x-looper-action", operation.action),
                       ("x-looper-risk", operation.risk)):
        if value:
            item[key] = value
    if operation.hand:
        item["x-looper-cli-hand"] = True
    if operation.operator_only:
        item["x-looper-operator-only"] = True
    if operation.method == "POST":
        item["parameters"] = [{"name": "X-Looper-Session", "in": "header", "required": False,
                               "description": "Confirmations belong to this session of the client (default: "
                                              "'default').",
                               "schema": {"type": "string", "pattern": SESSION_PATTERN}}]
    if operation.request is not None:
        item["requestBody"] = {"required": True, "content": {"application/json": {
            "schema": copy.deepcopy(dict(operation.request))}}}
    item["responses"] = _responses(operation)
    return {operation.method.lower(): item}


def document(tools: Sequence[Tool] | None = None) -> dict[str, Any]:
    """The OpenAPI 3.1 document of the operations (``tools``: the client catalog by default)."""
    if tools is None:
        return _default_document()
    return _document(operations(tools))


@functools.cache
def _default_document() -> dict[str, Any]:
    return _document(operations())


def _document(ops: Sequence[Operation]) -> dict[str, Any]:
    paths: dict[str, Any] = {}
    for operation in ops:
        paths.setdefault(operation.path.removeprefix(PREFIX), {}).update(_path_item(operation))
    return {
        "openapi": "3.1.0",
        "info": {"title": "Looper API", "version": looper.__version__, "x-looper-api": API_VERSION,
                 "description": "The operations of /api/v1 only: the ops tools an API client may use, the chat, "
                                "confirm and whoami. The dashboard's /api routes are not described yet."},
        "servers": [{"url": PREFIX}],
        "security": [{"bearer": []}],
        "paths": paths,
        "components": {
            "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}},
            "schemas": {
                "ErrorCode": {"type": "string", "enum": [code.value for code in ErrorCode]},
                "OpError": _object({"status": {"const": "error"}, "error": _object({
                    "code": {"$ref": "#/components/schemas/ErrorCode"}, "message": _STR})}),
                "OpOk": _object({"status": {"const": "ok"}, "result": _OBJ, "notes": _array(_STR)},
                                ["status", "result"]),
                "OpNeedsConfirmation": _object({
                    "status": {"const": "needs_confirmation"}, "confirmation_id": _STR,
                    "expires_in_seconds": _INT, "impact": _STR}),
            },
        },
    }


@functools.cache
def _index() -> dict[tuple[str, str], Operation]:
    return {(op.method, op.path): op for op in operations()}


def find(method: str, path: str) -> Operation | None:
    """The operation for a request: exact path and method."""
    return _index().get((method, path))


def allowed_method(path: str) -> str | None:
    """The method an operation at ``path`` has, for the 405 of a request with another."""
    return next((m for (m, p) in _index() if p == path), None)
