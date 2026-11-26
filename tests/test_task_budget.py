"""The shared task spend gate reads project settings without changing task state."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from looper.agents import task_budget


@pytest.mark.parametrize("cap,spent,allowed", [(None, 8, True), (2, 1.99, True), (2, 2, False)])
def test_budget_gate_project_and_workflow(monkeypatch, cap, spent, allowed):
    project = AsyncMock(return_value={"task_budget_usd": cap, "task_workflow": {"review_all": True}})
    costs = AsyncMock(return_value={"t": {"cost_usd": spent, "cost_status": "priced", "by_model": []}})
    monkeypatch.setattr(task_budget.db_projects, "get_task_project", project)
    monkeypatch.setattr(task_budget.db_llm_records, "task_costs", costs)
    gate = asyncio.run(task_budget.gate("t"))
    assert (gate.allowed, gate.cap) == (allowed, cap)
    project.assert_awaited_once_with("t")
    if cap is None:
        costs.assert_not_awaited()
    else:
        assert gate.spent == spent
        costs.assert_awaited_once_with(["t"])


def test_budget_unknown_models_and_read_failure(monkeypatch):
    monkeypatch.setattr(task_budget.db_projects, "get_task_project", AsyncMock(return_value={"task_budget_usd": 2}))
    costs = AsyncMock(return_value={"t": {"cost_usd": 0.5, "cost_status": "unknown", "by_model": [
        {"model": "unpriced", "unpriced_runs": 1, "input_tokens": 1},
        {"model": "never-called", "unpriced_runs": 1}]}})
    monkeypatch.setattr(task_budget.db_llm_records, "task_costs", costs)
    gate = asyncio.run(task_budget.gate("t"))
    assert gate.allowed and gate.spent == 0.5 and gate.unknown_models == ("unpriced",)
    costs.side_effect = RuntimeError("unavailable")
    with pytest.raises(RuntimeError, match="unavailable"):
        asyncio.run(task_budget.gate("t"))
