"""The task Manager's direct-session launch boundary: platform, no Looper hook, MCP launchers."""
from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import outbound_hook, task_conversations_settings, task_manager_safety
from looper.agents import task_manager_sandbox
from looper.agents.ops.service import Platform
from looper.agents.ops.task_manager import TaskManagerOps
from looper.agents.tiers import task_manager as tm
from looper.db import tasks
from tests.db_support import run_db
from tests.task_manager_support import NativeTaskManager, Sink


@pytest.fixture
def native(monkeypatch, tmp_path):
    return NativeTaskManager(monkeypatch, tmp_path)


def make_ops():
    return TaskManagerOps(Platform(bus=AsyncMock(), task_router=SimpleNamespace(has_planner=lambda _: False)),
                          url="http://127.0.0.1:18915")


def test_direct_mode_refuses_off_macos_and_starts_no_process(monkeypatch, tmp_path, native):
    monkeypatch.setattr(task_manager_sandbox, "SUPPORTED_PLATFORM", "darwin")
    monkeypatch.setattr(sys, "platform", "linux")
    task_manager_safety.refusal_counts.clear()

    async def check(db):
        task_id, _, _ = await native.create(db)
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 1)
        try:
            result = await pool.submit(task_id, "reply", message_id="m1", sink=Sink())
            assert result.state == "refused" and result.reason is tm.RefuseReason.SANDBOX_UNAVAILABLE
            assert native.records("launch") == []
            assert task_manager_safety.refusal_counts[("sandbox_unavailable", "launch", "unknown")] == 1
            # The read-only orchestrated session is not a sandboxed one and keeps working.
            await tasks.update_task_status(task_id, "pending", pool=db)
            assert (await pool.agent(task_id).prepare())[2].access.value != "full"
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch)


def test_direct_session_has_no_hook_while_orchestrated_still_needs_one(monkeypatch, tmp_path, native):
    monkeypatch.setattr(outbound_hook, "known_enabled", lambda: True)

    async def check(db):
        task_id, _, _ = await native.create(db)
        ops = make_ops()

        def broken(*args):
            raise RuntimeError("the hook factory is down")

        monkeypatch.setattr(ops, "tool_hook", broken)
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, ops=ops, max_resident=lambda: 1)
        try:
            agent = pool.agent(task_id)
            _, _, launch, _ = await agent.prepare()
            assert launch.tool_hook is None and "hooks" not in json.loads(
                open(agent.resident.command[agent.resident.command.index("--settings") + 1]).read())
            await tasks.update_task_status(task_id, "pending", pool=db)
            with pytest.raises(tm.Refused) as refused:
                await agent.prepare()
            assert refused.value.reason is tm.RefuseReason.OUTBOUND_REVIEW_UNAVAILABLE
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch)


def test_an_unsafe_launcher_refuses_before_launch(monkeypatch, tmp_path, native):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {
        "risky": {"type": "stdio", "command": "npx", "args": ["-y", "pkg"]}}}))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(task_conversations_settings, "_manager_mcp", ("risky",))
    task_manager_safety.refusal_counts.clear()

    async def check(db):
        task_id, _, _ = await native.create(db)
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, ops=make_ops(), max_resident=lambda: 1)
        try:
            sink = Sink()
            result = await pool.submit(task_id, "reply", message_id="m1", sink=sink)
            assert result.state == "refused" and result.reason is tm.RefuseReason.MCP_LAUNCHER_UNSAFE
            assert native.records("launch") == []
            assert task_manager_safety.refusal_counts[("mcp_launcher_unsafe", "launch", "unknown")] == 1
            with pytest.raises(tm.Refused, match="risky.*npx.*absolute path"):
                await pool.agent(task_id).prepare()
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch)
