"""Independent identity observations, coverage and deletion; synthetic messages only."""
from datetime import timedelta

import pytest

from looper.agents import message_stream_parity as parity
from looper.agents.source_service import run_bridge_pass
from looper.db import message_collection as collection, message_stream as store
from tests import msgflow_support as sup
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


async def setup(pool, directory):
    await store.save_source("src", {"ingest_mode": "bridge", "bridge_dir": str(directory),
        "bridge_start_from": "beginning", "lark_profile": "synthetic", "user_open_id": sup.ME}, db=pool)
    source = await store.get_source("src", db=pool)
    await run_bridge_pass(source, db=pool, now=sup.NOW)
    result = await collection.switch_mode("src", "collect", expected_generation=0, bridge_stopped=True,
                                        now=sup.NOW, db=pool)
    return int(result["parity_session_id"])


async def observe(pool, ids, *, complete=True):
    rows = [{"external_id": identity, "chat_id": "oc_a", "sent_at": sup.NOW - timedelta(minutes=5),
             "thread_id": None, "root_id": None, "parent_id": None} for identity in ids]
    async with pool.acquire() as conn, conn.transaction():
        await parity.observe_collect(conn, "src", "oc_a", rows,
            [{"container_kind": "chat", "container_id": "oc_a", "complete": complete}],
            sup.NOW - timedelta(days=1), sup.NOW)


def test_duplicate_observations_are_independent_of_insert_results(monkeypatch, tmp_path):
    async def check(pool):
        sup.append(tmp_path, "oc_a", sup.message("m1"), sup.message("m2"))
        session = await setup(pool, tmp_path)
        before = await pool.fetch("SELECT * FROM source_events ORDER BY seq")
        await observe(pool, ["m1", "m2"])
        async with pool.acquire() as conn:
            result = await parity.finalize_session(conn, session)
        assert result["state"] == "matched" and not result["empty_window"]
        assert result["totals"]["bridge"] == result["totals"]["collect"] == 2
        assert await pool.fetch("SELECT * FROM source_events ORDER BY seq") == before
        assert await pool.fetchval("SELECT count(*) FROM source_parity_members WHERE side='collect'") == 2
    run_db(check, monkeypatch, timeout=30)


def test_equal_counts_with_different_ids_are_drift_by_chat_and_hour(monkeypatch, tmp_path):
    async def check(pool):
        sup.append(tmp_path, "oc_a", sup.message("m1"), sup.message("m2"))
        session = await setup(pool, tmp_path)
        await observe(pool, ["m1", "m3"])
        async with pool.acquire() as conn:
            result = await parity.finalize_session(conn, session)
        assert result["state"] == "drift"
        assert result["totals"]["bridge_only"] == result["totals"]["collect_only"] == 1
        assert result["buckets"][0]["hour"] == "2026-11-18T11:00:00+00:00"
        assert result["samples"]["bridge_only"] == ["m2"]
        assert result["samples"]["collect_only"] == ["m3"]
    run_db(check, monkeypatch, timeout=30)


def test_incomplete_coverage_and_empty_reference_never_prove_collection(monkeypatch, tmp_path):
    async def check(pool):
        sup.append(tmp_path, "oc_a", sup.message("m1"))
        session = await setup(pool, tmp_path)
        await observe(pool, ["m1"], complete=False)
        async with pool.acquire() as conn:
            result = await parity.finalize_session(conn, session)
        assert result["state"] == "incomplete" and result["reason"] == "coverage_missing"
        await observe(pool, ["m1"])
        async with pool.acquire() as conn:
            assert (await parity.finalize_session(conn, session))["state"] == "matched"
        await collection.switch_mode("src", "bridge", now=sup.NOW, db=pool)
        path = tmp_path / "chats" / "oc_a.ndjson"
        path.write_bytes(path.read_bytes() + b'{"unfinished":')
        with pytest.raises(collection.CollectionConflict, match="reference_invalid"):
            await collection.switch_mode("src", "collect", expected_generation=2, bridge_stopped=True,
                                         now=sup.NOW, db=pool)
        assert (await store.get_source("src", db=pool))["ingest_mode"] == "bridge"
    run_db(check, monkeypatch, timeout=30)


@pytest.mark.parametrize("scope", ["message", "range", "chat"])
def test_deletion_atomically_removes_identity_evidence_and_invalidates_report(monkeypatch, tmp_path, scope):
    async def check(pool):
        sup.append(tmp_path, "oc_a", sup.message("m1"))
        session = await setup(pool, tmp_path)
        await observe(pool, ["m1"])
        options = {"message_id": "m1"} if scope == "message" else {"chat_id": "oc_a"}
        if scope == "range":
            options.update(start=sup.NOW - timedelta(hours=1), end=sup.NOW)
        await store.delete_events("src", scope, db=pool, **options)
        assert await pool.fetchval("SELECT count(*) FROM source_parity_members") == 0
        assert await pool.fetchval("SELECT count(*) FROM source_parity_coverage") == 0
        await observe(pool, ["m1"])
        async with pool.acquire() as conn:
            result = await parity.finalize_session(conn, session)
        assert result["state"] == "incomplete" and result["reason"] == "evidence_deleted"
        assert await pool.fetchval("SELECT count(*) FROM source_parity_members") == 0
    run_db(check, monkeypatch, timeout=30)


def test_successful_partial_overlap_preserves_full_coverage_but_failure_invalidates_it(monkeypatch, tmp_path):
    async def check(pool):
        sup.append(tmp_path, "oc_a", sup.message("m1"))
        session = await setup(pool, tmp_path)
        await observe(pool, ["m1"])
        async with pool.acquire() as conn, conn.transaction():
            await parity.observe_collect(conn, "src", "oc_a", [],
                [{"container_kind": "chat", "container_id": "oc_a", "complete": True}],
                sup.NOW - timedelta(minutes=1), sup.NOW)
            assert (await parity.finalize_session(conn, session))["state"] == "matched"
            await parity.observe_collect(conn, "src", "oc_a", [],
                [{"container_kind": "chat", "container_id": "oc_a", "complete": False}],
                sup.NOW - timedelta(minutes=1), sup.NOW)
            assert (await parity.finalize_session(conn, session))["state"] == "incomplete"
    run_db(check, monkeypatch, timeout=30)


def test_report_cursor_is_bound_and_totals_cover_unreturned_buckets(monkeypatch, tmp_path):
    async def check(pool):
        sup.append(tmp_path, "oc_a", sup.message("m1"))
        sup.append(tmp_path, "oc_b", sup.message("m2"))
        session = await setup(pool, tmp_path)
        await observe(pool, ["m1"])
        async with pool.acquire() as conn, conn.transaction():
            await parity.observe_collect(conn, "src", "oc_b", [],
                [{"container_kind": "chat", "container_id": "oc_b", "complete": True}],
                sup.NOW - timedelta(days=1), sup.NOW)
            await parity.finalize_session(conn, session)
        first = await parity.read_report("src", session, limit=1, db=pool)
        assert first["state"] == "drift" and first["totals"]["bridge_only"] == 1
        assert first["buckets"][0]["chat_id"] == "oc_a" and first["next_cursor"]
        second = await parity.read_report("src", session, limit=1, cursor=first["next_cursor"], db=pool)
        assert second["buckets"][0]["chat_id"] == "oc_b" and second["totals"] == first["totals"]
        assert second["next_cursor"] is None
        with pytest.raises(ValueError, match="cursor"):
            await parity.read_report("src", session, cursor="not!valid", db=pool)
    run_db(check, monkeypatch, timeout=30)


def test_same_side_identity_change_keeps_original_and_invalidates_session(monkeypatch, tmp_path):
    async def check(pool):
        sup.append(tmp_path, "oc_a", sup.message("m1"))
        session = await setup(pool, tmp_path)
        await observe(pool, ["m1"])
        async with pool.acquire() as conn, conn.transaction():
            changed = {"external_id": "m1", "chat_id": "oc_a", "sent_at": sup.NOW - timedelta(minutes=4),
                       "thread_id": None, "root_id": None, "parent_id": None}
            await parity.observe_collect(conn, "src", "oc_a", [changed],
                [{"container_kind": "chat", "container_id": "oc_a", "complete": True}],
                sup.NOW - timedelta(days=1), sup.NOW)
            result = await parity.finalize_session(conn, session)
        assert result["state"] == "incomplete" and result["reason"] == "identity_conflict"
        assert await pool.fetchval("SELECT sent_at FROM source_parity_members WHERE side='collect'") == sup.NOW - timedelta(minutes=5)
    run_db(check, monkeypatch, timeout=30)


def test_empty_frozen_window_is_marked_without_claiming_nonempty_evidence(monkeypatch, tmp_path):
    async def check(pool):
        (tmp_path / "chats").mkdir()
        session = await setup(pool, tmp_path)
        async with pool.acquire() as conn:
            result = await parity.finalize_session(conn, session)
        assert result["state"] == "matched" and result["empty_window"] is True
        assert result["totals"]["bridge"] == result["totals"]["collect"] == 0
        assert "does not prove collection" in parity.format_report(result)
    run_db(check, monkeypatch, timeout=30)


def test_deletion_evidence_failure_rolls_back_events_tombstone_and_reference(monkeypatch, tmp_path):
    import asyncpg

    async def check(pool):
        sup.append(tmp_path, "oc_a", sup.message("m1"))
        await setup(pool, tmp_path)
        await observe(pool, ["m1"])
        before = await pool.fetch("SELECT * FROM source_parity_members ORDER BY side")
        await pool.execute("CREATE FUNCTION reject_evidence_delete() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'synthetic rollback'; END $$; "
            "CREATE TRIGGER reject_delete BEFORE DELETE ON source_parity_coverage FOR EACH ROW EXECUTE FUNCTION reject_evidence_delete()")
        with pytest.raises(asyncpg.RaiseError):
            await store.delete_events("src", "message", message_id="m1", db=pool)
        assert await pool.fetchval("SELECT count(*) FROM source_events") == 1
        assert await pool.fetchval("SELECT count(*) FROM source_deletions") == 0
        assert await pool.fetch("SELECT * FROM source_parity_members ORDER BY side") == before
    run_db(check, monkeypatch, timeout=30)


def test_pruning_preserves_unresolved_drift_and_active_alert_references(monkeypatch, tmp_path):
    from looper.db import message_parity as evidence, message_stream_alerts as alerts

    async def check(pool):
        sup.append(tmp_path, "oc_a", sup.message("m1"))
        session = await setup(pool, tmp_path)
        await observe(pool, ["m1"])
        async with pool.acquire() as conn, conn.transaction():
            await parity.finalize_session(conn, session)
            await conn.execute("UPDATE source_parity_sessions SET state='drift',finished_at=$2 WHERE id=$1", session, sup.NOW - timedelta(days=2))
            assert await evidence.prune_sessions(conn, "src", before=sup.NOW) == 0
            await conn.execute("UPDATE source_parity_sessions SET state='matched' WHERE id=$1", session)
            await alerts.open_episode(conn, "src", "parity_drift", now=sup.NOW, parity_session_id=session)
            assert await evidence.prune_sessions(conn, "src", before=sup.NOW) == 0
            await alerts.resolve_episode(conn, "src", "parity_drift", now=sup.NOW, parity_session_id=session)
            assert await evidence.prune_sessions(conn, "src", before=sup.NOW) == 1
            assert await conn.fetchval("SELECT count(*) FROM source_parity_members") == 0
            assert await conn.fetchval("SELECT count(*) FROM source_parity_coverage") == 0
    run_db(check, monkeypatch, timeout=30)
