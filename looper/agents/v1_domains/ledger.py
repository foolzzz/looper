"""The ledger's operations (completion-ledger RFC, sections 4.8 and 4.9).

``ledger.list`` and ``ledger.get`` are read-only and open to an API client such as ``loopercli``; with the ledger off
they answer ``conflict`` (the dashboard's 409). The rest belong to the operator's dashboard: ``ledger.status`` (the
switch, the sidebar's number), ``ledger.add`` and ``ledger.act`` (written as the user, ``via dashboard``) and
``ledger.configure`` (the switch).
"""

from datetime import datetime

from ...db import ledger as store
from ..api_v1 import Operation, _array, _object, _STR, _NULL_STR, _INT, _BOOL
from ..ops import ErrorCode, OpError
from ..ops.service import _plain

_ITEM = _object({
    "id": _STR, "number": _INT, "kind": {"type": "string", "enum": list(store.KINDS)},
    "status": {"type": ["string", "null"], "enum": [*store.STATUSES, None]}, "title": _STR, "detail": _STR,
    "assurance": {"type": "string", "enum": list(store.ACTOR_KINDS)},
    "source": {"type": ["object", "null"]}, "owner": _STR, "project_id": _NULL_STR, "due_at": _NULL_STR,
    "overdue": _BOOL, "refs": _array(), "superseded_by": _NULL_STR, "added_via": _NULL_STR,
    "created_at": _STR, "updated_at": _STR, "closed_at": _NULL_STR}, closed=True)
_EVENT = _object({
    "id": _INT, "action": {"type": "string", "enum": list(store.ACTIONS)},
    "actor_kind": {"type": "string", "enum": list(store.ACTOR_KINDS)}, "actor_id": _NULL_STR,
    "relayed_by": _NULL_STR, "via": {"type": "string", "enum": list(store.VIA)}, "session_label": _NULL_STR,
    "quote": _NULL_STR, "note": _NULL_STR, "changes": {"type": ["object", "null"]}, "created_at": _STR}, closed=True)
_COUNTS = _object({status: _INT for status in store.STATUSES}, closed=True)
LIST = _object({"items": _array(_ITEM), "more": _INT, "counts": _COUNTS}, closed=True)
GET = _object({"item": _ITEM, "events": _array(_EVENT)}, closed=True)

STATUS = _object({"enabled": _BOOL, "needs_you": _INT, "counts": _COUNTS, "operator_unset": _BOOL}, closed=True)
_FIELDS = _object({"title": _STR, "detail": _STR, "due": _NULL_STR, "owner": _STR, "project_id": _NULL_STR},
                  [], closed=True)
ADD = _object({"kind": {"type": "string", "enum": list(store.KINDS)}, "title": {"type": "string", "minLength": 1,
               "maxLength": 200}, "detail": {"type": "string", "maxLength": 4000}, "due": _NULL_STR,
               "project_id": _NULL_STR}, ["kind", "title"], closed=True)
ACT = _object({"item": _STR, "action": {"type": "string", "enum": ["confirm", "close", "cancel", "reopen", "note",
                                                                     "amend"]},
               "note": {"type": "string", "maxLength": 2000}, "fields": _FIELDS}, ["item", "action"], closed=True)

RESULTS = {}
OPERATIONS = (
    Operation("ledger.list", "POST", "/api/v1/ledger/list",
              "List ledger items: what waits for the user, what was decided, who does what.",
              "List ledger items: what waits for the user, what was decided, who does what. Newest first, with how "
              "many more match and the count of every status. Read-only. "
              "status defaults to open (all, done, cancelled, expired, superseded); a sourced item's status is its "
              "source's. Answers conflict while the ledger is off.",
              _object({"status": {"type": "string", "enum": ["all", *store.STATUSES]},
                       "kind": {"type": "string", "enum": list(store.KINDS)}, "owner": _STR, "project_id": _STR,
                       "needs_you": _BOOL,
                       "limit": {"type": "integer", "minimum": 1, "maximum": store.LIST_MAX}}, [], closed=True),
              LIST, risk="read"),
    Operation("ledger.get", "POST", "/api/v1/ledger/get",
              "Read one ledger item with its events.",
              "Read one ledger item with its events. The item is L-0012, L-12 or 12; events come oldest first: who did what, through which "
              "way, and the words said. Read-only. Answers conflict while the ledger is off.",
              _object({"item": _STR}, closed=True), GET, risk="read"),
    Operation("ledger.status", "POST", "/api/v1/ledger/status",
              "Whether the ledger is on, what waits for the user, and whether a bot has no operator id.",
              "Whether the ledger is on, what waits for the user, and whether a bot has no operator id. Counts open "
              "to-dos and decisions only relayed, and every status; works while the ledger is off.", _object({}, closed=True), STATUS, risk="read", operator_only=True),
    Operation("ledger.add", "POST", "/api/v1/ledger/add",
              "Add an item to the ledger as the user, from the dashboard.",
              "Add an item to the ledger as the user, from the dashboard. A to-do, a decision or a work item.", ADD, _ITEM, risk="write",
              operator_only=True, audit_operator=False),
    Operation("ledger.act", "POST", "/api/v1/ledger/act",
              "Confirm, close, cancel, reopen, note or amend a ledger item as the user, from the dashboard.",
              "Confirm, close, cancel, reopen, note or amend a ledger item as the user, from the dashboard. Confirm a relayed "
              "decision, close, cancel or reopen an item, add a note or amend its title, detail, "
              "due date, owner or project, as the user. A sourced item's state is changed at its source.",
              ACT, _ITEM, risk="write", operator_only=True, audit_operator=False),
    Operation("ledger.configure", "POST", "/api/v1/ledger/configure",
              "Turn the ledger on or off.", "Turn the ledger on or off. It sets the system setting ledger.enabled.",
              _object({"enabled": _BOOL}, closed=True), STATUS, risk="write", operator_only=True,
              audit_operator=False),
)


async def _checked(call, arguments):
    if not await store.enabled():
        raise OpError(ErrorCode.CONFLICT, "the ledger is not enabled (system setting ledger.enabled)")
    try:
        return await call(arguments)
    except store.LedgerRefused as refused:
        code = {"not_found": ErrorCode.NOT_FOUND, "conflict": ErrorCode.CONFLICT}.get(refused.code, ErrorCode.INVALID)
        raise OpError(code, str(refused)) from None


async def _list(arguments):
    status = arguments.get("status", "open")
    items, more = await store.list_items(
        status=None if status == "all" else status, kind=arguments.get("kind"), owner=arguments.get("owner"),
        project_id=arguments.get("project_id"), limit=arguments.get("limit", store.LIST_DEFAULT),
        needs_you=arguments.get("needs_you", False))
    return {"items": items, "more": more, "counts": await store.counts()}


async def _get(arguments):
    item_id = store.parse_id(arguments["item"])
    item = await store.get_item(item_id)
    if item is None:
        raise store.LedgerRefused("not_found", f"{store.format_id(item_id)} does not exist")
    events = [{key: event[key] for key in _EVENT["properties"]} for event in await store.list_events(item_id)]
    return {"item": item, "events": events}


async def _status():
    on = await store.enabled()
    return {"enabled": on, "needs_you": await store.needs_you_count() if on else 0,
            "counts": await store.counts() if on else dict.fromkeys(store.STATUSES, 0),
            "operator_unset": await store.operator_unset()}


_DASHBOARD = store.Actor("user", "dashboard", actor_id="dashboard")


async def _write(arguments, operation):
    name = operation.operation_id
    if name == "ledger.configure":
        await store.set_enabled(arguments["enabled"])
        return await _status()
    if not await store.enabled():
        raise OpError(ErrorCode.CONFLICT, "the ledger is not enabled (turn it on in Settings → Chat)")
    try:
        if name == "ledger.add":
            new = store.NewItem(
                kind=arguments["kind"], title=arguments["title"], detail=arguments.get("detail", ""),
                due_at=store.parse_due(arguments["due"]) if arguments.get("due") else None,
                project_id=arguments.get("project_id"))
            item = await store.apply("add", _DASHBOARD, new=new)
            return {key: value for key, value in item.items() if key != "created"}
        fields = arguments.get("fields")
        if fields is not None:
            fields = {("due_at" if key == "due" else key): value for key, value in fields.items()}
            if fields.get("due_at") is not None:
                fields["due_at"] = store.parse_due(fields["due_at"])
        return await store.apply(arguments["action"], _DASHBOARD, item_id=store.parse_id(arguments["item"]),
                                 note=arguments.get("note"), fields=fields)
    except store.LedgerRefused as refused:
        code = {"not_found": ErrorCode.NOT_FOUND, "conflict": ErrorCode.CONFLICT,
                "forbidden": ErrorCode.FORBIDDEN}.get(refused.code, ErrorCode.INVALID)
        raise OpError(code, str(refused)) from None


async def run(server, operation, arguments, principal, headers):
    if operation.operation_id == "ledger.status":
        return _plain(await _status())
    if operation.operation_id in ("ledger.add", "ledger.act", "ledger.configure"):
        return _plain(await _write(arguments, operation))
    return _plain(await _checked(_list if operation.operation_id == "ledger.list" else _get, arguments))
