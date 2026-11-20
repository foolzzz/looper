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
                                             question={"problem": "The request as it was sent."})
        await pool.execute("UPDATE agent_spawns SET inputs = $2::jsonb WHERE id = $1", answered,
                           '{"bindings": [{"alias": "service A", "refers_to": "THE REAL NAME"}]}')
        await advisor_advice.record_advice(answered, ADVICE)
        await advisor_advice.adopt(answered, {"adoption": "partial", "items": [
            {"ref": "q1", "use": "adopted", "steps": ["s1"], "why": "it keeps a way back"}]})
        await advisor_advice.unlock("task-a", "step-a", 1, True)
        withdrawn = await advisor.open_consult(advisor.Brief("task-a", "step-a", 2, advisor.Source.OPTIONAL,
                                                             asked="NOT A KIND"), budget_usd=1)
        await advisor.withdraw(withdrawn, "the plan changed")
        blocked = await advisor.open_consult(advisor.Brief("task-a", "step-a", 3, advisor.Source.CHECKPOINT,
                                                           asked="plan_review"), budget_usd=1)
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
        # the card's parts: the request that was sent (never its bindings), the answer, the stance and its reasons
        assert first["question"] == {"problem": "The request as it was sent."} and first["advice"] == ADVICE
        assert first["adoption_items"] == [{"ref": "q1", "use": "adopted", "why": "it keeps a way back"}]
        assert first["duration_s"] is not None and first["duration_s"] >= 0
        assert (second["question"], third["question"], third["advice"]) == (None, None, None)
        assert (second["source"], second["attempt"], second["state"], second["failure"], second["answers"]) == (
            "optional", 2, "failed", "no_request", [])
        assert (first["checks"], second["checks"]) == ([], [])
        # a blocked consult shows the checks its request failed, by category and count, never where they matched
        # a request that never passed is named by the kind the Planner first wrote (a known one only)
        assert (second["kind"], third["kind"]) == (None, "plan_review")
        assert (third["state"], third["failure"], third["returns"], third["checks"]) == (
            "blocked", "blocked", advisor.RETURNS_MAX, [{"stage": "code", "category": "local_path", "count": 3},
                                                        {"stage": "judge", "category": "not_standalone", "count": 1}])
        assert "THE REAL NAME" not in repr(consults) and "NOT A KIND" not in repr(consults)
        detail = await task_detail("task-a")
        assert detail["advisor"]["consults"] == consults
        assert detail["advisor"]["summary"] == ("请教 advisor 3 次，花费 0.30 美元，采纳 1 条，其中 1 条帮步骤通过；"
                                                "检查点请教未完成：s1")
        schema = find("POST", "/api/v1/tasks/detail").result
        from looper.agents.v1_domains.task_views import _plain

        validate_arguments({"type": "object", "properties": {"result": schema}}, {"result": _plain(detail)})
        assert "advisor" not in await task_detail("task-b")

    run_db(test, monkeypatch)


def test_a_tasks_runs_name_their_kind_and_the_run_that_started_them(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import llm_records, projects

    async def test(pool: Any) -> None:
        project = (await projects.create_project(name="Store", pool=pool))["id"]
        await pool.execute("INSERT INTO tasks (id, project_id, requirement, status) VALUES ('task-a', $1, 'r', "
                           "'running')", project)
        planner = await llm_records.start_record(agent_id="planner-task-a", task_id="task-a")
        judge = await llm_records.start_record(agent_id="advice_check-0a1b2c3d4e5f", task_id="task-a",
                                               run_kind="advice_check", parent_record_id=planner)
        advisor = await llm_records.start_record(agent_id="advisor-0a1b2c3d4e5f", task_id="task-a",
                                                 run_kind="advisor", parent_record_id=planner)
        rows = {r["id"]: r for r in await llm_records.task_run_rows("task-a")}
        assert {i: (r["role"], r["kind"], r["parent_record_id"]) for i, r in rows.items()} == {
            planner: ("planner", "planner", None), judge: ("planner", "advice_check", planner),
            advisor: ("planner", "advisor", planner)}

    run_db(test, monkeypatch)


def test_the_view_knows_every_kind_of_request() -> None:
    from looper.agents.advice import AdviceKind
    from looper.db.advisor_advice import _KINDS

    assert _KINDS == {kind.value for kind in AdviceKind}


def test_the_summary_counts_only_adopted_points_that_helped_and_a_refused_checkpoint_as_unfinished(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from looper.agents import advisor
    from looper.db import advisor_advice, agent_spawns, projects
    from looper.db.agent_spawns import SpawnState

    async def answered(task: str, step: str, source: advisor.Source, advice: dict[str, Any], items: list[Any],
                       passed: bool | None) -> None:
        spawn = await advisor.open_consult(advisor.Brief(task, step, 1, source), budget_usd=1)
        for to, expect in ((SpawnState.LINTING, SpawnState.DRAFTING), (SpawnState.ASKING, SpawnState.LINTING),
                           (SpawnState.ANSWERED, SpawnState.ASKING)):
            await agent_spawns.move(spawn, to, expect=expect, spent_usd=0.05)
        await advisor_advice.record_question(spawn_id=spawn, task_id=task, step_id=step, attempt=1,
                                             source=source.value, kind="plan_review", question={"problem": "p"})
        await advisor_advice.record_advice(spawn, advice)
        if items is not None:
            await advisor_advice.adopt(spawn, {"adoption": "rejected" if not items or all(
                i["use"] == "rejected" for i in items) else "adopted", "items": items})
        if passed is not None:
            await advisor_advice.unlock(task, step, 1, passed)

    async def test(pool: Any) -> None:
        project = (await projects.create_project(name="Store", pool=pool))["id"]
        await pool.execute("INSERT INTO tasks (id, project_id, requirement, status) VALUES ('task-a', $1, 'r', "
                           "'running')", project)
        await pool.execute("INSERT INTO task_steps (id, task_id, step_index, step_key, instruction) VALUES "
                           "('step-a', 'task-a', 0, 's1', 'a'), ('step-b', 'task-a', 1, 's2', 'b')")
        # every point rejected, and the attempt passed on the Planner's own: no point helped
        await answered("task-a", "step-a", advisor.Source.OPTIONAL, ADVICE,
                       [{"ref": "q1", "use": "rejected", "why": "not needed"}], True)
        # a checkpoint the advisor refused (answered, out of scope): its step went on without a review
        await answered("task-a", "step-b", advisor.Source.CHECKPOINT,
                       {**ADVICE, "status": "out_of_scope", "answers": []}, None, None)
        assert await advisor_advice.summary_line("task-a") == (
            "请教 advisor 2 次，花费 0.30 美元，采纳 0 条，其中 0 条帮步骤通过；检查点请教未完成：s2")

    run_db(test, monkeypatch)


def test_the_managers_view_of_a_task_leaves_out_the_advisor_while_its_project_has_it_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from looper.agents.projects import task_detail
    from looper.db import projects

    async def test(pool: Any) -> None:
        project = (await projects.create_project(name="Store", pool=pool))["id"]
        await pool.execute(
            "INSERT INTO tasks (id, project_id, requirement, status, result_summary) VALUES ('task-a', $1, 'r', "
            "'completed', $2)", project,
            "## Delivered\nthe migration\n\n## Advisor\n请教 advisor 1 次，花费 0.20 美元，采纳 1 条，其中 1 条帮步骤通过")
        await pool.execute("UPDATE projects SET advisor = 'on' WHERE id = $1", project)
        assert "## Advisor" in (await task_detail("task-a", compact=True))["result"]
        await pool.execute("UPDATE projects SET advisor = 'off' WHERE id = $1", project)
        view = await task_detail("task-a", compact=True)
        assert view["result"] == "## Delivered the migration"
        assert "advis" not in repr(view).lower()
        assert "## Advisor" in (await task_detail("task-a"))["result_summary"]  # the page keeps its history

    run_db(test, monkeypatch)
