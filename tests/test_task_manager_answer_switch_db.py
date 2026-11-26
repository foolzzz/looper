"""``tasks.conversations.manager_answers``: while it is off, no entry point starts a task Manager.

Every path that can start a process, keep a resident session or write ``agent_sessions`` goes through the
pool's turn gate (``submit``, the driver and ``prepare``) or through ``operator_chat.send``'s target check.
Each test leaves work mode on, so only the task Manager's own switch is under test.
"""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from looper.agents import projects as project_service, task_conversations_settings as settings
from looper.agents.message import channel_address
from looper.agents.ops import OpError
from looper.agents.tiers import task_manager as tm
from looper.db import chat, task_chat, tasks
from tests.db_support import run_db, until
from tests.task_manager_support import NativeTaskManager, Sink


def launches(native):
    return native.records("launch") if native.transcript.exists() else []


async def nothing_started(db, native, pool):
    """No process was spawned, no model input was sent, no session or run row exists, no agent is resident."""
    assert launches(native) == []
    assert not native.transcript.exists() or native.records("input") == []
    assert not any(agent.resident.started or agent.busy for agent in pool.agents.values())
    assert await db.fetchval("SELECT count(*) FROM agent_sessions WHERE agent_id LIKE 'task-manager-%'") == 0
    assert await db.fetchval("SELECT count(*) FROM llm_records WHERE agent_id LIKE 'task-manager-%'") == 0


def test_default_is_off_and_only_the_string_true_turns_it_on(monkeypatch):
    async def check(db):
        monkeypatch.setattr(settings, "_manager_answers", True)
        await settings.load()
        assert settings.manager_answers() is False
        for stored in ("True", "1", "yes", "", "enabled"):
            await db.execute("INSERT INTO system_settings(key, value) VALUES ($1, $2) "
                             "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                             settings.MANAGER_ANSWERS_KEY, stored)
            await settings.load()
            assert settings.manager_answers() is False, stored
        await settings.update(manager_answers=True)
        await settings.load()
        assert settings.manager_answers() is True and settings.view()["manager_answers"] is True
        await settings.update(manager_answers=None)
        assert settings.manager_answers() is False
        assert await db.fetchval("SELECT value FROM system_settings WHERE key = $1",
                                 settings.MANAGER_ANSWERS_KEY) is None
    run_db(check, monkeypatch)


def test_prepare_refuses_and_starts_nothing(monkeypatch, tmp_path):
    native = NativeTaskManager(monkeypatch, tmp_path)

    async def check(db):
        task_id, _, _ = await native.create(db)
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 2)
        try:
            settings.set_manager_answers(False)
            with pytest.raises(tm.Refused) as refused:
                await pool.agent(task_id).prepare()
            assert refused.value.reason is tm.RefuseReason.MANAGER_ANSWERS_OFF
            await nothing_started(db, native, pool)
            settings.set_manager_answers(True)
            await pool.agent(task_id).prepare()  # negative control: the same call launches once it is on
            await until(lambda: len(launches(native)) == 1, "the launch after the switch is on")
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch, timeout=30)


def test_a_queued_turn_is_refused_when_the_switch_goes_off_before_it_runs(monkeypatch, tmp_path):
    native = NativeTaskManager(monkeypatch, tmp_path)

    async def check(db):
        task_id, _, _ = await native.create(db)
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 1)
        try:
            first, second = Sink(), Sink()
            assert (await pool.submit(task_id, "wait", message_id="first", sink=first)).state == "started"
            await until(lambda: len(native.records("input")) == 1)
            assert (await pool.submit(task_id, "second", message_id="second", sink=second)).state == "queued"
            settings.set_manager_answers(False)
            assert await pool.stop_turn(task_id, "first")
            await second.done()
            assert ("refused", {"reason": tm.RefuseReason.MANAGER_ANSWERS_OFF}) in second.events
            assert [row["text"] for row in native.records("input")] == ["wait"]
            assert len(launches(native)) == 1
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch, timeout=30)


def test_entries_that_are_not_a_message_never_start_a_task_manager(monkeypatch, tmp_path):
    """Task start, delete, profile change, the status read and a restart's left-over user row."""
    native = NativeTaskManager(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "_manager_answers", False)

    async def check(db):
        task_id, _, _ = await native.create(db)
        await db.execute("UPDATE tasks SET requirement = 'A synthetic requirement' WHERE id = $1", task_id)
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 2)
        try:
            # The last row of the thread is the user's, as after a restart in the middle of a turn.
            thread = chat.task_thread(task_id)
            await chat.claim_thread(thread, "manager")
            await chat.save_message(session_id=thread, message_id="m1", role="user", content="hello")
            assert await task_chat.turn_state(task_id) == "interrupted"
            bus = type("Bus", (), {"publish": AsyncMock()})()
            await project_service.start_ready_task(task_id, bus, channel_address("web"), pool=db)
            bus.publish.assert_awaited_once()
            from looper.agents import task_manager_settings
            await task_manager_settings.set_task_manager_profile(task_id, None)
            await pool.forget(task_id)
            await nothing_started(db, native, pool)
            assert await tasks.get_task(task_id, pool=db) is not None
        finally:
            await pool.shutdown()
        assert tm.current_pool() is None
    run_db(check, monkeypatch, timeout=30)


def test_tool_tokens_stop_working_when_the_switch_goes_off(monkeypatch):
    from looper.agents.ops.task_manager import TaskManagerOps

    monkeypatch.setattr(settings, "_enabled", True)
    monkeypatch.setattr(settings, "_manager_answers", True)
    ops = TaskManagerOps(None, url="http://127.0.0.1:1")
    token = ops.issue("task-one")
    assert ops.authorized(f"Bearer {token}")
    settings.set_manager_answers(False)
    assert not ops.authorized(f"Bearer {token}")
    with pytest.raises(OpError):
        ops.issue("task-one")
    settings.set_manager_answers(True)
    assert ops.authorized(f"Bearer {token}")  # negative control: the grant itself was never revoked
