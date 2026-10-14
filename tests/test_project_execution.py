"""A project's execution settings (the task spend cap, the supervision interval and the review policy), and
its agent settings' model and per-run cap as its agents launch with them.

Each setting is read where its agent starts a run or a turn; a project that
sets none runs its agents as before. Claude is a stand-in throughout: the
Planner's (``planner_claude``) and the Workers' (a script) report costs like
Claude Code does.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from agent_fixtures import agent_json

from loopx.agents.message import TASK_ROUTER_ADDRESS, MessageEnvelope, MessageType
from loopx.agents.tiers.planner import PlannerAgent
from loopx.agents.tiers.worker import WorkerConfig, WorkerPool
from loopx.db import llm_records as db_llm_records
from tests.test_agent_planner_worker import _fake_claude, _flag, _run_step, _step_envelope, _worker_row
from tests.test_planner_agent import (
    REVIEW_PASS,
    WORK_OK,
    Harness,
    activity,
    always,
    decide,
    drive,
    hold_first,
    plan,
    reply,
    step,
)
from tests.test_projects_api import _call, _Store, store  # noqa: F401  (the store fixture)

UNSET = {"task_budget_usd": None, "task_budget_action": None,
         "supervise_interval_seconds": None, "review_policy": None}


def _project(**settings: Any) -> dict[str, Any]:
    """A project row as the database returns it: every execution column, NULL unless given."""
    return {"id": "proj-1", "name": "calc", "background": "", **UNSET, **settings}


def _layers(agent_db: Any, *rows: dict[str, Any]) -> None:
    """The agent setting layers' rows the agents read."""
    agent_db.list_layers.return_value = list(rows)


def _in_project(**values: Any) -> dict[str, Any]:
    return {"scope": "project", "project_id": "proj-1", **values}


def _spend_from_records(monkeypatch: pytest.MonkeyPatch, agent_db: Any) -> None:
    """The task's cost, as ``task_costs`` sums it: what the finished runs recorded."""

    async def task_costs(task_ids: list[str], **_: Any) -> dict[str, dict[str, Any]]:
        spent = sum(call.kwargs.get("cost_usd") or 0 for call in agent_db.finish_record.await_args_list)
        return {task_id: {"cost_usd": spent} for task_id in task_ids}

    monkeypatch.setattr(db_llm_records, "task_costs", task_costs)


# --- The API ---


class TestProjectApi:
    def test_settings_are_stored_read_back_and_cleared(self, store: _Store) -> None:
        store.add_project("proj-a", "calc")
        assert {k: v for k, v in _call("GET", "/api/projects")[1]["projects"][0].items() if k in UNSET} == UNSET

        settings = {"task_budget_usd": 2,
                    "task_budget_action": "wrap_up", "supervise_interval_seconds": 0, "review_policy": "all"}
        status, body = _call("PUT", "/api/projects/proj-a", settings)
        assert status == 200
        assert {k: body[k] for k in UNSET} == {**settings, "task_budget_usd": 2.0}

        status, body = _call("PUT", "/api/projects/proj-a", {"task_budget_usd": None, "task_budget_action": ""})
        assert status == 200 and (body["task_budget_usd"], body["task_budget_action"]) == (None, None)
        assert body["review_policy"] == "all"  # a field left out keeps its value

    @pytest.mark.parametrize("field, value, says", [
        ("task_budget_usd", 0, "more than 0"),
        ("task_budget_usd", -1, "more than 0"),
        ("task_budget_usd", 0.00001, "4 decimals"),
        ("task_budget_usd", 100_001, "at most 100000"),
        ("task_budget_usd", "5", "number of US dollars"),
        ("task_budget_usd", True, "number of US dollars"),
        ("task_budget_action", "pause", "wrap_up, fail"),
        ("review_policy", "some", "planner, all"),
        ("supervise_interval_seconds", -5, "from 0"),
        ("supervise_interval_seconds", 1.5, "whole number"),
        ("supervise_interval_seconds", 86_401, "86400"),
    ])
    def test_an_invalid_setting_is_refused(self, store: _Store, field: str, value: Any, says: str) -> None:
        store.add_project("proj-a", "calc")
        status, body = _call("PUT", "/api/projects/proj-a", {field: value})
        assert status == 400 and says in body["error"]
        assert store.projects["proj-a"].get(field) is None


# --- Workers and Reviewers ---


@pytest.mark.usefixtures("agent_db")
class TestWorkerRuns:
    def test_the_project_model_and_run_cap_reach_worker_and_reviewer(
        self, agent_db: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _worker_row(monkeypatch, {"system_prompt": "ROLE", "timeout_seconds": 0, "max_turns": 0})
        _layers(agent_db, {"scope": "role", "role": "worker", "agent": agent_json("haiku")},
                _in_project(agent=agent_json("sonnet"), run_budget_usd=0.25))
        agent_db.get_task_project.return_value = _project()
        for verify in (False, True):
            _, seen = _run_step(verify=verify)
            assert (_flag(seen["argv"], "--model"), _flag(seen["argv"], "--max-budget-usd")) == ("sonnet", "0.25")
            metadata = agent_db.start_record.await_args.kwargs["metadata"]
            assert metadata["model_choice"]["source"] == "project"
            assert metadata["max_budget_usd"] == 0.25
            assert metadata["settings_sources"]["run_budget_usd"] == "project"

    def test_a_run_stopped_at_its_cap_fails_with_the_reason(self, agent_db: Any) -> None:
        _layers(agent_db, _in_project(run_budget_usd=0.01))
        agent_db.get_task_project.return_value = _project()
        # What Claude Code 2.1.285 writes at the cap, then it exits 1 when its input ends.
        body = (
            "sys.stdin.readline()\n"
            "print(json.dumps({'type': 'result', 'subtype': 'error_max_budget_usd', 'is_error': True, "
            "'total_cost_usd': 0.0103, 'errors': ['Reached maximum budget ($0.01)']}), flush=True)\n"
            "sys.stdin.read()\nsys.exit(1)"
        )

        async def _run() -> dict[str, Any]:
            with tempfile.TemporaryDirectory() as tmp:
                pool = WorkerPool(config=WorkerConfig(claude_bin=_fake_claude(Path(tmp), body)), runtime_root=Path(tmp))
                return await pool._execute(_step_envelope(Path(tmp), verify=False))

        result = asyncio.run(_run())
        assert result["status"] == "failed"
        assert result["error"] == ("Claude Code stopped at its per-run spend cap (--max-budget-usd): "
                                   "Reached maximum budget ($0.01)")


# --- The Planner ---


@pytest.mark.usefixtures("agent_db")
class TestPlanner:
    def test_the_project_model_and_run_cap_reach_the_planner(self, planner_claude: Any, agent_db: Any) -> None:
        _layers(agent_db, _in_project(agent=agent_json("sonnet"), run_budget_usd=1.5))
        agent_db.get_task_project.return_value = _project()
        claude = planner_claude(plan(step("s1")), decide("accept"))
        drive(claude, always(WORK_OK))
        (argv,) = claude.argvs()
        assert (_flag(argv, "--model"), _flag(argv, "--max-budget-usd")) == ("sonnet", "1.5")

    def test_a_role_in_project_model_reaches_only_that_roles_argv(
        self, planner_claude: Any, agent_db: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _layers(agent_db, {"scope": "system", "agent": agent_json("sonnet")},
                {"scope": "role_project", "role": "worker", "project_id": "proj-1", "agent": agent_json("haiku")},
                {"scope": "role_project", "role": "worker", "project_id": "proj-other", "agent": agent_json("opus")})
        agent_db.get_task_project.return_value = _project()
        claude = planner_claude(plan(step("s1")), decide("accept"))
        drive(claude, always(WORK_OK))
        (planner_argv,) = claude.argvs()
        _worker_row(monkeypatch, {"system_prompt": "ROLE", "timeout_seconds": 0, "max_turns": 0})
        worker_argv, reviewer_argv = (_run_step(verify=verify)[1]["argv"] for verify in (False, True))
        assert [_flag(argv, "--model") for argv in (planner_argv, worker_argv, reviewer_argv)] == [
            "sonnet", "haiku", "sonnet"]
        # Outside the project, the Worker has the system's.
        agent_db.get_task_project.return_value = {**_project(), "id": "proj-2"}
        assert _flag(_run_step()[1]["argv"], "--model") == "sonnet"

    def test_a_turn_at_the_run_cap_fails_and_the_next_turn_gets_a_new_process(
        self, planner_claude: Any, agent_db: Any,
    ) -> None:
        _layers(agent_db, _in_project(run_budget_usd=0.1))
        agent_db.get_task_project.return_value = _project()
        claude = planner_claude(plan(step("s1"), step("s2", "s1")), "<budget>", decide("accept", summary="done"))
        h = drive(claude, always(WORK_OK))

        final = h.final()
        assert final is not None and final.payload["status"] == "completed"
        # s1's decision hit the cap, so its Worker's own verdict decided; s2's ran in a resumed process.
        turns = claude.turns()
        assert len(turns) == 3 and turns[1]["pid"] != turns[2]["pid"]
        assert "--resume" in claude.argvs()[-1]
        assert any("per-run spend cap (--max-budget-usd): Reached maximum budget" in line for line in activity(agent_db))

    def test_review_policy_all_sends_every_step_to_the_reviewer(self, planner_claude: Any, agent_db: Any) -> None:
        agent_db.get_task_project.return_value = _project(review_policy="all")
        claude = planner_claude(
            plan(step("s1"), step("s2", "s1")),  # the Planner asks for no review
            decide("review"), decide("accept"),
            "<error>",  # s2's decision fails: its Worker's success still goes to a Reviewer
            decide("accept", summary="done"),
        )
        h = drive(claude, always(WORK_OK, REVIEW_PASS))

        final = h.final()
        assert final is not None and final.payload["status"] == "completed"
        assert [r.payload["step_key"] for r in h.reviews] == ["s1", "s2"]
        decision = claude.turns()[1]["text"]
        assert "needs a passing review" in decision and "Allowed decisions now: review," in decision

    def test_the_project_supervise_interval_beats_the_planner_config(
        self, planner_claude: Any, agent_db: Any,
    ) -> None:
        agent_db.get_task_project.return_value = _project(supervise_interval_seconds=1)  # the config says 300
        claude = planner_claude(plan(step("s1")), reply({"reviews": []}))

        async def scenario(h: Harness) -> None:
            await h.start()
            await h.until(lambda: len(h.held) == 1)
            await h.until(lambda: any(t["text"].startswith("[SUPERVISE]") for t in claude.turns()), timeout=8.0)

        drive(claude, lambda _: None, scenario)

    @pytest.mark.parametrize("action, turns, wrap_up", [
        (None, 2, ""),  # unset: fail at once
        ("fail", 2, ""),
        ("wrap_up", 3, ". Planner's wrap-up: s1 is done; s2 remains"),
    ])
    def test_the_task_stops_at_its_cap_with_the_reason(
        self, planner_claude: Any, agent_db: Any, monkeypatch: pytest.MonkeyPatch,
        action: str | None, turns: int, wrap_up: str,
    ) -> None:
        # Each Planner turn costs $0.25: the plan stays under the $0.4 cap, its first decision crosses it.
        agent_db.get_task_project.return_value = _project(task_budget_usd=0.4, task_budget_action=action)
        _spend_from_records(monkeypatch, agent_db)
        claude = planner_claude(
            plan(step("s1"), step("s2", "s1")), decide("accept"),
            decide("fail", reason="s1 is done; s2 remains"),
        )
        h = drive(claude, always(WORK_OK))

        final = h.final()
        assert final is not None and final.payload["status"] == "failed"
        reason = "Task spend cap reached: $0.5 spent of the $0.4 cap (task_budget_usd)"
        assert final.payload["summary"] == f"任务失败:\n{reason}{wrap_up}"
        assert final.payload["fail_kind"] == "task_budget"
        assert [w.payload["step_key"] for w in h.work_items] == ["s1"]  # s2 never started
        assert len(claude.turns()) == turns
        if wrap_up:
            assert claude.turns()[-1]["text"].startswith(f"[DECISION] {reason}. No more steps run")


    def test_a_task_continued_at_a_higher_cap_resumes_its_plan_and_session(
        self, planner_claude: Any, agent_db: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No new plan: the step the cap cut short runs again, finished steps stay, and Claude resumes."""
        agent_db.get_task_project.return_value = _project(task_budget_usd=0.4)
        _spend_from_records(monkeypatch, agent_db)
        claude = planner_claude(plan(step("s1"), step("s2")), decide("accept"), decide("accept", summary="done"))

        async def scenario(h: Harness) -> None:
            await h.start()
            await h.until(lambda: len(h.work_items) == 2)  # s1 is held, s2 is answered and accepted ($0.5)
            await h.until(lambda: len(claude.turns()) == 2)
            await h.release(WORK_OK)  # s1's result arrives over the cap: dropped, and the task fails
            assert (await h.finished()).payload["fail_kind"] == "task_budget"
            await h.planner.stop()

            agent_db.get_task_project.return_value = _project(task_budget_usd=2)
            agent_db.store.tasks["t1"] = {"requirement": "build the calc package", "work_dir": str(h.work_dir)}
            h.statuses.clear()
            h.planner = PlannerAgent(task_id="t1", runtime_root=h.work_dir.parent, claude_bin=claude.bin)
            await h.planner.start(h.bus)
            await h.bus.publish(MessageEnvelope.create(
                source=TASK_ROUTER_ADDRESS, target=h.planner.agent_address, message_type=MessageType.TASK_REQUEST,
                payload={"requirement": "build the calc package", "work_dir": str(h.work_dir),
                         "project_id": "proj-1", "resume": True},
            ))
            final = await h.finished()
            assert final.payload["status"] == "completed" and final.payload["summary"] == "done"

        h = drive(claude, hold_first(), scenario)
        assert [w.payload["step_key"] for w in h.work_items] == ["s1", "s2", "s1"]
        assert h.work_items[0].payload["task_id"] == h.work_items[2].payload["task_id"]  # the same step
        turns = claude.turns()
        assert len(turns) == 3 and turns[2]["text"].startswith("[DECISION]")  # never planned again
        assert _flag(claude.argvs()[-1], "--resume") == "sess-p"
        assert any(line.startswith("Task continued from its plan with a $2 spend cap: 1 step(s) run again")
                   for line in activity(agent_db))


    def test_a_step_the_cap_kept_from_review_goes_to_a_reviewer_with_its_saved_result(
        self, planner_claude: Any, agent_db: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        agent_db.get_task_project.return_value = _project(task_budget_usd=0.4)
        _spend_from_records(monkeypatch, agent_db)
        claude = planner_claude(plan(step("s1")), decide("review"), decide("accept", summary="done"))

        async def scenario(h: Harness) -> None:
            await h.start()
            await h.finished()  # the review decision crossed the cap: no Reviewer started
            await h.planner.stop()
            assert h.reviews == []

            agent_db.get_task_project.return_value = _project(task_budget_usd=2)
            agent_db.store.tasks["t1"] = {"requirement": "build the calc package", "work_dir": str(h.work_dir)}
            h.statuses.clear()
            h.planner = PlannerAgent(task_id="t1", runtime_root=h.work_dir.parent, claude_bin=claude.bin)
            await h.planner.start(h.bus)
            await h.bus.publish(MessageEnvelope.create(
                source=TASK_ROUTER_ADDRESS, target=h.planner.agent_address, message_type=MessageType.TASK_REQUEST,
                payload={"requirement": "build the calc package", "work_dir": str(h.work_dir),
                         "project_id": "proj-1", "resume": True},
            ))
            assert (await h.finished()).payload["status"] == "completed"

        h = drive(claude, always(WORK_OK, REVIEW_PASS), scenario)
        assert len(h.work_items) == 1
        (review,) = h.reviews
        assert review.payload["work_result"]["summary"] == "did it"  # the Worker's result, saved with its step
        assert len(claude.turns()) == 3


# --- Feature-off parity ---


def _normalized(argv: list[str], prompt: str, root: str) -> tuple[list[str], str]:
    """The argv and the system prompt without the temporary directory (the prompt file is named by its hash)."""
    argv = [arg.replace(root, "<root>") for arg in argv]
    argv[argv.index("--append-system-prompt-file") + 1] = "<prompt file>"
    return argv, prompt.replace(root, "<root>")


@pytest.mark.usefixtures("agent_db")
def test_a_project_without_settings_runs_as_before(
    planner_claude: Any, agent_db: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NULL settings give the argv a project row from before M5 (without the columns) gives, and the
    Planner, Workers and Reviewers behave as before: no cap, the default model, no forced review, and the
    task's cost is never read."""
    task_costs = AsyncMock()
    monkeypatch.setattr(db_llm_records, "task_costs", task_costs)
    before = {k: v for k, v in _project().items() if k not in UNSET}

    planner_argvs, worker_argvs = [], []
    for row in (before, _project()):
        agent_db.get_task_project.return_value = row
        claude = planner_claude(plan(step("s1")), decide("accept", summary="done"))
        h = drive(claude, always(WORK_OK))  # no Reviewer: one would fail ``always``
        assert h.final() is not None and h.final().payload["status"] == "completed"
        planner_argvs.append(_normalized(claude.argvs()[0], claude.prompts()[0], str(h.work_dir.parent)))
        for verify in (False, True):
            _, seen = _run_step(verify=verify)
            worker_argvs.append(_normalized(seen["argv"], seen["prompt"], seen["work_dir"]))

    assert planner_argvs[0] == planner_argvs[1] and worker_argvs[:2] == worker_argvs[2:]
    for argv, _ in planner_argvs + worker_argvs:
        assert "--max-budget-usd" not in argv and _flag(argv, "--model") == "opus"
    task_costs.assert_not_awaited()
    metadata = agent_db.start_record.await_args.kwargs["metadata"]
    assert metadata["model_choice"]["source"] == "builtin" and "max_budget_usd" not in metadata
