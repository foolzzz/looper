"""Deletion survives re-ingest; source exclusions apply to every subscription window."""

import asyncio
from datetime import timedelta

import pytest

from looper.agents import source_service
from looper.db import message_stream as store, subscriptions
from tests import msgflow_support as sup
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


async def setup(pool, directory):
    await store.save_source("src", {"name": "Synthetic stream", "ingest_mode": "bridge", "bridge_dir": str(directory),
                                    "bridge_start_from": "beginning"}, db=pool)


async def ingest(pool, **options):
    return await source_service.run_bridge_pass(await store.get_source("src", db=pool), db=pool, now=sup.NOW, **options)


async def selected(pool):
    sub = dict(source_id="src", max_age_minutes=10080, chat_kinds=["unknown"], include_chats=[], exclude_chats=[],
               sender_kinds=["user", "app"], exclude_mentions_me=False, include_history=True, max_events=100)
    result = await subscriptions.window(pool, sub, from_seq=0, to_seq=100000, now=sup.NOW, own_apps=[])
    return [r["text"] for r in result["rows"]]


def test_message_tombstone_blocks_overlap_and_later_import(monkeypatch, tmp_path):
    async def check(pool):
        path = sup.append(tmp_path, "chat_synthetic", sup.message("deleted_message"), sup.message("kept_message"))
        await ingest(pool)
        assert (await store.deletion_preview("src", "message", message_id="deleted_message", db=pool))["events"] == 1
        assert (await store.delete_events("src", "message", message_id="deleted_message", db=pool))["deleted"] == 1
        path.write_bytes(sup.line(sup.message("deleted_message")))
        await ingest(pool)
        await ingest(pool, days=7)
        assert [r[0] for r in await pool.fetch("SELECT external_id FROM source_events")] == ["kept_message"]
        assert await pool.fetchval("SELECT count(*) FROM source_deletions WHERE scope = 'message'") == 1

    run_db(check, monkeypatch, setup=lambda p: setup(p, tmp_path), timeout=30)


def test_range_is_half_open_and_limited_to_its_source_and_optional_chat(monkeypatch, tmp_path):
    async def check(pool):
        start, end = sup.NOW - timedelta(hours=2), sup.NOW - timedelta(hours=1)
        sup.append(tmp_path, "chat_synthetic", sup.message("before", sent_at=start-timedelta(seconds=1)),
                   sup.message("start", sent_at=start), sup.message("end", sent_at=end))
        sup.append(tmp_path, "other_synthetic", sup.message("other", sent_at=start))
        await ingest(pool)
        assert (await store.delete_events("src", "range", start=start, end=end, chat_id="chat_synthetic", db=pool))["deleted"] == 1
        await ingest(pool, days=7)
        assert {r[0] for r in await pool.fetch("SELECT external_id FROM source_events")} == {"before", "end", "other"}

    run_db(check, monkeypatch, setup=lambda p: setup(p, tmp_path), timeout=30)


def test_chat_delete_clears_name_excludes_and_cannot_be_repopulated(monkeypatch, tmp_path):
    async def check(pool):
        sup.append(tmp_path, "chat_synthetic", sup.message("m1", text="sensitive"))
        sup.write_names(tmp_path, chats={"chat_synthetic": {"name": "Synthetic name"}})
        await ingest(pool)
        assert await selected(pool) == ["sensitive"]
        assert (await store.delete_events("src", "chat", chat_id="chat_synthetic", db=pool))["deleted"] == 1
        chat = await pool.fetchrow("SELECT * FROM source_chats")
        assert (chat["name"], chat["inclusion"], chat["exclusion_reason"]) == ("", "excluded", "operator")
        assert await selected(pool) == []
        await ingest(pool, days=7)
        async with store.transaction(pool) as conn:
            await store.set_chat_directory(conn, "src", "chat_synthetic", kind="group", name="Synthetic name")
        assert await pool.fetchval("SELECT name FROM source_chats") == ""
        assert await pool.fetchval("SELECT count(*) FROM source_events") == 0

    run_db(check, monkeypatch, setup=lambda p: setup(p, tmp_path), timeout=30)


def test_exclusion_removes_existing_events_from_subscription_selection(monkeypatch, tmp_path):
    async def check(pool):
        sup.append(tmp_path, "chat_synthetic", sup.message("m1"))
        await ingest(pool)
        assert await selected(pool) == ["hello"]
        await pool.execute("UPDATE source_events SET sent_at = now()")
        chats = await store.list_chats("src", search="synthetic", db=pool)
        assert len(chats) == 1 and chats[0]["events_7d"] == 1
        await store.set_chat_inclusion("src", "chat_synthetic", False, db=pool)
        assert await selected(pool) == []
        await store.set_chat_inclusion("src", "chat_synthetic", True, db=pool)
        assert await selected(pool) == ["hello"]

    run_db(check, monkeypatch, setup=lambda p: setup(p, tmp_path), timeout=30)


def test_daily_retention_drops_only_expired_raw_bodies_and_preserves_text(monkeypatch, tmp_path):
    async def check(pool):
        old = sup.NOW - timedelta(days=31)
        sup.append(tmp_path, "chat_synthetic", sup.message("old_app", sent_at=old, sender_type="app"),
                   sup.message("boundary_app", sent_at=sup.NOW-timedelta(days=30), sender_type="app"),
                   sup.message("fresh_app", sender_type="app"), sup.message("old_user", sent_at=old))
        # Ingest earlier so retention, rather than ingest's age check, clears the expired body.
        await source_service.run_bridge_pass(await store.get_source("src", db=pool), db=pool, now=old)
        await pool.execute("UPDATE event_sources SET ingest_mode='off'")
        service = source_service.SourceService()
        await service.run_once(clock=lambda: sup.NOW)
        rows = {r["external_id"]: r for r in await pool.fetch("SELECT * FROM source_events")}
        assert rows["old_app"]["body"] is None and rows["old_app"]["body_dropped_at"] == sup.NOW
        assert all(rows[k]["body"] is not None for k in ("boundary_app", "fresh_app", "old_user"))
        assert all(r["text"] == "hello" for r in rows.values())
        assert (await store.get_source("src", db=pool))["retention_last_at"] == sup.NOW
        await service.run_once(clock=lambda: sup.NOW + timedelta(hours=1))
        assert (await store.get_source("src", db=pool))["retention_last_at"] == sup.NOW
        await store.save_source("src", {"body_retention_days": 1}, db=pool)
        await service.run_once(clock=lambda: sup.NOW + timedelta(days=1))
        assert await pool.fetchval("SELECT body IS NULL FROM source_events WHERE external_id='old_user'")

    run_db(check, monkeypatch, setup=lambda p: setup(p, tmp_path), timeout=30)


def test_delete_waits_for_an_ingest_chunk_then_removes_its_committed_event(monkeypatch, tmp_path):
    async def check(pool):
        sup.append(tmp_path, "chat_synthetic", sup.message("in_flight"))
        inserted, release = asyncio.Event(), asyncio.Event()
        real_insert = store.insert_events

        async def pause_after_insert(*args, **kwargs):
            result = await real_insert(*args, **kwargs)
            inserted.set()
            await release.wait()
            return result

        monkeypatch.setattr(store, "insert_events", pause_after_insert)
        writer = asyncio.create_task(ingest(pool))
        await inserted.wait()
        deletion = asyncio.create_task(store.delete_events("src", "message", message_id="in_flight", db=pool))
        # Prove the deletion cannot complete while the uncommitted chunk owns the mutation lock.
        done, _ = await asyncio.wait({deletion}, timeout=0.05)
        assert not done
        release.set()
        assert (await writer).events_new == 1
        assert (await deletion)["deleted"] == 1
        assert await pool.fetchval("SELECT count(*) FROM source_events") == 0

    run_db(check, monkeypatch, setup=lambda p: setup(p, tmp_path), timeout=30)
