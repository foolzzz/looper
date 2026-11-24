"""Planner lessons are per delivered session and never change its system contract."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents.tiers import planner
from looper.agents.tiers.planner_protocol import (
    DecisionContext, PlanView, ProtocolError, ResultKind, parse_decision, parse_plan,
)
from tests.runtime_model_helpers import builtin_launch

_STEP = {"id": "s1", "instruction": "Inspect synthetic data.", "acceptance_criteria": ["Evidence checked."],
         "depends_on": [], "review": False}


def reply(value):
    return "```json\n" + json.dumps(value) + "\n```"


def _notice_fixture():
    value = json.loads((Path(__file__).parent / "fixtures" / "memory_lesson_injection_notices.json").read_text())
    assert value["protocol"] == "project-lessons-notices/1"
    return value["items"]


def _assert_notice(text, name):
    item = _notice_fixture()[name]
    assert text == item["text"]
    assert hashlib.sha256(text.encode()).hexdigest() == item["sha256"]


@pytest.mark.parametrize("name", ["disabled", "replaced", "cleared"])
def test_new_notice_protocol_has_per_item_freezes(name):
    from looper.agents import memory_lesson_injection as lessons

    _assert_notice(getattr(lessons, "LESSONS_" + name.upper()), name)


def test_plan_references_are_optional_and_default_off_is_strict():
    text = reply({"steps": [_STEP], "lessons_used": ["M-0123"], "lessons_wrong": None})
    with pytest.raises(ProtocolError, match="unknown keys"):
        parse_plan(text)
    parsed = parse_plan(text, lessons_enabled=True)
    assert parsed.lessons_used == ("M-0123",)
    assert parsed.lessons_wrong == ()
    assert parsed.lessons_invalid == ()


@pytest.mark.parametrize("value", ["M-0123", 123, {}, [1]])
def test_bad_reference_fields_do_not_reject_a_valid_plan(value):
    parsed = parse_plan(reply({"steps": [_STEP], "lessons_used": value}), lessons_enabled=True)
    assert parsed.steps[0].id == "s1"
    assert parsed.lessons_used == ()
    assert parsed.lessons_invalid == ("lessons_used",)


def test_decision_references_keep_the_same_strict_gate():
    text = reply({"decision": "retry", "feedback": "try again", "lessons_wrong": ["M-0123"]})
    context = DecisionContext(step="s1", result=ResultKind.WORK, succeeded=False,
                              review_required=False, retries=0)
    view = PlanView(started={"s1": ()}, pending={})
    with pytest.raises(ProtocolError, match="unknown keys"):
        parse_decision(text, context, view)
    parsed = parse_decision(text, context, view, lessons_enabled=True)
    assert parsed.lessons_wrong == ("M-0123",)


def test_optional_lesson_messages_leave_empty_surfaces_unchanged(tmp_path):
    agent = planner.PlannerAgent(task_id="task", runtime_root=tmp_path)
    plan = planner.TaskPlan("plan", "goal", "Inspect", str(tmp_path))
    baseline = agent._plan_message(plan)
    assert agent._plan_message(plan, lessons="") == baseline
    text = agent._plan_message(plan, lessons="FROZEN LESSONS")
    assert "FROZEN LESSONS" in text
    assert "lessons_used (optional): ids of the lessons above you relied on." in text
    assert agent._resume_brief(plan, lessons="").startswith("[RESUME]")
    assert "FROZEN LESSONS" in agent._resume_brief(plan, lessons="FROZEN LESSONS")


def test_plan_and_decision_apply_references_only_after_actual_delivery(tmp_path, monkeypatch):
    from looper.agents import memory_lesson_injection as lessons
    from looper.db import memory_lesson_injection as store

    apply = AsyncMock()
    monkeypatch.setattr(store, "apply_references", apply)
    monkeypatch.setattr(lessons, "parse_references", lambda value, ids: (value, tuple(ids)))

    async def run():
        agent = planner.PlannerAgent(task_id="task", runtime_root=tmp_path)
        agent._read_run_deadline = AsyncMock()
        agent._read_roles = AsyncMock()
        parsed = SimpleNamespace(lessons_used=("M-0123",), lessons_wrong=(), lessons_invalid=())
        await agent._ask(planner.TurnKind.PLAN, "[PLAN]", lambda text: parsed, run=AsyncMock(return_value="{}"))
        assert apply.await_count == 0
        agent._lesson_delivery = {"id": 7, "candidate_ids": (123,)}
        await agent._ask(planner.TurnKind.PLAN, "[PLAN]", lambda text: parsed, run=AsyncMock(return_value="{}"))
        await agent._ask(planner.TurnKind.DECISION, "[DECISION]", lambda text: parsed,
                         run=AsyncMock(return_value="{}"))
        assert apply.await_count == 2
        assert apply.await_args.args[0] == 7

    asyncio.run(run())


def test_schedule_plan_replaces_prior_lesson_context(tmp_path, monkeypatch):
    from looper.agents import memory_lesson_injection as lessons
    from looper.db import memory_lesson_injection as store

    empty = SimpleNamespace(text="", candidate_ids=())
    block = SimpleNamespace(text="FROZEN BLOCK", candidate_ids=(123,))
    monkeypatch.setattr(lessons, "ensure_snapshot", AsyncMock(return_value=block))
    rendered = AsyncMock(return_value=block)
    monkeypatch.setattr(lessons, "runtime_settings", AsyncMock(return_value=object()))
    monkeypatch.setattr(lessons, "render_for_run", rendered)
    monkeypatch.setattr(lessons, "planner_notices", AsyncMock(return_value=SimpleNamespace(text="")))
    monkeypatch.setattr(store, "planner_injection", AsyncMock(return_value=None))
    monkeypatch.setattr(store, "recover_planner_injection", AsyncMock(return_value=None))
    monkeypatch.setattr(store, "planner_session_has_lessons", AsyncMock(return_value=True))
    prepared = AsyncMock(return_value={"id": 1, "block": block})
    monkeypatch.setattr(store, "prepare_injection", prepared)

    async def run():
        agent = planner.PlannerAgent(task_id="new-task", runtime_root=tmp_path)
        agent._scheduled = True
        agent._turn_kind = planner.TurnKind.PLAN
        plan = planner.TaskPlan("p", "g", "Inspect", str(tmp_path), project_id="project")
        plan.session_id = "shared-session"
        text, delivery, _ = await agent._lesson_prompt(plan, "[PLAN]")
        assert "FROZEN BLOCK" in text
        _assert_notice(text.splitlines()[-1], "replaced")
        assert delivery["id"] == 1
        assert prepared.await_args.args[2].startswith("session:shared-session:launch:")
        rendered.return_value = empty
        text, delivery, _ = await agent._lesson_prompt(plan, "[PLAN]")
        assert delivery is None
        _assert_notice(text.split("\n\n", 1)[1], "cleared")
        assert agent._lesson_delivery is None
        plan.session_id = None
        text, delivery, _ = await agent._lesson_prompt(plan, "[PLAN]")
        assert text == "[PLAN]"
        assert delivery is None

    asyncio.run(run())


@pytest.mark.parametrize("sends", [False, True])
def test_planner_notice_once_after_accepted_turn(sends, tmp_path, monkeypatch, agent_db):
    from looper.agents import claude_cli
    from looper.agents.system_prompt import SystemPrompt
    from looper.db import memory_lesson_injection as store

    notices = SimpleNamespace(text="Lesson M-0123 no longer applies.", ids=(123,), disabled=False)
    ack = AsyncMock()
    delivered = AsyncMock(return_value={"id": 7, "candidate_ids": (123,), "session_key": "session"})
    abandon = AsyncMock()
    monkeypatch.setattr(store, "ack_planner_notices", ack)
    monkeypatch.setattr(store, "bind_injection_record", AsyncMock())
    monkeypatch.setattr(store, "mark_delivered", delivered)
    monkeypatch.setattr(store, "abandon_injection", abandon)

    class Claude:
        command, started, pid, stderr, totals = ["claude"], True, 1, "", {}

        async def run_turn(self, text, on_event, stop=None, cancel=None):
            assert text.startswith(notices.text)
            if sends:
                cancel.sent()
            result = {"type": "result", "subtype": "success", "is_error": False, "result": "ok"}
            return claude_cli.ResidentTurn(result, session_started=True, prompt_sent=sends)

        async def stop(self):
            pass

    async def run():
        agent = planner.PlannerAgent(task_id="task", runtime_root=tmp_path)
        agent._plan = planner.TaskPlan("p", "g", "Inspect", str(tmp_path))
        agent._plan.session_id = "session"
        agent._claude = Claude()
        agent._lesson_prompt = AsyncMock(return_value=(notices.text + "\n\n[CHAT]", {"id": 7}, notices))
        process = planner._Process(["claude"], None, SystemPrompt("stable system"), builtin_launch())
        await agent._exchange("[CHAT]", process, 5)
        assert ack.await_count == int(sends)
        assert delivered.await_count == int(sends)
        assert abandon.await_count == int(not sends)

    asyncio.run(run())


def test_interrupted_preparation_is_uncertain_and_never_resent(tmp_path, monkeypatch):
    from looper.agents import memory_lesson_injection as lessons
    from looper.db import memory_lesson_injection as store

    block = SimpleNamespace(text="FROZEN BLOCK", candidate_ids=(123,))
    monkeypatch.setattr(lessons, "runtime_settings", AsyncMock(return_value=object()))
    monkeypatch.setattr(lessons, "render_for_run", AsyncMock(return_value=block))
    monkeypatch.setattr(lessons, "planner_notices", AsyncMock(return_value=SimpleNamespace(text="")))
    monkeypatch.setattr(store, "planner_injection", AsyncMock(return_value={"id": 7, "state": "prepared"}))
    monkeypatch.setattr(store, "recover_planner_injection", AsyncMock(return_value=None))
    uncertain = AsyncMock()
    monkeypatch.setattr(store, "mark_uncertain", uncertain)
    prepare = AsyncMock()
    monkeypatch.setattr(store, "prepare_injection", prepare)

    async def run():
        agent = planner.PlannerAgent(task_id="task", runtime_root=tmp_path)
        agent._turn_kind = planner.TurnKind.DECISION
        plan = planner.TaskPlan("p", "g", "Inspect", str(tmp_path), project_id="project")
        plan.session_id = "session"
        text, prepared, _ = await agent._lesson_prompt(plan, "[DECISION]")
        assert text == "[DECISION]"
        assert prepared is None
        assert agent._lesson_delivery is None
        uncertain.assert_awaited_once_with(7)
        prepare.assert_not_awaited()

    asyncio.run(run())


def test_planner_exchange_persists_delivery_references_and_notice_ack(tmp_path, monkeypatch):
    """Exercise real snapshot/record/transport/ref/ack paths across agent reconstruction."""
    from datetime import datetime, timezone
    from looper.agents import claude_cli
    from looper.agents.memory_model import Action, Actor, State, Via, parse_id
    from looper.agents.run_usage import Usage
    from looper.agents.system_prompt import SystemPrompt
    from looper.db import memory, memory_lessons
    from tests.db_support import run_db
    from tests.memory_lessons_support import lesson

    async def check(pool):
        candidate = parse_id(await lesson(pool, "project", "Preserve synthetic evidence"))
        await pool.execute("UPDATE projects SET memory_lessons='on' WHERE id='project'")
        await pool.execute("INSERT INTO system_settings(key,value) VALUES ('memory_lessons','{\"enabled\":true}')")
        await pool.execute("INSERT INTO tasks(id,requirement,project_id) VALUES ('target','Inspect','project')")
        async with memory.transaction(pool) as conn:
            await memory_lessons.transition_lesson(conn, candidate, State.IN_EFFECT, action=Action.ACTIVATE,
                actor=Actor.USER, via=Via.DASHBOARD, now=datetime.now(timezone.utc), valid_days=90)

        class Claude:
            command, started, pid, stderr = ["claude"], True, 1, ""
            totals = Usage()
            sends = True
            texts = []
            session = "lesson-session"

            async def run_turn(self, text, on_event, stop=None, cancel=None):
                self.texts.append(text)
                if not self.sends:
                    raise RuntimeError("synthetic failure before transport acceptance")
                if cancel is not None:
                    cancel.sent()
                await on_event({"type": "system", "subtype": "init", "session_id": self.session})
                value = reply({"steps": [_STEP], "lessons_used": [f"M-{candidate:04d}"]})
                event = {"type": "result", "subtype": "success", "is_error": False, "result": value}
                return claude_cli.ResidentTurn(event, session_started=True, prompt_sent=True)

            async def stop(self):
                pass

        claude = Claude()
        process = planner._Process(["claude"], None, SystemPrompt("Stable system"), builtin_launch())

        def reconstruct(session):
            agent = planner.PlannerAgent(task_id="target", runtime_root=tmp_path)
            agent._plan = planner.TaskPlan("plan", "goal", "Inspect", str(tmp_path), project_id="project")
            agent._plan.session_id = session
            agent._claude = claude
            agent._turn_kind = planner.TurnKind.PLAN
            return agent

        agent = reconstruct(claude.session)
        claude.sends = False
        with pytest.raises(planner.PlannerTurnError, match="synthetic failure"):
            await agent._ask(planner.TurnKind.PLAN, "[PLAN] Inspect", agent._plan_parser(),
                             run=lambda text: agent._exchange(text, process, 5))
        assert await pool.fetchval("SELECT state FROM memory_injections WHERE task_id='target'") == "abandoned"
        assert not await pool.fetchval("SELECT count(*) FROM memory_lesson_stats")
        claude.sends = True
        agent = reconstruct(claude.session)
        parsed = await agent._ask(planner.TurnKind.PLAN, "[PLAN] Inspect", agent._plan_parser(),
                                  run=lambda text: agent._exchange(text, process, 5))
        assert parsed.lessons_used == (f"M-{candidate:04d}",)
        assert "Lessons from earlier tasks" in claude.texts[-1]
        assert await pool.fetchval("SELECT injected FROM memory_lesson_stats WHERE candidate_id=$1", candidate) == 1
        assert await pool.fetchval("SELECT cited FROM memory_lesson_stats WHERE candidate_id=$1", candidate) == 1
        assert await pool.fetchval("SELECT session_key FROM memory_injections WHERE task_id='target' AND state='delivered'") == claude.session
        record_text = await pool.fetchval("SELECT input_text FROM llm_records WHERE id=(SELECT record_id "
                                         "FROM memory_injections WHERE task_id='target' AND state='delivered')")
        assert record_text == claude.texts[-1]
        agent = reconstruct(claude.session)
        agent._turn_kind = planner.TurnKind.CHAT
        await agent._exchange("[CHAT] Continue", process, 5)
        assert "Lessons from earlier tasks" not in claude.texts[-1]
        disabled = _notice_fixture()["disabled"]["text"]
        # Each close after reopening gets one notice, including across reconstructed agents.
        # Neither reopening nor changed settings reselects or redelivers this task's snapshot.
        for gate in ("system", "project"):
            if gate == "system":
                await pool.execute("UPDATE system_settings SET value='{\"enabled\":false}' WHERE key='memory_lessons'")
            else:
                await pool.execute("UPDATE projects SET memory_lessons='off' WHERE id='project'")
            agent = reconstruct(claude.session)
            agent._turn_kind = planner.TurnKind.CHAT
            await agent._exchange("[CHAT] Continue", process, 5)
            _assert_notice(claude.texts[-1].split("\n\n", 1)[0], "disabled")
            assert agent._lesson_delivery is None
            agent = reconstruct(claude.session)
            agent._turn_kind = planner.TurnKind.CHAT
            await agent._exchange("[CHAT] Continue", process, 5)
            assert disabled not in claude.texts[-1]
            if gate == "system":
                await pool.execute("UPDATE system_settings SET value='{\"enabled\":true}' WHERE key='memory_lessons'")
            else:
                await pool.execute("UPDATE projects SET memory_lessons='on' WHERE id='project'")
            agent = reconstruct(claude.session)
            agent._turn_kind = planner.TurnKind.CHAT
            await agent._exchange("[CHAT] Continue", process, 5)
            assert agent._lesson_delivery["candidate_ids"] == (candidate,)
            assert "Lessons from earlier tasks" not in claude.texts[-1]
            assert not await pool.fetchval("SELECT disabled_acked FROM task_lesson_planner_state WHERE task_id='target'")
            assert await pool.fetchval("SELECT injected FROM memory_lesson_stats WHERE candidate_id=$1", candidate) == 1
        async with memory.transaction(pool) as conn:
            await memory_lessons.transition_lesson(conn, candidate, State.REVOKED, action=Action.REVOKE,
                actor=Actor.USER, via=Via.DASHBOARD, now=datetime.now(timezone.utc), valid_days=90)
        claude.sends = False
        with pytest.raises(planner.PlannerTurnError, match="synthetic failure"):
            await agent._exchange("[CHAT] Continue", process, 5)
        notice = f"Lesson M-{candidate:04d} was revoked by the operator: do not rely on it."
        assert claude.texts[-1].startswith(notice)
        claude.sends = True
        agent = reconstruct(claude.session)
        agent._turn_kind = planner.TurnKind.CHAT
        await agent._exchange("[CHAT] Continue", process, 5)
        assert claude.texts[-1].startswith(notice)
        agent = reconstruct(claude.session)
        agent._turn_kind = planner.TurnKind.CHAT
        await agent._exchange("[CHAT] Continue", process, 5)
        assert notice not in claude.texts[-1]
        assert await pool.fetchval("SELECT count(*) FROM memory_injections WHERE task_id='target'") == 2
        assert await pool.fetchval("SELECT injected FROM memory_lesson_stats WHERE candidate_id=$1", candidate) == 1

    run_db(check, monkeypatch)


def test_restart_recovers_prepared_launch_from_record_and_session_without_resending(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from looper.agents import memory_lesson_injection as service
    from looper.agents.memory_model import Action, Actor, State, Via, parse_id
    from looper.db import agent_sessions, llm_records, memory, memory_lessons
    from looper.db import memory_lesson_injection as store
    from tests.db_support import run_db
    from tests.memory_lessons_support import lesson

    async def check(pool):
        candidate = parse_id(await lesson(pool, "project", "Check synthetic evidence"))
        await pool.execute("UPDATE projects SET memory_lessons='on' WHERE id='project'")
        await pool.execute("INSERT INTO system_settings(key,value) VALUES ('memory_lessons','{\"enabled\":true}')")
        await pool.execute("INSERT INTO tasks(id,requirement,project_id) VALUES ('target','Inspect','project')")
        async with memory.transaction(pool) as conn:
            await memory_lessons.transition_lesson(conn, candidate, State.IN_EFFECT, action=Action.ACTIVATE,
                actor=Actor.USER, via=Via.DASHBOARD, now=datetime.now(timezone.utc), valid_days=90)
        block = await service.ensure_snapshot("target", "project")
        pending = await store.prepare_injection("target", "planner", "launch:original", block, project_id="project")
        record = await llm_records.start_record(agent_id="planner-target", task_id="target", input_text=block.text)
        await store.bind_injection_record(pending["id"], record)
        await pool.execute("UPDATE llm_records SET last_output_at=now() WHERE id=$1", record)
        # The session was observed and saved, but the host died before marking its injection delivered.
        await agent_sessions.save_session("planner-target", "saved-session", {})
        agent = planner.PlannerAgent(task_id="target", runtime_root=tmp_path)
        plan = planner.TaskPlan("p", "g", "Inspect", str(tmp_path), project_id="project")
        plan.session_id = "saved-session"
        agent._turn_kind = planner.TurnKind.DECISION
        text, prepared, _ = await agent._lesson_prompt(plan, "[DECISION] Continue")
        assert text == "[DECISION] Continue"
        assert prepared is None and agent._lesson_delivery is None
        assert agent._lesson_recovery["id"] == record
        assert agent._lesson_recovery["saved_session_id"] == "saved-session"
        assert isinstance(agent._lesson_recovery["last_output_at"], str)
        json.dumps(agent._lesson_recovery)
        assert await pool.fetchval("SELECT state FROM memory_injections WHERE id=$1", pending["id"]) == "uncertain"
        assert not await pool.fetchval("SELECT count(*) FROM memory_lesson_stats")
        # A second reconstructed Planner still does not resend or count the uncertain launch.
        again = planner.PlannerAgent(task_id="target", runtime_root=tmp_path)
        again._turn_kind = planner.TurnKind.DECISION
        assert (await again._lesson_prompt(plan, "[DECISION] Continue"))[0] == text
        assert await pool.fetchval("SELECT count(*) FROM memory_injections WHERE task_id='target'") == 1

    run_db(check, monkeypatch)


def test_invalid_reference_fields_do_not_count_otherwise_valid_arrays(tmp_path, monkeypatch):
    from looper.db import memory_lesson_injection as store

    applied = AsyncMock(return_value={"cited": [], "wrong": [], "invalid": ["lessons_wrong"], "conflicts": []})
    monkeypatch.setattr(store, "apply_references", applied)

    async def run():
        agent = planner.PlannerAgent(task_id="task", runtime_root=tmp_path)
        agent._activity = AsyncMock()
        agent._lesson_delivery = {"id": 7, "candidate_ids": (123,)}
        parsed = parse_plan(reply({"steps": [_STEP], "lessons_used": ["M-0123"],
                                   "lessons_wrong": "M-0123"}), lessons_enabled=True)
        await agent._apply_lesson_references(parsed)
        refs = applied.await_args.args[1]
        assert refs.used == refs.wrong == ()
        assert refs.invalid == ("lessons_wrong",)
        assert "invalid" in agent._activity.await_args.args[1]

    asyncio.run(run())


def test_start_quarantines_interrupted_runs_only_for_the_restored_task(tmp_path, monkeypatch):
    """A restored task owns recovery even when ordinary runs left no keeper manifest."""
    from datetime import datetime, timezone
    from looper.agents import memory_lesson_injection as service
    from looper.agents.bus import MessageBus
    from looper.agents.memory_model import Action, Actor, State, Via, parse_id
    from looper.db import memory, memory_lessons, plans
    from looper.db import memory_lesson_injection as store
    from tests.db_support import run_db
    from tests.memory_lessons_support import lesson

    async def check(pool):
        model_turn = AsyncMock(side_effect=AssertionError("model execution is forbidden in this recovery test"))
        monkeypatch.setattr(planner.PlannerAgent, "_run_turn", model_turn)
        candidate = parse_id(await lesson(pool, "project", "Recover ordinary synthetic runs"))
        await pool.execute("UPDATE projects SET memory_lessons='on' WHERE id='project'")
        await pool.execute("INSERT INTO system_settings(key,value) VALUES "
                           "('memory_lessons','{\"enabled\":true,\"inject\":{\"reviewer\":true}}')")
        await pool.execute("INSERT INTO projects(id,name) VALUES ('off-project','Never enabled')")
        await pool.execute("INSERT INTO tasks(id,requirement,project_id) VALUES "
                           "('target','Inspect','project'),('other','Other task','project'),"
                           "('off-task','No project lessons','off-project'),('fresh-task','New task','off-project')")
        async with memory.transaction(pool) as conn:
            await memory_lessons.transition_lesson(conn, candidate, State.IN_EFFECT, action=Action.ACTIVATE,
                actor=Actor.USER, via=Via.DASHBOARD, now=datetime.now(timezone.utc), valid_days=90)
        block = await service.ensure_snapshot("target", "project")
        await service.ensure_snapshot("other", "project")
        lost = []
        for role in ("worker", "reviewer"):
            delivery = await store.prepare_injection("target", role, "lost-" + role, block, project_id="project")
            lost.append(delivery["id"])
        other = await store.prepare_injection("other", "worker", "other-launch", block, project_id="project")
        for task_id in ("target", "off-task"):
            plan = planner.TaskPlan("plan-" + task_id, "goal", "Inspect", str(tmp_path), status="executing")
            await plans.save(task_id, plan=plan.row())
        # Recovery must still quarantine historical launches after the operator closes the feature.
        await pool.execute("UPDATE system_settings SET value='{\"enabled\":false}' WHERE key='memory_lessons'")
        recover = AsyncMock(wraps=store.recover_prepared)
        monkeypatch.setattr(store, "recover_prepared", recover)
        bus = MessageBus(tmp_path / "bus")
        restored = planner.PlannerAgent(task_id="target", runtime_root=tmp_path)
        try:
            await restored.start(bus)
            assert restored._plan is not None
            assert [row["state"] for row in await pool.fetch("SELECT state FROM memory_injections "
                "WHERE id=ANY($1) ORDER BY id", lost)] == ["uncertain", "uncertain"]
            recover.assert_awaited_once_with(task_id="target")
            assert await pool.fetchval("SELECT state FROM memory_injections WHERE id=$1", other["id"]) == "prepared"
            assert not await pool.fetchval("SELECT count(*) FROM memory_lesson_stats")
            assert not list(tmp_path.rglob("manifest.json"))
            # Loading a plan again within the same owner never sweeps a current run's preparation.
            await pool.execute("UPDATE system_settings SET value='{\"enabled\":true}' WHERE key='memory_lessons'")
            current = await store.prepare_injection("target", "worker", "current-launch", block, project_id="project")
            await restored._load_plan()
            assert await pool.fetchval("SELECT state FROM memory_injections WHERE id=$1", current["id"]) == "prepared"
            assert recover.await_count == 1
        finally:
            await restored.stop()
        for task_id in ("off-task", "fresh-task"):
            agent = planner.PlannerAgent(task_id=task_id, runtime_root=tmp_path)
            try:
                await agent.start(bus)
                assert recover.await_count == 1
                assert not await pool.fetchval("SELECT count(*) FROM task_lesson_snapshot_sets WHERE task_id=$1", task_id)
                assert not await pool.fetchval("SELECT count(*) FROM memory_injections WHERE task_id=$1", task_id)
            finally:
                await agent.stop()
        model_turn.assert_not_awaited()

    run_db(check, monkeypatch)
