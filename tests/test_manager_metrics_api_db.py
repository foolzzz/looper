"""Manager metrics on disposable PostgreSQL and the authenticated v1 API."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from looper.agents import api_v1, chat_settings
from tests.db_support import DSN, run_db, v1_api

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def test_metrics_setting_is_high_risk_and_separate_from_manager_tools():
    from looper.agents.ops.service import MANAGER_TOOLS

    for action in ("get", "update"):
        operation = api_v1.find("POST", f"/api/v1/manager_metrics_settings/{action}")
        assert operation.operator_only and operation.tool is None
    assert operation.risk == "high" and operation.confirms
    assert all(chat_settings.METRICS_ENABLED_KEY not in json.dumps(tool.schema) for tool in MANAGER_TOOLS)
    import asyncio

    for value in (True, False, None):
        impact = asyncio.run(api_v1.domain_for(operation).impact(
            None, operation, {"enabled": value}, api_v1.OPERATOR, {}))
        assert f"manager.metrics.enabled to {str(bool(value)).lower()}" in impact


def test_metrics_switch_is_operator_only_and_reloadable(monkeypatch, tmp_path):
    async def check(pool):
        async with v1_api(tmp_path, pool) as call:
            previous = await chat_settings.settings_view()
            for action in ("get", "update"):
                status, body = await call(f"manager_metrics_settings.{action}",
                                          {"enabled": True} if action == "update" else {}, client=True)
                assert status == 403 and body["error"]["code"] == "forbidden"
                assert await pool.fetchval("SELECT count(*) FROM system_settings") == 0
            assert (await call("manager_metrics_settings.get"))[1]["result"] == {"enabled": False}
            for value in (True, False, None):
                status, body = await call("manager_metrics_settings.update", {"enabled": value})
                assert status == 200 and body["result"] == {"enabled": bool(value)}
                chat_settings.set_metrics_enabled(not bool(value))
                await chat_settings.load()
                assert chat_settings.metrics_enabled() is bool(value)
                assert await chat_settings.settings_view() == previous
            for arguments in ({}, {"enabled": "yes"}, {"enabled": 1}, {"metrics_enabled": True}):
                assert (await call("manager_metrics_settings.update", arguments))[0] == 400
            assert await pool.fetchval("SELECT count(*) FROM system_settings") == 0
            status, body = await call("chat_settings.update", {"stop_enabled": True, "metrics_enabled": True})
            assert status == 400
            assert await chat_settings.settings_view() == previous
            assert await pool.fetchval("SELECT count(*) FROM system_settings") == 0
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
        await chat_settings.update_metrics(True)
        async with pool.acquire() as conn:
            document = json.loads("".join([part async for part in config_transfer.export_chunks(
                conn, include_projects=False, include_secrets=False)]))
            assert document["settings"]["chat"] == {"metrics_enabled": True}
            await chat_settings.update_metrics(False)
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


def test_task_manager_refusals_only_in_work_mode(monkeypatch, tmp_path):
    from collections import Counter
    from looper.agents import task_conversations_settings, task_manager_safety

    monkeypatch.setattr(task_manager_safety, "refusal_counts", Counter({
        ("review_hook_disabled", "resume", "unknown"): 2,
        ("cli_unverified", "launch", "2.1.333"): 1}))

    async def check(pool):
        async with v1_api(tmp_path, pool) as call:
            for enabled in (False, True, False):
                monkeypatch.setattr(task_conversations_settings, "_enabled", enabled)
                status, body = await call("manager_metrics.get")
                assert status == 200
                result = body["result"]
                if enabled:
                    assert result["work_mode"]["task_manager_launch_refusals_total"] == [
                        {"reason": "cli_unverified", "phase": "launch", "cli_version": "2.1.333", "count": 1},
                        {"reason": "review_hook_disabled", "phase": "resume", "cli_version": "unknown", "count": 2}]
                else:
                    assert "work_mode" not in result
    run_db(check, monkeypatch)
