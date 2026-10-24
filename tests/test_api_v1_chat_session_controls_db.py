"""Authenticated session controls use the real Manager services and disposable PostgreSQL."""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import api_v1, chat_settings, profile_registry
from looper.agents.ops.clients import ClientOps
from looper.agents.tiers.manager import ManagerAgent, ManagerConfig
from looper.db import agent_sessions, api_clients
from tests.db_support import DSN, run_db
from tests.test_api_v1_auth import _bearer, _send, _serving
from tests.test_api_v1_chat_session_controls import _Manager, _turn
from tests.test_operator_auth import TOKEN
from tests.test_task_agents_db import _profiles

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


@asynccontextmanager
async def _api(tmp_path, pool, manager):
    client, token = await api_clients.create("session controls", pool=pool)
    async with _serving(tmp_path) as server:
        server._manager = manager
        server._client_ops = ClientOps(SimpleNamespace())
        async def call(operation, body=None, *, client=False, method="POST"):
            path = operation if operation.startswith("/") else "/api/v1/" + operation.replace(".", "/")
            status, _, answer = await _send(server, f"{method} {path}",
                {**_bearer(token if client else TOKEN), "X-Looper-Session": "controls"}, json.dumps(body or {}))
            if status == 200 and not operation.startswith("/") and operation != "confirm":
                api_v1.validate_arguments(api_v1.find(method, path).result, answer["result"])
            return status, answer
        yield call, client


def _manager(tmp_path, monkeypatch):
    from looper.agents import agent_settings, session_profile
    from looper.agents.tiers import manager

    # No model runs here; local runtime credentials are deliberately outside this fixture.
    monkeypatch.setattr(profile_registry, "_login_modules", lambda: {})
    for module in (agent_settings, manager, session_profile):
        monkeypatch.setattr(module, "profile_launch_problem", AsyncMock(return_value=None))
    monkeypatch.setattr(agent_settings, "_login_problem", AsyncMock(return_value=None))
    return ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)


@pytest.mark.parametrize("previous_runtime,effect", [(None, "starts"), ("claude-code", "resumes"), ("codex", "new_session")])
def test_session_selection_and_follow_settings_have_real_effects_and_client_provenance(
        tmp_path, monkeypatch, previous_runtime, effect):
    async def run(pool):
        await _profiles(pool)
        if previous_runtime:
            await agent_sessions.save_session("manager", "saved-session", {}, runtime_type=previous_runtime,
                                               provider_id="prov-claude-subscription", profile_id="agent-sonnet")
        manager = _manager(tmp_path, monkeypatch)
        async with _api(tmp_path, pool, manager) as (call, client):
            status, answer = await call("manager_session.get", client=True)
            assert status == 200 and answer["result"]["choices"] and answer["result"]["effective"]["profile"]
            status, answer = await call("manager_session.select_profile", {"profile_id": "agent-sonnet"}, client=True)
            assert status == 200 and answer["result"]["effect"] == effect
            assert (await agent_sessions.get_session("manager"))["selected_profile_id"] == "agent-sonnet"
            metadata = json.loads(await pool.fetchval("SELECT metadata FROM llm_records ORDER BY id DESC LIMIT 1"))
            assert metadata["session_profile"] == {
                "from": None, "to": "agent-sonnet", "effect": effect, "channel": "api", "chat": None,
                "sender": client["id"], "group": False, "client": {"id": client["id"], "name": client["name"]},
                "via": "client:session controls"}
            assert (await call("manager_session.select_profile", {"profile_id": "agent-sonnet"}))[1]["result"]["effect"] == "current"
            status, answer = await call("manager_session.select_profile", {"profile_id": None})
            assert status == 200 and answer["result"]["selected"] is None
            assert (await agent_sessions.get_session("manager"))["selected_profile_id"] is None
            assert await pool.fetchval("SELECT count(*) FROM manager_actions WHERE tool='manager_session.select_profile' AND status='ok'") == 3
            from looper.agents.bus import MessageBus
            from tests.test_manager_new_session import _web
            await call("manager_session.select_profile", {"profile_id": "agent-sonnet"})
            manager._bus = MessageBus(runtime_root=tmp_path)
            try:
                await manager._start_new_session(_web("/new"))
                assert (await agent_sessions.get_session("manager"))["selected_profile_id"] is None
                assert (await call("manager_session.get"))[1]["result"]["selected"] is None
            finally:
                await manager.stop()
                await manager._bus.shutdown()
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("profile_id", ["agent-one-off", "missing"])
def test_incompatible_or_missing_profiles_preserve_session(tmp_path, monkeypatch, profile_id):
    async def run(pool):
        await _profiles(pool)
        await agent_sessions.select_profile("manager", "agent-sonnet")
        before = await agent_sessions.get_session("manager")
        async with _api(tmp_path, pool, _manager(tmp_path, monkeypatch)) as (call, _):
            status, answer = await call("manager_session.select_profile", {"profile_id": profile_id})
            assert status == 400 and answer["error"]["code"] == "invalid"
            assert await agent_sessions.get_session("manager") == before
    run_db(run, monkeypatch, timeout=30)


def test_client_setting_update_waits_for_confirmation_and_null_restores_default(tmp_path, monkeypatch):
    async def run(pool):
        monkeypatch.setattr(chat_settings, "_stop_enabled", False)
        async with _api(tmp_path, pool, _Manager(turn=_turn())) as (call, _):
            status, asked = await call("chat_settings.update", {"stop_enabled": True}, client=True)
            assert status == 202 and asked["status"] == "needs_confirmation"
            assert "chat.stop_enabled" in asked["impact"] and "Telegram" in asked["impact"]
            assert await chat_settings.load() is False
            status, answer = await call("confirm", {"confirmation_id": asked["confirmation_id"]}, client=True)
            assert status == 200 and answer["result"] == {"stop_enabled": True}
            assert await chat_settings.load() is True
            status, answer = await call("chat_settings.update", {"stop_enabled": None})
            assert status == 200 and answer["result"] == {"stop_enabled": False}
            assert await chat_settings.load() is chat_settings.DEFAULT_STOP_ENABLED
            assert await pool.fetchval("SELECT count(*) FROM system_settings WHERE key=$1", chat_settings.STOP_ENABLED_KEY) == 0
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("operation,body,method,path", [
    ("manager_session.get", {}, "GET", "/api/manager/session"),
    ("manager_session.select_profile", {"profile_id": None}, "PUT", "/api/manager/session/profile"),
    ("operator_chat.turn", {"session_id": "s1"}, "GET", "/api/chat/turn"),
    ("operator_chat.stop", {"message_id": "m1"}, "POST", "/api/chat/stop"),
    ("chat_settings.get", {}, "GET", "/api/chat-settings"),
    ("chat_settings.update", {"stop_enabled": True}, "PUT", "/api/chat-settings"),
])
def test_retired_routes_have_authenticated_v1_positive_controls(tmp_path, monkeypatch, operation, body, method, path):
    async def run(pool):
        from tests.test_manager_session_api import _manager
        manager = _manager() if operation.startswith("manager_session") else _Manager(turn=_turn())
        monkeypatch.setattr(chat_settings, "_stop_enabled", True)
        async with _api(tmp_path, pool, manager) as (call, _):
            status, answer = await call(operation, body)
            assert status == 200 and answer["result"]
            assert (await call(path, body, method=method))[0] == 404
    run_db(run, monkeypatch, timeout=30)
