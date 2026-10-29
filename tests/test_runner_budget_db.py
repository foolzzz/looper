"""First-request admission uses the production task-cost aggregation and prices."""
from dataclasses import replace

import pytest

from looper.agents.runtimes.runner.turn import RunnerTurn
from looper.agents.tiers.worker import WorkerPool
from looper.db import llm_records
from tests.db_support import DSN, run_db
from tests.pi_runtime_support import launch, provider

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


@pytest.mark.parametrize("earlier_cost", [None, 0.25])
def test_first_request_uses_known_task_spend_with_unpriced_history(monkeypatch, earlier_cost):
    async def test(pool):
        task_id = "task-first-request"
        earlier = await llm_records.start_record(agent_id="worker-earlier", task_id=task_id)
        await llm_records.finish_record(earlier, status="failed", cost_usd=earlier_cost)
        # Like the Planner recorder, open the current run before reading limits.
        await llm_records.start_record(agent_id=f"planner-{task_id}", task_id=task_id)
        selected = launch(provider(), model="gpt-6-astra")
        selected = replace(selected, settings=replace(selected.settings, max_budget_usd=1.0))
        limits = WorkerPool._run_limits(selected, {"task_budget_usd": 2.0}, task_id)
        assert await limits() == {"run_cap": 1.0, "task_cap": 2.0, "task_spent": earlier_cost or 0.0}
        turn = RunnerTurn(session_id="s", trace_id="t", role="planner", tools=None,
            provider_id=selected.profile.provider.id, model=selected.model, price=selected.price, limits=limits)
        result = await turn.authorize({"session_id": "s", "trace_id": "t", "request_id": "first", "kind": "turn",
            "provider": selected.profile.provider.id, "model": selected.model,
            "estimated_input_tokens": 10000, "max_output_tokens": 32000})
        assert result["allow"]

    run_db(test, monkeypatch, timeout=30)
