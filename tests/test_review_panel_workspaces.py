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


@pytest.mark.parametrize("review_output", [None, "coverage/report.txt", "output.txt"])
def test_freeze_commits_pending_output_without_moving_an_advisory_review_head(workspace, review_output):
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
        "from pathlib import Path\n"
        "head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()\n"
        "if len(sys.argv) > 1:\n"
        "    output = Path(sys.argv[1])\n"
        "    output.parent.mkdir(parents=True, exist_ok=True)\n"
        "    output.write_text('review output\\n')\n"
        "print(head, flush=True)\n"
        "sys.stdin.readline()\n"
        "after = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()\n"
        "print(json.dumps({'passed': head == after}), flush=True)\n"
    ), *([review_output] if review_output else [])], cwd=directory,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert select.select([reviewer.stdout], [], [], 10)[0], "review did not start"
        assert reviewer.stdout.readline().strip() == reviewed_head
        pending = git(directory, "status", "--porcelain")
        async def save(_workspace):
            pass
        outcome = asyncio.run(ws.merge(frozen, step_key="s1", title="Deliver output", save=save))
        stdout, stderr = reviewer.communicate("finish\n", timeout=10)
        assert reviewer.returncode == 0, stderr
        assert json.loads(stdout)["passed"] is True
        assert outcome.workspace.state is ws.StepWorkspaceState.MERGED
        assert outcome.workspace.result_commit == frozen.result_commit
        assert outcome.leftovers is None
        assert git(directory, "rev-parse", "HEAD") == reviewed_head
        assert git(directory, "status", "--porcelain") == pending
        assert git(workspace.task_dir, "rev-parse", "HEAD^2") == reviewed_head
        if review_output:
            assert (directory / review_output).read_text() == "review output\n"
        assert (Path(workspace.task_dir) / "output.txt").read_text() == "delivered\n"
        assert git(workspace.task_dir, "ls-tree", "-r", "--name-only", "HEAD").splitlines() == [".gitignore", "output.txt"]
    finally:
        if reviewer.poll() is None:
            reviewer.kill()
            reviewer.communicate(timeout=10)


@pytest.mark.parametrize("change", ["commit", "index", "branch", "detached"])
def test_merge_refuses_changes_to_a_frozen_review_head_or_index(workspace, change):
    directory = Path(workspace.work_dir)
    (directory / "output.txt").write_text("delivered\n")
    frozen = asyncio.run(ws.freeze_review(workspace, step_key="s1"))
    if change in ("commit", "index"):
        (directory / "output.txt").write_text("unreviewed\n")
        git(directory, "add", "output.txt")
        if change == "commit":
            git(directory, "commit", "-qm", "Unreviewed change")
    elif change == "branch":
        git(directory, "switch", "-qc", "changed")
    else:
        git(directory, "checkout", "-q", "--detach")
    target_head = git(workspace.task_dir, "rev-parse", "HEAD")
    review_head = git(directory, "rev-parse", "HEAD")
    index = git(directory, "ls-files", "--stage")
    pending = git(directory, "status", "--porcelain")
    saved = []

    async def save(current):
        saved.append(current)

    outcome = asyncio.run(ws.merge(frozen, step_key="s1", title="Deliver output", save=save))
    assert outcome.workspace.state is ws.StepWorkspaceState.UNMERGED
    assert outcome.workspace.merge_error is ws.MergeError.STEP_BRANCH_CHANGED
    assert outcome.workspace.result_commit == frozen.result_commit
    assert saved[-1] == outcome.workspace
    assert outcome.leftovers is None
    assert git(workspace.task_dir, "rev-parse", "HEAD") == target_head
    assert not (Path(workspace.task_dir) / "output.txt").exists()
    assert git(directory, "rev-parse", "HEAD") == review_head
    assert git(directory, "ls-files", "--stage") == index
    assert git(directory, "status", "--porcelain") == pending


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


def test_shared_snapshot_preserves_the_workers_index_and_only_captures_the_task_directory(workspace):
    task = Path(workspace.task_dir)
    project = task / "project"
    project.mkdir()
    (project / "old.txt").write_text("old\n")
    git(task, "add", "project/old.txt")
    git(task, "commit", "-qm", "Project")
    head = git(task, "rev-parse", "HEAD")
    (project / "old.txt").unlink()
    (project / "output.txt").write_text("staged\n")
    git(task, "add", "project/output.txt")
    (project / "output.txt").write_text("delivered\n")
    (task / "other-step.txt").write_text("outside the task\n")
    index = git(task, "ls-files", "--stage")
    pending = git(task, "status", "--porcelain")
    repo = asyncio.run(ws.inspect(str(project)))
    assert isinstance(repo, ws.Repo)
    frozen = asyncio.run(ws.snapshot_review(repo, task_id="task-1", step_key="s1"))
    assert git(task, "rev-parse", "HEAD") == head
    assert git(task, "ls-files", "--stage") == index
    assert git(task, "status", "--porcelain") == pending
    assert git(task, "show", f"{frozen}:project/output.txt") == "delivered"
    assert git(task, "ls-tree", "-r", "--name-only", frozen).splitlines() == [".gitignore", "project/output.txt"]


@pytest.mark.parametrize("subdir", ["", "cache/project"])
def test_shared_snapshot_includes_force_added_output_without_changing_the_index(workspace, subdir):
    task = Path(workspace.task_dir)
    project = task / subdir
    project.mkdir(parents=True, exist_ok=True)
    output = project / "cache" / "output.txt"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("staged\n")
    git(task, "add", "-f", "--", output.relative_to(task).as_posix())
    output.write_text("delivered\n")
    (output.parent / "secret.txt").write_text("ignored local state\n")
    (task / "cache" / "outside.txt").write_text("outside the task\n")
    git(task, "add", "-f", "cache/outside.txt")
    head = git(task, "rev-parse", "HEAD")
    index = git(task, "ls-files", "--stage")
    pending = git(task, "status", "--porcelain")
    repo = asyncio.run(ws.inspect(str(project)))
    assert isinstance(repo, ws.Repo)
    frozen = asyncio.run(ws.snapshot_review(repo, task_id="task-1", step_key="s1"))
    assert git(task, "rev-parse", "HEAD") == head
    assert git(task, "ls-files", "--stage") == index
    assert git(task, "status", "--porcelain") == pending
    assert git(task, "show", f"{frozen}:{output.relative_to(task).as_posix()}") == "delivered"
    expected = [".gitignore", output.relative_to(task).as_posix()]
    if not subdir:
        expected.append("cache/outside.txt")
    assert git(task, "ls-tree", "-r", "--name-only", frozen).splitlines() == sorted(expected)


@pytest.mark.parametrize("tracked", [False, True])
def test_shared_snapshot_in_ignored_directory_excludes_local_state(workspace, tracked):
    task = Path(workspace.task_dir)
    project = task / "cache" / "project"
    project.mkdir()
    if tracked:
        (project / "output.txt").write_text("original\n")
        (project / "old.txt").write_text("old\n")
        git(task, "add", "-f", "cache/project/output.txt", "cache/project/old.txt")
        git(task, "commit", "-qm", "Track output")
        (project / "old.txt").unlink()
        (project / "output.txt").write_text("staged\n")
        git(task, "add", "-u", "--", "cache/project")
        (project / "output.txt").write_text("delivered\n")
    (project / "secret.txt").write_text("ignored local state\n")
    head = git(task, "rev-parse", "HEAD")
    index = git(task, "ls-files", "--stage")
    pending = git(task, "status", "--porcelain")
    repo = asyncio.run(ws.inspect(str(project)))
    assert isinstance(repo, ws.Repo)
    frozen = asyncio.run(ws.snapshot_review(repo, task_id="task-1", step_key="s1"))
    assert git(task, "rev-parse", "HEAD") == head
    assert git(task, "ls-files", "--stage") == index
    assert git(task, "status", "--porcelain") == pending
    if tracked:
        assert frozen != head
        assert git(task, "show", f"{frozen}:cache/project/output.txt") == "delivered"
        assert git(task, "ls-tree", "-r", "--name-only", frozen).splitlines() == [
            ".gitignore", "cache/project/output.txt"]
    else:
        assert frozen == head
        assert git(task, "ls-tree", "-r", "--name-only", frozen) == ".gitignore"
    assert (project / "secret.txt").read_text() == "ignored local state\n"
