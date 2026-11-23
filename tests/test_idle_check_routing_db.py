"""Queued idle checks are voided before the main user's next turn."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from looper.agents import idle_checks, lanes, lanes_settings, session_routing
from looper.agents.message import MANAGER_ADDRESS, MessageEnvelope, MessageType, channel_address
from looper.agents.session_routing import SessionKey, SessionKind
from looper.agents.tiers.manager import ManagerAgent, ManagerConfig
from looper.db import idle_checks as store
from tests.db_support import DSN, run_db, until

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def _user():
    return MessageEnvelope.create(source=channel_address("web"), target=MANAGER_ADDRESS,
        message_type=MessageType.USER_MESSAGE, payload={"text": "hello", "channel_context": {"session_id": "idle-chat"}})


async def _row(pool):
    return await store.insert_running(pool, manager_id="manager", local_date=datetime.now(timezone.utc).date(),
        items=[{"section": "logins", "key": "login:synthetic", "since": None, "title": "Synthetic login", "facts": {}}],
        channel_id="channel-lark", chat="ou_idle_operator")


@pytest.mark.parametrize("main_arrival", [False, True])
def test_user_voids_all_queued_checks_only_when_entering_main(monkeypatch, tmp_path, main_arrival):
    monkeypatch.setattr(lanes_settings, "_enabled", True)
    monkeypatch.setattr(session_routing, "_routing_enabled", False)
    async def check(pool):
        agent = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
        agent._running = True
        main = agent._sessions["main"]
        group = agent._router.session(SessionKey(SessionKind.GROUP, "channel-lark", "oc_idle_group"))
        settled, handled = [], []
        agent._bus = SimpleNamespace(settle_handled=lambda e: settled.append(e.message_id))
        entered, release = asyncio.Event(), asyncio.Event()
        async def handle(envelope):
            handled.append(envelope)
            if envelope.message_type is MessageType.USER_MESSAGE:
                entered.set()
                await release.wait()
        agent._handle = handle
        first = _user()
        agent._router.dispatch(main, first)
        await entered.wait()
        ids = [await _row(pool), await _row(pool)]
        wakes = [idle_checks.due_envelope(i) for i in ids]
        for wake in wakes:
            agent._router.dispatch(main, wake)
        try:
            await lanes.route_arrival(agent, main if main_arrival else group, _user())
            rows = await pool.fetch("SELECT state, reason, finished_at FROM idle_checks ORDER BY id")
            assert [row["state"] for row in rows] == ["preempted" if main_arrival else "running"] * 2
            assert all(row["reason"] is None for row in rows)
            assert all((row["finished_at"] is not None) is main_arrival for row in rows)
            assert settled == ([wake.message_id for wake in wakes] if main_arrival else [])
            assert agent._router.queued_count(main, lanes.Lane.BACKGROUND) == (0 if main_arrival else 2)
            release.set()
            await until(lambda: first.message_id in settled)
            if not main_arrival:
                await until(lambda: len(settled) == 3)
            assert all(wake not in handled for wake in wakes) is main_arrival
        finally:
            release.set()
            await agent._router.stop()
    run_db(check, monkeypatch)


def test_failed_void_write_keeps_wake_unacknowledged_and_dispatches_user(monkeypatch, tmp_path, caplog):
    from tests.api_manager_support import USAGE, _manager, _message
    from tests.fake_model_api import ResponseScript, text_events

    monkeypatch.setattr(lanes_settings, "_enabled", True)
    monkeypatch.setattr(session_routing, "_routing_enabled", False)
    async def check(pool):
        gate = asyncio.Event()
        first = ResponseScript(events=text_events("First answer", usage=USAGE), gate=gate)
        second = ResponseScript(events=text_events("Next answer", usage=USAGE))
        async with _manager(pool, tmp_path, monkeypatch, [first, second]) as chat:
            settled = []
            original = chat.bus.settle_handled
            def acknowledge(envelope):
                settled.append(envelope.message_id)
                original(envelope)
            monkeypatch.setattr(chat.bus, "settle_handled", acknowledge)
            await chat.bus.start()
            await chat.manager.start(chat.bus)
            await chat.bus.publish(_message("First question"))
            await asyncio.wait_for(first.gate_reached.wait(), 15)
            ids = [await _row(pool), await _row(pool)]
            wakes = [idle_checks.due_envelope(i) for i in ids]
            main = chat.manager._sessions["main"]
            for wake in wakes:
                await chat.bus.publish(wake)
            await until(lambda: chat.manager._router.queued_count(main, lanes.Lane.BACKGROUND) == 2)
            settle = store.settle
            async def failed_settle(check_id, *args, **kwargs):
                if check_id == ids[0]:
                    raise RuntimeError("synthetic write failure")
                return await settle(check_id, *args, **kwargs)
            monkeypatch.setattr(store, "settle", failed_settle)
            user = _message("Next question")
            try:
                await chat.bus.publish(user)
                await until(lambda: chat.manager._router.queued_count(main, lanes.Lane.BACKGROUND) == 0)
                gate.set()
                await until(lambda: user.message_id in settled, "user processed after idle settlement failed")
                assert chat.manager.is_healthy()
                assert len(chat.model.requests) == 2
                assert "Next question" in str(chat.model.requests[1].body)
                assert (await store.get(ids[0]))["state"] == "running"
                assert wakes[0].message_id not in settled
                assert (await store.get(ids[1]))["state"] == "preempted"
                assert wakes[1].message_id in settled
                assert any(record.levelname == "WARNING" and "synthetic write failure" in record.getMessage()
                           for record in caplog.records)
            finally:
                gate.set()
    run_db(check, monkeypatch, timeout=60)


def test_group_arrivals_do_not_reset_main_and_busy_group_does_not_block_its_check(monkeypatch, tmp_path):
    from looper.agents import proactive_delivery as delivery
    from looper.agents.operator_identity import DmTarget
    from tests.api_manager_support import USAGE, _manager
    from tests.fake_model_api import ResponseScript, text_events

    monkeypatch.setattr(lanes_settings, "_enabled", True)
    monkeypatch.setattr(session_routing, "_routing_enabled", True)
    monkeypatch.setattr(delivery, "load_policy", lambda: delivery.DeliveryPolicy(timezone=timezone.utc,
                                                                               quiet_hours=None))
    async def target():
        return DmTarget("channel-lark", "ou_idle_operator", "synthetic direct chat")
    monkeypatch.setattr(idle_checks, "operator_dm_target", target)

    async def check(pool):
        for key in ("manager.idle_checks.enabled", "manager.lanes.enabled", "delivery.enabled", "attention.enabled"):
            await pool.execute("INSERT INTO system_settings (key, value) VALUES ($1, 'true')", key)
        await pool.execute("INSERT INTO projects (id, name) VALUES ('idle-project', 'Synthetic project')")
        await pool.execute("INSERT INTO schedules (id, project_id, name, cron, timezone, requirement, "
                           "consecutive_failures) VALUES ('idle-schedule', 'idle-project', 'Synthetic schedule', "
                           "'0 9 * * *', 'UTC', 'Check it', 2)")
        scripts = [ResponseScript(events=text_events(reply, usage=USAGE)) for reply in ("hello", "NO_REPLY")]
        async with _manager(pool, tmp_path, monkeypatch, scripts) as chat:
            clock = [100.0]
            monkeypatch.setattr(chat.manager, "_clock", lambda: clock[0])
            handled, entered, release = [], asyncio.Event(), asyncio.Event()
            original = chat.manager._handle
            async def handle(envelope):
                if envelope.source.agent_id != "channel-lark":
                    return await original(envelope)
                session = chat.manager._session_now()
                async with session.turn_lock:
                    if envelope.payload["text"] == "hold":
                        entered.set()
                        await release.wait()
                token = chat.manager._turns.find(envelope.message_id)
                if token is not None:
                    token.settle()
                handled.append(envelope.message_id)
            monkeypatch.setattr(chat.manager, "_handle", handle)
            await chat.bus.start()
            await chat.manager.start(chat.bus)
            main = chat.manager._sessions["main"]
            await chat.bus.publish(_user())
            await until(lambda: main.turn_ended_at is not None, "main's first user turn ended")
            baseline = main.turn_ended_at
            def group(text):
                return MessageEnvelope.create(source=channel_address("lark"), target=MANAGER_ADDRESS,
                    message_type=MessageType.USER_MESSAGE,
                    payload={"text": text, "channel_context": {"chat_id": "oc_idle_group", "chat_type": "group",
                                                             "sender_id": "ou_idle_sender"}})
            try:
                for index in range(1, 7):
                    clock[0] = baseline + index * 300
                    envelope = group(str(index))
                    await chat.bus.publish(envelope)
                    await until(lambda: envelope.message_id in handled)
                    assert main.turn_ended_at == baseline
                    assert main.last_activity == clock[0]  # proves why last_activity cannot be the idle clock
                held = group("hold")
                await chat.bus.publish(held)
                await asyncio.wait_for(entered.wait(), 5)
                assert chat.manager.busy and chat.manager.idle_for() == 1800
                check_id = await idle_checks.IdleCheckService(chat.bus, idle_for=chat.manager.idle_for,
                                                               manager_running=lambda: True).run_once()
                assert check_id is not None
                async def finished():
                    return (await store.get(check_id))["state"] == "silent"
                async with asyncio.timeout(15):
                    while not await finished():
                        await asyncio.sleep(0.01)
                assert len(chat.model.requests) == 2
                assert "[IDLE CHECK]" in str(chat.model.requests[1].body)
                assert not release.is_set()  # main finished while the other session was still busy
            finally:
                release.set()
    run_db(check, monkeypatch, timeout=60)
