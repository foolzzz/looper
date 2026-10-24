"""Chat with a Manager over ``/api/v1`` on a real PostgreSQL: a send with its streamed reply, a thread read by
cursor, a client's conversations; the dashboard's send is as it was.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from looper.agents.api_v1 import OPERATOR, Principal
from tests.test_managers_schema_db import _manager
from tests.test_scenario_manager_chat_api_db import _Fleet, _Stub
from tests.db_support import DSN, run_db
from tests.test_usage_api_db import _Writer

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

FINAL = {"kind": "answer.final", "payload": {"text": "done", "outcome": "answered"}}


class _Web:
    def __init__(self, events: list[dict[str, Any]] | None = None) -> None:
        self.sent: list[dict[str, Any]] = []
        self.events = [FINAL] if events is None else events
        self.closed: list[str] = []
        self.timeouts: list[float] = []

    async def handle_user_message(self, **kwargs: Any) -> dict[str, Any]:
        self.sent.append(kwargs)
        return {"correlation_id": "c1"}

    async def iter_sse_events(self, correlation_id: str, timeout: float = 300.0) -> Any:
        self.timeouts.append(timeout)
        for event in self.events:
            yield event

    def close_sse_stream(self, correlation_id: str) -> None:
        self.closed.append(correlation_id)


async def _client(pool: Any, name: str = "agent") -> Principal:
    from looper.db import api_clients

    view, _ = await api_clients.create(name, pool=pool)
    return Principal("client", view["id"], view["name"])


async def _v1(web: Any, path: str, body: Any, principal: Principal, fleet: Any = None) -> tuple[int, Any]:
    """The answer of a JSON operation: its status and body."""
    from looper.agents.http_server import AgentHTTPServer
    from looper.agents.tiers.task import TaskRouter

    server = AgentHTTPServer(web_adapter=web, supervisor=SimpleNamespace(), managers=fleet,
                             task_router=TaskRouter(runtime_root=Path(tempfile.mkdtemp())))
    writer = _Writer()
    await server._handle_v1("POST", path, {}, {}, json.dumps(body).encode(), writer, principal)
    head, _, payload = writer.data.partition(b"\r\n\r\n")
    return int(head.split()[1]), (json.loads(payload) if payload.startswith(b"{") else payload.decode())


def _events(body: str) -> list[tuple[str, Any]]:
    parsed = []
    for block in body.strip().split("\n\n"):
        kind, data = block.split("\n")
        assert kind.startswith("event: ") and data.startswith("data: ")
        parsed.append((kind[7:], json.loads(data[6:])))
    return parsed


async def _send(web: _Web, principal: Principal, body: dict[str, Any], fleet: Any = None) -> tuple[int, Any]:
    from looper.agents.http_server import AgentHTTPServer
    from looper.agents.tiers.task import TaskRouter

    server = AgentHTTPServer(web_adapter=web, supervisor=SimpleNamespace(), managers=fleet,
                             task_router=TaskRouter(runtime_root=Path(tempfile.mkdtemp())))
    writer = _Writer()
    await server._handle_v1("POST", "/api/v1/chat/send", {}, {}, json.dumps(body).encode(), writer, principal)
    head, _, payload = writer.data.partition(b"\r\n\r\n")
    status = int(head.split()[1])
    if status == 200:
        assert b"Content-Type: text/event-stream" in head and b"Cache-Control: no-store" in head
        assert b"X-Accel-Buffering: no" in head
        return status, _events(payload.decode())
    return status, json.loads(payload)


def test_a_new_conversation_streams_the_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import chat

    async def test(pool: Any) -> None:
        client = await _client(pool)
        web = _Web([{"kind": "agent.phase", "payload": {"label": "thinking"}},
                    {"kind": "answer.delta", "payload": {"text": "do"}},
                    {"kind": "heartbeat", "payload": {}},
                    {"kind": "system.event", "payload": {"type": "x"}},
                    FINAL])
        status, events = await _send(web, client, {"text": "你好"})
        assert status == 200
        (kind, accepted), rest = events[0], events[1:]
        cid = accepted["conversation_id"]
        assert kind == "accepted" and cid.startswith("cli-") and accepted["manager_id"] == "manager"
        assert rest == [("status", {"label": "thinking"}), ("delta", {"text": "do"}), ("heartbeat", {}),
                        ("system.event", {"type": "x"}), ("final", {"text": "done", "outcome": "answered"})]
        assert web.timeouts == [1800]
        assert await chat.thread_owner(cid) == "manager" and await chat.thread_client(cid) == client.client_id
        kwargs = web.sent[0]
        assert kwargs["client"] == {"id": client.client_id, "name": "agent"} and kwargs["session_id"] == cid
        assert kwargs["message_id"] == accepted["message_id"] and kwargs["text"] == "你好"
        saved = await chat.messages_page(cid, pool=pool)
        assert [(m["message_id"], m["role"], m["content"]) for m in saved[0]] == [
            (accepted["message_id"], "user", "你好")]

    run_db(test, monkeypatch)


def test_a_stream_without_a_final_ends_with_a_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        client = await _client(pool)
        status, events = await _send(_Web([{"kind": "answer.delta", "payload": {"text": "x"}}]), client,
                                     {"text": "hi"})
        cid = events[0][1]["conversation_id"]
        assert status == 200 and [k for k, _ in events] == ["accepted", "delta", "timeout"]
        assert events[-1][1]["message"] == (
            f"the reply is still being written; read it later with: loopercli chat --conversation {cid} --read")

    run_db(test, monkeypatch)


def test_a_conversation_is_continued_only_by_its_client(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import chat

    async def test(pool: Any) -> None:
        mine, other = await _client(pool), await _client(pool, "other")
        web = _Web()
        _, events = await _send(web, mine, {"text": "one"})
        cid = events[0][1]["conversation_id"]
        status, events = await _send(web, mine, {"text": "two", "conversation_id": cid})
        assert status == 200 and events[0][1]["conversation_id"] == cid and len(web.sent) == 2
        assert web.sent[1]["session_id"] == cid

        status, answer = await _send(web, other, {"text": "x", "conversation_id": cid})
        assert status == 404 and answer["error"] == {
            "code": "not_found", "message": f"no conversation {cid} of this client"}
        await chat.claim_thread("web-1", "manager")
        status, answer = await _send(web, mine, {"text": "x", "conversation_id": "web-1"})
        assert status == 404 and answer["error"]["code"] == "not_found"
        status, answer = await _send(web, OPERATOR, {"text": "x"})
        assert status == 403 and answer["error"]["code"] == "forbidden"
        assert len(web.sent) == 2

    run_db(test, monkeypatch)


def test_the_arguments_are_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        client = await _client(pool)
        web = _Web()
        status, answer = await _send(web, client, {"text": "x" * 32_001})
        assert status == 400 and answer["error"]["code"] == "invalid"
        status, answer = await _send(web, client, {"text": "hi", "client": {"id": "client-other"}})
        assert status == 400 and answer["error"]["code"] == "invalid"
        status, answer = await _send(web, client, {"text": "hi", "api_key": "sk-1"})
        assert status == 403 and answer["error"]["code"] == "forbidden"
        status, answer = await _send(web, client, {})
        assert status == 400
        status, answer = await _send(web, client, {"text": "  "})
        assert status == 400
        status, answer = await _send(web, client, {"text": "hi", "manager_id": "manager-nope"})
        assert status == 400 and answer["error"]["message"] == "no Manager 'manager-nope'"
        assert web.sent == []

    run_db(test, monkeypatch)


def test_a_thread_is_read_by_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import chat

    async def test(pool: Any) -> None:
        mine, other = await _client(pool), await _client(pool, "other")
        await chat.claim_thread("cli-t", "manager", client_id=mine.client_id)
        for i in range(5):
            await chat.save_message(session_id="cli-t", message_id=f"m{i}", role="user" if i % 2 == 0 else "assistant",
                                    content=f"c{i}", pool=pool)
        read = lambda body, who=mine: _v1(None, "/api/v1/chat/messages", {"conversation_id": "cli-t", **body}, who)

        status, answer = await read({"limit": 2})
        result = answer["result"]
        assert status == 200 and [m["message_id"] for m in result["messages"]] == ["m0", "m1"]
        assert result["has_more"] is True and result["next_cursor"] == "m1" and result["manager_id"] == "manager"
        assert set(result["messages"][0]) == {"message_id", "role", "content", "created_at"}
        assert (result["messages"][1]["role"], result["messages"][1]["content"]) == ("assistant", "c1")

        seen, cursor = [], None
        while True:
            _, answer = await read({"limit": 2, **({"after": cursor} if cursor else {})})
            seen += [m["message_id"] for m in answer["result"]["messages"]]
            cursor = answer["result"]["next_cursor"]
            if not answer["result"]["has_more"]:
                break
        assert seen == ["m0", "m1", "m2", "m3", "m4"] and cursor == "m4"

        _, answer = await read({"after": "m4"})
        assert answer["result"] == {"conversation_id": "cli-t", "manager_id": "manager", "messages": [],
                                    "next_cursor": "m4", "has_more": False}
        status, answer = await read({"after": "nope"})
        assert status == 400 and answer["error"]["code"] == "invalid" and "unknown cursor nope" in answer["error"]["message"]
        for limit in (0, 201):
            assert (await read({"limit": limit}))[0] == 400
        assert (await read({}, other))[0] == 404
        assert (await read({}, OPERATOR))[0] == 403
        status, answer = await _v1(None, "/api/v1/chat/messages", {"conversation_id": "nope"}, mine)
        assert status == 404

        await chat.claim_thread("cli-empty", "manager", client_id=mine.client_id)
        _, answer = await _v1(None, "/api/v1/chat/messages", {"conversation_id": "cli-empty"}, mine)
        assert answer["result"]["next_cursor"] is None and answer["result"]["messages"] == []

    run_db(test, monkeypatch)


def test_conversations_are_the_clients_own(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import chat

    async def test(pool: Any) -> None:
        mine, other = await _client(pool), await _client(pool, "other")
        await chat.claim_thread("cli-a", "manager", client_id=mine.client_id)
        await chat.save_message(session_id="cli-a", message_id="m1", role="user", content="hello", pool=pool)
        await chat.claim_thread("cli-b", "manager", client_id=other.client_id)
        await chat.claim_thread("web-1", "manager")
        status, answer = await _v1(None, "/api/v1/chat/conversations", {}, mine)
        (only,) = answer["result"]["conversations"]
        assert status == 200 and only["conversation_id"] == "cli-a" and only["manager_id"] == "manager"
        assert only["last_content"] == "hello" and set(only) == {
            "conversation_id", "manager_id", "created_at", "last_message_at", "last_content"}
        assert (await _v1(None, "/api/v1/chat/conversations", {}, OPERATOR))[0] == 403

    run_db(test, monkeypatch)


def test_a_scenario_manager_keeps_its_conversation(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import chat

    async def test(pool: Any) -> None:
        await _manager(pool, "review", name="Code review")
        fleet = _Fleet(_Stub("manager", "Manager"), _Stub("manager-review", "Code review"))
        mine, other = await _client(pool), await _client(pool, "other")
        web = _Web()
        status, events = await _send(web, mine, {"text": "开始", "manager_id": "manager-review"}, fleet)
        cid = events[0][1]["conversation_id"]
        assert status == 200 and cid.startswith("cli-") and events[0][1]["manager_id"] == "manager-review"
        assert await chat.thread_owner(cid) == "manager-review" and await chat.thread_client(cid) == mine.client_id
        assert web.sent[0]["target"].agent_id == "manager-review"

        status, events = await _send(web, mine, {"text": "确认", "conversation_id": cid}, fleet)
        assert status == 200 and events[0][1]["manager_id"] == "manager-review"
        assert web.sent[1]["target"].agent_id == "manager-review" and web.sent[1]["session_id"] == cid

        status, answer = await _v1(web, "/api/v1/chat/conversations", {}, mine, fleet)
        assert [c["manager_id"] for c in answer["result"]["conversations"]] == ["manager-review"]
        status, answer = await _send(web, mine, {"text": "x", "conversation_id": cid, "manager_id": "manager"}, fleet)
        assert status == 409 and answer["error"] == {
            "code": "conflict", "message": f"conversation {cid} belongs to Manager manager-review"}
        status, answer = await _send(web, other, {"text": "x", "conversation_id": cid}, fleet)
        assert status == 404
        assert len(web.sent) == 2

    run_db(test, monkeypatch)


def test_a_manager_that_is_not_running_takes_no_message(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        await _manager(pool, "off", name="Off", enabled=False)
        fleet = _Fleet(_Stub("manager", "Manager"))
        mine = await _client(pool)
        web = _Web()
        status, answer = await _send(web, mine, {"text": "x", "manager_id": "manager-off"}, fleet)
        assert status == 409 and answer["error"]["code"] == "conflict"
        from looper.db import chat
        await chat.claim_thread("cli-off", "manager-off", client_id=mine.client_id)
        status, answer = await _send(web, mine, {"text": "x", "conversation_id": "cli-off"}, fleet)
        assert status == 409 and answer["error"]["code"] == "conflict" and web.sent == []

    run_db(test, monkeypatch)


def test_a_dropped_client_closes_the_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        from looper.agents.http_server import AgentHTTPServer
        from looper.agents.tiers.task import TaskRouter

        class Gone(_Writer):
            async def drain(self) -> None:
                if b"event: accepted" in self.data:
                    raise BrokenPipeError

        client, web = await _client(pool), _Web()
        server = AgentHTTPServer(web_adapter=web, supervisor=SimpleNamespace(),
                                 task_router=TaskRouter(runtime_root=Path(tempfile.mkdtemp())))
        await server._handle_v1("POST", "/api/v1/chat/send", {}, {}, b'{"text": "hi"}', Gone(), client)
        assert web.closed == ["c1"]

    run_db(test, monkeypatch)


def test_the_dashboard_send_has_no_client(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        from looper.agents.http_server import AgentHTTPServer
        from looper.agents.tiers.task import TaskRouter

        web = _Web()
        server = AgentHTTPServer(web_adapter=web, supervisor=SimpleNamespace(),
                                 task_router=TaskRouter(runtime_root=Path(tempfile.mkdtemp())))
        writer = _Writer()
        body = {"text": "hello", "session_id": "s1", "message_id": "m1"}
        from looper.agents.ops.clients import ClientOps
        server._client_ops = ClientOps(SimpleNamespace())
        await server._handle_v1("POST", "/api/v1/operator_chat/send", {}, {}, json.dumps(body).encode(), writer, OPERATOR)
        assert writer.data.startswith(b"HTTP/1.1 200") and len(web.sent) == 1
        assert "client" not in web.sent[0] and web.sent[0]["session_id"] == "s1"

    run_db(test, monkeypatch)
