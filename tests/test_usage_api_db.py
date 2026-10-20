"""The cost and context read-back API against a real PostgreSQL.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database. Each
test works in a new schema of its own, dropped afterwards.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import uuid
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

DSN = os.environ.get("LOOPER_TEST_DATABASE_URL")


def _run(test: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run ``test(pool)`` on a new schema that is also the server's pool."""
    if not DSN:
        pytest.skip("LOOPER_TEST_DATABASE_URL is not set")
    import asyncpg

    from looper.db import connection

    async def _main() -> None:
        schema = f"looper_test_{uuid.uuid4().hex[:12]}"
        admin = await asyncpg.connect(DSN)
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3, server_settings={"search_path": schema})
        try:
            await connection.ensure_schema(pool)
            monkeypatch.setattr(connection, "_pool", pool)
            await test(pool)
        finally:
            await pool.close()
            await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
            await admin.close()

    asyncio.run(_main())


class _Writer:
    def __init__(self) -> None:
        self.data = b""

    def write(self, data: bytes) -> None:
        self.data += data

    async def drain(self) -> None:
        pass


async def _get(path: str, query: dict[str, str] | None = None) -> tuple[int, Any]:
    from looper.agents.http_server import AgentHTTPServer
    from looper.agents.tiers.task import TaskRouter

    server = AgentHTTPServer(web_adapter=SimpleNamespace(), supervisor=SimpleNamespace(),
                             task_router=TaskRouter(runtime_root=Path(tempfile.mkdtemp())))
    writer = _Writer()
    await server._route_api("GET", path, query or {}, b"", writer)
    head, _, payload = writer.data.partition(b"\r\n\r\n")
    return int(head.split()[1]), json.loads(payload)


async def _run_record(pool: Any, agent_id: str, *, task_id: str | None = None, cost: float | None = None,
                      session_id: str | None = None, context: int | None = None,
                      compactions: list[dict[str, Any]] | None = None) -> int:
    from looper.db import llm_records

    record_id = await llm_records.start_record(agent_id=agent_id, task_id=task_id, session_id=session_id, pool=pool)
    await llm_records.finish_record(record_id, status="completed", cost_usd=cost, input_tokens=10, output_tokens=20,
                                    cache_read_tokens=30, cache_creation_tokens=40, context_tokens=context,
                                    compactions=compactions or [], pool=pool)
    return record_id


async def _task(pool: Any, task_id: str, project_id: str) -> None:
    from looper.db import tasks

    await tasks.create_task(task_id=task_id, requirement=f"do {task_id}", project_id=project_id, pool=pool)


def test_the_task_detail_and_list_carry_the_tasks_cost(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import llm_records, projects

    async def test(pool: Any) -> None:
        project = await projects.create_project(name="calc", pool=pool)
        await _task(pool, "task-a", project["id"])
        await _task(pool, "task-b", project["id"])
        for agent_id, cost in (("planner-task-a", 0.12), ("planner-task-a", 0.03), ("worker-1", 0.4),
                               ("reviewer-2", 0.05), ("worker-3", 0.25)):
            await _run_record(pool, agent_id, task_id="task-a", cost=cost)
        await _run_record(pool, "manager", task_id="task-a", cost=7.0)

        status, detail = await _get("/api/tasks/task-a")
        assert status == 200
        cost = detail["cost"]
        assert len(cost["manager_by_model"]) == 1
        assert cost["manager_by_model"][0]["runs"] == 1
        assert cost["manager_by_model"][0]["cost_usd"] == 7.0
        assert sum(r["runs"] for r in cost["by_model"]) == cost["runs"] == 5
        runs = [r for r in await llm_records.list_records(task_id="task-a", pool=pool) if r["agent_id"] != "manager"]
        assert len(runs) == cost["runs"] == 5
        assert cost["cost_usd"] == 0.85
        assert sum(Decimal(str(r["cost_usd"])) for r in runs) == Decimal(str(cost["cost_usd"]))
        assert sum(Decimal(str(r["cost_usd"] or 0)) for r in cost["by_model"]) == Decimal(str(cost["cost_usd"]))
        assert sum(Decimal(str(r["cost_usd"])) for r in cost["by_role"].values()) == Decimal(str(cost["cost_usd"]))
        assert {role: (row["cost_usd"], row["runs"]) for role, row in cost["by_role"].items()} == {
            "planner": (0.15, 2), "worker": (0.65, 2), "reviewer": (0.05, 1)}
        assert (cost["running"], cost["unpriced_runs"]) == (0, 0)

        status, listed = await _get("/api/tasks")
        assert status == 200
        by_id = {task["task_id"]: task for task in listed["tasks"]}
        assert by_id["task-a"]["cost"] == cost
        assert by_id["task-b"]["cost"]["cost_usd"] == 0.0 and by_id["task-b"]["cost"]["runs"] == 0

    _run(test, monkeypatch)


def _model_launch():
    from looper.agents.agent_settings import AgentSettings, Launch
    from looper.agents.profile_registry import (
        BUILTIN_RUNTIME, Endpoint, ModelSpec, ProviderSpec, ResolvedAgentProfile, ResolvedModel,
    )
    from looper.agents.runtimes import ApiProtocol, AuthKind, VendorType

    provider = ProviderSpec("prov-test", "test", VendorType.CUSTOM, AuthKind.API_KEY,
                            api_key="test-secret-never-record", env=(("EXAMPLE_OPTION", "private-value"),))
    model = ModelSpec("model-test", provider.id, "glm-5.3[1m]", ApiProtocol.ANTHROPIC_MESSAGES,
                      input_usd_per_mtok=2)
    resolved = ResolvedModel(model, provider, Endpoint(provider.id, model.protocol, "https://example.invalid"))
    profile = ResolvedAgentProfile("profile-test", "test", BUILTIN_RUNTIME, model.id, resolved_model=resolved)
    return Launch(AgentSettings(profile=profile))


def test_launch_records_identity_and_the_prices_used_without_credentials() -> None:
    from looper.agents.runtimes.pricing import PRICES_AS_OF

    launch = _model_launch()
    record = launch.record()
    assert record["llm_model"] == {
        "model_id": "model-test", "provider_id": "prov-test", "name": "glm-5.3[1m]", "protocol": "anthropic-messages"}
    assert record["profile"]["provider"]["auth"] == "api_key"
    pricing = record["pricing"]
    assert pricing["cost_source"] == "provider_price"
    assert pricing["rule"] == "exact_normalized_builtin_v1"
    assert pricing["builtin_prices_as_of"] == PRICES_AS_OF
    assert pricing["exact"]["glm-5.3[1m]"] == {
        "input_usd_per_mtok": 2, "output_usd_per_mtok": 4.4,
        "cache_read_usd_per_mtok": 0.26, "cache_write_usd_per_mtok": 1.4}
    assert pricing["normalized"]["glm-5.3"] == pricing["exact"]["glm-5.3[1m]"]
    assert pricing["builtin"]["deepseek-flash"]["input_usd_per_mtok"] == 0.3
    assert pricing["aliases"] == {}
    serialized = json.dumps(record)
    assert "test-secret-never-record" not in serialized and "private-value" not in serialized
    assert "api_key" not in pricing


@pytest.mark.parametrize("normalized", [False, True])
def test_subscription_records_preserve_auth_and_the_runtime_cost_rule(normalized: bool) -> None:
    from looper.agents.agent_settings import AgentSettings, Launch
    from looper.agents.profile_registry import builtin_resolved_profile
    from looper.agents.runtimes.pricing import PRICES_AS_OF

    launch = Launch(AgentSettings(profile=builtin_resolved_profile())) if normalized else Launch()
    record = launch.record()
    assert record["profile"]["provider"]["auth"] == "oauth"
    assert record["pricing"] == {"cost_source": "runtime", "rule": "runtime_reported",
                                  "builtin_prices_as_of": PRICES_AS_OF}
    if normalized:
        assert record["llm_model"]["model_id"] == launch.profile.model_id
        assert record["llm_model"]["name"] == "opus"


@pytest.mark.parametrize("unknown", [False, True])
def test_recorder_records_launch_snapshot_and_keeps_exact_reported_model(
    agent_db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unknown: bool,
) -> None:
    from looper.agents.claude_cli import ClaudeRunRecorder
    from looper.agents.runtimes import pricing

    launch = _model_launch()

    async def test() -> None:
        recorder = ClaudeRunRecorder(agent_id="worker-1", task_id=None, command=[], cwd=str(tmp_path),
                                     input_text="test", launch=launch)
        await recorder.start()
        snapshot = agent_db.start_record.await_args.kwargs["metadata"]
        assert snapshot["llm_model"]["model_id"] == "model-test"
        assert snapshot["pricing"] == launch.record()["pricing"]
        monkeypatch.setitem(pricing.BUILTIN_PRICES, "glm-5.3", pricing.ModelPrice(99, 99, 99, 99))
        await recorder.record({"type": "system", "subtype": "init", "model": "glm-5.3-20261001[1m]"})
        models = {"glm-5.3-20261001[1m]": {"inputTokens": 1_000_000, "outputTokens": 1_000_000}}
        if unknown:
            models["unknown-model"] = {"outputTokens": 1}
        await recorder.finish(output_text="done", result_event={
            "type": "result", "subtype": "success", "modelUsage": models, "total_cost_usd": 999})
        saved = agent_db.finish_record.await_args.kwargs
        assert saved["cost_usd"] == (None if unknown else 6.4)
        assert saved["model"] == "glm-5.3-20261001[1m]"
        assert saved["cost_source"] == "provider_price"
        assert launch.record()["pricing"] == snapshot["pricing"]

    asyncio.run(test())


@pytest.mark.parametrize("running_process", [False, True])
def test_manager_direct_record_uses_the_same_launch_metadata(agent_db, running_process: bool) -> None:
    from unittest.mock import AsyncMock

    from looper.agents.tiers.manager import ManagerAgent

    launch = _model_launch()
    manager = SimpleNamespace(agent_id="manager", _process=SimpleNamespace(launch=launch) if running_process else None,
                              _load_agent_settings=AsyncMock(return_value=launch.settings))
    envelope = SimpleNamespace(payload={"text": "/new"})
    asyncio.run(ManagerAgent._record_new_session(manager, envelope, {"channel": "web"}, "previous"))
    record = agent_db.start_record.await_args.kwargs["metadata"]
    assert record["llm_model"] == launch.record()["llm_model"]
    assert record["pricing"] == launch.record()["pricing"]
    assert record["profile"]["provider"]["auth"] == "api_key"
    assert record["session_reset"]["previous_session_id"] == "previous"


def test_old_and_new_records_keep_identical_task_costs_and_model_groups(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import llm_records, projects

    async def test(pool: Any) -> None:
        project = await projects.create_project(name="history", pool=pool)
        launch_metadata = _model_launch().record()
        # Old profile, old agent, and pre-profile records, including precedence when both keys exist.
        cases = [
            ("planner-history", "glm-5.3[1m]", 0.12, "runtime", None),
            ("worker-1", "glm-5.3[1m]", 0.3, "provider_price", {"agent": {"provider": {"auth": "api_key"}}}),
            ("reviewer-1", "glm-5.3", 0.07, "provider_price", {"profile": {"provider": {"auth": "oauth"}}}),
            ("worker-2", "unknown", None, "provider_price", {"profile": {"provider": {"auth": "api_key"}}}),
            ("worker-3", None, None, None, None),
            ("manager", "glm-5.3[1m]", 4.0, "provider_price", {"agent": {"provider": {"auth": "oauth"}}}),
            ("manager", "unknown", None, "provider_price", None),
            ("worker-4", "glm-5.3", 0.2, "provider_price",
             {"profile": {"provider": {"auth": "api_key"}}, "agent": {"provider": {"auth": "oauth"}}}),
        ]
        old_records = []
        for task_id in ("history", "mixed"):
            await _task(pool, task_id, project["id"])
            for i, (agent, model, cost, source, metadata) in enumerate(cases):
                if task_id == "mixed" and i % 2 == 0:
                    metadata = {**(metadata or {}), **{k: launch_metadata[k] for k in ("llm_model", "pricing")}}
                record_id = await llm_records.start_record(agent_id=agent, task_id=task_id, metadata=metadata, pool=pool)
                if i != 4:
                    await llm_records.finish_record(record_id, status="completed", model=model, cost_usd=cost,
                                                    cost_source=source, input_tokens=10, output_tokens=20,
                                                    cache_read_tokens=30, cache_creation_tokens=40, pool=pool)
                if task_id == "history":
                    old_records.append(await llm_records.get_record(record_id, pool=pool))

        costs = await llm_records.task_costs(["history", "mixed"], pool=pool)
        assert costs["history"] == costs["mixed"]
        cost = costs["mixed"]
        assert cost["cost_usd"] == 0.69
        assert (cost["runs"], cost["running"], cost["unpriced_runs"], cost["equivalent_runs"],
                cost["provider_price_runs"]) == (6, 1, 1, 2, 2)
        assert sum(m["input_tokens"] for m in cost["by_model"]) == 50
        assert {m["model"] for m in cost["by_model"]} == {"glm-5.3[1m]", "glm-5.3", "unknown", None}
        assert cost["manager_by_model"][0]["cost_usd"] == 4.0
        assert cost["manager_by_model"][1]["unpriced_runs"] == 1
        assert [await llm_records.get_record(r["id"], pool=pool) for r in old_records] == old_records

    _run(test, monkeypatch)


def test_registry_price_edits_and_deletion_do_not_reprice_a_run_or_its_history(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from looper.agents.claude_cli import ClaudeRunRecorder
    from looper.db import llm_records, profile_registry

    async def test(pool: Any) -> None:
        launch = _model_launch()
        await pool.execute("INSERT INTO llm_providers (id, name, vendor_type, auth) "
                           "VALUES ('prov-test', 'test', 'custom', 'api_key')")
        await pool.execute("INSERT INTO llm_provider_endpoints (provider_id, protocol, base_url) "
                           "VALUES ('prov-test', 'anthropic-messages', 'https://example.invalid')")
        await pool.execute("INSERT INTO llm_models (id, provider_id, name, protocol, input_usd_per_mtok) "
                           "VALUES ('model-test', 'prov-test', 'glm-5.3[1m]', 'anthropic-messages', 2)")
        historical_id = await llm_records.start_record(agent_id="worker-1", pool=pool)
        await llm_records.finish_record(historical_id, status="completed", cost_usd=1.25, input_tokens=100,
                                        model="glm-5.3[1m]", pool=pool)
        historical = await llm_records.get_record(historical_id, pool=pool)
        recorder = ClaudeRunRecorder(agent_id="worker-2", task_id=None, command=[], cwd=str(tmp_path),
                                     input_text="test", launch=launch)
        await recorder.start()
        opened = await llm_records.get_record(recorder.record_id, pool=pool)
        assert opened["metadata"]["llm_model"]["model_id"] == "model-test"
        assert "test-secret-never-record" not in json.dumps(opened)
        async with pool.acquire() as conn, conn.transaction():
            await profile_registry.update_model(conn, "model-test", {"input_usd_per_mtok": 99})
        changed = await profile_registry.get_model("model-test", pool=pool)
        assert changed["input_usd_per_mtok"] == 99
        assert await profile_registry.delete_model("model-test", pool=pool)
        assert await profile_registry.get_model("model-test", pool=pool) is None
        await recorder.record({"type": "system", "subtype": "init", "model": "glm-5.3-20261001[1m]"})
        await recorder.finish(output_text="done", result_event={
            "type": "result", "subtype": "success", "total_cost_usd": 999,
            "modelUsage": {"glm-5.3-20261001[1m]": {"inputTokens": 1_000_000, "outputTokens": 1_000_000}}})
        ended = await llm_records.get_record(recorder.record_id, pool=pool)
        assert ended["cost_usd"] == 6.4 and ended["input_tokens"] == ended["output_tokens"] == 1_000_000
        assert ended["model"] == "glm-5.3-20261001[1m]"
        assert ended["metadata"] == opened["metadata"]
        assert await llm_records.get_record(historical_id, pool=pool) == historical

    _run(test, monkeypatch)


def test_an_agents_usage_reads_back_its_context_cost_and_compactions(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        compacted = [{"trigger": "auto", "pre_tokens": 25120, "post_tokens": 1359, "duration_ms": 13135}]
        await _run_record(pool, "manager", session_id="s1", cost=0.25, context=18000)
        await _run_record(pool, "manager", session_id="s1", cost=0.5, context=24000)
        await _run_record(pool, "manager", session_id="s1", cost=0.75, context=17000, compactions=compacted)

        status, usage = await _get("/api/agents/manager/usage")
        assert status == 200
        assert usage["total"]["cost_usd"] == pytest.approx(1.5)
        assert (usage["total"]["runs"], usage["total"]["compactions"]) == (3, 1)
        assert [t["context_tokens"] for t in usage["turns"]] == [18000, 24000, 17000]
        assert usage["turns"][-1]["compactions"] == compacted
        assert usage["latest_context_tokens"] == 17000
        assert [(s["session_id"], s["runs"]) for s in usage["by_session"]] == [("s1", 3)]
        assert len(usage["by_day"]) == 1

        status, latest = await _get("/api/agents/manager/usage", {"turns": "1"})
        assert status == 200 and [t["context_tokens"] for t in latest["turns"]] == [17000]
        status, bad = await _get("/api/agents/manager/usage", {"turns": "many"})
        assert status == 400 and "turns" in bad["error"]

    _run(test, monkeypatch)
