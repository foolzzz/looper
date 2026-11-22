"""Operator actions on memory candidates, shared by the dashboard, DM adapter and direct database CLI.

Confirmation is accepted only from dashboard or a verified operator DM showing the complete current revision.
This module records intent; the serialized memory writer performs the vault commit afterwards.
"""

from __future__ import annotations

import asyncio
import difflib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..db import memory as db_memory
from . import memory, memory_model as model, memory_settings, memory_write, vault


def _refuse(message: str, code: str = "invalid", *, state: str | None = None) -> None:
    raise db_memory.MemoryRefused(message, code=code, state=state)


async def _enabled(db: Any) -> None:
    # Check the switch first: disabled operations never open the vault or write a database row.
    raw = await memory_settings.stored(db)
    if raw is None or raw.get("enabled") is not True:
        _refuse("memory writing is not enabled", "disabled")


async def _configuration(db: Any):
    settings, problem = await memory_settings.load(db)
    config, vault_problem = await vault.load()
    if settings is None or config is None or problem or vault_problem:
        _refuse("the memory vault configuration no longer holds", "invalid")
    return config, settings


async def _confirm(conn, row, digest, *, via, now, post_id, channel_id, chat, message_id, operator_verified):
    if via not in model.CONFIRM_VIAS:
        _refuse("a candidate is confirmed on the dashboard or in the operator's direct chat only")
    if row["expires_at"] is not None and row["expires_at"] <= now:
        _refuse("this candidate has expired: revive it before confirming", "state", state=row["state"])
    if via in (model.Via.LARK, model.Via.TELEGRAM):
        if not operator_verified or not channel_id or not chat or not message_id:
            _refuse("only a verified operator direct message may confirm a memory", "forbidden")
        if post_id is None:
            post = await conn.fetchrow(
                "SELECT * FROM memory_posts WHERE candidate_id=$1 AND channel_id=$2 AND chat=$3 AND kind='push' "
                "ORDER BY id DESC LIMIT 1", row["id"], channel_id, chat)
        else:
            post = await conn.fetchrow("SELECT * FROM memory_posts WHERE id=$1 AND candidate_id=$2 "
                                       "AND channel_id=$3 AND chat=$4 AND kind='push'", post_id, row["id"],
                                       channel_id, chat)
        if post is None or post["state"] != "sent" or not post["complete"]:
            _refuse("this candidate was not shown completely: confirm it on the dashboard", "forbidden")
        if post["content_hash"] != digest or post["revision"] != row["revision"]:
            _refuse("this candidate changed since the push: read its latest revision", "stale")
        post_id = post["id"]
    return await db_memory.transition(
        conn, row["id"], model.State.CONFIRMED, action=model.Action.CONFIRM, actor=model.Actor.USER, via=via,
        now=now, content_hash=digest, post_id=post_id, channel_id=channel_id, chat=chat, message_id=message_id)


def _stats(after, before):
    stats = {}
    for path, data in after.items():
        old = (before[path] or b"").decode("utf-8").splitlines()
        new = data.decode("utf-8").splitlines()
        added = removed = 0
        for tag, left, right, start, end in difflib.SequenceMatcher(a=old, b=new).get_opcodes():
            if tag in ("replace", "delete"):
                removed += right - left
            if tag in ("replace", "insert"):
                added += end - start
        stats[path] = {"added": added, "removed": removed}
    return stats


async def act(action: str, candidate: str, *, content_hash: str | None = None, changes=None,
              confirm: bool = False, reason: str | None = None, via: model.Via = model.Via.DASHBOARD,
              db: Any = None, runtime_root: Path | None = None, post_id: int | None = None,
              channel_id: str | None = None, chat: str | None = None, message_id: str | None = None,
              operator_verified: bool = False) -> dict[str, Any]:
    """Apply one operator action atomically. ``operator_verified`` is set only by an identity-checked DM adapter.

    A refusal uses ``db.memory.MemoryRefused`` (disabled, not_found, state, stale, forbidden or invalid). An edit
    and its optional confirmation share a transaction; neither saves anything when validation fails.
    """
    if action not in {"confirm", "reject", "edit", "retry", "revive", "delete"}:
        raise ValueError(f"unknown memory action {action!r}")
    via = model.Via(via)
    if via not in (model.Via.DASHBOARD, model.Via.LARK, model.Via.TELEGRAM, model.Via.CLI):
        _refuse("memory actions require an operator entry point", "forbidden")
    if via in (model.Via.LARK, model.Via.TELEGRAM) and not operator_verified:
        _refuse("only a verified operator direct message may act on a memory", "forbidden")
    if action in {"edit", "revive", "delete"} and via not in (model.Via.DASHBOARD, model.Via.CLI):
        _refuse(f"{action} is available on the dashboard only", "forbidden")
    if action in {"edit", "revive"} and via is not model.Via.DASHBOARD:
        _refuse(f"{action} is available on the dashboard only", "forbidden")
    number = model.parse_id(candidate)
    await _enabled(db)
    now = datetime.now(timezone.utc)
    async with db_memory.transaction(db) as conn:
        found = await conn.fetchrow("SELECT * FROM memory_candidates WHERE id=$1 FOR UPDATE", number)
        if found is None:
            _refuse(f"no memory candidate {model.format_id(number)}", "not_found")
        row = dict(found)
        if row["destination"] != model.Destination.VAULT:
            _refuse("this action applies to vault candidates only", "state", state=row["state"])
        if action == "delete":
            await db_memory.delete(conn, number, via=via)
            return {"candidate": model.format_id(number), "deleted": True}
        target = {"confirm": model.State.CONFIRMED, "reject": model.State.REJECTED,
                  "edit": model.State.PENDING, "retry": model.State.CONFIRMED,
                  "revive": model.State.PENDING}[action]
        try:
            model.check_transition(row["state"], target, model.Action(action), reason=row["reason"])
        except model.TransitionRefused as error:
            _refuse(str(error), "state", state=row["state"])
        if action in {"confirm", "edit"} and content_hash != row["content_hash"]:
            _refuse("this candidate changed since it was shown: read its latest revision", "stale")
        if action == "edit":
            config, settings = await _configuration(conn)
            parsed = await asyncio.to_thread(memory_write.validate_changes, config, settings, row["kind"], changes)
            detail = await db_memory.get(number, db=conn)
            # The editor changes the displayed text, never its targets or the anchors it was reviewed against.
            def identity(item):
                return {key: value for key, value in item.items() if key not in {"content", "line", "new"}}

            if [identity(item) for item in parsed] != [identity(item) for item in detail["revision"]["changes"]]:
                _refuse("editing changes text only: keep the same operations, paths and anchors")
            after, before = await asyncio.to_thread(memory_write.replay, config.path, parsed)
            stats = _stats(after, before)
            large_removal = any(
                item["op"] == model.ChangeOp.REPLACE_NOTE and before[item["path"]]
                and stats[item["path"]]["removed"] > len(before[item["path"]].splitlines()) * .30
                for item in parsed)
            note_path = next((item["path"] for item in parsed if item["op"] in (
                model.ChangeOp.CREATE_NOTE, model.ChangeOp.REPLACE_NOTE)), None)
            row = await db_memory.add_revision(
                conn, number, action=model.Action.EDIT, actor=model.Actor.USER, via=via, made_by=model.MadeBy.OPERATOR,
                changes=parsed, title=row["title"], subject=memory_write.subject(settings, row), diff_stats=stats,
                note_path=note_path, pending_days=settings.pending_days, now=now, large_removal=bool(large_removal))
        if action == "confirm" or (action == "edit" and confirm):
            await _confirm(conn, row, row["content_hash"], via=via, now=now, post_id=post_id, channel_id=channel_id,
                           chat=chat, message_id=message_id, operator_verified=operator_verified)
        elif action == "revive":
            config, settings = await _configuration(conn)
            detail = await db_memory.get(number, db=conn)
            parsed = await asyncio.to_thread(memory_write.validate_changes, config, settings, row["kind"],
                                             detail["revision"]["changes"])
            try:
                await asyncio.to_thread(memory_write.replay, config.path, parsed)
                target = model.State.PENDING
            except memory_write.WriteFailure as error:
                if error.state != model.State.CONFLICT:
                    raise
                target = model.State.DRAFTING
            await db_memory.transition(conn, number, target, action=model.Action.REVIVE, actor=model.Actor.USER,
                                       via=via, now=now,
                                       pending_days=settings.pending_days if target is model.State.PENDING else None)
        elif action in {"reject", "retry"}:
            await db_memory.transition(conn, number, model.State.REJECTED if action == "reject" else model.State.CONFIRMED,
                                       action=model.Action(action), actor=model.Actor.USER, via=via, now=now,
                                       note=reason if action == "reject" else None)
        result = await db_memory.get(number, db=conn)
        return {"candidate": memory.detail_view(result)}
