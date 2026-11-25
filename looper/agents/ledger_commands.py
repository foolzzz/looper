"""``looper ledger``: read and write the ledger straight in the database, so it works with the service stopped
(completion-ledger RFC, section 4.8).

A write is the user's own: it is recorded as ``actor_kind = user``, ``via = cli``, with the label of the interactive
session it was run from (``--session``). That is a speed bump, not isolation: an agent Looper started runs as the same
user and could run this, so a write is refused when ``LOOPER_AGENT_RUN`` (which Looper sets for every agent run) is set.

Exit codes: 0 done (``list``: with results); 1 ``list`` found nothing; 2 usage; 3 the ledger is not enabled;
4 refused (a rule, or the identity); 5 no such item; 6 the database cannot be used.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from datetime import datetime
from typing import Any

import asyncpg

from .. import child_env
from ..db import ledger

logger = logging.getLogger(__name__)

OK, EMPTY, USAGE, DISABLED, REFUSED, NOT_FOUND, NO_DATABASE = 0, 1, 2, 3, 4, 5, 6
_CODES = {"forbidden": REFUSED, "conflict": REFUSED, "not_found": NOT_FOUND, "invalid": USAGE}
WRITES = ("add", "close", "confirm")


class _Exit(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def _due(text: str) -> datetime:
    try:
        return ledger.parse_due(text)
    except ledger.LedgerRefused as refused:
        raise _Exit(USAGE, str(refused)) from None


def _ref(text: str) -> dict[str, str]:
    """``--ref``: ``L-12`` (another item) or ``<kind>:<ref>`` with a kind of ``ledger.REF_KINDS``."""
    if re.fullmatch(r"[Ll]-\d+", text):
        return {"kind": "ledger", "ref": ledger.format_id(ledger.parse_id(text))}
    kind, _, ref = text.partition(":")
    if kind not in ledger.REF_KINDS or not ref:
        raise _Exit(USAGE, f"--ref is L-<number> or <kind>:<ref> with a kind of {', '.join(ledger.REF_KINDS)}")
    return {"kind": kind, "ref": ref}


def _item_id(text: str) -> int:
    try:
        return ledger.parse_id(text)
    except ledger.LedgerRefused as refused:
        raise _Exit(USAGE, str(refused)) from None


def _row(item: dict[str, Any]) -> str:
    due = item["due_at"].astimezone().strftime("%Y-%m-%d") if item["due_at"] else "-"
    flag = " (relayed, not confirmed)" if item["kind"] == "decision" and item["assurance"] != "user" else ""
    return (f"{item['id']}  {item['kind']:<9} {item['status']:<10} {item['title']}{flag}  "
            f"owner={item['owner']} due={due}")


def _event_line(event: dict[str, Any]) -> str:
    who = {"user": "the user", "relayed": f"relayed by {event['relayed_by']}", "system": "the system"}[event["actor_kind"]]
    via = f"cli · {event['session_label']}" if event["via"] == "cli" else event["via"]
    extra = "".join(f"\n      {label}: {event[key]}" for key, label in (("quote", "said"), ("note", "note"))
                    if event[key])
    return f"  {event['created_at'].astimezone():%Y-%m-%d %H:%M}  {event['action']:<9} {who} via {via}{extra}"


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


async def _run(database_url: str, args: Any) -> tuple[int, str]:
    from ..db.connection import close_pool, init_pool

    try:
        pool = await init_pool(database_url, min_size=1, max_size=2)
    except (OSError, asyncpg.PostgresError) as error:
        return NO_DATABASE, f"cannot reach the database: {error}"
    try:
        if not await pool.fetchval("SELECT to_regclass('ledger_items') IS NOT NULL"):
            return NO_DATABASE, "the database has no ledger tables yet: start `looper serve` once to upgrade it"
        if not await ledger.enabled(db=pool):
            return DISABLED, "the ledger is not enabled (system setting ledger.enabled)"
        try:
            return await _command(pool, args)
        except ledger.LedgerRefused as refused:
            if refused.code in ("forbidden", "conflict"):
                logger.warning("ledger refused action=%s code=%s", args.ledger_command, refused.code)
            return _CODES[refused.code], str(refused)
        except _Exit as stop:
            return stop.code, str(stop)
    except (OSError, asyncpg.PostgresError) as error:
        return NO_DATABASE, f"the database failed: {error}"
    finally:
        await close_pool()


async def _project(pool: asyncpg.Pool, name: str) -> str:
    try:
        return await ledger.resolve_project(name, db=pool)
    except ledger.LedgerRefused as refused:
        raise _Exit(USAGE, f"--project: {refused}") from None


async def _command(pool: asyncpg.Pool, args: Any) -> tuple[int, str]:
    command = args.ledger_command
    if command == "list":
        project = await _project(pool, args.project) if args.project else None
        items, more = await ledger.list_items(
            status=None if args.status == "all" else args.status, kind=args.kind, owner=args.owner,
            project_id=project, limit=args.limit, db=pool)
        if args.json:
            text = _dump({"items": items, "more": more})
        else:
            text = "\n".join([*map(_row, items), *([f"... and {more} more"] if more else [])])
        return (OK if items else EMPTY), (text if items or args.json else f"no {args.status} items")
    if command == "show":
        item_id = _item_id(args.item)
        item = await ledger.get_item(item_id, db=pool)
        if item is None:
            return NOT_FOUND, f"{ledger.format_id(item_id)} does not exist"
        events = await ledger.list_events(item_id, db=pool)
        if args.json:
            return OK, _dump({"item": item, "events": events})
        return OK, "\n".join([_row(item), *([f"  {item['detail']}"] if item["detail"] else []),
                              *map(_event_line, events)])
    actor = ledger.Actor("user", "cli", actor_id="cli", session_label=args.session)
    if command == "add":
        project = await _project(pool, args.project) if args.project else None
        new = ledger.NewItem(kind=args.kind, title=args.title, detail=args.detail or "", project_id=project,
                             due_at=_due(args.due) if args.due else None, refs=[_ref(r) for r in args.ref or ()])
        item = await ledger.apply("add", actor, new=new, quote=args.quote, db=pool)
    else:
        item_id = _item_id(args.item)
        action = "confirm" if command == "confirm" else "cancel" if args.cancel else "close"
        item = await ledger.apply(action, actor, item_id=item_id, note=args.note, db=pool)
    return OK, f"{item['id']}  {item['kind']}  {item['status']}  {item['title']}"


def run(args: Any, load_ops: Any) -> int:
    from ..cli import task_manager_guard

    if (code := task_manager_guard("ledger", args.ledger_command)) is not None:
        return code
    if args.ledger_command in WRITES:
        if os.environ.get(child_env.AGENT_RUN_ENV):
            logger.warning("ledger cli refused reason=agent_run")
            print("error: a ledger write is the user's own; run it in your own terminal, not from an agent",
                  file=sys.stderr)
            return REFUSED
        if not (args.session or "").strip():
            print("error: a write needs --session LABEL, the interactive session it is run from", file=sys.stderr)
            return USAGE
    ops = load_ops(args)
    if ops is None:
        return NO_DATABASE
    code, text = asyncio.run(_run(ops.database_url, args))
    print(text, file=sys.stderr if code not in (OK, EMPTY) else sys.stdout)
    return code
