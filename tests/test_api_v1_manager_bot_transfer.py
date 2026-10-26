"""Manager bots use the existing operator transfer envelope and closed v4 projection."""

from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import api_v1, config_imports, config_transfer
from looper.agents.ops import OpError
from looper.agents.ops.clients import ClientOps
from looper.agents.v1_domains.configuration import CONFIG_DOCUMENT
from tests.db_support import DSN, run_db
from tests.test_api_v1_auth import CLIENT_TOKEN, TOKEN, _bearer, _send, _serving, clients  # noqa: F401


BOT = {"channel_id": "channel-telegram-2", "channel_type": "telegram", "manager_id": "manager",
       "display_name": "Synthetic Telegram", "enabled": True, "config": {"allowed_user_ids": ["42"]}}
GRAPH = {"format": "looper-config/v4", "channels": [BOT]}


@pytest.mark.parametrize("field", ["channel_id", "channel_type", "manager_id", "config"])
def test_v4_bot_schema_requires_graph_identity_and_config(field):
    document = copy.deepcopy(GRAPH)
    del document["channels"][0][field]
    with pytest.raises(OpError):
        api_v1.validate_arguments(CONFIG_DOCUMENT, document)


@pytest.mark.parametrize("fields", [
    {"channel_type": "email"}, {"manager_id": None}, {"config": []}, {"enabled": "yes"},
    {"bot_token": "synthetic-token"}, {"app_secret": "synthetic-secret"}, {"bound_chats_only": True},
    {"manager_chat_bindings": []}, {"channel_seen_chats": []}, {"channel_unbound_chats": []},
])
def test_v4_bot_schema_is_closed_and_typed(fields):
    document = {"format": "looper-config/v4", "channels": [{**BOT, **fields}]}
    with pytest.raises(OpError):
        api_v1.validate_arguments(CONFIG_DOCUMENT, document)


def test_v4_nullable_display_name_and_export_share_document_projection():
    document = {"format": "looper-config/v4", "channels": [{**BOT, "display_name": None}]}
    assert api_v1.validate_arguments(CONFIG_DOCUMENT, document) == document
    assert api_v1.RESULTS["config.export"]["properties"]["document"]["oneOf"] == CONFIG_DOCUMENT["oneOf"]


@pytest.mark.parametrize("version", ["loopx-config/v1", "looper-config/v1", "looper-config/v2", "looper-config/v3"])
def test_legacy_builtin_channel_without_manager_still_validates(version):
    entry = {"channel_id": "channel-telegram", "channel_type": "telegram", "enabled": False, "config": {}}
    document = {"format": version, "channels": [entry]}
    assert api_v1.validate_arguments(CONFIG_DOCUMENT, document) == document


@pytest.mark.parametrize("kind", ["telegram", "lark", "lark_bot"])
def test_v4_old_backup_builtin_without_manager_still_validates(kind):
    entry = {"channel_id": f"channel-{kind}", "channel_type": kind, "enabled": False, "config": {}}
    document = {"format": "looper-config/v4", "channels": [entry], "managers": [
        {"id": "manager-example", "name": "Example"}]}
    assert api_v1.validate_arguments(CONFIG_DOCUMENT, document) == document


@pytest.mark.parametrize("route", ["import_preview", "import_upload"])
@pytest.mark.usefixtures("clients")
def test_clients_cannot_stage_added_bots_even_for_default_manager(tmp_path, monkeypatch, route):
    runner = AsyncMock(side_effect=AssertionError("refused bots must not reach importer"))
    monkeypatch.setattr(config_imports, "_run", runner)

    async def run():
        async with _serving(tmp_path) as server:
            server._client_ops = ClientOps(SimpleNamespace())
            status, _, answer = await _send(server, f"POST /api/v1/config/{route}", _bearer(CLIENT_TOKEN),
                json.dumps({"document": GRAPH} if route == "import_preview" else GRAPH))
            assert status == 403 and answer["error"]["code"] == "forbidden"
            assert not server._config_imports._pending
            runner.assert_not_awaited()
    asyncio.run(asyncio.wait_for(run(), 10))


@pytest.mark.parametrize("execution", ["apply", "confirm"])
@pytest.mark.usefixtures("clients")
def test_clients_cannot_bypass_bot_guard_by_replacing_staged_file(tmp_path, monkeypatch, execution):
    runner = AsyncMock(return_value=config_transfer.Plan())
    monkeypatch.setattr(config_imports, "_run", runner)

    async def run():
        async with _serving(tmp_path) as server:
            server._client_ops = ClientOps(SimpleNamespace())
            async def request(route, data):
                return await _send(server, f"POST /api/v1/{route}",
                    {**_bearer(CLIENT_TOKEN), "X-Looper-Session": "bot-transfer"}, json.dumps(data))
            status, _, staged = await request("config/import_preview", {"document": {"format": "looper-config/v3"}})
            assert status == 200, staged
            import_id = staged["result"]["import_id"]
            pending = server._config_imports._pending[import_id]
            data = {"import_id": import_id}
            if execution == "confirm":
                status, _, answer = await request("config/import_apply", data)
                assert status == 202 and answer["status"] == "needs_confirmation"
                data = {"confirmation_id": answer["confirmation_id"]}
            pending.path.write_text(json.dumps(GRAPH))
            status, _, answer = await request("confirm" if execution == "confirm" else "config/import_apply", data)
            assert status == 403 and answer["error"]["code"] == "forbidden"
            assert runner.await_count == 1
            assert not pending.applying
            server._config_imports.clear()
    asyncio.run(asyncio.wait_for(run(), 10))


@pytest.mark.usefixtures("clients")
def test_legacy_builtin_client_import_keeps_confirmation_flow(tmp_path, monkeypatch):
    runner = AsyncMock(return_value=config_transfer.Plan())
    monkeypatch.setattr(config_imports, "_run", runner)
    document = {"format": "looper-config/v3", "channels": [{"channel_type": "telegram", "config": {}}]}

    async def run():
        async with _serving(tmp_path) as server:
            server._client_ops = ClientOps(SimpleNamespace())
            async def request(route, data):
                return await _send(server, f"POST /api/v1/{route}",
                    {**_bearer(CLIENT_TOKEN), "X-Looper-Session": "bot-transfer"}, json.dumps(data))
            status, _, staged = await request("config/import_preview", {"document": document})
            assert status == 200, staged
            status, _, pending = await request("config/import_apply", {"import_id": staged["result"]["import_id"]})
            assert status == 202 and pending["status"] == "needs_confirmation"
            status, _, applied = await request("confirm", {"confirmation_id": pending["confirmation_id"]})
            assert status == 200 and applied["status"] == "ok", applied
            assert runner.await_count == 2
            assert runner.await_args.kwargs["apply"] is True
    asyncio.run(asyncio.wait_for(run(), 10))


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
@pytest.mark.parametrize("route", ["import_preview", "import_upload"])
def test_operator_bot_preview_apply_and_export_preserve_same_type_rows(tmp_path, monkeypatch, route):
    document = {"format": "looper-config/v4", "channels": [BOT, {**BOT, "channel_id": "channel-telegram-3",
        "manager_id": "manager-example", "display_name": "Second synthetic Telegram"}], "managers": [
        {"id": "manager-example", "name": "Example", "system_prompt": "Handle example tasks.", "enabled": False}]}

    async def run(pool):
        async with _serving(tmp_path) as server:
            server._client_ops = ClientOps(SimpleNamespace())
            async def request(operation, data):
                return await _send(server, f"POST /api/v1/config/{operation}", _bearer(TOKEN), json.dumps(data))
            sequence = tuple(await pool.fetchrow("SELECT last_value, is_called FROM channel_bot_seq"))
            status, _, staged = await request(route, {"document": document} if route == "import_preview" else document)
            assert status == 200, staged
            assert await pool.fetchval("SELECT count(*) FROM channel_configs") == 0
            assert tuple(await pool.fetchrow("SELECT last_value, is_called FROM channel_bot_seq")) == sequence
            result = staged["result"]
            assert {row["key"] for row in result["plan"]["changes"] if row["section"] == "channels"} == {
                "channel-telegram-2", "channel-telegram-3"}
            assert {row["target"]["id"] for row in result["plan"]["need_secret"]} == {
                "channel-telegram-2", "channel-telegram-3"}
            status, _, applied = await request("import_apply", {"import_id": result["import_id"]})
            assert status == 200 and applied["result"]["plan"]["applied"], applied
            rows = await pool.fetch("SELECT channel_id, manager_id, enabled FROM channel_configs ORDER BY channel_id")
            assert [tuple(row) for row in rows] == [("channel-telegram-2", "manager", False),
                ("channel-telegram-3", "manager-example", False)]
            assert await pool.fetchval("SELECT count(*) FROM channel_secrets") == 0
            status, _, exported = await request("export", {"include_secrets": True})
            assert status == 200, exported
            channels = exported["result"]["document"]["channels"]
            assert {row["channel_id"] for row in channels} == {"channel-telegram-2", "channel-telegram-3"}
            assert all("bot_token" not in row and "bound_chats_only" not in row for row in channels)
            server._config_imports.clear()
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
def test_operator_cancellation_writes_no_bot_manager_or_sequence(tmp_path, monkeypatch):
    async def run(pool):
        async with _serving(tmp_path) as server:
            server._client_ops = ClientOps(SimpleNamespace())
            async def request(operation, data):
                return await _send(server, f"POST /api/v1/config/{operation}", _bearer(TOKEN), json.dumps(data))
            before = tuple(await pool.fetchrow("SELECT last_value, is_called FROM channel_bot_seq"))
            status, _, staged = await request("import_preview", {"document": GRAPH})
            assert status == 200, staged
            status, _, cancelled = await request("import_cancel", {"import_id": staged["result"]["import_id"]})
            assert status == 200 and cancelled["status"] == "ok", cancelled
            assert await pool.fetchval("SELECT count(*) FROM channel_configs") == 0
            assert await pool.fetchval("SELECT count(*) FROM managers") == 1
            assert tuple(await pool.fetchrow("SELECT last_value, is_called FROM channel_bot_seq")) == before
            assert not server._config_imports._pending
    run_db(run, monkeypatch, timeout=20)


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
@pytest.mark.parametrize("after_preview", [False, True])
def test_consumed_absent_bot_id_is_conflict_in_preview_and_rechecked_at_apply(tmp_path, monkeypatch, after_preview):
    async def run(pool):
        async with _serving(tmp_path) as server:
            server._client_ops = ClientOps(SimpleNamespace())
            async def request(operation, data):
                return await _send(server, f"POST /api/v1/config/{operation}", _bearer(TOKEN), json.dumps(data))
            if after_preview:
                status, _, staged = await request("import_preview", {"document": GRAPH})
                assert status == 200, staged
            # A local allocation is historical even if that local row was subsequently removed.
            await pool.execute("SELECT setval('channel_bot_seq', 2, true)")
            before = tuple(await pool.fetchrow("SELECT last_value, is_called FROM channel_bot_seq"))
            status, _, answer = await request("import_apply" if after_preview else "import_preview",
                {"import_id": staged["result"]["import_id"]} if after_preview else {"document": GRAPH})
            assert status == 409 and answer["error"]["code"] == "conflict", answer
            assert "locally" in answer["error"]["message"]
            assert await pool.fetchval("SELECT count(*) FROM channel_configs") == 0
            assert await pool.fetchval("SELECT count(*) FROM channel_secrets") == 0
            assert tuple(await pool.fetchrow("SELECT last_value, is_called FROM channel_bot_seq")) == before
            server._config_imports.clear()
    run_db(run, monkeypatch, timeout=20)
