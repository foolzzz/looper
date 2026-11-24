"""A runtime bill remains authoritative through recorder, task, daily and guard reads."""
from datetime import datetime, timezone

import pytest

from looper.agents import cost_guard as cg
from looper.agents.agent_settings import AgentSettings, AgentType, Launch
from looper.agents.claude_cli import ClaudeRunRecorder
from looper.agents.cost_guard_settings import Config, Mode
from looper.agents.profile_registry import builtin_resolved_profile
from looper.agents.run_usage import Usage
from looper.agents.runtimes.pricing import usage_event
from looper.db import llm_records
from tests.db_support import DSN, run_db
from tests.runtime_model_helpers import with_model_name

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def test_runtime_bill_counts_for_task_daily_and_guard_after_unknown_catalog_event(monkeypatch, tmp_path, caplog):
    async def check(pool):
        launch = Launch(AgentSettings(profile=with_model_name(builtin_resolved_profile(), "sonnet")),
                        role=AgentType.WORKER)
        guard = cg.Guard(Config(mode=Mode.ENFORCE, daily_equivalent_usd=4), pool=pool)
        monkeypatch.setattr(cg, "_guard", guard)
        recorder = ClaudeRunRecorder(agent_id="worker-priced-runtime", task_id="priced-runtime", command=["claude"],
                                     cwd=str(tmp_path), input_text="synthetic", launch=launch)
        await recorder.start()
        recorder.account(usage_event("claude-haiku-5-5", Usage(input_tokens=100)))
        assert recorder.cap_stop is None
        assert recorder._spend.budget_cost_usd == pytest.approx(0.0002)
        assert not caplog.records
        await recorder.finish(output_text="done", result_event={
            "type": "result", "subtype": "success", "total_cost_usd": 0.05})
        saved = await pool.fetchrow("SELECT cost_usd, cost_source, metadata FROM llm_records WHERE id=$1",
                                    recorder.record_id)
        assert saved["cost_usd"] == 0.05 and saved["cost_source"] == "runtime"
        task = (await llm_records.task_costs(["priced-runtime"], pool=pool))["priced-runtime"]
        assert task["cost_usd"] == 0.05 and task["unpriced_runs"] == 0
        assert task["cost_status"] == "known"
        daily = await llm_records.spend_since(guard.day_start(datetime.now(timezone.utc)), pool=pool)
        assert daily["equivalent_usd"] == 0.05 and daily["cost_status"] == "known"
        for rebuild in (False, True):
            if rebuild:
                await guard.rebuild()
            counters = guard.snapshot()["counters"]
            assert {row["scope"] for row in counters} == {"task", "global_day"}
            assert all(row["spent_usd"] == 0.05 and row["cost_status"] == "known" for row in counters)

    run_db(check, monkeypatch, timeout=30)
    assert not caplog.records


def test_unpriced_main_model_keeps_runtime_bill_after_manager_turn_cap(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from looper.agents import session_budget
    from looper.agents.tiers import manager_budget

    async def check(pool):
        launch = Launch(AgentSettings(profile=with_model_name(builtin_resolved_profile(), "haiku")),
                        role=AgentType.MANAGER)
        guard = cg.Guard(Config(mode=Mode.ENFORCE, daily_equivalent_usd=4), pool=pool)
        monkeypatch.setattr(cg, "_guard", guard)
        monkeypatch.setattr(session_budget, "turn_budget_usd", AsyncMock(return_value=4))
        options = await manager_budget.prepare(SimpleNamespace(budget=None, pending_switch=None),
                                               SimpleNamespace(), launch)
        recorder = ClaudeRunRecorder(agent_id="manager", task_id=None, command=["claude"], cwd=str(tmp_path),
                                     input_text="synthetic", launch=launch, **options)
        await recorder.start()
        recorder.account(usage_event("claude-haiku-5-5", Usage(input_tokens=100)))
        assert recorder.cap_stop is not None
        await recorder.finish(output_text="", result_event={
            "type": "result", "subtype": "success", "total_cost_usd": 0.05})
        saved = await llm_records.get_record(recorder.record_id, pool=pool)
        assert saved["cost_usd"] == 0.05 and saved["cost_source"] == "runtime"
        assert saved["metadata"]["catalog_cost_status"] == "unknown"
        assert saved["metadata"]["catalog_unknown_price_models"] == ["claude-haiku-5-5"]
        assert saved["metadata"].get("cost_status", "known") == "known"
        assert not saved["metadata"].get("unknown_price_models")
        daily = await llm_records.spend_since(guard.day_start(datetime.now(timezone.utc)), pool=pool)
        assert daily["equivalent_usd"] == 0.05 and daily["cost_status"] == "known"
        for rebuild in (False, True):
            if rebuild:
                await guard.rebuild()
            counter = next(row for row in guard.snapshot()["counters"] if row["scope"] == "global_day")
            assert counter["spent_usd"] == 0.05 and counter["cost_status"] == "known"

    run_db(check, monkeypatch, timeout=30)
