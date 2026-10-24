"""Authenticated channel discovery and gate selection with disposable PostgreSQL and fake lookups."""

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from looper.agents import api_v1, gate_service
from looper.agents.bus import MessageBus
from looper.agents.channels import lark_settings, lark_bot_settings, lark_cli, lark_api
from looper.agents.ops.clients import ClientOps
from looper.agents.ops.service import Platform
from looper.agents.serve import seed_channel_configs
from looper.db import api_clients, channel_configs, system_settings
from tests.db_support import DSN, run_db
from tests.test_api_v1_auth import _bearer, _send, _serving
from tests.test_operator_auth import TOKEN

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
PERSON = {"open_id": "ou_example123", "name": "Example", "email": "example@example.invalid", "department": "QA"}
TARGET = {"channel": "lark", "chat": PERSON["open_id"]}


@asynccontextmanager
async def _api(tmp_path, pool):
    _, token = await api_clients.create("discovery validation", pool=pool)
    async with _serving(tmp_path) as server:
        server._client_ops = ClientOps(Platform(bus=MessageBus(runtime_root=tmp_path),
                                               task_router=server._task_router, runtime_root=tmp_path))
        async def call(operation, body=None, *, client=False, session="discovery", method="POST"):
            path = operation if operation.startswith("/") else "/api/v1/" + operation.replace(".", "/")
            status, _, answer = await _send(server, f"{method} {path}", {
                **_bearer(token if client else TOKEN), "X-Looper-Session": session}, json.dumps(body or {}))
            if status == 200:
                schema = api_v1.find(method, path).result
                api_v1.validate_arguments({"type": "object", "properties": {"result": schema}},
                                          {"result": answer["result"]})
            return status, answer
        yield call


async def _seed(pool):
    await seed_channel_configs(pool)
    await channel_configs.set_config("channel-lark", {"enabled": True, "config": {
        "profile": "default", "allowed_open_ids": [PERSON["open_id"]], "allowlist_app_id": "cli_default"}})


@pytest.mark.parametrize("kind", ["lark", "lark_bot"])
@pytest.mark.parametrize("added", [False, True], ids=["builtin-default", "explicit-added"])
def test_status_dispatch_preserves_bot_identity_and_rejected_senders(tmp_path, monkeypatch, kind, added):
    async def run(pool):
        channel_id = f"channel-{kind}" + ("-9" if added else "")
        marker = "added" if added else "default"
        config = {"profile": marker, "app_id": "cli_" + marker,
                  "allowed_open_ids": [], "allowlist_app_id": "cli_" + marker}
        await pool.execute("INSERT INTO channel_configs (channel_id, channel_type, display_name, config) "
                           "VALUES ($1,$2,'Example',$3::jsonb) ON CONFLICT (channel_id) "
                           "DO UPDATE SET config=EXCLUDED.config", channel_id, kind, json.dumps(config))
        await channel_configs.record_rejected(channel_id, "ou_rejected", chat_id="oc_example")
        await channel_configs.record_rejected(channel_id, "ou_rejected", chat_id="oc_example")
        names = {"ou_rejected": "Example sender"}
        async def check_cli(config):
            return None, lark_cli.Identity(config["profile"], config["allowlist_app_id"], "Example bot", "ou_bot")
        async def check_bot(config, secret):
            return None, lark_api.BotInfo("Example bot", "ou_bot")
        monkeypatch.setattr(lark_cli, "check", check_cli)
        monkeypatch.setattr(lark_cli, "names", AsyncMock(return_value=names))
        monkeypatch.setattr(lark_bot_settings, "check", check_bot)
        monkeypatch.setattr(lark_api.LarkApi, "names", AsyncMock(return_value=names))
        body = {"channel_type": kind, **({"channel_id": channel_id} if added else {})}
        async with _api(tmp_path, pool) as call:
            status, answer = await call("channels.status", body, client=True)
            assert status == 200, answer
            result = answer["result"]
            assert result["bot"]["app_id"] == "cli_" + marker
            assert result["bot"]["profile"] == (marker if kind == "lark" else "")
            assert result["names"] == names and result["problem"] is None
            assert result["allowlist_app_id"] == "cli_" + marker
            rejected = result["rejected"][0]
            assert rejected["open_id"] == "ou_rejected" and rejected["count"] == 2
            assert rejected["name"] == names["ou_rejected"] and rejected["chat_id"] == "oc_example"
            assert isinstance(rejected["last_at"], str) and rejected["last_at"]
    run_db(run, monkeypatch, setup=_seed, timeout=30)


@pytest.mark.parametrize("kind", ["lark", "lark_bot"])
def test_status_keeps_unavailable_bot_and_rejected_sender_nulls(tmp_path, monkeypatch, kind):
    async def run(pool):
        settings = lark_cli if kind == "lark" else lark_bot_settings
        monkeypatch.setattr(settings, "check", AsyncMock(return_value=("Identity unavailable", None)))
        await channel_configs.record_rejected("channel-" + kind, "ou_rejected")
        async with _api(tmp_path, pool) as call:
            status, answer = await call("channels.status", {"channel_type": kind})
            assert status == 200 and answer["result"]["bot"] is None
            assert answer["result"]["problem"] == "Identity unavailable"
            assert answer["result"]["rejected"][0]["name"] is None
            assert answer["result"]["rejected"][0]["chat_id"] is None
    run_db(run, monkeypatch, setup=_seed, timeout=30)


@pytest.mark.parametrize(("kind", "complete", "fails"), [
    ("lark", True, False), ("lark_bot", False, False), ("lark_bot", True, False),
    ("lark", True, True), ("lark_bot", False, True)])
def test_people_query_completeness_and_external_failure(tmp_path, monkeypatch, kind, complete, fails):
    async def run(pool):
        await channel_configs.set_config("channel-lark_bot", {"config": {"app_id": "cli_example"}})
        await channel_configs.set_bot_token("channel-lark_bot", "example-test-secret")
        async def search(query, *args):
            assert query == "Example"
            if fails:
                raise lark_cli.LarkCliError("Directory unavailable")
            return [PERSON] if kind == "lark" else ([PERSON], complete)
        monkeypatch.setattr(lark_cli, "search_people", search)
        monkeypatch.setattr(lark_api.LarkApi, "search_people", lambda self, q: search(q))
        async with _api(tmp_path, pool) as call:
            status, answer = await call("channels.people", {"channel_type": kind, "q": " Example "}, client=True)
            if fails:
                assert status == 503 and answer["error"]["code"] == "unavailable"
                assert "result" not in answer
            else:
                assert status == 200 and answer["result"] == {"people": [PERSON], "complete": complete}
    run_db(run, monkeypatch, setup=_seed, timeout=30)


@pytest.mark.parametrize(("operation", "body", "status", "code"), [
    *[(f"channels.{action}", {"channel_type": kind, "channel_id": channel_id,
       **({"q": "Example"} if action == "people" else {})}, 404, "not_found")
      for action in ("status", "people") for kind, channel_id in
      (("lark", "missing"), ("lark", "channel-lark_bot"), ("lark_bot", "channel-lark"))],
    *[("channels.people", {"channel_type": "lark", "q": q}, 400, "invalid")
      for q in ("", "   ", "x" * (lark_settings.QUERY_MAX_CHARS + 1), None, 1)],
    ("channels.status", {"channel_type": "telegram"}, 400, "invalid"),
    ("channels.status", {"channel_type": "lark", "channel_id": None}, 400, "invalid"),
    ("channels.status", {"channel_type": "lark", "extra": True}, 400, "invalid"),
    ("channels.people", {"channel_type": "lark"}, 400, "invalid"),
    ("gate_settings.update", {}, 400, "invalid"),
    ("gate_settings.update", {"dashboard_target": {"channel": "web", "chat": "example"}}, 400, "invalid"),
])
def test_discovery_rejects_wrong_channel_and_invalid_input(tmp_path, monkeypatch, operation, body, status, code):
    async def run(pool):
        async with _api(tmp_path, pool) as call:
            actual, answer = await call(operation, body, client=True)
            assert actual == status and answer["error"]["code"] == code, answer
    run_db(run, monkeypatch, setup=_seed, timeout=30)


@pytest.mark.parametrize("selection", ["offered", "disabled", "unlisted", "clear"])
def test_operator_selects_only_enabled_offered_targets_and_can_clear(tmp_path, monkeypatch, selection):
    async def run(pool):
        if selection == "disabled":
            await channel_configs.set_config("channel-lark", {"enabled": False})
        target = None if selection == "clear" else {**TARGET, **({"chat": "ou_unlisted"} if selection == "unlisted" else {})}
        if selection == "clear":
            await gate_service.update_gate_settings({"dashboard_target": TARGET})
        async with _api(tmp_path, pool) as call:
            status, answer = await call("gate_settings.update", {"dashboard_target": target})
            accepted = selection in ("offered", "clear")
            assert status == (200 if accepted else 400), answer
            _, read = await call("gate_settings.get", client=True)
            assert read["result"]["dashboard_target"] == (target if accepted else None)
            assert read["result"]["targets"][0]["channel_enabled"] is (selection != "disabled")
            stored = await system_settings.get_settings([gate_service.DASHBOARD_TARGET_KEY])
            assert (json.loads(stored[gate_service.DASHBOARD_TARGET_KEY]) if stored.get(
                gate_service.DASHBOARD_TARGET_KEY) else None) == (target if accepted else None)
    run_db(run, monkeypatch, setup=_seed, timeout=30)


@pytest.mark.parametrize("removed", [False, True])
def test_client_target_update_waits_for_same_session_and_rechecks_current_allowlist(tmp_path, monkeypatch, removed):
    async def run(pool):
        async with _api(tmp_path, pool) as call:
            status, pending = await call("gate_settings.update", {"dashboard_target": TARGET}, client=True)
            assert status == 202 and pending["status"] == "needs_confirmation"
            assert TARGET["channel"] in pending["impact"] and TARGET["chat"] in pending["impact"]
            assert (await call("gate_settings.get"))[1]["result"]["dashboard_target"] is None
            confirm = {"confirmation_id": pending["confirmation_id"]}
            assert (await call("confirm", confirm, client=True, session="other"))[0] == 404
            if removed:
                await channel_configs.set_config("channel-lark", {"config": {"allowed_open_ids": []}})
            status, answer = await call("confirm", confirm, client=True)
            assert status == (400 if removed else 200), answer
            assert (await call("gate_settings.get"))[1]["result"]["dashboard_target"] == (None if removed else TARGET)
    run_db(run, monkeypatch, setup=_seed, timeout=30)


@pytest.mark.parametrize(("method", "legacy", "operation", "body"), [
    *[("GET", f"/api/channels/{kind}/{action}", f"channels.{action}",
       {"channel_type": kind, **({"q": "Example"} if action == "people" else {})})
      for kind in ("lark", "lark_bot") for action in ("status", "people")],
    ("GET", "/api/gate-settings", "gate_settings.get", {}),
    ("PUT", "/api/gate-settings", "gate_settings.update", {"dashboard_target": None}),
])
def test_retired_routes_404_with_authenticated_v1_positive_control(tmp_path, monkeypatch, method, legacy, operation, body):
    async def run(pool):
        monkeypatch.setattr(lark_cli, "check", AsyncMock(return_value=("Identity unavailable", None)))
        monkeypatch.setattr(lark_bot_settings, "check", AsyncMock(return_value=("Identity unavailable", None)))
        for settings in (lark_settings, lark_bot_settings):
            monkeypatch.setattr(settings, "people", AsyncMock(return_value={"people": [PERSON], "complete": True}))
        async with _api(tmp_path, pool) as call:
            assert (await call(legacy, body, method=method))[0] == 404
            status, answer = await call(operation, body)
            assert status == 200 and answer["status"] == "ok", answer
    run_db(run, monkeypatch, setup=_seed, timeout=30)
