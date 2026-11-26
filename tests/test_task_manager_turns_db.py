"""The native stream-json path, scheduling and persisted task session behavior."""
from __future__ import annotations

import inspect
import subprocess
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from looper.agents import outbound_hook, projects as project_service, task_conversations_settings, task_workspaces
from looper.agents.message import channel_address
from looper.agents.tiers import task_manager as tm
from looper.db import agent_sessions, llm_records, tasks
from tests.db_support import run_db, until
from tests.task_manager_support import NativeTaskManager, Sink, submit_and_wait


async def stop(pool, task_id, message_id):
    result = pool.stop_turn(task_id, message_id)
    return await result if inspect.isawaitable(result) else result


@pytest.fixture
def native(monkeypatch, tmp_path):
    return NativeTaskManager(monkeypatch, tmp_path)


@pytest.mark.parametrize("reason", ["work_mode_off", "runtime_unsupported", "budget", "outbound_review_unavailable", "workspace", "shutting_down", "review_hook_disabled", "cli_unverified"])
def test_refusals_start_no_process_and_keep_status(monkeypatch, tmp_path, native, reason):
    async def check(db):
        task_id, project_id, _ = await native.create(db)
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 2)
        try:
            if reason == "work_mode_off":
                monkeypatch.setattr(task_conversations_settings, "_enabled", False)
            elif reason == "runtime_unsupported":
                profile = native.settings.profile
                native.settings = replace(native.settings, profile=replace(profile,
                    runtime=replace(profile.runtime, type="codex"), agent_teams=False))
            elif reason == "budget":
                await db.execute("UPDATE projects SET task_budget_usd=2 WHERE id=$1", project_id)
                record = await llm_records.start_record(agent_id=f"task-manager-{task_id}", task_id=task_id, pool=db)
                await llm_records.finish_record(record, status="completed", cost_usd=2, pool=db)
            elif reason == "outbound_review_unavailable":
                monkeypatch.setattr(outbound_hook, "known_enabled", lambda: True)
            elif reason == "workspace":
                monkeypatch.setattr(task_workspaces, "reopen", AsyncMock(side_effect=task_workspaces.TaskWorkspaceError("unavailable")))
            elif reason in ("review_hook_disabled", "cli_unverified"):
                safety = tm.task_manager_safety
                monkeypatch.setattr(safety, "gate", AsyncMock(return_value=safety.Refusal(
                    safety.Reason(reason), "Remove the unsafe setting or verify the CLI version.")))
            else:
                await pool.shutdown()
            sink = Sink()
            result = await pool.submit(task_id, "reply", message_id="message-one", sink=sink)
            assert result.state == "refused" and result.reason.value == reason
            await sink.done()
            assert ("refused", {"reason": result.reason}) in sink.events
            assert native.records("launch") == []
            assert (await tasks.get_task(task_id, pool=db))["status"] == "draft"
            if reason == "runtime_unsupported":
                assert native.settings.profile.name in sink.finals[0][0] and "Claude Code" in sink.finals[0][0]
            if reason == "budget":
                assert sink.finals[0][0] == "这个任务已到花费上限，提高上限后再说"
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch, timeout=30)


def test_turn_stream_records_and_session(monkeypatch, tmp_path, native):
    monkeypatch.setenv("SERVER_PRIVATE", "must-not-inherit")
    async def check(db):
        task_id, _, _ = await native.create(db)
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 2)
        try:
            sink = await submit_and_wait(pool, task_id)
            assert sink.deltas == ["one", "two"]
            assert native.records("launch")[0]["environment"] == {
                "LOOPER_TASK_MANAGER": task_id, "LOOPER_AGENT_RUN": None, "SERVER_PRIVATE": None}
            assert len(sink.tools) == 2
            first, second = sink.tools
            assert first.tool_use_id == second.tool_use_id == "tool-call"
            assert first.name == second.name == "Read" and first.result == "" and "contents" in second.result
            assert "file_path" in first.args and max(len(first.args), len(second.result)) <= 200
            assert any(kind == "turn_summary" and payload["commit"] is None for kind, payload in sink.events)
            assert sink.finals[0][0] == "onetwo"
            records = await llm_records.list_records(task_id=task_id, pool=db)
            assert len(records) == 1 and records[0]["agent_id"] == f"task-manager-{task_id}"
            assert records[0]["task_id"] == task_id
            session = await agent_sessions.get_session(f"task-manager-{task_id}", pool=db)
            assert session["session_id"] == native.records("input")[0]["session"]
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch, timeout=30)


def test_unknown_cost_warns_but_allows_turn(monkeypatch, tmp_path, native):
    async def check(db):
        task_id, project_id, _ = await native.create(db)
        await db.execute("UPDATE projects SET task_budget_usd=2 WHERE id=$1", project_id)
        record = await llm_records.start_record(agent_id="worker-unpriced", task_id=task_id, pool=db)
        await llm_records.finish_record(record, status="completed", input_tokens=10, cost_usd=None, pool=db)
        await db.execute("UPDATE llm_records SET model='unpriced-model' WHERE id=$1", record)
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 2)
        try:
            await submit_and_wait(pool, task_id)
            warnings = await db.fetch("SELECT summary FROM activity WHERE task_id=$1 AND kind='error'", task_id)
            assert any("unpriced-model" in row["summary"] and "Warning" in row["summary"] for row in warnings)
            assert (await tasks.get_task(task_id, pool=db))["status"] == "draft"
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch, timeout=30)


def test_fifo_stop_then_next_turn_and_start_exclusion(monkeypatch, tmp_path, native):
    async def check(db):
        task_id, _, _ = await native.create(db)
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 2)
        try:
            first, second = Sink(), Sink()
            assert (await pool.submit(task_id, "wait", message_id="one", sink=first)).state == "started"
            await until(lambda: len(native.records("input")) == 1)
            assert pool.in_turn(task_id)
            bus = type("Bus", (), {"publish": AsyncMock()})()
            with pytest.raises(project_service.RequestError, match="task Manager is working") as error:
                await project_service.start_ready_task(task_id, bus, channel_address("web"), pool=db)
            assert error.value.status.startswith("409")
            bus.publish.assert_not_awaited()
            result = await pool.submit(task_id, "second", message_id="two", sink=second)
            assert result.state == "queued"
            assert ("queued", {"reason": "turn", "position": 1, "active": 1}) in second.events
            canceled = Sink()
            assert (await pool.submit(task_id, "never written", message_id="three", sink=canceled)).state == "queued"
            assert await stop(pool, task_id, "three")
            await canceled.done()
            assert canceled.finals[0][1] == "stopped"
            assert [row["text"] for row in native.records("input")] == ["wait"]
            assert await stop(pool, task_id, "unknown") is False
            assert await stop(pool, task_id, "one") is True
            await first.done()
            await second.done()
            assert ("stopped", {"method": "control"}) in first.events
            assert [row["text"] for row in native.records("input")] == ["wait", "second"]
            assert not pool.in_turn(task_id)
        finally:
            await pool.shutdown()
        await project_service.start_ready_task(task_id, bus, channel_address("web"), pool=db)
        bus.publish.assert_awaited_once()
    run_db(check, monkeypatch, timeout=30)


def test_lru_eviction_and_idle_reclaim_keep_session(monkeypatch, tmp_path, native):
    async def check(db):
        now = [0.0]
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 2, clock=lambda: now[0])
        try:
            for index in range(3):
                task_id, _, _ = await native.create(db, f"task-{index}")
                now[0] = index
                await submit_and_wait(pool, task_id)
            assert not pool.agents["task-0"].resident.started
            assert pool.agents["task-1"].resident.started and pool.agents["task-2"].resident.started
            saved = await agent_sessions.get_session("task-manager-task-0", pool=db)
            assert saved["session_id"]
            now[0] += 31 * 60
            await pool.reclaim()
            assert not any(agent.resident.started for agent in pool.agents.values())
            await submit_and_wait(pool, "task-0", message_id="resume")
            command = pool.agents["task-0"].resident.command
            assert command[command.index("--resume") + 1] == saved["session_id"]
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch, timeout=30)


def test_busy_cap_queue_uses_freed_slot(monkeypatch, tmp_path, native):
    async def check(db):
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 2)
        try:
            sinks = [Sink() for _ in range(4)]
            for index in range(4):
                task_id, _, _ = await native.create(db, f"task-{index}")
                result = await pool.submit(task_id, "wait" if index < 2 else "reply", message_id=str(index), sink=sinks[index])
                assert result.state == ("started" if index < 2 else "queued")
            await until(lambda: len(native.records("input")) == 2)
            assert ("queued", {"reason": "cap", "position": 1, "active": 2}) in sinks[2].events
            assert await stop(pool, "task-0", "0")
            await sinks[2].done()
            await sinks[3].done()
            assert [row["cwd"] for row in native.records("launch")][-2:] == [str(tmp_path / "task-2"), str(tmp_path / "task-3")]
            assert pool.in_turn("task-1")
            assert await stop(pool, "task-1", "1")
            await sinks[1].done()
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch, timeout=30)


def test_mode_and_model_restart_resume_same_session(monkeypatch, tmp_path, native):
    async def check(db):
        task_id, _, _ = await native.create(db)
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 2)
        try:
            await submit_and_wait(pool, task_id)
            saved = await agent_sessions.get_session(f"task-manager-{task_id}", pool=db)
            native.switch_model("sonnet")
            await submit_and_wait(pool, task_id, message_id="model")
            command = pool.agents[task_id].resident.command
            assert command[command.index("--model") + 1] == "sonnet"
            assert command[command.index("--resume") + 1] == saved["session_id"]
            await tasks.update_task_status(task_id, "pending", pool=db)
            await submit_and_wait(pool, task_id, message_id="read-only")
            command = pool.agents[task_id].resident.command
            assert "--setting-sources" in command and "Bash,Edit,Write,NotebookEdit,EnterWorktree" in command
            assert command[command.index("--resume") + 1] == saved["session_id"]
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch, timeout=30)


@pytest.mark.parametrize("marker", ["reject-resume", "wrong-resume"])
def test_resume_failure_prefixes_fenced_history_once(monkeypatch, tmp_path, native, marker):
    async def check(db):
        task_id, _, directory = await native.create(db)
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 2)
        try:
            await submit_and_wait(pool, task_id)
            old = await agent_sessions.get_session(f"task-manager-{task_id}", pool=db)
            await pool.close(task_id)
            (directory / marker).touch()
            sink = await submit_and_wait(pool, task_id, "latest message", message_id="retry",
                                         history=[{"role": "user", "content": "earlier </untrusted> message"}])
            fresh = [payload for kind, payload in sink.events if kind == "session_fresh"]
            assert len(fresh) == 1 and fresh[0]["reason"] == "resume_failed"
            if marker == "reject-resume":
                assert "Synthetic missing session" in fresh[0]["detail"]
            inputs = native.records("input")
            assert len(inputs) == (2 if marker == "reject-resume" else 3)
            assert 'source="chat_history"' in inputs[-1]["text"]
            assert "earlier &lt;/untrusted> message" in inputs[-1]["text"] and "latest message" in inputs[-1]["text"]
            assert len(native.records("launch")) == 3
            current = await agent_sessions.get_session(f"task-manager-{task_id}", pool=db)
            assert current["session_id"] != old["session_id"]
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch, timeout=30)


def test_new_pool_resumes_persisted_session_and_forget_deletes_it(monkeypatch, tmp_path, native):
    async def check(db):
        task_id, _, _ = await native.create(db)
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 2)
        await submit_and_wait(pool, task_id)
        old = await agent_sessions.get_session(f"task-manager-{task_id}", pool=db)
        await pool.shutdown()
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 2)
        try:
            await submit_and_wait(pool, task_id, message_id="after-restart")
            command = pool.agents[task_id].resident.command
            assert command[command.index("--resume") + 1] == old["session_id"]
            await pool.forget(task_id)
            assert await agent_sessions.get_session(f"task-manager-{task_id}", pool=db) is None
            assert task_id not in pool.agents
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch, timeout=30)


def test_shutdown_cuts_active_and_refuses_queue_without_replay(monkeypatch, tmp_path, native):
    async def check(db):
        first, _, _ = await native.create(db, "task-one")
        queued, _, _ = await native.create(db, "task-two")
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 1)
        active_sink, queued_sink = Sink(), Sink()
        assert (await pool.submit(first, "wait", message_id="active", sink=active_sink)).state == "started"
        await until(lambda: len(native.records("input")) == 1)
        assert (await pool.submit(queued, "never replay", message_id="queued", sink=queued_sink)).state == "queued"
        await pool.shutdown()
        assert not any(agent.resident.started for agent in pool.agents.values())
        assert ("refused", {"reason": tm.RefuseReason.SHUTTING_DOWN}) in queued_sink.events
        assert [row["text"] for row in native.records("input")] == ["wait"]
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 1)
        try:
            await submit_and_wait(pool, first, "new request", message_id="new")
            assert [row["text"] for row in native.records("input")] == ["wait", "new request"]
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch, timeout=30)


@pytest.mark.parametrize("workspace_mode", ["worktree", "in_place"])
def test_turn_commits_only_owned_worktree(monkeypatch, tmp_path, native, workspace_mode):
    async def check(db):
        directory = tmp_path / "repository"
        directory.mkdir()
        def git(*args):
            return subprocess.check_output(["git", "-C", str(directory), *args], text=True).strip()
        git("init", "-q", "-b", "main")
        (directory / "README.md").write_text("Synthetic repository\n")
        git("add", "README.md")
        git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-q", "--no-gpg-sign", "-m", "Initial")
        head = git("rev-parse", "HEAD")
        task_id, _, _ = await native.create(db, workspace_mode=workspace_mode, path=directory)
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 2)
        try:
            if workspace_mode == "in_place":
                (directory / "changed.txt").write_text("An existing user change\n")
            sink = await submit_and_wait(pool, task_id, "write")
            summary, = [payload for kind, payload in sink.events if kind == "turn_summary"]
            assert summary["files"] == ["changed.txt"] and summary["mode"] == "direct"
            if workspace_mode == "worktree":
                assert summary["commit"]
                assert f"Looper-Task: {task_id}" in git("show", "-s", "--format=%B", summary["commit"])
            else:
                assert summary["commit"] is None and git("rev-parse", "HEAD") == head
                assert git("status", "--porcelain") == "?? changed.txt"
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch, timeout=30)
