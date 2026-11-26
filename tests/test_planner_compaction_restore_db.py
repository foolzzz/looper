"""Planner compaction recovery on real records and a scripted resident CLI."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from looper.agents.schedule_session import BoundReason, SessionBound
from looper.agents.tiers import planner as p
from looper.agents.tiers.planner_protocol import DecisionContext, ResultKind
from looper.agents.user_gates import AgendaItem, Gate, GateState
from looper.db import tasks
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def _agent(tmp_path, claude):
    agent = p.PlannerAgent(task_id="task-restore", runtime_root=tmp_path, claude_bin=claude.bin)
    agent._plan = p.TaskPlan(
        "plan", "", "Inspect the files", str(tmp_path), status="executing", session_id="sess-p",
        session_runtime="claude-code", session_provider="prov-claude-subscription",
        steps=[p.PlanStep(f"step-{i}", i, f"Inspect file {i}", key=f"part-{i}") for i in range(3)],
    )
    return agent


def _decision(agent):
    step = agent._plan.steps[0]
    return agent._decision_message(agent._plan, step, DecisionContext(step.label, ResultKind.WORK, True, False, 0),
                                   ["Worker: done"])


def _script(claude, *, automatic=(), compact="success"):
    script = Path(claude.bin + ".py")
    source = script.read_text()
    marker = "    answers = {'session_id': 'sess-p'"
    assert marker in source
    events = (
        "    text = message['message']['content']\n"
        "    if text == '/compact' or text.startswith('/compact '):\n"
        "        emit({'type': 'system', 'subtype': 'status', 'status': 'compacting'})\n"
        + ("        emit({'type': 'system', 'subtype': 'compact_boundary', 'compact_metadata': "
           "{'trigger': 'manual', 'pre_tokens': 120000, 'post_tokens': 4000}})\n"
           if compact == "success" else
           "        emit({'type': 'system', 'subtype': 'status', 'compact_result': 'failed', "
           "'compact_error': 'too_few_groups'})\n" if compact == "failed" else "        pass\n")
        + f"    elif n in {tuple(automatic)!r}:\n"
          "        emit({'type': 'system', 'subtype': 'compact_boundary', 'compact_metadata': "
          "{'trigger': 'auto', 'pre_tokens': 120000, 'post_tokens': 4000}})\n"
    )
    script.write_text(source.replace(marker, events + marker))


async def _seed(pool, agent):
    await pool.execute("INSERT INTO projects (id,name,work_dir) VALUES ('restore-project','Restore project',$1)",
                       agent._plan.work_dir)
    await tasks.create_task(task_id=agent._task_id, requirement="Inspect the files", project_id="restore-project",
                            work_dir=agent._plan.work_dir)
    agent._plan.goal_id = ""
    agent._plan.status = "executing"
    await agent._save(agent._plan, *agent._plan.steps, plan_row=True)
    await pool.execute("INSERT INTO system_settings (key,value) VALUES ('compaction_restore.enabled','true')")


async def _records(pool):
    rows = await pool.fetch("SELECT id, input_text, metadata FROM llm_records ORDER BY id")
    return [{**dict(row), "metadata": json.loads(row["metadata"] or "{}")} for row in rows]


def test_schedule_compaction_keeps_context_then_sends_current_snapshot(monkeypatch, tmp_path, planner_claude):
    claude = planner_claude("ack")
    _script(claude)
    agent = _agent(tmp_path, claude)

    async def test(pool):
        await _seed(pool, agent)
        agent._schedule_bound = SessionBound(BoundReason.CONTEXT, 120000)
        agent._schedule_run_cost = 0.1
        agent._record_schedule_bound = AsyncMock()
        try:
            await agent._turn(_decision(agent))
        finally:
            await agent._claude.stop()
        from looper.agents import compaction_restore as restore
        turns = [turn["text"] for turn in claude.turns()]
        assert turns[0] == restore.command(restore.Role.PLANNER)
        assert turns[1].startswith("[COMPACTED]") and "(manual," in turns[1]
        assert "Task task-restore: requirement version 1" in turns[1] and "\nPlan:\n" in turns[1]
        records = await _records(pool)
        assert records[0]["metadata"]["compaction_restore"]["state"] == "due"
        assert records[1]["metadata"]["compaction_restore"]["state"] == "sent"
        assert records[1]["metadata"]["compaction_restore"]["due_record"] == records[0]["id"]

    run_db(test, monkeypatch)


def test_automatic_compaction_replays_waiting_gate_once(monkeypatch, tmp_path, planner_claude):
    claude = planner_claude("ack")
    _script(claude, automatic=(2,))
    agent = _agent(tmp_path, claude)
    gate_step = agent._plan.steps[-1]
    gate_step.gate = Gate("Choose the format", (AgendaItem("q1", "Which format?"),), state=GateState.OPEN)

    async def test(pool):
        await _seed(pool, agent)
        await agent._save(agent._plan, gate_step, gate_messages=[{
            "step_id": gate_step.step_id, "role": "planner", "kind": "message", "text": "Earlier gate discussion",
        }])
        try:
            for _ in range(4):
                await agent._turn(_decision(agent))
        finally:
            await agent._claude.stop()
        texts = [turn["text"] for turn in claude.turns()]
        assert texts[2].startswith("[COMPACTED]") and "(auto," in texts[2]
        assert "Gates and waits:" in texts[2] and 'Choose the format' in texts[2]
        assert "- q1 [open] Which format?" in texts[2] and "Earlier gate discussion" in texts[2]
        assert "[COMPACTED]" not in texts[3]
        assert (await _records(pool))[2]["metadata"]["compaction_restore"]["state"] == "sent"

    run_db(test, monkeypatch)


@pytest.mark.parametrize("compact", ["missing", "failed"])
def test_failed_maintenance_rotates_without_snapshot(monkeypatch, tmp_path, planner_claude, compact):
    claude = planner_claude("ack")
    _script(claude, compact=compact)
    agent = _agent(tmp_path, claude)

    async def test(pool):
        await _seed(pool, agent)
        agent._schedule_bound = SessionBound(BoundReason.CONTEXT, 120000)
        agent._schedule_run_cost = 0.1
        agent._record_schedule_bound = AsyncMock()
        try:
            await agent._turn(_decision(agent))
        finally:
            await agent._claude.stop()
        from looper.agents import compaction_restore as restore
        assert claude.turns()[0]["text"] == restore.command(restore.Role.PLANNER)
        assert claude.turns()[-1]["text"].startswith("[RESUME]")
        assert all("[COMPACTED]" not in turn["text"] for turn in claude.turns())
        assert all("compaction_restore" not in row["metadata"] for row in await _records(pool))

    run_db(test, monkeypatch)


def test_restart_reads_due_from_the_stored_session(monkeypatch, tmp_path, planner_claude):
    claude = planner_claude("ack")
    _script(claude, automatic=(1,))
    agent = _agent(tmp_path, claude)

    async def test(pool):
        await _seed(pool, agent)
        try:
            await agent._turn(_decision(agent))
        finally:
            await agent._claude.stop()
        restarted = p.PlannerAgent(task_id=agent._task_id, runtime_root=tmp_path, claude_bin=claude.bin)
        await restarted._load_plan()
        assert restarted._plan.session_id == agent._plan.session_id
        try:
            await restarted._turn(_decision(restarted))
        finally:
            await restarted._claude.stop()
        assert claude.turns()[-1]["text"].startswith("[COMPACTED]")
        assert "--resume" in claude.argvs()[-1]

    run_db(test, monkeypatch)


def test_failed_snapshot_is_offered_again_until_success(monkeypatch, tmp_path, planner_claude):
    claude = planner_claude("ack", "<error>", "ack", "ack")
    _script(claude, automatic=(1,))
    agent = _agent(tmp_path, claude)

    async def test(pool):
        await _seed(pool, agent)
        try:
            await agent._turn(_decision(agent))
            with pytest.raises(p.PlannerTurnError):
                await agent._turn(_decision(agent))
            await agent._turn(_decision(agent))
            await agent._turn(_decision(agent))
        finally:
            await agent._claude.stop()
        texts = [turn["text"] for turn in claude.turns()]
        assert texts[1].startswith("[COMPACTED]") and texts[2].startswith("[COMPACTED]")
        assert "[COMPACTED]" not in texts[3]
        records = await _records(pool)
        assert [row["metadata"]["compaction_restore"]["state"] for row in records[:3]] == ["due", "offered", "sent"]
        assert records[1]["metadata"]["compaction_restore"]["due_record"] == records[0]["id"]

    run_db(test, monkeypatch)


def test_boundary_during_snapshot_replaces_the_previous_due(monkeypatch, tmp_path, planner_claude):
    claude = planner_claude("ack")
    _script(claude, automatic=(1, 2))
    agent = _agent(tmp_path, claude)

    async def test(pool):
        await _seed(pool, agent)
        try:
            for _ in range(4):
                await agent._turn(_decision(agent))
        finally:
            await agent._claude.stop()
        records = await _records(pool)
        assert [row["metadata"]["compaction_restore"]["state"] for row in records[:3]] == ["due", "due", "sent"]
        assert records[2]["metadata"]["compaction_restore"]["due_record"] == records[1]["id"]
        assert "[COMPACTED]" not in claude.turns()[3]["text"]

    run_db(test, monkeypatch)


@pytest.mark.parametrize("failure", [None, "versions", "latest"])
def test_snapshot_reads_delivered_versions_and_tolerates_read_errors(monkeypatch, tmp_path, planner_claude, failure):
    claude = planner_claude("ack")
    _script(claude, automatic=(1,))
    agent = _agent(tmp_path, claude)

    async def test(pool):
        await _seed(pool, agent)
        await pool.execute("INSERT INTO task_requirement_versions (task_id,version,text,via,delivered_at) "
                           "VALUES ($1,2,'delivered','test',now()),($1,3,'pending','test',NULL)", agent._task_id)
        try:
            await agent._turn(_decision(agent))
            if failure == "versions":
                monkeypatch.setattr(p.db_changes, "requirement_versions", AsyncMock(side_effect=ConnectionError))
            if failure == "latest":
                monkeypatch.setattr(p.db_llm_records, "latest_compaction_restore", AsyncMock(side_effect=ConnectionError))
            await agent._turn(_decision(agent))
        finally:
            await agent._claude.stop()
        text = claude.turns()[1]["text"]
        if failure == "latest":
            assert "[COMPACTED]" not in text
        elif failure == "versions":
            assert "requirement version unknown (Looper could not read it)" in text
        else:
            assert "requirement version 2 (the Task section" in text

    run_db(test, monkeypatch)


@pytest.mark.parametrize("long_head", [False, True])
def test_snapshot_cap_keeps_the_request_tail(monkeypatch, tmp_path, planner_claude, long_head):
    from looper.agents import compaction_restore as restore
    claude = planner_claude("ack")
    _script(claude, automatic=(1,))
    agent = _agent(tmp_path, claude)

    async def test(pool):
        await _seed(pool, agent)
        try:
            await agent._turn(_decision(agent))
            monkeypatch.setattr(agent, "_gate_threads", AsyncMock(return_value=["", "discussion " * 2000]))
            if long_head:
                gate = Gate("Choose", tuple(AgendaItem(f"q{i}", "question " * 100) for i in range(30)),
                            state=GateState.OPEN)
                agent._plan.steps[-1].gate = gate
            offer = await agent._restore_offer(agent._plan)
        finally:
            await agent._claude.stop()
        assert offer is not None and len(offer.text) <= restore.PLANNER_MAX_CHARS
        assert restore.THREADS_LEFT_OUT in offer.text and "discussion discussion" not in offer.text
        assert offer.text.endswith("\n\nThe request for this turn follows; answer it in its shape.")

    run_db(test, monkeypatch)


@pytest.mark.parametrize("scenario", ["maintenance", "automatic", "no_boundary"])
def test_disabled_values_preserve_every_model_input(monkeypatch, tmp_path, planner_claude, scenario):
    from looper.agents import compaction_restore as restore
    captures = []
    original_reader = p.db_llm_records.latest_compaction_restore
    values = [None, "false", "yes"] + (["true"] if scenario == "no_boundary" else [])
    for value in values:
        claude = planner_claude("ack")
        _script(claude, automatic=(2,) if scenario == "automatic" else ())
        agent = _agent(tmp_path, claude)
        reader = AsyncMock(side_effect=AssertionError("disabled restore must not read records"))

        async def test(pool):
            await _seed(pool, agent)
            await pool.execute("DELETE FROM system_settings WHERE key=$1", restore.KEY)
            if value is not None:
                await pool.execute("INSERT INTO system_settings(key,value) VALUES ($1,$2)", restore.KEY, value)
            if value != "true":
                monkeypatch.setattr(p.db_llm_records, "latest_compaction_restore", reader)
            else:
                monkeypatch.setattr(p.db_llm_records, "latest_compaction_restore", original_reader)
            if scenario == "maintenance":
                agent._schedule_bound = SessionBound(BoundReason.CONTEXT, 120000)
                agent._schedule_run_cost = 0.1
                agent._record_schedule_bound = AsyncMock()
            try:
                for _ in range(3):
                    await agent._turn(_decision(agent))
            finally:
                await agent._claude.stop()
            reader.assert_not_awaited()
            assert all("compaction_restore" not in row["metadata"] for row in await _records(pool))

        run_db(test, monkeypatch)
        stdin = [turn["text"] for turn in claude.turns()]
        if scenario == "maintenance":
            assert stdin[0] == "/compact"
        captures.append((stdin, [argv[1:] for argv in claude.argvs()],
                         [hashlib.sha256(text.encode()).hexdigest() for text in claude.prompts()]))
    assert all(capture == captures[0] for capture in captures)
