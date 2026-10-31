"""Full dashboard projections and generated CLI commands share the v1 operation catalog."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from looper import loopercli
from looper.agents import api_v1, overview
from looper.agents.ops.clients import ClientOps
from looper.agents.ops.service import Platform
from looper.agents.ops import tasks as task_ops
from tests.test_api_v1_auth import CLIENT_TOKEN, _bearer, _send, _serving
from tests.test_api_v1_auth import clients as clients
from tests.test_operator_auth import TOKEN


@pytest.mark.usefixtures("clients")
@pytest.mark.parametrize("token", [TOKEN, CLIENT_TOKEN])
def test_overview_keeps_running_waiting_spend_health_and_fleet(tmp_path, monkeypatch, token):
    async def counts(**kwargs):
        return [{"project_id": "project-a", "project_name": "Demo", "status": "waiting_user", "count": 1}]

    async def spend(*args, **kwargs):
        return {"provider_priced_usd": 1.25, "equivalent_usd": 2.5, "runs": 3, "running": 1,
                "unpriced_runs": 1}

    async def schedules(*args):
        return {**overview.empty_schedule_overview(), "total": 1, "enabled": 1,
                "next": [{"schedule_id": "schedule-a", "name": "Daily", "project_id": "project-a",
                          "project_name": "Demo", "timezone": "UTC", "next_fire_at": now}],
                "failing": [{"schedule_id": "schedule-a", "name": "Daily", "project_id": "project-a",
                             "project_name": "Demo", "timezone": "UTC", "consecutive_failures": 1,
                             "failures_since": now, "stale_since": None, "last_success_at": None,
                             "deadline_since": now, "overlap_since": None,
                             "last_status": "failed"}]}

    async def concurrency(*args):
        return {"task-a": {"worker": 2}}

    async def gates(*args, **kwargs):
        return [{"task_id": "task-a"}]

    monkeypatch.setattr(overview.db_tasks, "status_counts", counts)
    monkeypatch.setattr(overview.db_llm_records, "spend_since", spend)
    monkeypatch.setattr(overview, "schedule_overview", schedules)
    monkeypatch.setattr(task_ops.db_layers, "task_concurrency", concurrency)
    monkeypatch.setattr(task_ops.db_user_gates, "open_gates", gates)
    now = datetime.now(timezone.utc)
    run = {"agent_id": "worker-a", "task_id": "task-a", "project_id": "project-a", "role": "worker",
           "step": "build", "instruction": "Build demo", "state": "running", "since": now - timedelta(seconds=10)}
    planner = {"planner_id": "planner-task-a", "task_id": "task-a", "project_id": "project-a",
               "requirement": "Build demo", "name": "Demo"}
    health = {"healthy": False, "state": "crash_looping", "up": False, "restart_count": 4,
              "last_crash_at": now.isoformat(), "next_restart_at": None}
    manager = SimpleNamespace(agent_id="manager", name="Manager", busy=True)

    async def check():
        async with _serving(tmp_path) as server:
            server._task_router = SimpleNamespace(running_planners=lambda: [planner])
            server._worker_pool = SimpleNamespace(runs=lambda: [run, {**run, "agent_id": "reviewer-a",
                                                                       "role": "reviewer", "state": "waiting"}])
            server._manager = manager
            server._managers = SimpleNamespace(agents=lambda: [manager], state=lambda _: {"running": True})
            server._supervisor = SimpleNamespace(agent_status=lambda: {"worker-pool": health})
            server._client_ops = ClientOps(Platform(bus=None, task_router=server._task_router,
                worker_pool=server._worker_pool, manager=manager, managers=server._managers,
                supervisor=server._supervisor))
            status, _, answer = await _send(server, "POST /api/v1/system/overview", _bearer(token), "{}")
            assert status == 200, answer
            result = answer["result"]
            operation = next(op for op in api_v1.operations() if op.operation_id == "system.overview")
            assert api_v1.validate_arguments(operation.result, result) == result
            assert result["tasks"]["needs_you"] == 1 and result["tasks"]["total"] == 1
            assert result["agents"]["planners"][0]["name"] == "Demo"
            assert result["agents"]["workers"][0]["elapsed_seconds"] >= 10
            assert result["agents"]["reviewers"][0]["state"] == "waiting"
            assert result["agents"]["manager"] == {"agent_id": "manager", "state": "busy"}
            assert result["agents"]["managers"][0]["name"] == "Manager"
            assert result["spend_today"]["provider_priced_usd"] == 1.25
            assert result["spend_today"]["equivalent_usd"] == 2.5
            assert result["health"]["unhealthy"][0]["restart_count"] == 4
            assert result["projects"][0]["reviewers"]["waiting"] == 1
            assert result["schedules"]["next"][0]["next_fire_at"] == now.isoformat()
            assert result["schedules"]["failing"][0]["failures_since"] == now.isoformat()
            assert result["schedules"]["failing"][0]["deadline_since"] == now.isoformat()
            assert result["schedules"]["failing"][0]["overlap_since"] is None
            status, _, compact = await _send(server, "POST /api/v1/ops/overview", _bearer(token), "{}")
            assert status == 200 and compact["result"]["waiting_gates"] == 1
            assert compact["result"]["running_tasks"][0]["concurrency"] == {"worker": 2}
            assert set(compact["result"]) == {"tasks", "running_tasks", "step_runs", "projects", "waiting_gates",
                                              "spend_today", "unhealthy"}
            if token == TOKEN:
                assert (await _send(server, "GET /api/overview", _bearer(token)))[0] == 404

    asyncio.run(check())


@pytest.mark.parametrize(("arguments", "path", "body"), [
    (["system", "overview"], "/api/v1/system/overview", {}),
    (["tasks", "requirement-update", "--task-id", "task-a", "--requirement", "Build demo"],
     "/api/v1/tasks/requirement_update", {"task_id": "task-a", "requirement": "Build demo"}),
])
def test_generated_cli_builds_new_ordinary_requests(arguments, path, body):
    args = loopercli.build_parser(api_v1.document()).parse_args(arguments)
    assert args._generated.path == path and args._generated.method == "POST"
    assert loopercli.request_body(args, args._generated) == body
    operation = next(op for op in api_v1.operations() if op.path == path)
    assert not operation.hand and operation.request["additionalProperties"] is False


def test_overview_database_failure_is_typed_unavailable(tmp_path, monkeypatch):
    async def unavailable(**kwargs):
        raise ConnectionError("temporarily unavailable")
    monkeypatch.setattr(overview.db_tasks, "status_counts", unavailable)
    async def check():
        async with _serving(tmp_path) as server:
            server._client_ops = ClientOps(Platform(bus=None, task_router=server._task_router))
            status, _, answer = await _send(server, "POST /api/v1/system/overview", _bearer(TOKEN), "{}")
            assert status == 503 and answer["error"]["code"] == "unavailable"
            status, _, answer = await _send(server, "POST /api/v1/system/overview", _bearer(TOKEN), '{"extra":true}')
            assert status == 400 and answer["error"]["code"] == "invalid"
    asyncio.run(check())
