"""Existing subscription/extraction SQL consumes a mode overlap by sequence once."""

from datetime import timedelta

import pytest

from looper.agents import extraction, processor, subscriptions
from looper.agents.message_collect import run_collect_pass
from looper.agents.source_service import run_bridge_pass
from looper.db import message_stream as store, subscriptions as consumers
from tests import msgflow_support as sup
from tests.db_support import DSN, run_db
from tests.message_collect_support import Backend

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def test_mode_overlap_is_seen_once_by_existing_subscription_and_extraction_queries(monkeypatch, tmp_path):
    async def facts(*args, **kwargs):
        return {}

    monkeypatch.setattr(processor, "run_facts", facts)

    async def check(pool):
        await store.save_source(
            "synthetic",
            {
                "name": "Synthetic",
                "ingest_mode": "bridge",
                "bridge_dir": str(tmp_path),
                "bridge_start_from": "beginning",
                "lark_profile": "synthetic",
                "user_open_id": sup.ME,
            },
            db=pool,
        )
        old = sup.message("old", sent_at=sup.NOW - timedelta(minutes=20))
        sup.append(tmp_path, "oc_synthetic", old)
        source = await store.get_source("synthetic", db=pool)
        assert (await run_bridge_pass(source, now=sup.NOW, db=pool)).events_new == 1
        high = await consumers.committed_seq(pool, "synthetic")
        initial = dict(await pool.fetchrow("SELECT * FROM source_events WHERE external_id='old'"))
        await pool.execute(
            "INSERT INTO source_collection_settings(source_id,collect_enabled_at) VALUES('synthetic',$1)",
            sup.NOW - timedelta(minutes=15),
        )
        await pool.execute(
            "UPDATE event_sources SET ingest_mode='collect',watermark_at=$1,last_listing_at=$2 WHERE id='synthetic'",
            sup.NOW - timedelta(minutes=10),
            sup.NOW,
        )
        await pool.execute("UPDATE source_chats SET kind='group'")
        backend = Backend(
            {
                "oc_synthetic": [
                    old,
                    sup.message("new", sent_at=sup.NOW - timedelta(minutes=2)),
                    sup.message("own", sender=sup.LOOPER_APP, sender_type="app"),
                    sup.message("history", sent_at=sup.NOW - timedelta(hours=2)),
                ]
            }
        )
        report = await run_collect_pass("synthetic", backend=backend, own_apps={sup.LOOPER_APP}, now=sup.NOW, db=pool)
        assert report.status == "ok"
        assert dict(await pool.fetchrow("SELECT * FROM source_events WHERE external_id='old'")) == initial
        after = await consumers.committed_seq(pool, "synthetic")
        sub = {
            "source_id": "synthetic",
            "chat_kinds": ["group"],
            "include_chats": [],
            "exclude_chats": [],
            "sender_kinds": ["user", "app"],
            "exclude_mentions_me": False,
            "include_history": False,
            "max_age_minutes": 180,
            "max_events": 100,
            "render": "lines_v1",
            "text_max_chars": 200,
            "quiet_word": "SKIP",
            "output_header": "",
            "output_kind": "leads",
            "rules": [],
            "window": "cursor",
            "delivery_policy": "per_run",
            "check_items": False,
            "cursor_seq": high,
            "config_epoch": 0,
            "target_confirmed_at": sup.NOW,
        }
        push = await consumers.window(pool, sub, from_seq=high, to_seq=after, now=sup.NOW, own_apps={sup.LOOPER_APP})
        assert [row["text"] for row in push["rows"]] == ["hello"]
        assert len(push["rows"]) == 1
        leads = await consumers.extraction_window(
            pool, sub, output_kind="leads", from_seq=high, to_seq=after, now=sup.NOW, own_apps={sup.LOOPER_APP}
        )
        assert {row["external_id"] for row in leads["rows"]} == {"new", "history"}
        assert await pool.fetchval("SELECT origin FROM source_events WHERE external_id='history'") == "history"
        schedule = {
            "id": "synthetic_subscription",
            "project_id": None,
            "cron": "*/10 * * * *",
            "timezone": "UTC",
            "subscription": sub,
        }
        sub["target_confirmed_version"] = await subscriptions.config_version(schedule)
        await pool.execute("INSERT INTO system_settings(key,value) VALUES('leads.enabled','true')")
        planned = await extraction.plan(pool, schedule, now=sup.NOW, own_apps={sup.LOOPER_APP})
        assert {row["external_id"] for row in planned["rows"]} == {"new", "history"}
        sub["cursor_seq"] = after
        assert await extraction.plan(pool, schedule, now=sup.NOW, own_apps={sup.LOOPER_APP}) is None
        await pool.execute(
            "INSERT INTO intent_group_reads(source_id,chat_id,added_at,read_from) VALUES('synthetic','oc_synthetic',$1,$1)",
            sup.NOW - timedelta(hours=3),
        )
        grouped = await consumers.extraction_window(
            pool,
            sub,
            output_kind="intents",
            from_seq=high,
            to_seq=after,
            now=sup.NOW,
            own_apps={sup.LOOPER_APP},
            group_read=consumers.GroupRead(100, {"oc_synthetic": 100}),
        )
        assert [row["external_id"] for row in grouped["rows"]] == ["new"]
        # Age, history/import and followed-group eligibility remain SQL-owned.
        await pool.execute("UPDATE source_events SET origin='import' WHERE external_id='history'")
        assert [
            row["external_id"]
            for row in (
                await consumers.extraction_window(
                    pool, sub, output_kind="leads", from_seq=high, to_seq=after, now=sup.NOW, own_apps=()
                )
            )["rows"]
        ] == ["new"]
        sub["max_age_minutes"] = 1
        aged = await consumers.extraction_window(
            pool, sub, output_kind="leads", from_seq=high, to_seq=after, now=sup.NOW, own_apps=()
        )
        assert aged["rows"] == [] and aged["too_old"] == 1

    run_db(check, monkeypatch, timeout=30)
