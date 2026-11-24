"""Mode changes share the writer lock and retain existing stream positions."""
from datetime import timedelta

import pytest

from looper.agents.source_service import run_bridge_pass
from looper.db import message_collection as collection, message_stream as store
from tests import msgflow_support as sup
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def test_mode_switch_serializes_with_bridge_collect_and_import(monkeypatch, tmp_path):
    async def check(pool):
        await store.save_source("src", {"ingest_mode": "bridge", "bridge_dir": str(tmp_path),
            "lark_profile": "synthetic", "user_open_id": sup.ME}, db=pool)
        assert await pool.fetchval("SELECT count(*) FROM source_collection_settings") == 0
        async with pool.acquire() as holder:
            assert await store.try_lock(holder, "src")
            try:
                with pytest.raises(collection.CollectionConflict, match="busy"):
                    await collection.switch_mode("src", "off", db=pool, now=sup.NOW)
                with pytest.raises(collection.CollectionConflict, match="busy"):
                    await store.save_source("src", {"ingest_mode": "off"}, db=pool)
            finally:
                await store.unlock(holder, "src")
        with pytest.raises(collection.CollectionConflict, match="bridge_stopped"):
            await collection.switch_mode("src", "collect", expected_generation=0, db=pool, now=sup.NOW)
        with pytest.raises(collection.CollectionConflict, match="generation"):
            await collection.switch_mode("src", "collect", expected_generation=3, bridge_stopped=True, db=pool, now=sup.NOW)
        assert (await store.get_source("src", db=pool))["ingest_mode"] == "bridge"
        assert await pool.fetchval("SELECT count(*) FROM source_collection_settings") == 0
    run_db(check, monkeypatch, timeout=30)


def test_one_setting_rollback_keeps_all_ingest_and_consumer_positions(monkeypatch, tmp_path):
    async def check(pool):
        sup.append(tmp_path, "oc_a", sup.message("m1"))
        await store.save_source("src", {"ingest_mode": "bridge", "bridge_dir": str(tmp_path),
            "bridge_start_from": "beginning", "lark_profile": "synthetic", "user_open_id": sup.ME,
            "cron": "*/2 * * * *"}, db=pool)
        await run_bridge_pass(await store.get_source("src", db=pool), db=pool, now=sup.NOW)
        before = await pool.fetch("SELECT * FROM source_chats")
        events = await pool.fetch("SELECT * FROM source_events")
        source = await store.get_source("src", db=pool)
        switched = await collection.switch_mode("src", "collect", expected_generation=0,
            bridge_stopped=True, now=sup.NOW, db=pool)
        assert switched["generation"] == "1"
        for identity_fields in ({"user_open_id": "ou_changed_synthetic"}, {"lark_profile": "other-synthetic"}):
            with pytest.raises(collection.CollectionConflict, match="identity"):
                await store.save_source("src", identity_fields, db=pool)
        assert (await collection.collector_source("src", db=pool))["generation"] == 1
        # A source dictionary captured before cutover cannot admit either bridge or import writes.
        for options in ({}, {"days": 1}, {"days": 1, "dry_run": True}):
            with pytest.raises(ValueError, match="collection owns"):
                await run_bridge_pass(source, db=pool, now=sup.NOW, **options)
        assert await pool.fetchval("SELECT watermark_at FROM event_sources") == sup.NOW - timedelta(days=1)
        result = await collection.switch_mode("src", "bridge", now=sup.NOW, db=pool)
        assert result["generation"] == "2"
        assert await pool.fetch("SELECT * FROM source_chats") == before
        assert await pool.fetch("SELECT * FROM source_events") == events
        final = await store.get_source("src", db=pool)
        assert final["committed_seq"] == source["committed_seq"] and final["cron"] == "*/2 * * * *"
        sup.append(tmp_path, "oc_a", sup.message("m2"))
        assert (await run_bridge_pass(final, db=pool, now=sup.NOW)).events_new == 1
    run_db(check, monkeypatch, timeout=30)


def test_unbridged_reference_rejects_cutover_without_creating_partial_state(monkeypatch, tmp_path):
    async def check(pool):
        sup.append(tmp_path, "oc_a", sup.message("m1"))
        await store.save_source("src", {"ingest_mode": "bridge", "bridge_dir": str(tmp_path),
            "lark_profile": "synthetic", "user_open_id": sup.ME}, db=pool)
        with pytest.raises(collection.CollectionConflict, match="archive_only"):
            await collection.switch_mode("src", "collect", expected_generation=0, bridge_stopped=True,
                                         now=sup.NOW, db=pool)
        assert (await store.get_source("src", db=pool))["ingest_mode"] == "bridge"
        for table in ("source_collection_settings", "source_parity_sessions", "source_parity_members", "source_parity_coverage"):
            assert await pool.fetchval(f"SELECT count(*) FROM {table}") == 0
    run_db(check, monkeypatch, timeout=30)
