"""Seeds from the operator's own Manager conversation, before asynchronous drafting."""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import timedelta

from ..db import connection, memory as db_memory
from .memory_model import Actor, EvidenceKind, Kind, Via
from .message import LARK_CHANNEL_TYPES
from .subscriptions import strip_mentions

TURN_MAX = 3
DAY_MAX = 20
QUOTE_HOURS = 24
HEAD_CHARS = 40
FOOTER = "（已提议记入记忆 {id}：{head}，待你确认）"
REVISED_FOOTER = "（已修改记忆提议 {id}：{head}，待你确认）"


@dataclass(frozen=True)
class Seed:
    kind: Kind
    statement: str
    why: str
    keywords: tuple[str, ...]
    quote: str
    revise: int | None = None

    def __post_init__(self):
        for name, low, high in (("statement", 1, 300), ("why", 0, 300), ("quote", 1, 500)):
            value = getattr(self, name).strip()
            if not low <= len(value) <= high:
                raise ValueError(f"{name} must have {low} to {high} characters")
            object.__setattr__(self, name, value)
        words = tuple(word.strip() for word in self.keywords)
        if not 1 <= len(words) <= 6 or any(not 1 <= len(word) <= 40 for word in words):
            raise ValueError("keywords must have 1 to 6 nonblank words of 1 to 40 characters")
        object.__setattr__(self, "keywords", words)


def normalized(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).split())


async def quote_found(quote, *, session_id: str | None, turn_text: str, now, db=None) -> bool:
    needle = normalized(quote)
    if not needle:
        return False
    if needle in normalized(turn_text):
        return True
    if session_id is None:
        return False
    rows = await (db or connection.get_pool()).fetch(
        "SELECT content FROM chat_messages WHERE session_id=$1 AND role='user' AND created_at >= $2 "
        "ORDER BY created_at DESC, id DESC LIMIT 200", session_id, now - timedelta(hours=QUOTE_HOURS))
    return any(needle in normalized(row["content"]) for row in rows)


async def counts(conn, manager_id, message_id, since) -> tuple[int, int]:
    row = await conn.fetchrow(
        "SELECT count(*) FILTER (WHERE a.message_id=$2) AS turn_count, "
        "count(*) FILTER (WHERE a.created_at >= $3) AS day_count "
        "FROM memory_candidate_sources s JOIN manager_actions a ON a.id=s.manager_action_id "
        "WHERE s.kind='manager_turn' AND a.manager_id=$1 AND (a.message_id=$2 OR a.created_at >= $3)",
        manager_id, message_id, since)
    return row["turn_count"], row["day_count"]


def via_of(channel: str | None) -> Via:
    if channel == "web":
        return Via.DASHBOARD
    if channel in LARK_CHANNEL_TYPES:
        return Via.LARK
    return Via.TELEGRAM if channel == "telegram" else Via.SYSTEM


def footer(candidate_id, statement, *, revised) -> str:
    from .memory_model import format_id

    head = " ".join(strip_mentions(statement).split())[:HEAD_CHARS]
    return (REVISED_FOOTER if revised else FOOTER).format(id=format_id(candidate_id), head=head)


async def create_or_reseed(conn, seed, *, manager_id, action_id, quote, via, now) -> tuple[int, bool]:
    evidence = [db_memory.Evidence(EvidenceKind.MANAGER_TURN, manager_action_id=action_id, quote=seed.quote)]
    fields = dict(statement=seed.statement, why=seed.why, keywords=seed.keywords, evidence=evidence,
                  actor=Actor.MANAGER, via=via)
    if seed.revise is None:
        ident = await db_memory.create(conn, kind=seed.kind, manager_id=manager_id, **fields)
        return ident, False
    # Ownership and kind are checked again with the row locked: preparation is only a preflight.
    row = await db_memory._locked(conn, seed.revise)
    if row["manager_id"] != manager_id:
        raise db_memory.MemoryRefused("no pending proposal of yours", code="not_found")
    if row["kind"] != seed.kind:
        raise db_memory.MemoryRefused("a revision keeps the kind: propose a new one instead", code="invalid")
    await db_memory.reseed(conn, seed.revise, now=now, **fields)
    return seed.revise, True
