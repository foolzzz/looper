"""Persistent holds through the Planner's real result, dispatch and event boundaries."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import failure_handling
from looper.agents.failures import FailureTyping
from looper.agents.message import MessageEnvelope, MessageType, planner_address, worker_address
from looper.agents.tiers import planner
from tests.db_support import run_db


@pytest.mark.parametrize(("review", "budget_wait"), [(False, False), (True, False), (True, True)])
def test_runtime_failure_waits_and_recovers_only_the_failed_execution(monkeypatch, tmp_path, review, budget_wait):
    async def body(pool):
        await pool.execute("INSERT INTO projects(id,name) VALUES ('project','Synthetic recovery')")
        await pool.execute("INSERT INTO tasks(id,project_id,requirement,status) VALUES "
                           "('task','project','Recover safely','executing')")
        await pool.execute("INSERT INTO system_settings(key,value) VALUES ('engine.failure_typing','act')")
        events = []
        async def emit(task_id, payload):
            events.append(payload)
        service = failure_handling.FailureHandlingService(pool=pool, mode=FailureTyping.ACT, publish=emit)
        monkeypatch.setattr(failure_handling, "_service", service)
        agent = planner.PlannerAgent(task_id="task", runtime_root=tmp_path)
        sent = []
        agent._bus = SimpleNamespace(publish=AsyncMock(side_effect=sent.append))
        unit = planner.PlanStep("step", 0, "Keep the instruction", key="work", review=review,
            worker_id="worker-one", reviewer_id="reviewer-one" if review else None,
            status="completed" if review else "dispatched",
            result={"status": "completed", "summary": "Existing worker output", "changed_files": {}} if review else None)
        plan = agent._plan = planner.TaskPlan("plan", "goal", "Recover safely", str(tmp_path),
                                             [unit], status="executing", project_id="project")
        await agent._save(plan, unit, plan_row=True)
        decide = AsyncMock()
        monkeypatch.setattr(agent, "_decide", decide)
        origin = "reviewer-one" if review else "worker-one"
        result = {"task_id": "step", "status": "failed", "passed": False, "summary": "Login required",
            "engine_run": {"outcome": "run_failed", "record_id": None, "provider_id": None,
                           "failure": {"class": "environment", "cause": "login", "signal": "typed"}}}
        envelope = MessageEnvelope.create(source=worker_address(origin), target=planner_address("task"),
            message_type=MessageType.VERIFY_RESULT if review else MessageType.WORK_RESULT, payload=result)
        try:
            await (agent._handle_verify_result(envelope) if review else agent._handle_work_result(envelope))
            decide.assert_not_awaited()
            assert unit.status == "pending" and unit.retries == 0
            assert not plan.ready_steps()
            assert planner._reported_status(plan) == "waiting_user"
            hold = (await service.get_holds("task"))[0]
            assert hold["targets"][0]["target"]["kind"] == ("reviewer" if review else "worker")
            assert await pool.fetchval("SELECT count(*) FROM task_user_gates") == 0
            # Simulate loading the persisted plan projection, without trusting runtime hold state.
            unit.failure_hold = None
            await agent._restore_failure_holds(plan)
            assert unit.failure_hold["failure_id"] == hold["failure_id"]
            await service.release_hold("task", "step", hold["failure_id"], "retry", operator=True)
            unit.failure_hold = None
            await agent._restore_failure_holds(plan)
            assert unit.failure_hold["kind"] == "recovery"
            assert not plan.ready_steps()  # released, but not yet applied by the actor
            if budget_wait:
                with monkeypatch.context() as patch:
                    patch.setattr(agent, "_within_budget", AsyncMock(return_value=False))
                    await agent._handle_system_event(MessageEnvelope.create(source=planner_address("task"),
                        target=planner_address("task"), message_type=MessageType.SYSTEM_EVENT, payload=events[-1]))
                assert not any(e.message_type in (MessageType.WORK_ITEM, MessageType.VERIFY_REQUEST) for e in sent)
                assert unit.failure_hold["kind"] == "recovery"
                assert await pool.fetchval("SELECT delivered_at IS NULL FROM step_failure_releases")
                await agent._restore_failure_holds(plan)
            await agent._handle_system_event(MessageEnvelope.create(source=planner_address("task"),
                target=planner_address("task"), message_type=MessageType.SYSTEM_EVENT, payload=events[-1]))
            await agent._handle_system_event(MessageEnvelope.create(source=planner_address("task"),
                target=planner_address("task"), message_type=MessageType.SYSTEM_EVENT, payload=events[-1]))
            work = [e for e in sent if e.message_type is MessageType.WORK_ITEM]
            reviews = [e for e in sent if e.message_type is MessageType.VERIFY_REQUEST]
            assert (len(work), len(reviews)) == ((0, 1) if review else (1, 0))
            dispatch = (reviews if review else work)[0]
            assert dispatch.payload["instruction"] == "Keep the instruction"
            assert dispatch.payload["failure_retry"]["cause"] == "login"
            if review:
                assert dispatch.payload["work_result"]["summary"] == "Existing worker output"
            assert await pool.fetchval("SELECT streak FROM step_failure_state") == 1
            assert await pool.fetchval("SELECT count(*) FROM step_failures") == 1
            assert unit.retries == 0
            assert await pool.fetchval("SELECT delivered_at IS NOT NULL FROM step_failure_releases")
        finally:
            await service.close()
    run_db(body, monkeypatch, timeout=40)


def test_panel_runtime_judgment_goes_to_decision_without_becoming_a_vote(monkeypatch, tmp_path):
    async def body(pool):
        await pool.execute("INSERT INTO projects(id,name) VALUES ('project','Synthetic panel')")
        await pool.execute("INSERT INTO tasks(id,project_id,requirement,status) VALUES "
                           "('task','project','Review safely','executing')")
        await pool.execute("INSERT INTO system_settings(key,value) VALUES ('engine.failure_typing','act')")
        service = failure_handling.FailureHandlingService(pool=pool, mode=FailureTyping.ACT)
        monkeypatch.setattr(failure_handling, "_service", service)
        agent = planner.PlannerAgent(task_id="task", runtime_root=tmp_path)
        agent._bus = SimpleNamespace(publish=AsyncMock())
        work = planner.PlanStep("work", 0, "Keep output", key="work", status="completed", worker_id="worker",
                                result={"status": "completed", "summary": "Keep this output"})
        seat = planner.PlanStep("seat", 1, "Review", key="seat", reviews="work", status="dispatched",
                                worker_id="reviewer", reviewer_id="reviewer")
        plan = agent._plan = planner.TaskPlan("plan", "goal", "Review safely", str(tmp_path), [work, seat],
                                             status="executing", project_id="project")
        await agent._save(plan, work, seat, plan_row=True)
        decide, vote = AsyncMock(), AsyncMock(side_effect=AssertionError("runtime failure became a vote"))
        monkeypatch.setattr(agent, "_decide", decide)
        monkeypatch.setattr(agent, "_review_done", vote)
        from looper.db import journal as db_journal
        monkeypatch.setattr(db_journal, "list_entries", AsyncMock(side_effect=RuntimeError("synthetic journal unavailable")))
        try:
            await agent._handle_verify_result(MessageEnvelope.create(source=worker_address("reviewer"),
                target=planner_address("task"), message_type=MessageType.VERIFY_RESULT,
                payload={"task_id": "seat", "passed": False, "summary": "Contract unreadable",
                    "engine_run": {"outcome": "contract", "failure": {"cause": "report", "signal": "typed"}}}))
            decide.assert_awaited_once()
            vote.assert_not_awaited()
            assert work.result["summary"] == "Keep this output"
            assert seat.status == "pending"
            assert await pool.fetchval("SELECT status FROM task_steps WHERE id='seat'") == "pending"
            assert await pool.fetchval("SELECT step_identity FROM step_failures") == "work"
            assert work.failure_history[0]["target"]["unit_step_id"] == "seat"
            work.failure_history.clear()  # a restored actor has only the persisted evidence
            monkeypatch.setattr(agent, "_decide", planner.PlannerAgent._decide.__get__(agent))
            reply = SimpleNamespace(decision=planner.Decision.FAIL)
            ask = AsyncMock(return_value=reply)
            monkeypatch.setattr(agent, "_ask", ask)
            monkeypatch.setattr(agent, "_apply_decision", AsyncMock())
            context = planner.DecisionContext("work", planner.ResultKind.REVIEW, False, True, 0, 2)
            await agent._decide(plan, work, context, ["Review execution failed"])
            assert "Consecutive execution failures" in ask.await_args.args[1]
            assert "judgment (report)" in ask.await_args.args[1]
        finally:
            await service.close()
    run_db(body, monkeypatch, timeout=40)


def test_replan_replaces_a_held_step_without_resetting_failure_state(monkeypatch, tmp_path):
    from looper.agents.failures import Failure, FailureCause
    from looper.agents.tiers.planner_protocol import StepSpec

    async def body(pool):
        await pool.execute("INSERT INTO projects(id,name) VALUES ('project','Synthetic replan')")
        await pool.execute("INSERT INTO tasks(id,project_id,requirement,status) VALUES "
                           "('task','project','Recover safely','executing')")
        service = failure_handling.FailureHandlingService(pool=pool, mode=FailureTyping.ACT)
        monkeypatch.setattr(failure_handling, "_service", service)
        agent = planner.PlannerAgent(task_id="task", runtime_root=tmp_path)
        agent._bus = SimpleNamespace(publish=AsyncMock())
        held = planner.PlanStep("held", 0, "Old instruction", key="held", status="dispatched", worker_id="old")
        decided = planner.PlanStep("other", 1, "Other", key="other", status="verified")
        plan = agent._plan = planner.TaskPlan("plan", "goal", "Recover safely", str(tmp_path), [held, decided],
                                             status="executing", project_id="project")
        await agent._save(plan, held, decided, plan_row=True)
        try:
            handling = await service.handle_step_result(task_id="task", step_id="held", run_id="old", role="worker",
                failure=Failure(FailureCause.LOGIN), target={"kind": "worker", "source": "old"}, result={})
            held.failure_hold, held.status = handling.hold, "pending"
            await agent._replan(plan, (StepSpec("held", "Replacement instruction", (), (), False),), decided, "Replace")
            assert not await service.get_holds("task")
            assert held.status == "pending" and held.failure_hold is None
            assert held.worker_id is None and held.instruction == "Replacement instruction"
            assert await pool.fetchval("SELECT streak FROM step_failure_state") == 1
            assert await pool.fetchval("SELECT count(*) FROM step_failures") == 1
        finally:
            await service.close()
    run_db(body, monkeypatch, timeout=40)


@pytest.mark.parametrize("leave_act", [False, True])
def test_expired_step_quota_event_ends_the_task_without_another_model_turn(monkeypatch, tmp_path, leave_act):
    from looper.agents.failures import Failure, FailureCause

    async def body(pool):
        await pool.execute("INSERT INTO projects(id,name) VALUES ('project','Synthetic quota expiry')")
        await pool.execute("INSERT INTO tasks(id,project_id,requirement,status) VALUES "
                           "('task','project','Bound quota waiting','executing')")
        await pool.execute("INSERT INTO system_settings(key,value) VALUES ('engine.failure_typing','act')")
        events = []
        async def publish(task_id, payload):
            events.append(payload)
        service = failure_handling.FailureHandlingService(pool=pool, mode=FailureTyping.ACT, publish=publish)
        monkeypatch.setattr(failure_handling, "_service", service)
        agent = planner.PlannerAgent(task_id="task", runtime_root=tmp_path)
        sent = []
        agent._bus = SimpleNamespace(publish=AsyncMock(side_effect=sent.append))
        step = planner.PlanStep("step", 0, "Wait safely", key="work", status="dispatched", worker_id="worker")
        plan = agent._plan = planner.TaskPlan("plan", "goal", "Bound quota waiting", str(tmp_path), [step],
                                             status="executing", project_id="project")
        await agent._save(plan, step, plan_row=True)
        model = AsyncMock(side_effect=AssertionError("quota expiration started a model turn"))
        monkeypatch.setattr(agent, "_ask", model)
        try:
            handled = await service.handle_step_result(task_id="task", step_id="step", run_id="worker", role="worker",
                failure=Failure(FailureCause.RATE_LIMIT), target={"kind": "worker", "source": "worker"}, result={})
            step.status, step.failure_hold = "pending", handled.hold
            await service.release_hold("task", "step", handled.failure["id"], "planner", operator=True)
            expired = {**events[-1], "quota_exhausted": True}
            decision = AsyncMock()
            if leave_act:
                await service.set_mode("off")
                monkeypatch.setattr(agent, "_decide", decision)
            await agent._failure_hold_event(plan, expired)
            statuses = [item.payload for item in sent if item.message_type is MessageType.GOAL_STATUS]
            if leave_act:
                assert plan.status == "executing"
                decision.assert_awaited_once()
                assert not any(item.get("fail_kind") for item in statuses)
            else:
                assert plan.status == "failed"
                assert len(statuses) == 1 and statuses[0]["fail_kind"] == "quota_exhausted"
            assert await pool.fetchval("SELECT count(*) FROM step_failures") == 1
            model.assert_not_awaited()
        finally:
            await service.close()
    run_db(body, monkeypatch, timeout=40)


@pytest.mark.parametrize("review", [False, True])
def test_release_before_resume_bootstrap_dispatches_exactly_once(monkeypatch, tmp_path, review):
    import asyncio
    from looper.agents.bus import MessageBus
    from looper.agents.failures import Failure, FailureCause
    from tests.db_support import until as wait_until

    async def body(pool):
        await pool.execute("INSERT INTO projects(id,name,work_dir) VALUES ('project','Synthetic startup',$1)", str(tmp_path))
        await pool.execute("INSERT INTO tasks(id,project_id,requirement,status,work_dir) VALUES "
                           "('task','project','Resume once','executing',$1)", str(tmp_path))
        await pool.execute("INSERT INTO system_settings(key,value) VALUES ('engine.failure_typing','act')")
        events, dispatched = [], []
        async def publish(task_id, payload):
            events.append(payload)
        service = failure_handling.FailureHandlingService(pool=pool, mode=FailureTyping.ACT, publish=publish)
        monkeypatch.setattr(failure_handling, "_service", service)
        bus = MessageBus(runtime_root=tmp_path)
        bus.subscribe_topic(MessageType.WORK_ITEM, dispatched.append)
        bus.subscribe_topic(MessageType.VERIFY_REQUEST, dispatched.append)
        agent = planner.PlannerAgent(task_id="task", runtime_root=tmp_path)
        unit = planner.PlanStep("step", 0, "Resume once", key="work", review=review, worker_id="old-worker",
            reviewer_id="old-reviewer" if review else None, status="completed" if review else "dispatched",
            result={"status": "completed", "summary": "Keep worker output", "changed_files": {}} if review else None)
        plan = planner.TaskPlan("plan", "goal", "Resume once", str(tmp_path), [unit],
                                status="executing", project_id="project")
        await agent._save(plan, unit, plan_row=True)
        source = "old-reviewer" if review else "old-worker"
        handled = await service.handle_step_result(task_id="task", step_id="step", run_id=source,
            role="reviewer" if review else "worker", failure=Failure(FailureCause.LOGIN),
            target={"kind": "reviewer" if review else "worker", "unit_step_id": "step", "source": source},
            result={"status": "failed", "summary": "Login required"})
        try:
            await agent.start(bus)
            assert agent._failure_bootstrap_pending
            await service.release_hold("task", "step", handled.failure["id"], "retry", operator=True)
            released = MessageEnvelope.create(source=planner_address("task"), target=planner_address("task"),
                message_type=MessageType.SYSTEM_EVENT, payload=events[-1])
            await agent._handle(released)  # force the real startup window before TASK_RESUME
            assert not dispatched
            assert await pool.fetchval("SELECT delivered_at IS NULL FROM step_failure_releases")
            await bus.publish(MessageEnvelope.create(source=planner_address("task"), target=planner_address("task"),
                message_type=MessageType.TASK_RESUME, payload={"task_id": "task"}))
            await wait_until(lambda: len(dispatched) >= 1, "one recovery dispatch", timeout=15)
            await bus.publish(released)  # a duplicate restoration event is harmless
            await asyncio.sleep(.05)
            assert len(dispatched) == 1
            assert dispatched[0].message_type is (MessageType.VERIFY_REQUEST if review else MessageType.WORK_ITEM)
            decide = AsyncMock()
            monkeypatch.setattr(agent, "_decide", decide)
            await bus.publish(MessageEnvelope.create(source=dispatched[0].target, target=agent.agent_address,
                message_type=MessageType.VERIFY_RESULT if review else MessageType.WORK_RESULT,
                payload={"task_id": "step", "status": "completed", "passed": True, "summary": "Recovered",
                         "engine_run": {"outcome": "passed" if review else "completed", "failure": None}}))
            await wait_until(lambda: decide.await_count == 1, "the unique recovered result", timeout=15)
            assert await pool.fetchval("SELECT streak FROM step_failure_state") == 0
            assert await pool.fetchval("SELECT count(*) FROM step_failures") == 1
        finally:
            await agent.stop()
            await service.close()
            await bus.shutdown()
    run_db(body, monkeypatch, timeout=40)
