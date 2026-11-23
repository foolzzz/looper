"""Daily alert state, concurrency, delivery and recovery on disposable PostgreSQL."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import chat_settings, gate_service, metrics_alerts as alerts, morning_brief, proactive_delivery
from looper.db import system_settings
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
NOW = datetime(2026, 11, 23, 12, tzinfo=timezone.utc)


def figures(value=.5, misses=0):
    previous = {"n": 100, "value": .95, "wilson": [.9, .99]}
    rate = {"n": 100, "value": value, "wilson": [max(0, value - .05), min(1, value + .05)], "previous": previous}
    return {"tasks": {"success_rate": rate}, "steps": {"first_pass_rate": rate},
            "window": {"since": "2026-11-16", "until": "2026-11-23"},
            "comparison_window": {"since": "2026-10-19", "until": "2026-11-16"},
            "alert_figures": {"cost": {"provider_priced": {"n": 0, "median": None},
                                      "equivalent": {"n": 0, "median": None}, "previous": {}},
                              "misses": {"n": 100, "count": misses, "previous": {"n": 100, "count": 0}}}}


def setup(monkeypatch):
    monkeypatch.setattr(alerts, "_enabled", True)
    monkeypatch.setattr(chat_settings, "_delivery_enabled", False)
    monkeypatch.setattr(alerts.proactive_delivery, "load_policy", lambda: proactive_delivery.DeliveryPolicy(timezone=timezone.utc))
    target = SimpleNamespace(channel_id="channel-telegram", in_group=False,
                             target=SimpleNamespace(id="424242", group=False))
    monkeypatch.setattr(gate_service, "dashboard_target", AsyncMock(return_value=target))
    monkeypatch.setattr(alerts.db_metrics, "missing_attempts", AsyncMock(return_value=0))
    compute = AsyncMock(return_value=figures(.75))
    monkeypatch.setattr(alerts.metrics, "compute", compute)
    registry = SimpleNamespace(notify=AsyncMock(return_value=(True, None)))
    return compute, registry


def test_daily_convergence_worsening_and_recovery(monkeypatch):
    compute, registry = setup(monkeypatch)
    async def check(pool):
        today = NOW
        service = alerts.MetricsAlertService(registry, clock=lambda: today)
        await service.run_once()
        assert registry.notify.await_count == 1
        text = registry.notify.call_args.args[2]
        assert "任务成功率下降" in text and "一次通过率下降" in text and "n=100" in text and "95%" in text
        today += timedelta(days=1)
        await service.run_once()
        assert registry.notify.await_count == 1
        compute.return_value = figures(.65)
        today += timedelta(days=1)
        await service.run_once()
        assert registry.notify.await_count == 2
        compute.return_value = figures(.95)
        today += timedelta(days=1)
        await service.run_once()
        assert registry.notify.await_count == 3 and "已恢复" in registry.notify.call_args.args[2]
        assert not await system_settings.get_settings(["metrics_alert:success_drop", "metrics_alert:first_pass_drop"])
        today += timedelta(days=1)
        await service.run_once()
        assert registry.notify.await_count == 3
    run_db(check, monkeypatch)


def test_two_services_and_restart_claim_one_daily_check(monkeypatch):
    compute, registry = setup(monkeypatch)
    async def check(pool):
        a = alerts.MetricsAlertService(registry, clock=lambda: NOW)
        b = alerts.MetricsAlertService(registry, clock=lambda: NOW)
        await asyncio.gather(a.run_once(), b.run_once())
        await alerts.MetricsAlertService(registry, clock=lambda: NOW).run_once()
        assert compute.await_count == registry.notify.await_count == 1
        assert (await system_settings.get_settings([alerts.CHECKED_KEY]))[alerts.CHECKED_KEY] == NOW.date().isoformat()
    run_db(check, monkeypatch)


def test_missing_is_immediate_alone_and_once_per_day_and_count(monkeypatch):
    compute, registry = setup(monkeypatch)
    missing = alerts.db_metrics.missing_attempts
    async def check(pool):
        today = NOW.replace(hour=3)
        service = alerts.MetricsAlertService(registry, clock=lambda: today)
        missing.return_value = 1
        await service.run_once()
        await service.run_once()
        assert compute.await_count == 0 and registry.notify.await_count == 1
        assert "尝试缺失" in registry.notify.call_args.args[2]
        missing.return_value = 2
        await service.run_once()
        missing.return_value = 1
        await service.run_once()
        assert registry.notify.await_count == 2
        today = NOW
        await service.run_once()
        assert registry.notify.await_count == 3 and "尝试缺失" not in registry.notify.call_args.args[2]
        today += timedelta(days=1)
        await service.run_once()
        assert registry.notify.await_count == 4  # missing alone; unchanged daily rules converge
    run_db(check, monkeypatch)


def test_quiet_hours_defer_normal_missing_alert_into_brief(monkeypatch):
    compute, registry = setup(monkeypatch)
    monkeypatch.setattr(chat_settings, "_delivery_enabled", True)
    alerts.db_metrics.missing_attempts.return_value = 1
    async def check(pool):
        await alerts.MetricsAlertService(registry, clock=lambda: NOW.replace(hour=3)).run_once()
        registry.notify.assert_not_awaited()
        row = await pool.fetchrow("SELECT source, urgency, decision, reason, digest_date FROM proactive_deliveries")
        assert (row["source"], row["urgency"], row["decision"], row["reason"]) == (
            "metrics_alert", "normal", "digest", "quiet_hours")
        from looper.db import morning_briefs
        async with pool.acquire() as conn:
            digests = await morning_briefs.digests(conn, channel_id="channel-telegram", chat="424242",
                until=NOW, dates=[NOW.date()])
        rendered = morning_brief.render(day=NOW.date(), needs=None, reminders=[], digests=digests, tz=timezone.utc)
        assert "质量告警" in rendered and "免打扰" in rendered
    run_db(check, monkeypatch)


def test_unjudged_does_not_recover_and_failure_waits_until_tomorrow(monkeypatch):
    compute, registry = setup(monkeypatch)
    async def check(pool):
        today = NOW
        service = alerts.MetricsAlertService(registry, clock=lambda: today)
        await service.run_once()
        state = await system_settings.get_settings(["metrics_alert:success_drop"])
        compute.return_value = figures()
        compute.return_value["tasks"]["success_rate"] = {"n": 19, "insufficient": True}
        compute.return_value["steps"]["first_pass_rate"] = {"error": "unavailable"}
        today += timedelta(days=1)
        await service.run_once()
        assert registry.notify.await_count == 1
        assert await system_settings.get_settings(["metrics_alert:success_drop"]) == state
        compute.side_effect = RuntimeError("unavailable")
        today += timedelta(days=1)
        await service.run_once()
        await service.run_once()
        assert compute.await_count == 3
    run_db(check, monkeypatch)


def test_switch_loaded_update_reset_and_get_never_read_db(monkeypatch):
    async def check(pool):
        await alerts.load()
        assert alerts.view()["enabled"] is False
        assert (await alerts.update(True))["enabled"] is True
        assert await pool.fetchval("SELECT value FROM system_settings WHERE key=$1", alerts.ENABLED_KEY) == "true"
        await alerts.load()
        assert alerts.view()["enabled"] is True
        assert (await alerts.update(None))["enabled"] is False
        assert await pool.fetchval("SELECT value FROM system_settings WHERE key=$1", alerts.ENABLED_KEY) is None
        with pytest.raises(ValueError):
            await alerts.update("true")
        monkeypatch.setattr(system_settings, "get_settings", AsyncMock(side_effect=AssertionError("off read")))
        assert len(alerts.view()["rules"]) == 5
        await alerts.MetricsAlertService(SimpleNamespace()).run_once()
    run_db(check, monkeypatch)


def test_real_outcomes_cross_threshold_and_generate_only_one_message(monkeypatch):
    from looper.agents import metrics
    original = metrics.compute
    _, registry = setup(monkeypatch)
    monkeypatch.setattr(metrics, "compute", original)
    async def check(pool):
        await pool.execute("INSERT INTO projects(id,name) VALUES ('quality','Synthetic project')")
        await pool.execute("INSERT INTO tasks(id,requirement,status,project_id) "
                           "SELECT 'quality-' || n, 'Synthetic task', 'completed', 'quality' FROM generate_series(1,40) n")
        await pool.execute(
            "INSERT INTO task_outcomes(task_id,run_no,outcome,stop_cause,ended_at) "
            "SELECT 'quality-' || n,1,CASE WHEN n <= 10 THEN 'failed' ELSE 'completed' END,'planner_end', "
            "CASE WHEN n <= 20 THEN $1::timestamptz - interval '1 day' ELSE $1 - interval '10 days' END "
            "FROM generate_series(1,40) n", NOW)
        service = alerts.MetricsAlertService(registry, clock=lambda: NOW)
        await service.run_once()
        assert registry.notify.await_count == 1
        assert "50.0% 对 100.0%" in registry.notify.call_args.args[2]
        await service.run_once()
        assert registry.notify.await_count == 1
    run_db(check, monkeypatch)


def test_settings_http_refuses_client_and_accepts_operator(monkeypatch, tmp_path):
    from tests.db_support import v1_api
    async def check(pool):
        await alerts.load()
        async with v1_api(tmp_path, pool) as call:
            for action in ("get", "update"):
                status, answer = await call(f"metrics_alerts.{action}", {"enabled": True} if action == "update" else {},
                                            client=True)
                assert status == 403 and answer["error"]["code"] == "forbidden"
            status, answer = await call("metrics_alerts.update", {"enabled": True})
            assert status == 200 and answer["result"]["enabled"] is True
            status, answer = await call("metrics_alerts.get")
            assert status == 200 and answer["result"]["enabled"] is True
            for value in ("true", 1):
                status, _ = await call("metrics_alerts.update", {"enabled": value})
                assert status == 400
    run_db(check, monkeypatch)


def test_missing_recovery_joins_daily_message_once(monkeypatch):
    compute, registry = setup(monkeypatch)
    async def check(pool):
        today = NOW.replace(hour=3)
        service = alerts.MetricsAlertService(registry, clock=lambda: today)
        alerts.db_metrics.missing_attempts.return_value = 1
        await service.run_once()
        alerts.db_metrics.missing_attempts.return_value = 0
        today = NOW
        await service.run_once()
        assert registry.notify.await_count == 2
        assert "尝试缺失：已恢复" in registry.notify.call_args.args[2]
        assert not await system_settings.get_settings(["metrics_alert:missing_attempts"])
        today += timedelta(days=1)
        await service.run_once()
        assert registry.notify.await_count == 2
    run_db(check, monkeypatch)


def test_cost_recovery_only_requires_the_previously_firing_basis(monkeypatch):
    compute, registry = setup(monkeypatch)
    async def check(pool):
        today = NOW
        report = figures(.95)
        cost = report["alert_figures"]["cost"]
        cost["equivalent"] = {"n": 20, "median": 2}
        cost["previous"]["equivalent"] = {"n": 20, "median": 1}
        compute.return_value = report
        service = alerts.MetricsAlertService(registry, clock=lambda: today)
        await service.run_once()
        assert registry.notify.await_count == 1
        today += timedelta(days=1)
        cost["equivalent"] = {"n": 19, "median": 1}
        await service.run_once()
        assert registry.notify.await_count == 1  # not enough samples cannot prove recovery
        today += timedelta(days=1)
        cost["equivalent"] = {"n": 20, "median": 1}
        await service.run_once()
        assert registry.notify.await_count == 2 and "已恢复" in registry.notify.call_args.args[2]
    run_db(check, monkeypatch)
