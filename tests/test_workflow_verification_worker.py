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
            execution_root = Path(launches[-1]["cwd"])
            if change == "head":
                _git(execution_root, "-c", "user.name=Test", "-c", "user.email=test@example.org", "commit", "--allow-empty", "-m", "moved")
            if change == "dirty":
                (execution_root / "unexpected.txt").write_text("changed during verification")
            if change in {"modified", "staged", "untracked"}:
                (execution_root / "existing.txt").write_text("silently rewritten by verifier\n")
                if change == "staged":
                    _git(execution_root, "add", "existing.txt")
                assert _git(execution_root, "status", "--porcelain") == before_status
            if change == "committed":
                (execution_root / "existing.txt").write_text("committed by verifier\n")
                _git(execution_root, "add", "existing.txt")
                _git(execution_root, "-c", "user.name=Test", "-c", "user.email=test@example.org", "commit", "-m", "verification")
            if change == "deleted":
                (execution_root / "existing.txt").unlink()
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
                    assert _git(execution_root, "rev-parse", "HEAD") == before_head
                    assert _git(execution_root, "status", "--porcelain") == before_status
                    assert _git(execution_root, "diff", "--cached") == before_index
                    assert {p.name: p.read_bytes() for p in execution_root.iterdir() if p.is_file()} == before_files
                    assert details["restored"] is True
                    if change in {"modified", "staged", "untracked"}:
                        # A new pool must reuse the original baseline, even if rollback later drifts.
                        await pool.stop()
                        (execution_root / "existing.txt").write_text("a dirty baseline must not become trusted\n")
                        pool = worker.WorkerPool(config=worker.WorkerConfig(), runtime_root=runtime)
                        await pool.start(bus)
                        await pool._dispatch(item)
                        await pool._tasks["w1"]
                        assert results[-1].payload["failure"] == "verification_changed_code"
                        assert results[-1].payload["status"] == "failed"
                        assert len(launches) == 1
                        assert (execution_root / "existing.txt").read_bytes() == before_files["existing.txt"]
                if change != "nongit":
                    assert _git(root, "rev-parse", "HEAD") == before_head
                    assert _git(root, "status", "--porcelain") == before_status
                    assert _git(root, "diff", "--cached") == before_index
                assert {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()} == before_files
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
        for step_id in ("s1", "new-verification-step"):
            attempt = replace(item, payload={**item.payload, "task_id": step_id})
            refused = asyncio.run(asyncio.wait_for(new_pool._prepare(attempt), 10))
            assert refused["failure"] == "verification_changed_code"
            assert refused["verification"]["restored"] is False


@pytest.fixture
def verification_runtime(monkeypatch, agent_db):
    launches = []
    runs = []

    class Runtime(ClaudeCodeRuntime):
        hosts_runs = True

        def hosted_run(self, executable, **kwargs):
            launches.append(kwargs)
            return runs[len(launches) - 1]

    monkeypatch.setitem(ADAPTERS, "claude-code", Runtime())
    monkeypatch.setattr(worker, "load_agent_settings", AsyncMock(return_value=builtin_launch().settings))
    monkeypatch.setattr(worker, "_load_settings", AsyncMock(return_value=worker.WorkerSettings(0, 0, "verify")))
    monkeypatch.setattr(worker, "load_task_limit", AsyncMock(return_value=2))
    return runs, launches


def _completed_run(*, hold=True):
    return ScriptedRun([{"type": "result", "subtype": "success", "is_error": False,
                         "result": json.dumps({"status": "completed", "summary": "Checked.",
                                               "changed_files": {}, "deleted_files": []})}], hold=hold)


def _repo(root):
    root.mkdir()
    _git(root, "init")
    (root / "tracked").write_text("committed")
    _git(root, "add", "tracked")
    _git(root, "-c", "user.name=Test", "-c", "user.email=test@example.org", "commit", "-m", "seed")
    return root


def _item(root, run="v1", *, verification=True):
    return MessageEnvelope.create(source=planner_address("t1"), target=worker_address(run),
        message_type=MessageType.WORK_ITEM, payload={"parent_task_id": "t1", "task_id": run,
            "verification": verification, "context": {"work_dir": str(root)}})


def test_verification_restore_never_falls_back_to_enclosing_repository(tmp_path):
    root = _repo(tmp_path / "repo")
    (root / ".gitignore").write_text("private/\n")
    private = _repo(root / "private")
    backup = tmp_path / "backup"
    worker._verification_baseline(str(private), backup)
    (root / "developer-new").write_text("must survive rollback")
    before = worker._verification_snapshot(str(root))
    shutil.rmtree(private / ".git")
    assert worker._restore_verification(str(private), backup) is False
    assert worker._verification_snapshot(str(root)) == before
    assert (root / "developer-new").read_text() == "must survive rollback"


@pytest.mark.parametrize("verifier_edits", [False, True])
def test_parallel_developer_writes_survive_verification(tmp_path, verification_runtime, verifier_edits):
    runs, launches = verification_runtime
    root = _repo(tmp_path / "repo")
    runs.extend([_completed_run(), _completed_run()])

    async def scenario():
        bus = MessageBus(runtime_root=tmp_path / "runtime")
        results = []
        bus.subscribe_topic(MessageType.WORK_RESULT, results.append)
        pool = worker.WorkerPool(config=worker.WorkerConfig(), runtime_root=tmp_path / "runtime")
        await pool.start(bus)
        try:
            await pool._dispatch(_item(root))
            verify_task = pool._tasks["v1"]
            await runs[0].started.wait()
            await pool._dispatch(_item(root, "dev", verification=False))
            dev_task = pool._tasks["dev"]
            await runs[1].started.wait()
            (root / "tracked").write_text("developer uncommitted modification")
            (root / "developer-new").write_text("developer untracked work")
            verify_root = Path(launches[0]["cwd"])
            assert Path(launches[1]["cwd"]) == root
            if verifier_edits:
                (verify_root / "tracked").write_text("verifier formatted it")
                (verify_root / "verifier-new").write_text("verifier edit")
            runs[0].release.set()
            await verify_task
            result = results[0].payload
            assert result["status"] == ("failed" if verifier_edits else "completed")
            assert (root / "tracked").read_text() == "developer uncommitted modification"
            assert (root / "developer-new").read_text() == "developer untracked work"
            assert not (root / "verifier-new").exists()
            assert (verify_root / "tracked").read_text() == "committed"
            assert not (verify_root / "verifier-new").exists()
            runs[1].release.set()
            await dev_task
            assert results[-1].payload["status"] == "completed"
        finally:
            await pool.stop()
            await bus.shutdown()

    asyncio.run(asyncio.wait_for(scenario(), 15))


@pytest.mark.parametrize("edit", [False, True])
def test_verification_checked_out_submodule(tmp_path, verification_runtime, edit):
    runs, launches = verification_runtime
    upstream = _repo(tmp_path / "upstream")
    root = _repo(tmp_path / "repo")
    _git(root, "-c", "protocol.file.allow=always", "submodule", "add", str(upstream), "module")
    _git(root, "-c", "user.name=Test", "-c", "user.email=test@example.org", "commit", "-am", "submodule")
    (root / "module" / "tracked").write_text("staged developer work")
    _git(root / "module", "add", "tracked")
    (root / "module" / "tracked").write_text("unstaged developer work")
    (root / "module" / "new").write_text("untracked developer work")
    before = worker._verification_snapshot(str(root))
    assert not before.get("error")
    runs.append(_completed_run())

    async def scenario():
        pool = worker.WorkerPool(config=worker.WorkerConfig(), runtime_root=tmp_path / "runtime")
        task = asyncio.create_task(pool._execute(_item(root)))
        await runs[0].started.wait()
        checkout = Path(launches[0]["cwd"])
        assert worker._verification_snapshot(str(checkout)) == before
        if edit:
            (checkout / "module" / "tracked").write_text("verifier edit")
            (checkout / "module" / "new").unlink()
        runs[0].release.set()
        result = await task
        assert result["status"] == ("failed" if edit else "completed")
        if edit:
            assert result["failure"] == "verification_changed_code"
            assert result["verification"]["restored"] is True
        assert worker._verification_snapshot(str(checkout)) == before
        assert worker._verification_snapshot(str(root)) == before

    asyncio.run(asyncio.wait_for(scenario(), 15))


def test_snapshot_failure_does_not_require_code_repair(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "_verification_snapshot", lambda _: {
        "git": False, "head": None, "status_sha256": None, "error": "git_snapshot_unavailable"})
    pool = worker.WorkerPool(config=worker.WorkerConfig(), runtime_root=tmp_path / "runtime")
    result = asyncio.run(asyncio.wait_for(pool._prepare(_item(tmp_path / "repo")), 5))
    assert result["status"] == "failed"
    assert result["failure"] == "verification_unavailable"
    assert not list(pool._verification_pending("t1", str(tmp_path / "repo")).glob("*.json"))


@pytest.mark.parametrize("stop,restore", [("timeout", True), ("kill", True), ("cancel", True),
                                         ("exception", True), ("timeout", False)])
def test_interrupted_verifier_restores_before_next_step(tmp_path, monkeypatch, verification_runtime, stop, restore):
    runs, launches = verification_runtime
    root = _repo(tmp_path / "repo")

    class BrokenRun(ScriptedRun):
        async def events(self):
            await self.release.wait()
            raise RuntimeError("event stream failed")
            yield  # async iterator

    runs.extend([BrokenRun([], hold=True) if stop == "exception" else _completed_run(),
                 _completed_run(hold=False)])
    if stop == "timeout":
        monkeypatch.setattr(worker, "_load_settings", AsyncMock(return_value=worker.WorkerSettings(0.5, 0, "verify")))

    async def scenario():
        bus = MessageBus(runtime_root=tmp_path / "runtime")
        results = []
        bus.subscribe_topic(MessageType.WORK_RESULT, results.append)
        pool = worker.WorkerPool(config=worker.WorkerConfig(), runtime_root=tmp_path / "runtime")
        fallback = AsyncMock(return_value=builtin_launch().settings)
        monkeypatch.setattr(pool, "_fallback_profile", fallback)
        await pool.start(bus)
        try:
            await pool._dispatch(_item(root))
            task = pool._tasks["v1"]
            await runs[0].started.wait()
            checkout = Path(launches[0]["cwd"])
            (checkout / "tracked").write_text("verifier edit before interruption")
            (checkout / "new").write_text("verifier new file")
            real_restore = worker._restore_verification
            if not restore:
                monkeypatch.setattr(worker, "_restore_verification", lambda *args: False)
            if stop == "kill":
                assert await pool.kill("v1")
            elif stop == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                if stop == "exception":
                    runs[0].release.set()
                await task
            assert runs[0].end_calls >= 1
            fallback.assert_not_awaited()  # A profile fallback must not hide a verifier's edits.
            assert (checkout / "tracked").read_text() == (
                "committed" if restore else "verifier edit before interruption")
            assert (checkout / "new").exists() == (not restore)
            if stop in {"timeout", "exception"}:
                assert results[0].payload["failure"] == "verification_changed_code"
                assert results[0].payload["verification"]["restored"] is restore
            else:
                assert not results  # kill/cancel retain their no-result contract
            if not restore:
                await pool.stop()
                pool = worker.WorkerPool(config=worker.WorkerConfig(), runtime_root=tmp_path / "runtime")
                await pool.start(bus)
                await pool._dispatch(_item(root, "new-verification-step"))
                await pool._tasks["new-verification-step"]
                assert results[-1].payload["failure"] == "verification_changed_code"
                assert results[-1].payload["verification"]["restored"] is False
                assert len(launches) == 1
                monkeypatch.setattr(worker, "_restore_verification", real_restore)
                await pool._dispatch(_item(root, "new-verification-step"))
                await pool._tasks["new-verification-step"]
                assert results[-1].payload["verification"]["restored"] is True
                assert (checkout / "tracked").read_text() == "committed"
                assert not (checkout / "new").exists()
                assert len(launches) == 1
            await pool._dispatch(_item(root, "new-verification-step"))
            await pool._tasks["new-verification-step"]
            assert results[-1].payload["status"] == "completed"
            assert (root / "tracked").read_text() == "committed"
            assert not (root / "new").exists()
        finally:
            await pool.stop()
            await bus.shutdown()

    asyncio.run(asyncio.wait_for(scenario(), 15))
