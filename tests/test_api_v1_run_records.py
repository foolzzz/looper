"""Run reads: authenticated dispatch, typed failures, retirement and generated CLI requests."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from looper import loopercli
from looper.agents import api_v1
from looper.db import llm_records
from tests.test_api_v1_auth import CLIENT_TOKEN, _bearer, _send, _serving, clients
from tests.test_loopercli import Server, _send as cli_send
from tests.test_operator_auth import TOKEN

READS = [
    ("for_task", {"task_id": "task-example"}, "task_run_rows", "/api/tasks/task-example/runs"),
    ("list", {}, "list_records", "/api/llm-records"),
    ("get", {"record_id": 1}, "get_record", "/api/llm-records/1"),
    ("events", {"record_id": 1}, "list_events", "/api/llm-records/1/events"),
    ("usage", {"agent_id": "worker-example"}, "agent_usage", "/api/agents/worker-example/usage"),
]


async def post(server, action, body, token=TOKEN):
    return await _send(server, f"POST /api/v1/runs/{action}", _bearer(token), json.dumps(body))


@pytest.mark.parametrize("action,body", [
    ("for_task", {}), ("for_task", {"task_id": 1}), ("get", {"record_id": 0}),
    ("get", {"record_id": "1"}), ("get", {"record_id": True}),
    ("list", {"limit": 0}), ("list", {"limit": 501}), ("list", {"offset": -1}),
    ("list", {"offset": None}), ("list", {"role": 4}), ("list", {"extra": 1}),
    ("events", {"record_id": 1, "after": -1}), ("events", {"record_id": 1, "limit": 0}),
    ("events", {"record_id": 1, "limit": 5001}),
    ("usage", {"agent_id": "manager", "turns": 0}), ("usage", {"agent_id": "manager", "turns": 1001}),
])
def test_invalid_request_is_typed_before_any_database_read(tmp_path, monkeypatch, action, body):
    for _, _, reader, _ in READS:
        monkeypatch.setattr(llm_records, reader, AsyncMock(side_effect=AssertionError("must not read")))
    async def run():
        async with _serving(tmp_path) as server:
            status, _, answer = await post(server, action, body)
            assert status == 400 and answer["error"]["code"] == "invalid"
    asyncio.run(run())


@pytest.mark.parametrize("action,body,reader,legacy", READS)
@pytest.mark.parametrize("failure", [RuntimeError, ValueError])
def test_database_failure_is_typed_unavailable(tmp_path, monkeypatch, action, body, reader, legacy, failure):
    monkeypatch.setattr(llm_records, reader, AsyncMock(side_effect=failure("private database error")))
    async def run():
        async with _serving(tmp_path) as server:
            status, _, answer = await post(server, action, body)
            assert status == 503 and answer["error"]["code"] == "unavailable"
            assert "private database error" not in json.dumps(answer)
    asyncio.run(run())


def test_missing_record_is_typed_not_found(tmp_path, monkeypatch):
    monkeypatch.setattr(llm_records, "get_record", AsyncMock(return_value=None))
    async def run():
        async with _serving(tmp_path) as server:
            status, _, answer = await post(server, "get", {"record_id": 1})
            assert status == 404 and answer["error"]["code"] == "not_found"
    asyncio.run(run())


@pytest.mark.parametrize("action,body,reader,legacy", READS)
def test_run_reads_refuse_authenticated_clients_before_validation_or_database_access(
        tmp_path, monkeypatch, clients, action, body, reader, legacy):
    read = AsyncMock(side_effect=AssertionError("must not read"))
    monkeypatch.setattr(llm_records, reader, read)
    async def run():
        async with _serving(tmp_path, listen=True) as server:
            status, _, answer = await _send(server, "GET /api/v1/whoami", _bearer(CLIENT_TOKEN))
            assert status == 200 and answer["result"]["principal"] == "client"
            for payload in (json.dumps(body), "not json"):
                status, _, answer = await _send(server, f"POST /api/v1/runs/{action}",
                                                _bearer(CLIENT_TOKEN), payload)
                assert status == 403 and answer["status"] == "error" and answer["error"]["code"] == "forbidden"
            read.assert_not_awaited()
    asyncio.run(run())


@pytest.mark.parametrize("action,body,reader,legacy", READS)
def test_retired_get_is_404_with_authenticated_v1_positive_control(
        tmp_path, monkeypatch, clients, action, body, reader, legacy):
    record = {"id": 1, "metadata": None, "status": "interrupted"}
    value = record if action == "get" else {"agent_id": "worker-example"} if action == "usage" else [record]
    monkeypatch.setattr(llm_records, reader, AsyncMock(return_value=value))
    async def run():
        async with _serving(tmp_path) as server:
            status, _, answer = await post(server, action, body)
            assert status == 200 and answer["status"] == "ok" and answer["result"]
            assert (await _send(server, f"GET {legacy}", _bearer(TOKEN)))[0] == 404
    asyncio.run(run())


@pytest.mark.parametrize("action,args,body", [
    ("for_task", ["--task-id", "task-example"], {"task_id": "task-example"}),
    ("list", ["--task-id", "task-example", "--agent-id", "worker-example", "--role", "worker",
              "--limit", "500", "--offset", "0"],
     {"task_id": "task-example", "agent_id": "worker-example", "role": "worker", "limit": 500, "offset": 0}),
    ("get", ["--record-id", "1"], {"record_id": 1}),
    ("events", ["--record-id", "1", "--after", "0", "--limit", "5000"], {"record_id": 1, "after": 0, "limit": 5000}),
    ("usage", ["--agent-id", "manager", "--turns", "1000"], {"agent_id": "manager", "turns": 1000}),
])
def test_generated_cli_sends_the_declared_ordinary_read(tmp_path, capsys, action, args, body):
    def respond(handler, request):
        cli_send(handler, 200, api_v1.document() if request[1].endswith("openapi.json")
                 else {"status": "ok", "result": {"received": True}})
    server = Server(respond)
    try:
        token = tmp_path / "operator-token"
        token.write_text(TOKEN + "\n")
        token.chmod(0o600)
        assert loopercli.main(["--client-file", str(tmp_path / "missing-client.json"), "--json", "runs",
                              action.replace("_", "-"), *args, "--url", server.url,
                              "--operator-token-file", str(token)]) == 0
        method, path, _, sent = server.requests[-1]
        assert (method, path, sent) == ("POST", f"/api/v1/runs/{action}", body)
        op = api_v1.find(method, path)
        assert not op.hand and op.tool is None and op.risk == "read" and op.operator_only
        assert api_v1.document()["paths"][f"/runs/{action}"]["post"]["x-looper-operator-only"] is True
        assert "202" not in api_v1.document()["paths"][f"/runs/{action}"]["post"]["responses"]
    finally:
        server.close()
