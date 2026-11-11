"""Plan views are substituted at the resident turn boundary and committed only on success."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from looper.agents.runtimes import CLAUDE_CODE, Capability
from looper.agents.tiers import planner as p
from looper.agents.tiers.journal import JournalEntry
from looper.agents.tiers.planner_protocol import DecisionContext, ResultKind


def _agent(tmp_path, claude, *, count=10):
    agent = p.PlannerAgent(task_id="task-view", runtime_root=tmp_path, claude_bin=claude.bin)
    agent._plan = p.TaskPlan(
        "plan", "goal", "Inspect the files", str(tmp_path), session_id="sess-p",
        session_runtime="claude-code", session_provider="prov-claude-subscription",
        steps=[p.PlanStep(f"step-{i}", i, f"Inspect file {i}. " + "Check its observable behavior. " * 8,
                          key=f"part-{i}") for i in range(count)],
        journal=[JournalEntry("step-0", "part-0", "Established the expected behavior")],
    )
    return agent


def _decision(agent):
    plan = agent._plan
    step = plan.steps[0]
    context = DecisionContext(step.label, ResultKind.WORK, True, False, 0)
    return agent._decision_message(plan, step, context, ["Worker: done"])


def _annotations(agent_db):
    return [call.args[1] for call in agent_db.add_metadata.await_args_list if "plan_view" in call.args[1]]


def _run(exercise):
    return asyncio.run(asyncio.wait_for(exercise(), 30))


def test_second_decision_sends_changes_and_records_view(agent_db, planner_claude, tmp_path):
    claude = planner_claude("ack")
    agent = _agent(tmp_path, claude)

    async def exercise():
        try:
            await agent._turn(_decision(agent))
            agent._plan.steps[0].status = "verified"
            agent._plan.steps[0].result = {"summary": "The checks passed"}
            await agent._turn(_decision(agent))
        finally:
            await agent._claude.stop()

    _run(exercise)
    first, second = [turn["text"] for turn in claude.turns()]
    assert "\nPlan:\n" in first
    assert "Plan (changes since" in second and "\nPlan:\n" not in second
    assert "verified: part-0; pending: " in second
    assert "Changed or new:\n- part-0 [verified]" in second
    assert "summary: The checks passed" in second
    assert "- part-1 [pending]" not in second
    assert "Progress and findings" not in second
    assert len(second) < len(first)
    assert all("\x00" not in turn["text"] for turn in claude.turns())
    assert _annotations(agent_db) == [
        {"plan_view": "full", "plan_view_reason": "new process"},
        {"plan_view": "changes", "plan_view_reason": "changes"},
    ]


def test_compaction_makes_next_turn_full(agent_db, planner_claude, tmp_path):
    claude = planner_claude("ack")
    script = Path(claude.bin + ".py")
    text = script.read_text()
    needle = "    answers = {'session_id': 'sess-p'"
    assert needle in text
    text = text.replace(needle, "    if n == 2:\n"
                        "        emit({'type': 'system', 'subtype': 'compact_boundary'})\n" + needle)
    script.write_text(text)
    agent = _agent(tmp_path, claude)

    async def exercise():
        try:
            for _ in range(3):
                await agent._turn(_decision(agent))
        finally:
            await agent._claude.stop()

    _run(exercise)
    texts = [turn["text"] for turn in claude.turns()]
    assert "Plan (changes since" in texts[1]
    assert "\nPlan:\n" in texts[2]
    assert _annotations(agent_db)[2]["plan_view_reason"] == "first view"


def test_failed_turn_makes_next_turn_full(agent_db, planner_claude, tmp_path):
    claude = planner_claude("ack", "<error>", "ack")
    agent = _agent(tmp_path, claude)

    async def exercise():
        try:
            await agent._turn(_decision(agent))
            with pytest.raises(p.PlannerTurnError):
                await agent._turn(_decision(agent))
            assert agent._plan_shown is None and agent._plan_staged is None
            await agent._turn(_decision(agent))
        finally:
            await agent._claude.stop()

    _run(exercise)
    assert "Plan (changes since" in claude.turns()[1]["text"]
    assert "\nPlan:\n" in claude.turns()[2]["text"]


def test_twenty_change_turns_then_periodic_full(agent_db, planner_claude, tmp_path):
    claude = planner_claude("ack")
    agent = _agent(tmp_path, claude)

    async def exercise():
        try:
            for _ in range(22):
                await agent._turn(_decision(agent))
        finally:
            await agent._claude.stop()

    _run(exercise)
    texts = [turn["text"] for turn in claude.turns()]
    assert "\nPlan:\n" in texts[0]
    assert all("Plan (changes since" in text for text in texts[1:21])
    assert "changes since the previous turn's message" in texts[20]
    assert "\nPlan:\n" in texts[21]
    assert _annotations(agent_db)[21]["plan_view_reason"] == "periodic"


def test_runtime_without_compaction_events_always_sends_full(
    agent_db, planner_claude, tmp_path, monkeypatch,
):
    monkeypatch.setattr(CLAUDE_CODE, "capabilities", CLAUDE_CODE.capabilities - {Capability.COMPACTION_EVENTS})
    claude = planner_claude("ack")
    agent = _agent(tmp_path, claude)

    async def exercise():
        try:
            for _ in range(3):
                await agent._turn(_decision(agent))
        finally:
            await agent._claude.stop()

    _run(exercise)
    assert all("\nPlan:\n" in turn["text"] for turn in claude.turns())
    assert [annotation["plan_view_reason"] for annotation in _annotations(agent_db)] == [
        "new process", "no compaction events", "no compaction events",
    ]


def test_grok_resident_session_always_sends_full(agent_db, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from fake_claude import fake_claude
    from fake_grok_acp import FAKE_ACP
    from looper.agents.agent_settings import AgentSettings
    from looper.agents.profile_registry import (
        Endpoint, ModelSpec, ProviderSpec, ResolvedAgentProfile, ResolvedModel, RuntimeSpec,
    )
    from looper.agents.runtimes import ApiProtocol, AuthKind, VendorType, adapter

    grok = adapter("grok")
    executable = fake_claude(tmp_path / "grok", FAKE_ACP)
    provider = ProviderSpec("grok-login", "Grok", VendorType.GROK_LOGIN, AuthKind.OAUTH, "grok")
    model = ModelSpec("grok-model", provider.id, "grok-4.7", ApiProtocol.OPENAI_RESPONSES)
    profile = ResolvedAgentProfile(
        "grok-planner", "Grok Planner", RuntimeSpec("grok-runtime", "Grok", "grok", executable, grok.capabilities),
        model.id, resolved_model=ResolvedModel(model, provider, Endpoint(provider.id, model.protocol, "")))
    agent = _agent(tmp_path, SimpleNamespace(bin=executable))
    agent._plan.session_id = None
    monkeypatch.setattr(agent, "_load_agent_settings", AsyncMock(return_value=AgentSettings(profile=profile)))

    async def exercise():
        try:
            texts = []
            for turn in range(3):
                agent._plan.steps[0].status = "pending" if turn == 0 else "verified"
                texts.append(await agent._turn(_decision(agent)))  # The ACP stand-in echoes the prompt.
                assert agent._claude.transport == "acp"
            return texts
        finally:
            await agent._claude.stop()

    texts = _run(exercise)
    assert all("\nPlan:\n" in text and "Plan (changes since" not in text for text in texts)
    assert all("- part-1 [pending]" in text for text in texts)  # Unchanged steps remain available.
    assert "- part-0 [verified]" in texts[1]
    assert [annotation["plan_view_reason"] for annotation in _annotations(agent_db)] == [
        "new session", "no compaction events", "no compaction events",
    ]


@pytest.mark.parametrize("new_session", [False, True], ids=["new-process", "resume-brief"])
def test_process_and_session_restart_send_full(agent_db, planner_claude, tmp_path, new_session):
    claude = planner_claude("ack")
    agent = _agent(tmp_path, claude)

    async def exercise():
        try:
            await agent._turn(_decision(agent))
            await agent._turn(_decision(agent))
            await agent._claude.stop()
            if new_session:
                agent._plan.session_id = None
            await agent._turn(_decision(agent))
        finally:
            await agent._claude.stop()

    _run(exercise)
    text = claude.turns()[-1]["text"]
    assert "\nPlan:\n" in text
    assert text.startswith("[RESUME]") is new_session
    assert _annotations(agent_db)[-1]["plan_view_reason"] == ("new session" if new_session else "new process")


def test_turn_without_view_leaves_view_unchanged(agent_db, planner_claude, tmp_path):
    claude = planner_claude("ack")
    agent = _agent(tmp_path, claude)

    async def exercise():
        try:
            await agent._turn(_decision(agent))
            shown = agent._plan_shown
            await agent._turn("[LESSONS] Record what was learned.")
            assert agent._plan_shown is shown
            await agent._turn(_decision(agent))
        finally:
            await agent._claude.stop()

    _run(exercise)
    assert claude.turns()[1]["text"] == "[LESSONS] Record what was learned."
    assert "changes since the previous turn's message" in claude.turns()[2]["text"]
    assert len(_annotations(agent_db)) == 2


def test_failed_resume_renders_original_placeholder_again(agent_db, planner_claude, tmp_path):
    claude = planner_claude("ack", forget_sessions=True)
    agent = _agent(tmp_path, claude)

    async def exercise():
        try:
            await agent._turn(_decision(agent))
        finally:
            await agent._claude.stop()

    _run(exercise)
    assert len(claude.argvs()) == 2
    assert "--resume" in claude.argvs()[0] and "--resume" not in claude.argvs()[1]
    text = claude.turns()[-1]["text"]
    assert text.startswith("[RESUME]") and "\nPlan:\n" in text and "\x00" not in text
    assert _annotations(agent_db)[-1] == {"plan_view": "full", "plan_view_reason": "new session"}


@pytest.mark.parametrize("already_shown", [False, True])
def test_room_view_is_clipped_within_batch_budget(agent_db, planner_claude, tmp_path, already_shown):
    claude = planner_claude("ack")
    agent = _agent(tmp_path, claude, count=40)
    rows = [{"id": i, "sender_seat": "worker-part-0", "kind": "message", "body": "details " * 240,
             "audience_all": True} for i in range(30)]
    message, shown = agent._room_message(agent._plan, rows)
    assert 0 < len(shown) < len(rows)

    async def exercise():
        try:
            if already_shown:
                await agent._turn(_decision(agent))
                for step in agent._plan.steps:
                    step.status = "skipped"
            await agent._turn(message)
            assert agent._plan_shown is None
            await agent._turn(_decision(agent))
        finally:
            await agent._claude.stop()

    _run(exercise)
    room_turn = 1 if already_shown else 0
    first = claude.turns()[room_turn]["text"]
    assert len(first) <= p.ROOM_BATCH_MAX_CHARS and "\x00" not in first
    assert "…\n\nReply in the [CHAT] shape" in first
    assert _annotations(agent_db)[room_turn] == {"plan_view": "full", "plan_view_reason": "clipped"}
    assert "\nPlan:\n" in claude.turns()[room_turn + 1]["text"]


def test_restart_discards_previous_view_and_sends_progress_once(agent_db, planner_claude, tmp_path):
    claude = planner_claude("ack")
    agent = _agent(tmp_path, claude)

    async def exercise():
        try:
            await agent._turn(_decision(agent))
            await agent._turn(_decision(agent))
            message = agent._restart_message(agent._plan, "failed", p.TaskChanges())
            assert agent._plan_shown is None
            await agent._turn(message)
        finally:
            await agent._claude.stop()

    _run(exercise)
    text = claude.turns()[-1]["text"]
    assert "\nPlan:\n" in text
    assert text.count("Progress and findings (") == 1
    assert _annotations(agent_db)[-1]["plan_view_reason"] == "first view"


def test_wait_and_gate_put_running_before_plan_view(agent_db, planner_claude, tmp_path):
    from looper.agents.tiers.planner_protocol import GateContext, WaitContext

    claude = planner_claude("ack")
    agent = _agent(tmp_path, claude)
    gate = p.PlanStep("gate", 10, "Pick a format", gate=p.Gate("Pick a format", ()))
    wait = p.PlanStep("wait", 11, "Await a change",
                      wait=p.Wait(p.WaitSpec("Await a change", 60), state=p.WaitState.TIMED_OUT))
    agent._plan.steps += [gate, wait]
    messages = [
        agent._gate_message(agent._plan, gate, [], GateContext(gate.label, ())),
        agent._wait_message(agent._plan, wait, {}, WaitContext(wait.label, p.WaitState.TIMED_OUT)),
    ]
    for message in messages:
        assert message.index("Running now:") < message.index(p.PLAN_VIEW)

    async def exercise():
        try:
            for message in messages:
                await agent._turn(message)
        finally:
            await agent._claude.stop()

    _run(exercise)
    for turn in claude.turns():
        text = turn["text"]
        assert text.index("Running now:") < text.index("Plan")
        assert "\x00" not in text


def test_condition_chat_full_view_includes_progress(agent_db, planner_claude, tmp_path):
    from tests.test_planner_agent import reply

    claude = planner_claude(reply({"reply": "Skip the blocked step", "action": "none"}))
    agent = _agent(tmp_path, claude)
    agent._plan.status = "executing"
    agent._plan.steps[0].status = "skipped"
    agent._plan.steps[1].depends_on = [agent._plan.steps[0].step_id]

    async def exercise():
        try:
            assert await agent._blocked_conditions(agent._plan)
        finally:
            await agent._claude.stop()

    _run(exercise)
    text = claude.turns()[0]["text"]
    assert text.startswith("[CHAT]")
    assert "\nPlan:\n" in text
    assert "Progress and findings (" in text
    assert "Established the expected behavior" in text
    assert "\x00" not in text


@pytest.mark.parametrize("success", [False, True])
def test_autonomous_turn_discards_previous_view(agent_db, planner_claude, tmp_path, success):
    from looper.agents.claude_cli import ResidentTurn

    claude = planner_claude("ack")
    agent = _agent(tmp_path, claude)

    async def exercise():
        try:
            await agent._turn(_decision(agent))
            await agent._turn(_decision(agent))
            process = agent._process
            begin = agent._autonomous_turns(agent._plan, process.command, process.system_prompt, process.launch)
            autonomous = await begin(None)
            result = {"type": "result", "subtype": "success", "result": "Background work ended"} if success else None
            await autonomous.end(ResidentTurn(result))
            assert agent._plan_shown is None
            await agent._turn(_decision(agent))
        finally:
            await agent._claude.stop()

    _run(exercise)
    assert "\nPlan:\n" in claude.turns()[-1]["text"]
    assert _annotations(agent_db)[-1]["plan_view_reason"] == "first view"


def test_full_view_preserves_five_step_plan_with_gate_wait_and_summary(tmp_path):
    from looper.agents.tiers.plan_view import render

    agent = p.PlannerAgent(task_id="task-view", runtime_root=tmp_path)
    steps = [
        p.PlanStep("step-a", 0, "Inspect the files", key="a", status="verified", result={"summary": "All checked"}),
        p.PlanStep("step-b", 1, "Implement behavior", key="b", depends_on=["step-a"], review=True),
        p.PlanStep("step-c", 2, "Unneeded work", key="c", status="skipped"),
        p.PlanStep("step-g", 3, "Pick a format", key="g", gate=p.Gate("Pick a format", ())),
        p.PlanStep("step-w", 4, "Await a change", key="w", wait=p.Wait(p.WaitSpec("Await a change", 60))),
    ]
    plan = p.TaskPlan("plan", "goal", "Inspect the files", str(tmp_path), steps=steps,
                      journal=[JournalEntry("step-a", "a", "All checked")])
    expected = "\n".join([
        "Plan:",
        "- a [verified] Inspect the files (depends on: none; review: no; retries: 0)",
        "  summary: All checked",
        "- b [pending] Implement behavior (depends on: a; review: yes; retries: 0)",
        "- c [skipped] Unneeded work (depends on: none; review: no; retries: 0)",
        '- g [gate: pending] "Pick a format" (depends on: none; agenda: 0 of 0 resolved)',
        agent._wait_line(steps[-1]),
        *agent._progress_lines(plan),
    ])
    items = agent._plan_items(plan)
    result = render(items, agent._progress_lines(plan)[1:], None, full_reason=None)
    assert "\n".join(result.lines) == expected
    assert "\n".join([*agent._plan_lines(plan), *agent._progress_lines(plan)]) == expected
    assert [item.step_id for item in items] == [step.step_id for step in steps]
    assert items[0].text.endswith("\n  summary: All checked")
    assert [item.state for item in items] == ["verified", "pending", "skipped", "gate: pending", "wait pending"]


def test_serial_decisions_send_only_new_progress_under_half_the_full_view(
    agent_db, planner_claude, tmp_path,
):
    from looper.agents.tiers.plan_view import render

    claude = planner_claude("ack")
    agent = _agent(tmp_path, claude, count=9)
    agent._plan.journal = []
    for previous, step in zip(agent._plan.steps, agent._plan.steps[1:]):
        step.depends_on = [previous.step_id]
    full_sizes = []

    async def exercise():
        try:
            for index, step in enumerate(agent._plan.steps):
                step.status = "completed"
                step.result = {"summary": f"Verified behavior for {step.label}."}
                if index:
                    previous = agent._plan.steps[index - 1]
                    previous.status = "verified"
                    agent._plan.journal.append(JournalEntry(previous.step_id, previous.label,
                                                            previous.result["summary"]))
                full = render(agent._plan_items(agent._plan), agent._progress_lines(agent._plan)[1:],
                              None, full_reason=None)
                full_sizes.append(len("\n".join(full.lines)))
                context = DecisionContext(step.label, ResultKind.WORK, True, False, 0)
                await agent._turn(agent._decision_message(agent._plan, step, context, ["Worker: done"]))
        finally:
            await agent._claude.stop()

    _run(exercise)
    texts = [turn["text"] for turn in claude.turns()]
    assert "\nPlan:\n" in texts[0]
    for index, text in enumerate(texts[1:], 1):
        view = text.split("\n\n", 1)[1].split("\n\nWorker: done", 1)[0]
        assert "Plan (changes since" in view
        assert len(view) < full_sizes[index] / 2
        progress = view.split("New progress:\n", 1)[1]
        assert progress == f"- part-{index - 1}: Verified behavior for part-{index - 1}."
        assert view.count(progress) == 1
    assert [item["plan_view"] for item in _annotations(agent_db)] == ["full", *["changes"] * 8]


@pytest.mark.parametrize("restart", [False, True], ids=["chat", "restart"])
@pytest.mark.parametrize("already_shown", [False, True])
def test_new_supplement_is_sent_once_with_the_remaining_journal(
    agent_db, planner_claude, tmp_path, restart, already_shown,
):
    from looper.agents.tiers.journal import JournalKind
    from looper.db.task_changes import Supplement

    claude = planner_claude("ack")
    agent = _agent(tmp_path, claude)
    supplement = Supplement(42, "Also check the cancellation behavior.", "web")
    changes = p.TaskChanges(supplements=(supplement,))

    async def exercise():
        try:
            if already_shown:
                await agent._turn(_decision(agent))
            agent._plan.journal.append(JournalEntry("", "", supplement.text,
                                                    kind=JournalKind.SUPPLEMENT, id=supplement.id))
            message = (agent._restart_message(agent._plan, "failed", changes) if restart else
                       agent._change_message(agent._plan, changes))
            await agent._turn(message)
            await agent._turn(_decision(agent))
        finally:
            await agent._claude.stop()

    _run(exercise)
    text = claude.turns()[-2]["text"]
    assert text.count(supplement.text) == 1
    assert text.count("Established the expected behavior") == 1
    assert "\x00" not in text
    if not restart:
        assert "\n\nRunning now:" in text
    following = claude.turns()[-1]["text"]
    assert supplement.text not in following
    assert "New progress:" not in following


@pytest.mark.parametrize("failure", ["command", "start"])
@pytest.mark.parametrize("marker", [p.PLAN_VIEW, p.PLAN_VIEW_CLIPPED, p.PLAN_VIEW_STEPS],
                         ids=["plan", "clipped", "steps"])
def test_launch_failure_records_a_rendered_message(
    agent_db, planner_claude, tmp_path, monkeypatch, failure, marker,
):
    from unittest.mock import AsyncMock, Mock

    claude = planner_claude("ack")
    agent = _agent(tmp_path, claude)
    if failure == "command":
        monkeypatch.setattr(agent, "_claude_command", Mock(side_effect=p.RuntimeUnsupported("unavailable")))
    else:
        agent._plan.session_id = None  # also exercise a failed launch with a resume brief
        monkeypatch.setattr(agent._claude, "start", AsyncMock(side_effect=OSError("unavailable")))
    message = _decision(agent).replace(p.PLAN_VIEW, marker)

    async def exercise():
        try:
            with pytest.raises(p.PlannerTurnError, match="cannot start.*unavailable"):
                await agent._turn(message)
        finally:
            await agent._claude.stop()

    _run(exercise)
    agent_db.start_record.assert_awaited_once()
    stored = agent_db.start_record.await_args.kwargs["input_text"]
    assert "\x00" not in stored
    assert "\nPlan:\n" in stored
    assert "- part-0 [pending]" in stored
    assert stored.startswith("[RESUME]") is (failure == "start")
    agent_db.finish_record.assert_awaited_once()
    assert agent_db.finish_record.await_args.kwargs["status"] == "failed"
    assert "unavailable" in agent_db.finish_record.await_args.kwargs["error"]
    assert agent._plan_shown is None and agent._plan_staged is None
