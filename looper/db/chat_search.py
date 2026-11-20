"""Searching the default Manager's chat history (``chat_messages``) for the user's own conversations, and recording
who a stored direct chat is with (``chat_direct_threads``), for the Manager's ``looper_session_search``.

The user's own conversations (the session key ``main`` of the manager sessions plan) are, of the default Manager's
threads without an API client: the dashboard chat (web sessions), the dashboard's chats with the Planners of the
tasks it created (``planner-<task_id>``), and the direct chats with a bot whose recorded sender is that bot's
operator now. Group chats and other people's direct chats are never searched.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

import asyncpg

from .chat import PLANNER_THREAD_PREFIX
from .connection import get_pool
from .text_search import Match, Term

Db = asyncpg.Pool | asyncpg.Connection
LIMIT_MAX = 10


def _db(db: Db | None) -> Db:
    return db if db is not None else get_pool()


async def note_direct_thread(session_id: str, channel_id: str, sender_id: str, *, db: Db | None = None) -> None:
    """Record that ``session_id`` is a direct chat of bot ``channel_id`` with ``sender_id`` (the first record
    stays: a direct chat is always with the same person)."""
    await _db(db).execute(
        "INSERT INTO chat_direct_threads (session_id, channel_id, sender_id) VALUES ($1, $2, $3) "
        "ON CONFLICT (session_id) DO NOTHING", session_id, channel_id, sender_id)


async def direct_thread_channels(*, db: Db | None = None) -> list[str]:
    """The bots with a recorded direct chat."""
    return [row["channel_id"] for row in await _db(db).fetch(
        "SELECT DISTINCT channel_id FROM chat_direct_threads ORDER BY channel_id")]


# $1 since, $2 the bots, $3 their operators' sender ids, $4 the session the search came from, $5 the message ids not
# to return there (the user's message being answered), $6 the limit; the match's values follow.
_SEARCH = """
WITH candidates AS MATERIALIZED (
    SELECT m.id, m.session_id, m.role, m.content, m.created_at, lower(m.content) AS doc
    FROM chat_messages m
    LEFT JOIN chat_threads t ON t.session_id = m.session_id
    WHERE m.role IN ('user', 'assistant') AND m.content <> ''
      AND ($1::timestamptz IS NULL OR m.created_at >= $1)
      AND coalesce(t.manager_id, 'manager') = 'manager' AND t.client_id IS NULL
      AND NOT (m.session_id = $4 AND m.message_id = ANY($5::text[]))
      AND ((m.channel = 'web' AND NOT starts_with(m.session_id, '{planner}'))
           OR (starts_with(m.session_id, '{planner}') AND EXISTS (
               SELECT 1 FROM tasks k WHERE k.id = substr(m.session_id, {planner_len} + 1)
                 AND k.manager_id = 'manager' AND k.client_id IS NULL))
           OR EXISTS (SELECT 1 FROM chat_direct_threads d
                      JOIN unnest($2::text[], $3::text[]) AS o(channel_id, sender_id)
                        ON o.channel_id = d.channel_id AND o.sender_id = d.sender_id
                      WHERE d.session_id = m.session_id))
      AND {prefilter}
), covered AS (
    SELECT c.id, c.session_id, c.role, c.content, c.created_at, {coverage} FROM candidates c
), weighed AS (
    SELECT v.*, {weights} FROM covered v
), scored AS (
    SELECT w.*, {score} AS score FROM weighed w
)
SELECT id, session_id, role, content, created_at, score, count(*) OVER () AS total
FROM scored WHERE score > 0
ORDER BY score DESC, created_at DESC, id DESC
LIMIT $6
"""


async def search(found: Sequence[Term], *, operators: Sequence[tuple[str, str]], since: datetime | None = None,
                 here: str | None = None, skip: Sequence[str] = (), limit: int = 5,
                 db: Db | None = None) -> tuple[list[dict[str, Any]], int]:
    """The user's own messages and the answers to them that match ``found``, best first, at most ``limit``, and
    how many match in all. ``operators``: (bot, operator sender id) of each bot whose operator is known; ``since``
    bounds the time; ``skip`` are message ids of session ``here`` left out."""
    match = Match(found, start=7)
    columns = [f"c{i}" for i in range(len(match.terms))]
    prefilter = match.prefilter("lower(m.content)")
    coverage = ", ".join(f"{match.coverage('c.doc', i)} AS {column}" for i, column in enumerate(columns))
    sql = _SEARCH.format(coverage=coverage, prefilter=prefilter, weights=match.weights(columns),
                         score=Match.score(columns), planner=PLANNER_THREAD_PREFIX,
                         planner_len=len(PLANNER_THREAD_PREFIX))
    limit = max(1, min(int(limit), LIMIT_MAX))
    rows = await _db(db).fetch(sql, since, [bot for bot, _ in operators], [sender for _, sender in operators],
                               here, list(skip), limit, *match.params)
    found_rows = [dict(row) for row in rows]
    return found_rows, (found_rows[0]["total"] if found_rows else 0)
