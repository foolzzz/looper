"""A task's consults of the advisor as its page shows them (``advisor_advice.consults_view``, ``task_detail``):
where each came from, how it ended, the conclusions, what the Planner took and whether the attempt passed; never
the request's text. Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database."""

from __future__ import annotations

from typing import Any

import pytest

from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

ADVICE = {"status": "answered", "answers": [{"question_id": "q1", "conclusion": "Keep the old column.",
                                              "basis": ["rollback"], "confidence": "high", "assumptions": []}],
          "options": [], "next_steps": [], "risks": [], "needs_info": []}


def test_a_tasks_page_shows_its_consults_and_a_task_without_any_shows_none(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents import advisor
    from looper.agents.api_v1 import find, validate_arguments
    from looper.agents.projects import task_detail
    from looper.db import advisor_advice, agent_spawns, projects
    from looper.db.agent_spawns import SpawnState

    async def test(pool: Any) -> None:
        project = (await projects.create_project(name="Store", pool=pool))["id"]
        for task in ("task-a", "task-b"):
            await pool.execute("INSERT INTO tasks (id, project_id, requirement, status) VALUES ($1, $2, 'r', 'running')",
                               task, project)
        await pool.execute("INSERT INTO task_steps (id, task_id, step_index, step_key, instruction) "
                           "VALUES ('step-a', 'task-a', 0, 's1', 'Migrate the table.')")
        answered = await advisor.open_consult(advisor.Brief("task-a", "step-a", 1, advisor.Source.CHECKPOINT),
                                              budget_usd=1)
        for to, expect in ((SpawnState.LINTING, SpawnState.DRAFTING), (SpawnState.ASKING, SpawnState.LINTING),
                           (SpawnState.ANSWERED, SpawnState.ASKING)):
            await agent_spawns.move(answered, to, expect=expect, spent_usd=0.1)
        await advisor_advice.record_question(spawn_id=answered, task_id="task-a", step_id="step-a", attempt=1,
                                             source="checkpoint", kind="plan_review",
                                             question={"problem": "THE REQUEST TEXT"})
        await advisor_advice.record_advice(answered, ADVICE)
        await advisor_advice.adopt(answered, {"adoption": "partial", "items": []})
        await advisor_advice.unlock("task-a", "step-a", 1, True)
        withdrawn = await advisor.open_consult(advisor.Brief("task-a", "step-a", 2, advisor.Source.OPTIONAL),
                                               budget_usd=1)
        await advisor.withdraw(withdrawn, "the plan changed")
        blocked = await advisor.open_consult(advisor.Brief("task-a", "step-a", 3, advisor.Source.OPTIONAL),
                                             budget_usd=1)
        for number in range(advisor.RETURNS_MAX + 1):  # the last return blocks it
            await agent_spawns.move(blocked, SpawnState.LINTING, expect=SpawnState.DRAFTING)
            hits = [{"stage": "code", "category": "local_path", "field": "problem", "start": 3, "end": 20}]
            if number == 0:
                hits.append({"stage": "judge", "category": "not_standalone"})
            await advisor._return(await agent_spawns.get(blocked), ["fix it"], hits)
        consults = await advisor_advice.consults_view("task-a")
        assert [c["id"] for c in consults] == [answered, withdrawn, blocked]
        first, second, third = consults
        assert (first["source"], first["kind"], first["attempt"], first["state"], first["adoption"],
                first["unlocked"]) == ("checkpoint", "plan_review", 1, "answered", "partial", True)
        assert first["answers"] == [{"question_id": "q1", "conclusion": "Keep the old column.", "confidence": "high"}]
        assert first["spent_usd"] == pytest.approx(0.3) and first["status"] == "answered"
        assert (second["source"], second["attempt"], second["state"], second["failure"], second["answers"]) == (
            "optional", 2, "failed", "no_request", [])
        assert (first["checks"], second["checks"]) == ([], [])
        # a blocked consult shows the checks its request failed, by category and count, never where they matched
        assert (third["state"], third["failure"], third["returns"], third["checks"]) == (
            "blocked", "blocked", advisor.RETURNS_MAX, [{"stage": "code", "category": "local_path", "count": 3},
                                                        {"stage": "judge", "category": "not_standalone", "count": 1}])
        assert "THE REQUEST TEXT" not in repr(consults)
        detail = await task_detail("task-a")
        assert detail["advisor"]["consults"] == consults
        schema = find("POST", "/api/v1/tasks/detail").result
        from looper.agents.v1_domains.task_views import _plain

        validate_arguments({"type": "object", "properties": {"result": schema}}, {"result": _plain(detail)})
        assert "advisor" not in await task_detail("task-b")

    run_db(test, monkeypatch)
