"""Identity-only parity evidence. All writes use the collector's locked connection."""
from __future__ import annotations



from . import message_stream as store

IDENTITY = ("chat_id", "sent_at", "thread_id", "root_id", "parent_id")
_STICKY = ("reference_changed", "reference_invalid", "policy_changed", "evidence_deleted", "identity_conflict")


async def create_session(conn, source_id, generation, window_from, window_to, baseline_seq, *,
                         policy_digest, reference_digest, stored_only=0):
    return await conn.fetchval(
        "INSERT INTO source_parity_sessions(source_id,generation,window_from,window_to,baseline_seq,policy_digest,"
        "reference_digest,stored_only) VALUES($1,$2,$3,$4,$5,$6,$7,$8) RETURNING id",
        source_id, generation, window_from, window_to, baseline_seq, policy_digest, reference_digest, stored_only)


async def put_members(conn, session_id, side, rows):
    session = await conn.fetchrow("SELECT * FROM source_parity_sessions WHERE id=$1", session_id)
    if not session or session["reason"] in _STICKY:
        return
    await store._mutation_lock(conn, session["source_id"])
    session = await conn.fetchrow("SELECT * FROM source_parity_sessions WHERE id=$1 FOR UPDATE", session_id)
    if session["reason"] in _STICKY:
        return
    candidates = [row for row in rows if session["window_from"] <= row["sent_at"] < session["window_to"]]
    deleted = await store.deleted_ids(conn, session["source_id"], candidates)
    unique = {}
    conflict = False
    for row in candidates:
        if row["external_id"] in deleted:
            continue
        old = unique.setdefault(row["external_id"], row)
        conflict |= any(old.get(key) != row.get(key) for key in IDENTITY)
    for start in range(0, len(unique), 500):
        batch = list(unique.values())[start:start + 500]
        existing = {row["external_id"]: row for row in await conn.fetch(
            "SELECT * FROM source_parity_members WHERE session_id=$1 AND side=$2 AND external_id=ANY($3::text[])",
            session_id, side, [row["external_id"] for row in batch])}
        for row in batch:
            old = existing.get(row["external_id"])
            if old and any(old[key] != row.get(key) for key in IDENTITY):
                conflict = True
        await conn.executemany(
            "INSERT INTO source_parity_members(session_id,side,external_id,chat_id,sent_at,thread_id,root_id,parent_id) "
            "SELECT $1,$2,$3,$4,$5,$6,$7,$8 WHERE EXISTS (SELECT 1 FROM source_chats c JOIN source_parity_sessions s "
            "ON c.source_id=s.source_id WHERE s.id=$1 AND c.chat_id=$4 AND c.inclusion='included' AND c.purged_at IS NULL) "
            "ON CONFLICT DO NOTHING", [(session_id, side, row["external_id"], *(row.get(k) for k in IDENTITY)) for row in batch])
    if conflict:
        await conn.execute("UPDATE source_parity_sessions SET state='incomplete',reason='identity_conflict' WHERE id=$1", session_id)


async def set_coverage(conn, session_id, chat_id, container_kind, container_id, window_from, window_to, *, state="pending", error=None):
    await conn.execute(
        "INSERT INTO source_parity_coverage(session_id,chat_id,container_kind,container_id,window_from,window_to,state,error) "
        "VALUES($1,$2,$3,$4,$5,$6,$7,$8) ON CONFLICT(session_id,container_kind,container_id) DO UPDATE "
        "SET state=EXCLUDED.state,error=EXCLUDED.error,window_from=EXCLUDED.window_from,window_to=EXCLUDED.window_to",
        session_id, chat_id, container_kind, container_id, window_from, window_to, state, error)


async def iter_sessions(conn, source_id):
    after = 0
    while True:
        rows = await conn.fetch("SELECT * FROM source_parity_sessions WHERE source_id=$1 AND id>$2 ORDER BY id LIMIT 500",
                                source_id, after)
        if not rows:
            return
        for row in rows:
            yield dict(row)
        after = rows[-1]["id"]


_JOIN = """WITH b AS (SELECT * FROM source_parity_members WHERE session_id=$1 AND side='bridge'),
 c AS (SELECT * FROM source_parity_members WHERE session_id=$1 AND side='collect'),
 pairs AS (SELECT COALESCE(b.external_id,c.external_id) AS external_id, COALESCE(b.chat_id,c.chat_id) AS chat_id,
   COALESCE(b.sent_at,c.sent_at) AS sent_at,b.external_id IS NOT NULL AS bridge,c.external_id IS NOT NULL AS collect,
   b.external_id IS NOT NULL AND c.external_id IS NOT NULL AS intersection,
   b.external_id IS NOT NULL AND c.external_id IS NULL AS bridge_only,
   c.external_id IS NOT NULL AND b.external_id IS NULL AS collect_only,
   b.external_id IS NOT NULL AND c.external_id IS NOT NULL AND
     ROW(b.chat_id,b.sent_at,b.thread_id,b.root_id,b.parent_id) IS DISTINCT FROM
     ROW(c.chat_id,c.sent_at,c.thread_id,c.root_id,c.parent_id) AS identity_conflicts
 FROM b FULL JOIN c USING(external_id)) """
_AGG = ",".join(f"count(*) FILTER(WHERE {key})::bigint AS {key}" for key in
                ("bridge", "collect", "intersection", "bridge_only", "collect_only", "identity_conflicts"))


async def report_rows(conn, session_id, *, limit=100, after=None):
    """Totals cover the whole window; only ordered buckets use a bounded keyset page."""
    totals = dict(await conn.fetchrow(_JOIN + f"SELECT {_AGG} FROM pairs", session_id))
    coverage = dict(await conn.fetchrow(
        "SELECT count(*)::int AS total,count(*) FILTER(WHERE state='complete')::int AS complete,"
        "count(*) FILTER(WHERE state='pending')::int AS pending,count(*) FILTER(WHERE state='failed')::int AS failed "
        "FROM source_parity_coverage WHERE session_id=$1", session_id))
    bucket_cte = ", entries AS (SELECT b.chat_id,b.sent_at,b.external_id,'bridge' AS side FROM b UNION ALL " \
        "SELECT c.chat_id,c.sent_at,c.external_id,'collect' AS side FROM c), bucket_ids AS (" \
        "SELECT e.chat_id,date_trunc('hour',e.sent_at AT TIME ZONE 'UTC') AT TIME ZONE 'UTC' AS hour," \
        "e.external_id,bool_or(side='bridge') AS bridge,bool_or(side='collect') AS collect," \
        "bool_or(p.identity_conflicts) AS identity_conflicts FROM entries e JOIN pairs p USING(external_id) " \
        "GROUP BY e.chat_id,hour,e.external_id), buckets AS (SELECT *,bridge AND collect AS intersection," \
        "bridge AND NOT collect AS bridge_only,collect AND NOT bridge AS collect_only FROM bucket_ids) "
    buckets = [dict(row) for row in await conn.fetch(_JOIN + bucket_cte +
        f"SELECT chat_id,hour,{_AGG} FROM buckets GROUP BY chat_id,hour "
        "HAVING ($2::text IS NULL OR (chat_id,hour)>($2,$3)) ORDER BY chat_id,hour LIMIT $4",
        session_id, after[0] if after else None, after[1] if after else None, limit + 1)]
    samples = {}
    for key in ("bridge_only", "collect_only", "identity_conflicts"):
        samples[key] = [row["external_id"] for row in await conn.fetch(
            _JOIN + f"SELECT external_id FROM pairs WHERE {key} ORDER BY external_id LIMIT 20", session_id)]
    return totals, coverage, buckets, samples


async def prune_sessions(conn, source_id, *, before):
    result = await conn.execute(
        "DELETE FROM source_parity_sessions s WHERE source_id=$1 AND state='matched' AND finished_at<$2 "
        "AND NOT EXISTS (SELECT 1 FROM source_stream_alerts a WHERE a.parity_session_id=s.id AND a.resolved_at IS NULL)",
        source_id, before)
    return int(result.rsplit(" ", 1)[-1])


async def delete_evidence(conn, source_id, scope, *, message_id=None, chat_id=None, start=None, end=None):
    """Called inside the deletion transaction after its mutation lock; discard the identifiers, never mask a gap."""
    where, args = store._deletion_where(scope, message_id, chat_id, start, end)
    affected = await conn.fetch(
        f"SELECT DISTINCT m.session_id,m.chat_id FROM source_parity_members m JOIN source_parity_sessions s "
        f"ON s.id=m.session_id WHERE s.source_id=$1 AND ({where})", source_id, *args)
    session_ids = list({row["session_id"] for row in affected})
    if scope in ("range", "chat"):
        # Even a currently empty window loses its reference policy when a range/chat is purged.
        extra = await conn.fetch("SELECT id FROM source_parity_sessions WHERE source_id=$1 "
            "AND ($2::timestamptz IS NULL OR (window_from<$3 AND window_to>$2))", source_id, start, end)
        session_ids = list(set(session_ids) | {r["id"] for r in extra})
    if not session_ids:
        return
    await conn.execute(f"DELETE FROM source_parity_members m USING source_parity_sessions s "
        f"WHERE s.id=m.session_id AND s.source_id=$1 AND ({where})", source_id, *args)
    if chat_id is not None:
        await conn.execute("DELETE FROM source_parity_coverage WHERE session_id=ANY($1::bigint[]) AND chat_id=$2", session_ids, chat_id)
    else:
        for row in affected:
            await conn.execute("DELETE FROM source_parity_coverage WHERE session_id=$1 AND chat_id=$2", row["session_id"], row["chat_id"])
    await conn.execute("UPDATE source_parity_sessions SET state='incomplete',reason='evidence_deleted',finished_at=NULL,"
        "reference_total=(SELECT count(*) FROM source_parity_members m WHERE m.session_id=source_parity_sessions.id AND side='bridge'),"
        "observed_total=(SELECT count(*) FROM source_parity_members m WHERE m.session_id=source_parity_sessions.id AND side='collect') "
        "WHERE id=ANY($1::bigint[])", session_ids)
