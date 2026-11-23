"""Searching the default Manager's chat history (``chat_messages``) for the user's own conversations, and recording
who a stored direct chat is with (``chat_direct_threads``), for the Manager's ``looper_session_search``.

The index ``idx_chat_messages_search`` (the messages' tokens, ``db.text_search``) picks the candidates: for each term
the newest ``CANDIDATES_PER_TERM`` in scope holding one of its tokens. Only they are read and scored.

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
from .text_search import RELATIVE_MIN, Match, Term, without_jit

Db = asyncpg.Pool | asyncpg.Connection
LIMIT_MAX = 10
# The most candidates one term brings, its newest: a word nearly every message holds cannot make a search read them
# all, and a rarer word still brings its own.
CANDIDATES_PER_TERM = 2000
# What the session search's index holds of a message (schema.sql ``idx_chat_messages_search``).
_VECTOR = "array_to_tsvector(looper_search_tokens(m.content))"


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
# to return there (the user's message being answered), $6 the limit, $7 the most candidates each term brings; the
# match's values follow.
_SCOPE = """m.role IN ('user', 'assistant') AND m.content <> ''
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
                      WHERE d.session_id = m.session_id))"""
# Each term's newest candidates in scope, as the index finds them: OFFSET 0 keeps the LIMIT from turning the index
# lookup into a newest-first scan that tokenizes every row.
_TERM_HITS = """(SELECT id FROM (
        SELECT m.id FROM chat_messages m LEFT JOIN chat_threads t ON t.session_id = m.session_id
        WHERE {holds} AND {scope} OFFSET 0) AS hit
    ORDER BY id DESC LIMIT $7)"""
_SEARCH = """
WITH hits AS MATERIALIZED (
    {hits}
), candidates AS MATERIALIZED (
    SELECT m.id, m.session_id, m.role, m.content, m.created_at, lower(m.content) AS doc
    FROM chat_messages m WHERE m.id IN (SELECT id FROM hits)
), covered AS (
    SELECT c.id, c.session_id, c.role, c.content, c.created_at, {coverage} FROM candidates c
), weighed AS (
    SELECT v.*, {weights} FROM covered v
), scored AS (
    SELECT w.*, {score} AS score FROM weighed w
), ranked AS (
    SELECT r.*, max(score) OVER () AS best FROM scored r WHERE score > 0
)
SELECT id, session_id, role, content, created_at, score, count(*) OVER () AS total
FROM ranked WHERE score >= {relative} * best
ORDER BY score DESC, created_at DESC, id DESC
LIMIT $6
"""


def query(found: Sequence[Term], *, operators: Sequence[tuple[str, str]], since: datetime | None = None,
          here: str | None = None, skip: Sequence[str] = (), limit: int = 5) -> tuple[str, list[Any]]:
    """The SQL of ``search`` and its values."""
    match = Match(found, start=8)
    columns = [f"c{i}" for i in range(len(match.terms))]
    scope = _SCOPE.format(planner=PLANNER_THREAD_PREFIX, planner_len=len(PLANNER_THREAD_PREFIX))
    hits = "\n    UNION\n    ".join(_TERM_HITS.format(holds=match.holds(_VECTOR, i), scope=scope)
                                     for i in range(len(match.terms)))
    coverage = ", ".join(f"{match.coverage('c.doc', i)} AS {column}" for i, column in enumerate(columns))
    sql = _SEARCH.format(hits=hits, coverage=coverage, weights=match.weights(columns), score=Match.score(columns),
                         relative=RELATIVE_MIN)
    limit = max(1, min(int(limit), LIMIT_MAX))
    return sql, [since, [bot for bot, _ in operators], [sender for _, sender in operators], here, list(skip), limit,
                 CANDIDATES_PER_TERM, *match.params]


async def search(found: Sequence[Term], *, operators: Sequence[tuple[str, str]], since: datetime | None = None,
                 here: str | None = None, skip: Sequence[str] = (), limit: int = 5,
                 db: Db | None = None) -> tuple[list[dict[str, Any]], int]:
    """The user's own messages and the answers to them that match ``found``, best first, at most ``limit``, and
    how many match in all. ``operators``: (bot, operator sender id) of each bot whose operator is known; ``since``
    bounds the time; ``skip`` are message ids of session ``here`` left out. The candidates are, for each term, the
    newest ``CANDIDATES_PER_TERM`` messages in scope that the index finds holding one of its tokens."""
    sql, args = query(found, operators=operators, since=since, here=here, skip=skip, limit=limit)
    rows = await without_jit(_db(db), sql, *args)
    found_rows = [dict(row) for row in rows]
    return found_rows, (found_rows[0]["total"] if found_rows else 0)
