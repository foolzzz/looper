"""Manager filters are client reads; the Manager's model-visible catalog stays unchanged."""

import asyncio
from copy import deepcopy
from contextlib import asynccontextmanager
from dataclasses import fields
import json

import pytest

from looper import loopercli
from looper.agents import api_v1
from looper.agents.ops.clients import CLIENT_TOOLS, ClientOps
from looper.agents.ops.service import MANAGER_TOOLS, Platform
from tests.db_support import run_db
from tests.test_api_v1_auth import CLIENT_TOKEN, _bearer, _send, _serving
from tests.test_api_v1_auth import clients as clients
from tests.test_loopercli import write_client_file
from tests.test_managers_schema_db import _manager
from tests.test_ops_tasks_db import _Chat, _project, _task


@asynccontextmanager
async def _client_server(tmp_path, **kwargs):
    async with _serving(tmp_path, **kwargs) as server:
        server._client_ops = ClientOps(Platform(bus=None, task_router=server._task_router))
        yield server


def test_discovery_extends_only_requests_and_keeps_result_shapes():
    document = api_v1.document()
    for path in ("/tasks/page", "/ops/tasks/list"):
        operation = document["paths"][path]["post"]
        request = operation["requestBody"]["content"]["application/json"]["schema"]
        assert request["properties"]["manager_id"] == {"type": "string", "minLength": 1}
        assert "manager_id" not in request.get("required", [])
    page = api_v1.find("POST", "/api/v1/tasks/page")
    compact = api_v1.find("POST", "/api/v1/ops/tasks/list")
    assert set(page.request["properties"]) == {
        "project_id", "schedule_id", "group", "status", "before", "limit", "query", "manager_id"}
    assert set(compact.request["properties"]) == {"task_id", "query", "group", "project", "limit", "manager_id"}
    assert set(page.result["properties"]) == {"tasks", "counts", "next_before"}
    assert set(compact.result["properties"]) == {"tasks", "more"}
    assert set(compact.result["properties"]["tasks"]["items"]["properties"]) == {
        "task_id", "name", "status", "group", "project", "created_at"}
    detail = api_v1.find("POST", "/api/v1/tasks/detail")
    assert set(detail.request["properties"]) == {"task_id"}


def test_client_clone_preserves_manager_schema_description_and_metadata(tmp_path):
    original = next(t for t in MANAGER_TOOLS if t.name == "looper_tasks")
    client = next(t for t in CLIENT_TOOLS if t.name == original.name)
    assert original is not client and original.schema is not client.schema
    schema = deepcopy(dict(client.schema))
    assert schema["properties"].pop("manager_id") == {"type": "string", "minLength": 1}
    assert schema == original.schema
    assert set(original.schema["properties"]) == {"action", "task_id", "query", "group", "project", "limit"}
    for field in fields(original):
        if field.name not in {"schema", "run"}:
            assert getattr(client, field.name) == getattr(original, field.name)
    listed = next(t for t in _Chat(tmp_path).ops.listing() if t["name"] == original.name)
    assert listed["inputSchema"] == original.schema
    assert listed["description"] == original.description


@pytest.mark.parametrize("action", ["get", "room"])
def test_non_list_actions_refuse_owner_without_changing_unfiltered_reads(tmp_path, monkeypatch, clients, action):
    async def run(pool):
        await _task(pool, "task-example", "init", await _project(pool))
        async with _client_server(tmp_path) as server:
            path = f"POST /api/v1/ops/tasks/{action}"
            arguments = {"task_id": "task-example"}
            status, _, answer = await _send(server, path, _bearer(CLIENT_TOKEN), json.dumps(arguments))
            assert status == 200 and answer["status"] == "ok", answer
            status, _, answer = await _send(server, path, _bearer(CLIENT_TOKEN),
                json.dumps({**arguments, "manager_id": "manager"}))
            assert status == 400 and answer["error"]["code"] == "invalid", answer
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("path", ["tasks/page", "ops/tasks/list"])
@pytest.mark.parametrize("owner", ["", None, 1])
def test_owner_input_is_a_nonempty_string(tmp_path, clients, path, owner):
    async def run():
        async with _client_server(tmp_path) as server:
            status, _, answer = await _send(server, f"POST /api/v1/{path}", _bearer(CLIENT_TOKEN),
                json.dumps({"manager_id": owner}))
            assert status == 400 and answer["error"]["code"] == "invalid", answer
    asyncio.run(run())


@pytest.mark.parametrize("command,path", [
    (["tasks", "list"], "/api/v1/ops/tasks/list"),
    (["tasks", "page"], "/api/v1/tasks/page"),
    (["list"], "/api/v1/ops/tasks/list"),
])
def test_generated_cli_forms_send_owner_to_real_client_adapter(
        tmp_path, monkeypatch, clients, capsys, command, path):
    async def run(pool):
        project = await _project(pool)
        await _manager(pool, "review", enabled=False)
        await _task(pool, "task-default", "init", project)
        await _task(pool, "task-review", "init", project)
        await pool.execute("UPDATE tasks SET manager_id='manager-review' WHERE id='task-review'")
        async with _client_server(tmp_path, listen=True) as server:
            sent = []
            answer_v1 = server._answer_v1

            async def record(method, target, operation, headers, body, principal):
                if method == "POST":
                    sent.append((target, json.loads(body)))
                return await answer_v1(method, target, operation, headers, body, principal)

            monkeypatch.setattr(server, "_answer_v1", record)
            client = write_client_file(tmp_path / "client.json", f"http://127.0.0.1:{server._port}", CLIENT_TOKEN)
            code = await asyncio.to_thread(loopercli.main, ["--client-file", str(client), "--json", *command,
                "--manager-id", "manager-review", "--limit", "1"])
            assert code == 0
            result = json.loads(capsys.readouterr().out)["result"]
            assert sent == [(path, {"manager_id": "manager-review", "limit": 1})]
            assert [t["task_id"] for t in result["tasks"]] == ["task-review"]
            if path.endswith("page"):
                assert result["counts"] == {"init": 1} and result["next_before"] is None
            else:
                assert result["more"] is False
            status, _, unfiltered = await _send(server, f"POST {path}", _bearer(CLIENT_TOKEN), "{}")
            assert status == 200 and len(unfiltered["result"]["tasks"]) == 2
    run_db(run, monkeypatch, timeout=30)
