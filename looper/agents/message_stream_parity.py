"""Frozen bridge references and independent collect identities; reports never call a platform or processor."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

import asyncpg

from ..db import message_parity as evidence, message_stream as store
from ..db.message_collection import CollectionConflict
from .bridge_reader import baseline_file, scan_file

_MAX_REFERENCE_IDS = 100_000
_MAX_REFERENCE_BYTES = 256 * 1024 * 1024
_KEYS = ("chat_id", "sent_at", "thread_id", "root_id", "parent_id")


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


async def policy_digest(conn, source_id, own_app_ids):
    source = await conn.fetchrow("SELECT user_open_id FROM event_sources WHERE id=$1", source_id)
    chats = await conn.fetch("SELECT chat_id,inclusion,purged_at FROM source_chats WHERE source_id=$1 AND (inclusion='excluded' OR purged_at IS NOT NULL) ORDER BY chat_id", source_id)
    tombstones = await conn.fetch("SELECT scope,message_id,chat_id,start_at,end_at FROM source_deletions WHERE source_id=$1 ORDER BY id", source_id)
    return _digest({"user_open_id": source["user_open_id"], "own_apps": sorted(own_app_ids),
                    "chats": [dict(row) for row in chats], "deletions": [dict(row) for row in tombstones]})


def _snapshot(directory, start, end, chats, own_app_ids):
    """Bounded chunks and a fixed deadline. No body or path survives the returned snapshot."""
    deadline = time.monotonic() + 20
    root = Path(directory)
    if not directory or not root.is_dir() or not (root / "chats").is_dir():
        raise CollectionConflict("reference_invalid")
    paths = sorted((root / "chats").glob("*.ndjson"))
    def signature(path):
        stat = path.stat()
        return stat.st_ino, stat.st_size, stat.st_mtime_ns
    before = [(path.name, signature(path)) for path in paths]
    total_bytes = sum(item[1][1] for item in before)
    if total_bytes > _MAX_REFERENCE_BYTES:
        raise CollectionConflict("reference_invalid")
    members = {}
    containers = set()
    metadata = []
    for path in paths:
        chat_id = path.stem
        chat = chats.get(chat_id)
        state = None
        messages = []
        own_p2p = False
        while True:
            if time.monotonic() >= deadline:
                raise CollectionConflict("reference_invalid")
            scan = scan_file(path, state, max_bytes=8 * 1024 * 1024)
            if scan.bad_lines or scan.incomplete_tail:
                raise CollectionConflict("reference_invalid")
            if scan.rescanned or (state and scan.state.offset <= state.offset):
                raise CollectionConflict("reference_changed")
            own_p2p |= bool(chat and chat["kind"] == "p2p" and any(
                m.sender_kind == "app" and m.sender_id in own_app_ids for m in scan.messages))
            for message in scan.messages:
                if start <= message.sent_at < end and not (message.sender_kind == "app" and message.sender_id in own_app_ids):
                    messages.append({"external_id": message.external_id, "chat_id": chat_id,
                        "sent_at": message.sent_at, "thread_id": message.thread_id, "root_id": message.root_id,
                        "parent_id": message.parent_id})
            if len(messages) > _MAX_REFERENCE_IDS:
                raise CollectionConflict("reference_invalid")
            state = scan.state
            if not scan.more:
                break
        final = baseline_file(path)
        if final.offset != path.stat().st_size or state.offset != final.offset or state.last_id != final.last_id:
            raise CollectionConflict("reference_changed")
        metadata.append((path.name, final.inode, final.size, str(final.mtime), final.last_id))
        if own_p2p or (chat and (chat["inclusion"] != "included" or chat["purged_at"] is not None)):
            continue
        for row in messages:
            old = members.setdefault(row["external_id"], row)
            # The first copy wins, but contradictory identities make a reference unusable.
            if any(old[key] != row[key] for key in _KEYS):
                raise CollectionConflict("identity_conflict")
            containers.add((chat_id, "chat", chat_id))
            if row["thread_id"]:
                containers.add((chat_id, "thread", row["thread_id"]))
        if len(members) > _MAX_REFERENCE_IDS:
            raise CollectionConflict("reference_invalid")
    after_paths = sorted((root / "chats").glob("*.ndjson"))
    if before != [(path.name, signature(path)) for path in after_paths]:
        raise CollectionConflict("reference_changed")
    return list(members.values()), sorted(containers), _digest(metadata)


async def capture_bridge_reference(conn, source, *, now, own_app_ids=frozenset()):
    await store._mutation_lock(conn, source["id"])
    chats = {row["chat_id"]: dict(row) for row in await conn.fetch("SELECT * FROM source_chats WHERE source_id=$1", source["id"])}
    try:
        members, containers, reference_digest = await asyncio.wait_for(asyncio.to_thread(
            _snapshot, source["bridge_dir"], now - timedelta(days=1), now, chats, own_app_ids), 25)
    except (OSError, TimeoutError) as exc:
        raise CollectionConflict("reference_invalid") from exc
    deleted = await store.deleted_ids(conn, source["id"], members)
    members = [row for row in members if row["external_id"] not in deleted]
    allowed_containers = {(row["chat_id"], "chat", row["chat_id"]) for row in members}
    allowed_containers |= {(row["chat_id"], "thread", row["thread_id"]) for row in members if row["thread_id"]}
    containers = [container for container in containers if container in allowed_containers]
    existing = set()
    for start in range(0, len(members), 500):
        existing.update(row["external_id"] for row in await conn.fetch(
            "SELECT external_id FROM source_events WHERE source_id=$1 AND seq<=$2 AND external_id=ANY($3::text[])",
            source["id"], source["committed_seq"], [row["external_id"] for row in members[start:start + 500]]))
    if len(existing) != len(members):
        raise CollectionConflict("reference_invalid: bridge baseline has archive_only messages; let bridge finish first")
    stored = await conn.fetchval(
        "SELECT count(*) FROM source_events e JOIN source_chats c USING(source_id,chat_id) WHERE e.source_id=$1 "
        "AND e.seq<=$2 AND e.sent_at>=$3 AND e.sent_at<$4 AND c.inclusion='included' AND c.purged_at IS NULL "
        "AND NOT(e.sender_kind='app' AND e.sender_id=ANY($5::text[]))", source["id"], source["committed_seq"],
        now - timedelta(days=1), now, list(own_app_ids))
    return {"members": members, "containers": containers, "reference_digest": reference_digest,
            "policy_digest": await policy_digest(conn, source["id"], own_app_ids), "stored_only": max(0, stored - len(existing))}


async def observe_collect(conn, source_id, chat_id, rows, containers, window_from, window_to, *, own_app_ids=frozenset()):
    await store._mutation_lock(conn, source_id)
    policy = await policy_digest(conn, source_id, own_app_ids)
    async for session in evidence.iter_sessions(conn, source_id):
        if session["reason"] in evidence._STICKY:
            continue
        if session["policy_digest"] != policy:
            await conn.execute("UPDATE source_parity_sessions SET state='incomplete',reason='policy_changed' WHERE id=$1", session["id"])
            continue
        if window_to <= session["window_from"] or window_from >= session["window_to"]:
            continue
        full = window_from <= session["window_from"] and window_to >= session["window_to"]
        if full and all(container.get("complete", True) for container in containers):
            # Check old identities before replacing this container's set; an ID changing identity is never a repair.
            await evidence.put_members(conn, session["id"], "collect", rows)
            reason = await conn.fetchval("SELECT reason FROM source_parity_sessions WHERE id=$1", session["id"])
            if reason == "identity_conflict":
                continue
            # A successful full re-read replaces this chat's observations. Omitted IDs must not survive a previous run.
            await conn.execute("DELETE FROM source_parity_members WHERE session_id=$1 AND side='collect' AND chat_id=$2", session["id"], chat_id)
        await evidence.put_members(conn, session["id"], "collect", rows)
        for container in containers:
            succeeded = container.get("complete", True)
            if not full and succeeded:
                existing = await conn.fetchval("SELECT state FROM source_parity_coverage WHERE session_id=$1 "
                    "AND container_kind=$2 AND container_id=$3", session["id"], container["container_kind"], container["container_id"])
                if existing == "complete":
                    continue
            state = "complete" if full and succeeded else "pending" if succeeded else "failed"
            await evidence.set_coverage(conn, session["id"], chat_id, container["container_kind"], container["container_id"],
                session["window_from"], session["window_to"], state=state)


def _empty(source_id):
    return {"version": 1, "source": source_id, "session": None, "window": None, "baseline_seq": None,
            "state": "pending", "empty_window": True, "coverage": dict.fromkeys(("total", "complete", "pending", "failed"), 0),
            "totals": dict.fromkeys(("bridge", "collect", "intersection", "bridge_only", "collect_only", "identity_conflicts", "stored_only"), 0),
            "buckets": [], "samples": {"bridge_only": [], "collect_only": [], "identity_conflicts": []},
            "reason": "coverage_missing", "next_cursor": None}


def _positive_id(value):
    text = str(value)
    if not re.fullmatch(r"[1-9][0-9]{0,18}", text) or int(text) > 2**63 - 1:
        raise ValueError("session must be a positive decimal BIGINT")
    return int(text)


def _decode_cursor(cursor, session):
    if not cursor:
        return None
    try:
        if len(cursor) > 2048 or not re.fullmatch(r"[A-Za-z0-9_-]+", cursor):
            raise ValueError
        payload = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        if set(payload) != {"v", "session", "generation", "chat_id", "hour"} or type(payload["v"]) is not int or payload["v"] != 1:
            raise ValueError
        if payload["session"] != str(session["id"]) or payload["generation"] != str(session["generation"]):
            raise ValueError
        if not isinstance(payload["chat_id"], str) or not isinstance(payload["hour"], str):
            raise ValueError
        hour = datetime.fromisoformat(payload["hour"])
        if hour.tzinfo is None or hour.utcoffset() != timedelta(0) or hour.minute or hour.second or hour.microsecond:
            raise ValueError
        return payload["chat_id"], hour
    except (ValueError, TypeError, KeyError, UnicodeError) as exc:
        raise ValueError("invalid parity cursor") from exc


async def _report(conn, source_id, session_id=None, *, limit=100, cursor=None):
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise ValueError("limit must be between 1 and 200")
    if not await conn.fetchval("SELECT EXISTS(SELECT 1 FROM event_sources WHERE id=$1)", source_id):
        raise LookupError("source not found")
    identity = _positive_id(session_id) if session_id is not None else None
    session = await conn.fetchrow("SELECT * FROM source_parity_sessions WHERE source_id=$1 "
        "AND ($2::bigint IS NULL OR id=$2) ORDER BY id DESC LIMIT 1", source_id, identity)
    if session is None:
        if identity is not None:
            raise LookupError("parity session not found")
        if cursor:
            raise ValueError("cursor requires a parity session")
        return _empty(source_id)
    after = _decode_cursor(cursor, session)
    totals, coverage, buckets, samples = await evidence.report_rows(conn, session["id"], limit=limit, after=after)
    totals["stored_only"] = session["stored_only"]
    next_cursor = None
    if len(buckets) > limit:
        last = buckets[limit - 1]
        payload = {"v": 1, "session": str(session["id"]), "generation": str(session["generation"]),
                   "chat_id": last["chat_id"], "hour": last["hour"].isoformat()}
        next_cursor = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    buckets = [{**row, "hour": row["hour"].isoformat()} for row in buckets[:limit]]
    return {"version": 1, "source": source_id, "session": str(session["id"]),
            "window": {"from": session["window_from"].isoformat(), "to": session["window_to"].isoformat()},
            "baseline_seq": str(session["baseline_seq"]), "state": session["state"],
            "empty_window": not (totals["bridge"] or totals["collect"]), "coverage": coverage, "totals": totals,
            "buckets": buckets, "samples": samples, "reason": session["reason"], "next_cursor": next_cursor}


async def read_report(source_id, session_id=None, *, limit=100, cursor=None, db=None):
    target = store._db(db)
    if isinstance(target, asyncpg.Pool):
        async with target.acquire() as conn, conn.transaction(isolation="repeatable_read", readonly=True):
            return await _report(conn, source_id, session_id, limit=limit, cursor=cursor)
    if target.is_in_transaction():
        return await _report(target, source_id, session_id, limit=limit, cursor=cursor)
    async with target.transaction(isolation="repeatable_read", readonly=True):
        return await _report(target, source_id, session_id, limit=limit, cursor=cursor)


async def finalize_session(conn, session_id):
    if not conn.is_in_transaction():
        async with conn.transaction(isolation="repeatable_read"):
            return await finalize_session(conn, session_id)
    session = await conn.fetchrow("SELECT * FROM source_parity_sessions WHERE id=$1 FOR UPDATE", session_id)
    if session is None:
        raise LookupError("parity session not found")
    totals, coverage, _, _ = await evidence.report_rows(conn, session_id)
    reason = session["reason"]
    if reason in evidence._STICKY:
        state = "incomplete"
    elif coverage["pending"] or coverage["failed"]:
        state, reason = "incomplete", "coverage_missing"
    elif any(totals[key] for key in ("bridge_only", "collect_only", "identity_conflicts")):
        state, reason = "drift", None
    else:
        state, reason = "matched", None
    await conn.execute("UPDATE source_parity_sessions SET state=$2,reason=$3,finished_at=CASE WHEN $2 IN ('matched','drift') "
        "THEN now() ELSE NULL END,reference_total=$4,observed_total=$5 WHERE id=$1", session_id, state, reason, totals["bridge"], totals["collect"])
    return await _report(conn, session["source_id"], session_id)


async def finalize_source(conn, source_id):
    finalized = 0
    async for session in evidence.iter_sessions(conn, source_id):
        await finalize_session(conn, session["id"])
        finalized += 1
    return finalized


def format_report(report):
    if report["session"] is None:
        return f"source {report['source']}: pending (no frozen bridge reference)"
    totals, coverage = report["totals"], report["coverage"]
    lines = [f"source {report['source']} session {report['session']}: {report['state']}",
        f"  window: [{report['window']['from']}, {report['window']['to']}); baseline seq {report['baseline_seq']}",
        f"  reference {totals['bridge']}, observed {totals['collect']}, intersection {totals['intersection']}; "
        f"bridge only {totals['bridge_only']}, collect only {totals['collect_only']}, identity conflicts {totals['identity_conflicts']}, stored only {totals['stored_only']}",
        f"  coverage: {coverage['complete']}/{coverage['total']} complete, {coverage['pending']} pending, {coverage['failed']} failed"]
    if report["empty_window"]:
        lines.append("  empty_window: true (does not prove collection)")
    if report["reason"]:
        lines.append(f"  reason: {report['reason']}")
    for bucket in report["buckets"]:
        lines.append(f"  {bucket['chat_id']} {bucket['hour']}: reference {bucket['bridge']}, observed {bucket['collect']}, "
            f"bridge only {bucket['bridge_only']}, collect only {bucket['collect_only']}, identity conflicts {bucket['identity_conflicts']}")
    for kind, ids in report["samples"].items():
        if ids:
            lines.append(f"  {kind}: {', '.join(ids)}")
    if report["next_cursor"]:
        lines.append(f"  next_cursor: {report['next_cursor']}")
    return "\n".join(lines)


async def _cli_report(database_url, args):
    import sys
    from ..db.connection import close_pool, init_pool

    pool = await init_pool(database_url, min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn, conn.transaction(isolation="repeatable_read", readonly=True):
            if not await conn.fetchval("SELECT to_regclass('source_parity_sessions') IS NOT NULL "
                "AND to_regclass('source_parity_members') IS NOT NULL AND to_regclass('source_parity_coverage') IS NOT NULL"):
                print("collection migration is unavailable; start looper serve once to upgrade", file=sys.stderr)
                return 6
            sources = await conn.fetch("SELECT id FROM event_sources ORDER BY id LIMIT 2")
            source_id = args.source
            if source_id is None:
                if len(sources) != 1:
                    raise ValueError("name an existing, unambiguous source with --source")
                source_id = sources[0]["id"]
            report = await _report(conn, source_id, args.session, limit=args.limit, cursor=args.cursor)
        print(json.dumps(report, sort_keys=True) if args.json else format_report(report))
        return {"matched": 0, "drift": 1, "pending": 3, "incomplete": 3}[report["state"]]
    finally:
        await close_pool()


def run_cli(args, load_ops):
    import sys
    try:
        if args.session is not None:
            _positive_id(args.session)
        if not 1 <= args.limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        ops = load_ops(args)
        if ops is None:
            return 6
        return asyncio.run(_cli_report(ops.database_url, args))
    except (ValueError, LookupError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (asyncpg.PostgresError, OSError, TimeoutError):
        print("parity database is unavailable or its collection migration is missing", file=sys.stderr)
        return 6
