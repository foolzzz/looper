"""Configuration uploads preserve transaction, ownership and runtime settings contracts."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import api_v1, config_imports, scheduler_settings
from looper.agents.ops.clients import ClientOps
from looper.db import api_clients, config_transfer as db_transfer
from tests.db_support import DSN, run_db
from tests.test_api_v1_auth import CLIENT, CLIENT_TOKEN, _bearer, _send, _serving, clients
from tests.test_config_v3_import_db import v3
from tests.test_operator_auth import TOKEN

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
OTHER = "lpc_" + "d" * 43


@pytest.fixture(autouse=True)
def isolated_scheduler(monkeypatch):
    for name in ("_min_interval", "_min_source"):
        monkeypatch.setattr(scheduler_settings, name, getattr(scheduler_settings, name))


async def request(server, token, action, data):
    status, _, answer = await _send(server, f"POST /api/v1/{action}", _bearer(token), json.dumps(data))
    return status, answer


def test_failed_apply_rolls_back_and_keeps_the_staged_upload(tmp_path, monkeypatch):
    async def run(pool):
        async with _serving(tmp_path) as server:
            server._client_ops = ClientOps(SimpleNamespace())
            status, uploaded = await request(server, TOKEN, "config/import_upload", v3())
            assert status == 200, uploaded
            import_id = uploaded["result"]["import_id"]
            path = tmp_path / config_imports.DIRNAME / f"{import_id}.json"
            write_profile = db_transfer.write_profile
            monkeypatch.setattr(db_transfer, "write_profile", AsyncMock(side_effect=RuntimeError("injected failure")))
            status, failed = await request(server, TOKEN, "config/import_apply", {"import_id": import_id})
            assert status == 503 and failed["error"]["code"] == "unavailable"
            assert await pool.fetchval("SELECT count(*) FROM llm_providers WHERE name='account'") == 0
            assert await pool.fetchval("SELECT count(*) FROM llm_models WHERE name='exact'") == 0
            assert path.exists()
            monkeypatch.setattr(db_transfer, "write_profile", write_profile)
            status, applied = await request(server, TOKEN, "config/import_apply", {"import_id": import_id})
            assert status == 200 and applied["result"]["plan"]["applied"]
            assert await pool.fetchval("SELECT count(*) FROM llm_models WHERE name='exact'") == 1
            assert not path.exists()
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("owner", [CLIENT_TOKEN, TOKEN])
def test_streamed_import_owner_confirmation_and_scheduler_reload(tmp_path, monkeypatch, clients, owner):
    monkeypatch.setattr(api_clients, "find", AsyncMock(side_effect=lambda token, **kw: (
        CLIENT if token == CLIENT_TOKEN else {**CLIENT, "id": "client-other"} if token == OTHER else None)))
    monkeypatch.setattr(scheduler_settings, "_min_interval", 300)
    monkeypatch.setattr(scheduler_settings, "_min_source", None)

    async def run(pool):
        async with _serving(tmp_path) as server:
            server._client_ops = ClientOps(SimpleNamespace())
            document = {"format": "looper-config/v4", "projects": [{"name": "Transferred"}],
                        "settings": {"scheduler": {"min_interval_seconds": 73}}}
            status, uploaded = await request(server, owner, "config/import_upload", document)
            assert status == 200 and not uploaded["result"]["plan"]["applied"]
            api_v1.validate_arguments(api_v1.find("POST", "/api/v1/config/import_upload").result, uploaded["result"])
            import_id = uploaded["result"]["import_id"]
            path = tmp_path / config_imports.DIRNAME / f"{import_id}.json"
            assert await pool.fetchval("SELECT count(*) FROM projects WHERE name='Transferred'") == 0
            assert scheduler_settings.min_interval_seconds(gated=False) == 300
            for other in (OTHER, TOKEN if owner == CLIENT_TOKEN else CLIENT_TOKEN):
                for action in ("config/import_apply", "config/import_cancel"):
                    status, refused = await request(server, other, action, {"import_id": import_id})
                    assert status == 404 and refused["error"]["code"] == "not_found"
                    assert path.exists()
            status, applied = await request(server, owner, "config/import_apply", {"import_id": import_id})
            if owner == CLIENT_TOKEN:
                assert status == 202 and applied["status"] == "needs_confirmation" and applied["impact"]
                assert await pool.fetchval("SELECT count(*) FROM projects WHERE name='Transferred'") == 0
                assert scheduler_settings.min_interval_seconds(gated=False) == 300
                status, applied = await request(server, owner, "confirm", {
                    "confirmation_id": applied["confirmation_id"]})
            assert status == 200 and applied["result"]["plan"]["applied"]
            assert await pool.fetchval("SELECT count(*) FROM projects WHERE name='Transferred'") == 1
            assert scheduler_settings.min_interval_seconds(gated=False) == 73
            assert not path.exists()
    run_db(run, monkeypatch, timeout=30)
