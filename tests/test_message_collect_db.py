"""Real collection commits and durable failures against disposable PostgreSQL."""

from datetime import timedelta

import pytest

from looper.agents.message_collect import AuthStatus, CollectorError, CollectorErrorKind, run_collect_pass
from looper.db import message_stream as store
from tests import msgflow_support as sup
from tests.message_collect_support import Backend
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


async def setup(pool):
    await store.save_source(
        "synthetic",
        {"name": "Synthetic", "ingest_mode": "collect", "lark_profile": "synthetic", "user_open_id": sup.ME},
        db=pool,
    )
    await pool.execute(
        "INSERT INTO source_collection_settings(source_id,collect_enabled_at) VALUES ('synthetic',$1)",
        sup.NOW - timedelta(hours=24),
    )
    await pool.execute(
        "UPDATE event_sources SET watermark_at=$1,last_listing_at=$2 WHERE id='synthetic'",
        sup.NOW - timedelta(minutes=10),
        sup.NOW,
    )


@pytest.mark.parametrize("state,identity", [("missing", None), ("expired", sup.ME), ("ready", "ou_changed_synthetic")])
def test_auth_failure_or_identity_change_prevents_all_message_reads(monkeypatch, state, identity):
    async def check(pool):
        backend = Backend({"oc_synthetic": [sup.message("m")]})
        backend.auth = AuthStatus(state, identity)
        report = await run_collect_pass("synthetic", backend=backend, now=sup.NOW, db=pool)
        assert report.status == "auth_failed"
        assert [call[0] for call in backend.calls] == ["auth"]
        assert await pool.fetchval("SELECT count(*) FROM source_events") == 0
        assert await pool.fetchval("SELECT watermark_at FROM event_sources") == sup.NOW - timedelta(minutes=10)

    run_db(check, monkeypatch, setup=setup, timeout=30)


def test_chat_transaction_commits_events_cursors_counts_and_observation_together(monkeypatch):
    async def check(pool):
        backend = Backend({"oc_a": [sup.message("m1"), sup.message("m2")], "oc_b": [sup.message("m3")]})
        first = await run_collect_pass("synthetic", backend=backend, now=sup.NOW, db=pool)
        assert first.status == "ok" and first.events_new == 3
        assert await pool.fetchval("SELECT committed_seq FROM event_sources") == await pool.fetchval(
            "SELECT max(seq) FROM source_events"
        )
        assert set(await pool.fetchval("SELECT cursor_ids FROM source_chats WHERE chat_id='oc_a'")) == {"m1", "m2"}
        original = dict(await pool.fetchrow("SELECT * FROM source_events WHERE external_id='m1'"))
        again = await run_collect_pass("synthetic", backend=backend, now=sup.NOW + timedelta(minutes=10), db=pool)
        assert again.status == "ok" and again.events_new == 0
        assert dict(await pool.fetchrow("SELECT * FROM source_events WHERE external_id='m1'")) == original

    run_db(check, monkeypatch, setup=setup, timeout=30)


def test_sweep_finds_unindexed_reply_and_survives_restart(monkeypatch):
    async def check(pool):
        root = sup.message("root", thread_id="omt_synthetic")
        backend = Backend({"oc_a": [root], "oc_b": [sup.message("b")]})
        assert (await run_collect_pass("synthetic", backend=backend, now=sup.NOW, db=pool)).status == "ok"
        assert await pool.fetchval("SELECT sweep_after FROM event_sources") == "oc_a"
        reply = sup.message(
            "reply", thread_id="omt_synthetic", root_id="root", parent_id="root", sent_at=sup.NOW + timedelta(minutes=1)
        )
        backend.messages["oc_a"].append(reply)
        backend.search_omit = {"root", "reply", "b"}
        await run_collect_pass("synthetic", backend=backend, now=sup.NOW + timedelta(minutes=10), db=pool)
        assert await pool.fetchval("SELECT sweep_after FROM event_sources") == "oc_b"
        assert await pool.fetchval("SELECT count(*) FROM source_events WHERE external_id='reply'") == 0
        # The next run has no in-memory rotation state: the database checkpoint selects a different chat.
        restarted = Backend(backend.messages)
        restarted.search_omit = backend.search_omit
        report = await run_collect_pass("synthetic", backend=restarted, now=sup.NOW + timedelta(minutes=20), db=pool)
        assert report.status == "ok" and report.events_new == report.sweep_found == 1
        assert report.unindexed == 2
        assert await pool.fetchval("SELECT cursor_ids FROM source_threads WHERE thread_id='omt_synthetic'") == ["reply"]
        assert await pool.fetchval("SELECT sweep_after FROM event_sources") == "oc_a"

    run_db(check, monkeypatch, setup=setup, timeout=30)


def test_chat_failures_and_listing_do_not_silently_drop_debt(monkeypatch):
    async def check(pool):
        backend = Backend({"oc_a": [sup.message("missing")]})
        backend.errors["oc_a"] = CollectorError(CollectorErrorKind.PERMISSION, code=99991679)
        before = await pool.fetchval("SELECT watermark_at FROM event_sources")
        for i in range(6):
            report = await run_collect_pass(
                "synthetic", backend=backend, now=sup.NOW + timedelta(minutes=i * 10), db=pool
            )
            assert report.status == "failed"
        assert await pool.fetchval("SELECT watermark_at FROM event_sources") == before
        assert await pool.fetchval("SELECT state FROM source_chats WHERE chat_id='oc_a'") == "failing"
        backend.errors.clear()
        report = await run_collect_pass("synthetic", backend=backend, now=sup.NOW + timedelta(hours=1), db=pool)
        assert await pool.fetchval("SELECT state FROM source_chats WHERE chat_id='oc_a'") == "active"
        # A successful sweep repairs the debt; the next contiguous search advances it.
        report = await run_collect_pass(
            "synthetic", backend=backend, now=sup.NOW + timedelta(hours=1, minutes=10), db=pool
        )
        assert report.status == "ok"

    run_db(check, monkeypatch, setup=setup, timeout=45)


def test_cursor_failure_rolls_back_the_whole_chat_but_keeps_prior_chat(monkeypatch):
    from looper.db import message_collection as collection

    async def check(pool):
        backend = Backend({"oc_a": [sup.message("a")], "oc_b": [sup.message("b")]})
        save = collection.save_chat_progress

        async def broken(conn, source_id, chat_id, **kwargs):
            if chat_id == "oc_b":
                raise RuntimeError("synthetic storage failure")
            return await save(conn, source_id, chat_id, **kwargs)

        monkeypatch.setattr(collection, "save_chat_progress", broken)
        result = await run_collect_pass("synthetic", backend=backend, now=sup.NOW, db=pool)
        assert result.status == "failed"
        assert [row[0] for row in await pool.fetch("SELECT external_id FROM source_events ORDER BY seq")] == ["a"]
        assert await pool.fetchval("SELECT count(*) FROM source_chats WHERE chat_id='oc_b'") == 0
        assert await pool.fetchval("SELECT events_new FROM source_runs WHERE id=$1", result.run_id) == 1
        monkeypatch.setattr(collection, "save_chat_progress", save)
        retry = await run_collect_pass("synthetic", backend=backend, now=sup.NOW, db=pool)
        assert retry.status == "ok" and retry.events_new == 1

    run_db(check, monkeypatch, setup=setup, timeout=30)


def test_lost_writer_session_never_continues_on_another_connection(monkeypatch):
    async def check(pool):
        writer = []
        locking = store.try_lock

        async def remember(conn, source_id):
            writer.append(conn)
            return await locking(conn, source_id)

        monkeypatch.setattr(store, "try_lock", remember)

        class Lost(Backend):
            async def list_messages(self, *args):
                if args[2] == "oc_b":
                    await writer[-1].close()
                return await super().list_messages(*args)

        backend = Lost({"oc_a": [sup.message("a")], "oc_b": [sup.message("b")]})
        result = await run_collect_pass("synthetic", backend=backend, now=sup.NOW, db=pool)
        assert result.status == "interrupted"
        assert len(writer) == 1
        assert [r[0] for r in await pool.fetch("SELECT external_id FROM source_events")] == ["a"]
        normal = Backend(backend.messages)
        retry = await run_collect_pass("synthetic", backend=normal, now=sup.NOW, db=pool)
        assert retry.status == "ok"
        assert await pool.fetchval("SELECT status FROM source_runs WHERE id=$1", result.run_id) == "interrupted"
        assert await pool.fetchval("SELECT count(*) FROM source_events") == 2

    run_db(check, monkeypatch, setup=setup, timeout=30)


@pytest.mark.parametrize("scope", ["message", "range", "chat", "global_range"])
def test_own_apps_retention_and_deletion_match_bridge(monkeypatch, scope):
    async def check(pool):
        await pool.execute(
            "UPDATE event_sources SET body_retention_days=1,watermark_at=$1 WHERE id='synthetic'",
            sup.NOW - timedelta(hours=26),
        )
        await pool.execute(
            "UPDATE source_collection_settings SET collect_enabled_at=$1 WHERE source_id='synthetic'",
            sup.NOW - timedelta(hours=24),
        )
        for chat, kind in [("oc_keep", "group"), ("oc_delete", "group"), ("oc_own_dm", "p2p")]:
            async with pool.acquire() as conn, conn.transaction():
                await store.chat_for_update(conn, "synthetic", chat, kind=kind, name="Synthetic")
        deleted = sup.message("deleted")
        target = (
            {"message_id": "deleted"}
            if scope == "message"
            else {"chat_id": "oc_delete"}
            if scope == "chat"
            else {
                "start": sup.NOW - timedelta(minutes=6),
                "end": sup.NOW - timedelta(minutes=4),
                **({"chat_id": "oc_delete"} if scope == "range" else {}),
            }
        )
        await store.delete_events("synthetic", "range" if scope == "global_range" else scope, db=pool, **target)
        backend = Backend(
            {
                "oc_keep": [
                    sup.message("app", sender=sup.OTHER_APP, sender_type="app", sent_at=sup.NOW - timedelta(minutes=2)),
                    sup.message("own", sender=sup.LOOPER_APP, sender_type="app"),
                    sup.message("old", sent_at=sup.NOW - timedelta(hours=25), mentions=[(sup.ME, "Operator")]),
                ],
                "oc_delete": [deleted],
                "oc_own_dm": [sup.message("own_dm", sender=sup.LOOPER_APP, sender_type="app")],
            }
        )
        result = await run_collect_pass("synthetic", backend=backend, own_apps={sup.LOOPER_APP}, now=sup.NOW, db=pool)
        assert result.status == "ok"
        assert {r[0] for r in await pool.fetch("SELECT external_id FROM source_events")} == {"old", "app"}
        old = await pool.fetchrow("SELECT body,text,mentions_me FROM source_events WHERE external_id='old'")
        assert old["body"] is None and old["text"] and old["mentions_me"]
        assert (
            await pool.fetchval("SELECT exclusion_reason FROM source_chats WHERE chat_id='oc_own_dm'")
            == "looper_channel"
        )

    run_db(check, monkeypatch, setup=setup, timeout=30)


def test_collect_success_does_not_complete_lark_catchup_debt(monkeypatch, tmp_path):
    from looper.agents.channels.lark_inbox import LarkInbox

    async def check(pool):
        path = tmp_path / "synthetic.sqlite"
        inbox = await LarkInbox.open(path)
        try:
            await inbox.defer("oc_debt_synthetic", 100, 200)
            await inbox.defer_thread("omt_debt_synthetic", "oc_debt_synthetic", 100, 200)
            await inbox.defer("oc_dropped_synthetic", 100, 200)
            await inbox.fail_catchup("oc_dropped_synthetic", 1)
            before = path.read_bytes()
            status = await inbox.catchup_status()
            result = await run_collect_pass(
                "synthetic", backend=Backend({"oc_synthetic": [sup.message("m")]}), now=sup.NOW, db=pool
            )
            assert result.status == "ok"
            assert path.read_bytes() == before and await inbox.catchup_status() == status
            source_before = dict(await pool.fetchrow("SELECT * FROM event_sources WHERE id='synthetic'"))
            await inbox.complete("oc_debt_synthetic")
            assert dict(await pool.fetchrow("SELECT * FROM event_sources WHERE id='synthetic'")) == source_before
        finally:
            await inbox.close()

    run_db(check, monkeypatch, setup=setup, timeout=30)


def test_protocol_failure_never_completes_a_window(monkeypatch):
    from looper.agents.message_collect import Page, SearchHit

    async def check(pool):
        class Broken(Backend):
            async def search(self, *args):
                return Page([SearchHit("m", "oc_synthetic", sup.NOW - timedelta(minutes=1))], True, "")

        before = await pool.fetchval("SELECT watermark_at FROM event_sources")
        result = await run_collect_pass("synthetic", backend=Broken(), now=sup.NOW, db=pool)
        assert result.status == "search_failed" and result.error == "pagination"
        assert await pool.fetchval("SELECT watermark_at FROM event_sources") == before
        assert await pool.fetchval("SELECT count(*) FROM source_events") == 0

    run_db(check, monkeypatch, setup=setup, timeout=30)


def test_not_member_waits_for_daily_rediscovery_and_listing_failure_is_not_success(monkeypatch):
    async def check(pool):
        backend = Backend({"oc_synthetic": [sup.message("m")]})
        backend.errors["oc_synthetic"] = CollectorError(CollectorErrorKind.NOT_MEMBER, code=230002)
        before = await pool.fetchval("SELECT watermark_at FROM event_sources")
        result = await run_collect_pass("synthetic", backend=backend, now=sup.NOW, db=pool)
        assert result.status == "failed"
        assert await pool.fetchval("SELECT state FROM source_chats WHERE chat_id='oc_synthetic'") == "left"
        backend.errors.clear()
        result = await run_collect_pass("synthetic", backend=backend, now=sup.NOW + timedelta(minutes=10), db=pool)
        assert result.status == "failed"
        assert await pool.fetchval("SELECT watermark_at FROM event_sources") == before
        result = await run_collect_pass("synthetic", backend=backend, now=sup.NOW + timedelta(days=1), db=pool)
        assert result.status == "ok"
        assert await pool.fetchval("SELECT state FROM source_chats WHERE chat_id='oc_synthetic'") == "active"
        await pool.execute("UPDATE event_sources SET last_listing_at=NULL")

        class ListingFailed(Backend):
            async def list_chats(self, *args):
                raise CollectorError(CollectorErrorKind.PERMISSION)

        result = await run_collect_pass(
            "synthetic", backend=ListingFailed(backend.messages), now=sup.NOW + timedelta(days=2), db=pool
        )
        assert result.status == "failed" and await pool.fetchval("SELECT last_listing_at FROM event_sources") is None
        assert await pool.fetchval("SELECT count(*) FROM source_events") == 1

    run_db(check, monkeypatch, setup=setup, timeout=30)


def test_deadline_cancels_the_inflight_read_and_leaves_progress_unchanged(monkeypatch):
    import asyncio

    async def check(pool):
        cancelled = []

        class Slow(Backend):
            async def auth_status(self, *args):
                try:
                    await asyncio.sleep(2)
                finally:
                    cancelled.append(True)

        before = await pool.fetchval("SELECT watermark_at FROM event_sources")
        report = await run_collect_pass("synthetic", backend=Slow(), now=sup.NOW, db=pool, seconds=0.1)
        assert report.status == "partial" and report.error == "deadline" and cancelled == [True]
        assert await pool.fetchval("SELECT watermark_at FROM event_sources") == before
        assert await pool.fetchval("SELECT status FROM source_runs WHERE id=$1", report.run_id) == "partial"
        assert await pool.fetchval("SELECT count(*) FROM source_events") == 0

    run_db(check, monkeypatch, setup=setup, timeout=30)


@pytest.mark.parametrize("cross_chat", [False, True])
def test_duplicate_identity_conflict_survives_canonical_deduplication(monkeypatch, cross_chat):
    from looper.agents import message_stream_parity
    from looper.db import message_parity

    async def check(pool):
        first = sup.message("same", sent_at=sup.NOW - timedelta(minutes=5))
        second = sup.message("same", sent_at=sup.NOW - timedelta(minutes=4))
        messages = {"oc_synthetic": [first]}
        messages.setdefault("oc_other" if cross_chat else "oc_synthetic", []).append(second)
        async with pool.acquire() as conn, conn.transaction():
            await store.chat_for_update(conn, "synthetic", "oc_synthetic", kind="group", name="Synthetic")
            session = await message_parity.create_session(
                conn,
                "synthetic",
                0,
                sup.NOW - timedelta(days=1),
                sup.NOW,
                0,
                policy_digest=await message_stream_parity.policy_digest(conn, "synthetic", ()),
                reference_digest="synthetic",
            )
            await message_parity.set_coverage(
                conn, session, "oc_synthetic", "chat", "oc_synthetic", sup.NOW - timedelta(days=1), sup.NOW
            )
        report = await run_collect_pass("synthetic", backend=Backend(messages), now=sup.NOW, db=pool)
        assert report.status == "ok"
        assert await pool.fetchval("SELECT count(*) FROM source_events") == 1
        evidence = await message_stream_parity.read_report("synthetic", session, db=pool)
        assert evidence["state"] == "incomplete" and evidence["reason"] == "identity_conflict"

    run_db(check, monkeypatch, setup=setup, timeout=30)
