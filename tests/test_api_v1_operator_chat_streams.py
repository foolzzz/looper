"""Dashboard chat keeps its own typed operator transport and closes abandoned subscriptions."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper import loopercli
from looper.agents import api_v1
from looper.agents.channels.web import WebChannelAdapter
from looper.agents.message import MANAGER_ADDRESS, MessageEnvelope, MessageType, channel_address
from looper.agents.ops.clients import ClientOps
from looper.db import chat
from looper.db import manager_actions
from tests.test_api_v1_auth import CLIENT_TOKEN, _bearer, _send, _send_raw, _serving, clients
from tests.test_operator_auth import TOKEN, _request
from tests.test_loopercli import Server, _send as cli_send, write_client_file

PREFIX = "/api/v1/operator_chat/"


@pytest.fixture
def state(monkeypatch):
    monkeypatch.setattr(chat, "save_message", AsyncMock())
    monkeypatch.setattr(chat, "thread_owner", AsyncMock(return_value=None))
    monkeypatch.setattr(chat, "has_messages", AsyncMock(return_value=False))
    monkeypatch.setattr(chat, "claim_thread", AsyncMock(return_value="manager"))
    monkeypatch.setattr(manager_actions, "record", AsyncMock())
    return SimpleNamespace(published=[])


def prepare(server, state):
    adapter = WebChannelAdapter()
    adapter._bus = SimpleNamespace(publish=AsyncMock(side_effect=state.published.append))
    server._web_adapter = adapter
    server._client_ops = ClientOps(SimpleNamespace())
    server._task_router = SimpleNamespace(has_planner=lambda task: task == "running")
    return adapter


def parse_events(body):
    return [(block.splitlines()[0][7:], json.loads(block.splitlines()[1][6:]))
            for block in body.strip().split("\n\n") if block.strip()]


async def post(server, action, body):
    return await _send(server, "POST " + PREFIX + action, _bearer(TOKEN), json.dumps(body))


@pytest.mark.parametrize("action", ["send", "reply_events", "thread_events"])
def test_client_refusal_precedes_body_validation_and_dispatch(tmp_path, clients, action):
    operation = api_v1.find("POST", PREFIX + action)
    assert operation.operator_only and not operation.hand
    assert operation.risk == ("write" if action == "send" else "read")
    async def run():
        async with _serving(tmp_path) as server:
            status, _, answer = await _send(server, "POST " + PREFIX + action, _bearer(CLIENT_TOKEN), "{")
            assert status == 403 and answer["error"]["code"] == "forbidden"
    asyncio.run(run())


@pytest.mark.parametrize("action,body", [("reply_events", {}), ("reply_events", {"correlation_id": None}),
    ("reply_events", {"correlation_id": "known", "extra": True}), ("thread_events", {}),
    ("thread_events", {"session_id": 1}), ("thread_events", {"session_id": "thread", "manager": None})])
def test_stream_requests_are_closed_with_required_typed_identifiers(tmp_path, action, body):
    async def run():
        async with _serving(tmp_path, listen=True) as server:
            status, _, answer = await post(server, action, body)
            assert status == 400 and answer["error"]["code"] == "invalid"
    asyncio.run(run())


@pytest.mark.parametrize("body", [
    {}, {"text": ""}, {"text": " "}, {"text": "x" * 32001}, {"text": "x", "client_id": "other"},
    {"text": "x", "language": None}, {"text": "x", "target": {"kind": "worker", "id": "one"}},
    {"text": "x", "target": {"kind": "manager", "client_id": "other"}},
    {"text": "x", "target": {"kind": "planner", "task_id": 1}},
    {"text": "x", "new_session_profile_id": "chosen"},
    {"text": "/new", "new_session_profile_id": "chosen", "target": {"kind": "planner", "task_id": "running"}},
])
def test_invalid_send_does_not_publish(tmp_path, state, body):
    async def run():
        async with _serving(tmp_path) as server:
            prepare(server, state)
            status, _, answer = await post(server, "send", body)
            assert status == 400 and answer["error"]["code"] == "invalid"
            assert state.published == []
    asyncio.run(run())


def test_manager_send_preserves_web_context_attachments_and_typed_correlation(tmp_path, state):
    async def run():
        async with _serving(tmp_path) as server:
            prepare(server, state)
            attachments = [{"name": "note.txt", "path": "note.txt", "mime_type": "text/plain", "size": 3}]
            status, _, answer = await post(server, "send", {
                "text": "hello", "session_id": "thread", "message_id": "message", "attachments": attachments})
            assert status == 200
            result = answer["result"]
            api_v1.validate_arguments(api_v1.find("POST", PREFIX + "send").result, result)
            assert set(result) == {"correlation_id"} and result["correlation_id"]
            (envelope,) = state.published
            assert envelope.source == channel_address("web", "channel-web") and envelope.target == MANAGER_ADDRESS
            assert envelope.payload["attachments"] == attachments
            assert envelope.payload["channel_context"] == {"session_id": "thread", "goal_id": None,
                                                            "client_message_id": "message"}
            chat.save_message.assert_awaited_once_with(session_id="thread", message_id="message", role="user", content="hello")
            assert (await _send(server, "POST /api/chat/send", _bearer(TOKEN), '{"text":"hello"}'))[0] == 404
    asyncio.run(run())


@pytest.mark.parametrize("action,body", [("send", {"text": "hello", "session_id": "thread"}),
                                         ("thread_events", {"session_id": "thread"})])
def test_foreign_manager_thread_reports_its_owner(tmp_path, state, monkeypatch, action, body):
    monkeypatch.setattr(chat, "thread_owner", AsyncMock(return_value="other"))
    async def run():
        async with _serving(tmp_path) as server:
            prepare(server, state)
            server._managers = SimpleNamespace(get=lambda name: SimpleNamespace(name="Other") if name == "other" else None)
            status, _, answer = await post(server, action, body)
            assert status == 409 and answer["error"]["details"] == {"manager_id": "other"}
            assert state.published == []
    asyncio.run(run())


def test_reply_events_keep_producer_payloads_and_retire_cid_route(tmp_path, state):
    async def run():
        async with _serving(tmp_path, listen=True) as server:
            adapter = prepare(server, state)
            _, _, sent = await post(server, "send", {"text": "hello"})
            cid = sent["result"]["correlation_id"]
            expected = []
            for message_type, payload in [(MessageType.STATUS_UPDATE, {"delta": True, "text": "par"}),
                    (MessageType.STATUS_UPDATE, {"text": "Reading", "activity": "tool", "tool": "Read"}),
                    (MessageType.WORK_PROGRESS, {"step": 1}), (MessageType.AGENT_REPLY, {"text": "done", "outcome": "answered"})]:
                event = adapter._envelope_to_sse_event(MessageEnvelope.create(source=MANAGER_ADDRESS,
                    target=channel_address("web"), message_type=message_type, payload=payload))
                expected.append((event["kind"], event["payload"]))
            expected.insert(-1, ("heartbeat", {}))
            for kind, payload in expected:
                adapter._sse_queues[cid].put_nowait({"kind": kind, "payload": payload})
            status, headers, body = await _send_raw(server, "POST " + PREFIX + "reply_events", _bearer(TOKEN),
                                                   json.dumps({"correlation_id": cid}))
            assert status == 200 and headers["content-type"] == "text/event-stream"
            assert parse_events(body) == expected and cid not in adapter._sse_queues
            schema = api_v1.document()["paths"]["/operator_chat/reply_events"]["post"]["responses"]["200"]["content"]["text/event-stream"]["schema"]
            assert schema["type"] == "string" and all(kind in schema["x-looper-events"] for kind, _ in expected)
            for kind, payload in expected:
                name = schema["x-looper-events"][kind]["data"]["$ref"].rsplit("/", 1)[-1]
                spec = api_v1.document()["components"]["schemas"][name]
                api_v1.validate_arguments(spec, {key: value for key, value in payload.items() if key in spec["properties"]})
                assert kind != "system.event" or spec["additionalProperties"] is True
            assert (await _send_raw(server, "GET /api/chat/events?cid=" + cid, _bearer(TOKEN)))[0] == 404
            assert (await asyncio.wait_for(_send_raw(server, "POST " + PREFIX + "reply_events", _bearer(TOKEN),
                                                       '{"correlation_id":"unknown"}'), 1))[0] == 200
    asyncio.run(run())


@pytest.mark.parametrize("action,body", [("reply_events", {"correlation_id": "reply"}),
                                         ("thread_events", {"session_id": "thread"})])
def test_disconnect_closes_idle_subscription_even_without_a_write(tmp_path, state, action, body):
    async def run():
        async with _serving(tmp_path, listen=True) as server:
            adapter = prepare(server, state)
            adapter._sse_queues["reply"] = asyncio.Queue()
            reader, writer = await asyncio.open_connection("127.0.0.1", server._port)
            writer.write(_request(server._port, "POST " + PREFIX + action, _bearer(TOKEN), json.dumps(body)).encode())
            await writer.drain()
            headers = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 1)
            assert b"200 OK" in headers
            writer.close()
            await writer.wait_closed()
            for _ in range(100):
                closed = "reply" not in adapter._sse_queues if action == "reply_events" else not adapter._chats._subs
                if closed:
                    break
                await asyncio.sleep(0.01)
            assert closed
    asyncio.run(run())


def test_autonomous_thread_message_and_session_end_close_stream_and_retire_session_route(tmp_path, state):
    async def run():
        async with _serving(tmp_path, listen=True) as server:
            adapter = prepare(server, state)
            reader, writer = await asyncio.open_connection("127.0.0.1", server._port)
            try:
                writer.write(_request(server._port, "POST " + PREFIX + "thread_events", _bearer(TOKEN),
                                      '{"session_id":"thread"}').encode())
                await writer.drain()
                assert b"200 OK" in await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 1)
                envelope = MessageEnvelope.create(source=MANAGER_ADDRESS, target=channel_address("web"),
                    message_type=MessageType.AGENT_REPLY, payload={"text": "Autonomous reply", "chat_session_id": "thread"})
                assert (await adapter.deliver(envelope)).success
                wire = (await asyncio.wait_for(reader.readuntil(b"\n\n"), 1)).decode()
                assert parse_events(wire) == [("chat.message", {"message_id": envelope.message_id, "role": "assistant",
                    "content": "Autonomous reply", "created_at": envelope.timestamp})]
                adapter._chats.close_task("thread")
                assert await asyncio.wait_for(reader.read(), 1) == b""
                assert (await _send_raw(server, "GET /api/chat/events?session=thread&manager=manager", _bearer(TOKEN)))[0] == 404
            finally:
                writer.close()
                await writer.wait_closed()
    asyncio.run(run())


def test_generated_cli_send_issues_the_declared_json_request(tmp_path, capsys):
    server = Server(lambda handler, request: cli_send(handler, 200, api_v1.document()
        if request[1].endswith("openapi.json") else {"status": "ok", "result": {"correlation_id": "reply"}}))
    try:
        client = tmp_path / "client.json"
        write_client_file(client, server.url)
        assert loopercli.main(["--client-file", str(client), "--json", "operator-chat", "send", "--text", "hello",
                              "--session-id", "thread", "--target", '{"kind":"manager"}']) == 0
        method, path, _, body = server.requests[-1]
        assert (method, path, body) == ("POST", PREFIX + "send", {"text": "hello", "session_id": "thread", "target": {"kind": "manager"}})
    finally:
        server.close()
