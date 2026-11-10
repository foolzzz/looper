"""Review snapshots stay on the reviewed commit while an accepted step is merged."""
from __future__ import annotations

import asyncio
import json
import select
import subprocess
import sys
from pathlib import Path

import pytest

from looper.agents import step_workspaces as ws


def git(path, *args):
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgSign=false", *args],
                          cwd=path, check=True, capture_output=True, text=True, timeout=10).stdout.strip()


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    task = tmp_path / "task"
    task.mkdir()
    git(task, "init", "-q", "-b", "main")
    git(task, "config", "user.name", "Example")
    git(task, "config", "user.email", "dev@example.org")
    (task / ".gitignore").write_text("cache/\n")
    git(task, "add", ".gitignore")
    git(task, "commit", "-qm", "Initial")
    (task / "cache").mkdir()
    (task / "cache" / "state").write_text("local state")
    repo = asyncio.run(ws.inspect(str(task)))
    assert isinstance(repo, ws.Repo)
    workspace = ws.new_workspace(repo, runtime_root=tmp_path / "runtime", workspace_id=1,
        task_id="task-1", step_id="step-1", step_key="s1", base=repo.head, checkpointed=False)
    asyncio.run(ws.ensure_worktree(workspace))
    workspace, skipped = ws.link(workspace, ["cache"])
    assert not skipped
    yield workspace
    git(task, "worktree", "remove", "--force", workspace.path)


def test_freeze_commits_pending_output_without_moving_an_advisory_review_head(workspace):
    directory = Path(workspace.work_dir)
    worker_head = git(directory, "rev-parse", "HEAD")
    (directory / "output.txt").write_text("delivered\n")
    frozen = asyncio.run(ws.freeze_review(workspace, step_key="s1"))
    reviewed_head = git(directory, "rev-parse", "HEAD")
    assert frozen.state is ws.StepWorkspaceState.ACTIVE
    assert frozen.result_commit == reviewed_head != worker_head
    assert git(workspace.task_dir, "rev-parse", "HEAD") == worker_head
    assert git(directory, "ls-tree", "-r", "--name-only", reviewed_head).splitlines() == [".gitignore", "output.txt"]
    # A slow advisory reviewer starts on the frozen HEAD before required seats allow acceptance.
    reviewer = subprocess.Popen([sys.executable, "-c", (
        "import json, subprocess, sys\n"
        "head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()\n"
        "print(head, flush=True)\n"
        "sys.stdin.readline()\n"
        "after = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()\n"
        "print(json.dumps({'passed': head == after}), flush=True)\n"
    )], cwd=directory, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert select.select([reviewer.stdout], [], [], 10)[0], "review did not start"
        assert reviewer.stdout.readline().strip() == reviewed_head
        async def save(_workspace):
            pass
        outcome = asyncio.run(ws.merge(frozen, step_key="s1", title="Deliver output", save=save))
        stdout, stderr = reviewer.communicate("finish\n", timeout=10)
        assert reviewer.returncode == 0, stderr
        assert json.loads(stdout)["passed"] is True
        assert outcome.workspace.state is ws.StepWorkspaceState.MERGED
        assert outcome.leftovers is None
        assert git(directory, "rev-parse", "HEAD") == reviewed_head
        assert (Path(workspace.task_dir) / "output.txt").read_text() == "delivered\n"
        assert git(workspace.task_dir, "ls-tree", "-r", "--name-only", "HEAD").splitlines() == [".gitignore", "output.txt"]
    finally:
        if reviewer.poll() is None:
            reviewer.kill()
            reviewer.communicate(timeout=10)


def test_freeze_keeps_a_link_replaced_with_worker_output(workspace):
    path = Path(workspace.work_dir) / "cache"
    path.unlink()
    path.write_text("worker output\n")
    frozen = asyncio.run(ws.freeze_review(workspace, step_key="s1"))
    assert git(workspace.path, "show", f"{frozen.result_commit}:cache") == "worker output"


def test_freeze_refuses_a_workspace_that_left_its_branch(workspace):
    git(workspace.path, "switch", "-qc", "changed")
    with pytest.raises(ws.GitError, match="no longer on its branch"):
        asyncio.run(ws.freeze_review(workspace, step_key="s1"))
