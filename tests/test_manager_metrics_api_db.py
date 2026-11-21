"""Manager metrics on disposable PostgreSQL and the authenticated v1 API."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from looper.agents import chat_settings
from tests.db_support import DSN, run_db, v1_api

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def test_metrics_switch_is_operator_only_and_reloadable(monkeypatch, tmp_path):
    async def check(pool):
        async with v1_api(tmp_path, pool) as call:
            previous = await chat_settings.settings_view()
            for arguments in ({"metrics_enabled": True}, {"stop_enabled": True, "metrics_enabled": None}):
                status, body = await call("chat_settings.update", arguments, client=True)
                assert status == 403 and body["error"]["code"] == "forbidden"
                assert await chat_settings.settings_view() == previous
                assert await pool.fetchval("SELECT count(*) FROM system_settings") == 0
            status, body = await call("chat_settings.update", {"metrics_enabled": True, "ledger_enabled": True})
            assert status == 200 and body["result"]["metrics_enabled"] is True
            assert body["result"]["ledger_enabled"] is True
            chat_settings.set_metrics_enabled(False)
            await chat_settings.load()
            assert chat_settings.metrics_enabled() is True
            assert (await call("chat_settings.update", {"metrics_enabled": "yes"}))[0] == 400
            await call("chat_settings.update", {"metrics_enabled": None})
            assert chat_settings.metrics_enabled() is False
            assert (await chat_settings.settings_view())["ledger_enabled"] is True
    run_db(check, monkeypatch)


def test_summary_percentiles_exclusions_and_proactive_reply_rates(monkeypatch, tmp_path):
    async def check(pool):
        from looper.db import proactive_deliveries as deliveries
        now = datetime.now(timezone.utc)
        for ack, delivery, origin, replayed, agent in [
            (100, "reply", "user", False, "manager"),
            (300, "empty", "user", False, "manager"),
            (9000, "reply", "user", True, "manager"),
            (9000, "reply", "completion", False, "manager"),
            (9000, "reply", "user", False, "manager-other"),
        ]:
            await pool.execute("INSERT INTO llm_records (agent_id, metadata) VALUES ($1,$2::jsonb)", agent,
                json.dumps({"turn_metrics": {"origin": origin, "replayed": replayed, "chat": "dashboard",
                    "ack_ms": ack, "first_reply_ms": ack * 2, "delivery": delivery}}))
        for source, age, reply in [("gate", 40, True), ("gate", 40, False), ("intent", 5, False)]:
            row = await deliveries.insert({"source": source, "source_ref": None, "manager_id": "manager",
                "channel_id": "web", "chat": "synthetic", "chat_kind": "direct", "external_id": None,
                "outcome": "sent", "sent_at": now - timedelta(minutes=age)})
            if reply:
                await pool.execute("UPDATE proactive_deliveries SET replied_at=$2, reply_match='next_message' WHERE id=$1",
                                   row["id"], now)
        for outcome in ("rejected", "unknown"):
            await deliveries.insert({"source": "gate", "channel_id": "web", "chat": "synthetic",
                "chat_kind": "direct", "outcome": outcome, "sent_at": now - timedelta(minutes=40)})
        async with v1_api(tmp_path, pool) as call:
            for days in (0, 31):
                assert (await call("manager_metrics.get", {"days": days}))[0] == 400
            status, body = await call("manager_metrics.get")
            assert status == 200
            result = body["result"]
            assert result["enabled"] is False and result["window_days"] == 7
            assert result["ack"]["p50_ms"] == 200 and result["ack"]["p95_ms"] == 290
            assert result["ack"]["by_chat"]["dashboard"]["p50_ms"] == 200
            assert result["first_reply"]["p95_ms"] == 580
            assert result["turns"]["by_delivery"] == {"reply": 1, "empty": 1}
            assert result["empty"] == {"count": 1, "ratio": 0.5}
            assert result["proactive"]["reply_rate"] == {"all": 0.5, "by_source": {"gate": 0.5}}
            assert sum(sum(day["by_source"].values()) for day in result["proactive"]["per_day"]) == 5
    run_db(check, monkeypatch)


def test_metrics_configuration_round_trip_requires_operator(monkeypatch):
    async def check(pool):
        from looper.agents import config_imports, config_transfer
        from looper.agents.ops import OpError
        await chat_settings.update({"metrics_enabled": True})
        async with pool.acquire() as conn:
            document = json.loads("".join([part async for part in config_transfer.export_chunks(
                conn, include_projects=False, include_secrets=False)]))
            assert document["settings"]["chat"] == {"metrics_enabled": True}
            await chat_settings.update({"metrics_enabled": False})
            with pytest.raises(OpError):
                await config_imports.authorize(document, "client")
            await config_imports.authorize(document, "operator")
            await config_transfer.run_import(conn, document, apply=False)
            await chat_settings.load()
            assert chat_settings.metrics_enabled() is False
            await config_transfer.run_import(conn, document, apply=True)
            await chat_settings.load()
            assert chat_settings.metrics_enabled() is True
            assert "proactive_deliveries" not in document
        chat_settings.set_metrics_enabled(False)
    run_db(check, monkeypatch)
