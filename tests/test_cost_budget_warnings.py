"""Only a configured cap needs a warning, once per unknown model set."""
import asyncio
from unittest.mock import AsyncMock

from looper.agents import cost_guard as cg
from looper.agents.cost_guard_settings import Config, Mode
from looper.agents.project_execution import ProjectExecution
from looper.agents.tiers import planner


def test_task_cap_unknown_warning_is_deduplicated_and_unset_cap_does_not_read(monkeypatch, tmp_path):
    monkeypatch.setattr(cg, "_guard", cg.Guard(Config(mode=Mode.OFF)))
    costs = {"t": {"cost_usd": 0.05, "cost_status": "unknown", "by_model": [
        {"model": "claude-haiku-5-5", "unpriced_runs": 1}]}}
    read = AsyncMock(return_value=costs)
    monkeypatch.setattr(planner.db_llm_records, "task_costs", read)
    owner = planner.PlannerAgent(task_id="t", runtime_root=tmp_path)
    owner._activity = AsyncMock()
    plan = planner.TaskPlan(plan_id="p", goal_id="", requirement="synthetic", work_dir=str(tmp_path))
    plan.status = "executing"

    async def check():
        assert await owner._within_budget(plan)
        read.assert_not_awaited()
        owner._activity.assert_not_awaited()
        owner._execution = ProjectExecution(task_budget_usd=4)
        assert await owner._within_budget(plan)
        owner._activity.assert_not_awaited()  # a failure before any request has no unpriced usage
        costs["t"]["by_model"][0]["input_tokens"] = 1
        for _ in range(3):
            assert await owner._within_budget(plan)
        owner._activity.assert_awaited_once()
        assert "claude-haiku-5-5" in owner._activity.await_args.args[1]

    asyncio.run(check())
