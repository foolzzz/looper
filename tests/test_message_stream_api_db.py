"""The message stream's operations through the v1 API: status and runs, the switch, and who may call them."""

import pytest

from looper.agents import source_service
from looper.db import message_stream as store
from tests import msgflow_support as sup
from tests.db_support import DSN, run_db, v1_api

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


@pytest.mark.parametrize("action,body", [("status", {}), ("runs", {}), ("configure", {"ingest_mode": "off"}),
    ("chats", {}), ("chat_update", {"chat_id": "c", "included": False}),
    ("delete_preview", {"scope": "message", "message_id": "m"}),
    ("delete", {"scope": "message", "message_id": "m", "confirmation_token": "x"})])
def test_operations_need_the_operator(monkeypatch, tmp_path, action, body):
    async def check(pool):
        async with v1_api(tmp_path, pool) as call:
            status, _ = await call("message_stream." + action, body, client=True)
            assert status == 403

    run_db(check, monkeypatch, timeout=20)


def test_no_source_reads_as_an_empty_status(monkeypatch, tmp_path):
    async def check(pool):
        async with v1_api(tmp_path, pool) as call:
            status, answer = await call("message_stream.status")
            assert (status, answer["result"]) == (200, {"sources": []})
            status, answer = await call("message_stream.runs")
            assert status == 404

    run_db(check, monkeypatch, timeout=20)


def test_the_switch_creates_the_source_and_status_counts_what_was_read(monkeypatch, tmp_path):
    async def check(pool):
        sup.append(tmp_path, "oc_a", sup.message("m1", text="a secret plan"), sup.message("m2"))
        async with v1_api(tmp_path / "api", pool) as call:
            status, answer = await call("message_stream.configure",
                                        {"ingest_mode": "bridge", "bridge_dir": str(tmp_path), "user_open_id": sup.ME,
                                         "start_from": "beginning"})
            assert status == 200, answer
            source = answer["result"]
            assert (source["source_id"], source["ingest_mode"], source["cron"], source["start_from"]) == (
                "lark-user", "bridge", source_service.BRIDGE_CRON, "beginning")
            await source_service.run_bridge_pass(await store.get_source("lark-user", db=pool), db=pool, now=sup.NOW)
            status, answer = await call("message_stream.status")
            (view,) = answer["result"]["sources"]
            assert (view["events"], view["chats"], view["health"], view["last_run"]["status"]) == (2, 1, "healthy", "ok")
            assert view["committed_seq"] > 0 and "secret" not in str(answer)
            status, answer = await call("message_stream.runs", {"limit": 5})
            assert [r["events_new"] for r in answer["result"]["runs"]] == [2]
            status, answer = await call("message_stream.configure", {"ingest_mode": "off"})
            assert answer["result"]["ingest_mode"] == "off" and answer["result"]["events"] == 2

    run_db(check, monkeypatch, timeout=30)


@pytest.mark.parametrize("body", [
    {"ingest_mode": "bridge"},
    {"ingest_mode": "bridge", "bridge_dir": "relative/dir"},
    {"ingest_mode": "bridge", "bridge_dir": "{tmp}/missing"},
    {"ingest_mode": "collect"},
    {"cron": "every minute"},
    {"start_from": "middle"},
    {"timezone": "Mars/Base"},
    {"user_open_id": "has space"},
    {"name": "  "},
])
def test_the_switch_refuses_what_cannot_work_and_stores_nothing(monkeypatch, tmp_path, body):
    async def check(pool):
        body_ = {key: value.replace("{tmp}", str(tmp_path)) if isinstance(value, str) else value
                 for key, value in body.items()}
        async with v1_api(tmp_path, pool) as call:
            status, answer = await call("message_stream.configure", body_)
            assert status == 400, answer
            assert await pool.fetchval("SELECT count(*) FROM event_sources") == 0

    run_db(check, monkeypatch, timeout=20)


def test_chat_controls_and_two_step_deletion_do_not_return_bodies(monkeypatch, tmp_path):
    async def check(pool):
        sup.append(tmp_path, "oc_synthetic", sup.message("synthetic-message", text="private synthetic body"))
        source = await store.save_source("src", {"bridge_dir": str(tmp_path), "bridge_start_from": "beginning"}, db=pool)
        await source_service.run_bridge_pass(source, db=pool, now=sup.NOW)
        backups = tmp_path / "api" / "backups"
        backups.mkdir(parents=True)
        dbname = await pool.fetchval("SELECT current_database()")
        (backups / f"looper-{dbname}-20261101T000000.000000Z.dump").write_bytes(b"synthetic archive")
        (backups / f"looper-{dbname}-20261115T000000.000000Z.dump").write_bytes(b"synthetic archive")
        (backups / "looper-another-db-20260101T000000.000000Z.dump").write_bytes(b"unrelated")
        async with v1_api(tmp_path / "api", pool) as call:
            status, answer = await call("message_stream.chats", {"search": "oc_synthetic"})
            assert status == 200, answer
            (chat,) = answer["result"]["chats"]
            assert chat["chat_id"] == "oc_synthetic" and "events_7d" in chat
            status, answer = await call("message_stream.chat_update", {"chat_id": "oc_synthetic", "included": False})
            assert status == 200 and answer["result"]["inclusion"] == "excluded", answer
            target = {"scope": "message", "message_id": "synthetic-message"}
            status, answer = await call("message_stream.delete", {**target, "confirmation_token": "missing"})
            assert status == 409, answer
            assert await pool.fetchval("SELECT count(*) FROM source_events") == 1
            status, answer = await call("message_stream.delete_preview", target)
            assert status == 200, answer
            preview = answer["result"]
            assert preview["events"] == 1 and preview["backups"]["count"] == 2
            assert preview["backups"]["earliest_at"].startswith("2026-11-01")
            assert len(preview["limits"]) == 4
            assert "private synthetic body" not in str(preview)
            token = preview["confirmation_token"]
            status, _ = await call("message_stream.delete", {**target, "message_id": "other", "confirmation_token": token})
            assert status == 409
            (backups / f"looper-{dbname}-20261118T000000.000000Z.dump").write_bytes(b"new synthetic archive")
            status, _ = await call("message_stream.delete", {**target, "confirmation_token": token})
            assert status == 409
            status, answer = await call("message_stream.delete_preview", target)
            token = answer["result"]["confirmation_token"]
            status, answer = await call("message_stream.delete", {**target, "confirmation_token": token})
            assert status == 200 and answer["result"]["deleted"] == 1, answer
            status, _ = await call("message_stream.delete", {**target, "confirmation_token": token})
            assert status == 409
            assert await pool.fetchval("SELECT count(*) FROM source_deletions") == 1

    run_db(check, monkeypatch, timeout=30)


@pytest.mark.parametrize("target", [
    {"scope": "message"}, {"scope": "message", "message_id": "m", "chat_id": "c"},
    {"scope": "chat"}, {"scope": "range", "start": "2026-11-01", "end": "2026-11-02"},
    {"scope": "range", "start": "2026-11-02T00:00:00Z", "end": "2026-11-01T00:00:00Z"},
    {"scope": "chat", "chat_id": "c", "message_id": "m"},
])
def test_delete_preview_refuses_ambiguous_or_invalid_scopes(monkeypatch, tmp_path, target):
    async def check(pool):
        await store.save_source("src", {}, db=pool)
        async with v1_api(tmp_path, pool) as call:
            status, answer = await call("message_stream.delete_preview", target)
            assert status == 400, answer
            assert await pool.fetchval("SELECT count(*) FROM source_deletions") == 0

    run_db(check, monkeypatch, timeout=20)


def test_expired_confirmation_and_unknown_chat_cannot_delete(monkeypatch, tmp_path):
    from looper.agents.v1_domains import message_stream

    async def check(pool):
        await store.save_source("src", {}, db=pool)
        async with v1_api(tmp_path, pool) as call:
            status, _ = await call("message_stream.delete_preview", {"scope": "chat", "chat_id": "unknown"})
            assert status == 404
            monkeypatch.setattr(message_stream, "CONFIRMATION_SECONDS", -1)
            target = {"scope": "message", "message_id": "synthetic"}
            status, answer = await call("message_stream.delete_preview", target)
            assert status == 200, answer
            status, _ = await call("message_stream.delete", {**target, "confirmation_token": answer["result"]["confirmation_token"]})
            assert status == 409
            assert await pool.fetchval("SELECT count(*) FROM source_deletions") == 0
            await store.save_source("other", {}, db=pool)
            status, _ = await call("message_stream.delete_preview", target)
            assert status == 400

    run_db(check, monkeypatch, timeout=20)
