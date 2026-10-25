"""Skill business refusals stay public across the credential-bearing catalog transport."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import catalog, catalog_sources, profile_registry, projects
from looper.agents.channels import bot_api
from looper.agents.ops import ErrorCode, OpError
from looper.agents.ops.clients import ClientOps
from looper.db import manager_actions
from tests.db_support import run_db
from tests.test_api_v1_auth import CLIENT_TOKEN, _bearer, _send, _serving, clients  # noqa: F401
from tests.test_operator_auth import TOKEN

SECRET = "synthetic-public-error-credential"


@pytest.mark.parametrize("fields,status,message", [
    ({"name": "BAD NAME"}, 400, "skill name 'BAD NAME' must be lowercase letters, digits and hyphens, "
     "such as deploy-check (no spaces, no hyphen at either end or twice in a row)"),
    ({"content": "---\nname: example\n---\nbody"}, 400,
     "give the content without frontmatter: the name and description fields make it"),
    ({}, 400, "give at least one of content, description, name"),
    ({"name": "taken"}, 409,
     "the catalog already has a skill named 'taken' (added in Settings); give this one another name"),
])
def test_skill_errors_and_successful_readback(tmp_path, monkeypatch, fields, status, message):
    async def run(pool):
        for name in ("example", "taken"):
            item = await catalog_sources.create_skill({"name": name, "description": "Example", "content": "Body"})
            if name == "example":
                item_id = item["item"]["item_id"]
        async with _serving(tmp_path, listen=True) as server:
            async def call(action, body):
                code, headers, answer = await _send(server, "POST /api/v1/catalog_items/" + action,
                                                   _bearer(TOKEN), json.dumps(body))
                assert headers["cache-control"] == "no-store"
                return code, answer
            code, answer = await call("update", {"item_id": item_id, "fields": fields})
            assert code == status and answer["error"]["message"] == message, answer
            code, answer = await call("update", {"item_id": item_id, "fields": {"description": "Changed"}})
            assert code == 200 and answer["result"]["item"]["description"] == "Changed"
            code, answer = await call("get", {"item_id": item_id})
            assert code == 200 and answer["result"]["item"]["description"] == "Changed"
            assert await pool.fetchval("SELECT count(*) FROM manager_actions") == 0
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("operation,body,module,service", [
    ("provider_accounts.create", {"name": "Example", "vendor_type": "custom", "api_key": SECRET},
     profile_registry, "create_provider"),
    ("channel_configs.create", {"channel_type": "telegram", "manager_id": "manager", "bot_token": SECRET},
     bot_api, "add_bot"),
    ("catalog_items.update", {"item_id": "mcp-example", "fields": {"env": {"API_KEY": SECRET}}},
     catalog, "update_item"),
    ("catalog_items.update", {"item_id": "skill-example", "fields": {"description": "Changed"}},
     catalog_sources, "update_item"),
])
@pytest.mark.parametrize("failure", ["parser", "schema", "op", "wrapped", "unexpected", "mcp", "ok"])
def test_credential_failures_are_private_and_unaudited(
        tmp_path, monkeypatch, caplog, operation, body, module, service, failure):
    audit = AsyncMock()
    caplog.set_level("DEBUG")
    monkeypatch.setattr(manager_actions, "record", audit)
    kind = "skill" if module is catalog_sources else "mcp"
    monkeypatch.setattr(catalog, "_item", AsyncMock(return_value={"kind": kind}))
    error = {"op": OpError(ErrorCode.INVALID, SECRET, details={"manager_id": SECRET}),
             "wrapped": projects.RequestError("500 Internal Server Error", SECRET),
             "unexpected": RuntimeError(SECRET), "mcp": catalog.CatalogError(SECRET)}
    if kind == "skill":
        error["mcp"] = RuntimeError(SECRET)  # arbitrary skill service failures are private too
    work = AsyncMock(return_value={"saved": True}, side_effect=error.get(failure))
    monkeypatch.setattr(module, service, work)

    async def run():
        async with _serving(tmp_path) as server:
            server._client_ops = SimpleNamespace(call=AsyncMock(side_effect=AssertionError("credential audit")))
            payload = (SECRET if failure == "parser" else
                       json.dumps({**body, SECRET: SECRET} if failure == "schema" else body))
            status, headers, answer = await _send(server, "POST /api/v1/" + operation.replace(".", "/"),
                                                  _bearer(TOKEN), payload)
            expected = 200 if failure == "ok" else 503 if (
                failure == "unexpected" or (failure == "mcp" and kind == "skill")) else 400
            assert status == expected, answer
            assert headers["cache-control"] == "no-store" and SECRET not in json.dumps(answer)
            assert "details" not in answer.get("error", {})
            assert work.await_count == (0 if failure in ("parser", "schema") else 1)
            audit.assert_not_awaited()
            server._client_ops.call.assert_not_awaited()
    asyncio.run(run())
    assert SECRET not in caplog.text


def test_safe_write_control_still_audits_arguments(tmp_path, monkeypatch):
    audit = AsyncMock()
    monkeypatch.setattr(manager_actions, "record", audit)
    monkeypatch.setattr(catalog_sources, "create_skill", AsyncMock(return_value={"item": {"name": "example"}}))
    body = {"name": "example", "description": "Example", "content": "Body"}
    async def run():
        async with _serving(tmp_path) as server:
            server._client_ops = ClientOps(SimpleNamespace())
            status, _, answer = await _send(server, "POST /api/v1/catalog_sources/create_skill",
                                            _bearer(TOKEN), json.dumps(body))
            assert status == 200 and answer["result"]["item"]["name"] == "example"
            audit.assert_awaited_once()
            assert audit.call_args.kwargs["arguments"] == body
    asyncio.run(run())


@pytest.mark.usefixtures("clients")
def test_client_catalog_update_refused_before_parsing_or_service(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("client credential request reached parsing, service or audit")
    monkeypatch.setattr(catalog, "_item", forbidden)
    monkeypatch.setattr(catalog, "update_item", forbidden)
    async def run():
        async with _serving(tmp_path) as server:
            monkeypatch.setattr(server, "_v1_body", forbidden)
            server._client_ops = SimpleNamespace(call=forbidden)
            status, _, answer = await _send(server, "POST /api/v1/catalog_items/update",
                                            _bearer(CLIENT_TOKEN), SECRET)
            assert status == 403 and answer["error"]["code"] == "forbidden"
    asyncio.run(run())
