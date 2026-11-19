"""The message stream's operations through the v1 API: status and runs, the switch, and who may call them."""

import pytest

from looper.agents import source_service
from looper.db import message_stream as store
from tests import msgflow_support as sup
from tests.db_support import DSN, run_db, v1_api

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


@pytest.mark.parametrize("action,body", [("status", {}), ("runs", {}), ("configure", {"ingest_mode": "off"})])
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
                                        {"ingest_mode": "bridge", "bridge_dir": str(tmp_path), "user_open_id": sup.ME})
            assert status == 200, answer
            source = answer["result"]
            assert (source["source_id"], source["ingest_mode"], source["cron"]) == (
                "lark-user", "bridge", source_service.BRIDGE_CRON)
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
