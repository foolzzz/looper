"""Memory candidates (memory-write-path RFC, section 4): reads, and the writes that move one, each in the caller's
transaction with the candidate's row locked.

Code owns the transitions (``memory_model.TRANSITIONS``); the schema keeps what each state needs, so a writer that
got one wrong is refused by the database too. Every write first adds its event, then changes the candidate: a
confirmation points at its event (``memory_candidates_confirm_fk``).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import asyncpg

from ..agents import memory_model as model
from ..agents.memory_model import Action, Actor, Destination, EvidenceKind, State, Via
from .connection import get_pool

EVENTS_SHOWN = 50
LIST_MAX = 200


class MemoryRefused(Exception):
    """A write the candidate's state, content or sources do not allow. ``code``: ``not_found``, ``state`` (the
    transition is not allowed), ``stale`` (the content changed since it was shown), ``class`` (a source class the kind
    does not accept), ``tombstoned`` (the operator deleted this before), ``busy`` (another open candidate changes that
    note) or ``invalid``."""

    def __init__(self, message: str, *, code: str, state: str | None = None) -> None:
        super().__init__(message)
        self.code, self.state = code, state


@dataclass(frozen=True)
class Evidence:
    """One source of a candidate: exactly the id its kind needs (``memory_sources_ref``)."""

    kind: EvidenceKind
    manager_action_id: int | None = None
    journal_id: int | None = None
    lead_id: int | None = None
    quote: str | None = None


@asynccontextmanager
async def transaction(db: Any = None) -> AsyncIterator[asyncpg.Connection]:
    """A connection in a transaction: ``db`` itself when it is a connection (a savepoint then), else one of the pool's."""
    if isinstance(db, asyncpg.Connection):
        async with db.transaction():
            yield db
        return
    async with (db or get_pool()).acquire() as conn, conn.transaction():
        yield conn


def _in_transaction(conn: Any) -> None:
    if not isinstance(conn, asyncpg.Connection) or not conn.is_in_transaction():
        raise RuntimeError("memory candidate writes run in the caller's transaction (db.memory.transaction)")


async def _locked(conn: asyncpg.Connection, candidate_id: int) -> dict[str, Any]:
    row = await conn.fetchrow("SELECT * FROM memory_candidates WHERE id = $1 FOR UPDATE", candidate_id)
    if row is None:
        raise MemoryRefused(f"no memory candidate {model.format_id(candidate_id)}", code="not_found")
    return dict(row)


async def _event(conn: asyncpg.Connection, candidate_id: int, action: Action, actor: Actor, via: Via, *,
                 revision: int | None = None, content_hash: str | None = None, note: str | None = None,
                 post_id: int | None = None, channel_id: str | None = None, chat: str | None = None,
                 message_id: str | None = None) -> int:
    if note is not None and len(note) > model.NOTE_MAX_CHARS:
        raise MemoryRefused(f"a note has at most {model.NOTE_MAX_CHARS} characters", code="invalid")
    return await conn.fetchval(
        "INSERT INTO memory_candidate_events (candidate_id, action, actor_kind, via, revision, content_hash, post_id, "
        "channel_id, chat, message_id, note) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11) RETURNING id",
        candidate_id, Action(action).value, Actor(actor).value, Via(via).value, revision, content_hash, post_id,
        channel_id, chat, message_id, note)


def _keywords(keywords: Sequence[str]) -> list[str]:
    found = [word.strip() for word in keywords]
    if not 1 <= len(found) <= model.KEYWORDS_MAX or any(
            not word or len(word) > model.KEYWORD_MAX_CHARS for word in found):
        raise MemoryRefused(f"a seed has 1 to {model.KEYWORDS_MAX} keywords of 1 to {model.KEYWORD_MAX_CHARS} "
                            "characters", code="invalid")
    return found


async def create(conn: asyncpg.Connection, *, kind: model.Kind | str, statement: str, why: str = "",
                 keywords: Sequence[str], evidence: Sequence[Evidence], actor: Actor, via: Via,
                 manager_id: str | None = None, project_id: str | None = None, untrusted: bool = False) -> int:
    """A new 'vault' candidate in ``drafting`` from one seed and its sources; its id.

    Refused (``MemoryRefused``) when its sources' least trusted class is one ``kind`` does not accept (``class``), or
    when the operator deleted the same thing before (``tombstoned``) and the seed is not the operator's own: an
    operator's seed clears that tombstone instead."""
    _in_transaction(conn)
    kind = model.Kind(kind)
    statement, why = statement.strip(), why.strip()
    if not statement or len(statement) > model.STATEMENT_MAX_CHARS or len(why) > model.WHY_MAX_CHARS:
        raise MemoryRefused(f"a seed's statement has 1 to {model.STATEMENT_MAX_CHARS} characters and its why at most "
                            f"{model.WHY_MAX_CHARS}", code="invalid")
    words = _keywords(keywords)
    if not evidence:
        raise MemoryRefused("a seed needs at least one source", code="invalid")
    source_class = model.least_trusted(model.EVIDENCE_CLASS[item.kind] for item in evidence)
    if not model.allowed(kind, source_class):
        raise MemoryRefused(f"a {kind} cannot come from {source_class} sources", code="class")
    if any(item.quote is not None and (item.kind is not EvidenceKind.MANAGER_TURN
                                       or len(item.quote) > model.QUOTE_MAX_CHARS) for item in evidence):
        raise MemoryRefused(f"only an operator turn has a quote, of at most {model.QUOTE_MAX_CHARS} characters",
                            code="invalid")
    fingerprint = model.fingerprint(kind, statement)
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM memory_tombstones WHERE fingerprint = $1)", fingerprint):
        if source_class is not model.SourceClass.OPERATOR:
            raise MemoryRefused("the operator deleted this memory before", code="tombstoned")
        await conn.execute("DELETE FROM memory_tombstones WHERE fingerprint = $1", fingerprint)
    candidate_id = await conn.fetchval(
        "INSERT INTO memory_candidates (kind, source_kind, source_class, statement, why, keywords, fingerprint, "
        "project_id, manager_id, untrusted) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) RETURNING id",
        kind.value, model.SOURCE_KIND[evidence[0].kind].value, source_class.value, statement, why, words, fingerprint,
        project_id, manager_id, untrusted)
    for item in evidence:
        await conn.execute(
            "INSERT INTO memory_candidate_sources (candidate_id, kind, manager_action_id, journal_id, lead_id, quote) "
            "VALUES ($1, $2, $3, $4, $5, $6)", candidate_id, item.kind.value, item.manager_action_id, item.journal_id,
            item.lead_id, item.quote)
    await _event(conn, candidate_id, Action.PROPOSE, actor, via)
    # PostgreSQL delivers this after commit, including to another server process.
    await conn.execute("SELECT pg_notify('looper_memory_draft', '')")
    return candidate_id


async def reseed(conn: asyncpg.Connection, candidate_id: int, *, statement: str, why: str,
                 keywords: Sequence[str], evidence: Sequence[Evidence], actor: Actor, via: Via,
                 now: datetime) -> dict[str, Any]:
    """Correct a trusted pending Manager seed, retaining revisions and adding the new turn as evidence."""
    _in_transaction(conn)
    row = await _locked(conn, candidate_id)
    _ensure_vault(row)
    statement, why = statement.strip(), why.strip()
    if not statement or len(statement) > model.STATEMENT_MAX_CHARS or len(why) > model.WHY_MAX_CHARS:
        raise MemoryRefused(f"a seed's statement has 1 to {model.STATEMENT_MAX_CHARS} characters and its why at most "
                            f"{model.WHY_MAX_CHARS}", code="invalid")
    words = _keywords(keywords)
    if row["state"] != State.PENDING:
        raise MemoryRefused("only a pending proposal can be revised", code="state", state=row["state"])
    if row["source_kind"] != model.SourceKind.MANAGER_TURN or row["untrusted"]:
        raise MemoryRefused("only a proposal made from the user's own words can be revised", code="invalid")
    if not evidence or any(item.kind is not EvidenceKind.MANAGER_TURN for item in evidence):
        raise MemoryRefused("a revised seed needs the user's own Manager turn as evidence", code="invalid")
    if any(item.quote is not None and len(item.quote) > model.QUOTE_MAX_CHARS for item in evidence):
        raise MemoryRefused(f"only an operator turn has a quote, of at most {model.QUOTE_MAX_CHARS} characters",
                            code="invalid")
    classes = [item["source_class"] for item in await conn.fetch(
        "SELECT source_class FROM memory_candidate_sources WHERE candidate_id=$1", candidate_id)]
    combined = model.least_trusted([*classes, *(model.EVIDENCE_CLASS[item.kind] for item in evidence)])
    if not model.allowed(row["kind"], combined):
        raise MemoryRefused(f"a {row['kind']} cannot come from {combined} sources", code="class")
    fingerprint = model.fingerprint(row["kind"], statement)
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM memory_tombstones WHERE fingerprint=$1)", fingerprint):
        # New evidence is entirely the operator's own turns, even if older sources lowered the candidate's class.
        await conn.execute("DELETE FROM memory_tombstones WHERE fingerprint=$1", fingerprint)
    await conn.execute("UPDATE memory_candidates SET statement=$2,why=$3,keywords=$4,fingerprint=$5,updated_at=$6 "
                       "WHERE id=$1", candidate_id, statement, why, words, fingerprint, now)
    for item in evidence:
        await conn.execute("INSERT INTO memory_candidate_sources(candidate_id,kind,manager_action_id,quote) "
                           "VALUES($1,$2,$3,$4)", candidate_id, item.kind.value, item.manager_action_id, item.quote)
    result = await transition(conn, candidate_id, State.DRAFTING, action=Action.PROPOSE, actor=actor, via=via,
                              now=now)
    await conn.execute("SELECT pg_notify('looper_memory_draft', '')")
    return result


def _actor_may(action: Action, actor: Actor, via: Via) -> None:
    """The schema's rules on who acts (memory_events_user, memory_events_confirm, memory_events_system)."""
    if action in model.USER_ACTIONS and actor is not Actor.USER:
        raise MemoryRefused(f"only the operator can {action} a candidate", code="invalid")
    if action is Action.RECOVER and actor is not Actor.SYSTEM:
        raise MemoryRefused("only the system can recover a candidate", code="invalid")
    if action is Action.CONFIRM and via not in model.CONFIRM_VIAS:
        raise MemoryRefused("a candidate is confirmed on the dashboard or in the operator's direct chat only",
                            code="invalid")


def _ensure_vault(row: Mapping[str, Any]) -> None:
    if row["destination"] != Destination.VAULT:
        raise MemoryRefused(f"{model.format_id(row['id'])} is a lesson: it has no vault transitions", code="state",
                            state=row["state"])


def _refused(row: Mapping[str, Any], error: model.TransitionRefused) -> MemoryRefused:
    return MemoryRefused(f"{model.format_id(row['id'])}: {error}", code="state", state=row["state"])


async def add_revision(conn: asyncpg.Connection, candidate_id: int, *, action: Action, actor: Actor, via: Via,
                       made_by: model.MadeBy, changes: Sequence[Mapping[str, Any]], title: str, subject: str,
                       diff_stats: Mapping[str, Any], note_path: str | None, pending_days: int, now: datetime,
                       large_removal: bool = False, flagged: bool | None = None, hard_rule: bool | None = None,
                       record_id: int | None = None) -> dict[str, Any]:
    """A new revision of ``candidate_id``'s content: its changes and content hash become the candidate's, which is
    ``pending`` again until ``pending_days`` from ``now``, with no confirmation (the new content was not confirmed).
    The candidate row afterwards."""
    _in_transaction(conn)
    action, actor, via = Action(action), Actor(actor), Via(via)
    row = await _locked(conn, candidate_id)
    _ensure_vault(row)
    try:
        model.check_transition(row["state"], State.PENDING, action, reason=row["reason"])
    except model.TransitionRefused as error:
        raise _refused(row, error) from None
    title = title.strip()
    if not title or len(title) > model.TITLE_MAX_CHARS:
        raise MemoryRefused(f"a revision's title has 1 to {model.TITLE_MAX_CHARS} characters", code="invalid")
    if note_path is not None and (problem := model.path_problem(note_path)):
        raise MemoryRefused(f"note_path {note_path!r} {problem}", code="invalid")
    try:
        parsed = model.parse_changes(changes)
        digest = model.content_hash(candidate_id, subject, parsed)
    except ValueError as error:
        raise MemoryRefused(str(error), code="invalid") from None
    revision = row["revision"] + 1
    _actor_may(action, actor, via)
    if note_path is not None and await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM memory_candidates WHERE note_path = $1 AND id <> $2 AND state IN "
            "('pending', 'confirmed', 'writing', 'conflict', 'blocked'))", note_path, candidate_id):
        raise MemoryRefused(f"another open candidate changes {note_path}: revise that one", code="busy")
    await conn.execute(
        "INSERT INTO memory_candidate_revisions (candidate_id, revision, made_by, changes, content_hash, title, "
        "diff_stats, large_removal, record_id) VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7::jsonb, $8, $9)",
        candidate_id, revision, model.MadeBy(made_by).value, json.dumps(parsed, ensure_ascii=False), digest, title,
        json.dumps(dict(diff_stats), ensure_ascii=False), large_removal, record_id)
    await _event(conn, candidate_id, action, actor, via, revision=revision, content_hash=digest)
    return dict(await conn.fetchrow(
        "UPDATE memory_candidates SET state = 'pending', reason = NULL, revision = $2, content_hash = $3, title = $4, "
        "note_path = $5, flagged = COALESCE($6, flagged), hard_rule = COALESCE($7, hard_rule), expires_at = $8, "
        "confirmed_event_id = NULL, confirmed_actor = NULL, confirmed_hash = NULL, merged_into = NULL, "
        "attempts = 0, updated_at = $9 WHERE id = $1 RETURNING *",
        candidate_id, revision, digest, title, note_path, flagged, hard_rule, now + timedelta(days=pending_days), now))


async def transition(conn: asyncpg.Connection, candidate_id: int, to: State | str, *, action: Action | str,
                     actor: Actor | str, via: Via | str, now: datetime, reason: model.Reason | str | None = None,
                     note: str | None = None, content_hash: str | None = None, pending_days: int | None = None,
                     commit_sha: str | None = None, merged_into: int | None = None, post_id: int | None = None,
                     channel_id: str | None = None, chat: str | None = None,
                     message_id: str | None = None) -> dict[str, Any]:
    """Move ``candidate_id`` to ``to`` by ``action`` without changing its content; the candidate row afterwards.

    A confirmation names the ``content_hash`` the operator saw: another one is ``stale``. ``reason`` goes with a
    dropped, conflicting or blocked state; ``pending_days`` with pending (a revived candidate); ``commit_sha`` with
    written; ``merged_into`` with a seed dropped as the same as another candidate."""
    _in_transaction(conn)
    to, action, actor, via = State(to), Action(action), Actor(actor), Via(via)
    row = await _locked(conn, candidate_id)
    _ensure_vault(row)
    try:
        model.check_transition(row["state"], to, action, reason=row["reason"])
    except model.TransitionRefused as error:
        raise _refused(row, error) from None
    if (to in model.REASONS) != (reason is not None) or (
            reason is not None and model.Reason(reason) not in model.REASONS[to]):
        raise MemoryRefused(f"a {to} candidate needs one of its reasons, and no other state has one", code="invalid")
    if (to is State.PENDING) != (pending_days is not None) or (to is State.WRITTEN) != (commit_sha is not None):
        raise MemoryRefused("pending_days goes with pending and commit_sha with written, and only there",
                            code="invalid")
    if merged_into is not None and (to is not State.DROPPED or reason != model.Reason.SAME_AS):
        raise MemoryRefused("merged_into goes only with a candidate dropped as the same as another", code="invalid")
    if to is State.PENDING and row["revision"] < 1:
        raise MemoryRefused("a candidate without a revision cannot be pending", code="state", state=row["state"])
    _actor_may(action, actor, via)
    if action is Action.RECOVER and row["attempts"] >= 3:
        raise MemoryRefused("a candidate cannot be recovered after three write attempts", code="state",
                            state=row["state"])
    if to is State.PENDING and row["note_path"] is not None and await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM memory_candidates WHERE note_path = $1 AND id <> $2 AND state IN "
            "('pending', 'confirmed', 'writing', 'conflict', 'blocked'))", row["note_path"], candidate_id):
        raise MemoryRefused(f"another open candidate changes {row['note_path']}: revise that one", code="busy")
    if action is Action.CONFIRM and content_hash != row["content_hash"]:
        raise MemoryRefused(f"{model.format_id(candidate_id)} changed since it was shown: look at it again",
                            code="stale", state=row["state"])
    event = await _event(conn, candidate_id, action, actor, via, revision=row["revision"],
                         content_hash=content_hash if action is Action.CONFIRM else None, note=note, post_id=post_id,
                         channel_id=channel_id, chat=chat, message_id=message_id)
    confirmed = (event, Actor.USER.value, content_hash) if action is Action.CONFIRM else (
        (row["confirmed_event_id"], row["confirmed_actor"], row["confirmed_hash"])
        if to in model.CONFIRMED_STATES else (None, None, None))
    attempts = row["attempts"] + 1 if action is Action.CLAIM else (
        row["attempts"] if to in (State.WRITING, State.WRITTEN, State.BLOCKED) or action is Action.RECOVER else 0)
    return dict(await conn.fetchrow(
        "UPDATE memory_candidates SET state = $2, reason = $3, confirmed_event_id = $4, confirmed_actor = $5, "
        "confirmed_hash = $6, expires_at = $7, commit_sha = COALESCE($8, commit_sha), "
        "written_at = CASE WHEN $2 = 'written' THEN $9 ELSE written_at END, merged_into = $10, attempts = $11, "
        "updated_at = $9 WHERE id = $1 RETURNING *",
        candidate_id, to.value, model.Reason(reason).value if reason is not None else None, *confirmed,
        now + timedelta(days=pending_days) if pending_days is not None else None, commit_sha, now, merged_into,
        attempts))


async def delete(conn: asyncpg.Connection, candidate_id: int, *, via: Via | str) -> bool:
    """Delete ``candidate_id`` with its revisions, events, posts and sources, leaving only a tombstone (its
    fingerprint): the same thing is not proposed again but by the operator. False when there is no such candidate.
    One being written, or a lesson in effect, cannot be deleted. A written note stays in the vault."""
    _in_transaction(conn)
    via = Via(via)
    if via not in (Via.DASHBOARD, Via.CLI):
        raise ValueError("a candidate is deleted on the dashboard or with the CLI")
    row = await conn.fetchrow("SELECT * FROM memory_candidates WHERE id = $1 FOR UPDATE", candidate_id)
    if row is None:
        return False
    if row["state"] in (State.WRITING, State.IN_EFFECT):
        raise MemoryRefused(f"{model.format_id(candidate_id)} is {row['state']}: it cannot be deleted now",
                            code="state", state=row["state"])
    await conn.execute(
        "INSERT INTO memory_tombstones (fingerprint, kind, destination, deleted_via) VALUES ($1, $2, $3, $4) "
        "ON CONFLICT (fingerprint) DO UPDATE SET deleted_via = EXCLUDED.deleted_via, deleted_at = now()",
        row["fingerprint"], row["kind"], row["destination"], via.value)
    await conn.execute("DELETE FROM memory_candidates WHERE id = $1", candidate_id)
    return True


async def clear_tombstones(*, db: Any = None) -> int:
    """Forget every tombstone; how many there were."""
    async with transaction(db) as conn:
        return int((await conn.execute("DELETE FROM memory_tombstones")).rsplit(" ", 1)[1])


async def tombstone_count(*, db: Any = None) -> int:
    return await (db or get_pool()).fetchval("SELECT count(*) FROM memory_tombstones")


# --- Reads ---

_SOURCES = """
SELECT s.id, s.candidate_id, s.kind, s.source_class, s.quote, s.manager_action_id, s.journal_id, s.lead_id,
       a.channel, a.chat, a.message_id, a.created_at AS said_at, j.task_id
FROM memory_candidate_sources s
LEFT JOIN manager_actions a ON a.id = s.manager_action_id
LEFT JOIN task_journal j ON j.id = s.journal_id
WHERE s.candidate_id = $1 ORDER BY s.id
"""


async def get(candidate_id: int, *, db: Any = None) -> dict[str, Any] | None:
    """A candidate with its current revision (or None), its sources and its latest events (oldest first); None when
    there is no such candidate."""
    db = db or get_pool()
    row = await db.fetchrow("SELECT * FROM memory_candidates WHERE id = $1", candidate_id)
    if row is None:
        return None
    revision = await db.fetchrow("SELECT * FROM memory_candidate_revisions WHERE candidate_id = $1 AND revision = $2",
                                 candidate_id, row["revision"]) if row["revision"] else None
    events = await db.fetch(
        "SELECT * FROM (SELECT id, action, actor_kind, via, revision, content_hash, note, created_at "
        "FROM memory_candidate_events WHERE candidate_id = $1 ORDER BY id DESC LIMIT $2) e ORDER BY id",
        candidate_id, EVENTS_SHOWN)
    return {"candidate": dict(row), "revision": _revision(revision) if revision else None,
            "sources": [dict(item) for item in await db.fetch(_SOURCES, candidate_id)],
            "events": [dict(item) for item in events]}


def _revision(row: Mapping[str, Any]) -> dict[str, Any]:
    found = dict(row)
    for key in ("changes", "diff_stats"):
        if isinstance(found[key], str):
            found[key] = json.loads(found[key])
    return found


async def list_candidates(*, states: Sequence[State | str] | None = None, kind: model.Kind | str | None = None,
                          limit: int = 50, before_id: int | None = None, db: Any = None
                          ) -> tuple[list[dict[str, Any]], bool]:
    """Candidates newest first (``states`` and ``kind`` filter; ``before_id`` continues), and whether there are
    more."""
    if not 1 <= limit <= LIST_MAX:
        raise ValueError(f"limit must be 1 to {LIST_MAX}")
    rows = await (db or get_pool()).fetch(
        "SELECT c.*, (SELECT count(*) FROM memory_candidate_sources s WHERE s.candidate_id = c.id) AS sources_total "
        "FROM memory_candidates c WHERE ($1::text[] IS NULL OR c.state = ANY($1::text[])) "
        "AND ($2::text IS NULL OR c.kind = $2) AND ($3::bigint IS NULL OR c.id < $3) ORDER BY c.id DESC LIMIT $4",
        [State(state).value for state in states] if states is not None else None,
        model.Kind(kind).value if kind is not None else None, before_id, limit + 1)
    return [dict(row) for row in rows[:limit]], len(rows) > limit
