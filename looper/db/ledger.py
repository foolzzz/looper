"""The Manager's ledger: items and their append-only events (completion-ledger RFC, section 4).

``apply`` is the only writer. It locks the item, checks who may do what (``PERMISSIONS``), inserts the event and
updates the item in one transaction; a refusal is a ``LedgerRefused`` with a code. The database's constraints are the
second line: a 'relayed' event cannot be pointed at by a confirmation or a closing, whatever this code does.

An own item keeps its status. A sourced item (a gate, a confirmation, a task) has none and reads its source's:
``SOURCE_STATUS`` is the one place that maps a source's state to a ledger status; list filters are built from it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import asyncpg

from . import tasks as task_store
from .connection import get_pool

logger = logging.getLogger(__name__)

Db = asyncpg.Pool | asyncpg.Connection

KINDS = ("pending", "decision", "work_item")
STATUSES = ("open", "done", "cancelled", "expired", "superseded")
OWN_STATUSES = ("open", "done", "cancelled", "superseded")
ACTIONS = ("add", "note", "amend", "confirm", "close", "cancel", "supersede", "reopen")
ACTOR_KINDS = ("user", "relayed", "system")
VIA = ("dashboard", "lark", "telegram", "cli", "api", "system")
REF_KINDS = ("task", "gate", "ledger", "intent", "pr", "doc", "vault", "url")
OWNER_KINDS = ("user", "manager", "task", "chat")
ENABLED_KEY = "ledger.enabled"
LIST_MAX = 50
LIST_DEFAULT = 20
REFS_MAX = 10
# A Manager's confirmation lapses this long after it was asked (``agents.ops.service.CONFIRMATION_SECONDS``, which a
# test keeps equal to this).
CONFIRMATION_SECONDS = 600

# Who may do what (RFC 4.4). ``add`` is open to all three kinds, split by the item: the system adds sourced items
# only, the user and a relayed message add own items only (``_check_new``). Everything but ``add`` and ``note`` is
# the user's alone.
PERMISSIONS: Mapping[str, frozenset[str]] = {
    "add": frozenset({"user", "relayed", "system"}),
    "note": frozenset({"user", "relayed"}),
    "confirm": frozenset({"user"}),
    "close": frozenset({"user"}),
    "cancel": frozenset({"user"}),
    "supersede": frozenset({"user"}),
    "reopen": frozenset({"user"}),
    "amend": frozenset({"user"}),
}
OWN_AMEND_FIELDS = ("title", "detail", "due_at", "owner", "project_id")
SOURCED_AMEND_FIELDS = ("due_at", "owner")

# A source's state, mapped to the ledger status it shows. The task's states go through ``task_group``.
SOURCE_STATUS: Mapping[str, Mapping[str, str]] = {
    "gate": {"open": "open", "discussing": "open", "passed": "done", "cancelled": "cancelled", "skipped": "cancelled"},
    # What ``_CONFIRMATION_STATE`` computes: asked and waiting, answered ok, answered with an error, or lapsed.
    "confirmation": {"asked": "open", "ok": "done", "error": "cancelled", "lapsed": "expired"},
}
_TASK_STATUS = {"completed": "done", "cancelled": "cancelled"}
_SOURCE_WHERE = {"gate": "g.state", "confirmation": "ce.state", "task": "t.status"}
_WHERE_HINT = {"gate": "decide it with looper_task_gate_reply", "task": "change the task with looper_task_control",
               "confirmation": "answer the confirmation in the chat that asked"}


class LedgerRefused(Exception):
    """The ledger will not do this. ``code`` is ``forbidden`` (not this actor), ``conflict`` (not in this state),
    ``invalid`` (a bad argument) or ``not_found``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Actor:
    """Who is writing, as the caller's code decided (never a model's say-so)."""

    kind: str
    via: str
    actor_id: str | None = None
    relayed_by: str | None = None
    manager_id: str | None = None
    channel_id: str | None = None
    chat: str | None = None
    message_id: str | None = None
    session_label: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in ACTOR_KINDS or self.via not in VIA:
            raise ValueError(f"unknown actor {self.kind!r} via {self.via!r}")
        # The database checks the same pairs; a caller that gets one wrong fails here, before a transaction.
        if ((self.kind == "system") != (self.via == "system") or (self.kind == "relayed") != bool(self.relayed_by)
                or (self.via == "cli") != bool(self.session_label) or (self.via == "cli" and self.kind != "user")):
            raise ValueError(f"inconsistent actor {self.kind!r} via {self.via!r}")


@dataclass(frozen=True)
class NewItem:
    """An item to add. A sourced one names its source and leaves ``status`` to it."""

    kind: str
    title: str
    detail: str = ""
    source_kind: str | None = None
    gate_step_id: str | None = None
    action_id: int | None = None
    task_id: str | None = None
    owner_kind: str = "user"
    owner_ref: str | None = None
    project_id: str | None = None
    due_at: datetime | None = None
    refs: Sequence[Mapping[str, str]] = ()


def format_id(item_id: int) -> str:
    return f"L-{item_id:04d}"


def parse_id(text: str) -> int:
    """``L-0012``, ``L-12``, ``l-12`` and ``12`` are item 12."""
    found = re.fullmatch(r"\s*(?:[Ll]-)?(\d{1,18})\s*", text or "")
    if found is None or int(found.group(1)) < 1:
        raise LedgerRefused("invalid", f"{text!r} is not a ledger number (L-0012)")
    return int(found.group(1))


def check_refs(refs: Any) -> list[dict[str, str]]:
    if not isinstance(refs, Sequence) or isinstance(refs, str) or len(refs) > REFS_MAX:
        raise LedgerRefused("invalid", f"refs is a list of at most {REFS_MAX} links")
    checked = []
    for ref in refs:
        if (not isinstance(ref, Mapping) or ref.get("kind") not in REF_KINDS
                or not isinstance(ref.get("ref"), str) or not ref["ref"].strip() or len(ref["ref"]) > 500
                or not isinstance(ref.get("label", ""), str) or set(ref) - {"kind", "ref", "label"}):
            raise LedgerRefused("invalid", f"a ref is {{kind: one of {', '.join(REF_KINDS)}, ref: up to 500 characters"
                                           ", label}")
        checked.append({key: ref[key] for key in ("kind", "ref", "label") if key in ref})
    return checked


async def enabled(*, db: Db | None = None) -> bool:
    """Whether the ledger is on (``ledger.enabled`` is ``true``); off without a row."""
    value = await _db(db).fetchval("SELECT value FROM system_settings WHERE key = $1", ENABLED_KEY)
    return value == "true"


def _db(db: Db | None) -> Db:
    return db if db is not None else get_pool()


class _transaction:
    """A transaction on a pool, or on the caller's connection."""

    def __init__(self, db: Db | None) -> None:
        self._db = _db(db)
        self._conn: asyncpg.Connection | None = None
        self._tx: Any = None

    async def __aenter__(self) -> asyncpg.Connection:
        self._conn = await self._db.acquire() if isinstance(self._db, asyncpg.Pool) else self._db
        self._tx = self._conn.transaction()
        await self._tx.start()
        return self._conn

    async def __aexit__(self, *exc: Any) -> None:
        try:
            if exc[0] is None:
                await self._tx.commit()
            else:
                await self._tx.rollback()
        finally:
            if isinstance(self._db, asyncpg.Pool) and self._conn is not None:
                await self._db.release(self._conn)


# --- reads -------------------------------------------------------------------------------------------------------

def _task_statuses(status: str) -> list[str]:
    return [raw for raw in task_store.STATUS_GROUPS if _task_status(raw) == status]


def _task_status(raw: str | None) -> str:
    return _TASK_STATUS.get(task_store.task_group(raw).value, "open")


def _mapped_status(source_kind: str | None, raw: str | None) -> str | None:
    if source_kind is None:
        return None
    if source_kind == "task":
        return _task_status(raw)
    return SOURCE_STATUS[source_kind].get(raw or "")


# What a sourced item reads: the source's state, and for a confirmation what became of it (the same confirmation id
# answered by an 'ok' row, or an 'error' row, or neither and past its time).
_SELECT = f"""
SELECT i.*, g.state AS gate_state, t.status AS task_status, ce.state AS confirmation_state,
       COALESCE(g.topic, NULLIF(t.title, ''), a.tool) AS source_title,
       COALESCE(i.project_id, t.project_id) AS shown_project_id,
       CASE i.source_kind WHEN 'gate' THEN g.state WHEN 'task' THEN t.status WHEN 'confirmation' THEN ce.state END
           AS source_state,
       (SELECT e.actor_kind FROM ledger_events e WHERE e.id = i.confirmed_event_id) AS confirmed_by,
       (SELECT e.via FROM ledger_events e WHERE e.item_id = i.id AND e.action = 'add') AS added_via,
       (SELECT e.actor_kind FROM ledger_events e WHERE e.item_id = i.id AND e.action = 'add') AS added_by
FROM ledger_items i
LEFT JOIN task_user_gates g ON g.step_id = i.gate_step_id
LEFT JOIN tasks t ON t.id = COALESCE(i.task_id, g.task_id)
LEFT JOIN manager_actions a ON a.id = i.action_id
LEFT JOIN LATERAL (SELECT CASE
        WHEN a.id IS NULL THEN NULL
        WHEN EXISTS (SELECT 1 FROM manager_actions r WHERE r.confirmation_id = a.confirmation_id
                     AND a.confirmation_id IS NOT NULL AND r.id > a.id AND r.status = 'ok') THEN 'ok'
        WHEN EXISTS (SELECT 1 FROM manager_actions r WHERE r.confirmation_id = a.confirmation_id
                     AND a.confirmation_id IS NOT NULL AND r.id > a.id AND r.status = 'error') THEN 'error'
        WHEN a.created_at < now() - make_interval(secs => {CONFIRMATION_SECONDS}) THEN 'lapsed'
        ELSE 'asked' END AS state) ce ON true
"""


def item_view(row: Mapping[str, Any]) -> dict[str, Any]:
    """A row of ``_SELECT`` as the item the CLI and the API show: its number, its status (a sourced item's read from
    its source), who made it and whether the user has confirmed it."""
    sourced = row["source_kind"] is not None
    status = _mapped_status(row["source_kind"], row["source_state"]) if sourced else row["status"]
    if row["kind"] == "decision":
        assurance = "user" if row["confirmed_event_id"] is not None else "relayed"
    else:
        assurance = row["added_by"] or "system"
    return {
        "id": format_id(row["id"]), "number": row["id"], "kind": row["kind"], "status": status,
        "title": (row["title"] if not sourced else row["source_title"] or row["title"]),
        "detail": row["detail"], "assurance": assurance,
        "source": ({"kind": row["source_kind"], "ref": row["gate_step_id"] or row["task_id"] or str(row["action_id"]),
                    "state": row["source_state"]} if sourced else None),
        "owner": row["owner_kind"] if row["owner_ref"] is None else f"{row['owner_kind']}:{row['owner_ref']}",
        "project_id": row["shown_project_id"], "due_at": row["due_at"],
        "overdue": bool(row["due_at"] and status == "open" and row["due_at"] < datetime.now(row["due_at"].tzinfo)),
        "refs": json.loads(row["refs"]) if isinstance(row["refs"], str) else list(row["refs"]),
        "superseded_by": format_id(row["superseded_by"]) if row["superseded_by"] else None,
        "added_via": row["added_via"], "created_at": row["created_at"], "updated_at": row["updated_at"],
        "closed_at": row["closed_at"],
    }


def _status_clause(status: str, args: list[Any]) -> str:
    """SQL for ``status`` over own and sourced items, with the source states taken from ``SOURCE_STATUS``."""
    parts = []
    if status in OWN_STATUSES:
        args.append(status)
        parts.append(f"(i.source_kind IS NULL AND i.status = ${len(args)})")
    for kind, mapping in SOURCE_STATUS.items():
        raws = [raw for raw, mapped in mapping.items() if mapped == status]
        if raws:
            args.append(raws)
            parts.append(f"(i.source_kind = '{kind}' AND {_SOURCE_WHERE[kind]} = ANY(${len(args)}::text[]))")
    raws = _task_statuses(status)
    if raws:
        args.append(raws)
        parts.append(f"(i.source_kind = 'task' AND t.status = ANY(${len(args)}::text[]))")
    if status == "open":  # a status no task group knows is still open work
        args.append(list(task_store.STATUS_GROUPS))
        parts.append(f"(i.source_kind = 'task' AND NOT t.status = ANY(${len(args)}::text[]))")
    return "(" + " OR ".join(parts or ["false"]) + ")"


def _added_in(args: list[Any], added_in: tuple[str | None, str | None]) -> str:
    """SQL for "added in this chat" (a group sees only what was written down in it)."""
    args.extend(added_in)
    return (f"EXISTS (SELECT 1 FROM ledger_events v WHERE v.item_id = i.id AND v.action = 'add' "
            f"AND v.channel_id IS NOT DISTINCT FROM ${len(args) - 1} AND v.chat IS NOT DISTINCT FROM ${len(args)})")


async def get_item(item_id: int, *, added_in: tuple[str | None, str | None] | None = None,
                   db: Db | None = None) -> dict[str, Any] | None:
    """One item; with ``added_in`` (channel id, chat), only if it was added in that chat."""
    args: list[Any] = [item_id]
    extra = f" AND {_added_in(args, added_in)}" if added_in else ""
    row = await _db(db).fetchrow(_SELECT + " WHERE i.id = $1" + extra, *args)
    return item_view(row) if row else None


async def list_items(*, status: str | None = "open", kind: str | None = None, owner: str | None = None,
                     project_id: str | None = None, limit: int = LIST_DEFAULT,
                     added_in: tuple[str | None, str | None] | None = None, db: Db | None = None) -> tuple[list[dict[str, Any]], int]:
    """The items matching the filters, newest first, at most ``limit``; and how many more match."""
    if status is not None and status not in STATUSES:
        raise LedgerRefused("invalid", f"status is one of {', '.join(STATUSES)} or all")
    if kind is not None and kind not in KINDS:
        raise LedgerRefused("invalid", f"kind is one of {', '.join(KINDS)}")
    if not 1 <= limit <= LIST_MAX:
        raise LedgerRefused("invalid", f"limit is 1 to {LIST_MAX}")
    args: list[Any] = []
    where = []
    if status is not None:
        where.append(_status_clause(status, args))
    if kind is not None:
        args.append(kind)
        where.append(f"i.kind = ${len(args)}")
    if owner is not None:
        owner_kind, _, owner_ref = owner.partition(":")
        if owner_kind not in OWNER_KINDS:
            raise LedgerRefused("invalid", f"owner is one of {', '.join(OWNER_KINDS)}, or task:<id>")
        args.append(owner_kind)
        where.append(f"i.owner_kind = ${len(args)}")
        if owner_ref:
            args.append(owner_ref)
            where.append(f"i.owner_ref = ${len(args)}")
    if project_id is not None:
        args.append(project_id)
        where.append(f"COALESCE(i.project_id, t.project_id) = ${len(args)}")
    if added_in:
        where.append(_added_in(args, added_in))
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    pool = _db(db)
    rows = await pool.fetch("SELECT *, count(*) OVER () AS matching FROM (" + _SELECT + clause + ") AS found "
                            f"ORDER BY id DESC LIMIT {limit}", *args)
    return [item_view(row) for row in rows], (rows[0]["matching"] - len(rows) if rows else 0)


async def list_events(item_id: int, *, limit: int | None = None, db: Db | None = None) -> list[dict[str, Any]]:
    """An item's events, oldest first (with ``limit``: the last ``limit`` of them)."""
    rows = await _db(db).fetch(
        "SELECT * FROM (SELECT * FROM ledger_events WHERE item_id = $1 ORDER BY id DESC LIMIT $2) AS e ORDER BY id",
        item_id, limit or 10_000)
    return [{**row, "changes": json.loads(row["changes"]) if isinstance(row["changes"], str) else row["changes"]}
            for row in map(dict, rows)]


async def counts(*, db: Db | None = None) -> dict[str, int]:
    """How many items have each status."""
    result = {}
    for status in STATUSES:
        args: list[Any] = []
        result[status] = await _db(db).fetchval(
            "SELECT count(*) FROM (" + _SELECT + " WHERE " + _status_clause(status, args) + ") AS n", *args)
    return result


# --- the one writer ----------------------------------------------------------------------------------------------

def _refuse_actor(action: str, actor: Actor) -> None:
    if actor.kind not in PERMISSIONS[action]:
        raise LedgerRefused(
            "forbidden", f"{action} is not allowed for a {actor.kind} actor: only the user's own message, in a direct "
                         "chat or on the dashboard, can do this; record what was said with note")


def _own(item: Mapping[str, Any], action: str) -> None:
    if item["source_kind"] is not None:
        raise LedgerRefused(
            "conflict", f"{format_id(item['id'])} is read from its {item['source_kind']}, so it cannot be {action}ed "
                        f"here: {_WHERE_HINT[item['source_kind']]}")


def _open(item: Mapping[str, Any], action: str) -> None:
    if item["status"] != "open":
        raise LedgerRefused("conflict", f"{format_id(item['id'])} is {item['status']}, not open: it cannot be {action}ed")


def _check_new(new: NewItem, actor: Actor) -> None:
    sourced = new.source_kind is not None
    if sourced != (actor.kind == "system"):
        raise LedgerRefused("forbidden", "the system only adds sourced items, and only the system adds them"
                            if sourced or actor.kind == "system" else "an item the user or a relayed message adds is own")
    if new.kind not in KINDS:
        raise LedgerRefused("invalid", f"kind is one of {', '.join(KINDS)}")
    if not new.title.strip() or len(new.title) > 200:
        raise LedgerRefused("invalid", "title is 1 to 200 characters")
    if len(new.detail) > 4000:
        raise LedgerRefused("invalid", "detail is at most 4000 characters")
    if new.owner_kind not in OWNER_KINDS or (new.owner_kind == "user") != (new.owner_ref is None):
        raise LedgerRefused("invalid", "owner is user (no ref), or manager, task or chat with a ref")
    check_refs(list(new.refs))


async def apply(action: str, actor: Actor, *, item_id: int | None = None, new: NewItem | None = None,
                note: str | None = None, quote: str | None = None, fields: Mapping[str, Any] | None = None,
                superseded_by: int | None = None, card_token: str | None = None,
                db: Db | None = None) -> dict[str, Any]:
    """Do ``action`` as ``actor`` and return the item. ``add`` takes ``new``; every other action takes ``item_id``
    (``supersede`` also the deciding item, ``amend`` the ``fields``). Raises ``LedgerRefused``."""
    if action not in ACTIONS:
        raise LedgerRefused("invalid", f"action is one of {', '.join(ACTIONS)}")
    _refuse_actor(action, actor)
    if action == "add":
        if new is None:
            raise LedgerRefused("invalid", "add needs the item")
        _check_new(new, actor)
    elif item_id is None:
        raise LedgerRefused("invalid", f"{action} needs an item")
    async with _transaction(db) as conn:
        if action == "add":
            return await _add(conn, new, actor, note, quote)
        if action == "supersede" and superseded_by:
            # Both decisions are locked, lowest id first, so two supersedes that cross cannot wait on each other;
            # the state is read after the locks.
            await conn.fetch("SELECT id FROM ledger_items WHERE id = ANY($1::bigint[]) ORDER BY id FOR UPDATE",
                             [item_id, superseded_by])
        row = await conn.fetchrow("SELECT * FROM ledger_items WHERE id = $1 FOR UPDATE", item_id)
        if row is None:
            raise LedgerRefused("not_found", f"{format_id(item_id)} does not exist")
        item = dict(row)
        changes: dict[str, list[Any]] | None = None
        sets: dict[str, Any] = {}
        if action == "note":
            if not (note or "").strip():
                raise LedgerRefused("invalid", "a note needs text")
        elif action == "confirm":
            _own(item, "confirm")
            if item["kind"] != "decision":
                raise LedgerRefused("conflict", f"{format_id(item_id)} is not a decision: only a relayed decision is confirmed")
            if item["confirmed_event_id"] is not None:
                raise LedgerRefused("conflict", f"{format_id(item_id)} is already confirmed")
            _open(item, "confirm")
            if card_token is not None:
                await _check_card(conn, item, card_token)
        elif action == "close":
            _own(item, "close")
            if item["kind"] == "decision":
                raise LedgerRefused("conflict", f"{format_id(item_id)} is a decision: cancel it, or supersede it")
            _open(item, "close")
            sets = {"status": "done"}
        elif action == "cancel":
            _own(item, "cancel")
            _open(item, "cancel")
            sets = {"status": "cancelled"}
        elif action == "supersede":
            _own(item, "supersede")
            if item["kind"] != "decision":
                raise LedgerRefused("conflict", f"{format_id(item_id)} is not a decision")
            _open(item, "supersede")
            newer = await conn.fetchrow("SELECT id, kind, status FROM ledger_items WHERE id = $1", superseded_by) \
                if superseded_by else None
            if newer is None or newer["kind"] != "decision" or newer["id"] == item_id or newer["status"] != "open":
                raise LedgerRefused("invalid", "supersede names another open decision, the one that replaces this")
            sets = {"status": "superseded", "superseded_by": superseded_by}
        elif action == "reopen":
            _own(item, "reopen")
            if item["status"] == "open":
                raise LedgerRefused("conflict", f"{format_id(item_id)} is already open")
            sets = {"status": "open", "superseded_by": None}
        else:  # amend
            sets, changes = _amend(item, fields or {})
        event = await _event(conn, item_id, item["source_kind"] is not None, action, actor, note=note, quote=quote,
                             changes=changes)
        if action == "confirm":
            sets = {"confirmed_event_id": event, "confirmed_actor": "user"}
        elif action in ("close", "cancel", "supersede"):
            sets |= {"closed_event_id": event, "closed_actor": "user", "closed_at": _NOW}
        elif action == "reopen":
            sets |= {"closed_event_id": None, "closed_actor": None, "closed_at": None}
        await _update(conn, item_id, sets)
        if action in ("confirm", "amend", "close", "cancel", "supersede", "reopen"):
            from . import card_actions

            if await conn.fetchval("SELECT value = 'true' FROM system_settings WHERE key = 'cards.actions.enabled'"):
                await card_actions.resolve_subject(
                    "ledger", format_id(item_id), by=actor.actor_id, via="button" if card_token else "typed",
                    choice="confirm" if action == "confirm" else action,
                    outcome="ok" if action == "confirm" else "stale", pool=conn)
        return item_view(await conn.fetchrow(_SELECT + " WHERE i.id = $1", item_id))


def confirmation_digest(item: Mapping[str, Any]) -> str:
    """The decision's confirmed content, independent of notes and the time they were added."""
    fields = ("id", "kind", "title", "detail", "source_kind", "status", "confirmed_event_id", "owner_kind",
              "owner_ref", "project_id", "due_at", "refs")
    data = {key: item[key] for key in fields}
    if isinstance(data["refs"], str):
        data["refs"] = json.loads(data["refs"])
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


async def _check_card(conn: asyncpg.Connection, item: Mapping[str, Any], token: str) -> None:
    """Check the button under the decision's lock; a concurrent amendment cannot change what is confirmed."""
    card = await conn.fetchrow("SELECT * FROM card_actions WHERE token = $1 FOR UPDATE", token)
    if (card is None or card["kind"] != "ledger" or card["subject"] != format_id(item["id"])
            or card["handled_at"] is not None
            or not await conn.fetchval("SELECT $1::timestamptz > clock_timestamp()", card["expires_at"])):
        raise LedgerRefused("conflict", "这张卡片已经处理或过期")
    if card["digest"] != confirmation_digest(item):
        raise LedgerRefused("conflict", "决定已经更新，请看最新的消息")
    if not await enabled(db=conn):
        raise LedgerRefused("conflict", "账本已关闭")


_NOW = object()  # a column set to now() in the same statement


async def _update(conn: asyncpg.Connection, item_id: int, sets: Mapping[str, Any]) -> None:
    assignments, args = ["updated_at = now()"], []
    for name, value in sets.items():
        if value is _NOW:
            assignments.append(f"{name} = now()")
        else:
            args.append(value)
            cast = "::jsonb" if name == "refs" else ""
            assignments.append(f"{name} = ${len(args)}{cast}")
    try:
        await conn.execute(f"UPDATE ledger_items SET {', '.join(assignments)} WHERE id = ${len(args) + 1}",
                           *args, item_id)
    except (asyncpg.CheckViolationError, asyncpg.ForeignKeyViolationError) as error:
        raise LedgerRefused("invalid", f"the change is not valid ({error.constraint_name})") from None


async def _event(conn: asyncpg.Connection, item_id: int, sourced: bool, action: str, actor: Actor, *,
                 note: str | None, quote: str | None, changes: Mapping[str, Any] | None) -> int:
    try:
        return await conn.fetchval(
            "INSERT INTO ledger_events (item_id, item_sourced, action, actor_kind, actor_id, relayed_by, via, manager_id, "
            "channel_id, chat, message_id, session_label, quote, note, changes) "
            "VALUES ($1, $15, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14::jsonb) RETURNING id",
            item_id, action, actor.kind, actor.actor_id, actor.relayed_by, actor.via, actor.manager_id,
            actor.channel_id, actor.chat, actor.message_id, actor.session_label, quote, note,
            None if changes is None else json.dumps(changes, default=str), sourced)
    except asyncpg.CheckViolationError as error:
        raise LedgerRefused("invalid", f"the event is not valid ({error.constraint_name})") from None


async def _add(conn: asyncpg.Connection, new: NewItem, actor: Actor, note: str | None, quote: str | None) -> dict[str, Any]:
    columns = {"kind": new.kind, "title": new.title.strip(), "detail": new.detail, "source_kind": new.source_kind,
               "gate_step_id": new.gate_step_id, "action_id": new.action_id, "task_id": new.task_id,
               "status": None if new.source_kind else "open", "owner_kind": new.owner_kind,
               "owner_ref": new.owner_ref, "project_id": new.project_id, "due_at": new.due_at,
               "refs": json.dumps(check_refs(list(new.refs)))}
    names = list(columns)
    marks = [f"${n}::jsonb" if name == "refs" else f"${n}" for n, name in enumerate(names, 1)]
    try:
        item_id = await conn.fetchval(
            f"INSERT INTO ledger_items ({', '.join(names)}) VALUES ({', '.join(marks)}) "
            "ON CONFLICT DO NOTHING RETURNING id", *columns.values())
    except (asyncpg.CheckViolationError, asyncpg.ForeignKeyViolationError) as error:
        raise LedgerRefused("invalid", f"the item is not valid ({error.constraint_name})") from None
    if item_id is None:  # a sourced item is registered once: the existing one stands
        existing = await conn.fetchrow(_SELECT + " WHERE i.gate_step_id = $1 OR i.action_id = $2 OR i.task_id = $3",
                                       new.gate_step_id, new.action_id, new.task_id)
        return {**item_view(existing), "created": False}
    event = await _event(conn, item_id, new.source_kind is not None, "add", actor, note=note, quote=quote,
                         changes=None)
    if new.kind == "decision" and actor.kind == "user":  # what the user decides is confirmed from the start
        await _update(conn, item_id, {"confirmed_event_id": event, "confirmed_actor": "user"})
    return {**item_view(await conn.fetchrow(_SELECT + " WHERE i.id = $1", item_id)), "created": True}


async def add_superseding(actor: Actor, new: NewItem, old_id: int, *, quote: str | None = None,
                          db: Db | None = None) -> dict[str, Any]:
    """Add the decision ``new`` and mark decision ``old_id`` superseded by it, in one transaction: the old decision
    is locked and checked first, and whatever fails leaves nothing of either."""
    async with _transaction(db) as conn:
        row = await conn.fetchrow("SELECT id FROM ledger_items WHERE id = $1 FOR UPDATE", old_id)
        if row is None:
            raise LedgerRefused("not_found", f"{format_id(old_id)} does not exist")
        item = await apply("add", actor, new=new, quote=quote, db=conn)
        await apply("supersede", actor, item_id=old_id, superseded_by=item["number"], quote=quote, db=conn)
        return item


def _amend(item: Mapping[str, Any], fields: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, list[Any]]]:
    allowed = SOURCED_AMEND_FIELDS if item["source_kind"] else OWN_AMEND_FIELDS
    unknown = set(fields) - set(allowed)
    if not fields or unknown:
        raise LedgerRefused("invalid", f"amend changes some of {', '.join(allowed)} on this item")
    sets: dict[str, Any] = {}
    for name, value in fields.items():
        if name == "owner":
            kind, _, ref = (value or "user").partition(":")
            if kind not in OWNER_KINDS or (kind == "user") != (not ref):
                raise LedgerRefused("invalid", "owner is user, manager, chat:<ref> or task:<id>")
            sets["owner_kind"], sets["owner_ref"] = kind, ref or None
        elif name == "title":
            if not isinstance(value, str) or not value.strip() or len(value) > 200:
                raise LedgerRefused("invalid", "title is 1 to 200 characters")
            sets[name] = value.strip()
        elif name == "detail":
            if not isinstance(value, str) or len(value) > 4000:
                raise LedgerRefused("invalid", "detail is at most 4000 characters")
            sets[name] = value
        elif name == "due_at":
            if value is not None and not isinstance(value, datetime):
                raise LedgerRefused("invalid", "due_at is a date and time, or null")
            sets[name] = value
        else:
            sets[name] = value
    changes = {}
    for name, value in fields.items():
        current = (item["owner_kind"] if item["owner_ref"] is None else f"{item['owner_kind']}:{item['owner_ref']}") \
            if name == "owner" else item[name]
        shown = value if name != "owner" else (value or "user")
        if current != shown:
            changes[name] = [current, shown]
    if not changes:
        raise LedgerRefused("conflict", "nothing to change")
    return sets, changes


# --- arguments the CLI and the Manager's tools share ---------------------------------------------------------------

def parse_due(text: str) -> datetime:
    """``YYYY-MM-DD`` (the end of that day) or an ISO 8601 time; without a time zone, this machine's."""
    from datetime import date, time

    try:
        parsed = (datetime.combine(date.fromisoformat(text), time(23, 59))
                  if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text) else datetime.fromisoformat(text))
    except ValueError:
        raise LedgerRefused("invalid", f"a due date is YYYY-MM-DD or an ISO 8601 time, not {text!r}") from None
    return parsed if parsed.tzinfo else parsed.astimezone()


async def resolve_project(name: str, *, db: Db | None = None) -> str:
    """The id of the one project whose id or name is ``name``."""
    found = await _db(db).fetch("SELECT id FROM projects WHERE id = $1 OR name = $1", name)
    if len(found) != 1:
        raise LedgerRefused("invalid", f"project {name!r} matches {len(found)} projects: name one by its id")
    return found[0]["id"]


# --- registering what other tables own (RFC 4.5) --------------------------------------------------------------------

SYSTEM = Actor("system", "system")
RECONCILE_SECONDS = 300
# A task is registered while it runs or waits to start; one that already ended is not caught up.
_LIVE_TASK_GROUPS = (task_store.TaskGroup.IN_PROGRESS, task_store.TaskGroup.NEEDS_YOU, task_store.TaskGroup.READY)


async def _register(new: NewItem, *, db: Db | None) -> bool:
    """Register a sourced item; False when it was there already. Never raises: a source's own write has been done."""
    try:
        return bool((await apply("add", SYSTEM, new=new, db=db))["created"])
    except Exception:
        logger.warning("ledger could not register a %s", new.source_kind, exc_info=True)
        return False


def _gate_item(row: Mapping[str, Any]) -> NewItem:
    return NewItem(kind="pending", title=row["topic"].strip()[:200] or "A question for you",
                   source_kind="gate", gate_step_id=row["step_id"])


def _task_item(row: Mapping[str, Any]) -> NewItem:
    title = (row["title"] or row["requirement"] or row["id"]).strip()[:200] or row["id"]
    return NewItem(kind="work_item", title=title, source_kind="task", task_id=row["id"], owner_kind="task",
                   owner_ref=row["id"])


def _confirmation_item(row: Mapping[str, Any]) -> NewItem:
    return NewItem(kind="pending", title=f"Confirm {row['tool']}"[:200], source_kind="confirmation",
                   action_id=row["id"])


async def register_gates(task_id: str, *, db: Db | None = None) -> None:
    """After a plan write: register the task's waiting gates that have no item. A no-op while the ledger is off."""
    try:
        if not await enabled(db=db):
            return
        rows = await _db(db).fetch(_MISSING_GATES + " AND g.task_id = $1", task_id)
    except Exception:
        logger.warning("ledger could not look for the gates of a task", exc_info=True)
        return
    for row in rows:
        await _register(_gate_item(row), db=db)


async def register_task(task_id: str, *, db: Db | None = None) -> None:
    """After a task was created (not by a schedule): register it as a work item. A no-op while the ledger is off."""
    try:
        if not await enabled(db=db):
            return
        row = await _db(db).fetchrow("SELECT id, title, requirement FROM tasks WHERE id = $1 AND schedule_id IS NULL",
                                     task_id)
    except Exception:
        logger.warning("ledger could not read a new task", exc_info=True)
        return
    if row is not None:
        await _register(_task_item(row), db=db)


async def register_confirmation(action_id: int | None, *, db: Db | None = None) -> None:
    """After the Manager's question was recorded: register the ``needs_confirmation`` row as a pending item."""
    if action_id is None:
        return
    try:
        if not await enabled(db=db):
            return
        row = await _db(db).fetchrow("SELECT id, tool FROM manager_actions WHERE id = $1", action_id)
    except Exception:
        logger.warning("ledger could not read a confirmation", exc_info=True)
        return
    if row is not None:
        await _register(_confirmation_item(row), db=db)


_MISSING_GATES = """
SELECT g.step_id, g.topic FROM task_user_gates g
WHERE g.state IN ('open', 'discussing') AND NOT EXISTS (SELECT 1 FROM ledger_items i WHERE i.gate_step_id = g.step_id)"""
_MISSING_TASKS = """
SELECT t.id, t.title, t.requirement FROM tasks t
WHERE t.schedule_id IS NULL AND t.status = ANY($1::text[])
  AND NOT EXISTS (SELECT 1 FROM ledger_items i WHERE i.task_id = t.id)"""
_MISSING_CONFIRMATIONS = f"""
SELECT a.id, a.tool FROM manager_actions a
WHERE a.status = 'needs_confirmation' AND a.confirmation_id IS NOT NULL
  AND a.created_at > now() - make_interval(secs => {CONFIRMATION_SECONDS})
  AND NOT EXISTS (SELECT 1 FROM manager_actions r WHERE r.confirmation_id = a.confirmation_id AND r.id > a.id
                  AND r.status IN ('ok', 'error'))
  AND NOT EXISTS (SELECT 1 FROM ledger_items i WHERE i.action_id = a.id)"""


async def reconcile(*, db: Db | None = None) -> int:
    """Register what the hooks missed: waiting gates, live tasks and recent confirmations without an item.

    How many it registered; more than none means a hook missed one, which is logged as a warning."""
    live = [status for status, group in task_store.STATUS_GROUPS.items() if group in _LIVE_TASK_GROUPS]
    pool = _db(db)
    registered = 0
    for query, args, make in ((_MISSING_GATES, (), _gate_item), (_MISSING_TASKS, (live,), _task_item),
                              (_MISSING_CONFIRMATIONS, (), _confirmation_item)):
        for row in await pool.fetch(query, *args):
            registered += await _register(make(row), db=db)
    (logger.warning if registered else logger.info)("ledger reconcile registered=%d", registered)
    return registered
