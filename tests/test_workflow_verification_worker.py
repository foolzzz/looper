"""Verification work is observed through the pool's normal result path."""
from __future__ import annotations

import asyncio
import json
import subprocess
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from looper.agents.bus import MessageBus
from looper.agents.message import MessageEnvelope, MessageType, planner_address, worker_address
from looper.agents.runtimes import ADAPTERS
from looper.agents.runtimes.claude_code import ClaudeCodeRuntime
from looper.agents.tiers import worker
from tests.run_handle_support import ScriptedRun
from tests.runtime_model_helpers import builtin_launch


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True,
                          text=True, timeout=5).stdout.strip()


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("change", ["reported_change", "reported_deletion", "head", "dirty", "none", "nongit"])
def test_verification_guard_on_pool_result(tmp_path, monkeypatch, agent_db, enabled, change):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    root = tmp_path / "work"
    root.mkdir()
    if change != "nongit":
        _git(root, "init")
        _git(root, "-c", "user.name=Test", "-c", "user.email=test@example.org", "commit", "--allow-empty", "-m", "seed")
        before_head = _git(root, "rev-parse", "HEAD")
    report = {"status": "completed", "summary": "Checked the result.", "changed_files": {}, "deleted_files": []}
    if change == "reported_change":
        (root / "result.txt").write_text("already exists")
        report["changed_files"] = {"result.txt": "updated"}
    if change == "reported_deletion":
        report["deleted_files"] = ["deleted.txt"]
    run = ScriptedRun([{"type": "result", "subtype": "success", "is_error": False,
                        "result": json.dumps(report)}], hold=True)

    class Runtime(ClaudeCodeRuntime):
        hosts_runs = True

        def hosted_run(self, executable, **kwargs):
            return run

    monkeypatch.setitem(ADAPTERS, "claude-code", Runtime())
    monkeypatch.setattr(worker, "load_agent_settings", AsyncMock(return_value=replace(builtin_launch().settings)))
    monkeypatch.setattr(worker, "_load_settings", AsyncMock(return_value=worker.WorkerSettings(0, 0, "verify")))
    monkeypatch.setattr(worker, "load_task_limit", AsyncMock(return_value=1))

    async def scenario():
        runtime = tmp_path / "runtime"
        bus = MessageBus(runtime_root=runtime)
        results = []
        bus.subscribe_topic(MessageType.WORK_RESULT, results.append)
        pool = worker.WorkerPool(config=worker.WorkerConfig(), runtime_root=runtime)
        await pool.start(bus)
        try:
            item = MessageEnvelope.create(source=planner_address("t1"), target=worker_address("w1"),
                message_type=MessageType.WORK_ITEM,
                payload={"task_id": "s1", "parent_task_id": "t1", "instruction": "verify",
                         "verification": enabled, "context": {"work_dir": str(root)}})
            await pool._dispatch(item)
            task = pool._tasks["w1"]
            await run.started.wait()
            if change == "head":
                _git(root, "-c", "user.name=Test", "-c", "user.email=test@example.org", "commit", "--allow-empty", "-m", "moved")
            if change == "dirty":
                (root / "unexpected.txt").write_text("changed during verification")
            run.release.set()
            await task
            result = results[0].payload
            violated = change in {"reported_change", "reported_deletion", "head", "dirty"}
            assert result["status"] == ("failed" if enabled and violated else "completed")
            if enabled:
                details = result["verification"]
                assert details["git"] == (change != "nongit")
                if change == "nongit":
                    assert details["limitation"] == "report_only"
                    assert "report only" in result["summary"]
                else:
                    assert details["head"] == before_head
                    assert len(details["status_sha256"]) == 64
                if violated:
                    assert result["failure"] == "verification_changed_code"
                    assert result["summary"].startswith("Verification changed the code:")
                    assert any("Verification changed the code" in call.kwargs["summary"]
                               for call in agent_db.insert_activity.await_args_list)
                    assert agent_db.finish_record.await_args.kwargs["status"] == "failed"
            else:
                assert "verification" not in result
                assert "failure" not in result
        finally:
            await pool.stop()
            await bus.shutdown()

    asyncio.run(asyncio.wait_for(scenario(), 10))
