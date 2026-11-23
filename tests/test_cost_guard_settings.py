"""Cost guard observation settings belong to the operator, not agent chat controls."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import api_v1, chat_settings, cost_guard_settings as settings
from looper.agents.action_levels import ActionLevel
from looper.agents.ops.clients import ClientOps
from looper.agents.ops.service import MANAGER_TOOLS
from tests.test_api_v1_auth import CLIENT_TOKEN, _bearer, _send, _serving, clients  # noqa: F401
from tests.test_operator_auth import TOKEN


@pytest.fixture
def stored(monkeypatch):
    values, writes = {}, []

    async def read(keys, **kwargs):
        return {key: values[key] for key in keys if key in values}

    async def write(changes, **kwargs):
        writes.append(changes)
        for key, value in changes.items():
            if value is None:
                values.pop(key, None)
            else:
                values[key] = value

    monkeypatch.setattr(settings.db_settings, "get_settings", read)
    monkeypatch.setattr(settings.db_settings, "put_settings", write)
    monkeypatch.setattr(settings, "_config", settings.Config())
    monkeypatch.setattr(settings, "_problem", None)
    return values, writes


def test_no_default_daily_caps_and_observe_survives_reload(stored):
    async def check():
        await settings.load()
        assert settings.cached() == settings.Config()
        assert stored == ({}, [])
        await settings.update({"mode": "observe", "daily_usd": 2, "roles": {
            "outbound_check": {"daily_equivalent_usd": 0.5}}})
        await settings.load()
        assert settings.cached().mode is settings.Mode.OBSERVE
        assert settings.cached().daily_usd == 2
        assert settings.cached().daily_equivalent_usd is None
        assert settings.cached().roles["outbound_check"].daily_equivalent_usd == 0.5
        await settings.update({"mode": None, "daily_usd": None})
        assert settings.cached().mode is settings.Mode.OFF
        assert settings.cached().daily_usd is None
        assert "cost_guard.mode" not in stored[0]
    asyncio.run(check())


@pytest.mark.parametrize("body", [
    {"mode": "invalid"}, {"mode": True}, {"daily_usd": True}, {"daily_usd": -1},
    {"daily_usd": float("nan")}, {"daily_usd": float("inf")}, {"unknown": 1}, {},
    {"roles": {"worker": {"daily_usd": 1}}}, {"roles": {"advisor": {"fallback": "cheap"}}},
    {"unanswered_pushes": {}}, {"unanswered_pushes": {"window_days": True}},
    {"unanswered_pushes": {"min_pushes": 0}}, {"unanswered_pushes": {"snooze_days": 366}},
    {"unanswered_pushes": {"pause_after_days": 31}}, {"unanswered_pushes": {"extra": 1}},
])
def test_invalid_update_is_atomic(stored, body):
    async def check():
        with pytest.raises(ValueError):
            await settings.update(body)
        assert stored == ({}, [])
        assert settings.cached() == settings.Config()
    asyncio.run(check())


def test_invalid_stored_configuration_is_off_and_explained(stored):
    stored[0].update({"cost_guard.mode": "enforce", "cost_guard.daily_usd": "garbage"})
    asyncio.run(settings.load())
    assert settings.cached().mode is settings.Mode.OFF
    assert settings.view()["problem"]
    assert stored[1] == []


def test_budget_zero_is_a_valid_observation_threshold(stored):
    asyncio.run(settings.update({"daily_usd": 0}))
    assert settings.cached().daily_usd == 0


@pytest.mark.parametrize("action", ["get", "update"])
@pytest.mark.usefixtures("clients")
def test_client_token_cannot_reach_guard_settings(tmp_path, monkeypatch, stored, action):
    operation = api_v1.find("POST", f"/api/v1/cost_guard/{action}")
    assert operation.operator_only and operation.tool is None
    assert operation.level is ActionLevel.INSIDE
    assert all("cost_guard" not in json.dumps(tool.schema) for tool in MANAGER_TOOLS)
    run = AsyncMock(side_effect=AssertionError("client reached operator cost settings"))
    monkeypatch.setattr(api_v1.domain_for(operation), "run", run)

    async def check():
        async with _serving(tmp_path) as server:
            server._client_ops = ClientOps(SimpleNamespace())
            status, _, answer = await _send(server, f"POST {operation.path}", _bearer(CLIENT_TOKEN),
                                            json.dumps({"mode": "observe"} if action == "update" else {}))
            assert status == 403 and answer["error"]["code"] == "forbidden"
            run.assert_not_awaited()
    asyncio.run(check())
    assert stored == ({}, [])


def test_operator_can_enable_observe_and_read_snapshot(tmp_path, monkeypatch, stored):
    from looper.agents import cost_guard
    from looper.db import manager_actions

    monkeypatch.setattr(manager_actions, "record", AsyncMock(return_value=1))
    monkeypatch.setattr(cost_guard, "refresh", AsyncMock())
    snapshot = {"counters": [], "recent": [], "recommended_daily_usd": None,
                "recommended_daily_equivalent_usd": None}
    monkeypatch.setattr(cost_guard, "current", lambda: SimpleNamespace(snapshot=lambda: snapshot))

    async def check():
        async with _serving(tmp_path) as server:
            server._client_ops = ClientOps(SimpleNamespace())
            for action, body in (("update", {"mode": "observe", "daily_usd": 1}), ("get", {})):
                status, _, answer = await _send(server, f"POST /api/v1/cost_guard/{action}",
                                                _bearer(TOKEN), json.dumps(body))
                assert status == 200, answer
                assert answer["result"]["mode"] == "observe"
                assert answer["result"]["daily_usd"] == 1
                assert answer["result"]["recent"] == []
        assert cost_guard.refresh.await_count == 1
    asyncio.run(check())


def test_guard_settings_never_expand_chat_settings(stored):
    with pytest.raises(ValueError, match="unknown setting"):
        chat_settings.checked({"cost_guard": {"mode": "observe"}})
    operation = api_v1.find("POST", "/api/v1/cost_guard/update")
    assert operation.confirms and operation.risk == "high"
    impact = asyncio.run(api_v1.domain_for(operation).impact(
        None, operation, {"daily_usd": None}, api_v1.OPERATOR, {}))
    assert "Enforce holds task dispatches" in impact and "Off and observe do not" in impact
    assert 'soft stop' in impact and 'no caps' in impact
    assert 'fail' in impact and 'reservations' in impact and 'closing turns' in impact
    assert "Spend refusals take precedence" in impact
    assert "Rejected windows and quota failures hold all runs" in impact
    assert "95% utilization holds only schedule firings and background roles" in impact
    assert "quota-failure" in impact and "recorded only" not in impact
    assert "Needs you" in impact
    assert stored == ({}, [])


def test_unanswered_rule_stays_off_until_reply_metrics_are_on(stored):
    from looper.agents import chat_settings

    assert settings.view()["unanswered_pushes"] == {
        "enabled": False, "window_days": 3, "min_pushes": 15, "pause_after_days": 3, "snooze_days": 30}
    assert settings.cached() == settings.Config()

    async def check():
        with pytest.raises(ValueError, match="先打开 manager.metrics.enabled：没有推送和回复记录就无法判断没人回"):
            await settings.update({"unanswered_pushes": {"enabled": True}})
        assert stored == ({}, [])
        chat_settings.set_metrics_enabled(True)
        try:
            await settings.update({"unanswered_pushes": {"enabled": True, "min_pushes": 15}})
        finally:
            chat_settings.set_metrics_enabled(False)
        assert settings.cached().unanswered == settings.UnansweredRule(enabled=True, min_pushes=15)
        assert stored[0]["cost_guard.unanswered_pushes.enabled"] == "true"
        assert stored[0]["cost_guard.unanswered_pushes.min_pushes"] == "15"
        assert "cost_guard.unanswered_pushes.window_days" not in stored[0]

    asyncio.run(check())
    operation = api_v1.find("POST", "/api/v1/cost_guard/update")
    assert operation.summary == "Configure cost guard admission."
    assert api_v1.find("POST", "/api/v1/cost_guard/get").summary == "Read cost guard observations."
    assert operation.description.startswith(operation.summary)
    assert "Needs you" in operation.description and "recorded only" not in operation.description


def test_client_cannot_import_cost_guard_even_as_an_empty_section():
    from looper.agents import config_imports
    from looper.agents.ops import ErrorCode, OpError

    async def check():
        for guard in ({}, {"mode": "off"}, {"daily_usd": None}):
            document = {"settings": {"cost_guard": guard}}
            with pytest.raises(OpError) as failure:
                await config_imports.authorize(document, "client")
            assert failure.value.code is ErrorCode.FORBIDDEN
            assert await config_imports.authorize(document, "operator") == document
    asyncio.run(check())


def test_cost_guard_configuration_export_preview_apply_readback(monkeypatch):
    from looper.agents import config_imports, config_transfer, cost_guard
    from tests.db_support import DSN, run_db

    if not DSN:
        pytest.skip("LOOPER_TEST_DATABASE_URL is not set")
    monkeypatch.setattr(settings, "_config", settings.Config())
    refresh = AsyncMock()
    monkeypatch.setattr(cost_guard, "refresh", refresh)

    async def check(pool):
        await settings.update({"mode": "observe", "daily_usd": 2,
                               "roles": {"outbound_check": {"daily_equivalent_usd": 0.1}}})
        async with pool.acquire() as conn:
            exported = json.loads("".join([chunk async for chunk in config_transfer.export_chunks(
                conn, include_projects=False, include_secrets=False)]))
        portable = exported["settings"]["cost_guard"]
        assert portable == {"mode": "observe", "daily_usd": 2.0,
                            "roles": {"outbound_check": {"daily_equivalent_usd": 0.1}}}
        assert "recent" not in json.dumps(portable) and "reservation" not in json.dumps(portable)
        await pool.execute("DELETE FROM system_settings WHERE key LIKE 'cost_guard.%'")
        document = {"format": config_transfer.FORMAT, "settings": {"cost_guard": portable}}
        plan = await config_imports._run(document, apply=False)
        assert not plan.applied
        assert await pool.fetchval("SELECT count(*) FROM system_settings WHERE key LIKE 'cost_guard.%'") == 0
        plan = await config_imports._run(document, apply=True)
        assert plan.applied
        await settings.load()
        assert settings.cached().mode is settings.Mode.OBSERVE
        assert settings.cached().roles["outbound_check"].daily_equivalent_usd == 0.1
        refresh.assert_awaited_once()
        bad = {"format": config_transfer.FORMAT, "settings": {"cost_guard": {"mode": "invalid"}}}
        before = await pool.fetch("SELECT key,value FROM system_settings WHERE key LIKE 'cost_guard.%' ORDER BY key")
        plan = await config_imports._run(bad, apply=True)
        assert any(change.action is config_transfer.Action.SKIPPED for change in plan.changes)
        assert await pool.fetch("SELECT key,value FROM system_settings WHERE key LIKE 'cost_guard.%' ORDER BY key") == before
    run_db(check, monkeypatch, timeout=30)


def test_enforcement_mode_is_explicitly_accepted(stored):
    asyncio.run(settings.update({"mode": "enforce"}))
    assert settings.cached().mode is settings.Mode.ENFORCE
