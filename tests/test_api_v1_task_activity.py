"""Task activity's v1 errors, authorization and stream contracts."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from looper.agents import api_v1
from looper.db import activity, tasks
from tests.db_support import until
from tests.test_api_v1_auth import CLIENT_TOKEN, _bearer, _send, _send_raw, _serving, clients
from tests.test_operator_auth import TOKEN
from tests.test_usage_api_db import _Writer


def validate_result(schema, result):
    def resolve(value):
        if isinstance(value, dict):
            if "$ref" in value:
                return resolve(api_v1.document()["components"]["schemas"][value["$ref"].split("/")[-1]])
            return {key: resolve(child) for key, child in value.items()}
        return [resolve(child) for child in value] if isinstance(value, list) else value
    return api_v1.validate_arguments(resolve(schema), result)


def event(seq=1, task_id="task-a"):
    return {"seq": seq, "task_id": task_id, "worker": None, "kind": "text", "summary": f"Event {seq}",
            "metadata": {"phase": "work"}, "input_tokens": None, "output_tokens": 2,
            "created_at": "2026-10-23T00:00:00+00:00"}


@pytest.fixture
def reads(monkeypatch):
    row = event()
    monkeypatch.setattr(tasks, "get_task", AsyncMock(return_value={"id": "task-a", "status": "completed"}))
    monkeypatch.setattr(activity, "list_activity", AsyncMock(
        side_effect=lambda task_id, *, since_seq=0, limit=100: [row] if since_seq < row["seq"] else []))
    monkeypatch.setattr(activity, "activity_window", AsyncMock(return_value={
        "rows": [row], "has_more": False, "oldest_seq": 1, "latest_seq": 1}))
    monkeypatch.setattr(activity, "conversation_window", AsyncMock(return_value={
        "rows": [{k: row[k] for k in ("seq", "worker", "kind", "summary", "created_at")}],
        "has_more": False, "oldest_seq": 1, "latest_seq": 1}))
    monkeypatch.setattr(activity, "get_activity_detail", AsyncMock(return_value={**row, "detail": "Details"}))


async def post(server, action, body=None, token=TOKEN):
    return await _send(server, f"POST /api/v1/tasks/{action}", _bearer(token),
                       json.dumps({"task_id": "task-a", **(body or {})}))


def test_stream_has_its_own_typed_event_contract():
    document = api_v1.document()
    response = document["paths"]["/tasks/activity_stream"]["post"]["responses"]["200"]
    assert "accepted" not in response["description"] and "ActivityEvent" in response["description"]
    events = response["content"]["text/event-stream"]["schema"]["x-looper-events"]
    assert events["activity"]["data"] == {"$ref": "#/components/schemas/ActivityEvent"}
    assert events["activity"]["id"]["type"] == "integer"
    validate_result(events["activity"]["data"], event())
    validate_result(events["done"]["data"], {})
    assert events["heartbeat"] == {"comment": "ping"}
    assert document["components"]["schemas"]["ActivityDone"]["additionalProperties"] is False


@pytest.mark.parametrize("action,body", [
    ("activity", {"after": -1}), ("activity", {"since": -1}), ("activity", {"before": 0}),
    ("activity", {"limit": True}), ("conversation", {"before": 0}), ("conversation", {"limit": None}),
    ("activity_detail", {"seq": 0}), ("activity_detail", {"seq": "1"}),
    ("activity_stream", {"since": -1}), ("activity", {"unknown": 1}), ("activity", {"task_id": 1}),
])
def test_invalid_bounds_are_typed_json_before_stream_headers(tmp_path, clients, reads, action, body):
    async def run():
        async with _serving(tmp_path) as server:
            status, headers, answer = await post(server, action, body)
            assert status == 400 and headers["content-type"].startswith("application/json")
            assert answer["status"] == "error" and answer["error"]["code"] == "invalid"
    asyncio.run(run())


@pytest.mark.parametrize("action,extra,method", [
    ("activity", {}, "activity_window"), ("conversation", {}, "conversation_window"),
    ("activity_detail", {"seq": 1}, "get_activity_detail"), ("activity_stream", {}, "list_activity"),
])
def test_database_failure_is_unavailable(tmp_path, monkeypatch, clients, reads, action, extra, method):
    monkeypatch.setattr(activity, method, AsyncMock(side_effect=RuntimeError("database unavailable")))
    async def run():
        async with _serving(tmp_path) as server:
            status, headers, body = await post(server, action, extra)
            assert status == 503 and headers["content-type"].startswith("application/json")
            assert body["error"]["code"] == "unavailable"
    asyncio.run(run())


@pytest.mark.parametrize("action,suffix,extra", [
    ("activity", "activity", {}), ("activity_detail", "activity/1", {"seq": 1}),
    ("conversation", "conversation", {}), ("activity_stream", "activity/stream", {}),
])
def test_retired_route_is_404_with_authenticated_v1_control(tmp_path, clients, reads, action, suffix, extra):
    async def run():
        async with _serving(tmp_path) as server:
            if action == "activity_stream":
                status, _, body = await _send_raw(server, "POST /api/v1/tasks/activity_stream", _bearer(TOKEN),
                                                 '{"task_id":"task-a"}')
                assert status == 200 and "event: activity" in body and "event: done" in body
            else:
                assert (await post(server, action, extra, CLIENT_TOKEN if action == "activity" else TOKEN))[0] == 200
            assert (await _send(server, f"GET /api/tasks/task-a/{suffix}", _bearer(TOKEN)))[0] == 404
    asyncio.run(run())


def test_stream_replay_live_done_has_ordered_schema_valid_payloads(tmp_path, monkeypatch, clients, reads):
    async def run():
        async with _serving(tmp_path) as server:
            bus = server._broadcaster
            monkeypatch.setattr(tasks, "get_task", AsyncMock(return_value={"id": "task-a", "status": "running"}))
            replayed, live = False, False
            async def replay(task_id, *, since_seq=0, limit=100):
                nonlocal replayed, live
                if since_seq == 1:
                    if not live:
                        live = True
                        return []
                    return [event(2)]
                if replayed or since_seq:
                    return []
                replayed = True
                bus.publish(task_id, event(1))
                bus.publish(task_id, {"kind": "text", "summary": "Live wakeup"})
                return [event(1)]
            monkeypatch.setattr(activity, "list_activity", replay)
            class Writer(_Writer):
                async def drain(self):
                    if b"id: 2\n" in self.data:
                        bus.close_task("task-a")
            writer = Writer()
            await asyncio.wait_for(server._handle_v1("POST", "/api/v1/tasks/activity_stream", {}, {},
                b'{"task_id":"task-a"}', writer, api_v1.OPERATOR), 2)
            payload = writer.data.split(b"\r\n\r\n", 1)[1].decode()
            assert payload.count("id: 1\n") == payload.count("id: 2\n") == 1
            assert payload.index("id: 1\n") < payload.index("id: 2\n") < payload.index("event: done")
            assert "data: {}" in payload and not bus._subs
            schema = api_v1.find("POST", "/api/v1/tasks/activity_detail").result
            for line in payload.splitlines():
                if line.startswith("data: ") and line != "data: {}":
                    validate_result(schema, json.loads(line[6:]))
    asyncio.run(run())


def test_reconnect_replays_only_after_the_last_delivered_sequence(tmp_path, monkeypatch, clients, reads):
    monkeypatch.setattr(activity, "list_activity", AsyncMock(
        side_effect=lambda task_id, *, since_seq=0, limit=100: [event(3)] if since_seq < 3 else []))
    async def run():
        async with _serving(tmp_path) as server:
            status, _, tail = await _send_raw(server, "POST /api/v1/tasks/activity_stream", _bearer(TOKEN),
                                             '{"task_id":"task-a","since":2}')
            assert status == 200 and tail.count("id: 3\n") == 1 and "id: 2\n" not in tail
            assert "event: done" in tail and activity.list_activity.await_args_list[0].kwargs["since_seq"] == 2
    asyncio.run(run())


def test_connection_teardown_unregisters_stream_listener(tmp_path, monkeypatch, clients, reads):
    monkeypatch.setattr(tasks, "get_task", AsyncMock(return_value={"id": "task-a", "status": "running"}))
    monkeypatch.setattr(activity, "list_activity", AsyncMock(return_value=[]))
    async def run():
        async with _serving(tmp_path, listen=True) as server:
            reader, writer = await asyncio.open_connection("127.0.0.1", server._port)
            body = b'{"task_id":"task-a"}'
            writer.write((f"POST /api/v1/tasks/activity_stream HTTP/1.1\r\nHost: 127.0.0.1:{server._port}\r\n"
                          f"Authorization: Bearer {TOKEN}\r\nContent-Length: {len(body)}\r\n\r\n").encode() + body)
            await writer.drain()
            try:
                assert b"text/event-stream" in await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2)
                assert server._broadcaster._subs
            finally:
                writer.close()
                await writer.wait_closed()
            await until(lambda: not server._broadcaster._subs, timeout=2)
    asyncio.run(run())
