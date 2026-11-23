"""Intent reads and the single locked state transition writer."""
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from typing import Any

from .connection import get_pool


class FinalNoticeFailure(StrEnum):
    RETRY_EXHAUSTED = "final_notice_retry_exhausted"


class IntentRefused(Exception):
    def __init__(self, message: str = "intent cannot be handled in its current state", *, code: str = "conflict",
                 state: str | None = None, row: dict | None = None) -> None:
        super().__init__(message)
        self.code, self.state, self.row = code, state, row


async def locked(conn: Any, intent_id: int) -> dict:
    row = await conn.fetchrow("SELECT * FROM intents WHERE id = $1 FOR UPDATE", intent_id)
    if row is None:
        raise IntentRefused(f"no intent {intent_id}", code="not_found")
    return dict(row)


async def transition(conn: Any, intent_id: int, to: str, *, via: str, now: datetime,
                     task_id: str | None = None, reason: str | None = None) -> dict:
    row = await locked(conn, intent_id)
    allowed = {"pending": ("tasked", "rejected", "expired"), "expired": ("tasked",)}
    if to not in allowed.get(row["state"], ()):
        raise IntentRefused(state=row["state"], row=row)
    if via not in ("dashboard", "lark", "telegram", "system"):
        raise ValueError("invalid handled_via")
    if reason is not None and (to != "rejected" or len(reason) > 200):
        raise ValueError("rejection reason must have at most 200 characters")
    return dict(await conn.fetchrow(
        "UPDATE intents SET state=$2, handled_at=$3, handled_via=$4, task_id=$5, reject_reason=$6, updated_at=$3 "
        "WHERE id=$1 RETURNING *", intent_id, to, now, via, task_id if to == "tasked" else None,
        reason if to == "rejected" else None))


async def source_rows(ids: Sequence[int], *, db: Any = None) -> dict[int, list[dict]]:
    rows = await (db if db is not None else get_pool()).fetch(
        "SELECT s.intent_id, e.seq, e.external_id, e.chat_id, e.sender_id, e.sent_at, "
        "c.kind AS chat_kind, c.name AS chat_name, COALESCE(p.name,'') AS sender_name "
        "FROM intent_sources s JOIN source_events e ON e.seq=s.event_seq "
        "JOIN source_chats c ON c.source_id=e.source_id AND c.chat_id=e.chat_id "
        "LEFT JOIN source_people p ON p.source_id=e.source_id AND p.open_id=e.sender_id "
        "WHERE s.intent_id=ANY($1::bigint[]) ORDER BY e.sent_at,e.seq", list(ids))
    result: dict[int, list[dict]] = {}
    for row in rows:
        result.setdefault(row["intent_id"], []).append(dict(row))
    return result
