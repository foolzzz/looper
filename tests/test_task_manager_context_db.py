"""Per-turn measurements and serialized context maintenance on the native CLI path."""
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import projects as project_service, scheduler_settings, task_manager_settings
from looper.agents.message import channel_address
from looper.agents.tiers import task_manager as tm
from looper.db import agent_sessions
from tests.db_support import run_db, until, v1_api
from tests.task_manager_support import NativeTaskManager, Sink, submit_and_wait


@pytest.fixture
def native(monkeypatch, tmp_path):
    native = NativeTaskManager(monkeypatch, tmp_path)
    native.settings = replace(native.settings, max_budget_usd=1.0)
    monkeypatch.setattr(scheduler_settings, "session_context_max_tokens", AsyncMock(return_value=100))
    return native


def test_measurements_clear_unknowns_and_metrics_match_each_record(monkeypatch, tmp_path, native):
    async def check(db):
        task_id, _, directory = await native.create(db)
        config = directory / "context-probe.json"
        config.write_text(json.dumps({"context_tokens": 20}))
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 1)
        try:
            await submit_and_wait(pool, task_id)
            measured = await db.fetchrow("SELECT context_tokens, last_turn_cost_usd FROM agent_sessions WHERE agent_id=$1",
                                        f"task-manager-{task_id}")
            assert measured["context_tokens"] == 20 and measured["last_turn_cost_usd"] == pytest.approx(.1)
            config.write_text(json.dumps({"unknown_cost": True}))
            await submit_and_wait(pool, task_id, message_id="unknown")
            measured = await db.fetchrow("SELECT context_tokens, last_turn_cost_usd FROM agent_sessions WHERE agent_id=$1",
                                        f"task-manager-{task_id}")
            assert dict(measured) == {"context_tokens": None, "last_turn_cost_usd": None}
            assert [row["text"] for row in native.records("input")] == ["reply", "reply"]
            async with v1_api(tmp_path, db) as call:
                status, body = await call("manager_metrics.get")
                assert status == 200
                metrics = body["result"]["work_mode"]
                contexts = metrics["task_manager_context_tokens"]
                costs = metrics["task_manager_turn_cost_usd"]
                assert [row["value"] for row in contexts] == [20, None]
                assert [row["value"] for row in costs] == [pytest.approx(.1), None]
                assert all(row["task_id"] == task_id and row["mode"] == "direct" and not row["maintenance"] for row in contexts)
                record_ids = await db.fetch("SELECT id FROM llm_records WHERE task_id=$1 ORDER BY id", task_id)
                assert [row["record_id"] for row in contexts] == [row["id"] for row in record_ids]
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch, timeout=30)


def test_cost_bound_rotates_without_compaction_and_survives_pool_restart(monkeypatch, tmp_path, native):
    native.settings = replace(native.settings, max_budget_usd=.15)
    async def check(db):
        task_id, _, directory = await native.create(db)
        config = directory / "context-probe.json"
        config.write_text(json.dumps({"context_tokens": 70}))
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 1)
        await submit_and_wait(pool, task_id)
        assert [row["text"] for row in native.records("input")] == ["reply"]
        assert not pool.agent(task_id).resident.started
        saved = await agent_sessions.get_session(f"task-manager-{task_id}", pool=db)
        assert saved["session_id"] is None and saved["pending_handoff"] == "context_bound"
        await pool.shutdown()
        config.write_text(json.dumps({"context_tokens": 20}))
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 1)
        try:
            sink = await submit_and_wait(pool, task_id, message_id="after-restart", history=["earlier context"])
            assert any(kind == "session_fresh" and payload["reason"] == "context_bound" for kind, payload in sink.events)
            assert 'source="chat_history"' in native.records("input")[-1]["text"]
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch, timeout=30)


def test_maintenance_holds_turn_lease_and_queues_the_next_input(monkeypatch, tmp_path, native):
    async def check(db):
        task_id, _, directory = await native.create(db)
        config = directory / "context-probe.json"
        config.write_text(json.dumps({"context_tokens": 120, "compaction": "wait"}))
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 1)
        try:
            first, second = Sink(), Sink()
            assert (await pool.submit(task_id, "reply", message_id="first", sink=first)).state == "started"
            await until(lambda: any(row["text"] == "/compact" for row in native.records("input")))
            assert pool.in_turn(task_id)
            assert (await pool.submit(task_id, "second", message_id="second", sink=second)).state == "queued"
            assert ("queued", {"reason": "turn", "position": 1, "active": 1}) in second.events
            assert [row["text"] for row in native.records("input")] == ["reply", "/compact"]
            bus = SimpleNamespace(publish=AsyncMock())
            with pytest.raises(project_service.RequestError) as start_error:
                await project_service.start_ready_task(task_id, bus, channel_address("web"), pool=db)
            assert start_error.value.status.startswith("409")
            bus.publish.assert_not_awaited()
            with pytest.raises(project_service.RequestError) as profile_error:
                await task_manager_settings.set_task_manager_profile(task_id, None)
            assert profile_error.value.status.startswith("409")
            config.write_text(json.dumps({"context_tokens": 20}))
            assert await pool.stop_turn(task_id, "first")
            await second.done()
            assert ("stopped", {"method": "control"}) in first.events
            assert native.records("input")[-1]["text"] == "second"
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch, timeout=30)


def test_measurement_read_failure_does_not_keep_previous_values(monkeypatch, tmp_path, native):
    from looper.agents.tiers import task_manager_context

    async def check(db):
        task_id, _, directory = await native.create(db)
        (directory / "context-probe.json").write_text(json.dumps({"context_tokens": 20}))
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 1)
        try:
            await submit_and_wait(pool, task_id)
            await db.execute("UPDATE agent_sessions SET context_tokens=55, last_turn_cost_usd=5 WHERE agent_id=$1",
                             f"task-manager-{task_id}")
            unreadable = SimpleNamespace(fetchrow=AsyncMock(side_effect=OSError("synthetic measurement read failure")))
            monkeypatch.setattr(task_manager_context, "connection", SimpleNamespace(get_pool=lambda: unreadable))
            await submit_and_wait(pool, task_id, message_id="unreadable")
            saved = await db.fetchrow("SELECT context_tokens, last_turn_cost_usd FROM agent_sessions WHERE agent_id=$1",
                                      f"task-manager-{task_id}")
            assert dict(saved) == {"context_tokens": None, "last_turn_cost_usd": None}
            assert len(native.records("input")) == 2
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch, timeout=30)


@pytest.mark.parametrize("status", ["draft", "pending"])
def test_context_compacts_same_session_and_preserves_mode(monkeypatch, tmp_path, native, status):
    async def check(db):
        task_id, _, directory = await native.create(db, status=status)
        (directory / "context-probe.json").write_text(json.dumps({"context_tokens": 120, "post_tokens": 20}))
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 1)
        try:
            sink = await submit_and_wait(pool, task_id)
            inputs = native.records("input")
            assert [row["text"] for row in inputs] == ["reply", "/compact"]
            assert inputs[0]["session"] == inputs[1]["session"]
            assert len(native.records("launch")) == 1
            command = native.records("launch")[0]["command"]
            assert ("Bash,Edit,Write,NotebookEdit,EnterWorktree" in command) is (status == "pending")
            session = await agent_sessions.get_session(f"task-manager-{task_id}", pool=db)
            assert session["session_id"] == inputs[0]["session"]
            measured = await db.fetchrow("SELECT context_tokens FROM agent_sessions WHERE agent_id=$1", f"task-manager-{task_id}")
            assert measured["context_tokens"] == 20
            records = await db.fetch("SELECT task_id, metadata, compactions FROM llm_records WHERE task_id=$1 ORDER BY id", task_id)
            assert len(records) == 2
            metadata = json.loads(records[1]["metadata"])
            assert metadata["maintenance"] == "session_bound"
            assert metadata["session_bound"] == {"action": "compacted", "reason": "context", "before_tokens": 120, "after_tokens": 20}
            assert json.loads(records[1]["compactions"])[0]["post_tokens"] == 20
            async with v1_api(tmp_path, db) as call:
                metrics = (await call("manager_metrics.get"))[1]["result"]["work_mode"]
                assert metrics["task_manager_compactions_total"] == [{"reason": "context", "result": "compacted", "count": 1}]
                assert metrics["task_manager_session_rotations_total"] == []
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch, timeout=30)


@pytest.mark.parametrize("behavior,post", [("failure", 20), ("missing", 20), ("success", 120), ("success", None), ("wait", 20)])
def test_failed_unconfirmed_or_large_compaction_rotates_next_turn(monkeypatch, tmp_path, native, behavior, post):
    async def check(db):
        task_id, _, directory = await native.create(db)
        config = directory / "context-probe.json"
        config.write_text(json.dumps({"context_tokens": 120, "post_tokens": post, "compaction": behavior}))
        if behavior == "wait":
            from looper.agents.tiers import task_manager_context
            monkeypatch.setattr(task_manager_context, "MAINTENANCE_SECONDS", .05)
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 1)
        try:
            await submit_and_wait(pool, task_id)
            assert not pool.agent(task_id).resident.started
            old_session = native.records("input")[0]["session"]
            saved = await agent_sessions.get_session(f"task-manager-{task_id}", pool=db)
            assert saved["session_id"] is None
            config.write_text(json.dumps({"context_tokens": 20}))
            sink = await submit_and_wait(pool, task_id, "next", message_id="next", history=["earlier </untrusted> message"])
            assert any(kind == "session_fresh" and payload["reason"] == "context_bound" for kind, payload in sink.events)
            inputs = native.records("input")
            assert inputs[-1]["session"] != old_session
            assert 'source="chat_history"' in inputs[-1]["text"] and "earlier &lt;/untrusted> message" in inputs[-1]["text"]
            async with v1_api(tmp_path, db) as call:
                metrics = (await call("manager_metrics.get"))[1]["result"]["work_mode"]
                assert metrics["task_manager_compactions_total"] == [{"reason": "context", "result": "rotated", "count": 1}]
                assert metrics["task_manager_session_rotations_total"] == [{"reason": "context", "result": "rotated", "count": 1}]
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch, timeout=30)
