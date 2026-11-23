"""Read-only stored triage events and their message provenance; no why policy or event writer."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import asyncpg

from ..agents.leads_model import Confidence
from ..agents.triage_model import Category, ReasonKind, TriageEvent
from .connection import get_pool

Db = asyncpg.Pool | asyncpg.Connection


def _db(db: Db | None) -> Db:
    return db if db is not None else get_pool()


def _event(row: Mapping[str, Any]) -> TriageEvent:
    return TriageEvent(**{**dict(row), "category": Category(row["category"]),
                          "reason_kind": ReasonKind(row["reason_kind"]), "confidence": Confidence(row["confidence"])})


async def get(event_id: int, *, db: Db | None = None) -> TriageEvent | None:
    """An event by its numeric id, or None when it does not exist."""
    row = await _db(db).fetchrow("SELECT * FROM triage_events WHERE id = $1", event_id)
    return _event(row) if row is not None else None


async def for_message(event_seq: int, *, db: Db | None = None) -> list[TriageEvent]:
    """Events citing this message, highest category first then earliest id; an uncited window is not an event citation."""
    rows = await _db(db).fetch(
        "SELECT e.* FROM triage_events e JOIN triage_event_sources s ON s.event_id = e.id "
        "WHERE s.event_seq = $1 ORDER BY CASE e.category WHEN 'important' THEN 0 WHEN 'digest' THEN 1 ELSE 2 END, e.id", event_seq)
    return [_event(row) for row in rows]


async def for_run(run_id: str, *, db: Db | None = None) -> list[TriageEvent]:
    """A subscription run's events, in id order."""
    rows = await _db(db).fetch("SELECT * FROM triage_events WHERE run_id = $1 ORDER BY id", run_id)
    return [_event(row) for row in rows]


async def source_rows(event_ids: Sequence[int], *, db: Db | None = None) -> dict[int, list[dict[str, Any]]]:
    """Each event's cited messages, oldest first, with current chat and sender names; unknown sender names are empty."""
    rows = await _db(db).fetch(
        "SELECT s.event_id, e.seq, e.external_id, e.chat_id, e.sender_id, e.sender_kind, e.sent_at, "
        "c.kind AS chat_kind, c.name AS chat_name, COALESCE(p.name, '') AS sender_name "
        "FROM triage_event_sources s JOIN source_events e ON e.seq = s.event_seq "
        "JOIN source_chats c ON c.source_id = e.source_id AND c.chat_id = e.chat_id "
        "LEFT JOIN source_people p ON p.source_id = e.source_id AND p.open_id = e.sender_id "
        "WHERE s.event_id = ANY($1::bigint[]) ORDER BY s.event_id, e.sent_at, e.seq", list(event_ids))
    found: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        found.setdefault(row["event_id"], []).append(dict(row))
    return found


async def owned_counts(run_id: str, *, db: Db | None = None) -> dict[int, int]:
    """How many of the run's window messages each of its events owns; an event that owns none is absent.

    A message belongs to one event: the highest category that cites it, then the earliest id (the order ``for_message``
    reads in). An event fully covered by a higher one owns nothing, so it has no handoff of its own and no brief
    entry of its own: its messages are told once, under the event that owns them. Context citations outside the
    run's window own nothing.
    """
    rows = await _db(db).fetch(
        "SELECT owner, count(*) AS n FROM ("
        "SELECT DISTINCT ON (w.seq) w.seq, s.event_id AS owner "
        "FROM subscription_runs r CROSS JOIN LATERAL unnest(r.window_seqs) AS w(seq) "
        "JOIN triage_event_sources s ON s.event_seq = w.seq "
        "JOIN triage_events e ON e.id = s.event_id AND e.run_id = r.run_id "
        "WHERE r.run_id = $1 "
        "ORDER BY w.seq, CASE e.category WHEN 'important' THEN 0 WHEN 'digest' THEN 1 ELSE 2 END, e.id) owned "
        "GROUP BY owner", run_id)
    return {row["owner"]: row["n"] for row in rows}
