"""Reading message leads (message-leads-intents RFC, section 7.1): the one query every surface searches with.

``search_rows`` filters and orders leads; ``source_rows`` gives their sources with the chat's and the sender's names,
read now (a deleted chat takes its name with it). ``agents.leads.search`` shapes both into what callers return.
Writers live with their callers: the extraction run stores leads, ``looper leads`` and the operator operations mark
and delete them.

How the words match is ``leads.search_mode`` (assistant search RFC, section 4): every word as written (``words``),
or any word as ``db.text_search`` matches it, a name the lead concerns holding it, and with pg_trgm installed an
ASCII word's likeness (``fulltext``).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

import asyncpg

from .connection import get_pool
from .text_search import RELATIVE_MIN, Match, Term, terms as search_terms, without_jit

Db = asyncpg.Pool | asyncpg.Connection

LIMIT_MAX = 50
SOURCES_SHOWN = 3
# An ASCII word of at least this many characters also matches what is at least this like it (pg_trgm's
# word_similarity; 0.6 is pg_trgm's own default threshold for it).
FUZZY_MIN_CHARS = 4
FUZZY_MIN = 0.6
# The most candidates one set brings (a term's tokens, as written, its likeness, a name), its newest: a word nearly
# every lead holds cannot make a search score them all, and a rarer word still brings its own.
CANDIDATES_PER_SET = 2000


def _db(db: Db | None) -> Db:
    return db if db is not None else get_pool()


def _like(text: str) -> str:
    """``text`` as an ILIKE pattern that matches it anywhere, its own wildcards taken literally."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


# state: '' all; 'expired' an active lead past valid_until; 'active' one before it; else the stored state. A lead the
# check dropped comes after every other.
_SEARCH = """
WITH found AS (
    SELECT l.*,
           CASE WHEN l.state = 'active' AND l.valid_until > $2 THEN 0 WHEN l.state = 'active' THEN 1
                WHEN l.state = 'dropped' THEN 3 ELSE 2 END
               AS state_rank
    FROM message_leads l
    WHERE ($1::text IS NULL OR l.source_id = $1)
      AND ($3 = '' OR ($3 = 'active' AND l.state = 'active' AND l.valid_until > $2)
           OR ($3 = 'expired' AND l.state = 'active' AND l.valid_until <= $2)
           OR ($3 NOT IN ('active', 'expired') AND l.state = $3))
      AND ($4::text IS NULL OR l.kind = $4)
      AND ($5::timestamptz IS NULL OR l.last_sent_at >= $5)
      AND NOT EXISTS (
          SELECT 1 FROM unnest($6::text[]) AS w(pattern)
          WHERE NOT (l.summary ILIKE w.pattern OR l.detail ILIKE w.pattern
                     OR EXISTS (SELECT 1 FROM message_lead_sources s
                                JOIN source_events e ON e.seq = s.event_seq
                                JOIN source_chats c ON c.source_id = e.source_id AND c.chat_id = e.chat_id
                                WHERE s.lead_id = l.id AND c.name ILIKE w.pattern)
                     OR EXISTS (SELECT 1 FROM message_lead_people p
                                JOIN source_people sp ON sp.source_id = l.source_id AND sp.open_id = p.open_id
                                WHERE p.lead_id = l.id AND sp.name ILIKE w.pattern)))
      AND ($7::text IS NULL OR EXISTS (
          SELECT 1 FROM message_lead_sources s
          JOIN source_events e ON e.seq = s.event_seq
          JOIN source_chats c ON c.source_id = e.source_id AND c.chat_id = e.chat_id
          WHERE s.lead_id = l.id AND (e.chat_id = $7 OR c.name ILIKE $8)))
      AND ($9::text IS NULL OR EXISTS (
          SELECT 1 FROM message_lead_people p
          LEFT JOIN source_people sp ON sp.source_id = l.source_id AND sp.open_id = p.open_id
          WHERE p.lead_id = l.id AND (p.open_id = $9 OR sp.name ILIKE $10)))
)
SELECT id, source_id, kind, summary, detail, confidence, state, valid_until, superseded_by, first_sent_at,
       last_sent_at, created_at, count(*) OVER () AS total
FROM found
ORDER BY state_rank, CASE confidence WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, last_sent_at DESC, id DESC
LIMIT $11
"""


async def trigram_schema(db: Db | None = None) -> str | None:
    """The schema pg_trgm is installed in, quoted for SQL; None when it is not installed."""
    return await _db(db).fetchval("SELECT quote_ident(n.nspname) FROM pg_extension e "
                                  "JOIN pg_namespace n ON n.oid = e.extnamespace WHERE e.extname = 'pg_trgm'")


async def search_rows(*, now: datetime, words: Sequence[str] = (), chat: str | None = None, person: str | None = None,
                      since: datetime | None = None, kind: str | None = None, state: str = "active", limit: int = 10,
                      source_id: str | None = None, fulltext: bool = False, trigram: str | None = None,
                      db: Db | None = None) -> tuple[list[dict[str, Any]], int]:
    """The leads that match, best first, at most ``limit``, and how many match in all. Every word must appear in the
    lead's summary or detail, or in the name of a chat or a person it concerns (ignoring case); ``chat`` is a chat id
    or part of its name, ``person`` an open_id or part of a name; ``since`` compares with the lead's newest source;
    ``state`` is one of ``leads_model.SEARCH_STATES``. Order: active, then expired, then the rest, then the dropped;
    high confidence first; newest first.

    With ``fulltext``, a lead matches any word as ``db.text_search`` says (or a name it concerns holding it; and,
    with ``trigram`` naming pg_trgm's schema, an ASCII word it is like); within each state the leads that match more
    of the words, and rarer ones, come first, and those scoring much under the best one are left out."""
    from ..agents.leads_model import SEARCH_STATES

    if state not in SEARCH_STATES:
        raise ValueError(f"state must be one of {', '.join(SEARCH_STATES)}")
    limit = max(1, min(int(limit), LIMIT_MAX))
    found_terms = await search_terms(words, _db(db)) if fulltext else ()
    if found_terms:
        return await _search_fulltext(found_terms, trigram, now=now, chat=chat, person=person, since=since,
                                      kind=kind, state=state, limit=limit, source_id=source_id, db=db)
    patterns = [_like(word) for word in words if word.strip()]
    rows = await _db(db).fetch(
        _SEARCH, source_id, now, "" if state == "all" else state, kind, since, patterns,
        chat, _like(chat) if chat else None, person, _like(person) if person else None, limit)
    found = [dict(row) for row in rows]
    return found, (found[0]["total"] if found else 0)


# The full-text search: _SEARCH's filters without its words ($1 source, $2 now, $3 state, $4 kind, $5 since, $6-$7
# chat, $8-$9 person, $10 limit, $11 the names' patterns, $12 the most candidates a set brings). The candidates
# first, each set as an index finds it, filtered to the requested scope, state, kind and time before its newest
# $12 distinct leads are selected: the leads holding a term's token
# (``idx_message_leads_search``), with pg_trgm those holding a term as written or one like it
# (``idx_message_leads_trigram``), and those whose chat or person has a term in its name. Only they are read, with
# their text and the names of the chats and people they concern, lower-cased; then each term's coverage, the terms'
# weights and the score. The match's values follow $12.
# A set of candidates: OFFSET 0 keeps the LIMIT from turning the index lookup into a newest-first scan.
_FILTERS = """($1::text IS NULL OR l.source_id = $1)
      AND ($3 = '' OR ($3 = 'active' AND l.state = 'active' AND l.valid_until > $2)
           OR ($3 = 'expired' AND l.state = 'active' AND l.valid_until <= $2)
           OR ($3 NOT IN ('active', 'expired') AND l.state = $3))
      AND ($4::text IS NULL OR l.kind = $4)
      AND ($5::timestamptz IS NULL OR l.last_sent_at >= $5)
      AND ($6::text IS NULL OR EXISTS (
          SELECT 1 FROM message_lead_sources s
          JOIN source_events e ON e.seq = s.event_seq
          JOIN source_chats c ON c.source_id = e.source_id AND c.chat_id = e.chat_id
          WHERE s.lead_id = l.id AND (e.chat_id = $6 OR c.name ILIKE $7)))
      AND ($8::text IS NULL OR EXISTS (
          SELECT 1 FROM message_lead_people p
          LEFT JOIN source_people sp ON sp.source_id = l.source_id AND sp.open_id = p.open_id
          WHERE p.lead_id = l.id AND (p.open_id = $8 OR sp.name ILIKE $9)))
"""
_SET = ("(SELECT DISTINCT id FROM (SELECT l.* FROM {source} WHERE {where} OFFSET 0) AS l WHERE "
        + _FILTERS + " ORDER BY id DESC LIMIT $12)")
# The leads of the chats and people whose name holds a term, found from the (few) chats and people.
_BY_CHAT = """source_chats c
        JOIN source_events e ON e.source_id = c.source_id AND e.chat_id = c.chat_id
        JOIN message_lead_sources s ON s.event_seq = e.seq
        JOIN message_leads l ON l.id = s.lead_id AND l.source_id = c.source_id"""
_BY_PERSON = """source_people sp
        JOIN message_lead_people p ON p.open_id = sp.open_id
        JOIN message_leads l ON l.id = p.lead_id AND l.source_id = sp.source_id"""
_FULLTEXT = """
WITH {tokens}, ids AS MATERIALIZED (
    {candidates}
), found AS MATERIALIZED (
    SELECT l.*,
           CASE WHEN l.state = 'active' AND l.valid_until > $2 THEN 0 WHEN l.state = 'active' THEN 1
                WHEN l.state = 'dropped' THEN 3 ELSE 2 END
               AS state_rank,
           lower(l.summary || ' ' || l.detail) AS doc,
           ARRAY(SELECT lower(c.name) FROM message_lead_sources s
                 JOIN source_events e ON e.seq = s.event_seq
                 JOIN source_chats c ON c.source_id = e.source_id AND c.chat_id = e.chat_id
                 WHERE s.lead_id = l.id AND c.name IS NOT NULL
                 UNION
                 SELECT lower(sp.name) FROM message_lead_people p
                 JOIN source_people sp ON sp.source_id = l.source_id AND sp.open_id = p.open_id
                 WHERE p.lead_id = l.id) AS names
    FROM message_leads l
    WHERE l.id IN (SELECT id FROM ids)
), covered AS (
    SELECT f.*, {coverage} FROM found f
), weighed AS (
    SELECT v.*, {weights} FROM covered v
), scored AS (
    SELECT w.*, {score} AS score FROM weighed w
), ranked AS (
    SELECT r.*, max(score) OVER () AS best FROM scored r WHERE score > 0
)
SELECT id, source_id, kind, summary, detail, confidence, state, valid_until, superseded_by, first_sent_at,
       last_sent_at, created_at, count(*) OVER () AS total
FROM ranked WHERE score >= {relative} * best
ORDER BY state_rank, score DESC, CASE confidence WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
         last_sent_at DESC, id DESC
LIMIT $10
"""
# What the leads' indexes hold of a lead (schema.sql ``idx_message_leads_search``, connection.py's trigram index).
_VECTOR = "array_to_tsvector(looper_search_tokens(l.summary || ' ' || l.detail))"
_TEXT = "lower(l.summary || ' ' || l.detail)"


def fulltext_query(found_terms: Sequence[Term], trigram: str | None, *, now: datetime, chat: str | None = None,
                   person: str | None = None, since: datetime | None = None, kind: str | None = None,
                   state: str = "active", limit: int = 10, source_id: str | None = None) -> tuple[str, list[Any]]:
    """The SQL of the full-text search and its values; ``trigram``: pg_trgm's schema, quoted, or None."""
    match = Match(found_terms, start=13)
    # Each term's leads by its tokens first; with pg_trgm, the term as written (inside a word) only while its tokens
    # bring fewer than a full set, and its likeness only while they bring none (a misspelling): a common word reads
    # its index alone.
    tokens = [f"t{i} AS MATERIALIZED {_SET.format(source='message_leads l', where=match.holds(_VECTOR, i))}"
              for i in range(len(match.terms))]
    sets = [f"SELECT id FROM t{i}" for i in range(len(match.terms))]
    sets += [_SET.format(source=_BY_CHAT, where="lower(c.name) LIKE ANY($11::text[])"),
             _SET.format(source=_BY_PERSON, where="lower(sp.name) LIKE ANY($11::text[])")]
    parts = []
    for i, term in enumerate(match.terms):
        text = match.param(term.text, "::text")
        whole = f"EXISTS (SELECT 1 FROM unnest(f.names) AS name WHERE strpos(name, {text}) > 0)"
        fuzzy = None
        if trigram is not None:
            if len(term.text) >= 3:  # pg_trgm finds a pattern of three characters or more
                sets.append(_SET.format(source="message_leads l", where=(
                    f"(SELECT count(*) FROM t{i}) < $12 AND {_TEXT} LIKE {match.param(_like(term.text))}")))
            if term.text.isascii() and len(term.text) >= FUZZY_MIN_CHARS:
                sets.append(_SET.format(source="message_leads l", where=(
                    f"NOT EXISTS (SELECT 1 FROM t{i}) AND {text} OPERATOR({trigram}.<%) {_TEXT}")))
                fuzzy = (f"(SELECT CASE WHEN likeness >= {FUZZY_MIN} THEN likeness ELSE 0 END FROM (SELECT "
                         f"{trigram}.word_similarity({text}, f.doc || ' ' || array_to_string(f.names, ' '))::float8 "
                         "AS likeness) AS fuzzy)")
        parts.append(f"{match.coverage('f.doc', i, whole=whole, fuzzy=fuzzy)} AS c{i}")
    columns = [f"c{i}" for i in range(len(match.terms))]
    sql = _FULLTEXT.format(tokens=", ".join(tokens), candidates="\n    UNION\n    ".join(sets),
                           coverage=", ".join(parts), weights=match.weights(columns), score=Match.score(columns),
                           relative=RELATIVE_MIN)
    return sql, [source_id, now, "" if state == "all" else state, kind, since, chat, _like(chat) if chat else None,
                 person, _like(person) if person else None, limit, [_like(term.text) for term in match.terms],
                 CANDIDATES_PER_SET, *match.params]


async def _search_fulltext(found_terms: Sequence[Term], trigram: str | None, *, now: datetime, chat: str | None,
                           person: str | None, since: datetime | None, kind: str | None, state: str, limit: int,
                           source_id: str | None, db: Db | None) -> tuple[list[dict[str, Any]], int]:
    sql, args = fulltext_query(found_terms, trigram, now=now, chat=chat, person=person, since=since, kind=kind,
                               state=state, limit=limit, source_id=source_id)
    found = [dict(row) for row in await without_jit(_db(db), sql, *args)]
    return found, (found[0]["total"] if found else 0)


async def source_rows(lead_ids: Sequence[int], *, db: Db | None = None) -> dict[int, list[dict[str, Any]]]:
    """Each lead's sources, oldest first, with the chat's kind and name and the sender's name (empty when unknown)."""
    rows = await _db(db).fetch(
        """
        SELECT s.lead_id, e.seq, e.external_id, e.chat_id, e.sender_id, e.sender_kind, e.sent_at,
               c.kind AS chat_kind, c.name AS chat_name, COALESCE(p.name, '') AS sender_name
        FROM message_lead_sources s
        JOIN source_events e ON e.seq = s.event_seq
        JOIN source_chats c ON c.source_id = e.source_id AND c.chat_id = e.chat_id
        LEFT JOIN source_people p ON p.source_id = e.source_id AND p.open_id = e.sender_id
        WHERE s.lead_id = ANY($1::bigint[])
        ORDER BY s.lead_id, e.sent_at, e.seq
        """, list(lead_ids))
    found: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        found.setdefault(row["lead_id"], []).append(dict(row))
    return found
