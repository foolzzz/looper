"""Result summaries read the durable current attempt and expose read failures honestly."""
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import acceptance_result as result
from looper.agents.acceptance_checks import CommandCheck
from looper.agents.project_execution import AcceptanceChecksMode
from looper.agents.tiers.planner import PlanStep
from looper.db import plans, projects, step_check_runs as runs, tasks
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="requires disposable LOOPER_TEST_DATABASE_URL")


def test_current_attempt_and_recheck_survive_project_off(monkeypatch):
    async def run(pool):
        owner = await projects.create_project(name="Result fixture", pool=pool)
        await tasks.create_task(task_id="result-task", requirement="Measure", project_id=owner["id"], pool=pool)
        check = CommandCheck("c1", 1, "printf fixture")
        await plans.save("result-task", plan={"plan_id": "p", "status": "executing"}, steps=[{
            "id": "result-step", "index": 0, "key": "s1", "instruction": "Measure",
            "acceptance_criteria": ["works"], "checks": [check.row()]}], pool=pool)
        for attempt, state in ((1, runs.CheckState.FAILED), (2, runs.CheckState.ERROR), (2, runs.CheckState.PASSED)):
            row = await runs.create_run("result-task", "result-step", check.row(), attempt,
                                        head_sha="abcdef1234567890", guarded=True, pool=pool)
            await runs.finish_run(row["id"], state,
                **({"error_kind": runs.ErrorKind.TIMEOUT} if state is runs.CheckState.ERROR else {}), pool=pool)
        step = PlanStep("result-step", 0, "Measure", key="s1", retries=1,
                        acceptance_criteria=["works"], checks=[check])
        agent = SimpleNamespace(_task_id="result-task", _activity=AsyncMock(),
                                _execution=SimpleNamespace(acceptance_checks=AcceptanceChecksMode.OFF))
        answer = await result.for_plan(agent, SimpleNamespace(steps=[step]))
        assert answer.section == "## Evidence\n- s1 c1 (criterion 1): passed at abcdef123456"
        assert answer.counts.line() == "Checks: 1 passed"
        agent._activity.assert_not_awaited()
    run_db(run, monkeypatch)


@pytest.mark.parametrize("mode,has_checks,shown", [
    (AcceptanceChecksMode.LOCAL, True, True), (AcceptanceChecksMode.OFF, True, False),
    (AcceptanceChecksMode.LOCAL, False, False)])
def test_read_failure_fallback_and_activity(monkeypatch, caplog, mode, has_checks, shown):
    async def run(pool):
        # A real failed query, rather than a prebuilt fallback value.
        async def broken(task_id):
            await pool.fetch("SELECT * FROM deliberately_missing_evidence_table WHERE task_id=$1", task_id)
        monkeypatch.setattr(result.step_check_runs, "list_runs", broken)
        step = PlanStep("s", 0, "Measure", checks=[CommandCheck("c", 1, "true")] if has_checks else [])
        agent = SimpleNamespace(_task_id="fixture", _activity=AsyncMock(),
                                _execution=SimpleNamespace(acceptance_checks=mode))
        with caplog.at_level(logging.WARNING):
            answer = await result.for_plan(agent, SimpleNamespace(steps=[step]))
        if shown:
            assert answer.section == ("## Evidence\nEvidence could not be read when the task ended; "
                                      "see the step cards.")
            assert answer.counts.line() == ""
            assert "Acceptance evidence summary unavailable" in caplog.text
            assert agent._activity.await_args.args[1] == "Acceptance evidence summary unavailable"
        else:
            assert answer is None
            agent._activity.assert_not_awaited()
    run_db(run, monkeypatch)
