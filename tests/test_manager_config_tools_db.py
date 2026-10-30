"""The Manager's configuration tools on a real PostgreSQL: each call changes what the dashboard's API reads back,
refuses with the dashboard's own message, asks the user first where it is ``high``, and leaves the trail.

Each call goes through ``ManagerOps`` as the endpoint hands it over, in a turn
of a Lark chat (``tests.test_ops_tasks_db._Chat``); what changed is read back
through the dashboard's HTTP API. Runs only when LOOPER_TEST_DATABASE_URL
points at a disposable database.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from looper.agents.ops import Turn
from looper.agents import profile_registry as registry
from looper.agents.runtimes import VendorType
from tests.db_support import DSN, run_db
from tests.test_catalog_db import _call as _catalog_call
from tests.test_ops_tasks_db import _Chat, _error, _project, _task
from tests.test_task_agents_db import _call, _concurrency_chat
from tests.test_usage_api_db import _run_record

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

SECRET = "sk-test-0123456789"


async def _ok(method: str, path: str, body: Any = None) -> Any:
    call = _catalog_call if path == "/api/v1/registry/view" or path.startswith(
        ("/api/v1/provider_accounts/", "/api/v1/installs/", "/api/v1/catalog_sources/", "/api/v1/ops/models/", "/api/v1/models/")) else _call
    status, answer = await call(method, path, body)
    assert status in (200, 201), (status, answer)
    return answer


async def _trail(pool: Any) -> list[tuple[str, str, str]]:
    return [(r["tool"], r["risk"], r["status"]) for r in await pool.fetch(
        "SELECT tool, risk, status FROM manager_actions ORDER BY id")]


async def _sonnet(pool: Any) -> None:
    model = next(m for m in (await registry.list_models(provider_id="prov-claude-subscription"))["models"]
                 if m["name"] == "sonnet")
    await pool.execute("INSERT INTO agent_profiles (id, name, runtime_id, model_id) VALUES "
                       "('agent-sonnet', 'claude-code/sonnet', 'rt-claude-code', $1)", model["model_id"])


@pytest.fixture(autouse=True)
def isolated_logins(monkeypatch):
    # Listing configuration never reads the operator's CLI homes.
    monkeypatch.setattr(registry, "_login_modules", lambda: {})


async def _model(name="test-model", provider="prov-claude-subscription", protocol="anthropic-messages", **settings):
    return (await _ok("POST", "/api/v1/ops/models/create", {"provider": provider, "name": name,
                                               "protocol": protocol, **settings}))["model"]


async def _tool(chat, tool, **arguments):
    (answer,) = await chat.say("配置", (tool, arguments))
    assert answer["status"] == "ok", answer
    return answer["result"]


# --- looper_projects ---


def test_projects_are_created_changed_and_read_back_as_the_dashboard_reads_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def test(pool: Any) -> None:
        chat = _Chat(tmp_path)
        (created,) = await chat.say("建一个项目 tetris，目录 /srv/tetris", ("looper_projects", {
            "action": "create", "name": "tetris", "work_dir": "/srv/tetris", "background": "A falling-blocks game."}))
        assert created["status"] == "ok", created
        (changed,) = await chat.say("把 tetris 改名叫 Tetris2，背景改成 Blocks.", ("looper_projects", {
            "action": "update", "project": "TETRIS", "name": "Tetris2", "background": "Blocks."}))
        assert changed["result"]["project"]["name"] == "Tetris2"
        (project,) = [p for p in (await _ok("POST", "/api/v1/project_admin/list", {}))["projects"] if p["name"] == "Tetris2"]
        assert (project["work_dir"], project["background"]) == ("/srv/tetris", "Blocks.")
        (listed,) = await chat.say("有哪些项目", ("looper_projects", {"action": "list"}))
        assert listed["result"]["projects"] == [{"project": "Tetris2", "work_dir": "/srv/tetris", "tasks": 0,
                                                 "active_tasks": 0, "schedules": 0, "background": "Blocks."}]
        (got,) = await chat.say("看看 Tetris2", ("looper_projects", {"action": "get", "project": "tetris2"}))
        assert got["result"]["project"]["project_id"] == project["project_id"]
        # Writes keep their result in the trail; reads do not.
        assert await _trail(pool) == [("looper_projects", "write", "ok"), ("looper_projects", "write", "ok"),
                                      ("looper_projects", "read", "ok"), ("looper_projects", "read", "ok")]
        assert await pool.fetchval("SELECT count(*) FROM manager_actions WHERE risk = 'read' AND result IS NOT NULL") \
            == 0

    run_db(test, monkeypatch)


@pytest.mark.parametrize(("arguments", "code", "message"), [
    ({"action": "create", "name": "calc"}, "conflict", "a project named 'calc' already exists"),
    ({"action": "create", "name": "x", "work_dir": "relative/dir"}, "invalid", "absolute"),
    ({"action": "create"}, "invalid", "name is required for action create"),
    ({"action": "update", "project": "nope", "name": "y"}, "not_found", "no project named 'nope'"),
    ({"action": "update", "project": "calc"}, "invalid", "give at least one of name"),
])
def test_a_project_change_is_refused_with_the_dashboards_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, arguments: dict[str, Any], code: str, message: str,
) -> None:
    async def test(pool: Any) -> None:
        await _project(pool)
        (answer,) = await _Chat(tmp_path).say("改项目", ("looper_projects", arguments))
        assert _error(answer) == code and message in answer["error"]["message"], answer
        assert await pool.fetchval("SELECT count(*) FROM projects") == 1

    run_db(test, monkeypatch)


def test_deleting_a_project_asks_first_and_runs_only_on_the_next_messages_confirmation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def test(pool: Any) -> None:
        project = await _project(pool, "scratch", "/srv/scratch")
        chat = _Chat(tmp_path)
        (asked,) = await chat.say("删掉 scratch 项目", ("looper_projects", {"action": "delete", "project": "scratch"}))
        assert asked["status"] == "needs_confirmation"
        (block,) = chat.turns[0].blocks
        assert "要删除项目「scratch」（默认目录：/srv/scratch）吗？它没有任务和定时任务。" in block
        assert await pool.fetchval("SELECT count(*) FROM projects") == 1
        assert await chat.say("算了") == []  # a message that does not confirm it: it lapses
        (again,) = await chat.say("还是删吧", ("looper_projects", {"action": "delete", "project": "scratch"}))
        (done,) = await chat.say("确认", ("looper_confirm", {"confirmation_id": again["confirmation_id"]}))
        assert done == {"status": "ok", "result": {"deleted": "scratch"}}
        assert (await _call("GET", f"/api/projects/{project['id']}/agent-settings"))[0] == 404
        assert await _trail(pool) == [("looper_projects", "high", "needs_confirmation"),
                                      ("looper_projects", "high", "needs_confirmation"),
                                      ("looper_projects", "high", "ok")]

    run_db(test, monkeypatch)


def test_a_project_with_tasks_is_confirmed_then_deleted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(pool: Any) -> None:
        await _task(pool, "task-a", "completed", await _project(pool))
        chat = _Chat(tmp_path)
        (answer,) = await chat.say("删掉 calc", ("looper_projects", {"action": "delete", "project": "calc"}))
        assert answer["status"] == "needs_confirmation", answer
        assert "删除它的全部 1 个任务" in chat.turns[0].blocks[0]
        assert await pool.fetchval("SELECT count(*) FROM projects") == 1
        (done,) = await chat.say("确认", ("looper_confirm", {"confirmation_id": answer["confirmation_id"]}))
        assert done == {"status": "ok", "result": {"deleted": "calc"}}, done
        assert await pool.fetchval("SELECT count(*) FROM tasks") == 0
        assert await pool.fetchval("SELECT count(*) FROM projects") == 0

    run_db(test, monkeypatch)


# --- looper_agent_profiles and looper_providers ---


def test_agent_profiles_are_created_changed_and_read_back(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(pool: Any) -> None:
        model = next(m for m in (await _ok("POST", "/api/v1/ops/models/list", {}))["models"]
                     if m["provider_id"] == "prov-claude-subscription" and m["name"] == "sonnet")
        chat = _Chat(tmp_path)
        created = await _tool(chat, "looper_agent_profiles", action="create", name="fast", runtime="claude-code",
                              model=model["model_id"], effort="low", agent_teams=False, custom_prompt="Be concise.")
        profile = created["profile"]
        assert (profile["runtime"], profile["model_id"], profile["model"], profile["effort"]) == (
            "claude-code", model["model_id"], {"model_id": model["model_id"], "name": "sonnet",
            "protocol": "anthropic-messages", "provider_id": "prov-claude-subscription"}, "low")
        assert profile["provider"] == {"provider_id": "prov-claude-subscription", "name": "claude-subscription",
                                        "vendor_type": "claude-subscription", "auth": "oauth"}
        assert profile["agent_teams"] is False and profile["custom_prompt"] == "Be concise."
        assert profile["used_by"] is None
        opus = next(m for m in (await _ok("POST", "/api/v1/ops/models/list", {}))["models"] if m["name"] == "opus")
        changed = await _tool(chat, "looper_agent_profiles", action="update", profile="fast", effort=None,
                              model={"provider": "CLAUDE-SUBSCRIPTION", "name": "opus", "protocol": opus["protocol"]})
        assert changed["profile"]["effort"] is None
        assert changed["profile"]["model_id"] == opus["model_id"]
        assert changed["profile"]["custom_prompt"] == "Be concise." and changed["profile"]["agent_teams"] is False
        registered = next(p for p in (await _ok("POST", "/api/v1/registry/view", {}))["profiles"] if p["name"] == "fast")
        assert (registered["profile_id"], registered["model_id"], registered["effort"]) == (
            profile["profile_id"], opus["model_id"], None)
        await _ok("PUT", "/api/agent-settings/roles/worker", {"profile_id": profile["profile_id"]})
        listed = await _tool(chat, "looper_agent_profiles", action="list")
        assert [p["name"] for p in listed["profiles"]] == ["claude-code/opus", "fast"]
        assert listed["profiles"][1]["used_by"] == "the worker role"
        assert listed["profiles"][1]["model_id"] == opus["model_id"]
        assert listed["runtimes"] == [{"runtime_id": "rt-claude-code", "name": "claude-code",
                                       "type": "claude-code", "roles": ["manager", "planner", "worker", "reviewer"]}]
        assert {m["model_id"] for m in listed["models"]} == {
            m["model_id"] for m in (await _ok("POST", "/api/v1/ops/models/list", {}))["models"]}
        assert all("prices" not in m for m in listed["models"])
        (unknown,) = await chat.say("改 slow", ("looper_agent_profiles", {
            "action": "update", "profile": "slow", "effort": "high"}))
        assert _error(unknown) == "not_found" and "claude-code/opus, fast" in unknown["error"]["message"]
        (taken,) = await chat.say("再加一个", ("looper_agent_profiles", {
            "action": "create", "name": "fast", "runtime": "rt-claude-code", "model": opus["model_id"]}))
        assert _error(taken) == "conflict" and "already exists" in taken["error"]["message"]

    run_db(test, monkeypatch)


@pytest.mark.parametrize("runtime,protocol,model_name,effort", [
    ("pi", "openai-responses", "gpt-6.1-sol", "minimal"),
    ("pi", "openai-chat", "deepseek-flash", "off"),
    ("dsh", "anthropic-messages", "deepseek-flash", "low"),
    ("dsh", "anthropic-messages", "deepseek-flash[1m]", "high"),
    ("dsh", "anthropic-messages", "deepseek-v4-pro", "max"),
    ("dsh", "anthropic-messages", "deepseek-v4-pro[1m]", "max"),
])
def test_pi_and_dsh_manager_models_profiles_and_config_import_share_pairing_rules(
    monkeypatch, tmp_path, runtime, protocol, model_name, effort,
):
    from looper.agents import config_transfer as transfer
    from tests.test_profile_registry_db import _runtime

    async def test(pool):
        runtime_id = await _runtime(runtime)
        chat = _Chat(tmp_path)
        provider = (await _ok("POST", "/api/v1/provider_accounts/create", {
            "name": "gateway", "vendor_type": "custom", "endpoints": [
                {"protocol": p, "base_url": "https://gateway.example.invalid"}
                for p in ("anthropic-messages", "openai-chat", "openai-responses")]}))["provider"]
        model = (await _tool(chat, "looper_models", action="create", provider=provider["provider_id"],
                             name=model_name, protocol=protocol, input_usd_per_mtok=2.5))["model"]
        incompatible_protocol = "anthropic-messages" if runtime == "pi" else "openai-chat"
        wrong_protocol = await _model(model_name, provider["provider_id"], incompatible_protocol)
        unsupported = await _model("unsupported-model", provider["provider_id"], protocol)
        listed = await _tool(chat, "looper_models", action="list", runtime=runtime)
        ids = {m["model_id"] for m in listed["models"]}
        assert model["model_id"] in ids and wrong_protocol["model_id"] not in ids
        assert (unsupported["model_id"] in ids) == (runtime == "pi")
        created = await _tool(chat, "looper_agent_profiles", action="create", name="native", runtime=runtime,
                              model=model["model_id"], effort=effort, custom_prompt="Keep this instruction.")
        assert created["profile"]["runtime"] == runtime and created["profile"]["effort"] == effort
        listed_profiles = await _tool(chat, "looper_agent_profiles", action="list")
        kind = next(t for t in listed_profiles["runtime_types"] if t["type"] == runtime)
        assert effort in kind["effort_levels"] and protocol in kind["protocols"]
        assert kind["supported_models"] is None if runtime == "pi" else model_name in kind["supported_models"]
        (refused,) = await chat.say("创建不兼容配方", ("looper_agent_profiles", {
            "action": "create", "name": "bad", "runtime": runtime,
            "model": wrong_protocol["model_id"]}))
        assert _error(refused) == "invalid" and "exact protocol" in refused["error"]["message"]

        data = {"format": transfer.FORMAT, "profiles": [{
            "name": "imported", "runtime": runtime, "model": {
                "provider": "gateway", "name": model_name, "protocol": protocol},
            "effort": effort, "agent_teams": False, "custom_prompt": "Keep this instruction."}]}
        async with pool.acquire() as conn:
            plan = await transfer.run_import(conn, data, apply=True)
        change = next(c for c in plan.changes if c.section == "profiles")
        assert change.action == transfer.Action.ADDED
        imported = next(p for p in (await _ok("POST", "/api/v1/registry/view", {}))["profiles"] if p["name"] == "imported")
        assert (imported["runtime_id"], imported["model_id"], imported["effort"], imported["agent_teams"],
                imported["custom_prompt"]) == (runtime_id, model["model_id"], effort, False, "Keep this instruction.")
        assert (await _ok("POST", "/api/v1/models/get", {"model": model['model_id']}))["model"]["input_usd_per_mtok"] == 2.5

    run_db(test, monkeypatch)


def test_deleting_an_agent_profile_asks_first_and_one_in_use_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def test(pool: Any) -> None:
        await _sonnet(pool)
        await _ok("PUT", "/api/agent-settings/roles/worker", {"profile_id": "agent-sonnet"})
        chat = _Chat(tmp_path)
        (used,) = await chat.say("删掉 sonnet", ("looper_agent_profiles", {"action": "delete",
                                                                         "profile": "claude-code/sonnet"}))
        assert _error(used) == "conflict" and "selected by the worker role" in used["error"]["message"]
        assert chat.turns[0].blocks == []
        await _ok("PUT", "/api/agent-settings/roles/worker", {"profile_id": None})
        (asked,) = await chat.say("删掉 sonnet", ("looper_agent_profiles", {"action": "delete",
                                                                          "profile": "agent-sonnet"}))
        assert "要删除 Agent Profile「claude-code/sonnet」（claude-code · claude-subscription · sonnet · anthropic-messages）吗？" in \
            chat.turns[1].blocks[0]
        assert await pool.fetchval("SELECT count(*) FROM agent_profiles WHERE id = 'agent-sonnet'") == 1
        (done,) = await chat.say("确认", ("looper_confirm", {"confirmation_id": asked["confirmation_id"]}))
        assert done["result"] == {"deleted": "claude-code/sonnet"}
        assert [p["name"] for p in (await _ok("POST", "/api/v1/registry/view", {}))["profiles"]] == ["claude-code/opus"]

    run_db(test, monkeypatch)


def test_a_provider_is_registered_without_a_key_and_the_user_told_where_to_enter_it(monkeypatch, tmp_path):
    async def test(pool):
        chat = _Chat(tmp_path)
        answer = await _tool(chat, "looper_providers", action="register", name="deepseek", vendor_type="deepseek",
                             env=[{"key": "API_TIMEOUT_MS", "value": "3000000"}])
        provider = answer["provider"]
        assert (provider["auth"], provider["model_count"], provider["api_key"]) == (
            "api_key", 0, "missing: the user enters it on the dashboard")
        assert "Settings → LLM Providers → deepseek → API key" in answer["note"]
        model = await _tool(chat, "looper_models", action="create", provider=provider["provider_id"],
                            name="deepseek-flash", protocol="anthropic-messages")
        stored = next(p for p in (await _ok("POST", "/api/v1/registry/view", {}))["providers"] if p["name"] == "deepseek")
        assert stored["api_key"] is None and stored["model_count"] == 1
        assert {e["protocol"]: e["base_url"] for e in stored["endpoints"]}["anthropic-messages"] == \
            "https://api.deepseek.com/anthropic"
        assert stored["env"] == [{"key": "API_TIMEOUT_MS", "value": "3000000"}]
        assert model["model"]["provider_id"] == stored["provider_id"]
        listed = await _tool(chat, "looper_providers", action="list")
        assert [p["name"] for p in listed["providers"]] == ["claude-subscription", "codex-login", "grok-login", "deepseek"]
        (refused,) = await chat.say("再登记一个", ("looper_providers", {
            "action": "register", "name": "other", "vendor_type": "deepseek",
            "env": [{"key": "OTHER_API_TOKEN", "value": "x"}]}))
        assert _error(refused) == "invalid" and "looks like a credential" in refused["error"]["message"]
    run_db(test, monkeypatch)


@pytest.mark.parametrize("vendor", [v for v in VendorType if v.auth_kind.value == "api_key"])
def test_registering_a_vendor_and_its_suggested_models_reads_back_defaults(monkeypatch, tmp_path, vendor):
    from looper.agents.provider_presets import vendor_defaults

    async def test(pool):
        defaults = vendor_defaults(vendor, provider_id="draft")
        fields = {"name": vendor.value, "vendor_type": vendor.value}
        if defaults.requires_url_confirmation or not defaults.endpoints:
            fields["endpoints"] = ([{"protocol": e.protocol.value, "base_url": "https://example.com/v1"}
                                    for e in defaults.endpoints] or
                                   [{"protocol": "openai-chat", "base_url": "https://example.com/v1"}])
        chat = _Chat(tmp_path)
        provider = (await _tool(chat, "looper_providers", action="register", **fields))["provider"]
        assert provider["model_count"] == 0
        for model in defaults.models:
            await _tool(chat, "looper_models", action="create", provider=provider["provider_id"], name=model.name,
                        protocol=model.protocol.value, context_window=model.context_window,
                        background_model=model.background_model)
        stored = next(p for p in (await _ok("POST", "/api/v1/registry/view", {}))["providers"] if p["name"] == vendor.value)
        expected = fields.get("endpoints", [{"protocol": e.protocol.value, "base_url": e.base_url}
                                            for e in defaults.endpoints])
        assert {e["protocol"]: e["base_url"] for e in stored["endpoints"]} == {e["protocol"]: e["base_url"] for e in expected}
        models = (await _ok("POST", "/api/v1/ops/models/list", {}))["models"]
        assert {(m["name"], m["protocol"], m["context_window"], m["background_model"]) for m in models
                if m["provider_id"] == provider["provider_id"]} == {
            (m.name, m.protocol.value, m.context_window, m.background_model) for m in defaults.models}
        assert stored["env"] == [{"key": k, "value": v} for k, v in defaults.env]
        assert stored["api_key"] is None
    run_db(test, monkeypatch)


def test_provider_explicit_overrides_win(monkeypatch, tmp_path):
    async def test(pool):
        chat = _Chat(tmp_path)
        provider = (await _tool(chat, "looper_providers", action="register", name="custom", vendor_type="glm-bigmodel",
            endpoints=[{"protocol": "openai-chat", "base_url": "https://example.com/v1"}],
            env=[{"key": "API_TIMEOUT_MS", "value": "1234"}]))["provider"]
        model = (await _tool(chat, "looper_models", action="create", provider="custom", name="custom-model",
                             protocol="openai-chat"))["model"]
        stored = next(p for p in (await _ok("POST", "/api/v1/registry/view", {}))["providers"] if p["name"] == "custom")
        assert stored["endpoints"] == [{"provider_id": provider["provider_id"], "protocol": "openai-chat",
                                        "base_url": "https://example.com/v1", "use_runtime_default": False}]
        assert stored["env"] == [{"key": "API_TIMEOUT_MS", "value": "1234"}]
        assert model["name"] == "custom-model" and model["provider_id"] == provider["provider_id"]
    run_db(test, monkeypatch)


@pytest.mark.parametrize(("arguments", "message"), [
    ({"vendor_type": "unknown", "name": "x"}, "vendor_type must be one of"),
    ({"name": "deepseek"}, "vendor_type is required"),
    ({"vendor_type": "codex-login", "name": "copy"}, "builtin login account"),
    ({"preset": "deepseek"}, "unknown argument 'preset'"),
])
def test_invalid_provider_registration_is_refused_without_registering(monkeypatch, tmp_path, arguments, message):
    async def test(pool):
        before = await pool.fetchval("SELECT count(*) FROM llm_providers")
        (answer,) = await _Chat(tmp_path).say("登记", ("looper_providers", {"action": "register", **arguments}))
        assert _error(answer) == "invalid" and message in answer["error"]["message"], answer
        assert await pool.fetchval("SELECT count(*) FROM llm_providers") == before
    run_db(test, monkeypatch)


def test_provider_list_includes_vendor_suggestions(monkeypatch, tmp_path):
    async def test(pool):
        answer = await _tool(_Chat(tmp_path), "looper_providers", action="list")
        assert answer["vendors"] == (await _ok("POST", "/api/v1/registry/view", {}))["vendor_types"]
        deepseek = next(v for v in answer["vendors"] if v["vendor_type"] == "deepseek")
        assert {e["protocol"] for e in deepseek["endpoints"]} == {"anthropic-messages", "openai-chat", "openai-responses"}
        assert {m["name"] for m in deepseek["suggested_models"]} == {"deepseek-flash[1m]", "deepseek-v4-pro[1m]",
                                                                    "deepseek-flash", "deepseek-v4-pro"}
        assert any(e["base_url"] == "https://api.deepseek.com" for e in deepseek["endpoints"])
    run_db(test, monkeypatch)


@pytest.mark.parametrize("reference", ["id", "name"])
def test_provider_update_changes_dashboard_and_preserves_models_and_key(monkeypatch, tmp_path, reference):
    async def test(pool):
        provider = (await _ok("POST", "/api/v1/provider_accounts/create", {"name": "deepseek", "vendor_type": "deepseek",
            "api_key": SECRET, "env": [{"key": "API_TIMEOUT_MS", "value": "3000000"}],
            "max_concurrency": 2}))["provider"]
        model = await _model("deepseek-flash", provider["provider_id"])
        endpoints = [{"protocol": e["protocol"], "base_url": "https://example.invalid/" + e["protocol"]}
                     for e in provider["endpoints"]]
        chat = _Chat(tmp_path)
        answer = await _tool(chat, "looper_providers", action="update",
                             provider=provider["provider_id"] if reference == "id" else "DEEPSEEK",
                             name="renamed", endpoints=endpoints, env=[], max_concurrency=None)
        assert answer["provider"]["name"] == "renamed"
        assert answer["provider"]["api_key"] == "set" and answer["warnings"] == []
        stored = next(p for p in (await _ok("POST", "/api/v1/registry/view", {}))["providers"]
                      if p["provider_id"] == provider["provider_id"])
        assert {e["protocol"]: e["base_url"] for e in stored["endpoints"]} == {
            e["protocol"]: e["base_url"] for e in endpoints}
        assert stored["env"] == [] and stored["max_concurrency"] is None
        assert stored["vendor_type"] == "deepseek"
        assert (await _ok("POST", "/api/v1/models/get", {"model": model['model_id']}))["model"] == {
            **model, "provider_name": "renamed", "endpoint": {
                **model["endpoint"], "base_url": "https://example.invalid/anthropic-messages"}}
        await _tool(chat, "looper_providers", action="update", provider=provider["provider_id"],
                    env=[{"key": "API_TIMEOUT_MS", "value": "1234"}])
        stored = next(p for p in (await _ok("POST", "/api/v1/registry/view", {}))["providers"]
                      if p["provider_id"] == provider["provider_id"])
        assert stored["env"] == [{"key": "API_TIMEOUT_MS", "value": "1234"}]
        assert {e["protocol"]: e["base_url"] for e in stored["endpoints"]} == {
            e["protocol"]: e["base_url"] for e in endpoints}
        assert await pool.fetchval("SELECT api_key FROM llm_provider_secrets WHERE provider_id = $1",
                                   provider["provider_id"]) == SECRET
        assert SECRET not in json.dumps(answer)
        assert await _trail(pool) == [("looper_models", "write", "ok"),
                                    *[("looper_providers", "write", "ok")] * 2]
    run_db(test, monkeypatch)


@pytest.mark.parametrize("fields,message", [
    ({"vendor_type": "cpa"}, "vendor_type is immutable"),
    ({"endpoints": [{"protocol": "openai-chat", "base_url": "file:///tmp/provider"}]}, "base_url"),
    ({"endpoints": [{"protocol": "openai-chat", "base_url": "https://example.invalid"}] * 2}, "distinct protocols"),
    ({"env": [{"key": "OTHER_API_TOKEN", "value": "x"}]}, "looks like a credential"),
    ({"max_concurrency": 0}, "max_concurrency"),
    ({}, "give at least one"),
])
def test_provider_update_uses_dashboard_validation_and_rolls_back(monkeypatch, tmp_path, fields, message):
    async def test(pool):
        provider = (await _ok("POST", "/api/v1/provider_accounts/create", {"name": "deepseek", "vendor_type": "deepseek",
            "api_key": SECRET}))["provider"]
        await _model("deepseek-flash", provider["provider_id"])
        before = await _ok("POST", "/api/v1/registry/view", {})
        status, expected = await _call("POST", "/api/v1/provider_accounts/update", {"provider_id": provider['provider_id'], "fields": fields})
        assert status == 400, expected
        (answer,) = await _Chat(tmp_path).say("修改账户", ("looper_providers", {
            "action": "update", "provider": provider["provider_id"], **fields}))
        assert _error(answer) == "invalid" and message in answer["error"]["message"], answer
        assert expected["error"] == ("invalid provider account request" if fields else "invalid credential request")
        assert SECRET not in json.dumps([answer, expected])
        assert await _ok("POST", "/api/v1/registry/view", {}) == before
        assert await pool.fetchval("SELECT api_key FROM llm_provider_secrets WHERE provider_id = $1",
                                   provider["provider_id"]) == SECRET
    run_db(test, monkeypatch)


@pytest.mark.parametrize("action", ["register", "update"])
@pytest.mark.parametrize("name", ["API_KEY", "OTHER_API_TOKEN", "CLIENT_SECRET", "PASSWORD", "CREDENTIAL"])
def test_rejected_provider_env_credentials_are_masked_in_audit(monkeypatch, tmp_path, action, name):
    async def test(pool):
        provider = (await _ok("POST", "/api/v1/provider_accounts/create", {
            "name": "deepseek", "vendor_type": "deepseek"}))["provider"]
        before = await _ok("POST", "/api/v1/registry/view", {})
        fields = {"name": "new-account", "vendor_type": "deepseek"} if action == "register" else {
            "provider": provider["provider_id"]}
        env = [{"key": "API_TIMEOUT_MS", "value": "1234"}, {"key": name, "value": SECRET}]
        (answer,) = await _Chat(tmp_path).say("配置账户", ("looper_providers", {
            "action": action, **fields, "env": env}))
        assert _error(answer) == "invalid", answer
        assert await _ok("POST", "/api/v1/registry/view", {}) == before
        (row,) = await pool.fetch("SELECT arguments, result, status, error_code FROM manager_actions")
        assert (row["status"], row["error_code"]) == ("error", "invalid")
        assert SECRET not in row["arguments"] + row["result"]
        saved_env = json.loads(row["arguments"])["env"]
        assert saved_env[0]["value"] == "1234"
        assert saved_env[1]["value"] == "***"
        assert env[1]["value"] == SECRET  # Auditing must not mutate the original request.

    run_db(test, monkeypatch)


def test_model_update_changes_dashboard_and_preserves_provider_key(monkeypatch, tmp_path):
    async def test(pool):
        provider = (await _ok("POST", "/api/v1/provider_accounts/create", {"name": "deepseek", "vendor_type": "deepseek",
            "api_key": SECRET, "env": [{"key": "API_TIMEOUT_MS", "value": "3000000"}]}))["provider"]
        model = await _model("deepseek-flash", provider["provider_id"])
        chat = _Chat(tmp_path)
        answer = await _tool(chat, "looper_models", action="update", model=model["model_id"], context_window=1234,
                             background_model="deepseek-v4-pro")
        assert answer["model"]["context_window"] == 1234 and answer["model"]["background_model"] == "deepseek-v4-pro"
        assert answer["warnings"] == []
        stored = (await _ok("POST", "/api/v1/models/get", {"model": model['model_id']}))["model"]
        assert stored == answer["model"]
        assert await pool.fetchval("SELECT api_key FROM llm_provider_secrets WHERE provider_id = $1",
                                   provider["provider_id"]) == SECRET
        assert SECRET not in json.dumps(answer)
        endpoints = [{"protocol": "anthropic-messages", "base_url": "https://api.deepseek.com/anthropic"},
                     {"protocol": "openai-chat", "base_url": "https://api.deepseek.com"},
                     {"protocol": "openai-responses", "base_url": "https://api.deepseek.com"}]
        updated = (await _ok("POST", "/api/v1/provider_accounts/update", {"provider_id": provider['provider_id'], "fields": {
            "endpoints": endpoints, "env": []}}))["provider"]
        assert {e["protocol"]: e["base_url"] for e in updated["endpoints"]} == {
            e["protocol"]: e["base_url"] for e in endpoints}
        assert updated["env"] == []
        assert await pool.fetchval("SELECT api_key FROM llm_provider_secrets WHERE provider_id = $1",
                                   provider["provider_id"]) == SECRET
        assert (await _ok("POST", "/api/v1/models/get", {"model": model['model_id']}))["model"] == stored
        cleared = await _tool(chat, "looper_models", action="update", model=model["model_id"], context_window=None)
        assert cleared["model"]["context_window"] is None and cleared["model"]["background_model"] == "deepseek-v4-pro"
    run_db(test, monkeypatch)


@pytest.mark.parametrize("changes", [{"context_window": 1}, {"background_model": None}, {"efforts": []},
                                      {"input_usd_per_mtok": 0}, {"output_usd_per_mtok": None}])
def test_model_update_preserves_other_catalog_metadata(monkeypatch, tmp_path, changes):
    async def test(pool):
        model = await _model("priced-a", context_window=200_000, background_model="background-a",
            efforts=["low", "high"], input_usd_per_mtok=1.0, output_usd_per_mtok=2.0,
            cache_read_usd_per_mtok=0.1, cache_write_usd_per_mtok=0.5)
        other = await _model("priced-b", context_window=1_000_000, background_model="background-b",
            efforts=["medium"], input_usd_per_mtok=3.0, output_usd_per_mtok=4.0,
            cache_read_usd_per_mtok=0.0, cache_write_usd_per_mtok=1.5)
        answer = await _tool(_Chat(tmp_path), "looper_models", action="update", model=model["model_id"], **changes)
        stored = (await _ok("POST", "/api/v1/models/get", {"model": model['model_id']}))["model"]
        assert stored == answer["model"]
        for field in registry.MODEL_FIELDS:
            assert stored[field] == changes.get(field, model[field])
        assert (await _ok("POST", "/api/v1/models/get", {"model": other['model_id']}))["model"] == other
        assert any("no full price" in w for w in answer["warnings"]) == (changes.get("output_usd_per_mtok", 2) is None)
    run_db(test, monkeypatch)


@pytest.mark.parametrize("action", ["create", "update"])
@pytest.mark.parametrize("name", ["deepseek-typo", "gpt-6.1-sol"])
def test_model_writes_report_prices_and_keep_exact_unknown_names(monkeypatch, tmp_path, action, name):
    async def test(pool):
        provider = (await _ok("POST", "/api/v1/provider_accounts/create", {"name": "deepseek", "vendor_type": "deepseek"}))["provider"]
        model = await _model(name, provider["provider_id"]) if action == "update" else None
        fields = {"model": model["model_id"], "context_window": 200_000} if model else {
            "provider": "deepseek", "name": name, "protocol": "anthropic-messages"}
        answer = await _tool(_Chat(tmp_path), "looper_models", action=action, **fields)
        assert any("no full price" in w and "spend caps" in w for w in answer["warnings"]) == (name == "deepseek-typo")
        stored = (await _ok("POST", "/api/v1/models/get", {"model": answer['model']['model_id']}))["model"]
        assert stored["name"] == name and stored["provider_id"] == provider["provider_id"]
        assert stored["warnings"] == answer["warnings"]
    run_db(test, monkeypatch)


@pytest.mark.parametrize("fields", [{}, {"context_window": 0}, {"input_usd_per_mtok": -1},
    {"efforts": ["bad value"]}, {"protocol": "openai-chat"}, {"name": "renamed"}])
def test_model_update_refuses_with_dashboard_validation_and_rolls_back(monkeypatch, tmp_path, fields):
    async def test(pool):
        model = await _model("editable-sonnet")
        await _ok("POST", "/api/agent-profiles", {"name": "fast", "runtime_id": "rt-claude-code",
                                                "model_id": model["model_id"]})
        # The baseline for the rollback check is the model as the profile now uses it (profile_count 1).
        model = (await _ok("POST", "/api/v1/models/get", {"model": model['model_id']}))["model"]
        status, dashboard = await _catalog_call("POST", "/api/v1/ops/models/update", {"model": model['model_id'], **fields})
        assert status == 400, dashboard
        (answer,) = await _Chat(tmp_path).say("更新", ("looper_models", {
            "action": "update", "model": model["model_id"], **fields}))
        assert _error(answer) == "invalid", answer
        assert answer["error"]["message"] == dashboard["error"]
        assert (await _ok("POST", "/api/v1/models/get", {"model": model['model_id']}))["model"] == model
    run_db(test, monkeypatch)


@pytest.mark.parametrize("background_priced", [True, False])
def test_profile_price_warnings_use_the_catalog_a_launch_prices_with(monkeypatch, tmp_path, background_priced):
    async def test(pool):
        provider = (await _ok("POST", "/api/v1/provider_accounts/create", {"name": "priced", "vendor_type": "deepseek"}))["provider"]
        prices = dict.fromkeys(registry.PRICE_FIELDS, 1.0)
        if background_priced:
            await _model("bg-priced", provider["provider_id"], **prices)
        main = await _model("main-priced", provider["provider_id"], background_model="bg-priced", **prices)
        created = await _ok("POST", "/api/agent-profiles", {"name": "priced", "runtime_id": "rt-claude-code",
                                                           "model_id": main["model_id"]})
        unpriced = [] if background_priced else ["bg-priced"]
        assert created["profile"]["cost"]["unpriced"] == unpriced
        assert sum("no full price" in w for w in created["warnings"]) == len(unpriced)
        view = next(p for p in (await registry.registry_view())["profiles"] if p["name"] == "priced")
        assert view["cost"]["unpriced"] == unpriced
    run_db(test, monkeypatch)


@pytest.mark.parametrize("changes", [{"model": "sonnet"}, {"model": {"provider": "claude-subscription", "name": "sonnet"}},
    {"mode": "auto"}, {"system_prompt_mode": "append"}, {"effort": "unsupported"}, {"runtime": "omp", "agent_teams": True}])
def test_invalid_profile_changes_preserve_saved_reference(monkeypatch, tmp_path, changes):
    async def test(pool):
        await _sonnet(pool)
        await pool.execute("INSERT INTO agent_runtimes (id, name, type, capabilities) VALUES ('rt-omp', 'omp', 'omp', '{}')")
        fields = dict(changes)
        if fields.get("agent_teams"):
            account = (await _ok("POST", "/api/v1/provider_accounts/create", {"name": "api", "vendor_type": "custom",
                "endpoints": [{"protocol": "anthropic-messages", "base_url": "https://example.com"}]}))["provider"]
            fields["model"] = (await _model("api-model", account["provider_id"]))["model_id"]
        before = await pool.fetchrow("SELECT * FROM agent_profiles WHERE id = 'agent-sonnet'")
        (answer,) = await _Chat(tmp_path).say("更新配方", ("looper_agent_profiles", {
            "action": "update", "profile": "agent-sonnet", **fields}))
        assert _error(answer) == "invalid", answer
        if fields.get("agent_teams"):
            assert "agent teams are a Claude Code feature" in answer["error"]["message"]
        if "effort" in fields:
            assert "effort" in answer["error"]["message"]
        assert await pool.fetchrow("SELECT * FROM agent_profiles WHERE id = 'agent-sonnet'") == before
    run_db(test, monkeypatch)


def test_profile_login_cannot_be_used_by_another_runtime(monkeypatch, tmp_path):
    async def test(pool):
        model = await _model("login-model", "prov-grok-login", "openai-responses")
        await pool.execute("INSERT INTO agent_runtimes (id, name, type, capabilities) VALUES ('rt-codex', 'codex', 'codex', '{}')")
        (answer,) = await _Chat(tmp_path).say("创建配方", ("looper_agent_profiles", {
            "action": "create", "name": "wrong-login", "runtime": "codex", "model": model["model_id"]}))
        assert _error(answer) == "invalid" and "own runtime" in answer["error"]["message"]
        assert await pool.fetchval("SELECT count(*) FROM agent_profiles WHERE name = 'wrong-login'") == 0
        valid = await _tool(_Chat(tmp_path), "looper_agent_profiles", action="create", name="valid", runtime="claude-code",
                            model=(await _model())["model_id"])
        assert valid["profile"]["model_id"] and valid["profile"]["provider"]["auth"] == "oauth"
    run_db(test, monkeypatch)


def test_no_tool_takes_a_key_and_none_is_kept_or_answered(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Which names count as a secret's is ``check_arguments``' (test_manager_ops); here none reaches the tables.

    async def test(pool: Any) -> None:
        chat = _Chat(tmp_path)
        (answer,) = await chat.say(f"登记 deepseek，key 是 {SECRET}", ("looper_providers", {
            "action": "register", "name": "deepseek", "vendor_type": "deepseek", "api_key": SECRET}))
        assert _error(answer) == "forbidden"
        assert "Settings → LLM Providers" in answer["error"]["message"] and SECRET not in json.dumps(answer)
        assert await pool.fetchval("SELECT count(*) FROM llm_providers WHERE name = 'deepseek'") == 0
        assert await pool.fetchval("SELECT count(*) FROM llm_provider_secrets") == 0
        (row,) = await pool.fetch("SELECT arguments, result FROM manager_actions")
        assert SECRET not in row["arguments"] + row["result"]
        assert json.loads(row["arguments"])["api_key"] == "***"

    run_db(test, monkeypatch)


# --- looper_settings ---


async def _project_settings(project_id: str) -> dict[str, Any]:
    return await _ok("GET", f"/api/projects/{project_id}/agent-settings")


def test_a_role_in_a_projects_profile_is_set_at_once_and_reads_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def test(pool: Any) -> None:
        project = await _project(pool)
        await _sonnet(pool)
        chat = _Chat(tmp_path)
        (answer,) = await chat.say("calc 里的 Worker 用 sonnet", ("looper_settings", {
            "action": "set", "project": "calc", "role": "worker", "values": {"profile": "claude-code/sonnet"}}))
        assert answer["status"] == "ok", answer
        assert chat.turns[0].blocks == []
        assert answer["result"]["changes"] == [{"setting": "profile_id", "before": "未设置（继承上一层）",
                                                "after": "claude-code/sonnet"}]
        assert answer["result"]["effective"]["worker"]["profile"] == {"value": "claude-code/sonnet",
                                                                      "source": "role_project"}
        worker = (await _project_settings(project["id"]))["roles"]["worker"]
        assert worker["values"]["profile_id"] == "agent-sonnet"
        assert worker["effective"]["profile"]["value"]["name"] == "claude-code/sonnet"
        # The view says the same as the dashboard.
        (view,) = await chat.say("看看 calc 的设置", ("looper_settings", {"action": "view", "project": "calc"}))
        assert view["result"]["roles"]["worker"]["set"] == {"profile_id": "claude-code/sonnet"}
        assert view["result"]["roles"]["planner"]["effective"]["profile"] == {"value": "claude-code/opus",
                                                                              "source": "builtin"}
        assert await _trail(pool) == [("looper_settings", "write", "ok"), ("looper_settings", "read", "ok")]

    run_db(test, monkeypatch)


def test_the_shared_defaults_change_only_after_the_user_confirms(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def test(pool: Any) -> None:
        await _sonnet(pool)
        chat = _Chat(tmp_path)
        change = {"action": "set", "values": {"profile": "claude-code/sonnet", "env": {"LOG_LEVEL": "debug"}}}
        (asked,) = await chat.say("所有角色默认用 sonnet", ("looper_settings", change))
        assert asked["status"] == "needs_confirmation"
        assert chat.turns[0].blocks[0].splitlines()[1:4] == [
            "要修改共用默认（Settings → Roles → Shared defaults：所有角色都继承，下层设置的除外）吗？",
            "- Agent Profile：未设置（继承上一层） → claude-code/sonnet",
            "- 环境变量：未设置（继承上一层） → LOG_LEVEL=debug",
        ]
        assert (await _ok("GET", "/api/agent-settings"))["system"]["values"]["profile_id"] is None
        (done,) = await chat.say("确认", ("looper_confirm", {"confirmation_id": asked["confirmation_id"]}))
        assert done["status"] == "ok", done
        system = (await _ok("GET", "/api/agent-settings"))["system"]["values"]
        assert (system["profile_id"], system["env"]) == ("agent-sonnet", [{"key": "LOG_LEVEL", "value": "debug"}])
        assert set(done["result"]["effective"]) == {"manager", "planner", "worker", "reviewer"}
        assert await _trail(pool) == [("looper_settings", "high", "needs_confirmation"),
                                      ("looper_settings", "high", "ok")]

    run_db(test, monkeypatch)


def test_a_roles_default_limit_changes_only_after_the_user_confirms_and_a_projects_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def test(pool: Any) -> None:
        project = await _project(pool)
        chat = _concurrency_chat(tmp_path)
        # A project has no limits: refused with the reason, nothing asked or stored.
        (refused,) = await chat.say("calc 的 Worker 并发改成 3", ("looper_settings", {
            "action": "set", "project": "calc", "role": "worker", "values": {"max_concurrency": 3}}))
        assert refused["status"] == "error" and "concurrency is per task" in refused["error"]["message"]
        assert chat.turns[0].blocks == [] and chat.workers.rechecks == 0
        assert "max_concurrency" not in (await _project_settings(project["id"]))["roles"]["worker"]["effective"]

        (asked,) = await chat.say("Worker 的默认并发改成 3", ("looper_settings", {
            "action": "set", "role": "worker", "values": {"max_concurrency": 3}}))
        assert asked["status"] == "needs_confirmation"
        lines = chat.turns[1].blocks[0].splitlines()
        assert lines[2] == "- 并发上限（每个任务同时运行的 Worker 数）：16（内置默认） → 3（角色默认）"
        assert "调低不会停止正在运行的工作" in lines[3]
        assert chat.workers.rechecks == 0
        (done,) = await chat.say("确认", ("looper_confirm", {"confirmation_id": asked["confirmation_id"]}))
        assert done["status"] == "ok", done
        assert (chat.router.rechecks, chat.workers.rechecks) == (0, 1)
        assert done["result"]["effective"]["worker"]["max_concurrency"] == {"value": 3, "source": "role"}
        # The Planner has none.
        (planner,) = await chat.say("Planner 并发 2", ("looper_settings", {
            "action": "set", "role": "planner", "values": {"max_concurrency": 2}}))
        assert planner["status"] == "error" and "concurrency is per task" in planner["error"]["message"]

    run_db(test, monkeypatch)


@pytest.mark.parametrize(("start", "change", "asks"), [
    (None, 5.0, False),  # no cap at all before: a cap only lowers it
    (5.0, 2.0, False),
    (2.0, 5.0, True),
    (2.0, None, True),  # back to no cap
])
def test_raising_a_spend_cap_asks_first_and_lowering_it_does_not(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, start: float | None, change: float | None, asks: bool,
) -> None:
    async def test(pool: Any) -> None:
        if start is not None:
            await _ok("PUT", "/api/agent-settings/roles/worker", {"run_budget_usd": start})
        chat = _Chat(tmp_path)
        (answer,) = await chat.say("改 Worker 的单次花费上限", ("looper_settings", {
            "action": "set", "role": "worker", "values": {"run_budget_usd": change}}))
        worker = (await _ok("GET", "/api/agent-settings"))["roles"]["worker"]["values"]
        if asks:
            assert answer["status"] == "needs_confirmation"
            assert "这会提高（或取消）运行的花费上限。" in chat.turns[0].blocks[0]
            assert worker["run_budget_usd"] == start
        else:
            assert answer["status"] == "ok", answer
            assert worker["run_budget_usd"] == change

    run_db(test, monkeypatch)


def test_a_cap_set_lower_down_counts_against_what_runs_with_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A project's own cap above the role's raises what its runs get, though the project had none of its own."""

    async def test(pool: Any) -> None:
        await _project(pool)
        await _ok("PUT", "/api/agent-settings/roles/worker", {"run_budget_usd": 1.0})
        chat = _Chat(tmp_path)
        (answer,) = await chat.say("calc 的单次上限 3", ("looper_settings", {
            "action": "set", "project": "calc", "values": {"run_budget_usd": 3.0}}))
        assert answer["status"] == "needs_confirmation"

    run_db(test, monkeypatch)


def test_a_roles_timeout_is_set_as_settings_roles_saves_it(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from looper.agents.serve import seed_role_configs

    async def test(pool: Any) -> None:
        await seed_role_configs()
        await _project(pool)
        chat = _Chat(tmp_path)
        (answer,) = await chat.say("Worker 超时改成一小时", ("looper_settings", {
            "action": "set", "role": "worker", "values": {"timeout_seconds": 3600}}))
        assert answer["status"] == "ok", answer
        assert answer["result"]["changes"] == [{"setting": "timeout_seconds", "before": 86400, "after": 3600}]
        assert (await _ok("GET", "/api/roles/worker"))["timeout_seconds"] == 3600
        (refused,) = await chat.say("calc 的 Worker 超时一小时", ("looper_settings", {
            "action": "set", "project": "calc", "role": "worker", "values": {"timeout_seconds": 3600}}))
        assert _error(refused) == "invalid" and "a role's own" in refused["error"]["message"]

    run_db(test, monkeypatch)


@pytest.mark.parametrize(("arguments", "message"), [
    ({"role": "worker", "values": {"effort": "high"}}, "values has an unknown key 'effort'"),
    ({"role": "worker", "values": {"env": {"MY_TOKEN": "x"}}}, "MY_TOKEN looks like a credential"),
    ({"values": {"max_concurrency": 2}}, "concurrency is per task: max_concurrency is how many of one task's"),
    ({"role": "manager", "values": {"max_concurrency": 2}}, "max_concurrency is how many"),
    ({"role": "manager", "project": "calc", "values": {"harness_prompt": "x"}}, "the manager spans every project"),
    ({"role": "worker", "values": {"profile": "nope"}}, "no Agent Profile 'nope'; there are: claude-code/opus"),
    ({"role": "worker", "values": {}}, "values: give at least one setting"),
    ({"role": "worker", "values": {"run_budget_usd": -1}}, "run_budget_usd must be more than 0"),
])
def test_a_settings_change_is_refused_with_the_dashboards_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, arguments: dict[str, Any], message: str,
) -> None:
    async def test(pool: Any) -> None:
        await _project(pool)
        chat = _Chat(tmp_path)
        (answer,) = await chat.say("改设置", ("looper_settings", {"action": "set", **arguments}))
        assert _error(answer) in ("invalid", "not_found") and message in answer["error"]["message"], answer
        assert chat.turns[0].blocks == []
        assert await pool.fetchval("SELECT count(*) FROM agent_setting_layers") == 0

    run_db(test, monkeypatch)


def test_a_projects_allowed_profiles_change_as_its_settings_tab_sets_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def test(pool: Any) -> None:
        project = await _project(pool)
        await _sonnet(pool)
        chat = _Chat(tmp_path)

        async def allowed() -> list[str]:
            return [p["name"] for p in (await _ok("GET", f"/api/projects/{project['id']}/allowed-profiles"))[
                "profiles"]]

        (answer,) = await chat.say("calc 的规划者可以选 sonnet", ("looper_settings", {
            "action": "allowed_profiles", "project": "calc", "add": ["claude-code/sonnet"]}))
        assert answer["status"] == "ok", answer
        api = await _ok("GET", f"/api/projects/{project['id']}/allowed-profiles")
        assert answer["result"]["allowed_profiles"] == api["profiles"]
        assert await allowed() == ["claude-code/sonnet"] and answer["result"]["before"] == []
        # Adding one keeps what is there; removing one keeps the rest.
        (more,) = await chat.say("也可以选 opus", ("looper_settings", {
            "action": "allowed_profiles", "project": "calc", "add": ["agent-claude-code-opus"]}))
        assert more["result"]["before"] == ["claude-code/sonnet"]
        assert sorted(await allowed()) == ["claude-code/opus", "claude-code/sonnet"]
        (fewer,) = await chat.say("不要 sonnet 了", ("looper_settings", {
            "action": "allowed_profiles", "project": "calc", "remove": ["CLAUDE-CODE/SONNET"]}))
        assert fewer["status"] == "ok" and await allowed() == ["claude-code/opus"]
        (view,) = await chat.say("看看 calc", ("looper_settings", {"action": "view", "project": "calc"}))
        assert view["result"]["allowed_profiles"] == ["claude-code/opus"]
        # Refused as the tab refuses, or as not on the list; nothing changes either way.
        (unknown,) = await chat.say("再加一个", ("looper_settings", {
            "action": "allowed_profiles", "project": "calc", "add": ["claude-code/sonnet", "nope"]}))
        assert _error(unknown) == "invalid" and "no Agent Profile 'nope'" in unknown["error"]["message"]
        (absent,) = await chat.say("去掉 sonnet", ("looper_settings", {
            "action": "allowed_profiles", "project": "calc", "remove": ["claude-code/sonnet"], "add": ["fast"]}))
        assert _error(absent) == "not_found" and "is not on the list" in absent["error"]["message"]
        (empty,) = await chat.say("改列表", ("looper_settings", {"action": "allowed_profiles", "project": "calc"}))
        assert _error(empty) == "invalid"
        assert await allowed() == ["claude-code/opus"]

    run_db(test, monkeypatch)


def test_a_turn_the_manager_runs_on_its_own_reads_settings_but_changes_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from looper.agents.serve import seed_role_configs

    async def test(pool: Any) -> None:
        await seed_role_configs()
        await _project(pool)
        chat = _Chat(tmp_path)
        chat.ops.begin_turn(own := Turn(channel=None, chat=None))
        try:
            view = await chat.ops.call("looper_settings", {"action": "view"})
            listed = await chat.ops.call("looper_projects", {"action": "list"})
            changed = await chat.ops.call("looper_settings", {"action": "set", "role": "worker",
                                                              "values": {"timeout_seconds": 60}})
            created = await chat.ops.call("looper_projects", {"action": "create", "name": "x"})
        finally:
            chat.ops.end_turn(own)
        assert (view["status"], listed["status"]) == ("ok", "ok")
        assert _error(changed) == _error(created) == "needs_user_turn"
        assert (await _ok("GET", "/api/roles/worker"))["timeout_seconds"] == 86400
        assert await pool.fetchval("SELECT count(*) FROM projects") == 1

    run_db(test, monkeypatch)


# --- looper_catalog ---


def test_a_catalog_item_is_installed_changed_and_uninstalled_as_the_dashboard_does(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def test(pool: Any) -> None:
        project = await _project(pool)
        server = (await _ok("POST", "/api/v1/catalog_sources/create_mcp", {"name": "search", "transport": "stdio", "command": "python3",
                                                          "env": {"SEARCH_KEY": SECRET}}))["item"]
        chat = _Chat(tmp_path)
        (listed,) = await chat.say("有哪些 MCP", ("looper_catalog", {"action": "list"}))
        assert listed["result"]["items"] == [{"item_id": server["item_id"], "kind": "mcp", "name": "search",
                                              "description": None, "project": None, "manager_install_count": 0,
                                              "installs": []}]
        assert SECRET not in json.dumps(listed)
        (installed,) = await chat.say("calc 的 Worker 装上 search", ("looper_catalog", {
            "action": "install", "item": "search", "project": "calc", "roles": ["worker"]}))
        assert installed["status"] == "ok", installed
        install_id = installed["result"]["install"]["install_id"]
        (entry,) = (await _ok("POST", "/api/v1/installs/project", {"project_id": project['id']}))["installs"]
        assert (entry["install_id"], entry["roles"], entry["inherited"]) == (install_id, ["worker"], False)
        (again,) = await chat.say("再装一次", ("looper_catalog", {"action": "install", "item": "search",
                                                                "project": "calc"}))
        assert _error(again) == "conflict" and "installed there already" in again["error"]["message"]
        (manager,) = await chat.say("calc 里给管家装", ("looper_catalog", {
            "action": "install", "item": "search", "project": "calc", "roles": ["manager"]}))
        assert _error(manager) == "invalid" and "cannot include the manager" in manager["error"]["message"]
        (changed,) = await chat.say("Reviewer 也装上", ("looper_catalog", {
            "action": "update", "install_id": install_id, "roles": ["worker", "reviewer"]}))
        assert changed["result"]["install"]["roles"] == ["worker", "reviewer"]
        (here,) = await chat.say("calc 里有什么", ("looper_catalog", {"action": "list", "project": "calc"}))
        assert [(i["item"]["name"], i["roles"]) for i in here["result"]["installs"]] == [
            ("search", ["worker", "reviewer"])]
        (gone,) = await chat.say("卸掉", ("looper_catalog", {"action": "uninstall", "install_id": install_id}))
        assert gone["status"] == "ok"
        assert (await _ok("POST", "/api/v1/installs/project", {"project_id": project['id']}))["installs"] == []

    run_db(test, monkeypatch)


def test_a_skill_installed_only_on_one_manager_lists_its_manager_install_count(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def test(pool: Any) -> None:
        from looper.db import manager_installs

        used = (await _ok("POST", "/api/v1/catalog_sources/create_skill", {
            "name": "manager-only", "description": "Only a Manager.", "content": "Reply carefully."}))["item"]
        unused = (await _ok("POST", "/api/v1/catalog_sources/create_skill", {
            "name": "unused-skill", "description": "Installed nowhere.", "content": "Stay quiet."}))["item"]
        await manager_installs.put_install("manager", used["item_id"])
        assert await pool.fetchval("SELECT count(*) FROM library_installs WHERE item_id = ANY($1::text[])",
                                   [used["item_id"], unused["item_id"]]) == 0
        assert await pool.fetchval("SELECT count(*) FROM manager_library_installs WHERE item_id = $1",
                                   used["item_id"]) == 1
        (listed,) = await _Chat(tmp_path).say("有哪些技能", ("looper_catalog", {"action": "list"}))
        assert listed["status"] == "ok", listed
        by_id = {item["item_id"]: item for item in listed["result"]["items"]}
        assert by_id[used["item_id"]]["name"] == "personal:manager-only"
        assert by_id[used["item_id"]]["installs"] == []
        assert by_id[used["item_id"]]["manager_install_count"] == 1
        assert by_id[unused["item_id"]]["installs"] == []
        assert by_id[unused["item_id"]]["manager_install_count"] == 0

    run_db(test, monkeypatch)


# --- looper_costs ---


async def _at(pool: Any, record_id: int, when: datetime) -> None:
    await pool.execute("UPDATE llm_records SET created_at = $2 WHERE id = $1", record_id, when)


def test_a_tasks_cost_is_the_task_pages(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from looper.db import llm_records

    async def test(pool: Any) -> None:
        await _task(pool, "task-a", "completed", await _project(pool))
        for agent_id, cost in (("planner-task-a", 0.12), ("worker-1", 0.4), ("reviewer-2", 0.05)):
            await _run_record(pool, agent_id, task_id="task-a", cost=cost)
        before = (await _ok("POST", "/api/v1/tasks/detail", {"task_id": "task-a"}))["cost"]["cost_usd"]
        record_id = await _run_record(pool, "manager", cost=7.0)
        await llm_records.link_task(record_id, "task-a", pool=pool)
        (answer,) = await _Chat(tmp_path).say("task-a 花了多少", ("looper_costs", {"task_id": "task-a"}))
        page = await _ok("POST", "/api/v1/tasks/detail", {"task_id": "task-a"})
        assert page["cost"]["cost_usd"] == before
        assert answer["result"]["cost"] == page["cost"]
        assert answer["result"]["cost"]["by_model"] == page["cost"]["by_model"]
        assert "manager_by_model" in answer["result"]["note"]
        assert answer["result"]["cost"]["cost_usd"] == pytest.approx(0.57)
        (mixed,) = await _Chat(tmp_path).say("task-a 今天花了多少", ("looper_costs", {"task_id": "task-a",
                                                                                     "period": "today"}))
        assert _error(mixed) == "invalid"

    run_db(test, monkeypatch)


def test_spend_by_project_and_time_range_counts_the_runs_started_in_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from looper.agents.overview import local_midnight
    from looper.db import llm_records

    async def test(pool: Any) -> None:
        calc, other = await _project(pool), await _project(pool, "other", "/o")
        await _task(pool, "task-a", "completed", calc)
        await _task(pool, "task-b", "completed", calc)
        await _task(pool, "task-c", "completed", other)
        now = datetime.now(timezone.utc)
        await _run_record(pool, "worker-1", task_id="task-a", cost=0.4)
        await _run_record(pool, "planner-task-b", task_id="task-b", cost=0.1)
        await _run_record(pool, "worker-2", task_id="task-c", cost=0.2)
        await _run_record(pool, "manager", cost=0.03)
        await _run_record(pool, "manager", task_id="task-a", cost=7.0)  # the Manager's: no task's, no project's
        await _at(pool, await _run_record(pool, "worker-3", task_id="task-a", cost=5.0), now - timedelta(days=40))
        chat = _Chat(tmp_path)

        (calc_now,) = await chat.say("calc 最近花了多少", ("looper_costs", {
            "project": "calc", "since": (now - timedelta(days=7)).date().isoformat()}))
        result = calc_now["result"]
        assert result["total"]["provider_priced_usd"] + result["total"]["equivalent_usd"] == pytest.approx(0.5)
        assert [(t["task_id"], t["runs"]) for t in result["tasks"]] == [("task-a", 1), ("task-b", 1)]
        # All time, the run of 40 days ago counts too; a project's total is the sum of its tasks' costs.
        (calc_all,) = await chat.say("calc 一共花了多少", ("looper_costs", {"project": "calc"}))
        total = calc_all["result"]["total"]
        task_costs = [(await _ok("POST", "/api/v1/tasks/detail", {"task_id": t}))["cost"]["cost_usd"] for t in ("task-a", "task-b")]
        assert total["provider_priced_usd"] + total["equivalent_usd"] == pytest.approx(sum(task_costs)) == \
            pytest.approx(5.5)

        (today,) = await chat.say("今天花了多少", ("looper_costs", {"period": "today"}))
        spend = await llm_records.spend_since(local_midnight())  # the overview's today
        assert {k: today["result"]["total"][k] for k in ("runs", "provider_priced_usd", "equivalent_usd")} == \
            {k: spend[k] for k in ("runs", "provider_priced_usd", "equivalent_usd")}
        assert [(p["project"], p["tasks"], pytest.approx(p["provider_priced_usd"] + p["equivalent_usd"]))
                for p in today["result"]["projects"]] == [("calc", 2, 0.5), ("other", 1, 0.2)]
        # A day as until counts that whole day.
        (to_today,) = await chat.say("calc 到今天为止", ("looper_costs", {
            "project": "calc", "until": datetime.now().astimezone().date().isoformat()}))
        assert to_today["result"]["total"]["runs"] == 3
        (window,) = await chat.say("40 天前那天", ("looper_costs", {
            "since": (now - timedelta(days=41)).isoformat(), "until": (now - timedelta(days=39)).isoformat()}))
        assert window["result"]["total"]["runs"] == 1
        (bad,) = await chat.say("上周", ("looper_costs", {"since": "last week"}))
        assert _error(bad) == "invalid" and "YYYY-MM-DD" in bad["error"]["message"]

    run_db(test, monkeypatch)
