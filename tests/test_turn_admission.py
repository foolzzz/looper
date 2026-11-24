"""Turn admission contracts with synthetic accounts and cancellable stand-in turns."""
from __future__ import annotations

import ast
import asyncio
import inspect
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from looper.agents import admission, lanes, turn_admission
from looper.agents.agent_settings import AgentType
from looper.agents.lanes import Lane
from looper.agents.message import MessageType
from looper.agents.ops import TurnOrigin
from looper.agents.tiers.planner_protocol import TurnKind
from looper.agents.tiers.turn_token import TurnToken, TurnStopped

NOW = datetime(2026, 11, 23, 12, tzinfo=timezone.utc)


@pytest.mark.parametrize("origin", list(TurnOrigin))
@pytest.mark.parametrize("message_type", list(MessageType))
def test_manager_lanes(origin, message_type):
    assert turn_admission.manager_turn_lane(message_type, origin) is lanes.manager_lane(origin)


@pytest.mark.parametrize("message_type", list(MessageType))
def test_manager_without_origin(message_type):
    assert turn_admission.manager_turn_lane(message_type, None) is (
        Lane.INTERACTIVE if message_type is MessageType.USER_MESSAGE else Lane.BACKGROUND)


@pytest.mark.parametrize("kind", [None, *TurnKind])
@pytest.mark.parametrize("recovered", [False, True])
@pytest.mark.parametrize("task_lane", [Lane.USER_TASK, Lane.BACKGROUND])
def test_planner_lanes(kind, recovered, task_lane):
    expected = (Lane.INTERACTIVE if kind in (TurnKind.CHAT, TurnKind.GATE) else Lane.RECOVERY
                if kind is TurnKind.RESTART or recovered else task_lane)
    assert turn_admission.planner_turn_lane(kind, task_lane=task_lane, recovered=recovered) is expected


@pytest.mark.parametrize("windows", [(), ("five_hour",), ("five_hour", "seven_day")])
@pytest.mark.parametrize("zone", [ZoneInfo("Asia/Shanghai"), ZoneInfo("America/Los_Angeles")])
@pytest.mark.parametrize("known", [False, True])
def test_freeze_reply(windows, zone, known):
    text = turn_admission.freeze_reply("Synthetic", windows, NOW if known else None, zone)
    if known:
        names = f"（{'、'.join(windows)}）" if windows else ""
        assert text == (f"Synthetic 的额度已用完{names}，{NOW.astimezone(zone):%m-%d %H:%M} 重置。"
                        "你的消息已保留，额度恢复后自动回答；发 /stop 可取消。")
    else:
        assert text == ("Synthetic 的额度已用完，重置时间未知；Looper 会定时重新检查，"
                        "恢复后自动回答你的消息。发 /stop 可取消。")


@pytest.fixture
def controller(monkeypatch):
    from looper.db import profile_registry, provider_admission, provider_limits
    monkeypatch.setattr(profile_registry, "get_provider", AsyncMock(return_value={
        "id": "synthetic", "name": "Synthetic", "max_concurrency": 1, "vendor_type": "anthropic"}))
    monkeypatch.setattr(provider_limits, "windows", AsyncMock(return_value=[]))
    monkeypatch.setattr(provider_admission, "save", AsyncMock(return_value=1))
    monkeypatch.setattr(admission, "_freeze_alert", AsyncMock())
    c = admission.Controller(now=lambda: NOW)
    c.active = True
    monkeypatch.setattr(admission, "_controller", c)
    for name in ("slot", "wake", "snapshot"):
        monkeypatch.setattr(admission, name, getattr(c, name))
    monkeypatch.setattr(admission, "worker_lane", AsyncMock(return_value=Lane.BACKGROUND))
    return c


def prepared(origin=TurnOrigin.USER, key="main"):
    return NS(session=NS(key=key), launch=NS(profile=NS(provider=NS(id="synthetic"))),
              turn=NS(origin=origin), envelope=NS(message_type=MessageType.USER_MESSAGE, message_id="synthetic"))


def token():
    return TurnToken("synthetic", None, "web", "synthetic", NOW)


def manager():
    status = AsyncMock()
    return NS(agent_id="manager", _send_status=status, _freeze_notice_once=status)


async def wait_for_waiters(c, n):
    async with asyncio.timeout(2):
        while len(c.waiters) != n:
            await asyncio.sleep(0)


def test_worker_then_interactive_before_scheduled_planner(controller):
    async def run():
        c = controller
        order = []
        @turn_admission.admitted_manager_turn
        async def user(self, t, p):
            assert not t.admission_waiting
            order.append("manager")
        @turn_admission.admitted_planner_exchange
        async def exchange(self, text, process, limit, **kw):
            order.append("planner")
        planner = NS(agent_id="planner-synthetic", _task_id="synthetic", _turn_kind=TurnKind.DECISION,
                     _admission_recovered=False, _activity=AsyncMock())
        async with c.slot("synthetic", Lane.USER_TASK, owner="worker"):
            p = asyncio.create_task(exchange(planner, "[DECISION]", prepared(), 10))
            u = asyncio.create_task(user(manager(), token(), prepared()))
            await wait_for_waiters(c, 2)
            assert order == []
        await asyncio.gather(p, u)
        assert order == ["manager", "planner"]
        assert not c.running
    asyncio.run(run())


def test_stop_cancels_acquire_and_cleans_waiter(controller):
    async def run():
        c, t = controller, token()
        call = AsyncMock()
        wrapped = turn_admission.admitted_manager_turn(call)
        async with c.slot("synthetic", Lane.USER_TASK, owner="worker"):
            task = asyncio.create_task(wrapped(manager(), t, prepared()))
            await wait_for_waiters(c, 1)
            assert t.admission_waiting
            t.request_stop("user")
            with pytest.raises(TurnStopped):
                await asyncio.wait_for(task, 1)
            assert not t.admission_waiting
            assert not c.waiters
            assert list(c.running) == ["worker"]
        call.assert_not_called()
        assert not c.running
    asyncio.run(run())


def test_retry_reentrant_and_record_owner(controller):
    async def run():
        calls = 0
        @turn_admission.admitted_manager_turn
        async def method(self, t, p):
            nonlocal calls
            calls += 1
            assert turn_admission._current_owner.get() == "manager:main"
            assert list(controller.running) == ["manager:main"]
            if calls == 1:
                return await method(self, t, p)
            return "answer"
        assert await asyncio.wait_for(method(manager(), token(), prepared()), 1) == "answer"
        assert calls == 2 and not controller.running
        assert turn_admission._current_owner.get() is None
    asyncio.run(run())


def test_routed_sessions_have_independent_owners(controller):
    from looper.db import profile_registry

    async def run():
        profile_registry.get_provider.return_value["max_concurrency"] = None
        entered, release = asyncio.Event(), asyncio.Event()

        @turn_admission.admitted_manager_turn
        async def method(self, t, p):
            if len(controller.running) == 2:
                entered.set()
            await release.wait()

        jobs = [asyncio.create_task(method(manager(), token(), prepared(key=key)))
                for key in ("main", "group")]
        await asyncio.wait_for(entered.wait(), 1)
        assert set(controller.running) == {"manager:main", "manager:group"}
        release.set()
        await asyncio.gather(*jobs)
        assert not controller.running

    asyncio.run(run())


def test_background_freeze_does_not_send_a_user_notice(controller):
    async def run():
        await controller.transition("synthetic", admission.QuotaFailure(None, None))
        agent, t, call = manager(), token(), AsyncMock()
        job = asyncio.create_task(turn_admission.admitted_manager_turn(call)(
            agent, t, prepared(TurnOrigin.COMPLETION)))
        await wait_for_waiters(controller, 1)
        await controller.wake()
        agent._send_status.assert_not_called()
        call.assert_not_called()
        t.request_stop("preempt")
        with pytest.raises(TurnStopped):
            await asyncio.wait_for(job, 1)
        assert not controller.waiters and not controller.running

    asyncio.run(run())


def test_freeze_once_per_message_then_probe_and_reply(controller):
    async def run():
        c, a = controller, manager()
        await c.transition("synthetic", admission.QuotaFailure(NOW + timedelta(hours=1), None))
        calls = AsyncMock(return_value="answer")
        wrapped = turn_admission.admitted_manager_turn(calls)
        for _ in range(2):
            task = asyncio.create_task(wrapped(a, token(), prepared()))
            await wait_for_waiters(c, 1)
            async with asyncio.timeout(1):
                while a._send_status.await_count < _ + 1:
                    await asyncio.sleep(0)
            calls.assert_not_called() if _ == 0 else None
            await c.wake()
            assert a._send_status.await_count == _ + 1
            if _ == 0:
                c.now = lambda: NOW + timedelta(hours=1, seconds=61)
            else:
                await c.transition("synthetic", admission.OperatorClear())
            await c.wake()
            assert await asyncio.wait_for(task, 1) == "answer"
            if _ == 0:
                await c.transition("synthetic", admission.QuotaFailure(NOW + timedelta(hours=2), None))
        assert calls.await_count == 2 and a._send_status.await_count == 2
        assert "额度已用完" in a._send_status.call_args.args[1]
    asyncio.run(run())


@pytest.mark.parametrize("origin", [TurnOrigin.OWN, TurnOrigin.COMPLETION])
def test_own_bypasses_and_background_no_notice(controller, origin):
    async def run():
        a, call = manager(), AsyncMock(return_value="answer")
        wrapped = turn_admission.admitted_manager_turn(call)
        if origin is TurnOrigin.OWN:
            controller.lock = None
        assert await wrapped(a, token(), prepared(origin)) == "answer"
        a._send_status.assert_not_called()
    asyncio.run(run())


def test_planner_sequence_recovery_handoff_compact_and_cached_task_lane(controller):
    async def run():
        p = NS(agent_id="planner-synthetic", _task_id="synthetic", _turn_kind=TurnKind.DECISION,
               _admission_recovered=True, _activity=AsyncMock())
        seen = []
        @turn_admission.admitted_planner_exchange
        async def exchange(self, text, process, limit, **kw):
            seen.append(controller.running[p.agent_id].lane)
        for kind, maintenance in [(TurnKind.DECISION, False), (TurnKind.HANDOFF, False), (TurnKind.CHAT, True)]:
            p._turn_kind = kind
            await exchange(p, "message", prepared(), 10, maintenance=maintenance)
            assert not controller.running
        assert seen == [Lane.RECOVERY, Lane.BACKGROUND, Lane.BACKGROUND]
        admission.worker_lane.assert_awaited_once_with("synthetic", {}, role=AgentType.PLANNER)
    asyncio.run(run())


@pytest.mark.parametrize("draining", [False, True])
def test_switch_off_releases_waiting_turns_even_during_drain(controller, draining):
    async def run():
        c, mcall, pcall = controller, AsyncMock(), AsyncMock()
        c.draining = lambda: draining
        if not draining:
            await c.register("synthetic", Lane.USER_TASK, "worker")
        p = NS(agent_id="planner-synthetic", _task_id="synthetic", _turn_kind=TurnKind.PLAN,
               _admission_recovered=False, _activity=AsyncMock())
        tasks = [asyncio.create_task(turn_admission.admitted_manager_turn(mcall)(manager(), token(), prepared())),
                 asyncio.create_task(turn_admission.admitted_planner_exchange(pcall)(p, "text", prepared(), 1))]
        await wait_for_waiters(c, 2)
        mcall.assert_not_called()
        pcall.assert_not_called()
        await c.set_enabled(False)
        await asyncio.wait_for(asyncio.gather(*tasks), 1)
        assert not c.waiters
        assert set(c.running) <= {"worker"}
    asyncio.run(run())


def test_lane_lookup_fail_open(controller, monkeypatch, caplog):
    async def run():
        monkeypatch.setattr(admission, "worker_lane", AsyncMock(side_effect=RuntimeError("lookup")))
        p = NS(agent_id="planner-synthetic", _task_id="synthetic", _turn_kind=TurnKind.PLAN,
               _admission_recovered=False, _activity=AsyncMock())
        call = AsyncMock(return_value="answer")
        assert await turn_admission.admitted_planner_exchange(call)(p, "text", prepared(), 1) == "answer"
        assert "admission" in caplog.text.lower()
        assert any(r.levelname == "ERROR" for r in caplog.records)
    asyncio.run(run())


def test_preempts_waiting_background_without_interrupt(controller):
    async def run():
        c, t, a = controller, token(), manager()
        p = prepared(TurnOrigin.COMPLETION)
        t.ops_turn = p.turn
        session = NS(running_token=t, claude=NS(can_interrupt=False))
        call = AsyncMock()
        async with c.slot("synthetic", Lane.USER_TASK, owner="worker"):
            task = asyncio.create_task(turn_admission.admitted_manager_turn(call)(a, t, p))
            await wait_for_waiters(c, 1)
            from looper.agents.runtimes.cancel import StopCleanup
            before = t.cancel.cleanup
            assert lanes.preempt(session, ops=None)
            assert t.cancel.cleanup is before is StopCleanup.NONE
            with pytest.raises(TurnStopped):
                await asyncio.wait_for(task, 1)
            assert not c.waiters
        call.assert_not_called()
        a._send_status.assert_not_called()
    asyncio.run(run())


def test_exchange_callers_and_kind_capture():
    from looper.agents.tiers.planner import PlannerAgent
    tree = ast.parse(inspect.getsource(inspect.getmodule(PlannerAgent)))
    callers = set()
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "_exchange":
                    callers.add(fn.name)
                    if fn.name == "_switch_session":
                        assert any(isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                                   and call.func.attr == "_ask" and call.args
                                   and ast.unparse(call.args[0]) == "TurnKind.HANDOFF"
                                   and any(k.arg == "run" and isinstance(k.value, ast.Lambda) for k in call.keywords)
                                   for call in ast.walk(fn))
                    elif fn.name != "_run_turn":
                        assert any(k.arg == "maintenance" and isinstance(k.value, ast.Constant) and k.value.value
                                   for k in node.keywords)
    assert callers == {"_run_turn", "_switch_session"}
    ask = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_ask")
    assert isinstance(ask.body[1], ast.Assign)
    assert ast.unparse(ask.body[1]) == "self._turn_kind = kind"


def test_interactive_two_minute_protection_overadmits_worker(controller, caplog):
    async def run():
        c, called = controller, asyncio.Event()
        @turn_admission.admitted_manager_turn
        async def user(self, t, p):
            assert len(c.running) == 2
            called.set()
        async with c.slot("synthetic", Lane.USER_TASK, owner="worker"):
            pending = asyncio.create_task(user(manager(), token(), prepared()))
            await wait_for_waiters(c, 1)
            assert not called.is_set()
            c.now = lambda: NOW + timedelta(seconds=121)
            await c.wake()
            await asyncio.wait_for(pending, 1)
            assert called.is_set()
        assert c.daily_overadmissions() == 1
        assert any(r.levelname == "WARNING" and "two minutes" in r.message for r in caplog.records)
    asyncio.run(run())


def test_stop_racing_grant_releases_without_calling_model(controller):
    async def run():
        t, call = token(), AsyncMock()
        step = t.step
        async def stop_after_grant(name, aw):
            value = await step(name, aw)
            t.request_stop("user")
            return value
        t.step = stop_after_grant
        with pytest.raises(TurnStopped):
            await turn_admission.admitted_manager_turn(call)(manager(), t, prepared())
        assert not controller.running and not controller.waiters
        assert not t.admission_waiting
        call.assert_not_called()
    asyncio.run(run())


def test_unlimited_open_account_never_waits(controller):
    from looper.db import profile_registry
    async def run():
        profile_registry.get_provider.return_value["max_concurrency"] = None
        controller._notify_waiters = AsyncMock()
        call = AsyncMock(return_value="answer")
        assert await turn_admission.admitted_manager_turn(call)(manager(), token(), prepared()) == "answer"
        assert not controller.waiters and not controller.running
    asyncio.run(run())


def test_chat_keeps_interactive_lane_after_nested_handoff(controller, monkeypatch, tmp_path, agent_db):
    from looper.agents import untrusted
    from looper.agents.system_prompt import SystemPrompt
    from looper.agents.tiers import planner as module
    from tests.runtime_model_helpers import builtin_launch
    async def run():
        a = module.PlannerAgent(task_id="synthetic", runtime_root=tmp_path)
        a._plan = module.TaskPlan("plan", "goal", "Inspect", str(tmp_path))
        a._plan.session_id = "saved-session"
        launch = builtin_launch(role=AgentType.PLANNER)
        process = module._Process(["synthetic-runtime"], None, SystemPrompt("Synthetic"), launch)
        a._process = process
        a._claude = NS(started=True, pid=None, label=a.agent_id, stale=lambda signature: False)
        monkeypatch.setattr(a, "_load_knowledge", AsyncMock(return_value=launch.knowledge))
        monkeypatch.setattr(a, "_load_installed", AsyncMock(return_value=launch.library))
        monkeypatch.setattr(a, "_load_agent_settings", AsyncMock(return_value=launch.settings))
        monkeypatch.setattr(a, "_read_run_deadline", AsyncMock())
        monkeypatch.setattr(a, "_read_roles", AsyncMock())
        monkeypatch.setattr(a, "_record_profile_fallbacks", AsyncMock())
        monkeypatch.setattr(a, "_note_background", AsyncMock())
        monkeypatch.setattr(a, "_system_prompt", lambda *args, **kw: SystemPrompt("Synthetic"))
        monkeypatch.setattr(a, "_claude_command", lambda *args: ["synthetic-runtime"])
        monkeypatch.setattr(module.llmrec, "attach", AsyncMock(return_value=launch))
        monkeypatch.setattr(module.session_switch, "profile_switch", lambda *args, **kw: NS(reason=None))
        monkeypatch.setattr(untrusted, "enabled", AsyncMock(return_value=False))
        observed = []
        @turn_admission.admitted_planner_exchange
        async def exchange(self, text, process, limit, **kw):
            observed.append(controller.running[self.agent_id].lane)
            return "answer"
        a._exchange = exchange.__get__(a)
        async def switch(*args):
            await a._ask(TurnKind.HANDOFF, "[HANDOFF]", lambda value: value,
                         run=lambda text: a._exchange(text, process, 10))
        monkeypatch.setattr(a, "_switch_session", switch)
        assert await a._ask(TurnKind.CHAT, "[CHAT]", lambda value: value) == "answer"
        assert observed == [Lane.BACKGROUND, Lane.INTERACTIVE]
        assert not controller.running
    asyncio.run(run())


@pytest.mark.parametrize("schedule_id,run_now,expected", [
    (None, False, Lane.USER_TASK), ("synthetic-schedule", False, Lane.BACKGROUND),
    ("synthetic-schedule", True, Lane.USER_TASK),
])
def test_planner_task_origin_table(schedule_id, run_now, expected):
    task_lane = admission.lane_for(role=AgentType.PLANNER, schedule_id=schedule_id, run_now=run_now)
    assert turn_admission.planner_turn_lane(TurnKind.DECISION, task_lane=task_lane, recovered=False) is expected


def test_manager_lane_lookup_fail_open(controller, monkeypatch, caplog):
    async def run():
        def broken_lane(*args):
            raise RuntimeError("lookup")
        monkeypatch.setattr(turn_admission, "manager_turn_lane", broken_lane)
        call = AsyncMock(return_value="answer")
        assert await turn_admission.admitted_manager_turn(call)(manager(), token(), prepared()) == "answer"
        assert any(r.levelname == "ERROR" for r in caplog.records)
        assert not controller.running
    asyncio.run(run())


def test_shutdown_cancels_planner_and_manager_waiting_during_drain(controller):
    async def run():
        c, call = controller, AsyncMock()
        c.draining = lambda: True
        p = NS(agent_id="planner-synthetic", _task_id="synthetic", _turn_kind=TurnKind.PLAN,
               _admission_recovered=False, _activity=AsyncMock())
        jobs = [asyncio.create_task(turn_admission.admitted_manager_turn(call)(manager(), token(), prepared())),
                asyncio.create_task(turn_admission.admitted_planner_exchange(call)(p, "text", prepared(), 1))]
        await wait_for_waiters(c, 2)
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        call.assert_not_called()
        assert not c.waiters and not c.running
    asyncio.run(run())


def test_resumed_planner_first_real_exchange_uses_recovery_only_once(
    controller, tmp_path, agent_db, planner_claude,
):
    from looper.agents.message import MessageEnvelope, planner_address
    from looper.agents.tiers.planner import PlanStep, PlannerAgent, TaskPlan

    reply = ('```json\n{"decision":"accept","final":{"delivered":"Saved work",'
             '"how_to_use":"Use the result","verification":"Checked","open_issues":"None"}}\n```')
    claude = planner_claude(reply, reply)

    async def run():
        planner = PlannerAgent(task_id="resume-admission", runtime_root=tmp_path, claude_bin=claude.bin)
        planner._bus = NS(publish=AsyncMock())
        planner._plan = TaskPlan("resume-plan", "resume-goal", "Finish saved work", str(tmp_path),
            status="executing", steps=[
                PlanStep(step_id=f"saved-{index}", index=index, instruction="Finish saved work",
                         status="deciding", result={"status": "completed", "summary": "Done"})
                for index in range(2)
            ])
        try:
            await asyncio.wait_for(planner._resume(MessageEnvelope.create(
                source=planner_address("previous"), target=planner.agent_address,
                message_type=MessageType.TASK_RESUME, payload={})), 10)
            assert planner._plan.status == "completed"
            assert [call.kwargs["metadata"]["admission_lane"]
                    for call in agent_db.start_record.await_args_list] == [Lane.RECOVERY.value, Lane.BACKGROUND.value]
            assert len(claude.turns()) == 2
            assert all("[DECISION] Step" in turn["text"] for turn in claude.turns())
            assert not controller.running
        finally:
            await planner.stop()
            await controller.stop()

    asyncio.run(run())
