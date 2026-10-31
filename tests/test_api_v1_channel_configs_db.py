"""Channel configuration v1 readback and deletion confirmation on disposable PostgreSQL."""

import asyncio
import json
from contextlib import asynccontextmanager

import pytest

from looper.agents import api_v1
from looper.agents.bus import MessageBus
from looper.agents.ops.clients import ClientOps
from looper.agents.ops.service import Platform
from looper.agents.serve import seed_channel_configs
from looper.db import api_clients, channel_configs
from tests.db_support import DSN, run_db
from tests.test_api_v1_auth import _bearer, _send, _serving
from tests.test_operator_auth import TOKEN

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
SECRET = "7712345:synthetic-channel-secret"
NEW_SECRET = "7712345:synthetic-rotated-secret"
CREATE = {"channel_type": "telegram", "manager_id": "manager", "bot_token": SECRET,
          "config": {"allowed_user_ids": ["12345"]}}


@asynccontextmanager
async def _api(tmp_path, pool):
    _, token = await api_clients.create("Channel validation", pool=pool)
    async with _serving(tmp_path) as server:
        server._client_ops = ClientOps(Platform(bus=MessageBus(runtime_root=tmp_path),
                                               task_router=server._task_router, runtime_root=tmp_path))
        async def call(action, body=None, *, client=False, session="channels", method="POST"):
            path = action if action.startswith("/") else "/api/v1/" + (
                action if action == "confirm" else "channel_configs/" + action)
            status, _, answer = await _send(server, f"{method} {path}", {
                **_bearer(token if client else TOKEN), "X-Looper-Session": session}, json.dumps(body or {}))
            if status == 200:
                api_v1.validate_arguments({"type": "object", "properties": {
                    "result": api_v1.find(method, path).result}}, {"result": answer["result"]})
            return status, answer
        yield call


def _run(run, monkeypatch):
    run_db(run, monkeypatch, setup=seed_channel_configs, timeout=30)


def test_crud_readback_preserves_manager_builtin_rows_and_secret_presence(tmp_path, monkeypatch):
    async def run(pool):
        await pool.execute("INSERT INTO managers (id,name,system_prompt) VALUES ('manager-review','Review','p')")
        async with _api(tmp_path, pool) as call:
            status, answer = await call("create", {**CREATE, "manager_id": "manager-review"})
            assert status == 200, answer
            added = answer["result"]
            assert added["manager_id"] == "manager-review" and added["manager_name"] == "Review"
            assert not added["enabled"] and not added["builtin"] and added["has_bot_token"]
            channel_id = added["channel_id"]
            status, answer = await call("update", {"channel_id": channel_id, "fields": {"display_name": "Example"}})
            assert status == 200 and answer["result"]["display_name"] == "Example"
            status, answer = await call("get", {"channel_id": channel_id}, client=True)
            assert status == 200 and answer["result"]["manager_id"] == "manager-review"
            status, listed = await call("list", client=True)
            assert status == 200 and len(listed["result"]["configs"]) == 4
            builtin = next(row for row in listed["result"]["configs"] if row["channel_id"] == "channel-telegram")
            assert builtin["builtin"] and builtin["manager_id"] == "manager"
            assert SECRET not in json.dumps(answer) + json.dumps(listed)
            assert await pool.fetchval("SELECT count(*) FROM manager_actions") == 0
    _run(run, monkeypatch)


@pytest.mark.parametrize("action", ["create", "update"])
@pytest.mark.parametrize("senders", [[7, 8], ["7", "8"], "7, 8"])
def test_telegram_sender_ids_round_trip_as_strings(tmp_path, monkeypatch, action, senders):
    async def run(pool):
        async with _api(tmp_path, pool) as call:
            config = {"allowed_user_ids": senders}
            request = ({**CREATE, "config": config} if action == "create" else
                       {"channel_id": "channel-telegram", "fields": {"config": config}})
            status, answer = await call(action, request)
            assert status == 200, answer
            channel_id = answer["result"]["channel_id"]
            assert answer["result"]["config"]["allowed_user_ids"] == ["7", "8"]
            status, answer = await call("get", {"channel_id": channel_id}, client=True)
            assert status == 200 and answer["result"]["config"]["allowed_user_ids"] == ["7", "8"]
            assert (await channel_configs.get_config(channel_id))["config"]["allowed_user_ids"] == ["7", "8"]
    _run(run, monkeypatch)


@pytest.mark.parametrize("action", ["create", "update"])
def test_ordinary_channel_saves_cannot_set_bound_only_policy(tmp_path, monkeypatch, action):
    from looper.db import chat_bindings

    async def run(pool):
        async with _api(tmp_path, pool) as call:
            status, answer = await call("create", CREATE)
            assert status == 200 and answer["result"]["bound_chats_only"] is False
            channel_id = answer["result"]["channel_id"]
            async with pool.acquire() as conn, conn.transaction():
                await chat_bindings.set_bound_chats_only(channel_id, True, conn=conn)
            body = ({**CREATE, "bound_chats_only": False} if action == "create" else
                    {"channel_id": channel_id, "fields": {"bound_chats_only": False}})
            status, answer = await call(action, body)
            assert status == 400 and answer["error"]["code"] == "invalid"
            status, answer = await call("update", {"channel_id": channel_id,
                "fields": {"display_name": "Renamed", "config": {"allowed_user_ids": ["12345"]}}})
            assert status == 200 and answer["result"]["bound_chats_only"] is True
            assert (await channel_configs.get_config(channel_id))["bound_chats_only"] is True
    _run(run, monkeypatch)


@pytest.mark.parametrize("action", ["create", "update"])
def test_read_only_database_channel_save_returns_private_service_failure(tmp_path, monkeypatch, action, caplog):
    import asyncpg

    from looper.db import connection

    async def run(pool):
        async with _serving(tmp_path, listen=True) as server:
            async def call(verb, body):
                return await _send(server, "POST /api/v1/channel_configs/" + verb,
                                   _bearer(TOKEN), json.dumps(body))

            status, _, created = await call("create", CREATE)
            assert status == 200, created
            channel_id = created["result"]["channel_id"]
            before = await pool.fetch("SELECT * FROM channel_configs ORDER BY channel_id")
            secrets_before = await pool.fetch("SELECT * FROM channel_secrets ORDER BY channel_id")
            schema = await pool.fetchval("SELECT current_schema()")
            async with asyncpg.create_pool(DSN, min_size=1, max_size=1, server_settings={
                    "search_path": schema, "default_transaction_read_only": "on"}) as readonly_pool:
                assert await readonly_pool.fetchval("SHOW transaction_read_only") == "on"
                with monkeypatch.context() as patch:
                    patch.setattr(connection, "_pool", readonly_pool)
                    request = ({**CREATE, "bot_token": "8899123:synthetic-readonly-secret"}
                               if action == "create" else {"channel_id": channel_id,
                                   "fields": {"bot_token": NEW_SECRET, "display_name": "Changed"}})
                    status, headers, answer = await call(action, request)
                assert status == 503 and answer["error"] == {
                    "code": "unavailable", "message": "invalid credential request"}, answer
                assert headers["cache-control"] == "no-store"
            assert await pool.fetch("SELECT * FROM channel_configs ORDER BY channel_id") == before
            assert await pool.fetch("SELECT * FROM channel_secrets ORDER BY channel_id") == secrets_before
            assert await pool.fetchval("SELECT count(*) FROM manager_actions") == 0
            assert all(value not in json.dumps(answer) + caplog.text for value in
                       (SECRET, NEW_SECRET, "8899123:synthetic-readonly-secret", "read-only transaction"))
    _run(run, monkeypatch)


@pytest.mark.parametrize("fields,expected", [({}, SECRET), ({"bot_token": "***"}, SECRET),
    ({"bot_token": NEW_SECRET}, NEW_SECRET), ({"bot_token": None}, None), ({"bot_token": ""}, None)])
def test_token_keep_replace_remove_variants_have_database_readback(tmp_path, monkeypatch, fields, expected):
    async def run(pool):
        async with _api(tmp_path, pool) as call:
            channel_id = (await call("create", CREATE))[1]["result"]["channel_id"]
            status, answer = await call("update", {"channel_id": channel_id,
                "fields": {"display_name": "Edited", **fields}})
            assert status == 200 and answer["result"]["has_bot_token"] is (expected is not None), answer
            assert await channel_configs.get_bot_token(channel_id) == expected
            assert SECRET not in json.dumps(answer) and NEW_SECRET not in json.dumps(answer)
    _run(run, monkeypatch)


@pytest.mark.usefixtures("lark_cli_stand_in")  # Resolve the seeded Lark channel to a distinct app.
@pytest.mark.parametrize("kind,config", [
    ("telegram", {"allowed_user_ids": ["ou_example123"]}),
    ("telegram", {"allowed_user_ids": [True]}),
    ("telegram", {"allowed_user_ids": [7.5]}),
    ("telegram", {"allowed_user_ids": [0]}),
    ("telegram", {"allowed_user_ids": [-7]}),
    ("lark_bot", {"app_id": "cli_example", "allowed_open_ids": ["on_example123"]}),
    ("lark_bot", {"app_id": "cli_example", "allowed_open_ids": ["12345"]}),
    ("lark_bot", {"app_id": "cli_example", "allowed_open_ids": [7]}),
])
def test_invalid_allowlist_cannot_partially_replace_a_secret(tmp_path, monkeypatch, kind, config):
    async def run(pool):
        create = CREATE if kind == "telegram" else {**CREATE, "channel_type": kind,
            "config": {"app_id": "cli_example", "allowed_open_ids": ["ou_example123"]}}
        async with _api(tmp_path, pool) as call:
            status, answer = await call("create", create)
            assert status == 200, answer
            channel_id = answer["result"]["channel_id"]
            before = await channel_configs.get_config(channel_id)
            status, answer = await call("update", {"channel_id": channel_id,
                "fields": {"bot_token": NEW_SECRET, "config": config}})
            assert status == 400 and answer["error"]["code"] == "invalid"
            assert await channel_configs.get_config(channel_id) == before
            assert await channel_configs.get_bot_token(channel_id) == SECRET
    _run(run, monkeypatch)


def test_duplicate_concurrent_creates_leave_one_added_bot(tmp_path, monkeypatch):
    async def run(pool):
        async with _api(tmp_path, pool) as call:
            answers = await asyncio.gather(call("create", CREATE), call("create", CREATE))
            assert sorted(status for status, _ in answers) == [200, 409], answers
            assert await pool.fetchval("SELECT count(*) FROM channel_configs WHERE NOT channel_id = ANY($1)",
                ["channel-telegram", "channel-lark", "channel-lark_bot"]) == 1
    _run(run, monkeypatch)


@pytest.mark.parametrize("mode", ["confirm", "cancel", "gone"])
def test_client_delete_waits_for_confirmation_and_rechecks_current_row(tmp_path, monkeypatch, mode):
    async def run(pool):
        async with _api(tmp_path, pool) as call:
            channel_id = (await call("create", CREATE))[1]["result"]["channel_id"]
            status, pending = await call("delete", {"channel_id": channel_id}, client=True)
            assert status == 202 and pending["status"] == "needs_confirmation"
            assert channel_id in pending["impact"]
            confirm = {"confirmation_id": pending["confirmation_id"]}
            assert (await call("confirm", confirm, client=True, session="other"))[0] == 404
            assert await channel_configs.get_config(channel_id) is not None
            if mode == "cancel":
                return  # Declining the dialog never sends confirm.
            if mode == "gone":
                assert (await call("delete", {"channel_id": channel_id}))[0] == 200
            status, answer = await call("confirm", confirm, client=True)
            assert status == (404 if mode == "gone" else 200), answer
            if mode == "confirm":
                assert answer["result"] == {"deleted": channel_id, "restart": True}
            assert await channel_configs.get_config(channel_id) is None
            assert await channel_configs.get_bot_token(channel_id) is None
            assert len(await channel_configs.list_configs()) == 3
    _run(run, monkeypatch)


@pytest.mark.parametrize("method,action,fields", [
    ("GET", "list", {}), ("GET", "get", {}), ("POST", "create", CREATE),
    ("PUT", "update", {"display_name": "Edited"}), ("DELETE", "delete", {}),
])
def test_old_routes_are_404_with_matching_v1_positive_controls(tmp_path, monkeypatch, method, action, fields):
    async def run(pool):
        async with _api(tmp_path, pool) as call:
            channel_id = (await call("create", CREATE))[1]["result"]["channel_id"]
            legacy = "/api/channel-configs" + ("/" + channel_id if action in ("get", "update", "delete") else "")
            assert (await call(legacy, fields, method=method))[0] == 404
            body = fields if action in ("list", "create") else {"channel_id": channel_id}
            if action == "create":
                body = {**CREATE, "bot_token": "8812345:synthetic-other-secret"}
            if action == "update":
                body["fields"] = fields
            status, answer = await call(action, body)
            assert status == 200 and answer["result"], answer
    _run(run, monkeypatch)


@pytest.mark.parametrize("kind", ["lark", "lark_bot"])
def test_changing_app_preserves_original_sender_binding(tmp_path, monkeypatch, kind):
    from looper.agents.channels import lark_cli
    from types import SimpleNamespace

    async def identity(profile=None, **kwargs):
        return SimpleNamespace(app_id="cli_" + (profile or "default"))
    monkeypatch.setattr(lark_cli, "identity", identity)
    async def run(pool):
        config = {"allowed_open_ids": ["ou_example123"],
                  ("profile" if kind == "lark" else "app_id"): "first" if kind == "lark" else "cli_first"}
        create = {"channel_type": kind, "manager_id": "manager", "config": config}
        if kind == "lark_bot":
            create["bot_token"] = SECRET
        async with _api(tmp_path, pool) as call:
            status, answer = await call("create", create)
            assert status == 200, answer
            channel_id = answer["result"]["channel_id"]
            key, value = ("profile", "other") if kind == "lark" else ("app_id", "cli_other")
            status, answer = await call("update", {"channel_id": channel_id,
                "fields": {"config": {**config, key: value, "allowlist_app_id": "cli_forged"}}})
            assert status == 200 and answer["result"]["config"]["allowlist_app_id"] == "cli_first", answer
    _run(run, monkeypatch)


@pytest.mark.parametrize("action,body,status", [
    ("update", {"channel_id": "channel-telegram", "fields": {"manager_id": "manager-other"}}, 400),
    ("delete", {"channel_id": "channel-telegram"}, 400),
    ("get", {"channel_id": "missing"}, 404),
    ("update", {"channel_id": "missing", "fields": {"enabled": False}}, 404),
    ("delete", {"channel_id": "missing"}, 404),
])
def test_refused_manager_builtin_and_unknown_operations_leave_rows(tmp_path, monkeypatch, action, body, status):
    async def run(pool):
        async with _api(tmp_path, pool) as call:
            before = await channel_configs.list_configs()
            actual, answer = await call(action, body)
            assert actual == status and answer["error"]["code"] == ("not_found" if status == 404 else "invalid"), answer
            assert await channel_configs.list_configs() == before
    _run(run, monkeypatch)


@pytest.mark.parametrize("value,status", [(1800, 200), (0, 400), ("600", 400)])
def test_telegram_backlog_cap_update_validates_and_reads_back(tmp_path, monkeypatch, value, status):
    async def run(pool):
        before = await channel_configs.get_config("channel-telegram")
        async with _api(tmp_path, pool) as call:
            code, answer = await call("update", {"channel_id": "channel-telegram", "fields": {
                "config": {"backlog_max_age_seconds": value}}})
            assert code == status, answer
            if status == 200:
                assert answer["result"]["config"]["backlog_max_age_seconds"] == value
                code, answer = await call("get", {"channel_id": "channel-telegram"})
                assert code == 200 and answer["result"]["config"]["backlog_max_age_seconds"] == value
                assert (await channel_configs.get_config("channel-telegram"))["config"]["backlog_max_age_seconds"] == value
            else:
                assert answer["error"]["code"] == "invalid"
                assert await channel_configs.get_config("channel-telegram") == before
    _run(run, monkeypatch)
