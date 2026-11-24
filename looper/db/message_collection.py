"""Collection-only settings and progress; old source/run projections stay unchanged."""
from __future__ import annotations

from contextlib import asynccontextmanager

from datetime import datetime, timedelta, timezone

import asyncpg

from . import message_stream as store

DEFAULTS = {"collect_cron": "*/10 * * * *", "alerts_enabled": False, "generation": 0,
            "mode_changed_at": None, "collect_enabled_at": None, "last_collect_ok_at": None, "last_fire_at": None}
_SOURCE_FIELDS = frozenset(("lark_profile", "user_open_id", "sweep_cycle_runs", "alert_channel", "alert_chat"))
_SETTINGS_FIELDS = frozenset(("collect_cron", "alerts_enabled"))


class CollectionConflict(ValueError):
    """A busy writer or changed prerequisite prevents this operation without a partial write."""


@asynccontextmanager
async def writer(db, source_id):
    target = store._db(db)
    pooled = isinstance(target, asyncpg.Pool)
    conn = await target.acquire() if pooled else target
    locked = False
    try:
        locked = await store.try_lock(conn, source_id)
        if not locked:
            raise CollectionConflict("busy")
        yield conn
    finally:
        try:
            if locked and not conn.is_closed():
                await store.unlock(conn, source_id)
        finally:
            if pooled:
                await target.release(conn)


async def get_settings(source_id, *, db=None):
    row = await store._db(db).fetchrow("SELECT * FROM source_collection_settings WHERE source_id=$1", source_id)
    return dict(row) if row else {"source_id": source_id, **DEFAULTS}


async def collector_source(source_id, *, db=None):
    row = await store._db(db).fetchrow("SELECT * FROM event_sources WHERE id=$1", source_id)
    if row is None:
        return None
    counts = await store._db(db).fetchrow(
        "SELECT COALESCE(sum(events_new),0)::bigint AS new_ids,COALESCE(sum(sweep_found),0)::bigint AS sweep_ids "
        "FROM source_runs WHERE source_id=$1 AND mode='collect' AND finished_at>=now()-interval '24 hours'", source_id)
    return {**dict(row), **await get_settings(source_id, db=db),
            "new_ids_last_day": counts["new_ids"], "sweep_ids_last_day": counts["sweep_ids"],
            "unindexed_ratio": counts["sweep_ids"] / counts["new_ids"] if counts["new_ids"] else None}


async def save_settings(source_id, fields, *, db=None):
    from ..agents import cron
    from ..agents.channels.lark_cli import profile_name

    if set(fields) - _SOURCE_FIELDS - _SETTINGS_FIELDS:
        raise ValueError("unknown collection setting")
    if "lark_profile" in fields:
        profile_name(fields["lark_profile"])
    if "collect_cron" in fields:
        cron.parse(fields["collect_cron"])
    if "sweep_cycle_runs" in fields and (isinstance(fields["sweep_cycle_runs"], bool) or not 1 <= fields["sweep_cycle_runs"] <= 288):
        raise ValueError("sweep_cycle_runs must be between 1 and 288")
    async with writer(db, source_id) as conn, conn.transaction():
        source = await collector_source(source_id, db=conn)
        if source is None:
            raise LookupError("source not found")
        if source["ingest_mode"] == "collect" and any(
                field in fields and fields[field] != source[field] for field in ("lark_profile", "user_open_id")):
            raise CollectionConflict("switch to bridge before changing profile or identity")
        await conn.execute("INSERT INTO source_collection_settings(source_id) VALUES($1) ON CONFLICT DO NOTHING", source_id)
        for field in _SOURCE_FIELDS & fields.keys():
            await conn.execute(f"UPDATE event_sources SET {field}=$2,updated_at=now() WHERE id=$1", source_id, fields[field])
        for field in _SETTINGS_FIELDS & fields.keys():
            await conn.execute(f"UPDATE source_collection_settings SET {field}=$2 WHERE source_id=$1", source_id, fields[field])
        return await collector_source(source_id, db=conn)

async def claim_fire(source_id, generation, fire_at, *, db=None):
    return bool(await store._db(db).fetchval(
        "UPDATE source_collection_settings cs SET last_fire_at=$3 FROM event_sources s WHERE cs.source_id=$1 "
        "AND cs.generation=$2 AND s.id=cs.source_id AND s.ingest_mode='collect' "
        "AND (cs.last_fire_at IS NULL OR cs.last_fire_at<$3) RETURNING true", source_id, int(generation), fire_at))


async def switch_mode(source_id, ingest_mode, *, expected_generation=None, bridge_stopped=False,
                      own_app_ids=frozenset(), now=None, db=None):
    from ..agents import message_stream_parity as parity
    from ..agents.channels.lark_cli import profile_name
    from . import message_parity

    now = now or datetime.now(timezone.utc)
    if ingest_mode not in ("off", "bridge", "collect"):
        raise ValueError("invalid ingest mode")
    async with writer(db, source_id) as conn, conn.transaction():
        source = await collector_source(source_id, db=conn)
        if source is None:
            raise LookupError("source not found")
        if ingest_mode == "bridge" and not source["bridge_dir"]:
            raise CollectionConflict("bridge directory is required")
        generation = source["generation"]
        if ingest_mode == "collect":
            if bridge_stopped is not True:
                raise CollectionConflict("bridge_stopped attestation is required")
            if expected_generation is None or int(expected_generation) != generation:
                raise CollectionConflict("generation changed")
            try:
                if not profile_name(source["lark_profile"]):
                    raise ValueError("explicit profile required")
            except ValueError as exc:
                raise CollectionConflict("an explicit profile is required") from exc
            if not source["user_open_id"]:
                raise CollectionConflict("a bound user identity is required")
            reference = await parity.capture_bridge_reference(conn, source, now=now, own_app_ids=own_app_ids)
        await conn.execute("INSERT INTO source_collection_settings(source_id) VALUES($1) ON CONFLICT DO NOTHING", source_id)
        generation += 1
        session_id = None
        if ingest_mode == "collect":
            session_id = await message_parity.create_session(conn, source_id, generation,
                now - timedelta(days=1), now, source["committed_seq"], policy_digest=reference["policy_digest"],
                reference_digest=reference["reference_digest"], stored_only=reference["stored_only"])
            await message_parity.put_members(conn, session_id, "bridge", reference["members"])
            for chat_id, container_kind, container_id in reference["containers"]:
                await message_parity.set_coverage(conn, session_id, chat_id, container_kind, container_id,
                                                 now - timedelta(days=1), now)
            await conn.execute("UPDATE source_parity_sessions SET reference_total=$2 WHERE id=$1", session_id, len(reference["members"]))
            await conn.execute("UPDATE event_sources SET watermark_at=$2 WHERE id=$1", source_id, now - timedelta(days=1))
            await conn.execute("UPDATE source_collection_settings SET collect_enabled_at=$2,last_collect_ok_at=NULL,last_fire_at=NULL WHERE source_id=$1", source_id, now)
        await conn.execute("UPDATE event_sources SET ingest_mode=$2,updated_at=$3 WHERE id=$1", source_id, ingest_mode, now)
        await conn.execute("UPDATE source_collection_settings SET generation=$2,mode_changed_at=$3 WHERE source_id=$1", source_id, generation, now)
        return {"source_id": source_id, "mode": ingest_mode, "generation": str(generation),
                "parity_session_id": str(session_id) if session_id is not None else None}

async def save_chat_progress(conn, source_id, chat_id, *, cursor_time, cursor_ids, now):
    await conn.execute(
        "UPDATE source_chats SET cursor_ids=CASE WHEN cursor_time=$3 THEN "
        "ARRAY(SELECT DISTINCT x FROM unnest(cursor_ids || $4::text[]) AS x ORDER BY x) "
        "WHEN cursor_time IS NULL OR cursor_time<$3 THEN $4::text[] ELSE cursor_ids END,"
        "cursor_time=GREATEST(cursor_time,$3),last_message_at=GREATEST(last_message_at,$3),last_pulled_at=$5,"
        "failures=0,last_error=NULL,last_error_code=NULL,state='active' WHERE source_id=$1 AND chat_id=$2",
        source_id, chat_id, cursor_time, sorted(set(cursor_ids)), now)


async def save_thread_progress(conn, source_id, chat_id, thread_id, *, cursor_time, cursor_ids, now):
    await conn.execute(
        "INSERT INTO source_threads(source_id,chat_id,thread_id,cursor_time,cursor_ids,last_pulled_at) VALUES($1,$2,$3,$4,$5,$6) "
        "ON CONFLICT(source_id,thread_id) DO UPDATE SET cursor_ids=CASE WHEN source_threads.cursor_time=EXCLUDED.cursor_time THEN "
        "ARRAY(SELECT DISTINCT x FROM unnest(source_threads.cursor_ids || EXCLUDED.cursor_ids) AS x ORDER BY x) "
        "WHEN source_threads.cursor_time IS NULL OR source_threads.cursor_time<EXCLUDED.cursor_time THEN EXCLUDED.cursor_ids ELSE source_threads.cursor_ids END,"
        "cursor_time=GREATEST(source_threads.cursor_time,EXCLUDED.cursor_time),last_pulled_at=EXCLUDED.last_pulled_at,failures=0,last_error=NULL",
        source_id, chat_id, thread_id, cursor_time, sorted(set(cursor_ids)), now)


async def finish_collect_run(conn, run_id, status, *, now, error=None, error_code=None, source_id=None, counters=None, watermark=None):
    allowed = {"slices", "search_hits", "unindexed", "chats_pulled", "chats_failed", "threads_pulled", "threads_failed",
               "sweep_chats", "sweep_failed", "sweep_found", "events_new", "events_seen", "events_skipped", "skipped_by_deletion"}
    counters = counters or {}
    if set(counters) - allowed:
        raise ValueError("unknown collect counter")
    fields = ["status=$2", "finished_at=$3", "error=$4", "error_code=$5", "watermark_after=$6"]
    values = [run_id, status, now, error, error_code, watermark]
    for key, value in counters.items():
        values.append(value)
        fields.append(f"{key}=${len(values)}")
    await conn.execute(f"UPDATE source_runs SET {','.join(fields)} WHERE id=$1", *values)
    if source_id and watermark is not None:
        await conn.execute("UPDATE event_sources SET watermark_at=GREATEST(watermark_at,$2) WHERE id=$1", source_id, watermark)
    if status == "ok" and source_id:
        await conn.execute("UPDATE source_collection_settings SET last_collect_ok_at=$2 WHERE source_id=$1", source_id, now)
