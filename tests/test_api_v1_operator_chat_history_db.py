"""Operator history reads and persistence on disposable PostgreSQL, separate from client-owned chat."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from looper.agents import api_v1
from looper.agents.http_server import AgentHTTPServer
from looper.agents.ops.clients import ClientOps
from looper.db import api_clients, chat
from tests.db_support import DSN, run_db
from tests.test_managers_schema_db import _manager
from tests.test_scenario_manager_chat_api_db import _Fleet, _Stub

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def _api():
    server = AgentHTTPServer(web_adapter=SimpleNamespace(), supervisor=SimpleNamespace(),
                             task_router=SimpleNamespace(), client_ops=ClientOps(SimpleNamespace()),
                             managers=_Fleet(_Stub("manager", "Manager"), _Stub("manager-review", "Review")))

    async def call(action, body=None, *, principal=api_v1.OPERATOR, client_chat=False):
        path = "/api/v1/" + ("chat/" if client_chat else "operator_chat/") + action
        status, answer = await server._answer_v1("POST", path, api_v1.find("POST", path), {},
                                               json.dumps(body or {}).encode(), principal)
        if answer.get("status") == "ok":
            api_v1.validate_arguments(api_v1.find("POST", path).result, answer["result"])
        return int(status.split()[0]), answer

    return call


@pytest.mark.parametrize("action", ["messages", "sessions"])
@pytest.mark.parametrize("old_default", [False, True], ids=["claimed-review", "legacy-default"])
def test_foreign_history_returns_its_manager_without_messages(monkeypatch, action, old_default):
    async def run(pool):
        await _manager(pool, "review")
        call = _api()
        if not old_default:
            await chat.claim_thread("web-foreign", "manager-review")
        await chat.save_message(session_id="web-foreign", message_id="one", role="user", content="private history")
        owner, wanted = ("manager", "manager-review") if old_default else ("manager-review", "manager")
        status, answer = await call(action, {"session_id": "web-foreign", "manager": wanted})
        assert status == 409 and answer["error"]["code"] == "conflict"
        assert answer["error"]["details"]["manager_id"] == owner
        assert "result" not in answer and "messages" not in answer
        status, answer = await call(action, {"session_id": "web-foreign", "manager": owner})
        assert status == 200 and len(answer["result"][action]) == 1

    run_db(run, monkeypatch, timeout=30)


def test_save_readback_is_idempotent_for_a_message_id(monkeypatch):
    async def run(pool):
        call = _api()
        body = {"session_id": "web-save", "message_id": "one", "role": "user", "content": "saved"}
        for _ in range(2):
            status, answer = await call("save_message", body)
            assert status == 200 and answer["result"] == {"ok": True}
        status, answer = await call("messages", {"session_id": "web-save"})
        assert status == 200 and [(m["message_id"], m["content"]) for m in answer["result"]["messages"]] == [
            ("one", "saved")]
        assert await pool.fetchval("SELECT count(*) FROM chat_messages WHERE session_id = 'web-save'") == 1

    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("action", ["messages", "sessions", "save_message"])
def test_database_failure_is_unavailable_instead_of_empty_or_success(monkeypatch, action):
    async def run(pool):
        call = _api()
        body = {"session_id": "web-save", **({"message_id": "one", "role": "user", "content": "saved"}
                                             if action == "save_message" else {})}
        await chat.save_message(session_id="web-save", message_id="one", role="user", content="saved")
        status, answer = await call(action, body)
        assert status == 200 and answer["result"]
        await pool.execute("ALTER TABLE chat_messages RENAME TO unavailable_messages")
        status, answer = await call(action, body)
        assert status == 503 and answer["error"]["code"] == "unavailable" and "result" not in answer

    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("action", ["targets", "sessions", "messages", "save_message"])
def test_client_cannot_use_operator_history_but_keeps_its_own_chat(monkeypatch, action):
    async def run(pool):
        view, _ = await api_clients.create("history reader", pool=pool)
        client = api_v1.Principal("client", view["id"], view["name"])
        call = _api()
        await chat.claim_thread("web-operator", "manager")
        await chat.save_message(session_id="web-operator", message_id="one", role="user", content="operator")
        body = {} if action == "targets" else {"session_id": "web-operator"}
        if action == "save_message":
            body.update(message_id="two", role="user", content="must not save")
        status, answer = await call(action, body, principal=client)
        assert status == 403 and answer["error"]["code"] == "forbidden" and "result" not in answer
        assert len(await chat.list_messages("web-operator")) == 1
        await chat.claim_thread("cli-owned", "manager", client_id=client.client_id)
        await chat.save_message(session_id="cli-owned", message_id="one", role="user", content="client")
        status, answer = await call("messages", {"conversation_id": "cli-owned"}, principal=client, client_chat=True)
        assert status == 200 and answer["result"]["messages"][0]["content"] == "client"
        status, answer = await call("messages", {"conversation_id": "web-operator"}, principal=client, client_chat=True)
        assert status == 404 and answer["error"]["code"] == "not_found"

    run_db(run, monkeypatch, timeout=30)
