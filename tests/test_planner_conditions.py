"""Conditions on the real Planner event path with scripted agents."""
from __future__ import annotations

from typing import Any

import pytest

from looper.agents.tiers.planner import PlanStep, TaskPlan
from tests.test_planner_agent import REVIEW_PASS, WORK_OK, _project, always, chat, decide, drive, plan, step


def test_any_of_dispatches_without_cancelling_the_other_source(planner_claude: Any, agent_db: Any) -> None:
    agent_db.get_task_project.return_value = {**_project(), "dependency_conditions": "on"}
    claude = planner_claude(
        plan(step("s1"), step("s2"), {**step("s3", "s1", "s2"), "ready_when": {"any_of": ["s1", "s2"]}}),
        decide("accept"), decide("accept"), decide("accept"),
    )

    async def scenario(h: Any) -> None:
        await h.start()
        await h.until(lambda: len(h.work_items) == 3)
        assert [item.payload["step_key"] for item in h.held] == ["s2"]
        assert h.final() is None and not h.killed
        await h.release(WORK_OK)
        await h.finished()

    h = drive(claude, lambda item: None if item.payload["step_key"] == "s2" else WORK_OK, scenario)
    assert h.final().payload["status"] == "completed"
    rows = {row["key"]: row for row in agent_db.store.steps["t1"].values()}
    assert rows["s3"]["ready_when"] == {"any_of": [rows["s1"]["id"], rows["s2"]["id"]]}


def test_cancelled_gate_asks_before_skipping_a_passed_dependent(planner_claude: Any, agent_db: Any) -> None:
    agent_db.get_task_project.return_value = {**_project(), "dependency_conditions": "on", "user_gates": "on"}
    gate = {"id": "g1", "kind": "gate", "topic": "Choose source", "depends_on": [],
            "agenda": [{"question": "Which source?"}]}
    claude = planner_claude(
        plan(step("s1"), gate, {**step("s4", "g1"), "ready_when": {"step": "g1", "on": "passed"}}),
        decide("accept", gates=[{"id": "g1", "action": "cancel", "resolution": "Source unavailable"}]),
        chat("Discard the blocked step."),
    )
    h = drive(claude, always(WORK_OK))
    assert h.final().payload["status"] == "completed"
    assert [item.payload["step_key"] for item in h.work_items] == ["s1"]
    assert "[CHAT]" in claude.turns()[2]["text"]
    assert "Step s4 can no longer start" in claude.turns()[2]["text"]
    assert "g1" in claude.turns()[2]["text"] and "cancelled" in claude.turns()[2]["text"]
    rows = {row["key"]: row for row in agent_db.store.steps["t1"].values()}
    assert rows["s4"]["status"] == "skipped"
    journal = agent_db.store.journal["t1"]
    assert sum(entry["kind"] == "condition_blocked" for entry in journal) == 1
    assert any(entry["kind"] == "decision" and entry["step_id"] == rows["s4"]["id"] for entry in journal)


@pytest.mark.parametrize("action", ["replan", "add_steps"])
def test_an_unresolved_condition_reply_retries_without_another_event(
    planner_claude: Any, agent_db: Any, action: str,
) -> None:
    agent_db.get_task_project.return_value = {**_project(), "dependency_conditions": "on", "user_gates": "on"}
    gate = {"id": "g1", "kind": "gate", "topic": "Choose source", "depends_on": [],
            "agenda": [{"question": "Which source?"}]}
    blocked = {**step("s2", "g1"), "ready_when": {"step": "g1", "on": "passed"}}
    claude = planner_claude(
        plan(gate, blocked),
        chat("Cancel the source gate.", gates=[
            {"id": "g1", "action": "cancel", "resolution": "Source unavailable"}]),
        chat("Keep the blocked branch.", action, steps=[blocked] if action == "replan" else [step("s3", "s2")]),
        chat("Skip s2 because the gate cannot pass."),
    )

    async def scenario(h: Any) -> None:
        await h.start()
        await h.until(lambda: h.planner._plan is not None and h.planner._plan.waiting_gates())
        await h.say("Cancel g1.")
        await h.finished()

    h = drive(claude, always(WORK_OK), scenario)
    assert h.final().payload["status"] == "completed"
    assert h.work_items == []
    assert "[CHAT] Step s2 can no longer start" in claude.turns()[2]["text"]
    assert "broke the reply contract" in claude.turns()[3]["text"]
    assert "s2" in claude.turns()[3]["text"] and "unsatisfiable" in claude.turns()[3]["text"]
    rows = {row["key"]: row for row in agent_db.store.steps["t1"].values()}
    assert rows["s2"]["status"] == "skipped"
    if action == "add_steps":
        assert "s3" not in rows  # The rejected reply must not change the plan.
    assert sum(entry["kind"] == "condition_blocked" and entry["step_id"] == rows["s2"]["id"]
               for entry in agent_db.store.journal["t1"]) == 1


@pytest.mark.parametrize("failure", ["replan", "add_steps", "runtime_error"])
def test_a_condition_without_inflight_work_fails_after_an_unresolved_turn(
    planner_claude: Any, agent_db: Any, failure: str,
) -> None:
    agent_db.get_task_project.return_value = {**_project(), "dependency_conditions": "on", "user_gates": "on"}
    gate = {"id": "g1", "kind": "gate", "topic": "Choose source", "depends_on": [],
            "agenda": [{"question": "Which source?"}]}
    blocked = {**step("s2", "g1"), "ready_when": {"step": "g1", "on": "passed"}}
    unresolved = (chat("Keep the blocked branch.", failure,
                       steps=[blocked] if failure == "replan" else [step("s3", "s2")])
                  if failure != "runtime_error" else "<error>")
    claude = planner_claude(
        plan(gate, blocked),
        chat("Cancel the source gate.", gates=[
            {"id": "g1", "action": "cancel", "resolution": "Source unavailable"}]),
        unresolved, unresolved, "<error>",
    )

    async def scenario(h: Any) -> None:
        await h.start()
        await h.until(lambda: h.planner._plan is not None and h.planner._plan.waiting_gates())
        await h.say("Cancel g1.")
        await h.finished()

    h = drive(claude, always(WORK_OK), scenario)
    assert h.final().payload["status"] == "failed"
    assert "condition" in h.final().payload["summary"]
    assert len(claude.turns()) == (3 if failure == "runtime_error" else 4)
    assert h.work_items == []
    rows = {row["key"]: row for row in agent_db.store.steps["t1"].values()}
    assert rows["s2"]["status"] == "pending" and "s3" not in rows
    assert not any(entry["kind"] == "decision" and entry["step_id"] == rows["s2"]["id"]
                   for entry in agent_db.store.journal["t1"])


def test_condition_replans_cannot_keep_replacing_the_blocked_step_forever(
    planner_claude: Any, agent_db: Any, monkeypatch: Any,
) -> None:
    agent_db.get_task_project.return_value = {**_project(), "dependency_conditions": "on", "user_gates": "on"}
    gate = {"id": "g1", "kind": "gate", "topic": "Choose source", "depends_on": [],
            "agenda": [{"question": "Which source?"}]}
    claude = planner_claude(
        plan(gate, {**step("s2", "g1"), "ready_when": {"step": "g1", "on": "passed"}}),
        chat("Cancel the source gate.", gates=[
            {"id": "g1", "action": "cancel", "resolution": "Source unavailable"}]),
    )
    attempts = 0

    async def scenario(h: Any) -> None:
        nonlocal attempts
        await h.start()
        await h.until(lambda: h.planner._plan is not None and h.planner._plan.waiting_gates())
        turn = h.planner._turn

        async def replace_blocked(message: str) -> str:
            nonlocal attempts
            if "can no longer start" not in message:
                return await turn(message)
            attempts += 1
            assert attempts <= 8, "condition recovery kept creating blocked steps without yielding"
            return chat("Replace the blocked branch.", "replan", steps=[{
                **step(f"replacement{attempts}", "g1"), "ready_when": {"step": "g1", "on": "passed"},
            }])

        monkeypatch.setattr(h.planner, "_turn", replace_blocked)
        await h.say("Cancel g1.")
        await h.finished()

    h = drive(claude, always(WORK_OK), scenario)
    assert h.final().payload["status"] == "failed"
    assert "condition" in h.final().payload["summary"]
    assert 1 < attempts <= 8
    assert h.work_items == []


def test_unconditional_ready_steps_keep_all_verified_semantics() -> None:
    source = PlanStep("a", 0, "source", status="completed")
    dependent = PlanStep("b", 1, "dependent", depends_on=["a"])
    plan_ = TaskPlan("p", "g", "build", "", steps=[source, dependent])
    assert plan_.ready_steps() == []
    source.status = "verified"
    assert plan_.ready_steps() == [dependent]


def test_completed_condition_starts_while_the_source_is_being_reviewed(planner_claude: Any, agent_db: Any) -> None:
    from looper.agents.message import MessageType

    agent_db.get_task_project.return_value = {**_project(), "dependency_conditions": "on"}
    claude = planner_claude(
        plan(step("s1", review=True), {**step("s2", "s1"), "ready_when": {"step": "s1", "on": "completed"}}),
        decide("review"), decide("accept"), decide("accept"),
    )

    async def scenario(h: Any) -> None:
        await h.start()
        await h.until(lambda: len(h.work_items) == 2 and len(claude.turns()) >= 3)
        assert h.final() is None
        assert len(h.held) == 1 and h.held[0].message_type is MessageType.VERIFY_REQUEST
        await h.release(REVIEW_PASS)
        await h.finished()

    h = drive(claude, lambda item: None if item.message_type is MessageType.VERIFY_REQUEST else WORK_OK, scenario)
    assert h.final().payload["status"] == "completed"


@pytest.mark.parametrize("explicit_condition", [True, False])
def test_skipping_an_impossible_condition_cascades_to_its_dependents(
    planner_claude: Any, agent_db: Any, explicit_condition: bool,
) -> None:
    agent_db.get_task_project.return_value = {**_project(), "dependency_conditions": "on", "user_gates": "on"}
    gate = {"id": "g1", "kind": "gate", "topic": "Choose source", "depends_on": [],
            "agenda": [{"question": "Which source?"}]}
    dependent = step("s5", "s4")
    if explicit_condition:
        dependent["ready_when"] = "s4"
    claude = planner_claude(
        plan(step("s1"), gate, {**step("s4", "g1"), "ready_when": {"step": "g1", "on": "passed"}},
             dependent, step("s6", "s5")),
        decide("accept", gates=[{"id": "g1", "action": "cancel", "resolution": "Source unavailable"}]),
        chat("Skip s4."), chat("Skip s5 too."), chat("Skip s6 too."),
    )
    h = drive(claude, always(WORK_OK))
    assert h.final().payload["status"] == "completed"
    assert [item.payload["step_key"] for item in h.work_items] == ["s1"]
    assert "Step s4 can no longer start" in claude.turns()[2]["text"]
    assert "Step s5 can no longer start" in claude.turns()[3]["text"]
    assert "s4: step is skipped" in claude.turns()[3]["text"]
    assert "Step s6 can no longer start" in claude.turns()[4]["text"]
    rows = {row["key"]: row for row in agent_db.store.steps["t1"].values()}
    assert rows["s4"]["status"] == rows["s5"]["status"] == rows["s6"]["status"] == "skipped"
    assert sum(entry["kind"] == "condition_blocked" for entry in agent_db.store.journal["t1"]) == 3


def test_a_missing_ordinary_dependency_asks_before_skipping(agent_db: Any, monkeypatch: Any, tmp_path: Any) -> None:
    import asyncio

    from looper.agents.tiers.planner import PlannerAgent

    blocked = PlanStep("dependent", 0, "Needs missing output", depends_on=["missing"])
    plan_ = TaskPlan("p", "g", "build", "", steps=[blocked], status="executing")
    agent = PlannerAgent(task_id="t1", runtime_root=tmp_path)

    async def answer(kind: Any, text: str, check: Any) -> Any:
        assert "[CHAT] Step dependent can no longer start" in text
        assert "missing: step no longer exists" in text
        assert blocked.status == "pending"
        return check(chat("Skip the step without its required output."))

    monkeypatch.setattr(agent, "_ask", answer)
    assert asyncio.run(agent._blocked_conditions(plan_))
    assert plan_.is_complete and blocked.status == "skipped"
    assert [entry["kind"] for entry in agent_db.store.journal["t1"]] == ["condition_blocked", "decision"]


def test_a_failed_condition_turn_remains_pending_and_retries_on_the_next_event(planner_claude: Any, agent_db: Any) -> None:
    agent_db.get_task_project.return_value = {**_project(), "dependency_conditions": "on", "user_gates": "on"}
    gate = {"id": "g1", "kind": "gate", "topic": "Choose source", "depends_on": [],
            "agenda": [{"question": "Which source?"}]}
    claude = planner_claude(
        plan(step("s1"), step("s2"), gate,
             {**step("s4", "g1"), "ready_when": {"step": "g1", "on": "passed"}}),
        decide("accept", gates=[{"id": "g1", "action": "cancel", "resolution": "Source unavailable"}]),
        "<error>", decide("accept"), chat("Skip the blocked step."),
    )

    async def scenario(h: Any) -> None:
        await h.start()
        await h.until(lambda: len(claude.turns()) >= 3)
        assert h.final() is None
        rows = {row["key"]: row for row in agent_db.store.steps["t1"].values()}
        assert rows["s4"]["status"] == "pending"
        await h.release(WORK_OK)
        await h.finished()

    h = drive(claude, lambda item: None if item.payload["step_key"] == "s2" else WORK_OK, scenario)
    assert h.final().payload["status"] == "completed"
    assert "Step s4 can no longer start" in claude.turns()[4]["text"]
    assert sum(entry["kind"] == "condition_blocked" for entry in agent_db.store.journal["t1"]) == 1


def test_a_blocked_condition_replan_can_stop_an_unneeded_running_branch(planner_claude: Any, agent_db: Any) -> None:
    agent_db.get_task_project.return_value = {**_project(), "dependency_conditions": "on", "user_gates": "on"}
    gate = {"id": "g1", "kind": "gate", "topic": "Choose source", "depends_on": [],
            "agenda": [{"question": "Which source?"}]}
    claude = planner_claude(
        plan(step("s1"), step("s2"), gate,
             {**step("s4", "g1"), "ready_when": {"step": "g1", "on": "passed"}}),
        decide("accept", gates=[{"id": "g1", "action": "cancel", "resolution": "Source unavailable"}]),
        chat("Use another source.", "replan", stop=["s2"], steps=[
            {**step("s5", "g1"), "ready_when": {"step": "g1", "on": "completed"}}]),
        decide("accept"),
    )
    h = drive(claude, lambda item: None if item.payload["step_key"] == "s2" else WORK_OK)
    assert h.final().payload["status"] == "completed"
    assert len(h.killed) == 1
    rows = {row["key"]: row for row in agent_db.store.steps["t1"].values()}
    assert rows["s2"]["status"] == rows["s4"]["status"] == "skipped"
    assert rows["s5"]["status"] == "verified"


def test_default_planner_system_prompt_matches_the_released_bytes() -> None:
    from pathlib import Path

    from looper.agents.agent_settings import Harness, Layer
    from looper.agents.knowledge import EMPTY_KNOWLEDGE
    from looper.agents.tiers.planner import PlannerAgent, PlannerSettings

    agent = PlannerAgent(task_id="task-baseline", runtime_root=Path("."))
    prompt = agent._system_prompt(
        TaskPlan("p", "g", "Build the project", ""), PlannerSettings("Plan carefully.", 60, 0, 0),
        EMPTY_KNOWLEDGE, Harness("Work carefully.", Layer.BUILTIN),
    )
    assert prompt.sha256 == "9bc15086404f4efa8254e7b02da2bca0f7a334c9ebd2e83c17b1cfa3a0f90693"


def test_replan_preserves_a_started_condition_and_replaces_a_pending_condition(planner_claude: Any, agent_db: Any) -> None:
    agent_db.get_task_project.return_value = {**_project(), "dependency_conditions": "on"}
    claude = planner_claude(
        plan(step("s1"), step("s2"), {**step("s3", "s1", "s2"), "ready_when": {"any_of": ["s1", "s2"]}},
             {**step("s4", "s3"), "ready_when": "s3"}),
        decide("accept"),
        chat("Wait for both source results.", "replan", steps=[
            {**step("s4", "s2", "s3"), "ready_when": {"all_of": ["s2", "s3"]}}]),
        decide("accept"), decide("accept"), decide("accept"),
    )

    async def scenario(h: Any) -> None:
        await h.start()
        await h.until(lambda: len(h.held) == 2)
        original = next(s for s in h.planner._plan.steps if s.label == "s3").ready_when
        await h.say("Require both results for the final step.")
        assert next(s for s in h.planner._plan.steps if s.label == "s3").ready_when == original
        assert len(h.work_items) == 3
        await h.release(WORK_OK)
        await h.release(WORK_OK)
        await h.finished()

    h = drive(claude, lambda item: None if item.payload["step_key"] in ("s2", "s3") else WORK_OK, scenario)
    assert h.final().payload["status"] == "completed"
    rows = {row["key"]: row for row in agent_db.store.steps["t1"].values()}
    assert rows["s3"]["ready_when"] == {"any_of": [rows["s1"]["id"], rows["s2"]["id"]]}
    assert rows["s4"]["ready_when"] == {"all_of": [rows["s2"]["id"], rows["s3"]["id"]]}
