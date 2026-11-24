"""Collection is an explicit operator action, separate from the bridge switch."""

import pytest

from looper.db import message_stream as store
from tests.db_support import DSN, run_db, v1_api

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


@pytest.mark.parametrize(("action", "body"), [
    ("get", {}), ("update", {"fields": {"alerts_enabled": True}}),
    ("mode_update", {"ingest_mode": "collect", "expected_generation": "0", "bridge_stopped": True}),
    ("run_now", {}),
])
def test_only_operator_can_configure_switch_or_run_collection(monkeypatch, tmp_path, action, body):
    async def check(pool):
        await store.save_source("synthetic", {}, db=pool)
        async with v1_api(tmp_path, pool) as call:
            status, _ = await call("message_collection." + action, {"source_id": "synthetic", **body}, client=True)
            assert status == 403
        for table in ("source_collection_settings", "source_parity_sessions", "source_stream_alerts", "source_runs"):
            assert await pool.fetchval(f"SELECT count(*) FROM {table}") == 0

    run_db(check, monkeypatch, timeout=30)


def test_get_has_defaults_without_creating_state_and_update_cannot_enable_collect(monkeypatch, tmp_path):
    async def check(pool):
        await store.save_source("synthetic", {}, db=pool)
        async with v1_api(tmp_path, pool) as call:
            status, answer = await call("message_collection.get", {"source_id": "synthetic"})
            assert status == 200, answer
            view = answer["result"]
            assert (view["ingest_mode"], view["generation"]) == ("off", "0")
            assert view["settings"]["collect_cron"] == "*/10 * * * *"
            assert view["settings"]["alerts_enabled"] is False
            assert view["latest_run"] is None and view["parity"] is None and view["alerts"] == []
            assert await pool.fetchval("SELECT count(*) FROM source_collection_settings") == 0
            status, answer = await call("message_collection.update", {"source_id": "synthetic", "fields": {
                "lark_profile": "synthetic-reader", "user_open_id": "ou_synthetic", "collect_cron": "*/20 * * * *"}})
            assert status == 200, answer
            assert answer["result"]["settings"]["lark_profile"] == "synthetic-reader"
            assert (await store.get_source("synthetic", db=pool))["ingest_mode"] == "off"
            status, _ = await call("message_stream.configure", {"source_id": "synthetic", "ingest_mode": "collect"})
            assert status == 400
            status, _ = await call("message_collection.get", {"source_id": "missing"})
            assert status == 404

    run_db(check, monkeypatch, timeout=30)


@pytest.mark.parametrize("fields", [
    {"ingest_mode": "collect"}, {"lark_profile": "../default"}, {"collect_cron": "not a cron"},
    {"sweep_cycle_runs": 0}, {"sweep_cycle_runs": 289}, {"user_open_id": "has space"},
    {"alert_channel": "lark"}, {"alert_channel": "lark", "alert_chat": "oc_some_group"},
])
def test_invalid_settings_are_rejected_before_any_write(monkeypatch, tmp_path, fields):
    async def check(pool):
        await store.save_source("synthetic", {}, db=pool)
        async with v1_api(tmp_path, pool) as call:
            status, _ = await call("message_collection.update", {"source_id": "synthetic", "fields": fields})
            assert status in (400, 409)
            assert await pool.fetchval("SELECT count(*) FROM source_collection_settings") == 0

    run_db(check, monkeypatch, timeout=30)


def test_missing_attestation_and_stale_generation_cannot_switch(monkeypatch, tmp_path):
    async def check(pool):
        await store.save_source("synthetic", {"bridge_dir": str(tmp_path)}, db=pool)
        async with v1_api(tmp_path, pool) as call:
            for fields in ({"ingest_mode": "collect"}, {"ingest_mode": "collect", "bridge_stopped": False,
                           "expected_generation": "0"}):
                status, _ = await call("message_collection.mode_update", {"source_id": "synthetic", **fields})
                assert status in (400, 409)
            assert (await store.get_source("synthetic", db=pool))["ingest_mode"] == "off"
            assert await pool.fetchval("SELECT count(*) FROM source_parity_sessions") == 0
            status, answer = await call("message_collection.mode_update", {
                "source_id": "synthetic", "ingest_mode": "bridge"})
            assert status == 200, answer
            assert answer["result"]["mode"] == "bridge"
            status, _ = await call("message_collection.run_now", {"source_id": "synthetic"})
            assert status == 409

    run_db(check, monkeypatch, timeout=30)
