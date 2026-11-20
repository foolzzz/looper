"""Bridge ingest into the event store, against a disposable database and synthetic collector files."""

import asyncio
import os
from datetime import timedelta

import pytest

from looper.agents import source_service as service
from looper.db import message_stream as store
from tests import msgflow_support as sup
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def make_source(directory, **fields):
    async def setup(pool):
        await store.save_source("src", {"name": "Test", "ingest_mode": "bridge", "bridge_dir": str(directory),
                                        "bridge_start_from": "beginning", "user_open_id": sup.ME, **fields}, db=pool)
    return setup


async def passes(pool, directory, *, now=sup.NOW, **options):
    source = await store.get_source("src", db=pool)
    return await service.run_bridge_pass(source, db=pool, now=now, **options)


async def rows(pool, columns="external_id"):
    return [tuple(r.values()) if len(r) > 1 else r[0] for r in await pool.fetch(
        f"SELECT {columns} FROM source_events ORDER BY seq")]


def test_appended_lines_are_stored_once_and_advance_the_committed_sequence(monkeypatch, tmp_path):
    async def check(pool):
        sup.append(tmp_path, "oc_a", sup.message("m1"), sup.message("m2"))
        first = await passes(pool, tmp_path)
        assert (first.status, first.events_new) == ("ok", 2)
        committed = await pool.fetchval("SELECT committed_seq FROM event_sources WHERE id='src'")
        assert committed == await pool.fetchval("SELECT max(seq) FROM source_events")
        sup.append(tmp_path, "oc_a", sup.message("m3"))
        again = await passes(pool, tmp_path)
        assert (again.events_new, again.files_unchanged) == (1, 0)
        assert await rows(pool) == ["m1", "m2", "m3"]
        assert await pool.fetchval("SELECT committed_seq FROM event_sources WHERE id='src'") > committed
        quiet = await passes(pool, tmp_path)
        assert (quiet.events_new, quiet.files_unchanged) == (0, 1)

    run_db(check, monkeypatch, setup=make_source(tmp_path), timeout=30)


def test_an_unterminated_last_line_is_not_stored_until_it_is_complete(monkeypatch, tmp_path):
    async def check(pool):
        whole = sup.line(sup.message("m2"))
        path = sup.append(tmp_path, "oc_a", sup.message("m1"), partial=whole[:30])
        await passes(pool, tmp_path)
        assert await rows(pool) == ["m1"]
        with path.open("ab") as handle:
            handle.write(whole[30:])
        await passes(pool, tmp_path)
        assert await rows(pool) == ["m1", "m2"]

    run_db(check, monkeypatch, setup=make_source(tmp_path), timeout=30)


def test_duplicate_lines_store_one_event(monkeypatch, tmp_path):
    async def check(pool):
        sup.append(tmp_path, "oc_a", sup.message("m1"), sup.message("m1", text="second copy"), sup.message("m2"))
        report = await passes(pool, tmp_path)
        assert (report.events_new, report.events_seen) == (2, 1)
        assert await rows(pool, "external_id, text") == [("m1", "hello"), ("m2", "hello")]
        sup.append(tmp_path, "oc_a", sup.message("m2"))
        assert (await passes(pool, tmp_path)).events_seen == 1

    run_db(check, monkeypatch, setup=make_source(tmp_path), timeout=30)


@pytest.mark.parametrize("damage", ["shrink", "new_inode", "other_id_before_offset"])
def test_a_rewritten_file_is_read_again_without_duplicates(monkeypatch, tmp_path, damage):
    async def check(pool):
        path = sup.append(tmp_path, "oc_a", sup.message("m1"), sup.message("m2"), sup.message("m3"))
        await passes(pool, tmp_path)
        if damage == "shrink":
            path.write_bytes(sup.line(sup.message("m1")) + sup.line(sup.message("m9")))
        elif damage == "new_inode":
            replacement = path.with_suffix(".new")
            replacement.write_bytes(path.read_bytes() + sup.line(sup.message("m9")))
            os.replace(replacement, path)
        else:
            path.write_bytes(sup.line(sup.message("m1")) + sup.line(sup.message("m2")) + sup.line(sup.message("mX"))
                             + sup.line(sup.message("m9")))
        report = await passes(pool, tmp_path)
        assert report.files_rescanned == 1
        stored = await rows(pool)
        assert len(stored) == len(set(stored)) and "m9" in stored and {"m1", "m2", "m3"} <= set(stored)
        assert await pool.fetchval("SELECT files_rescanned FROM source_runs WHERE id=$1", report.run_id) == 1

    run_db(check, monkeypatch, setup=make_source(tmp_path), timeout=30)


def test_lines_out_of_time_order_keep_their_send_times_and_file_order(monkeypatch, tmp_path):
    async def check(pool):
        late, early = sup.NOW - timedelta(minutes=1), sup.NOW - timedelta(minutes=30)
        sup.append(tmp_path, "oc_a", sup.message("late", sent_at=late), sup.message("early", sent_at=early))
        await passes(pool, tmp_path)
        assert await rows(pool, "external_id, sent_at") == [("late", late), ("early", early)]

    run_db(check, monkeypatch, setup=make_source(tmp_path), timeout=30)


def test_a_pass_killed_in_the_middle_of_a_file_resumes_without_loss_or_duplicates(monkeypatch, tmp_path):
    async def check(pool):
        sup.append(tmp_path, "oc_a", *(sup.message(f"m{n}") for n in range(6)))
        sup.append(tmp_path, "oc_b", sup.message("b1"))
        real, calls = store.insert_events, []

        async def dying(conn, source_id, run_id, batch):
            calls.append(len(batch))
            stored = await real(conn, source_id, run_id, batch)
            if len(calls) == 1:
                raise RuntimeError("killed after the insert, before the commit")
            return stored

        monkeypatch.setattr(store, "insert_events", dying)
        broken = await passes(pool, tmp_path)
        assert broken.status == "partial" and broken.files_failed == 1
        assert await rows(pool) == ["b1"]
        assert await pool.fetchval("SELECT bridge_offset FROM source_chats WHERE chat_id='oc_a'") is None
        monkeypatch.setattr(store, "insert_events", real)
        healed = await passes(pool, tmp_path)
        assert healed.status == "ok"
        assert sorted(await rows(pool)) == sorted([f"m{n}" for n in range(6)] + ["b1"])
        assert "RuntimeError" in broken.error and "killed" not in broken.error

    run_db(check, monkeypatch, setup=make_source(tmp_path), timeout=30)


def test_the_committed_sequence_never_goes_back(monkeypatch, tmp_path):
    async def check(pool):
        seen = []
        for number in range(3):
            sup.append(tmp_path, "oc_a", sup.message(f"m{number}"))
            await passes(pool, tmp_path)
            seen.append(await pool.fetchval("SELECT committed_seq FROM event_sources WHERE id='src'"))
        path = tmp_path / "chats" / "oc_a.ndjson"
        path.write_bytes(sup.line(sup.message("m0")))
        await passes(pool, tmp_path)
        seen.append(await pool.fetchval("SELECT committed_seq FROM event_sources WHERE id='src'"))
        assert seen == sorted(seen) and len(set(seen[:3])) == 3

    run_db(check, monkeypatch, setup=make_source(tmp_path), timeout=30)


def test_two_servers_on_one_database_have_one_writer(monkeypatch, tmp_path):
    async def check(pool):
        sup.append(tmp_path, "oc_a", *(sup.message(f"m{n}") for n in range(50)))
        entered, release = asyncio.Event(), asyncio.Event()
        real = store.insert_events

        async def slow(*args, **kwargs):
            entered.set()
            await release.wait()
            return await real(*args, **kwargs)

        monkeypatch.setattr(store, "insert_events", slow)
        first = asyncio.create_task(passes(pool, tmp_path))
        await entered.wait()
        second = await passes(pool, tmp_path)
        assert second.status == "skipped_busy" and second.run_id is None
        release.set()
        assert (await first).events_new == 50
        assert await pool.fetchval("SELECT count(*) FROM source_runs") == 1

    run_db(check, monkeypatch, setup=make_source(tmp_path), timeout=30, max_size=5)


def test_a_pass_left_running_by_a_dead_server_is_marked_interrupted(monkeypatch, tmp_path):
    async def check(pool):
        sup.append(tmp_path, "oc_a", sup.message("m1"))
        await pool.execute("INSERT INTO source_runs (source_id, mode, trigger, status) "
                           "VALUES ('src', 'bridge', 'scheduled', 'running')")
        await passes(pool, tmp_path)
        assert [r[0] for r in await pool.fetch("SELECT status FROM source_runs ORDER BY id")] == ["interrupted", "ok"]

    run_db(check, monkeypatch, setup=make_source(tmp_path), timeout=30)


def test_origin_follows_the_first_read_and_the_age_of_a_line(monkeypatch, tmp_path):
    async def check(pool):
        old, recent = sup.NOW - timedelta(days=3), sup.NOW - timedelta(hours=2)
        sup.append(tmp_path, "oc_a", sup.message("first_old", sent_at=old), sup.message("first_recent", sent_at=recent))
        await passes(pool, tmp_path)
        assert await rows(pool, "external_id, origin") == [("first_old", "import"), ("first_recent", "import")]
        sup.append(tmp_path, "oc_a", sup.message("backfill", sent_at=old), sup.message("fresh", sent_at=sup.NOW))
        await passes(pool, tmp_path)
        assert dict(await rows(pool, "external_id, origin")) == {
            "first_old": "import", "first_recent": "import", "backfill": "history", "fresh": "live"}

    run_db(check, monkeypatch, setup=make_source(tmp_path), timeout=30)


def test_a_source_that_starts_from_the_end_stores_only_what_is_appended_later(monkeypatch, tmp_path):
    async def check(pool):
        whole = sup.line(sup.message("tail"))
        sup.append(tmp_path, "oc_a", sup.message("before1"), sup.message("before2"), partial=whole[:25])
        first = await passes(pool, tmp_path)
        assert (first.events_new, first.files_baselined) == (0, 1)
        assert await pool.fetchval("SELECT count(*) FROM source_events") == 0
        path = tmp_path / "chats" / "oc_a.ndjson"
        with path.open("ab") as handle:
            handle.write(whole[25:])
        sup.append(tmp_path, "oc_a", sup.message("after"))
        await passes(pool, tmp_path)
        # The line that was half written at the baseline is the first line after it.
        assert await rows(pool, "external_id, origin") == [("tail", "live"), ("after", "live")]
        # A rewrite does not bring the older history in: a later read keeps a week back at most.
        path.write_bytes(sup.line(sup.message("ancient", sent_at=sup.NOW - timedelta(days=30)))
                         + sup.line(sup.message("recentish", sent_at=sup.NOW - timedelta(days=3))) + path.read_bytes())
        again = await passes(pool, tmp_path)
        assert again.files_rescanned == 1
        assert "ancient" not in await rows(pool) and "recentish" in await rows(pool)

    run_db(check, monkeypatch, setup=make_source(tmp_path, bridge_start_from="end"), timeout=30)


def test_a_long_file_is_read_in_chunks_that_resume_after_an_interruption(monkeypatch, tmp_path):
    async def check(pool):
        monkeypatch.setattr(service, "CHUNK_BYTES", 2000)
        sup.append(tmp_path, "oc_a", *(sup.message(f"m{n:03d}", text="z" * 100) for n in range(40)))
        real, calls = store.insert_events, []

        async def dying(conn, source_id, run_id, batch):
            calls.append(1)
            if len(calls) == 3:
                raise RuntimeError("the pass died in the third chunk")
            return await real(conn, source_id, run_id, batch)

        monkeypatch.setattr(store, "insert_events", dying)
        broken = await passes(pool, tmp_path)
        assert broken.status == "failed"
        done = await pool.fetchval("SELECT count(*) FROM source_events")
        assert 0 < done < 40
        offset = await pool.fetchval("SELECT bridge_offset FROM source_chats WHERE chat_id = 'oc_a'")
        assert 0 < offset < (tmp_path / "chats" / "oc_a.ndjson").stat().st_size
        monkeypatch.setattr(store, "insert_events", real)
        healed = await passes(pool, tmp_path)
        assert healed.status == "ok" and healed.events_new == 40 - done
        assert await rows(pool) == [f"m{n:03d}" for n in range(40)]
        # The first import is one import however many passes it took: its recent half is not live news.
        assert {r["origin"] for r in await pool.fetch("SELECT origin FROM source_events")} == {"import"}
        assert await pool.fetchval("SELECT bridge_importing FROM source_chats WHERE chat_id = 'oc_a'") is False
        sup.append(tmp_path, "oc_a", sup.message("after", sent_at=sup.NOW))
        await passes(pool, tmp_path)
        assert await pool.fetchval("SELECT origin FROM source_events WHERE external_id = 'after'") == "live"

    run_db(check, monkeypatch, setup=make_source(tmp_path), timeout=30)


def test_a_dry_run_counts_across_its_own_chunks_and_stores_nothing(monkeypatch, tmp_path):
    async def check(pool):
        monkeypatch.setattr(service, "CHUNK_BYTES", 2000)
        sup.append(tmp_path, "oc_a", *(sup.message(f"m{n}", text="z" * 100) for n in range(30)),
                   sup.message("m3", text="a repeat in a later chunk"))
        dry = await passes(pool, tmp_path, days=7, dry_run=True)
        assert (dry.events_new, dry.events_seen) == (30, 1)
        assert await pool.fetchval("SELECT count(*) FROM source_events") == 0

    run_db(check, monkeypatch, setup=make_source(tmp_path), timeout=30)


def test_the_import_reconciles_every_file(monkeypatch, tmp_path):
    async def check(pool):
        monkeypatch.setattr(service, "CHUNK_BYTES", 1500)
        sup.append(tmp_path, "oc_a", *(sup.message(f"a{n}", text="y" * 80) for n in range(20)),
                   sup.message("a1"), sup.message("own", sender=sup.LOOPER_APP, sender_type="app"),
                   sup.message("far", sent_at=sup.NOW - timedelta(days=40)), partial=b'{"message_id": "half')
        sup.append(tmp_path, "oc_b", sup.message("b1"), partial=b"\nnot json\n")
        report = await passes(pool, tmp_path, days=7, own_apps={sup.LOOPER_APP})
        by_chat = {f.chat_id: f for f in report.file_reports}
        a = by_chat["oc_a"]
        assert (a.lines, a.distinct, a.new, a.duplicates, a.skipped) == (23, 22, 20, 1, 2)
        assert a.incomplete_tail and a.reconciled
        b = by_chat["oc_b"]
        assert (b.lines, b.new, b.bad_lines) == (1, 1, 1) and b.reconciled
        assert report.events_new == 21 and report.events_seen == 1

    run_db(check, monkeypatch, setup=make_source(tmp_path), timeout=30)


def test_a_body_past_its_retention_is_stored_as_text_only(monkeypatch, tmp_path):
    async def check(pool):
        old, recent = sup.NOW - timedelta(days=45), sup.NOW - timedelta(days=2)
        sup.append(tmp_path, "oc_a", sup.message("bot_old", sent_at=old, sender=sup.OTHER_APP, sender_type="app", text="old bot"),
                   sup.message("bot_new", sent_at=recent, sender=sup.OTHER_APP, sender_type="app"),
                   sup.message("human_old", sent_at=old, text="old human"))
        await passes(pool, tmp_path)
        got = {r["external_id"]: r for r in await pool.fetch(
            "SELECT external_id, text, body IS NULL AS dropped, body_dropped_at IS NOT NULL AS marked, body_bytes "
            "FROM source_events")}
        assert (got["bot_old"]["dropped"], got["bot_old"]["marked"], got["bot_old"]["text"]) == (True, True, "old bot")
        assert got["bot_old"]["body_bytes"] > 0
        assert not got["bot_new"]["dropped"] and not got["human_old"]["dropped"]

    run_db(check, monkeypatch, setup=make_source(tmp_path), timeout=30)


def test_an_import_reads_back_the_days_asked_and_dry_run_stores_nothing(monkeypatch, tmp_path):
    async def check(pool):
        sup.append(tmp_path, "oc_a", sup.message("d1", sent_at=sup.NOW - timedelta(days=1, hours=1)),
                   sup.message("d5", sent_at=sup.NOW - timedelta(days=5)),
                   sup.message("d20", sent_at=sup.NOW - timedelta(days=20)))
        dry = await passes(pool, tmp_path, days=7, dry_run=True)
        assert (dry.events_new, dry.events_skipped, dry.run_id) == (2, 1, None)
        assert await pool.fetchval("SELECT count(*) FROM source_events") == 0
        assert await pool.fetchval("SELECT count(*) FROM source_chats") == 0
        real = await passes(pool, tmp_path, days=7)
        assert real.events_new == 2
        assert await pool.fetchval("SELECT mode || '/' || trigger FROM source_runs WHERE id=$1", real.run_id) == "import/manual"
        assert set(await rows(pool)) == {"d1", "d5"}
        # The bridge carries on after the import: nothing is read twice.
        assert (await passes(pool, tmp_path)).files_unchanged == 1

    run_db(check, monkeypatch, setup=make_source(tmp_path), timeout=30)


def test_messages_of_the_bots_own_apps_are_not_stored_and_their_direct_chat_is_excluded(monkeypatch, tmp_path):
    async def check(pool):
        sup.write_names(tmp_path, chats={"oc_dm": {"name": "DM", "type": "p2p"}, "oc_g": {"name": "G", "type": "group"}})
        sup.append(tmp_path, "oc_dm", sup.message("mine", sender=sup.LOOPER_APP, sender_type="app"),
                   sup.message("me_to_bot", sender=sup.ME))
        sup.append(tmp_path, "oc_g", sup.message("push", sender=sup.LOOPER_APP, sender_type="app"),
                   sup.message("other_bot", sender=sup.OTHER_APP, sender_type="app"), sup.message("human"))
        report = await passes(pool, tmp_path, own_apps={sup.LOOPER_APP})
        assert set(await rows(pool)) == {"other_bot", "human"}
        assert report.events_skipped == 3
        chat = await pool.fetchrow("SELECT inclusion, exclusion_reason FROM source_chats WHERE chat_id='oc_dm'")
        assert (chat["inclusion"], chat["exclusion_reason"]) == ("excluded", "looper_channel")
        sup.append(tmp_path, "oc_dm", sup.message("later", sender=sup.ME))
        await passes(pool, tmp_path, own_apps={sup.LOOPER_APP})
        assert "later" not in await rows(pool)

    run_db(check, monkeypatch, setup=make_source(tmp_path), timeout=30)


def test_events_carry_mentions_names_and_the_chat_directory(monkeypatch, tmp_path):
    async def check(pool):
        sup.write_names(tmp_path, chats={"oc_a": {"name": "Group A", "type": "group"}},
                        users={sup.ALICE: {"name": "Alice", "source": "contact"},
                               sup.BOB: {"name": "", "source": "none"}})
        sup.append(tmp_path, "oc_a", sup.message("m1", mentions=[(sup.ME, "Me")]), sup.message("m2", sender=sup.BOB))
        await passes(pool, tmp_path)
        assert await rows(pool, "external_id, mentions_me, sender_kind") == [("m1", True, "user"), ("m2", False, "user")]
        chat = await pool.fetchrow("SELECT kind, name FROM source_chats WHERE chat_id='oc_a'")
        assert (chat["kind"], chat["name"]) == ("group", "Group A")
        people = {r["open_id"]: (r["name"], r["name_source"]) for r in await pool.fetch("SELECT * FROM source_people")}
        assert people == {sup.ALICE: ("Alice", "contact"), sup.ME: ("Me", "mention")}

    run_db(check, monkeypatch, setup=make_source(tmp_path), timeout=30)


def test_a_missing_directory_fails_the_run_loudly(monkeypatch, tmp_path):
    async def check(pool):
        report = await passes(pool, tmp_path / "nowhere")
        assert report.status == "failed" and "chats directory" in report.error
        row = await pool.fetchrow("SELECT status, error FROM source_runs")
        assert (row["status"], row["error"]) == ("failed", report.error)

    run_db(check, monkeypatch, setup=make_source(tmp_path / "nowhere"), timeout=30)


def test_health_follows_the_runs(monkeypatch, tmp_path):
    async def check(pool):
        source = await store.get_source("src", db=pool)
        later = sup.NOW + timedelta(hours=2)
        assert service.health(source, None, source["updated_at"]) == "healthy"
        assert service.health(source, "partial", source["updated_at"]) == "degraded"
        assert service.health(source, None, source["updated_at"] + timedelta(hours=2)) == "stale"
        assert service.health({**source, "ingest_mode": "off"}, None, later) == "off"

    run_db(check, monkeypatch, setup=make_source(tmp_path), timeout=30)


@pytest.mark.parametrize("wait_for_disconnect", [False, True])
def test_a_lost_lock_session_stops_the_writes(monkeypatch, tmp_path, wait_for_disconnect):
    async def check(pool):
        monkeypatch.setattr(service, "CHUNK_BYTES", 2000)
        sup.append(tmp_path, "oc_a", *(sup.message(f"m{n:03d}", text="z" * 100) for n in range(40)))
        real, calls = store.insert_events, []

        async def losing(conn, source_id, run_id, batch):
            calls.append(1)
            if len(calls) == 2:
                disconnected = asyncio.Event()
                conn.add_termination_listener(lambda _: disconnected.set())
                # The session that holds the writer lock goes away; the writes must go with it.
                # This schema's lock only: other tests of other workers take the same lock class in their own schemas.
                holder = await pool.fetchval("SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND granted "
                                             "AND classid = $1 AND objid = hashtext(current_schema() || ':' || 'src')::oid",
                                             store._LOCK_CLASS)
                assert holder == conn.get_server_pid()
                assert await pool.fetchval("SELECT pg_terminate_backend($1)", holder)
                if wait_for_disconnect:
                    # Force asyncpg's cleanup to detach the pooled proxy before the next write.
                    await asyncio.wait_for(disconnected.wait(), timeout=5)
            return await real(conn, source_id, run_id, batch)

        monkeypatch.setattr(store, "insert_events", losing)
        broken = await passes(pool, tmp_path)
        assert broken.status == "failed"
        stored = await pool.fetchval("SELECT count(*) FROM source_events")
        assert 0 < stored < 40
        assert len(calls) == 2
        committed = await pool.fetchval("SELECT committed_seq FROM event_sources WHERE id='src'")
        assert committed == await pool.fetchval("SELECT max(seq) FROM source_events")
        await asyncio.sleep(0.3)
        assert await pool.fetchval("SELECT count(*) FROM source_events") == stored
        monkeypatch.setattr(store, "insert_events", real)
        assert (await passes(pool, tmp_path)).status == "ok"
        assert await pool.fetchval("SELECT count(*) FROM source_events") == 40
        assert await pool.fetchval("SELECT status FROM source_runs WHERE id=$1", broken.run_id) == "interrupted"

    run_db(check, monkeypatch, setup=make_source(tmp_path), timeout=30, max_size=5)


def test_a_dry_run_writes_nothing_at_all_and_counts_a_repeat_across_files_once(monkeypatch, tmp_path):
    async def check(pool):
        sup.append(tmp_path, "oc_a", sup.message("m1"), sup.message("shared"))
        sup.append(tmp_path, "oc_b", sup.message("shared"), sup.message("m2"))
        await pool.execute("SELECT nextval('source_events_seq_seq')")
        before = await pool.fetchval("SELECT last_value FROM source_events_seq_seq")
        dry = await passes(pool, tmp_path, days=7, dry_run=True)
        assert (dry.events_new, dry.events_seen) == (3, 1)
        assert await pool.fetchval("SELECT last_value FROM source_events_seq_seq") == before
        assert await pool.fetchval("SELECT count(*) FROM source_chats") == 0
        real = await passes(pool, tmp_path, days=7)
        assert (real.events_new, real.events_seen) == (dry.events_new, dry.events_seen)

    run_db(check, monkeypatch, setup=make_source(tmp_path), timeout=30)


def test_a_log_line_about_a_failed_read_never_carries_the_exception_text(monkeypatch, tmp_path, caplog):
    async def check(pool):
        sup.append(tmp_path, "oc_a", sup.message("m1"))

        async def failing(*args, **kwargs):
            raise RuntimeError("Failing row contains (a secret plan)")

        monkeypatch.setattr(store, "insert_events", failing)
        with caplog.at_level("DEBUG"):
            await passes(pool, tmp_path)
        assert "secret" not in caplog.text and all(r.exc_info is None for r in caplog.records)

    run_db(check, monkeypatch, setup=make_source(tmp_path), timeout=30)
