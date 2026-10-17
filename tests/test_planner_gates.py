"""A task's Planner and its user gates: a gate opens once its dependencies are done and waits for the user while the
rest of the plan runs; the user's messages reach the Planner in [GATE] turns; closing the gate keeps its summary as
a supplement and plans what follows; a decision may update or cancel a gate.

The Planner's Claude is a stand-in that answers each turn with a scripted reply (``planner_claude``); the Workers
are a stand-in pool (``tests.test_planner_agent.Harness``); the gates and their messages are kept in memory
(``agent_db.store``).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from looper.agents.message import MessageEnvelope, MessageType, planner_address
from looper.agents.tiers import planner as planner_module
from looper.agents.tiers.planner_protocol import reply_contract
from tests.test_planner_agent import (
    FINAL,
    WEB,
    WORK_OK,
    _project,
    activity,
    decide,
    drive,
    hold_first,
    always,
    plan,
    reply,
    step,
)

GATE = {"id": "g1", "kind": "gate", "topic": "storage", "depends_on": [],
        "agenda": [{"question": "Which database?", "options": ["PostgreSQL", "SQLite"]}]}
OPEN_Q1 = [{"id": "q1", "question": "Which database?", "status": "open"}]
SETTLED = [{"id": "q1", "question": "Which database?", "status": "resolved", "answer": "PostgreSQL"}]
SUMMARY = {"decisions": ["PostgreSQL"], "assumptions": [], "open": []}


def gate_reply(answer: str, verdict: str = "continue", agenda: list[dict[str, Any]] | None = None,
               **extra: Any) -> str:
    return reply({"reply": answer, "gate": verdict, "agenda": agenda or OPEN_Q1, "action": "none", **extra})


@pytest.fixture
def gates_on(agent_db: Any) -> None:
    """The task's project lets its Planner open user gates."""
    agent_db.get_task_project.return_value = {**_project(), "user_gates": "on"}


def _gate(agent_db: Any) -> dict[str, Any] | None:
    return next(iter(agent_db.store.gates.get("t1", {}).values()), None)


def _messages(agent_db: Any) -> list[tuple[str, str]]:
    return [(m["role"], m["kind"]) for m in agent_db.store.gate_messages]


async def _write(h: Any, agent_db: Any, text: str, kind: str = "message") -> None:
    """The user writes ``text`` in gate g1 on the dashboard (``gate_service.post_message``)."""
    gate, added = await agent_db.store.add_gate_message("t1", "g1", {"kind": kind, "text": text, "via": "dashboard"})
    assert added is not None, f"the gate does not wait for the user: {gate}"
    await h.bus.publish(MessageEnvelope.create(source=WEB, target=planner_address("t1"),
                                               message_type=MessageType.GATE_MESSAGE,
                                               payload={"task_id": "t1", "gate": "g1"}))


def _statuses(h: Any) -> list[str]:
    return [e.payload["status"] for e in h.statuses if e.message_type == MessageType.GOAL_STATUS]


def test_the_user_decides_at_a_gate_while_another_branch_runs_and_closing_it_plans_the_rest(
    planner_claude: Any, agent_db: Any, gates_on: None,
) -> None:
    claude = planner_claude(
        plan(step("s1"), GATE),
        reply({"reply": "PostgreSQL it is; I'll build on it.", "gate": "close", "agenda": SETTLED, "summary": SUMMARY,
               "action": "add_steps", "steps": [step("s2", "g1")]}),
        decide("accept", summary="s2 built"),
        decide("accept", summary="s1 explored"),
    )

    async def scenario(h: Any) -> None:
        await h.start()
        await h.until(lambda: (_gate(agent_db) or {}).get("state") == "open" and h.work_items)
        await _write(h, agent_db, "Use PostgreSQL")
        await h.until(lambda: len(h.work_items) == 2 and len(claude.turns()) >= 3)
        await h.release(WORK_OK)
        await h.finished()

    h = drive(claude, hold_first(), scenario)

    assert h.final().payload["status"] == "completed"  # type: ignore[union-attr]
    # s1 explored while the gate waited for the user; the task waited for the user meanwhile.
    assert h.dispatched_at_turn[0] == 1 and "waiting_user" in _statuses(h)
    texts = [turn["text"] for turn in claude.turns()]
    assert texts[1].startswith('[GATE] Gate g1 "storage": discussing, opened 0 min ago, turn 1.')
    assert "- q1 [open] Which database? (options: PostgreSQL / SQLite)" in texts[1]
    assert "- (dashboard) Use PostgreSQL" in texts[1] and "Running now: s1 (" in texts[1]
    # What was settled reaches the step planned after the gate, as a supplement and as its dependency's report.
    conclusion = "Settled with the user at gate g1 (storage):\nDecisions:\n- PostgreSQL"
    s2 = h.work_items[1]
    assert s2.payload["step_key"] == "s2" and conclusion in s2.payload["supplements"]
    assert s2.payload["dependencies"][0]["step"] == "g1"
    gate = _gate(agent_db)
    assert gate is not None and (gate["state"], gate["turns"], gate["summary"]) == ("passed", 1, SUMMARY)
    assert gate["plan_change"] == {"added": ["s2"], "dropped": [], "stopped": []}
    user = next(m for m in agent_db.store.gate_messages if m["role"] == "user")
    assert gate["delivered_through"] == user["id"]
    assert _messages(agent_db) == [("looper", "opened"), ("user", "message"), ("planner", "reply"),
                                   ("looper", "closed")]
    kinds = [(e["kind"], e.get("via", "")) for e in agent_db.store.journal["t1"] if e["kind"] != "progress"]
    assert ("gate_opened", "") in kinds and ("supplement", "gate") in kinds and ("gate_closed", "") in kinds
    assert "Gate g1 passed (the Planner closed it): 1 decision(s), 0 assumption(s); plan: added s2" in \
        activity(agent_db)
    # The contract says how to open and discuss gates, since the project lets the Planner open them.
    assert "[GATE]: the user wrote in a gate's discussion" in claude.prompts()[0]


def test_without_user_gates_a_gate_is_refused_and_the_contract_is_as_before(
    planner_claude: Any, agent_db: Any,
) -> None:
    claude = planner_claude(plan(GATE), plan(step("s1")), decide("accept"))
    h = drive(claude, always(WORK_OK))

    assert h.final().payload["status"] == "completed"  # type: ignore[union-attr]
    repair = claude.turns()[1]["text"]
    assert "step g1: this project does not let you open gates (user_gates is off)" in repair
    assert reply_contract(2) in claude.prompts()[0] and "[GATE]" not in claude.prompts()[0]


def test_a_task_a_schedule_started_may_not_open_a_gate(planner_claude: Any, agent_db: Any) -> None:
    agent_db.get_task_project.return_value = {**_project(), "user_gates": "on", "task_schedule_id": "sched-1"}
    claude = planner_claude(plan(GATE), plan(step("s1")), decide("accept"))
    h = drive(claude, always(WORK_OK))

    assert h.final().payload["status"] == "completed"  # type: ignore[union-attr]
    assert "this task runs unattended (a schedule started it), so it cannot wait for the user" in \
        claude.turns()[1]["text"]
    assert "[GATE]" not in claude.prompts()[0]


def test_when_the_user_ends_the_discussion_the_planner_closes_it_on_stated_assumptions(
    planner_claude: Any, agent_db: Any, gates_on: None,
) -> None:
    assumed = {"decisions": [], "assumptions": ["SQLite, as the requirement does not say"], "open": []}
    claude = planner_claude(
        plan(GATE),
        gate_reply("Shall I keep asking?"),  # refused: the user ended the discussion
        reply({"reply": "Going on with SQLite.", "gate": "close", "agenda": OPEN_Q1, "summary": assumed,
               "action": "add_steps", "steps": [step("s1", "g1")]}),
        decide("accept"),
    )

    async def scenario(h: Any) -> None:
        await h.start()
        await h.until(lambda: (_gate(agent_db) or {}).get("state") == "open")
        await _write(h, agent_db, "", kind="end")
        await h.finished()

    h = drive(claude, always(WORK_OK), scenario)

    assert h.final().payload["status"] == "completed"  # type: ignore[union-attr]
    texts = [turn["text"] for turn in claude.turns()]
    assert "The user ended the discussion and asked you to go on with the current conclusions" in texts[1]
    assert "(dashboard, ended the discussion)" in texts[1]
    assert 'the user ended the discussion, so gate must be "close"' in texts[2]
    gate = _gate(agent_db)
    assert gate is not None and gate["ended_by_user"] is True and gate["summary"] == assumed
    assert "Assumptions (not confirmed by the user):\n- SQLite, as the requirement does not say" in \
        h.work_items[0].payload["supplements"][0]


def test_closing_the_last_open_gate_without_new_steps_completes_the_task_with_its_final_summary(
    planner_claude: Any, agent_db: Any, gates_on: None,
) -> None:
    claude = planner_claude(
        plan(GATE),
        gate_reply("Nothing more to build.", "close", SETTLED, summary=SUMMARY),  # refused: it needs "final"
        gate_reply("Nothing more to build.", "close", SETTLED, summary=SUMMARY, final=FINAL),
    )

    async def scenario(h: Any) -> None:
        await h.start()
        await h.until(lambda: (_gate(agent_db) or {}).get("state") == "open")
        await _write(h, agent_db, "PostgreSQL, and that's all")
        await h.finished()

    h = drive(claude, always(WORK_OK), scenario)

    final = h.final()
    assert final is not None and final.payload["status"] == "completed" and "## Delivered" in final.payload["summary"]
    texts = [turn["text"] for turn in claude.turns()]
    assert "This gate is the task's last open step: closing it without adding steps completes the task" in texts[1]
    assert "closing this gate without adding steps completes the task" in texts[2]
    assert not h.work_items


def test_a_decision_cancels_a_gate_the_exploration_answered_and_its_dependents_run(
    planner_claude: Any, agent_db: Any, gates_on: None,
) -> None:
    claude = planner_claude(
        plan(step("s1"), GATE, step("s2", "g1")),
        decide("accept", summary="found PostgreSQL in use",
               gates=[{"id": "g1", "action": "cancel", "resolution": "db/schema.sql already uses PostgreSQL"}]),
        decide("accept"),
    )
    h = drive(claude, always(WORK_OK))

    assert h.final().payload["status"] == "completed"  # type: ignore[union-attr]
    assert [w.payload["step_key"] for w in h.work_items] == ["s1", "s2"]
    assert not any(turn["text"].startswith("[GATE]") for turn in claude.turns())
    gate = _gate(agent_db)
    assert gate is not None and (gate["state"], gate["resolution"]) == (
        "cancelled", "db/schema.sql already uses PostgreSQL")
    assert _messages(agent_db) == [("looper", "opened"), ("looper", "cancelled")]
    assert "Gate g1 cancelled: db/schema.sql already uses PostgreSQL" in activity(agent_db)


def test_a_decision_that_cancels_the_only_other_open_gate_completes_the_task(
    planner_claude: Any, agent_db: Any, gates_on: None,
) -> None:
    claude = planner_claude(
        plan(step("s1"), GATE),
        decide("accept", summary="found it", gates=[{"id": "g1", "action": "cancel", "resolution": "s1 found it"}]),
    )
    h = drive(claude, always(WORK_OK))

    final = h.final()
    assert final is not None and final.payload["status"] == "completed" and "## Delivered" in final.payload["summary"]
    assert "The task's other open steps are gates (g1): a decision that cancels all of them and completes the task" \
        in claude.turns()[1]["text"]


def test_a_decision_updates_an_open_gate_and_the_user_is_told_what_changed(
    planner_claude: Any, agent_db: Any, gates_on: None,
) -> None:
    claude = planner_claude(
        plan(step("s1"), GATE, step("s2", "g1")),
        decide("accept", summary="explored",
               gates=[{"id": "g1", "action": "update", "findings": "the service runs PostgreSQL 15",
                       "note": "s1 found PostgreSQL 15 in use"}]),
        gate_reply("Then PostgreSQL.", "close", SETTLED, summary=SUMMARY),
        decide("accept"),
    )

    async def scenario(h: Any) -> None:
        await h.start()
        await h.until(lambda: ("looper", "updated") in _messages(agent_db))
        await _write(h, agent_db, "Fine, PostgreSQL")
        await h.finished()

    h = drive(claude, always(WORK_OK), scenario)

    assert h.final().payload["status"] == "completed"  # type: ignore[union-attr]
    update = next(m for m in agent_db.store.gate_messages if m["kind"] == "updated")
    assert update["text"] == "s1 found PostgreSQL 15 in use"
    assert "Your findings so far: the service runs PostgreSQL 15" in claude.turns()[2]["text"]
    assert [w.payload["step_key"] for w in h.work_items] == ["s1", "s2"]


def test_a_replan_keeps_an_open_gate_and_drops_one_that_has_not_opened(
    planner_claude: Any, agent_db: Any, gates_on: None,
) -> None:
    later = {**GATE, "id": "g2", "depends_on": ["s1"]}
    claude = planner_claude(
        plan(step("s1"), GATE, later),
        decide("replan", reason="the second question is moot", steps=[step("s3", "g1")]),
    )

    async def scenario(h: Any) -> None:
        await h.start()
        await h.until(lambda: any(t["text"].startswith("[DECISION]") for t in claude.turns())
                      and planner_module.WAITING_USER in _statuses(h)
                      and "Plan changed" in " ".join(activity(agent_db)))

    h = drive(claude, always(WORK_OK), scenario)

    gates = {row["step_id"]: row for row in agent_db.store.gates["t1"].values()}
    by_key = {agent_db.store.steps["t1"][step_id]["key"]: row["state"] for step_id, row in gates.items()}
    assert by_key == {"g1": "open", "g2": "skipped"}
    assert [w.payload["step_key"] for w in h.work_items] == ["s1"]  # s3 waits for g1


def test_a_gate_turn_that_fails_tells_the_user_and_keeps_the_message_for_the_next_turn(
    planner_claude: Any, agent_db: Any, gates_on: None,
) -> None:
    broken = reply({"reply": "hmm", "gate": "continue", "action": "none"})  # no agenda
    claude = planner_claude(
        plan(GATE),
        broken, broken,
        gate_reply("Both noted: PostgreSQL, kept 90 days.", "close", SETTLED, summary=SUMMARY,
                   action="add_steps", steps=[step("s1", "g1")]),
        decide("accept"),
    )

    async def scenario(h: Any) -> None:
        await h.start()
        await h.until(lambda: (_gate(agent_db) or {}).get("state") == "open")
        await _write(h, agent_db, "PostgreSQL")
        await h.until(lambda: ("looper", "error") in _messages(agent_db))
        await _write(h, agent_db, "and keep 90 days")
        await h.finished()

    drive(claude, always(WORK_OK), scenario)

    error = next(m for m in agent_db.store.gate_messages if m["kind"] == "error")
    assert error["text"].startswith("The Planner could not take this (invalid reply after a repair turn: agenda is "
                                    "required")
    texts = [turn["text"] for turn in claude.turns()]
    assert "- (dashboard) PostgreSQL\n- (dashboard) and keep 90 days" in texts[3]


def test_while_the_task_only_waits_for_the_user_its_claude_stops_and_the_next_turn_resumes_the_session(
    planner_claude: Any, agent_db: Any, gates_on: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(planner_module, "GATE_IDLE_STOP_SEC", 0)
    monkeypatch.setattr(planner_module, "GATE_IDLE_CHECK_SEC", 0.05)
    claude = planner_claude(
        plan(GATE),
        gate_reply("PostgreSQL then.", "close", SETTLED, summary=SUMMARY, action="add_steps",
                   steps=[step("s1", "g1")]),
        decide("accept"),
    )
    stopped = "Planner Claude Code stopped while the task waits for the user; the next turn resumes its session"

    async def scenario(h: Any) -> None:
        await h.start()
        await h.until(lambda: stopped in activity(agent_db))
        await _write(h, agent_db, "PostgreSQL")
        await h.finished()

    drive(claude, always(WORK_OK), scenario)

    argvs = claude.argvs()
    assert len(argvs) == 2 and "--resume" not in argvs[0]
    assert argvs[1][argvs[1].index("--resume") + 1] == "sess-p"


def test_while_a_step_runs_the_planners_claude_stays_however_long_the_gate_waits(
    planner_claude: Any, agent_db: Any, gates_on: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(planner_module, "GATE_IDLE_STOP_SEC", 0)
    monkeypatch.setattr(planner_module, "GATE_IDLE_CHECK_SEC", 0.02)
    claude = planner_claude(plan(step("s1"), GATE))

    async def scenario(h: Any) -> None:
        await h.start()
        await h.until(lambda: h.held and (_gate(agent_db) or {}).get("state") == "open")
        await asyncio.sleep(0.3)  # many checks: s1 runs, so the task does not only wait for the user

    drive(claude, hold_first(), scenario)

    assert not any("stopped while the task waits" in line for line in activity(agent_db))
    assert len(claude.argvs()) == 1


def test_a_message_written_while_the_gate_turn_runs_reaches_the_next_one(
    planner_claude: Any, agent_db: Any, gates_on: None,
) -> None:
    claude = planner_claude(
        plan(GATE),
        gate_reply("Which version?"),
        gate_reply("PostgreSQL 16.", "close", SETTLED, summary=SUMMARY, action="add_steps", steps=[step("s1", "g1")]),
        decide("accept"),
    )

    async def scenario(h: Any) -> None:
        await h.start()
        await h.until(lambda: (_gate(agent_db) or {}).get("state") == "open")
        await _write(h, agent_db, "PostgreSQL")
        await h.until(lambda: len(claude.turns()) >= 2)
        await _write(h, agent_db, "16")
        await h.finished()

    drive(claude, always(WORK_OK), scenario)

    texts = [turn["text"] for turn in claude.turns()]
    assert "- (dashboard) PostgreSQL" in texts[1] and "16" not in texts[1].split("New messages")[1].split("\n\n")[0]
    assert "- (dashboard) 16" in texts[2] and texts[2].startswith('[GATE] Gate g1 "storage": discussing')
    assert "turn 2." in texts[2].splitlines()[0]


def test_the_plan_lines_show_a_gate_with_its_state_and_agenda(planner_claude: Any, agent_db: Any,
                                                              gates_on: None) -> None:
    # A project that reviews every step reviews no gate.
    agent_db.get_task_project.return_value = {**_project(), "user_gates": "on", "review_policy": "all"}
    claude = planner_claude(plan(step("s1"), GATE), decide("review"))

    async def scenario(h: Any) -> None:
        await h.start()
        await h.until(lambda: any(t["text"].startswith("[DECISION]") for t in claude.turns()))
        await asyncio.sleep(0.1)

    drive(claude, always(WORK_OK), scenario)

    decision = next(t["text"] for t in claude.turns() if t["text"].startswith("[DECISION]"))
    assert '- g1 [gate: open, waited 0 min] "storage" (depends on: none; agenda: 0 of 1 resolved)' in decision
    reviewed = {row["key"]: row["review"] for row in agent_db.store.steps["t1"].values()}
    assert reviewed == {"s1": True, "g1": False}
