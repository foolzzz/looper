"""Worker explorer tools are opt-in and follow the parent run's lifetime."""

from __future__ import annotations

import ast
import asyncio
import json
import subprocess
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from looper.agents.bus import MessageBus
from looper.agents.message import MessageEnvelope, MessageType, planner_address, worker_address
from looper.agents.runtimes import ADAPTERS
from looper.agents.runtimes.claude_code import ClaudeCodeRuntime
from looper.agents.tiers import worker
from tests.run_handle_support import ScriptedRun
from tests.runtime_model_helpers import builtin_launch


BASELINE_SHA = "0a8b16d112752ff09e5701cabb91767c022d19c1"
# main before #1566 merged; update only with an intentional off-path change, and say why in the PR.


def _item(root, *, review=False):
    return MessageEnvelope.create(
        source=planner_address("task-subagents"), target=worker_address("worker-subagents"),
        message_type=MessageType.VERIFY_REQUEST if review else MessageType.WORK_ITEM,
        payload={"task_id": "step-subagents", "parent_task_id": "task-subagents", "step_key": "inspect",
                 "instruction": "Inspect source", "context": {"work_dir": str(root)}},
    )


def _configured_pool(root, monkeypatch, project):
    monkeypatch.setattr(worker, "load_agent_settings", AsyncMock(return_value=builtin_launch().settings))
    monkeypatch.setattr(worker, "_load_settings", AsyncMock(return_value=worker.WorkerSettings(0, 0, "WORKER")))
    monkeypatch.setattr(worker, "load_task_limit", AsyncMock(return_value=1))
    monkeypatch.setattr(worker.WorkerPool, "_task_project", AsyncMock(return_value=project))
    pool = worker.WorkerPool(config=worker.WorkerConfig(), runtime_root=root / "runtime")
    room = SimpleNamespace(presence_changed=AsyncMock(), first_turn=AsyncMock(return_value="room intro"),
                           delivery=AsyncMock(return_value=None), confirm=AsyncMock())

    async def same_library(library, *args, **kwargs):
        return library

    pool.room_ops = SimpleNamespace(
        room=room, issue=Mock(return_value="run.synthetic"),
        library_with_server=AsyncMock(side_effect=same_library), bind_subagents=Mock(),
        end_subagents=AsyncMock(), revoke_run=Mock(), export_run=Mock(return_value=None),
    )
    return pool


@pytest.mark.parametrize(("switch", "review", "enabled"), [
    (None, False, False), ("off", False, False), ("on", False, True), ("on", True, False),
])
def test_worker_tools_and_instructions_require_the_project_switch(tmp_path, monkeypatch, agent_db,
                                                                 switch, review, enabled):
    from looper.agents.ops.subagents import WORKER_INSTRUCTIONS

    pool = _configured_pool(tmp_path, monkeypatch,
                            {"id": "project-subagents", "agent_room": "off", "subagents": switch})

    async def scenario():
        ctx = await pool._prepare(_item(tmp_path / "work", review=review), is_verify=review)
        assert isinstance(ctx, worker.RunContext)
        assert (WORKER_INSTRUCTIONS in ctx.system_prompt.text) is enabled
        assert pool.room_ops.issue.call_count == int(enabled)
        if enabled:
            assert pool.room_ops.issue.call_args.kwargs["subagents"] is True
            assert pool.room_ops.issue.call_args.kwargs["room"] is False
            assert pool.room_ops.library_with_server.await_count == 1
        else:
            pool.room_ops.library_with_server.assert_not_awaited()
        pool.room_ops.room.first_turn.assert_not_awaited()

    asyncio.run(scenario())


def test_worker_off_launch_matches_main_bytes(tmp_path, monkeypatch, agent_db):
    """Compare opt-out preparation with pinned main using the same dependencies."""
    source = subprocess.run(
        ["git", "show", f"{BASELINE_SHA}:looper/agents/tiers/worker.py"], check=True, capture_output=True, text=True,
    ).stdout
    definition = next(node for node in ast.parse(source).body
                      if isinstance(node, ast.ClassDef) and node.name == "WorkerPool")
    method = next(node for node in definition.body
                  if isinstance(node, ast.AsyncFunctionDef) and node.name == "_prepare")
    namespace = dict(vars(worker))
    exec(compile(ast.Module(body=[method], type_ignores=[]), "main-worker-prepare", "exec"), namespace)
    project = {"id": "project-subagents", "agent_room": "off", "subagents": "off"}
    pool = _configured_pool(tmp_path, monkeypatch, project)
    # The extracted main method must use the same test settings as the current one.
    namespace.update({key: getattr(worker, key) for key in ("load_agent_settings", "_load_settings")})
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)

    async def scenario():
        item = _item(tmp_path / "work")
        current = await pool._prepare(item, started_at=started)
        baseline = await namespace["_prepare"](pool, item, started_at=started)
        assert isinstance(current, worker.RunContext) and isinstance(baseline, worker.RunContext)
        assert current.system_prompt.text.encode() == baseline.system_prompt.text.encode()
        assert current.prompt.encode() == baseline.prompt.encode()
        assert current.command == baseline.command
        assert current.launch.record() == baseline.launch.record()
        assert current.seat == baseline.seat is None
        pool.room_ops.issue.assert_not_called()

    asyncio.run(scenario())


@pytest.mark.parametrize("ending", ["completed", "failed", "killed"])
def test_worker_end_cascades_after_binding_the_started_parent(tmp_path, monkeypatch, agent_db, ending):
    events = [{"type": "system", "subtype": "init", "session_id": "synthetic"},
              {"type": "result", "subtype": "success", "is_error": False,
               "result": json.dumps({"status": "completed", "summary": "done", "changed_files": {}})}]
    run = ScriptedRun(events, hold=ending == "killed",
                      start_error=RuntimeError("synthetic start failure") if ending == "failed" else None)

    class Runtime(ClaudeCodeRuntime):
        hosts_runs = True

        def hosted_run(self, *args, **kwargs):
            return run

    monkeypatch.setitem(ADAPTERS, "claude-code", Runtime())
    pool = _configured_pool(tmp_path, monkeypatch,
                            {"id": "project-subagents", "agent_room": "off", "subagents": "on"})
    bound = []

    def bind(run_id, recorder, launch):
        assert recorder.record_id == 1
        assert run.launch_calls == 0
        bound.append(run_id)

    pool.room_ops.bind_subagents.side_effect = bind

    async def scenario():
        bus = MessageBus(runtime_root=tmp_path / "runtime")
        await pool.start(bus)
        try:
            await pool._dispatch(_item(tmp_path / "work"))
            task = pool._tasks["worker-subagents"]
            if ending == "killed":
                await run.started.wait()
                await pool.kill("worker-subagents")
            else:
                await task
            assert bound == ["worker-subagents"]
            pool.room_ops.end_subagents.assert_awaited_once_with("worker-subagents")
            pool.room_ops.revoke_run.assert_called_once_with("worker-subagents")
        finally:
            await pool.stop()
            await bus.shutdown()

    asyncio.run(asyncio.wait_for(scenario(), 5))
