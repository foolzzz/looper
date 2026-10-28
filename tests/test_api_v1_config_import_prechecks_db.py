"""Import prechecks reject retired fields and distinguish unchanged default Managers."""

import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import config_imports, config_transfer, scheduler_settings
from looper.agents.ops.clients import ClientOps
from tests.db_support import DSN, run_db
from tests.test_api_v1_auth import CLIENT_TOKEN, _bearer, _send, _serving, clients  # noqa: F401
from tests.test_operator_auth import TOKEN

pytestmark = [pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set"),
              pytest.mark.usefixtures("clients")]


@pytest.fixture(autouse=True)
def isolated_scheduler(monkeypatch):
    for name in ("_min_interval", "_min_source"):
        monkeypatch.setattr(scheduler_settings, name, getattr(scheduler_settings, name))


async def request(server, token, action, data):
    status, _, answer = await _send(server, f"POST /api/v1/{action}", _bearer(token), json.dumps(data))
    return status, answer


@pytest.mark.parametrize("section,field,value", [
    ("settings.layers", "model", "opus"),
    ("settings.layers", "effort", None),
    ("providers", "protocols", ["anthropic-messages"]),
    ("providers", "base_url", "https://example.invalid"),
    ("providers", "models", []),
    ("projects[0].layers", "model", None),
    ("projects[0].layers", "effort", "high"),
])
def test_retired_fields_refuse_staging_and_execution_before_target_access(
        tmp_path, monkeypatch, section, field, value):
    async def run(pool):
        await pool.execute("INSERT INTO system_settings (key, value) VALUES ($1, $2)",
                           scheduler_settings.MIN_INTERVAL_KEY, "300")
        baseline = {"format": config_transfer.FORMAT,
                    "settings": {"scheduler": {"min_interval_seconds": 73}}}
        document = deepcopy(baseline)
        if section == "settings.layers":
            document["settings"]["layers"] = [{"scope": "system"}, {"scope": "role", "role": "worker", field: value}]
            path = f"{section}[1].{field}"
        elif section == "providers":
            document[section] = [{"name": "first"}, {"name": "second"}, {"name": "third", field: value}]
            path = f"{section}[2].{field}"
        else:
            document["projects"] = [{"name": "Example", "layers": [{"scope": "project", field: value}]}]
            path = f"{section}[0].{field}"
        runner = AsyncMock(wraps=config_imports._run)
        monkeypatch.setattr(config_imports, "_run", runner)
        async with _serving(tmp_path, listen=True) as server:
            server._client_ops = ClientOps(SimpleNamespace())
            for route in ("import_upload", "import_preview"):
                body = {"document": document} if route == "import_preview" else document
                status, refused = await request(server, TOKEN, f"config/{route}", body)
                assert status == 400, refused
                assert path in refused["error"]["message"]
                runner.assert_not_awaited()
                assert not server._config_imports._pending
            for token, confirm in ((TOKEN, False), (CLIENT_TOKEN, False), (CLIENT_TOKEN, True)):
                status, staged = await request(server, token, "config/import_preview", {"document": baseline})
                assert status == 200, staged
                import_id = staged["result"]["import_id"]
                pending_path = server._config_imports._pending[import_id].path
                if confirm:
                    status, confirmation = await request(server, token, "config/import_apply", {"import_id": import_id})
                    assert status == 202, confirmation
                pending_path.write_text(json.dumps(document))
                runner.reset_mock()
                route, body = (("confirm", {"confirmation_id": confirmation["confirmation_id"]}) if confirm else
                               ("config/import_apply", {"import_id": import_id}))
                status, refused = await request(server, token, route, body)
                assert status == 400, refused
                assert path in refused["error"]["message"]
                runner.assert_not_awaited()
                assert not pending_path.exists()
                assert await pool.fetchval("SELECT value FROM system_settings WHERE key=$1",
                                           scheduler_settings.MIN_INTERVAL_KEY) == "300"
            server._config_imports.clear()
    run_db(run, monkeypatch, timeout=40)


@pytest.mark.parametrize("token", [CLIENT_TOKEN, TOKEN], ids=["client", "operator"])
@pytest.mark.parametrize("include_default", [False, True], ids=["exported", "matching-default"])
def test_default_manager_export_preview_apply_round_trip(tmp_path, monkeypatch, token, include_default):
    async def run(pool):
        await pool.execute("UPDATE managers SET name='Custom default', description='Preserved metadata' WHERE id='manager'")
        before = await pool.fetch("SELECT * FROM managers ORDER BY id")
        async with _serving(tmp_path, listen=True) as server:
            server._client_ops = ClientOps(SimpleNamespace())
            status, exported = await request(server, token, "config/export", {})
            assert status == 200, exported
            document = exported["result"]["document"]
            if include_default:
                # Also accept files from exporters that carry the default even when it is alone.
                document.setdefault("managers", [{"id": "manager", "name": "Custom default",
                                                 "description": "Preserved metadata", "idle_timeout_seconds": 0}])
            for route in ("import_preview", "import_upload"):
                body = {"document": document} if route == "import_preview" else document
                status, staged = await request(server, token, f"config/{route}", body)
                assert status == 200, staged
                assert not staged["result"]["plan"]["applied"]
                status, applied = await request(server, token, "config/import_apply",
                                                {"import_id": staged["result"]["import_id"]})
                if token == CLIENT_TOKEN:
                    assert status == 202, applied
                    status, applied = await request(server, token, "confirm",
                                                    {"confirmation_id": applied["confirmation_id"]})
                assert status == 200 and applied["result"]["plan"]["applied"], applied
                assert await pool.fetch("SELECT * FROM managers ORDER BY id") == before
            server._config_imports.clear()
    run_db(run, monkeypatch, timeout=40)


@pytest.mark.parametrize("change", ["name", "description", "second_manager"])
def test_client_manager_changes_remain_operator_only(tmp_path, monkeypatch, change):
    async def run(pool):
        before = await pool.fetch("SELECT * FROM managers ORDER BY id")
        row = dict(before[0])
        document = {"format": config_transfer.FORMAT, "managers": [config_transfer.manager_entry(row, [], {}, {})]}
        if change == "second_manager":
            document["managers"].append({"id": "manager-example", "name": "Example", "system_prompt": "Example tasks."})
        else:
            document["managers"][0][change] = "Changed metadata"
        async with _serving(tmp_path, listen=True) as server:
            server._client_ops = ClientOps(SimpleNamespace())
            for route in ("import_preview", "import_upload"):
                body = {"document": document} if route == "import_preview" else document
                status, refused = await request(server, CLIENT_TOKEN, f"config/{route}", body)
                assert status == 403 and refused["error"]["code"] == "forbidden", refused
                assert not server._config_imports._pending
            assert await pool.fetch("SELECT * FROM managers ORDER BY id") == before
            server._config_imports.clear()
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("confirm", [False, True], ids=["apply", "confirm"])
def test_client_default_manager_is_rechecked_before_execution(tmp_path, monkeypatch, confirm):
    async def run(pool):
        row = dict(await pool.fetchrow("SELECT * FROM managers WHERE id='manager'"))
        document = {"format": config_transfer.FORMAT, "managers": [config_transfer.manager_entry(row, [], {}, {})],
                    "settings": {"scheduler": {"min_interval_seconds": 73}}}
        async with _serving(tmp_path, listen=True) as server:
            server._client_ops = ClientOps(SimpleNamespace())
            status, staged = await request(server, CLIENT_TOKEN, "config/import_preview", {"document": document})
            assert status == 200, staged
            import_id = staged["result"]["import_id"]
            if confirm:
                status, confirmation = await request(server, CLIENT_TOKEN, "config/import_apply", {"import_id": import_id})
                assert status == 202, confirmation
            await pool.execute("UPDATE managers SET description='Changed after staging' WHERE id='manager'")
            before = await pool.fetch("SELECT * FROM managers ORDER BY id")
            route, body = (("confirm", {"confirmation_id": confirmation["confirmation_id"]}) if confirm else
                           ("config/import_apply", {"import_id": import_id}))
            status, refused = await request(server, CLIENT_TOKEN, route, body)
            assert status == 403 and refused["error"]["code"] == "forbidden", refused
            assert await pool.fetch("SELECT * FROM managers ORDER BY id") == before
            assert await pool.fetchval("SELECT value FROM system_settings WHERE key=$1",
                                       scheduler_settings.MIN_INTERVAL_KEY) is None
            server._config_imports.clear()
    run_db(run, monkeypatch, timeout=30)
