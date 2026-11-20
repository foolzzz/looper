"""Operator writes and chat extraction controls for message leads."""

from __future__ import annotations

import logging
from datetime import datetime

import asyncpg

from .connection import get_pool
from .message_stream import transaction
from ..agents import leads_model as model

Db = asyncpg.Pool | asyncpg.Connection
logger = logging.getLogger(__name__)


class LeadConflictError(Exception):
    """The lead's current state forbids the requested transition."""


def _db(db: Db | None) -> Db:
    return db if db is not None else get_pool()


async def mark_inaccurate(lead_id: int, *, via: str, note: str | None, db: Db | None = None) -> bool:
    """Mark a lead inaccurate; a repeated mark replaces its note."""
    if via not in ("dashboard", "cli"):
        raise ValueError("via must be dashboard or cli")
    note = note.strip() or None if note is not None else None
    if note is not None and len(note) > 500:
        raise ValueError("note must be at most 500 characters")
    async with transaction(db) as conn:
        state = await conn.fetchval("SELECT state FROM message_leads WHERE id = $1 FOR UPDATE", lead_id)
        if state == "promoted":
            raise LeadConflictError("a promoted lead cannot be marked inaccurate")
        if state == "dropped":
            raise LeadConflictError("the check already dropped this lead: it is out of the default search")
        # Clear obsolete references atomically with the transition.
        return await conn.fetchval(
            "UPDATE message_leads SET state = 'inaccurate', marked_via = $2, marked_note = $3, "
            "superseded_by = NULL, promoted_kind = NULL, promoted_ref = NULL, updated_at = now() "
            "WHERE id = $1 RETURNING true", lead_id, via, note) is True


async def delete_lead(lead_id: int, *, db: Db | None = None) -> bool:
    """Delete a lead, with its cascading sources and people."""
    return await _db(db).fetchval("DELETE FROM message_leads WHERE id = $1 RETURNING true", lead_id) is True


async def leads_subscriptions(db: Db | None = None) -> list[dict]:
    """All leads subscriptions, ordered by schedule name."""
    rows = await _db(db).fetch(
        "SELECT s.id AS schedule_id, s.name, s.enabled, s.project_id FROM schedules s "
        "JOIN subscriptions sub ON sub.schedule_id = s.id WHERE sub.output_kind = 'leads' ORDER BY s.name, s.id")
    return [dict(row) for row in rows]


async def put_settings(*, enabled: bool | None = None, valid_days: int | None = None, db: Db | None = None) -> None:
    """Save settings and, when turning leads off, disable their schedules in the same transaction."""
    if enabled is not None and not isinstance(enabled, bool):
        raise ValueError("enabled must be a boolean")
    if valid_days is not None and (type(valid_days) is not int or not 1 <= valid_days <= 365):
        raise ValueError("valid_days must be between 1 and 365")
    values = {}
    if enabled is not None:
        values[model.LEADS_ENABLED_KEY] = "true" if enabled else "false"
    if valid_days is not None:
        values[model.LEADS_VALID_DAYS_KEY] = str(valid_days)
    async with transaction(db) as conn:
        for key, value in values.items():
            await conn.execute(
                "INSERT INTO system_settings (key, value) VALUES ($1, $2) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()", key, value)
        if enabled is False:
            await conn.execute(
                "UPDATE schedules SET enabled = false, next_fire_at = NULL, updated_at = now() "
                "WHERE id IN (SELECT schedule_id FROM subscriptions WHERE output_kind = 'leads')")


async def chats(source_id: str, *, query: str | None, extraction: str | None, limit: int, offset: int,
                db: Db | None = None) -> tuple[list[dict], int]:
    """Included chats, filtered by literal name or exact id, with distinct lead and pending-intent counts."""
    if extraction not in (None, "included", "excluded"):
        raise ValueError("extraction must be included or excluded")
    if type(limit) is not int or not 1 <= limit <= 200 or type(offset) is not int or offset < 0:
        raise ValueError("limit must be between 1 and 200 and offset must be nonnegative")
    pattern = None
    if query:
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
    where = ("c.source_id = $1 AND c.inclusion = 'included' "
             "AND ($2::text IS NULL OR c.name ILIKE $2 OR c.chat_id = $3) "
             "AND ($4::text IS NULL OR c.extraction = $4)")
    args = (source_id, pattern, query, extraction)
    store = _db(db)
    total = await store.fetchval(f"SELECT count(*) FROM source_chats c WHERE {where}", *args)
    rows = await store.fetch(
        "SELECT c.chat_id, c.name, c.kind, c.extraction, c.extraction_excluded_at, c.last_message_at, "
        "(SELECT count(DISTINCT ls.lead_id) FROM message_lead_sources ls "
        " JOIN source_events e ON e.seq = ls.event_seq "
        " WHERE e.source_id = c.source_id AND e.chat_id = c.chat_id) AS leads, "
        "(SELECT count(DISTINCT ins.intent_id) FROM intent_sources ins "
        " JOIN source_events e ON e.seq = ins.event_seq JOIN intents i ON i.id = ins.intent_id "
        " WHERE e.source_id = c.source_id AND e.chat_id = c.chat_id AND i.state = 'pending') AS open_intents "
        f"FROM source_chats c WHERE {where} ORDER BY c.last_message_at DESC NULLS LAST, c.chat_id "
        "LIMIT $5 OFFSET $6", *args, limit, offset)
    return [dict(row) for row in rows], total


async def set_extraction(source_id: str, chat_id: str, *, included: bool, delete_extracted: bool, now: datetime,
                         db: Db | None = None) -> dict:
    """Change extraction and optionally delete the chat's leads and intents, keeping tasked intents."""
    if delete_extracted and included:
        raise ValueError("delete_extracted is only allowed when extraction is excluded")
    extraction = "included" if included else "excluded"
    deleted_leads = deleted_intents = 0
    detached = []
    async with transaction(db) as conn:
        found = await conn.fetchval(
            "UPDATE source_chats SET extraction = $3, "
            "extraction_excluded_at = CASE WHEN $3 = 'included' THEN NULL "
            "ELSE COALESCE(extraction_excluded_at, $4) END "
            "WHERE source_id = $1 AND chat_id = $2 RETURNING true",
            source_id, chat_id, extraction, None if included else now)
        if not found:
            raise LookupError(f"unknown chat {chat_id!r}")
        if delete_extracted:
            lead_rows = await conn.fetch(
                "DELETE FROM message_leads l WHERE EXISTS (SELECT 1 FROM message_lead_sources ls "
                "JOIN source_events e ON e.seq = ls.event_seq WHERE ls.lead_id = l.id "
                "AND e.source_id = $1 AND e.chat_id = $2) RETURNING l.id", source_id, chat_id)
            targets = await conn.fetch(
                "SELECT i.id FROM intents i WHERE i.state <> 'tasked' AND EXISTS (SELECT 1 FROM intent_sources ins "
                "JOIN source_events e ON e.seq = ins.event_seq WHERE ins.intent_id = i.id "
                "AND e.source_id = $1 AND e.chat_id = $2) ORDER BY i.id FOR UPDATE", source_id, chat_id)
            target_ids = [row["id"] for row in targets]
            # Detach before DELETE so the database trigger cannot hide these rows from the count.
            # Items with any source in this chat are themselves deletion targets.
            detached = await conn.fetch(
                "WITH candidates AS (SELECT id, merged_into FROM intents "
                "WHERE merged_into = ANY($1::bigint[]) AND NOT (id = ANY($1::bigint[])) FOR UPDATE) "
                "UPDATE intents i SET merged_into = NULL, state = 'pending', handled_at = NULL, handled_via = NULL, "
                "updated_at = now() FROM candidates c WHERE i.id = c.id RETURNING i.id, c.merged_into", target_ids)
            intent_rows = await conn.fetch("DELETE FROM intents WHERE id = ANY($1::bigint[]) RETURNING id", target_ids)
            deleted_leads, deleted_intents = len(lead_rows), len(intent_rows)
    for row in detached:
        logger.info("intent_id=%s merged_into=%s state=pending reason=merge_target_deleted", row["id"], row["merged_into"])
    return {"chat_id": chat_id, "extraction": extraction,
            "deleted_leads": deleted_leads, "deleted_intents": deleted_intents, "detached_intents": len(detached)}
