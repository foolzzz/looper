"""The Manager's ledger tools: ``looper_ledger_add`` and ``looper_ledger_update`` (completion-ledger RFC, 4.3 to 4.6).

Only the default Manager has them, and only while ``ledger.enabled`` is on (``service.ManagerOps.current_scope``).
Who is speaking is decided here from the turn's typed facts (``actor_for``), never from an argument. A quote must be
a passage of the message the turn answers. After a write, code adds one line to the reply (``Turn.blocks``) naming
what was recorded. In a group chat, a call sees and touches only what was written down in that chat.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from ...db import channel_configs as db_configs, ledger as store, tasks as db_tasks
from ..message import is_lark
from . import Call, ErrorCode, OpError, Risk, Tool, Turn

TOOL_NAMES = ("looper_ledger_add", "looper_ledger_update")
_CODES = {"forbidden": ErrorCode.FORBIDDEN, "conflict": ErrorCode.CONFLICT, "not_found": ErrorCode.NOT_FOUND,
          "invalid": ErrorCode.INVALID}
_VIA = {"web": "dashboard", "telegram": "telegram"}
_EVENTS_SHOWN = 10
_LEDGER_REF = re.compile(r"([LlIi])-(\d+)")


def _via(channel: str | None) -> str | None:
    return "lark" if is_lark(channel) else _VIA.get(channel or "")


def actor_for(turn: Turn, operator_sender_id: str | None, *, manager_id: str | None = None) -> store.Actor:
    """Who a call of this turn writes as (RFC 4.3): the user only for the dashboard's own chat or a direct chat with
    the bot's operator; every other voice is relayed."""
    where = {"manager_id": manager_id, "channel_id": turn.channel_id, "chat": turn.chat, "message_id": turn.message_id}
    if turn.client_id is not None:
        return store.Actor("relayed", "api", actor_id=turn.sender, relayed_by=f"client:{turn.client_name or turn.client_id}",
                           **where)
    via = _via(turn.channel)
    if via is None:  # a channel this code does not know speaks for nobody in particular
        return store.Actor("relayed", "api", actor_id=turn.sender, relayed_by=f"channel:{turn.channel or 'unknown'}",
                           **where)
    if via == "dashboard":
        return store.Actor("user", "dashboard", actor_id="dashboard", **where)
    if not turn.group and operator_sender_id and turn.sender == operator_sender_id:
        return store.Actor("user", via, actor_id=f"{via}:{turn.sender}", **where)
    return store.Actor("relayed", via, actor_id=turn.sender, relayed_by=f"{via}:{turn.sender or 'unknown'}", **where)


async def _operator_sender_id(turn: Turn) -> str | None:
    if not turn.channel_id:
        return None
    row = await db_configs.get_config(turn.channel_id)
    value = (row or {}).get("config", {}).get("operator_sender_id") if isinstance((row or {}).get("config"), dict) else None
    return value or None


async def _actor(call: Call) -> store.Actor:
    return actor_for(call.turn, await _operator_sender_id(call.turn), manager_id=call.manager_id)


def _squash(text: str) -> str:
    return " ".join(text.split())


def check_quote(quote: str | None, turn: Turn) -> str | None:
    """``quote`` if it is a passage of the message this turn answers (leading and trailing space trimmed, runs of
    space merged), else ``invalid``."""
    if quote is None or not quote.strip():
        return None
    message = turn.envelope.payload.get("text", "") if turn.envelope is not None else ""
    if not isinstance(message, str) or _squash(quote) not in _squash(message):
        raise OpError(ErrorCode.INVALID, "quote must be an exact excerpt of the message you are answering")
    return _squash(quote)


def _refused(refused: store.LedgerRefused) -> OpError:
    return OpError(_CODES.get(refused.code, ErrorCode.INVALID), str(refused))


def _group_scope(call: Call) -> tuple[str | None, str | None] | None:
    """In a group chat: the (channel id, chat) whose items the call may see; None elsewhere."""
    return (call.turn.channel_id, call.turn.chat) if call.turn.group else None


async def _owner(call: Call, text: str | None) -> tuple[str, str | None]:
    if text in (None, "user"):
        return "user", None
    if text == "manager":
        return "manager", call.manager_id
    if await db_tasks.get_task(text) is None:
        raise OpError(ErrorCode.INVALID, "owner is user, manager or the id of a task")
    return "task", text


async def _ref(text: str) -> dict[str, str]:
    found = _LEDGER_REF.fullmatch(text)
    if found is not None:
        kind = "ledger" if found.group(1) in "Ll" else "intent"
        number = int(found.group(2))
        return {"kind": kind, "ref": store.format_id(number) if kind == "ledger" else f"I-{number:04d}"}
    if re.fullmatch(r"https?://\S{1,490}", text):
        return {"kind": "url", "ref": text}
    if text.startswith("vault:") and len(text) > len("vault:"):
        return {"kind": "vault", "ref": text}
    if await db_tasks.get_task(text) is not None:
        return {"kind": "task", "ref": text}
    raise OpError(ErrorCode.INVALID, "a ref is a task id, an L- or I- number, an http(s) URL or vault:<path>")


def _item_result(item: Mapping[str, Any]) -> dict[str, Any]:
    return {"id": item["id"], "kind": item["kind"], "title": item["title"], "status": item["status"],
            "assurance": item["assurance"], "url": f"#ledger?item={item['id']}"}


_SHOWN = ("Looper adds a line about it after your reply; do not repeat it.")


def _added_line(item: Mapping[str, Any], actor: store.Actor) -> str:
    if actor.kind == "user":
        return f"已记入账本 {item['id']}：{item['title']}（说「{item['id']} 不对」可以改或撤销）"
    return f"已记入账本 {item['id']}（转述，待本人核实）：{item['title']}"


async def _add(call: Call) -> dict[str, Any]:
    args, actor = call.arguments, await _actor(call)
    quote = check_quote(args.get("quote"), call.turn)
    if args["kind"] == "decision" and quote is None:
        raise OpError(ErrorCode.INVALID, "a decision needs the quote: the words of the message you are answering")
    supersedes = args.get("supersedes")
    if supersedes is not None:
        if args["kind"] != "decision":
            raise OpError(ErrorCode.INVALID, "supersedes is for a decision")
        if actor.kind != "user":
            raise OpError(ErrorCode.FORBIDDEN, "only the user's own message can replace a decision; record it "
                                               "without supersedes")
    try:
        old = store.parse_id(supersedes) if supersedes is not None else None
        if old is not None and await store.get_item(old, added_in=_group_scope(call)) is None:
            raise store.LedgerRefused("not_found", f"{store.format_id(old)} does not exist")
        owner_kind, owner_ref = await _owner(call, args.get("owner"))
        due = store.parse_due(args["due"]) if args.get("due") else None
        project = await store.resolve_project(args["project"]) if args.get("project") else None
        new = store.NewItem(kind=args["kind"], title=args["title"], detail=args.get("detail") or "",
                            owner_kind=owner_kind, owner_ref=owner_ref, project_id=project, due_at=due,
                            refs=[await _ref(text) for text in args.get("refs") or ()])
        item = (await store.add_superseding(actor, new, old, quote=quote) if old is not None
                else await store.apply("add", actor, new=new, quote=quote))
    except store.LedgerRefused as refused:
        raise _refused(refused) from None
    call.turn.blocks.append(_added_line(item, actor))
    return {"item": _item_result(item), "shown_to_user": _SHOWN}


def _listed(item: Mapping[str, Any]) -> dict[str, Any]:
    return {"id": item["id"], "kind": item["kind"], "title": item["title"], "status": item["status"],
            "assurance": item["assurance"], "source": item["source"], "owner": item["owner"],
            "project": item["project_id"], "due_at": item["due_at"], "overdue": item["overdue"],
            "url": f"#ledger?item={item['id']}"}


_WRITE_LINES = {"confirm": "{id} 已确认", "close": "{id} 已完成", "cancel": "{id} 已撤销", "reopen": "{id} 已重新打开",
                "note": "{id} 已备注", "amend": "{id} 已改：{fields}"}
_FIELDS = ("title", "detail", "due", "owner", "project")


async def _amend_fields(call: Call, fields: Mapping[str, Any]) -> dict[str, Any]:
    unknown = set(fields) - set(_FIELDS)
    if unknown or not fields:
        raise OpError(ErrorCode.INVALID, f"fields is some of {', '.join(_FIELDS)}")
    out: dict[str, Any] = {}
    for name, value in fields.items():
        if name == "title":
            if value is None:
                raise OpError(ErrorCode.INVALID, "title cannot be cleared")
            out["title"] = value
        elif name == "detail":
            out["detail"] = value or ""
        elif name == "due":
            out["due_at"] = store.parse_due(value) if value else None
        elif name == "owner":
            kind, ref = await _owner(call, value)
            out["owner"] = kind if ref is None else f"{kind}:{ref}"
        else:
            out["project_id"] = await store.resolve_project(value) if value else None
    return out


async def _update(call: Call) -> dict[str, Any]:
    args, action = call.arguments, call.arguments["action"]
    scope = _group_scope(call)
    try:
        if action == "list":
            status = args.get("status", "open")
            project = await store.resolve_project(args["project"]) if args.get("project") else None
            items, more = await store.list_items(
                status=None if status == "all" else status, kind=args.get("kind"), project_id=project,
                limit=args.get("limit", store.LIST_DEFAULT), added_in=scope)
            from .. import ledger_cards
            await ledger_cards.offer(call, items)
            return {"items": [_listed(item) for item in items], "more": more}
        if "item" not in args:
            raise OpError(ErrorCode.INVALID, "item is required")
        number = store.parse_id(args["item"])
        item = await store.get_item(number, added_in=scope)
        if item is None:
            raise store.LedgerRefused("not_found", f"{store.format_id(number)} does not exist")
        if action == "get":
            events = await store.list_events(number, limit=_EVENTS_SHOWN)
            from .. import ledger_cards
            await ledger_cards.offer(call, [item])
            return {"item": {**_listed(item), "detail": item["detail"]},
                    "events": [{"at": e["created_at"], "action": e["action"], "actor_kind": e["actor_kind"],
                                "via": e["via"], "quote": e["quote"], "note": e["note"]} for e in events]}
        actor = await _actor(call)
        quote = check_quote(args.get("quote"), call.turn)
        fields = await _amend_fields(call, args.get("fields") or {}) if action == "amend" else None
        done = await store.apply(action, actor, item_id=number, note=args.get("note"), quote=quote, fields=fields)
    except store.LedgerRefused as refused:
        raise _refused(refused) from None
    changed = ", ".join(name for name in _FIELDS if name in (args.get("fields") or {}))
    call.turn.blocks.append(_WRITE_LINES[action].format(id=done["id"], fields=changed))
    return {"item": _item_result(done), "shown_to_user": _SHOWN}


TOOLS: tuple[Tool, ...] = (
    Tool(
        name="looper_ledger_add",
        description="Record in the ledger something the user said: a decision (quote required: their words), a "
                    "to-do waiting for them (pending) or who does what (work_item). Only in the turn answering "
                    "their message.",
        schema={"type": "object", "properties": {
            "kind": {"type": "string", "enum": ["pending", "decision", "work_item"]},
            "title": {"type": "string", "minLength": 1, "maxLength": 200},
            "detail": {"type": "string", "maxLength": 2000},
            "quote": {"type": "string", "maxLength": 1000},
            "due": {"type": "string"}, "project": {"type": "string"}, "owner": {"type": "string"},
            "refs": {"type": "array"}, "supersedes": {"type": "string"}},
            "required": ["kind", "title"]},
        risk=Risk.WRITE, run=_add),
    Tool(
        name="looper_ledger_update",
        description="List or read ledger items, or change one: confirm, amend, note, close, cancel, reopen. Only the "
                    "user's own message confirms or closes.",
        schema={"type": "object", "properties": {
            "action": {"type": "string", "enum": ["list", "get", "confirm", "amend", "note", "close", "cancel",
                                                   "reopen"]},
            "item": {"type": "string"},
            "status": {"type": "string", "enum": ["open", "all", "done", "cancelled", "expired", "superseded"]},
            "kind": {"type": "string", "enum": ["pending", "decision", "work_item"]},
            "project": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": store.LIST_MAX},
            "fields": {"type": "object", "properties": {name: {"type": ["string", "null"]} for name in _FIELDS}},
            "note": {"type": "string", "maxLength": 2000}, "quote": {"type": "string", "maxLength": 1000}},
            "required": ["action"]},
        risk=Risk.WRITE, run=_update, read_actions=frozenset({"list", "get"})),
)
