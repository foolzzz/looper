"""Catalog completeness and authentication boundaries of the public dashboard API."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper import loopercli
from looper.agents import api_v1
from looper.agents.ops.clients import CLIENT_TOOLS
from tests.test_api_v1_auth import CLIENT_TOKEN, _bearer, _send, _serving, clients as clients


@pytest.mark.parametrize("name,fields,branches", [
    ("costs", {"cost", "total", "projects", "tasks"}, 3),
    ("catalog.list", {"items", "project", "installs"}, 2),
    ("settings.view", {"scheduler", "roles", "project_layer"}, 2),
    ("concurrency", {"changes", "task_id", "note"}, 1),
    ("tasks.room", {"seats", "messages", "counts", "more"}, 1),
])
def test_formerly_untyped_polymorphic_results_keep_their_projections(name, fields, branches):
    schema = next(op.result for op in api_v1.operations() if op.operation_id == name)
    variants = schema.get("oneOf", [schema])
    assert len(variants) == branches
    assert fields <= {key for variant in variants for key in variant["properties"]}


def test_confirmation_describes_exactly_registered_confirmable_results():
    operations = api_v1.operations()
    result = next(op.result for op in operations if op.operation_id == "confirm")
    assert "oneOf" not in result
    assert result["anyOf"] == [op.result for op in operations if op.confirms]
    assert result not in result["anyOf"]


def test_missing_tool_schema_is_a_catalog_error(monkeypatch):
    monkeypatch.delitem(api_v1.RESULTS, "overview")
    with pytest.raises(KeyError, match="overview"):
        api_v1.operations()


@pytest.mark.parametrize("schema", [{}, {"type": "object"}, {"type": "object", "x-looper-result-typed": False}])
def test_missing_domain_schema_is_a_catalog_error(monkeypatch, schema):
    domain = api_v1.domains()[0]
    monkeypatch.setattr(domain, "OPERATIONS", (replace(domain.OPERATIONS[0], result=schema),))
    with pytest.raises(ValueError, match="missing concrete result schema"):
        api_v1.operations()


@pytest.mark.parametrize("operation", [op for op in api_v1.operations() if op.operator_only],
                         ids=lambda op: op.operation_id)
def test_every_operator_operation_refuses_clients_before_validation_or_effects(tmp_path, monkeypatch, clients, operation):
    async def run():
        async with _serving(tmp_path) as server:
            effects = AsyncMock(side_effect=AssertionError("forbidden request reached a service or audit"))
            server._client_ops = SimpleNamespace(call=effects, confirm=effects)
            server._v1_online = effects
            for domain in api_v1.domains():
                for callback in ("run", "stream", "receive"):
                    if hasattr(domain, callback):
                        monkeypatch.setattr(domain, callback, effects)
            status, _, answer = await _send(server, f"{operation.method} {operation.path}",
                                            _bearer(CLIENT_TOKEN), "not-json")
            assert status == 403 and answer["error"]["code"] == "forbidden"
            effects.assert_not_awaited()
    asyncio.run(run())


@pytest.mark.parametrize("operation", [op for op in api_v1.operations() if op.stream],
                         ids=lambda op: op.operation_id)
def test_stream_discovery_preserves_event_payloads_and_uses_stream_transport(operation):
    item = api_v1.document()["paths"][operation.path.removeprefix(api_v1.PREFIX)][operation.method.lower()]
    content = item["responses"]["200"]["content"]
    assert set(content) == {"text/event-stream"}
    assert content["text/event-stream"]["schema"]["x-looper-events"]
    if not operation.hand:
        generated = next(op for *_, op in loopercli.generated_operations(api_v1.document()) if op.path == operation.path)
        assert generated.response_media_type == "text/event-stream"


@pytest.mark.parametrize("method,path", [("GET", "/api/agent/v1/tools"),
    ("POST", "/api/agent/v1/ops/looper_task_profile_set"),
    ("POST", "/api/agent/v1/ops/looper_room_send")])
def test_client_cannot_authenticate_to_private_agent_protocol(tmp_path, clients, method, path):
    async def run():
        async with _serving(tmp_path) as server:
            assert (await _send(server, "GET /api/v1/whoami", _bearer(CLIENT_TOKEN)))[0] == 200
            assert (await _send(server, f"{method} {path}", _bearer(CLIENT_TOKEN), "{}"))[0] == 401
    asyncio.run(run())
    assert not {"looper_task_profile_set", "looper_room_send"} & {tool.name for tool in CLIENT_TOOLS}


@pytest.mark.parametrize("kind", ["planner", "run"])
def test_private_grants_are_valid_only_on_the_agent_transport(tmp_path, monkeypatch, clients, kind):
    from looper.agents.ops import planner
    from looper.agents.ops.planner import PlannerOps
    from looper.agents.ops.room import RoomOps
    from looper.agents.room import Seat

    monkeypatch.setattr(planner.db_tasks, "get_task", AsyncMock(return_value=None))
    async def run():
        async with _serving(tmp_path) as server:
            planners = server._planner_ops = PlannerOps(SimpleNamespace(), url="http://127.0.0.1")
            rooms = server._room_ops = RoomOps(SimpleNamespace(), url="http://127.0.0.1")
            token = planners.issue("task-example") if kind == "planner" else rooms.issue("task-example", Seat.planner())
            assert (await _send(server, "GET /api/agent/v1/tools", _bearer(token)))[0] == 200
            assert (await _send(server, "GET /api/v1/whoami", _bearer(token)))[0] == 401
    asyncio.run(run())
