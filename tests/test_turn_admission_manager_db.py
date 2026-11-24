"""Frozen Manager messages retain FIFO order, journal identity and one notice across restart."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from manager_mcp_stand_in import manager_endpoint
from looper.agents import admission, lanes_settings, session_routing
from looper.agents.bus import MessageBus
from looper.agents.message import MessageType
from looper.agents.tiers.manager import ManagerAgent, ManagerConfig
from tests.api_manager_support import USAGE, WEB, _manager, _message, _profile
from tests.db_support import DSN, run_db, until
from tests.fake_model_api import FakeModelAPI, ResponseScript, text_events

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
PROVIDER = "prov-api-test"


def _controller(monkeypatch, controller=None):
    c = controller or admission.Controller()
    c.active = True
    monkeypatch.setattr(admission, "_controller", c)
    for name in ("slot", "transition", "wake", "snapshot", "register", "release"):
        monkeypatch.setattr(admission, name, getattr(c, name))
    monkeypatch.setattr(admission, "_freeze_alert", AsyncMock())
    monkeypatch.setattr(lanes_settings, "enabled", lambda: True)
    monkeypatch.setattr(session_routing, "routes", lambda _: False)
    return c


def _drain_items(bus):
    queue = bus.get_queue(WEB.agent_id)
    items = []
    while not queue.empty():
        items.append(queue.get_nowait())
    return items


def _env(monkeypatch):
    from looper.agents import profile_registry
    from looper.agents.runtimes.api.session import FORBIDDEN_ENV
    monkeypatch.setattr(profile_registry, "_login_modules", lambda: {})
    for name in FORBIDDEN_ENV:
        monkeypatch.delenv(name, raising=False)


def test_restart_replays_frozen_message_without_repeating_the_notice(monkeypatch, tmp_path):
    """Freeze -> message waits -> process stops -> new process (new bus on same journal, new controller loaded
    from storage) -> clear -> exactly one model call and one answer."""
    _env(monkeypatch)

    async def run(pool):
        c1 = _controller(monkeypatch)
        async with FakeModelAPI([ResponseScript(events=text_events("After restart.", usage=USAGE))]) as model:
            await _profile(pool, model.base_url)
            reset = datetime.now(timezone.utc) + timedelta(hours=1)
            await c1.transition(PROVIDER, admission.QuotaFailure(reset, None))
            # Process 1
            bus1 = MessageBus(runtime_root=tmp_path)
            m1 = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
            m1._bus = bus1
            user = _message("Keep me across restart.")
            async with manager_endpoint(m1, bus1, SimpleNamespace(has_planner=lambda _: False), tmp_path):
                await bus1.start()
                await m1.start(bus1)
                await bus1.publish(user)
                await until(lambda: len(c1.waiters) == 1, "first process waits frozen")
                await until(lambda: any("额度已用完" in str(e.payload)
                    for e in bus1.get_queue(WEB.agent_id)._queue), "first freeze notice")
                first = [e for e in _drain_items(bus1) if "额度已用完" in str(e.payload)]
                await m1.stop()
                await c1.stop()
            await bus1.shutdown()
            undelivered = [e.message_id for e in MessageBus(runtime_root=tmp_path).journal.undelivered()]
            # Process 2: controller restored from storage only.
            c2 = _controller(monkeypatch, admission.Controller())
            await c2.load_states()
            restored = c2._state(PROVIDER).state
            bus2 = MessageBus(runtime_root=tmp_path)
            m2 = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
            m2._bus = bus2
            async with manager_endpoint(m2, bus2, SimpleNamespace(has_planner=lambda _: False), tmp_path):
                await bus2.start()
                await m2.start(bus2)
                await until(lambda: len(c2.waiters) == 1, "replayed message waits frozen in process 2")
                await c2.wake()  # join the locked freeze callback before checking that it sent nothing
                second_items = _drain_items(bus2)
                second = [e for e in second_items if "额度已用完" in str(e.payload)]
                await c2.transition(PROVIDER, admission.OperatorClear())
                await until(lambda: len(model.requests) == 1, "answered after clear")
                await until(lambda: any(e.message_type is MessageType.AGENT_REPLY
                    for e in bus2.get_queue(WEB.agent_id)._queue), "answer after restart")
                answers = [e for e in _drain_items(bus2) if e.message_type is MessageType.AGENT_REPLY]
                assert len(first) == 1
                assert second == []
                await m2.stop()
                await c2.stop()
            await bus2.shutdown()
            assert user.message_id in undelivered
            assert restored is admission.AdmissionState.FROZEN
            assert [a.payload.get("text") for a in answers] == ["After restart."]
            assert len(model.requests) == 1

    run_db(run, monkeypatch, timeout=60)


@pytest.mark.parametrize("frozen", [True, False])
def test_frozen_turn_preserves_user_order_without_blocking_task_reports_with_lanes_off(monkeypatch, tmp_path, frozen):
    _env(monkeypatch)

    async def run(pool):
        from looper.agents.message import MANAGER_ADDRESS, TASK_ROUTER_ADDRESS, MessageEnvelope
        c = _controller(monkeypatch)
        monkeypatch.setattr(lanes_settings, "enabled", lambda: False)
        async with FakeModelAPI([
            ResponseScript(events=text_events("First answer.", usage=USAGE)),
            ResponseScript(events=text_events("Second answer.", usage=USAGE)),
        ]) as model:
            await _profile(pool, model.base_url)
            await c.transition(PROVIDER, admission.QuotaFailure(None, None))
            bus = MessageBus(runtime_root=tmp_path)
            m = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
            m._bus = bus
            async with manager_endpoint(m, bus, SimpleNamespace(has_planner=lambda _: False), tmp_path):
                await bus.start()
                await m.start(bus)
                if frozen:
                    await bus.publish(_message("Frozen question."))
                    await until(lambda: len(c.waiters) == 1, "waits frozen")
                    await bus.publish(_message("Second question.", index=1))
                    await until(lambda: m._router.queued_count(m._sessions["main"]) == 1,
                                "second question remains behind the frozen first question")
                await bus.publish(MessageEnvelope.create(
                    source=TASK_ROUTER_ADDRESS, target=MANAGER_ADDRESS, message_type=MessageType.WORK_RESULT,
                    payload={"summary": "Other-account task finished."},
                    metadata={"origin_channel_id": WEB.agent_id}))
                queue = bus.get_queue(WEB.agent_id)
                await until(lambda: any(e.payload.get("text") == "Other-account task finished."
                                       for e in queue._queue), "task result bypasses frozen user turn", timeout=3)
                assert not model.requests
                if frozen:
                    await c.transition(PROVIDER, admission.OperatorClear())
                    await until(lambda: len([e for e in queue._queue
                        if e.message_type is MessageType.AGENT_REPLY]) == 3,
                        "both questions answered after recovery")
                    replies = [e.payload.get("text") for e in _drain_items(bus)
                               if e.message_type is MessageType.AGENT_REPLY]
                    assert replies == ["Other-account task finished.", "First answer.", "Second answer."]
                    assert len(model.requests) == 2
                await m.stop()
                await c.stop()
            await bus.shutdown()

    run_db(run, monkeypatch, timeout=40)


def test_freeze_notice_checkpoint_failure_keeps_message_waiting(monkeypatch, tmp_path):
    async def run(pool):
        c = _controller(monkeypatch)
        script = ResponseScript(events=text_events("Recovered despite notice failure.", usage=USAGE))
        async with _manager(pool, tmp_path, monkeypatch, [script]) as chat:
            await c.transition(PROVIDER, admission.QuotaFailure(None, None))
            def unavailable(*args):
                raise OSError("synthetic journal failure")
            monkeypatch.setattr(chat.bus.journal, "claim_notice", unavailable)
            await chat.bus.start()
            await chat.manager.start(chat.bus)
            await chat.bus.publish(_message("Retain me if the notice cannot be saved."))
            await until(lambda: c.waiters and c.waiters[0].frozen_told, "freeze notification attempted")
            await c.wake()
            assert not chat.model.requests and not c.running
            assert await pool.fetchval("SELECT count(*) FROM llm_records") == 0
            assert not any("额度已用完" in str(e.payload)
                           for e in chat.bus.get_queue(WEB.agent_id)._queue)
            await c.transition(PROVIDER, admission.OperatorClear())
            await until(lambda: any(e.message_type is MessageType.AGENT_REPLY
                for e in chat.bus.get_queue(WEB.agent_id)._queue), "retained question answered")
            assert len(chat.model.requests) == 1
            assert [e.payload.get("text") for e in _drain_items(chat.bus)
                    if e.message_type is MessageType.AGENT_REPLY] == ["Recovered despite notice failure."]
            await c.stop()

    run_db(run, monkeypatch, timeout=40)


@pytest.mark.parametrize("origin", ["idle", "completion"])
def test_frozen_background_turn_does_not_block_code_notifications_with_lanes_off(monkeypatch, tmp_path, origin):
    from looper.agents import chat_settings, completion_events
    from looper.agents.lanes import Lane
    from looper.agents.message import MANAGER_ADDRESS, TASK_ROUTER_ADDRESS, MessageEnvelope
    from looper.db import idle_checks, projects, tasks

    monkeypatch.setattr(chat_settings, "_completion_turns", chat_settings.CompletionTurns.DM_ONLY)

    async def run(pool):
        c = _controller(monkeypatch)
        monkeypatch.setattr(lanes_settings, "enabled", lambda: False)
        async with _manager(pool, tmp_path, monkeypatch, []) as chat:
            await c.transition(PROVIDER, admission.QuotaFailure(None, None))
            if origin == "idle":
                check_id = await idle_checks.insert_running(pool, manager_id="manager",
                    local_date=datetime.now(timezone.utc).date(),
                    items=[{"section": "logins", "key": "login:synthetic", "since": None,
                            "title": "Synthetic login", "facts": {}}],
                    channel_id="channel-lark", chat="ou_idle_synthetic")
                wake = MessageEnvelope.create(source=MANAGER_ADDRESS, target=MANAGER_ADDRESS,
                    message_type=MessageType.IDLE_CHECK_DUE, payload={"check_id": check_id})
            else:
                project = await projects.create_project(name="frozen-background", work_dir=str(tmp_path), pool=pool)
                await tasks.create_task(task_id="frozen-background-task", requirement="Synthetic completed task",
                    project_id=project["id"], work_dir=str(tmp_path), status="completed",
                    title="Background task", pool=pool, origin_thread="api-chat")
                await chat.manager._handle(MessageEnvelope.create(source=TASK_ROUTER_ADDRESS,
                    target=MANAGER_ADDRESS, message_type=MessageType.GOAL_STATUS,
                    payload={"task_id": "frozen-background-task", "goal_id": "synthetic-goal",
                             "status": "completed", "summary": "Synthetic task completed."},
                    metadata={"task_title": "Background task", "origin_thread": "api-chat",
                              "origin_channel_id": WEB.agent_id}))
                wake = completion_events.wake(MANAGER_ADDRESS,
                    completion_events.Target("manager", WEB.agent_id, "api-chat"))
                _drain_items(chat.bus)
            await chat.bus.start()
            await chat.manager.start(chat.bus)
            await chat.bus.publish(wake)
            await until(lambda: len(c.waiters) == 1, "background turn frozen before model admission")
            assert c.waiters[0].lane is Lane.BACKGROUND
            from looper.agents.managers import ManagerFleet
            fleet = object.__new__(ManagerFleet)
            monkeypatch.setattr(fleet, "agents", lambda: [chat.manager])
            assert fleet.drain_waiting() == {"manager_turns": []}
            await chat.bus.publish(MessageEnvelope.create(source=TASK_ROUTER_ADDRESS,
                target=MANAGER_ADDRESS, message_type=MessageType.WORK_RESULT,
                payload={"summary": "Independent task result."},
                metadata={"origin_channel_id": WEB.agent_id}))
            await until(lambda: any(e.payload.get("text") == "Independent task result."
                for e in chat.bus.get_queue(WEB.agent_id)._queue),
                "code notification bypasses frozen background turn", timeout=3)
            assert not chat.model.requests and not c.running
            assert len(c.waiters) == 1
            assert await pool.fetchval("SELECT count(*) FROM llm_records") == 0
            await chat.manager.stop()
            await c.stop()

    run_db(run, monkeypatch, timeout=40)


def test_claude_quota_diagnostic_does_not_consume_the_frozen_user_message(monkeypatch, tmp_path):
    from fake_claude import fake_claude

    _env(monkeypatch)
    counter = tmp_path / "claude-turns"
    body = "import json,sys\nfrom pathlib import Path\ncounter=Path(" + repr(str(counter)) + ")\n" + r"""
for line in sys.stdin:
    turn = int(counter.read_text()) + 1 if counter.exists() else 1
    counter.write_text(str(turn))
    print(json.dumps(dict(type="system", subtype="init", session_id="claude-quota-probe")), flush=True)
    if turn == 1:
        print(json.dumps(dict(type="assistant", error="rate_limit", message=dict(role="assistant",
            content=[dict(type="text", text="Synthetic quota diagnostic.")]))), flush=True)
        print(json.dumps(dict(type="result", subtype="error_during_execution", is_error=True,
            result="Synthetic quota diagnostic.", session_id="claude-quota-probe")), flush=True)
    else:
        print(json.dumps(dict(type="assistant", message=dict(role="assistant",
            content=[dict(type="text", text="Recovered CLI answer.")]))), flush=True)
        print(json.dumps(dict(type="result", subtype="success", is_error=False,
            result="Recovered CLI answer.", session_id="claude-quota-probe")), flush=True)
"""
    binary = fake_claude(tmp_path / "claude-stand-in", body)

    async def run(pool):
        c = _controller(monkeypatch)
        provider = await pool.fetchval("SELECT model.provider_id FROM agent_profiles profile "
            "JOIN llm_models model ON model.id=profile.model_id WHERE profile.id='agent-claude-code-opus'")
        await pool.execute("INSERT INTO system_settings (key,value) VALUES ('engine.failure_typing','record')")
        await c.transition(provider, admission.QuotaFailure(None, None))
        bus = MessageBus(runtime_root=tmp_path)
        manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path, claude_bin=binary), runtime_root=tmp_path)
        manager._bus = bus
        user = _message("Answer once the CLI quota recovers.")
        async with manager_endpoint(manager, bus, SimpleNamespace(has_planner=lambda _: False), tmp_path):
            await bus.start()
            await manager.start(bus)
            await bus.publish(user)
            try:
                await until(lambda: len(c.waiters) == 1, "CLI user waits frozen")
                token = manager._turns.find(user.message_id)
                c.now = lambda: datetime.now(timezone.utc) + timedelta(minutes=6)
                await c.wake()
                await until(lambda: counter.exists(), "quota probe invoked the real resident subprocess")
                await until(lambda: c.waiters or not token.live,
                            "quota probe retained or incorrectly consumed the message")
                assert len(c.waiters) == 1 and not c.running
                assert manager._turns.find(user.message_id).admission_waiting
                assert user.message_id in [e.message_id for e in bus.journal.undelivered()]
                queue = bus.get_queue(WEB.agent_id)
                assert not any("Synthetic quota diagnostic." in str(e.payload) for e in queue._queue)
                assert not any(e.message_type is MessageType.AGENT_REPLY for e in queue._queue)
                await c.transition(provider, admission.OperatorClear())
                await until(lambda: any(e.message_type is MessageType.AGENT_REPLY for e in queue._queue),
                            "retained CLI message answered after recovery")
                assert [e.payload["text"] for e in queue._queue
                        if e.message_type is MessageType.AGENT_REPLY] == ["Recovered CLI answer."]
                assert counter.read_text() == "2"
            finally:
                await manager.stop()
                await c.stop()
        await bus.shutdown()

    run_db(run, monkeypatch, timeout=45)


def test_quota_probe_exception_stays_stoppable_until_classified(monkeypatch, tmp_path):
    """A probe that raises after the quota diagnostic stays registered until the failure is classified."""
    from looper.agents.claude_cli import ResidentClaude
    from fake_claude import fake_claude

    _env(monkeypatch)
    counter = tmp_path / "claude-turns"
    body = "import json,sys,time\nfrom pathlib import Path\ncounter=Path(" + repr(str(counter)) + ")\n" + r"""
for line in sys.stdin:
    turn = int(counter.read_text()) + 1 if counter.exists() else 1
    counter.write_text(str(turn))
    print(json.dumps(dict(type="system", subtype="init", session_id="claude-quota-probe")), flush=True)
    print(json.dumps(dict(type="assistant", error="rate_limit", message=dict(role="assistant",
        content=[dict(type="text", text="Synthetic quota diagnostic.")]))), flush=True)
    time.sleep(30)
"""
    binary = fake_claude(tmp_path / "claude-stand-in", body)
    original = ResidentClaude.run_turn

    async def failing_run(self, text, on_event, **kwargs):
        async def failing(event):
            await on_event(event)
            if event.get("type") == "assistant" and event.get("error") == "rate_limit":
                raise RuntimeError("quota probe reader failed after the diagnostic")
        return await original(self, text, failing, **kwargs)

    monkeypatch.setattr(ResidentClaude, "run_turn", failing_run)

    async def run(pool):
        c = _controller(monkeypatch)
        provider = await pool.fetchval("SELECT model.provider_id FROM agent_profiles profile "
            "JOIN llm_models model ON model.id=profile.model_id WHERE profile.id='agent-claude-code-opus'")
        await pool.execute("INSERT INTO system_settings (key,value) VALUES ('engine.failure_typing','record')")
        await c.transition(provider, admission.QuotaFailure(None, None))
        bus = MessageBus(runtime_root=tmp_path)
        manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path, claude_bin=binary), runtime_root=tmp_path)
        manager._bus = bus
        user = _message("Stay stoppable if the probe reader fails.")
        async with manager_endpoint(manager, bus, SimpleNamespace(has_planner=lambda _: False), tmp_path):
            await bus.start()
            await manager.start(bus)
            await bus.publish(user)
            try:
                await until(lambda: len(c.waiters) == 1, "CLI user waits frozen")
                c.now = lambda: datetime.now(timezone.utc) + timedelta(minutes=6)
                await c.wake()
                await until(lambda: counter.exists(), "quota probe invoked the resident subprocess")
                await until(lambda: len(c.waiters) == 1 and not c.running, "exception probe requeued")
                found = manager._turns.find(user.message_id)
                assert found is not None and found.admission_waiting and found.live
                queue = bus.get_queue(WEB.agent_id)
                assert not any("Synthetic quota diagnostic." in str(e.payload) for e in queue._queue)
                assert not any(e.message_type is MessageType.AGENT_REPLY for e in queue._queue)
                assert user.message_id in [e.message_id for e in bus.journal.undelivered()]
                assert [tuple(row) for row in await pool.fetch("SELECT status,failure_class FROM llm_records")] == [
                    ("failed", "quota")]
                result = await manager.stop_turn(user.message_id, via="test")
                assert result.value == "stopping"
                await until(lambda: any("已停止" in str(e.payload.get("text"))
                            for e in queue._queue if e.message_type is MessageType.AGENT_REPLY),
                            "stopped probe replies")
            finally:
                await manager.stop()
                await c.stop()
        await bus.shutdown()

    run_db(run, monkeypatch, timeout=45)
