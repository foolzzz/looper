"""Cost scopes and time bounds read back through authenticated HTTP and disposable PostgreSQL."""

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest

from looper.agents import api_v1, overview
from looper.agents.bus import MessageBus
from looper.agents.ops import OpError
from looper.agents.ops.clients import ClientOps
from looper.agents.ops.service import Platform
from looper.agents.project_execution import ProjectExecution
from looper.agents.tiers.planner import PlannerAgent
from looper.db import api_clients, llm_records
from tests.db_support import DSN, run_db
from tests.test_api_v1_auth import _bearer, _send, _serving
from tests.test_operator_auth import TOKEN

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
PATH = "/api/v1/ops/costs"


def _validate(result):
    schema = api_v1.find("POST", PATH).result
    # The request validator closes its root; keep the oneOf result inside an envelope.
    def check(spec):
        api_v1.validate_arguments({"type": "object", "properties": {"result": spec}}, {"result": result})
    check(schema)
    matches = 0
    for variant in schema["oneOf"]:
        try:
            check(variant)
            matches += 1
        except OpError:
            pass
    assert matches == 1


@asynccontextmanager
async def _api(tmp_path, pool, *, client=True):
    _, token = await api_clients.create("Cost reader", pool=pool)
    async with _serving(tmp_path, listen=True) as server:
        server._client_ops = ClientOps(Platform(bus=MessageBus(runtime_root=tmp_path),
                                               task_router=server._task_router, runtime_root=tmp_path))
        async def call(body):
            status, _, answer = await _send(server, f"POST {PATH}", _bearer(token if client else TOKEN),
                                            json.dumps(body))
            if status == 200:
                assert answer["status"] == "ok"
                _validate(answer["result"])
                return status, answer["result"]
            return status, answer
        yield call


async def _record(pool, agent, task, cost, *, auth="oauth", model=None, status="completed",
                  at="2026-06-17T12:00:00Z"):
    await pool.execute("""INSERT INTO llm_records
        (agent_id, task_id, cost_usd, model, status, metadata, cost_source, created_at,
         input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, 1, 2, 3, 4)""",
        agent, task, cost, model, status, json.dumps({"profile": {"provider": {"auth": auth}}}),
        "provider_price" if auth == "api_key" else "runtime", datetime.fromisoformat(at))


async def _seed(pool):
    # Exercise the nullable project projection with a historical unassigned task.
    await pool.execute("ALTER TABLE tasks ALTER COLUMN project_id DROP NOT NULL")
    await pool.execute("""INSERT INTO projects (id, name) VALUES ('proj-example', 'Example'), ('proj-other', 'Other');
        INSERT INTO tasks (id, title, requirement, project_id, status) VALUES
        ('task-example', 'Example task', 'Read reports', 'proj-example', 'completed'),
        ('task-other', 'Other task', 'Read reports', 'proj-other', 'completed'),
        ('task-unassigned', 'Unassigned task', 'Read reports', NULL, 'completed');""")
    await _record(pool, "planner-example", "task-example", 0.25, model="plan-model")
    await _record(pool, "worker-example", "task-example", 1.5, auth="api_key", model="work-model")
    await _record(pool, "worker-running", "task-example", None, model="work-model", status="running")
    await _record(pool, "reviewer-example", "task-example", None, status="interrupted")
    await _record(pool, "manager", "task-example", 5, model="manager-model")
    await _record(pool, "worker-unassigned", "task-unassigned", 0.5)
    await _record(pool, "worker-other", "task-other", 7, auth="api_key")
    await _record(pool, "manager", None, 9, auth="api_key")


@pytest.mark.parametrize("client", [True, False])
def test_task_cost_matches_db_and_keeps_manager_out_of_cap_spend(tmp_path, monkeypatch, client):
    async def run(pool):
        await _seed(pool)
        async with _api(tmp_path, pool, client=client) as call:
            status, result = await call({"task_id": "task-example"})
            assert status == 200 and result["name"] == "Example task" and result["status"] == "completed"
            cost = result["cost"]
            assert cost == (await llm_records.task_costs(["task-example"], pool=pool))["task-example"]
            assert cost["cost_usd"] == 1.75 and cost["runs"] == 4
            assert cost["running"] == cost["unpriced_runs"] == 1
            assert cost["equivalent_runs"] == cost["provider_price_runs"] == 1
            assert cost["by_role"] == {"planner": {"cost_usd": 0.25, "runs": 1},
                "worker": {"cost_usd": 1.5, "runs": 2}, "reviewer": {"cost_usd": 0, "runs": 1}}
            assert sum(row["cost_usd"] or 0 for row in cost["by_model"]) == cost["cost_usd"]
            assert cost["manager_by_model"][0]["cost_usd"] == 5
            planner = PlannerAgent(task_id="task-example", runtime_root=tmp_path)
            planner._execution = ProjectExecution(task_budget_usd=2)
            assert await planner._cap_reached() is None
            planner._execution = ProjectExecution(task_budget_usd=1.75)
            assert await planner._cap_reached() == 1.75
            assert cost["by_model"][-1] == {"model": None, "cost_usd": None, "runs": 1, "running": 0,
                "unpriced_runs": 1, "interrupted_runs": 1, "input_tokens": 1, "output_tokens": 2,
                "cache_read_tokens": 3, "cache_creation_tokens": 4}
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("change", ["extra_provider", "missing_counter", "invalid_cost"])
def test_task_model_contract_rejects_fields_or_values_not_emitted(tmp_path, monkeypatch, change):
    async def run(pool):
        await _seed(pool)
        async with _api(tmp_path, pool) as call:
            _, result = await call({"task_id": "task-example"})
            model = result["cost"]["by_model"][0]
            if change == "extra_provider":
                model["provider"] = "Example"
            elif change == "missing_counter":
                del model["interrupted_runs"]
            else:
                model["cost_usd"] = "unknown"
            with pytest.raises(OpError):
                _validate(result)
    run_db(run, monkeypatch, timeout=30)


def test_project_breakdown_is_limited_and_reports_more_tasks(tmp_path, monkeypatch):
    async def run(pool):
        await _seed(pool)
        for i in range(10):
            task = f"task-extra-{i}"
            await pool.execute("INSERT INTO tasks (id, title, requirement, project_id) VALUES ($1, $1, 'Read', 'proj-example')", task)
            await _record(pool, f"worker-extra-{i}", task, 0.01)
        async with _api(tmp_path, pool) as call:
            status, result = await call({"project": "example"})
            assert status == 200 and result["project"] == "Example"
            assert result["since"] is result["until"] is None
            assert result["total"] == await llm_records.spend_since(None, project_id="proj-example", pool=pool)
            assert result["total"]["provider_priced_usd"] == 1.5
            assert result["total"]["equivalent_usd"] == pytest.approx(0.35)
            assert result["more_tasks"] == 1 and len(result["tasks"]) == 10
            assert result["tasks"][0] == {"task_id": "task-example", "name": "Example task",
                "provider_priced_usd": 1.5, "equivalent_usd": 0.25, "runs": 4}
    run_db(run, monkeypatch, timeout=30)


def test_global_aggregates_include_unassigned_tasks_and_manager_totals(tmp_path, monkeypatch):
    async def run(pool):
        await _seed(pool)
        async with _api(tmp_path, pool) as call:
            status, result = await call({})
            assert status == 200 and result["since"] is result["until"] is None
            assert result["total"] == await llm_records.spend_since(None, pool=pool) == {
                "provider_priced_usd": 17.5, "equivalent_usd": 5.75, "runs": 8, "running": 1, "unpriced_runs": 1}
            assert result["projects"] == [
                {"project": "Other", "provider_priced_usd": 7, "equivalent_usd": 0, "runs": 1, "tasks": 1},
                {"project": "Example", "provider_priced_usd": 1.5, "equivalent_usd": 0.25, "runs": 4, "tasks": 1},
                {"project": None, "provider_priced_usd": 0, "equivalent_usd": 0.5, "runs": 1, "tasks": 1}]
            assert result["total"]["runs"] - sum(p["runs"] for p in result["projects"]) == 2
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("project", [False, True])
@pytest.mark.parametrize("bounds,provider,runs", [
    ({"period": "today"}, 117.5, 9),
    ({"period": "this_week"}, 117.5, 9),
    ({"period": "this_month"}, 117.5, 9),
    ({"since": "2026-06-17"}, 117.5, 9),
    ({"until": "2026-06-17"}, 217.5, 9),
    ({"since": "2026-06-17", "until": "2026-06-17"}, 17.5, 8),
    ({"since": "2026-06-17T05:00:00-07:00", "until": "2026-06-17T12:00:01Z"}, 17.5, 8),
])
def test_time_filters_keep_nonempty_results_and_timestamp_serialization(tmp_path, monkeypatch, project, bounds, provider, runs):
    monkeypatch.setattr(overview, "local_midnight", lambda: datetime(2026, 6, 17, tzinfo=timezone.utc))
    async def run(pool):
        await _seed(pool)
        await _record(pool, "manager", None, 200, auth="api_key", at="2026-05-01T12:00:00Z")
        await _record(pool, "manager", None, 100, auth="api_key", at="2026-07-01T12:00:00Z")
        async with _api(tmp_path, pool) as call:
            status, result = await call({**bounds, **({"project": "Example"} if project else {})})
            assert status == 200 and result["total"]["provider_priced_usd"] == (1.5 if project else provider)
            assert result["total"]["runs"] == (4 if project else runs)
            assert result["tasks"] if project else result["projects"]
            if "period" in bounds:
                day = {"today": 17, "this_week": 15, "this_month": 1}[bounds["period"]]
                assert result["since"] == f"2026-06-{day:02}T00:00:00+00:00" and result["until"] is None
            else:
                for key in ("since", "until"):
                    if key not in bounds:
                        assert result[key] is None
                    elif "T" in bounds[key]:
                        assert result[key] == datetime.fromisoformat(bounds[key]).isoformat()
                    else:
                        day = 18 if key == "until" else 17
                        assert result[key] == datetime(2026, 6, day).astimezone().isoformat()
    run_db(run, monkeypatch, timeout=30)


def test_null_optional_filters_keep_existing_all_time_input_handling(tmp_path, monkeypatch):
    async def run(pool):
        await _seed(pool)
        async with _api(tmp_path, pool) as call:
            status, result = await call(dict.fromkeys(("task_id", "project", "period", "since", "until")))
            assert status == 200 and result == (await call({}))[1]
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("body,message", [
    *[({"task_id": "task-example", key: value}, "give task_id alone") for key, value in (
        ("project", "Example"), ("period", "today"), ("since", "2026-06-17"), ("until", "2026-06-17"))],
    *[({"period": "today", key: "2026-06-17"}, "not both") for key in ("since", "until")],
    ({"since": "bad"}, "since must be a day"), ({"until": "bad"}, "until must be a day"),
    ({"since": "2026-06-18", "until": "2026-06-17"}, "until must be after since"),
    ({"since": "2026-06-17T12:00:00Z", "until": "2026-06-17T12:00:00Z"}, "until must be after since"),
    ({"period": "yesterday"}, None), ({"since": 3}, None), ({"task_id": 3}, None), ({"extra": True}, None),
])
def test_invalid_combinations_retain_public_errors(tmp_path, monkeypatch, body, message):
    async def run(pool):
        await _seed(pool)
        async with _api(tmp_path, pool) as call:
            assert (await call({}))[0] == 200
            status, answer = await call(body)
            assert status == 400 and answer["error"]["code"] == "invalid"
            if message:
                assert message in answer["error"]["message"]
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("body", [{"task_id": "task-missing"}, {"project": "Missing"}])
def test_unknown_scope_retains_not_found(tmp_path, monkeypatch, body):
    async def run(pool):
        async with _api(tmp_path, pool) as call:
            status, answer = await call(body)
            assert status == 404 and answer["error"]["code"] == "not_found"
    run_db(run, monkeypatch, timeout=30)
