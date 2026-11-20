"""Operator-only message lead searches, settings and per-chat extraction controls."""

from datetime import date, datetime, time, timedelta, timezone

from ...db import lead_admin, message_leads, message_stream
from .. import leads, leads_model as model
from ..api_v1 import Operation, _array, _object, _STR, _NULL_STR, _INT, _BOOL
from ..ops import ErrorCode, OpError
from ..ops.service import _plain
from ..subscriptions import strip_mentions

_SOURCE = _object({
    "chat": _STR, "chat_id": _STR, "chat_kind": _STR, "sender": _STR, "sender_id": _STR,
    "sent_at": _NULL_STR, "message_id": _STR, "link": _NULL_STR}, closed=True)
_LEAD = _object({
    "id": _STR, "kind": _STR, "summary": _STR, "detail": _STR, "confidence": _STR, "state": _STR,
    "valid_until": _STR, "superseded_by": _NULL_STR, "sources": _array(_SOURCE), "sources_total": _INT}, closed=True)
_SUBSCRIPTION = _object({"schedule_id": _STR, "name": _STR, "enabled": _BOOL, "project_id": _STR}, closed=True)
_SETTINGS = _object({"enabled": _BOOL, "valid_days": _INT, "subscriptions": _array(_SUBSCRIPTION)}, closed=True)
_EXTRACTION = {"type": "string", "enum": ["included", "excluded"]}
_CHAT = _object({
    "chat_id": _STR, "name": _STR, "kind": _STR, "extraction": _EXTRACTION,
    "extraction_excluded_at": _NULL_STR, "last_message_at": _NULL_STR, "leads": _INT, "open_intents": _INT}, closed=True)
_SET_CHAT = _object({"chat_id": _STR, "extraction": _EXTRACTION,
                     "deleted_leads": _INT, "deleted_intents": _INT, "detached_intents": _INT},
                    ["chat_id", "extraction", "deleted_leads", "deleted_intents"], closed=True)
_LEAD_ID = {"type": "string", "minLength": 1}

RESULTS = {}
OPERATIONS = (
    Operation("leads.search", "POST", "/api/v1/leads/search",
              "Search sourced, unconfirmed message leads.",
              "Search sourced, unconfirmed message leads. Match every query word, with chat, person, time, kind and state filters. "
              "Read-only; the default state is active. Leads must be enabled.",
              _object({"source_id": _STR, "query": {"type": "string", "maxLength": 200},
                       "chat": _STR, "person": _STR, "since": _STR,
                       "days": {"type": "integer", "minimum": 1, "maximum": 3650},
                       "kind": {"type": "string", "enum": [kind.value for kind in model.LeadKind]},
                       "state": {"type": "string", "enum": list(model.SEARCH_STATES)},
                       "limit": {"type": "integer", "minimum": 1, "maximum": 50}}, [], closed=True),
              _object({"notice": _STR, "leads": _array(_LEAD), "more": _INT}, closed=True),
              risk="read", operator_only=True),
    Operation("leads.mark", "POST", "/api/v1/leads/mark",
              "Mark a message lead inaccurate.",
              "Mark a message lead inaccurate. Marking it again replaces its note; promoted leads conflict.",
              _object({"lead_id": _LEAD_ID, "note": {"type": "string", "maxLength": 500}}, ["lead_id"], closed=True),
              _object({"lead": _LEAD}, closed=True), risk="write", operator_only=True, audit_operator=False),
    Operation("leads.delete", "POST", "/api/v1/leads/delete",
              "Delete a message lead and its references.",
              "Delete a message lead and its references. Source messages remain; allowed while leads are off.",
              _object({"lead_id": _LEAD_ID}, closed=True), _object({"deleted": _STR}, closed=True),
              risk="write", operator_only=True),
    Operation("leads.settings_get", "POST", "/api/v1/leads/settings_get",
              "Read message lead settings and subscriptions.",
              "Read message lead settings and subscriptions. Includes validity in days, even while leads are off.",
              _object({}, closed=True), _SETTINGS, risk="read", operator_only=True),
    Operation("leads.settings_put", "POST", "/api/v1/leads/settings_put",
              "Change message lead settings.",
              "Change message lead settings. Update the enabled switch or validity in days. Turning leads off disables leads subscriptions; "
              "turning them on leaves subscriptions disabled.",
              _object({"enabled": _BOOL, "valid_days": {"type": "integer", "minimum": 1, "maximum": 365}},
                      [], closed=True), _SETTINGS, risk="write", operator_only=True),
    Operation("leads.chats", "POST", "/api/v1/leads/chats",
              "List collected chats and their extraction switches.",
              "List collected chats and their extraction switches. Included chats sort by newest message, with lead and pending intent counts. "
              "The query matches a literal name fragment or an exact chat id.",
              _object({"source_id": _STR, "query": _STR, "extraction": _EXTRACTION,
                       "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                       "offset": {"type": "integer", "minimum": 0}}, [], closed=True),
              _object({"source_id": _STR, "chats": _array(_CHAT), "total": _INT}, closed=True),
              risk="read", operator_only=True),
    Operation("leads.set_chat", "POST", "/api/v1/leads/set_chat",
              "Include or exclude a chat from extraction.",
              "Include or exclude a chat from extraction. Excluding may also delete its leads and non-tasked intents; "
              "source messages and tasked intents remain. Exclusion with deletion is allowed while leads are off. "
              "Other-chat intents merged into deleted targets return to pending and count as detached_intents.",
              _object({"source_id": _STR, "chat_id": _STR, "extraction": _EXTRACTION, "delete_extracted": _BOOL},
                      ["chat_id", "extraction"], closed=True), _SET_CHAT, risk="write", operator_only=True),
)


async def _source_id(arguments):
    sources = await message_stream.list_sources()
    if not sources:
        raise OpError(ErrorCode.CONFLICT, "no message source is set up")
    if "source_id" in arguments:
        if any(row["id"] == arguments["source_id"] for row in sources):
            return arguments["source_id"]
        raise OpError(ErrorCode.NOT_FOUND, "no such message source")
    if len(sources) != 1:
        raise OpError(ErrorCode.INVALID, "name a source_id: " + ", ".join(row["id"] for row in sources))
    return sources[0]["id"]


def _since(arguments):
    if "since" in arguments and "days" in arguments:
        raise ValueError("since and days cannot be used together")
    if "days" in arguments:
        return datetime.now(timezone.utc) - timedelta(days=arguments["days"])
    if "since" not in arguments:
        return None
    day = date.fromisoformat(arguments["since"])
    if day.isoformat() != arguments["since"]:
        raise ValueError("since must be YYYY-MM-DD")
    return datetime.combine(day, time()).astimezone()


async def _settings():
    return {"enabled": await model.leads_enabled(), "valid_days": await model.valid_days(),
            "subscriptions": await lead_admin.leads_subscriptions()}


async def _mark(arguments):
    number = model.parse_lead_id(arguments["lead_id"])
    async with message_stream.transaction() as conn:
        if not await lead_admin.mark_inaccurate(number, via="dashboard", note=arguments.get("note"), db=conn):
            raise OpError(ErrorCode.NOT_FOUND, "no such lead")
        row = await conn.fetchrow("SELECT * FROM message_leads WHERE id = $1", number)
        sources = await message_leads.source_rows([number], db=conn)
        return {"lead": leads.lead_view(row, sources.get(number, []), now=datetime.now(timezone.utc))}


async def run(server, operation, arguments, principal, headers):
    action = operation.operation_id.rsplit(".", 1)[1]
    cleanup = action == "delete" or (action == "set_chat" and arguments["extraction"] == "excluded"
                                    and arguments.get("delete_extracted", False))
    if action not in ("settings_get", "settings_put") and not cleanup and not await model.leads_enabled():
        raise OpError(ErrorCode.CONFLICT, "message leads are not enabled")
    try:
        if action == "settings_get":
            result = await _settings()
        elif action == "settings_put":
            if not arguments:
                raise ValueError("at least one of enabled or valid_days is required")
            await lead_admin.put_settings(**arguments)
            result = await _settings()
        elif action == "search":
            since = _since(arguments)
            result = await leads.search(
                words=arguments.get("query", "").split(), chat=arguments.get("chat"), person=arguments.get("person"),
                since=since, kind=arguments.get("kind"), state=arguments.get("state", "active"),
                limit=arguments.get("limit", 10), source_id=await _source_id(arguments))
        elif action == "mark":
            result = await _mark(arguments)
        elif action == "delete":
            number = model.parse_lead_id(arguments["lead_id"])
            if not await lead_admin.delete_lead(number):
                raise OpError(ErrorCode.NOT_FOUND, "no such lead")
            result = {"deleted": model.format_lead_id(number)}
        elif action == "chats":
            source_id = await _source_id(arguments)
            rows, total = await lead_admin.chats(
                source_id, query=arguments.get("query"), extraction=arguments.get("extraction"),
                limit=arguments.get("limit", 50), offset=arguments.get("offset", 0))
            result = {"source_id": source_id, "chats": [{**row, "name": strip_mentions(row["name"])} for row in rows],
                      "total": total}
        else:
            result = await lead_admin.set_extraction(
                await _source_id(arguments), arguments["chat_id"], included=arguments["extraction"] == "included",
                delete_extracted=arguments.get("delete_extracted", False), now=datetime.now(timezone.utc))
        return _plain(result)
    except lead_admin.LeadConflictError as error:
        raise OpError(ErrorCode.CONFLICT, str(error)) from None
    except ValueError as error:
        raise OpError(ErrorCode.INVALID, str(error)) from None
    except LookupError as error:
        raise OpError(ErrorCode.NOT_FOUND, str(error)) from None
