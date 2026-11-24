"""Ledger reads and the operator's page and writes (completion-ledger RFC, sections 4.8 and 4.9)."""

from dataclasses import asdict
from urllib.parse import quote, urlencode

from ...db import ledger as store
from ...db.connection import get_pool
from ..action_levels import ActionLevel
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
PAGE = _object({"items": _array(_ITEM), "more": _INT, "next_cursor": _NULL_STR, "counts": _COUNTS,
                "attention_count": _INT,
                "missing_operator_ids": _array(_object({"channel_id": _STR, "channel_type": _STR, "name": _STR},
                                                       closed=True))}, closed=True)
WRITE = _object({"item": _ITEM}, closed=True)
_TITLE = {"type": "string"}  # trim before checking the 1–200 character bound
_DETAIL = {"type": "string", "maxLength": 4000}
_NOTE = {"type": "string", "maxLength": 2000}
_FIELDS = {**_object({"title": _TITLE, "detail": {**_DETAIL, "type": ["string", "null"]},
                     "due": _NULL_STR, "owner": _NULL_STR, "project_id": _NULL_STR}, [], closed=True),
           "minProperties": 1}
_ACTION = {**_object({"item": _STR, "action": _STR, "note": _NOTE, "fields": _FIELDS},
                     ["item", "action"], closed=True), "oneOf": [
    _object({"item": _STR, "action": {"enum": ["confirm", "reopen"]}}, closed=True),
    _object({"item": _STR, "action": {"enum": ["close", "cancel"]}, "note": _NOTE},
            ["item", "action"], closed=True),
    _object({"item": _STR, "action": {"const": "note"}, "note": _NOTE}, closed=True),
    _object({"item": _STR, "action": {"const": "amend"}, "fields": _FIELDS}, closed=True),
]}

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
                       "limit": {"type": "integer", "minimum": 1, "maximum": store.LIST_MAX}}, [], closed=True),
              LIST, risk="read"),
    Operation("ledger.get", "POST", "/api/v1/ledger/get",
              "Read one ledger item with its events.",
              "Read one ledger item with its events. The item is L-0012, L-12 or 12; events come oldest first: who did what, through which "
              "way, and the words said. Read-only. Answers conflict while the ledger is off.",
              _object({"item": _STR}, closed=True), GET, risk="read"),
    Operation("ledger.page", "POST", "/api/v1/ledger/page",
              "Read a section of the operator's ledger.",
              "Read a section of the operator's ledger. Keyset pagination, global status counts and the actual "
              "attention count. Source links are disclosed only to the operator. Answers conflict while off.",
              _object({"section": {"type": "string", "enum": list(store.PAGE_SECTIONS)},
                       "status": {"type": "string", "enum": ["all", *store.STATUSES]},
                       "kind": {"type": "string", "enum": list(store.KINDS)}, "owner": _STR, "project_id": _STR,
                       "limit": {"type": "integer", "minimum": 1, "maximum": store.LIST_MAX},
                       "cursor": {"type": "string", "maxLength": 2048}}, [], closed=True),
              PAGE, risk="read", operator_only=True, cli=False,
              level=ActionLevel.INSIDE),  # reads Looper's own ledger only
    Operation("ledger.add", "POST", "/api/v1/ledger/add",
              "Record an item as the operator.",
              "Record an item as the operator. Decisions are confirmed by the operator's current input, recorded "
              "as user via api; caller-supplied identity and source fields are refused. Answers conflict while off.",
              _object({"kind": {"type": "string", "enum": list(store.KINDS)}, "title": _TITLE,
                       "detail": _DETAIL, "due": _NULL_STR, "owner": _STR, "project_id": _NULL_STR,
                       "refs": {"type": "array", "maxItems": store.REFS_MAX, "items": _object({
                           "kind": {"type": "string", "enum": list(store.REF_KINDS)},
                           "ref": {"type": "string", "maxLength": 500}, "label": _STR},
                           ["kind", "ref"], closed=True)},
                       "quote": {"type": ["string", "null"], "maxLength": 1000}},
                      ["kind", "title"], closed=True), WRITE, risk="write", operator_only=True, cli=False,
              level=ActionLevel.INSIDE),  # changes only the local ledger, never its source or an external service
    Operation("ledger.action", "POST", "/api/v1/ledger/action",
              "Change a ledger item as the operator.",
              "Change a ledger item as the operator. Confirm, amend, note, close, cancel or reopen through the "
              "ledger's locked event writer. Sourced state is handled at its source. Answers conflict while off.",
              _ACTION, WRITE, risk="write", operator_only=True, cli=False,
              level=ActionLevel.INSIDE),  # changes only the local ledger and its events
)


async def _checked(call, arguments):
    if not await store.enabled():
        raise OpError(ErrorCode.CONFLICT, "the ledger is not enabled (system setting ledger.enabled)")
    try:
        return await call(arguments)
    except store.LedgerRefused as refused:
        code = {"not_found": ErrorCode.NOT_FOUND, "conflict": ErrorCode.CONFLICT,
                "forbidden": ErrorCode.FORBIDDEN}.get(refused.code, ErrorCode.INVALID)
        raise OpError(code, str(refused)) from None


async def _list(arguments):
    status = arguments.get("status", "open")
    items, more = await store.list_items(
        status=None if status == "all" else status, kind=arguments.get("kind"), owner=arguments.get("owner"),
        project_id=arguments.get("project_id"), limit=arguments.get("limit", store.LIST_DEFAULT))
    return {"items": items, "more": more, "counts": await store.counts()}


async def _get(arguments):
    item_id = store.parse_id(arguments["item"])
    item = await store.get_item(item_id)
    if item is None:
        raise store.LedgerRefused("not_found", f"{store.format_id(item_id)} does not exist")
    events = [{key: event[key] for key in _EVENT["properties"]} for event in await store.list_events(item_id)]
    return {"item": item, "events": events}


async def _operator_source_links(items):
    """Resolve source navigation in batches; never add private conversation locators to the client reads."""
    from ..attention import _chat_href

    gates = [item["source"]["ref"] for item in items if item["source"] and item["source"]["kind"] == "gate"]
    actions = [int(item["source"]["ref"]) for item in items
               if item["source"] and item["source"]["kind"] == "confirmation"]
    pool = get_pool()
    gate_links = {row["step_id"]: f"#tasks/{quote(row['task_id'], safe='')}?{urlencode({'gate': row['step_key']})}"
                  for row in await pool.fetch(
                      "SELECT g.step_id, g.task_id, s.step_key FROM task_user_gates g "
                      "JOIN task_steps s ON s.id = g.step_id WHERE g.step_id = ANY($1::text[])", gates)} if gates else {}
    action_links = {str(row["id"]): _chat_href(row["manager_id"], row["chat"]) if row["channel"] == "web" else None
                    for row in await pool.fetch(
                        "SELECT id, manager_id, channel, chat FROM manager_actions WHERE id = ANY($1::bigint[])",
                        actions)} if actions else {}
    for item in items:
        source = item["source"]
        if source:
            source["href"] = (f"#tasks/{quote(source['ref'], safe='')}" if source["kind"] == "task" else
                              gate_links.get(source["ref"]) if source["kind"] == "gate" else
                              action_links.get(source["ref"]))


async def _page(arguments):
    from ..channels import operator_ids

    items, more, cursor = await store.page_items(**arguments)
    await _operator_source_links(items)
    return {"items": items, "more": more, "next_cursor": cursor, "counts": await store.counts(),
            "attention_count": await store.attention_count(),
            "missing_operator_ids": [asdict(bot) for bot in await operator_ids.missing()]}


async def _owner(value):
    value = "user" if value is None else value
    kind, _, ref = value.partition(":")
    if kind not in store.OWNER_KINDS or (kind == "user") != (not ref) or value != "user" and not ref.strip():
        raise OpError(ErrorCode.INVALID, "owner is user, manager:<id>, task:<id> or chat:<ref>", field="owner")
    if kind in ("manager", "task"):
        table = "managers" if kind == "manager" else "tasks"
        if not await get_pool().fetchval(f"SELECT EXISTS (SELECT 1 FROM {table} WHERE id = $1)", ref):
            raise OpError(ErrorCode.INVALID, f"owner must name an existing {kind}", field="owner")
    return kind, ref or None


async def _project(value):
    if value is not None and not await get_pool().fetchval("SELECT EXISTS (SELECT 1 FROM projects WHERE id = $1)", value):
        raise OpError(ErrorCode.INVALID, "project_id must name an existing project", field="project_id")
    return value


def _title(value):
    title = value.strip()
    if not 1 <= len(title) <= 200:
        raise OpError(ErrorCode.INVALID, "title is 1 to 200 characters after trimming", field="title")
    return title


def _due(value):
    try:
        return store.parse_due(value) if value is not None else None
    except store.LedgerRefused as refused:
        raise OpError(ErrorCode.INVALID, str(refused), field="due") from None


async def _add(arguments):
    kind, ref = await _owner(arguments.get("owner", "user"))
    try:
        refs = store.check_refs(arguments.get("refs", []))
    except store.LedgerRefused as refused:
        raise OpError(ErrorCode.INVALID, str(refused), field="refs") from None
    new = store.NewItem(kind=arguments["kind"], title=_title(arguments["title"]), detail=arguments.get("detail", ""),
                        owner_kind=kind, owner_ref=ref, project_id=await _project(arguments.get("project_id")),
                        due_at=_due(arguments.get("due")), refs=refs)
    item = await store.apply("add", store.Actor("user", "api", actor_id="operator"), new=new,
                             quote=arguments.get("quote"))
    item.pop("created", None)  # apply's registration flag is not part of the public item DTO
    return {"item": item}


async def _action(arguments):
    action = arguments["action"]
    item_id = store.parse_id(arguments["item"])
    fields = None
    if action == "amend":
        fields = dict(arguments["fields"])
        if not fields:
            raise OpError(ErrorCode.INVALID, "fields must include at least one change", field="fields")
        item = await store.get_item(item_id)
        if item and item["source"] and set(fields) - {"due", "owner"}:
            raise OpError(ErrorCode.CONFLICT, "a sourced item's title, detail and project are changed at its source")
        if "title" in fields:
            fields["title"] = _title(fields["title"])
        if "detail" in fields:
            fields["detail"] = fields["detail"] or ""
        if "due" in fields:
            value = fields.pop("due")
            fields["due_at"] = _due(value)
        if "owner" in fields:
            kind, ref = await _owner(fields["owner"])
            fields["owner"] = f"{kind}:{ref}" if ref else kind
        if "project_id" in fields:
            fields["project_id"] = await _project(fields["project_id"])
    note = arguments.get("note")
    if action == "note" and not (note or "").strip():
        raise OpError(ErrorCode.INVALID, "note must include text", field="note")
    return {"item": await store.apply(action, store.Actor("user", "api", actor_id="operator"), item_id=item_id,
                                      fields=fields, note=note)}


async def run(server, operation, arguments, principal, headers):
    operation_id = operation.operation_id
    if operation_id in ("ledger.page", "ledger.add", "ledger.action") and principal.kind != "operator":
        raise OpError(ErrorCode.FORBIDDEN, "only the operator may use this ledger operation")
    calls = {"ledger.list": _list, "ledger.get": _get, "ledger.page": _page, "ledger.add": _add,
             "ledger.action": _action}
    result = await _checked(calls[operation_id], arguments)
    if operation_id == "ledger.get" and principal.kind == "operator":
        await _operator_source_links([result["item"]])
    return _plain(result)
