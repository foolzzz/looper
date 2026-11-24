"""Operator-only intent actions, types and configuration for the dashboard."""

from datetime import datetime, timezone

from ...db.intents import IntentRefused
from .. import intents as service
from .. import intent_recognition
from ..api_v1 import Operation, _array, _object, _STR, _NULL_STR, _INT, _BOOL
from ..leads_model import Confidence, IntentPriority, IntentState, parse_intent_id
from ..ops import ErrorCode, OpError
from ..ops.service import Platform, _plain
from ..projects import RequestError


def _enum(values):
    return {"type": "string", "enum": [str(value) for value in values]}


def _nullable(schema):
    return {"anyOf": [schema, {"type": "null"}]}


_PROJECT = _object({"id": _STR, "name": _STR}, closed=True)
_WORKFLOW = _object({"key": _STR, "name": _STR}, closed=True)
_SOURCE = _object({**dict.fromkeys(("chat", "chat_id", "chat_kind", "sender", "sender_id", "message_id"), _STR),
                   "sent_at": _NULL_STR, "link": _NULL_STR}, closed=True)
INTENT = _object({
    "id": _STR, "type": _object({"key": _STR, "name": _STR}, closed=True), "type_version": _INT,
    "title": _STR, "summary": _STR, "confidence": _enum(Confidence), "state": _enum(IntentState),
    "flagged": _BOOL, "chat": _object({"chat_id": _STR, "name": _STR, "kind": _STR}, closed=True),
    "project": _nullable(_PROJECT), "workflow": _nullable(_WORKFLOW),
    "effective_workflow": _nullable(_object({"key": _STR, "name": _STR,
        "source": _enum(("intent", "type_project", "type", "project"))}, closed=True)),
    "candidates": _array(_PROJECT), "merged_into": _NULL_STR,
    "task": _nullable(_object({"id": _STR, "title": _STR, "status": _STR}, closed=True)),
    "sources": {**_array(_SOURCE), "maxItems": 3}, "sources_total": _INT,
    "push_state": _enum(("none", "pending", "sending", "sent", "failed", "uncertain")),
    **dict.fromkeys(("expires_at", "handled_at", "handled_via", "reject_reason"), _NULL_STR),
    "created_at": _STR}, closed=True)
TYPE = _object({"key": _STR, "builtin": _BOOL, "enabled": _BOOL, "archived": _BOOL, "version": _INT,
    "name": _STR, "guidance": _STR, "priority": _enum(IntentPriority), "default_workflow": _NULL_STR,
    "recommended": _BOOL, "projects": _array(_object({"project_id": _STR, "project_name": _STR,
                                                    "workflow_key": _STR}, closed=True))}, closed=True)
_VERSION = _object({"version": _INT, "name": _STR, "guidance": _STR, "priority": _enum(IntentPriority),
    "default_workflow": _NULL_STR, "rolled_back_from": {"type": ["integer", "null"]},
    "edited_via": _STR, "created_at": _STR}, closed=True)
_BINDING = _object({"chat_id": _STR, "chat_name": _STR, "chat_kind": _STR, "project": _PROJECT}, closed=True)
_APP = {"app_id": _STR, "chats": {**_array(_STR), "maxItems": 3}, "messages_30d": _INT}
_SENDER = _object({**_APP, "added_at": _STR}, closed=True)
_GROUP = _object({"chat_id": _STR, "name": _STR, "kind": _STR, "added_at": _STR, "read_from": _STR,
                  "readable": _BOOL, "read_today": _INT, "skipped_today": _INT, "messages_24h": _INT,
                  "intents_7d": _INT}, closed=True)
_GROUPS = _object({"enabled": _BOOL, "delivery_enabled": _BOOL, "run_max": _INT, "day_max": _INT,
                   "max_groups": _INT, "groups": _array(_GROUP)}, closed=True)
_PREVIEW = _object({"group": _object({"chat_id": _STR, "name": _STR, "kind": _STR, "messages_24h": _INT,
    "estimate_usd": {**_array({"type": "number"}), "minItems": 2, "maxItems": 2}}, closed=True)}, closed=True)
_CHAT = {"chat_id": {"type": "string", "minLength": 1}}
_SETTINGS = _object({"enabled": _BOOL, "subscriptions": _array(_object({"schedule_id": _STR, "name": _STR,
    "enabled": _BOOL, "project_id": _STR, "target_ok": _BOOL}, closed=True))}, closed=True)
_STAT = _object({"key": _STR, "name": _STR,
    **dict.fromkeys(("recognized", "tasked", "rejected", "expired", "merged", "dropped"), _INT),
    **dict.fromkeys(("adoption_rate", "rejection_rate"), {"type": ["number", "null"]})}, closed=True)

_ID = {"intent_id": {"type": "string", "pattern": r"^(?:[Ii]-)?\d{1,18}$"}}
_KEY = {"key": {"type": "string", "pattern": "^[a-z][a-z0-9_]{0,31}$"}}
_FIELDS = {"name": {"type": "string", "minLength": 1, "maxLength": 30},
           "guidance": {"type": "string", "minLength": 1, "maxLength": 2000},
           "priority": _enum(IntentPriority), "default_workflow": _NULL_STR}
_ONE_INTENT = _object({"intent": INTENT}, closed=True)
_ONE_TYPE = _object({"type": TYPE}, closed=True)
_BINDINGS = _object({"bindings": _array(_BINDING)}, closed=True)
_SOURCE_ID = {"source_id": _STR}


_RECOGNITION_INTENT = _object({"id": _STR, "type": _object({"key": _STR, "name": _STR}, closed=True),
    "title": _STR, "state": _enum(IntentState)}, closed=True)
_RECOGNITION = _object({"id": _STR, "source_id": _STR, "chat_id": _STR, "name": _STR,
    "state": _enum(("running", "done", "failed")), "range_from": _STR, "range_to": _STR,
    "events_in_range": _INT, "events_read": _INT, "truncated": _nullable(_enum(("messages", "budget"))),
    "cost_usd": {"type": "number"}, "error": _NULL_STR, "created_at": _STR, "finished_at": _NULL_STR,
    "intents": _array(_RECOGNITION_INTENT)}, closed=True)
_RECOGNITIONS = _object({"enabled": _BOOL, "recognitions": _array(_RECOGNITION)}, closed=True)
_RECOGNITION_RANGE = {**_SOURCE_ID, **_CHAT, "since": _STR, "until": _STR}
_RECOGNITION_PREVIEW = _object({"chat": _object({"chat_id": _STR, "name": _STR}, closed=True),
    "range_from": _STR, "range_to": _STR, "effective_from": _STR, "events": _INT,
    "truncated": _nullable(_enum(("messages", "budget"))), "schedule_id": _STR,
    "estimate_usd": {**_array({"type": "number"}), "minItems": 2, "maxItems": 2}}, closed=True)


def _op(domain, action, fields, result, *, required=None, risk="read", at_least=None):
    request = _object(fields, required, closed=True)
    if at_least:
        request["anyOf"] = [{"required": [field]} for field in at_least]
    description = f"Operator only: {domain.replace('_', ' ')} {action.replace('_', ' ')}."
    return Operation(f"{domain}.{action}", "POST", f"/api/v1/{domain}/{action}", description, description,
                     request, result, risk=risk, operator_only=True)


RESULTS = {}
OPERATIONS = (
    _op("intents", "list", {"state": _array(_enum(IntentState)), "type": _STR, "project_id": _STR,
        "chat": _STR, "limit": {"type": "integer", "minimum": 1, "maximum": 100}, "before_id": _ID["intent_id"]},
        _object({"intents": _array(INTENT), "more": _BOOL}, closed=True), required=[]),
    _op("intents", "get", _ID, _object({"intent": INTENT,
        "posts": _array(_object({"kind": _enum(("push", "progress", "final")), "state": _STR,
            "created_at": _STR, "edited_at": _NULL_STR}, closed=True)),
        "merged": _array(_object({"id": _STR, "summary": _STR, "created_at": _STR}, closed=True))}, closed=True)),
    _op("intents", "update", {**_ID, "project_id": _NULL_STR, "workflow_key": _NULL_STR}, _ONE_INTENT,
        required=["intent_id"], risk="write", at_least=("project_id", "workflow_key")),
    _op("intents", "build", {**_ID, "project_id": _STR, "workflow_key": _STR},
        _object({"intent": INTENT, "task_id": _STR}, closed=True), required=["intent_id"], risk="write"),
    _op("intents", "reject", {**_ID, "reason": {"type": "string", "maxLength": 200}}, _ONE_INTENT,
        required=["intent_id"], risk="write"),
    _op("intents", "stats", {"days": {"type": "integer", "minimum": 1, "maximum": 365}},
        _object({"days": _INT, "types": _array(_STAT)}, closed=True), required=[]),
    _op("intents", "settings_get", {}, _SETTINGS),
    _op("intents", "settings_put", {"enabled": _BOOL}, _SETTINGS, risk="write"),
    _op("intent_types", "list", {}, _object({"types": _array(TYPE)}, closed=True)),
    _op("intent_types", "history", _KEY,
        _object({"key": _STR, "current": _INT, "versions": _array(_VERSION)}, closed=True)),
    _op("intent_types", "create", {**_KEY, **_FIELDS}, _ONE_TYPE,
        required=["key", "name", "guidance", "priority"], risk="write"),
    _op("intent_types", "save", {**_KEY, **_FIELDS, "enabled": _BOOL}, _ONE_TYPE,
        required=["key"], at_least=(*_FIELDS, "enabled"), risk="write"),
    _op("intent_types", "rollback", {**_KEY, "version": {"type": "integer", "minimum": 1}}, _ONE_TYPE,
        risk="write"),
    _op("intent_types", "archive", _KEY, _ONE_TYPE, risk="write"),
    _op("intent_types", "set_project_template", {**_KEY, "project_id": _STR, "workflow_key": _NULL_STR},
        _ONE_TYPE, risk="write"),
    _op("intent_bindings", "list", {**_SOURCE_ID, "query": _STR}, _BINDINGS, required=[]),
    _op("intent_bindings", "put", {**_SOURCE_ID, "chat_id": _STR, "project_id": _STR}, _BINDINGS,
        required=["chat_id", "project_id"], risk="write"),
    _op("intent_bindings", "delete", {**_SOURCE_ID, "chat_id": _STR}, _BINDINGS,
        required=["chat_id"], risk="write"),
    _op("intent_alert_senders", "list", _SOURCE_ID,
        _object({"senders": _array(_SENDER), "candidates": _array(_object(_APP, closed=True))}, closed=True), required=[]),
    _op("intent_alert_senders", "put", {**_SOURCE_ID, "app_id": _STR},
        _object({"senders": _array(_SENDER)}, closed=True), required=["app_id"], risk="write"),
    _op("intent_alert_senders", "delete", {**_SOURCE_ID, "app_id": _STR},
        _object({"senders": _array(_SENDER)}, closed=True), required=["app_id"], risk="write"),
    _op("intent_recognitions", "preview", _RECOGNITION_RANGE, _RECOGNITION_PREVIEW, required=["chat_id"]),
    _op("intent_recognitions", "start", _RECOGNITION_RANGE,
        _object({"recognition": _RECOGNITION}, closed=True), required=["chat_id"], risk="write"),
    _op("intent_recognitions", "list", {**_SOURCE_ID,
        "limit": {"type": "integer", "minimum": 1, "maximum": 50}}, _RECOGNITIONS, required=[]),
    _op("intent_recognitions", "settings_put", {**_SOURCE_ID, "enabled": _BOOL}, _RECOGNITIONS,
        required=["enabled"], risk="write"),
    _op("intent_group_reads", "list", _SOURCE_ID, _GROUPS, required=[]),
    _op("intent_group_reads", "preview", {**_SOURCE_ID, **_CHAT}, _PREVIEW, required=["chat_id"]),
    _op("intent_group_reads", "put", {**_SOURCE_ID, **_CHAT}, _GROUPS, required=["chat_id"], risk="write"),
    _op("intent_group_reads", "delete", {**_SOURCE_ID, **_CHAT}, _GROUPS, required=["chat_id"], risk="write"),
    _op("intent_group_reads", "settings_put", {**_SOURCE_ID, "enabled": _BOOL}, _GROUPS,
        required=["enabled"], risk="write"),
)


async def run(server, operation, arguments, principal, headers):
    domain, action = operation.operation_id.split(".")
    values = dict(arguments)
    try:
        if domain == "intent_recognitions":
            if action == "start":
                return _plain(await intent_recognition.service().begin(**values, requested_via="dashboard", reply=None))
            function = {"preview": "preview", "list": "list_recognitions", "settings_put": "settings_put"}[action]
            return _plain(await getattr(intent_recognition, function)(**values))
        if domain == "intents":
            if "intent_id" in values:
                values["intent_id"] = parse_intent_id(values["intent_id"])
            if "before_id" in values:
                values["before_id"] = parse_intent_id(values["before_id"])
            if action == "list":
                return _plain(await service.list_intents(**values))
            if action == "build":
                platform = Platform(bus=server._supervisor.bus, task_router=server._task_router,
                                    runtime_root=server._runtime_root)
                values.update(via="dashboard", origin=None, platform=platform, now=datetime.now(timezone.utc))
            elif action == "reject":
                values["via"] = "dashboard"
            return _plain(await getattr(service, action)(**values))
        if domain == "intent_types":
            function = {"list": "types_list", "history": "type_history", "create": "type_create",
                        "save": "type_save", "rollback": "type_rollback", "archive": "type_archive",
                        "set_project_template": "set_project_template"}[action]
        elif domain == "intent_bindings":
            function = {"list": "bindings_list", "put": "binding_put", "delete": "binding_delete"}[action]
        elif domain == "intent_group_reads":
            function = {"list": "group_reads_list", "preview": "group_read_preview", "put": "group_read_put",
                        "delete": "group_read_delete", "settings_put": "group_read_settings_put"}[action]
        else:
            function = {"list": "alert_senders_list", "put": "alert_sender_put", "delete": "alert_sender_delete"}[action]
        return _plain(await getattr(service, function)(**values))
    except service.NeedsProject as exc:
        from ..intent_routing import _needs_project
        from ..leads_model import format_intent_id

        raise OpError(ErrorCode.CONFLICT, _needs_project(format_intent_id(values["intent_id"]), exc.candidates)) from None
    except IntentRefused as exc:
        from ..intent_routing import _state_reply

        code = {"conflict": ErrorCode.CONFLICT, "not_found": ErrorCode.NOT_FOUND}.get(exc.code, ErrorCode.INVALID)
        message = _state_reply(exc.row) if exc.row else None
        raise OpError(code, message or str(exc)) from None
    except RequestError as exc:
        code = {"404 Not Found": ErrorCode.NOT_FOUND, "409 Conflict": ErrorCode.CONFLICT}.get(exc.status,
                                                                                               ErrorCode.INVALID)
        raise OpError(code, str(exc)) from None
    except ValueError as exc:
        raise OpError(ErrorCode.INVALID, str(exc)) from None
