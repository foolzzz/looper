"""Manager account/model handlers, schema checks and disposable database readback."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from looper.agents import profile_registry as registry
from looper.agents.ops import Turn
from looper.agents.ops.config import TOOLS
from looper.agents.ops.service import ManagerOps, Platform
from looper.db import manager_actions, profile_registry as db
from tests.test_catalog_db import _call
from tests.test_llm_models_db import DSN, _run
from tests.test_ops_tasks_db import _Chat

needs_db = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
SECRET = "sk-synthetic-manager-only"


def _tool(name):
    return next(t for t in TOOLS if t.name == name)


async def _ok(chat, tool_name, **arguments):
    (answer,) = await chat.say("配置账户和模型", (tool_name, arguments))
    assert answer["status"] == "ok", answer
    assert SECRET not in json.dumps(answer)
    return answer["result"]


def test_model_reference_schema_and_fixed_descriptions():
    providers, models = _tool("looper_providers"), _tool("looper_models")
    assert providers.schema["properties"]["action"]["enum"] == ["list", "register"]
    assert providers.description == ("LLM accounts: list vendors and accounts; register an API-key account "
        "without its key. Never take or pass a key; the user enters it on the dashboard.")
    assert models.description == ("LLM models: list (optionally compatible with a runtime); create; update "
        "settings; delete an unused model with confirmation. A model is one provider, exact name and "
        "protocol; use its id or full reference.")
    assert models.schema["additionalProperties"] is False
    reference = models.schema["properties"]["model"]["oneOf"]
    assert reference[0]["type"] == "string"
    assert reference[1]["required"] == ["provider", "name", "protocol"]
    assert reference[1]["additionalProperties"] is False


@pytest.mark.parametrize("name,arguments", [
    ("looper_providers", {"action": "update", "provider": "prov-test"}),
    ("looper_providers", {"action": "register", "name": "account", "vendor_type": "custom", "api_key": SECRET}),
    ("looper_providers", {"action": "register", "name": "account", "vendor_type": "custom", "token": SECRET}),
    ("looper_providers", {"action": "register", "name": "account", "vendor_type": "custom", "credential": SECRET}),
    ("looper_providers", {"action": "register", "name": "account", "vendor_type": "custom", "env": [
        {"key": "MY_API_KEY", "value": SECRET}], "endpoints": [
        {"protocol": "openai-chat", "base_url": "https://api.example.invalid"}]}),
    ("looper_providers", {"action": "register", "name": "account", "vendor_type": "custom", "endpoints": [
        {"protocol": "openai-chat", "base_url": f"https://api.example.invalid?api_key={SECRET}"}]}),
    ("looper_providers", {"action": "register", "name": "account", "vendor_type": "custom", "endpoints": [
        {"protocol": "openai-chat", "base_url": f"https://user:{SECRET}@api.example.invalid"}]}),
    ("looper_models", {"action": "update", "model": {"name": "same"}, "context_window": 100}),
    ("looper_models", {"action": "update", "model": {"provider": "p", "name": "same", "protocol": "openai-chat",
        "extra": True}, "context_window": 100}),
    ("looper_models", {"action": "update", "model": 3, "context_window": 100}),
    ("looper_models", {"action": "update", "model": "same", "context_window": 100}),
    ("looper_models", {"action": "update", "model": "model-", "context_window": 100}),
    ("looper_providers", {"action": "register", "name": "account", "vendor_type": "custom", "endpoints": [
        {"protocol": "openai-chat", "base_url": "https://api.example.invalid", "provider_id": "draft"}]}),
    ("looper_models", {"action": "list", "env": {"API_KEY": SECRET}}),
    ("looper_models", {"action": "list", "base_url": "https://example.invalid"}),
])
def test_real_handlers_refuse_invalid_or_secret_arguments_without_database(monkeypatch, name, arguments):
    monkeypatch.setattr(manager_actions, "record", AsyncMock())
    ops = ManagerOps(Platform(bus=None, task_router=None), url="http://127.0.0.1:9")
    assert name in {tool["name"] for tool in ops.listing()}
    ops.begin_turn(Turn(channel="web", chat="test", message_id="m1"))
    answer = asyncio.run(asyncio.wait_for(ops.call(name, arguments), timeout=2))
    assert answer["status"] == "error", answer
    assert answer["error"]["code"] in {"invalid", "forbidden"}, answer
    assert SECRET not in json.dumps(answer)
    if name == "looper_providers" and "env" in arguments:
        assert "credential" in answer["error"]["message"]
    if name == "looper_providers" and any(SECRET in e.get("base_url", "") for e in arguments.get("endpoints", [])):
        assert "credentials" in answer["error"]["message"]


@needs_db
def test_accounts_register_list_and_http_readback(monkeypatch, tmp_path):
    async def test(pool):
        chat = _Chat(tmp_path)
        created = await _ok(chat, "looper_providers", action="register", name="Manager account", vendor_type="deepseek")
        provider = created["provider"]
        assert provider["vendor_type"] == "deepseek" and provider["api_key"].startswith("missing")
        assert "Settings → LLM Providers" in created["note"]
        assert (await db.get_provider(provider["provider_id"]))["api_key_set"] is False
        status, readback = await _call("GET", "/api/providers")
        assert status == 200
        saved = next(p for p in readback["providers"] if p["provider_id"] == provider["provider_id"])
        assert saved["model_count"] == 0 and saved["endpoints"] == provider["endpoints"]
        listed = await _ok(chat, "looper_providers", action="list")
        assert any(v["vendor_type"] == "deepseek" for v in listed["vendors"])
        assert any(p["provider_id"] == provider["provider_id"] for p in listed["providers"])
        explicit = await _ok(chat, "looper_providers", action="register", name="Local account", vendor_type="custom",
            endpoints=[{"protocol": "openai-chat", "base_url": "https://local.example.invalid"}],
            env=[{"key": "REGION", "value": "test"}], max_concurrency=2)
        assert (await db.get_provider(explicit["provider"]["provider_id"]))["max_concurrency"] == 2
    _run(test, monkeypatch)


@needs_db
@pytest.mark.parametrize("vendor", ["claude-subscription", "codex-login", "grok-login"])
def test_register_login_uses_builtins_without_copying(monkeypatch, tmp_path, vendor):
    async def test(pool):
        before = await pool.fetchval("SELECT count(*) FROM llm_providers")
        (answer,) = await _Chat(tmp_path).say("注册登录账户", ("looper_providers", {
            "action": "register", "name": "duplicate login", "vendor_type": vendor}))
        assert answer["status"] == "error" and answer["error"]["code"] == "invalid", answer
        assert "builtin" in answer["error"]["message"]
        assert await pool.fetchval("SELECT count(*) FROM llm_providers") == before
    _run(test, monkeypatch)


@needs_db
def test_model_crud_filters_and_exact_reference_readback(monkeypatch, tmp_path):
    async def test(pool):
        chat = _Chat(tmp_path)
        first = (await _ok(chat, "looper_models", action="create", provider="TEST ACCOUNT", name="same",
            protocol="anthropic-messages", context_window=100000, efforts=["low", "high"],
            input_usd_per_mtok=0, output_usd_per_mtok=2.5, cache_read_usd_per_mtok=0,
            cache_write_usd_per_mtok=0))["model"]
        second = (await _ok(chat, "looper_models", action="create", provider="prov-test", name="same",
            protocol="openai-chat"))["model"]
        third = (await _ok(chat, "looper_models", action="create", provider="Other account", name="same",
            protocol="anthropic-messages"))["model"]
        assert len({m["model_id"] for m in (first, second, third)}) == 3
        listed = await _ok(chat, "looper_models", action="list", provider="Test account", runtime="claude-code")
        assert [m["model_id"] for m in listed["models"]] == [first["model_id"]]
        assert len((await _ok(chat, "looper_models", action="list", provider="prov-test"))["models"]) == 2
        updated = (await _ok(chat, "looper_models", action="update", model={"provider": "Test account", "name": "same",
            "protocol": "openai-chat"}, context_window=42, efforts=[], background_model="helper"))["model"]
        status, readback = await _call("GET", f"/api/models/{updated['model_id']}")
        assert status == 200 and readback["model"]["context_window"] == 42
        cleared = (await _ok(chat, "looper_models", action="update", model=first["model_id"],
            context_window=None, efforts=None, input_usd_per_mtok=None))["model"]
        assert cleared["context_window"] is None and cleared["efforts"] is None
        assert cleared["output_usd_per_mtok"] == 2.5
        assert (await db.get_model(second["model_id"]))["background_model"] == "helper"
        (ambiguous,) = await chat.say("改同名模型", ("looper_models", {
            "action": "update", "model": "same", "context_window": 1}))
        assert ambiguous["status"] == "error" and ambiguous["error"]["code"] == "invalid"
        (asked,) = await chat.say("删除模型", ("looper_models", {"action": "delete", "model": {
            "provider": "Test account", "name": "same", "protocol": "openai-chat"}}))
        assert asked["status"] == "needs_confirmation", asked
        assert "Test account" in chat.turns[-1].blocks[0] and "openai-chat" in chat.turns[-1].blocks[0]
        assert await db.get_model(second["model_id"]) is not None
        (done,) = await chat.say("确认", ("looper_confirm", {"confirmation_id": asked["confirmation_id"]}))
        assert done == {"status": "ok", "result": {"deleted": second["model_id"]}}
        assert await db.get_model(second["model_id"]) is None
        assert await db.get_model(first["model_id"]) is not None
    _run(test, monkeypatch)


@needs_db
def test_model_errors_match_http_and_keep_saved_state(monkeypatch, tmp_path):
    async def test(pool):
        chat = _Chat(tmp_path)
        for fields in ({"protocol": "openai-responses"}, {"context_window": 0}, {"input_usd_per_mtok": -1}):
            body = {"provider": "prov-test", "name": "invalid", "protocol": "anthropic-messages", **fields}
            (answer,) = await chat.say("创建模型", ("looper_models", {"action": "create", **body}))
            status, http = await _call("POST", "/api/models", {"provider_id": body.pop("provider"), **body})
            assert answer["error"]["code"] == "invalid" and status == 400, (answer, http)
            assert answer["error"]["message"] == http["error"]
        assert await db.list_models(provider_id="prov-test") == []
        model = (await _ok(chat, "looper_models", action="create", provider="prov-test", name="valid",
            protocol="anthropic-messages"))["model"]  # positive control for the empty read above
        (duplicate,) = await chat.say("重复创建", ("looper_models", {"action": "create", "provider": "prov-test",
            "name": "valid", "protocol": "anthropic-messages"}))
        assert duplicate["error"]["code"] == "conflict"
        (unknown,) = await chat.say("更新不存在的模型", ("looper_models", {
            "action": "update", "model": "model-missing", "context_window": 1}))
        assert unknown["error"]["code"] == "not_found"
        assert (await db.get_model(model["model_id"]))["context_window"] is None
    _run(test, monkeypatch)


@needs_db
def test_references_and_builtin_models_cannot_be_deleted(monkeypatch, tmp_path):
    async def test(pool):
        chat = _Chat(tmp_path)
        model = (await _ok(chat, "looper_models", action="create", provider="prov-test", name="referenced",
            protocol="anthropic-messages"))["model"]
        await registry.create_profile({"name": "Reference", "runtime_id": "rt-claude-code", "model_id": model["model_id"]})
        for mid, code in ((model["model_id"], "conflict"),
                          ((await registry.list_models(provider_id="prov-claude-subscription"))["models"][0]["model_id"], "invalid")):
            (answer,) = await chat.say("删除模型", ("looper_models", {"action": "delete", "model": mid}))
            assert answer["status"] == "error" and answer["error"]["code"] == code, answer
            assert chat.turns[-1].blocks == [] and await db.get_model(mid) is not None
    _run(test, monkeypatch)


@needs_db
def test_writes_stay_in_user_turn_and_confirmation_rechecks_references(monkeypatch, tmp_path):
    async def test(pool):
        chat = _Chat(tmp_path)
        model = (await _ok(chat, "looper_models", action="create", provider="prov-test", name="later-used",
            protocol="anthropic-messages"))["model"]
        (asked,) = await chat.say("删除", ("looper_models", {"action": "delete", "model": model["model_id"]}))
        await registry.create_profile({"name": "New reference", "runtime_id": "rt-claude-code", "model_id": model["model_id"]})
        (refused,) = await chat.say("确认", ("looper_confirm", {"confirmation_id": asked["confirmation_id"]}))
        assert refused["error"]["code"] == "conflict" and await db.get_model(model["model_id"]) is not None
        chat.ops.begin_turn(Turn(channel="lark", chat="oc_1"))
        assert (await chat.ops.call("looper_models", {"action": "list"}))["status"] == "ok"
        assert (await chat.ops.call("looper_models", {"action": "update", "model": model["model_id"],
            "context_window": 10}))["error"]["code"] == "needs_user_turn"
        assert (await chat.ops.call("looper_providers", {"action": "register", "name": "background",
            "vendor_type": "deepseek"}))["error"]["code"] == "needs_user_turn"
    _run(test, monkeypatch)
