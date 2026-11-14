"""DSH catalog validation must leave unrelated event-loop work responsive."""

import asyncio
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents.claude_cli import ClaudeRunRecorder
from looper.agents.runtimes import dsh
from looper.agents.tiers import manager as manager_module, planner, worker
from tests.test_agent_planner_worker import _step_envelope, _worker_row
from tests.test_dsh_runtime import _installed_dsh_skills, _run_launch
from tests.test_manager_new_session import _web
from tests.test_manager_stop import _manager, _replies, _Session


@pytest.mark.parametrize("entry", ["manager", "worker", "planner", "recorder", "new-session", "profile"])
def test_slow_dsh_catalog_validation_keeps_loop_ticking(agent_db, tmp_path, monkeypatch, entry):
    library = _installed_dsh_skills(tmp_path, monkeypatch)
    launch = _run_launch(tmp_path, role="manager" if entry in {"manager", "new-session", "profile"} else "worker")
    launch = replace(launch, library=replace(library, mcp_config=launch.library.mcp_config))
    calls = []

    def slow_node(*args, **kwargs):
        assert args[0][0] == "node"
        calls.append(args)
        time.sleep(0.5)
        raise OSError("unavailable skill provider")

    monkeypatch.setattr(dsh.subprocess, "run", slow_node)
    monkeypatch.setattr(dsh, "_bootstrap_files", lambda *args, **kwargs: {"looper-bootstrap.mjs": "// bootstrap"})
    monkeypatch.setattr(dsh, "mcp_config", lambda *args, **kwargs: None)
    monkeypatch.setattr(manager_module, "load_installed", AsyncMock(return_value=launch.library))
    monkeypatch.setattr(worker, "load_installed", AsyncMock(return_value=launch.library))
    monkeypatch.setattr(worker, "load_agent_settings", AsyncMock(return_value=launch.settings))
    _worker_row(monkeypatch, {"system_prompt": "Inspect the project", "timeout_seconds": 5})

    async def exercise():
        if entry == "manager":
            session = _Session(hold=False)
            agent, bus, queue = _manager(tmp_path, session)
            agent._claude_runtime = launch.runtime
            monkeypatch.setattr(agent, "_load_agent_settings", AsyncMock(return_value=launch.settings))
            try:
                await agent._handle(_web("inspect"))
                assert "catalog" in _replies(queue)[0]["error"]
            finally:
                await agent.stop()
                await bus.shutdown()
        elif entry == "worker":
            agent = worker.WorkerPool(config=worker.WorkerConfig(), runtime_root=tmp_path)
            result = await agent._execute(_step_envelope(tmp_path, verify=False), is_verify=False)
            assert result["status"] == "failed" and "catalog" in result["error"]
        elif entry == "planner":
            agent = planner.PlannerAgent(task_id="task-1", runtime_root=tmp_path)
            agent._plan = planner.TaskPlan("p1", "goal", "Inspect", str(tmp_path))
            monkeypatch.setattr(agent, "_load_installed", AsyncMock(return_value=launch.library))
            monkeypatch.setattr(agent, "_load_agent_settings", AsyncMock(return_value=launch.settings))
            with pytest.raises(planner.PlannerTurnError, match="catalog"):
                await agent._run_turn("inspect")
        elif entry in {"new-session", "profile"}:
            agent, bus, _ = _manager(tmp_path, _Session(hold=False))
            agent._process = SimpleNamespace(launch=launch)
            try:
                if entry == "new-session":
                    await agent._record_new_session(_web("/new"), {}, "previous")
                else:
                    await agent._record_profile_choice(None, "profile", manager_module.Effect.STARTS,
                                                       {}, "web", "Selected", "/profile 1")
                agent_db.start_record.assert_not_awaited()
            finally:
                await agent.stop()
                await bus.shutdown()
        else:
            recorder = ClaudeRunRecorder(agent_id="worker", task_id=None, command=[], cwd=str(tmp_path),
                                        input_text="inspect", launch=launch)
            await recorder.start()
            assert recorder.record_id is None  # a refused catalog still uses the existing record failure path

    async def check():
        gaps = []
        done = asyncio.Event()

        async def ticker():
            previous = time.monotonic()
            while not done.is_set():
                await asyncio.sleep(0.01)
                now = time.monotonic()
                gaps.append(now - previous)
                previous = now

        ticking = asyncio.create_task(ticker())
        await asyncio.sleep(0)
        try:
            await exercise()
        finally:
            done.set()
            await ticking
        assert calls, "the asynchronous entry must reach the blocking provider validation"
        assert max(gaps) < 0.2

    asyncio.run(asyncio.wait_for(check(), 10))
