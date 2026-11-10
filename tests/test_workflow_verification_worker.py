"""Verification work is observed through the pool's normal result path."""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from types import SimpleNamespace
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
@pytest.mark.parametrize("change", ["reported_change", "reported_deletion", "head", "dirty",
                                   "modified", "staged", "untracked", "committed", "deleted", "none", "nongit"])
def test_verification_guard_on_pool_result(tmp_path, monkeypatch, agent_db, enabled, change):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    root = tmp_path / "work"
    root.mkdir()
    if change != "nongit":
        _git(root, "init")
        _git(root, "-c", "user.name=Test", "-c", "user.email=test@example.org", "commit", "--allow-empty", "-m", "seed")
        before_head = _git(root, "rev-parse", "HEAD")
    if change in {"modified", "staged", "untracked", "committed", "deleted"}:
        (root / "existing.txt").write_text("original\n")
        if change != "untracked":
            _git(root, "add", "existing.txt")
            _git(root, "-c", "user.name=Test", "-c", "user.email=test@example.org", "commit", "-m", "file")
            before_head = _git(root, "rev-parse", "HEAD")
            (root / "existing.txt").write_text("developer's uncommitted work\n")
            if change == "staged":
                _git(root, "add", "existing.txt")
    report = {"status": "completed", "summary": "Checked the result.", "changed_files": {}, "deleted_files": []}
    if change == "reported_change":
        (root / "result.txt").write_text("already exists")
        report["changed_files"] = {"result.txt": "updated"}
    if change == "reported_deletion":
        report["deleted_files"] = ["deleted.txt"]
    before_status = _git(root, "status", "--porcelain") if change != "nongit" else None
    before_index = _git(root, "diff", "--cached") if change != "nongit" else None
    before_files = {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}
    run = ScriptedRun([{"type": "result", "subtype": "success", "is_error": False,
                        "result": json.dumps(report)}], hold=True)
    launches = []

    class Runtime(ClaudeCodeRuntime):
        hosts_runs = True

        def hosted_run(self, executable, **kwargs):
            launches.append(kwargs)
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
            if change in {"modified", "staged", "untracked"}:
                (root / "existing.txt").write_text("silently rewritten by verifier\n")
                if change == "staged":
                    _git(root, "add", "existing.txt")
                assert _git(root, "status", "--porcelain") == before_status
            if change == "committed":
                (root / "existing.txt").write_text("committed by verifier\n")
                _git(root, "add", "existing.txt")
                _git(root, "-c", "user.name=Test", "-c", "user.email=test@example.org", "commit", "-m", "verification")
            if change == "deleted":
                (root / "existing.txt").unlink()
            run.release.set()
            await task
            result = results[0].payload
            violated = change in {"reported_change", "reported_deletion", "head", "dirty",
                                  "modified", "staged", "untracked", "committed", "deleted"}
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
                    assert _git(root, "rev-parse", "HEAD") == before_head
                    assert _git(root, "status", "--porcelain") == before_status
                    assert _git(root, "diff", "--cached") == before_index
                    assert {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()} == before_files
                    assert details["restored"] is True
                    if change in {"modified", "staged", "untracked"}:
                        # A new pool must reuse the original baseline, even if rollback later drifts.
                        await pool.stop()
                        (root / "existing.txt").write_text("a dirty baseline must not become trusted\n")
                        pool = worker.WorkerPool(config=worker.WorkerConfig(), runtime_root=runtime)
                        await pool.start(bus)
                        await pool._dispatch(item)
                        await pool._tasks["w1"]
                        assert results[-1].payload["failure"] == "verification_changed_code"
                        assert results[-1].payload["status"] == "failed"
                        assert len(launches) == 1
            else:
                assert "verification" not in result
                assert "failure" not in result
        finally:
            await pool.stop()
            await bus.shutdown()

    asyncio.run(asyncio.wait_for(scenario(), 10))


@pytest.mark.parametrize("restore", [False, True])
def test_verification_rollback_in_linked_worktree(tmp_path, monkeypatch, restore):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init")
    (root / "tracked").write_bytes(b"original\0file")
    (root / "sub").mkdir()
    (root / "sub" / "nested").write_text("original nested file")
    _git(root, "add", "tracked", "sub/nested")
    _git(root, "-c", "user.name=Test", "-c", "user.email=test@example.org", "commit", "-m", "seed")
    tree = tmp_path / "step"
    _git(root, "worktree", "add", "--detach", str(tree))
    (tree / "tracked").write_bytes(b"staged\0work")
    _git(tree, "add", "tracked")
    (tree / "tracked").write_bytes(b"unstaged\0work")
    (tree / "new\nfile").write_bytes(b"untracked\0work")
    (tree / "link").symlink_to("tracked")
    pool = worker.WorkerPool(config=worker.WorkerConfig(), runtime_root=tmp_path / "runtime")
    backup = pool._verification_backup("t1", "s1", str(tree))
    before = worker._verification_baseline(str(tree), backup)
    assert not before.get("error")
    (tree / "tracked").write_text("verifier edit")
    (tree / "new\nfile").unlink()
    (tree / "link").unlink()
    (tree / "link").write_text("replaced symlink")
    _git(tree, "add", "-A")
    _git(tree, "-c", "user.name=Test", "-c", "user.email=test@example.org", "commit", "-m", "verification")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "nested").write_text("must survive rollback")
    shutil.rmtree(tree / "sub")
    (tree / "sub").symlink_to(outside, target_is_directory=True)
    if not restore:
        monkeypatch.setattr(worker, "_restore_verification", lambda *args: False)
    ctx = SimpleNamespace(verification=before, work_dir=str(tree), parent_task_id="t1", worker_id="w1", step_id="s1")
    monkeypatch.setattr(pool, "_activity", AsyncMock())
    result = asyncio.run(asyncio.wait_for(pool._verification_result(ctx, {"status": "completed"}), 10))
    assert result["failure"] == "verification_changed_code"
    assert result["verification"]["restored"] is restore
    assert (outside / "nested").read_text() == "must survive rollback"
    if restore:
        assert worker._verification_snapshot(str(tree)) == before
        assert (tree / "tracked").read_bytes() == b"unstaged\0work"
        assert (tree / "new\nfile").read_bytes() == b"untracked\0work"
        assert (tree / "link").is_symlink()
        assert (tree / "sub" / "nested").read_text() == "original nested file"
    else:
        new_pool = worker.WorkerPool(config=worker.WorkerConfig(), runtime_root=tmp_path / "runtime")
        item = MessageEnvelope.create(source=planner_address("t1"), target=worker_address("w2"),
            message_type=MessageType.WORK_ITEM, payload={"parent_task_id": "t1", "task_id": "s1",
                "verification": True, "context": {"work_dir": str(tree)}})
        refused = asyncio.run(asyncio.wait_for(new_pool._prepare(item), 10))
        assert refused["failure"] == "verification_changed_code"
