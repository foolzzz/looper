"""Portable Manager configuration stays an operator-only document capability."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import api_v1, config_imports, config_transfer
from looper.agents.ops import OpError
from looper.agents.ops.clients import ClientOps
from looper.agents.v1_domains.configuration import CONFIG_DOCUMENT
from tests.db_support import DSN, run_db
from tests.test_api_v1_auth import CLIENT_TOKEN, _bearer, _send, _serving, clients  # noqa: F401
from tests.test_operator_auth import TOKEN


GRAPH = {"format": "looper-config/v4", "managers": [
    {"id": "manager", "name": "Portable manager", "description": "Default metadata"},
    {"id": "manager-example", "name": "Example manager", "system_prompt": "Handle example tasks.",
     "tools": "tasks", "enabled": False, "templates": [
         {"key": "example", "name": "Example task", "requirement": "Inspect the example."}]},
]}


@pytest.mark.parametrize("version", ["looper-config/v1", "loopx-config/v1", "looper-config/v2", "looper-config/v3"])
def test_legacy_document_versions_reject_manager_fields(version):
    assert api_v1.validate_arguments(CONFIG_DOCUMENT, {"format": version}) == {"format": version}
    with pytest.raises(OpError):
        api_v1.validate_arguments(CONFIG_DOCUMENT, {"format": version, "managers": []})


@pytest.mark.parametrize("manager", [
    {"id": "manager", "name": "Default", "enabled": False},
    {"id": "manager-example", "name": "Example", "system_prompt": "   "},
    {"id": "manager-example", "name": "Example", "templates": [
        {"key": "example", "name": "Example", "work_dir": "/machine/path"}]},
    {"id": "example", "name": "Suffix is not an identity"},
])
def test_manager_document_schema_rejects_nonportable_or_default_overrides(manager):
    with pytest.raises(OpError):
        api_v1.validate_arguments(CONFIG_DOCUMENT, {"format": "looper-config/v4", "managers": [manager]})


def test_manager_full_identity_schema_keeps_manager_suffix_distinct():
    document = {"format": "looper-config/v4", "managers": [
        {"id": "manager", "name": "Default"}, {"id": "manager-manager", "name": "Scenario"}]}
    assert api_v1.validate_arguments(CONFIG_DOCUMENT, document) == document
    assert api_v1.RESULTS["config.export"]["properties"]["document"]["oneOf"] == CONFIG_DOCUMENT["oneOf"]


@pytest.mark.parametrize("route", ["import_preview", "import_upload"])
@pytest.mark.usefixtures("clients")
def test_client_cannot_stage_manager_graph(tmp_path, monkeypatch, route):
    runner = AsyncMock(side_effect=AssertionError("a refused document must not reach the importer"))
    monkeypatch.setattr(config_imports, "_run", runner)

    async def run():
        async with _serving(tmp_path) as server:
            server._client_ops = ClientOps(SimpleNamespace())
            body = {"document": GRAPH} if route == "import_preview" else GRAPH
            status, _, answer = await _send(server, f"POST /api/v1/config/{route}",
                                            _bearer(CLIENT_TOKEN), json.dumps(body))
            assert status == 403 and answer["error"]["code"] == "forbidden"
            assert not server._config_imports._pending
            assert not list((tmp_path / config_imports.DIRNAME).glob("*.json"))
            runner.assert_not_awaited()
    asyncio.run(asyncio.wait_for(run(), 10))


@pytest.mark.parametrize("execution", ["apply", "confirm"])
@pytest.mark.usefixtures("clients")
def test_client_stored_document_policy_is_rechecked_before_execution(tmp_path, monkeypatch, execution):
    runner = AsyncMock(return_value=config_transfer.Plan())
    monkeypatch.setattr(config_imports, "_run", runner)

    async def run():
        async with _serving(tmp_path) as server:
            server._client_ops = ClientOps(SimpleNamespace())
            async def request(route, data):
                return await _send(server, f"POST /api/v1/{route}",
                    {**_bearer(CLIENT_TOKEN), "X-Looper-Session": "portable"}, json.dumps(data))
            status, _, answer = await request("config/import_preview", {"document": {"format": "looper-config/v3"}})
            assert status == 200, answer
            import_id = answer["result"]["import_id"]
            pending = server._config_imports._pending[import_id]
            assert pending.policy == "client"
            if execution == "confirm":
                status, _, answer = await request("config/import_apply", {"import_id": import_id})
                assert status == 202 and answer["status"] == "needs_confirmation"
                data = {"confirmation_id": answer["confirmation_id"]}
            else:
                data = {"import_id": import_id}
            # Simulate staged-file tampering after preview or after the high-risk request.
            pending.path.write_text(json.dumps(GRAPH))
            status, _, refused = await request("confirm" if execution == "confirm" else "config/import_apply", data)
            assert status == 403 and refused["error"]["code"] == "forbidden"
            assert runner.await_count == 1  # preview only; no apply transaction
            assert pending.path.exists() and not pending.applying
            server._config_imports.clear()
    asyncio.run(asyncio.wait_for(run(), 10))


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
def test_operator_manager_preview_apply_and_export_use_real_importer(tmp_path, monkeypatch):
    async def run(pool):
        async with _serving(tmp_path) as server:
            server._client_ops = ClientOps(SimpleNamespace())
            async def request(route, data):
                return await _send(server, f"POST /api/v1/config/{route}", _bearer(TOKEN), json.dumps(data))
            status, _, staged = await request("import_preview", {"document": GRAPH})
            assert status == 200, staged
            assert await pool.fetchval("SELECT count(*) FROM managers WHERE id='manager-example'") == 0
            assert any(row["section"] == "managers" for row in staged["result"]["plan"]["changes"])
            status, _, applied = await request("import_apply", {"import_id": staged["result"]["import_id"]})
            assert status == 200 and applied["status"] == "ok" and applied["result"]["plan"]["applied"]
            assert await pool.fetchval("SELECT name FROM managers WHERE id='manager-example'") == "Example manager"
            partial = {"format": "looper-config/v4", "managers": [
                {"id": "manager-example", "name": "Example manager", "templates": [
                    {"key": "example", "name": "Renamed task"}]}]}
            before = await pool.fetchrow("SELECT * FROM manager_task_templates WHERE manager_id='manager-example'")
            status, _, staged = await request("import_preview", {"document": partial})
            assert status == 200, staged
            preview = staged["result"]["plan"]
            assert preview["changes"] == [{"section": "manager.templates", "key": "manager-example/example",
                                            "action": "changed", "fields": ["name"]}]
            assert await pool.fetchrow(
                "SELECT * FROM manager_task_templates WHERE manager_id='manager-example'") == before
            status, _, applied = await request("import_apply", {"import_id": staged["result"]["import_id"]})
            assert status == 200, applied
            assert applied["result"]["plan"] == {**preview, "applied": True}
            after = await pool.fetchrow("SELECT * FROM manager_task_templates WHERE manager_id='manager-example'")
            assert dict(after) == {**before, "name": "Renamed task", "updated_at": after["updated_at"]}
            status, _, exported = await request("export", {})
            assert status == 200, exported
            document = exported["result"]["document"]
            api_v1.validate_arguments(CONFIG_DOCUMENT, document)
            managers = {entry["id"]: entry for entry in document["managers"]}
            assert managers["manager"] == GRAPH["managers"][0]
            assert managers["manager-example"]["templates"][0]["key"] == "example"
            assert managers["manager-example"]["templates"][0]["requirement"] == "Inspect the example."
            server._config_imports.clear()
    run_db(run, monkeypatch, timeout=30)
