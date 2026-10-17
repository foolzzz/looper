"""Step workspaces in real git repositories: the checkpoint, the worktrees, the merge into the task's branch.

Every repository here signs commits by default with a gpg that always fails,
verifies signatures on merge, and has every hook write to a marker file (and
the pre-commit, commit-msg and pre-merge-commit hooks fail): none of that may
touch what Looper does. The user's and the Worker's own git commands are run
with ``git`` directly, without hooks.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from looper.agents import step_workspaces as ws
from looper.agents.step_workspaces import (
    ALLOWED_TRANSITIONS,
    IllegalTransition,
    MergeError,
    Repo,
    Round,
    StepWorkspace,
    StepWorkspaceState,
    Unusable,
)

HOOKS = ("pre-commit", "commit-msg", "post-commit", "post-checkout", "post-merge", "pre-merge-commit",
         "reference-transaction", "post-rewrite")


@pytest.fixture(autouse=True)
def _git_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                 "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(name, raising=False)


def git(cwd: Path | str, *args: str) -> str:
    """A git command of the user or a Worker (no hooks, no signing), its output."""
    run = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgSign=false", *args],
                         cwd=cwd, capture_output=True, text=True, check=True)
    return run.stdout.rstrip()


def run(coro: Any) -> Any:
    return asyncio.run(coro)


class Saved(list):
    """The workspace states ``merge`` saved, in order."""

    async def __call__(self, workspace: StepWorkspace) -> None:
        self.append(workspace)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repository on main with one commit, signing that fails, and hooks that leave a mark."""
    path = tmp_path / "task"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    for key, value in (("user.name", "User"), ("user.email", "user@example.com"), ("commit.gpgSign", "true"),
                       ("gpg.program", "false"), ("merge.verifySignatures", "true")):
        git(path, "config", key, value)
    (path / "shared.txt").write_text("a\nb\nc\n")
    (path / "base.txt").write_text("base\n")
    (path / ".gitignore").write_text(".env\n")
    git(path, "add", "-A")
    git(path, "commit", "-qm", "init")
    hooks = path / ".git" / "hooks"
    marks = tmp_path / "hook-marks"
    for hook in HOOKS:
        fail = "exit 1" if hook in ("pre-commit", "commit-msg", "pre-merge-commit") else "exit 0"
        (hooks / hook).write_text(f"#!/bin/sh\necho {hook} >> {marks}\n{fail}\n")
        (hooks / hook).chmod(0o755)
    return path


@pytest.fixture(autouse=True)
def _no_hook_ran(tmp_path: Path) -> Any:
    yield
    marks = tmp_path / "hook-marks"
    assert not marks.exists(), f"hooks ran: {marks.read_text()}"


def snapshot(path: Path) -> tuple[Any, ...]:
    """The task directory as the user sees it: HEAD, its branch, the index, the files, and no merge in progress."""
    files = {str(p.relative_to(path)): p.read_bytes() for p in sorted(path.rglob("*"))
             if p.is_file() and ".git" not in p.relative_to(path).parts}
    return (git(path, "rev-parse", "HEAD"), git(path, "symbolic-ref", "-q", "HEAD"), git(path, "ls-files", "-s"),
            git(path, "status", "--porcelain", "--untracked-files=all"), files,
            (path / ".git" / "MERGE_HEAD").exists())


def inspected(path: Path) -> Repo:
    repo = run(ws.inspect(str(path)))
    assert isinstance(repo, Repo), repo
    return repo


def workspace(repo: Repo, root: Path, wid: int, key: str, base: str, *, checkpointed: bool = False) -> StepWorkspace:
    created = ws.new_workspace(repo, runtime_root=root, workspace_id=wid, task_id="task-1", step_id=f"step-{key}",
                               step_key=key, base=base, checkpointed=checkpointed)
    run(ws.ensure_worktree(created))
    return created


def merge(workspace_: StepWorkspace, key: str, saved: Saved | None = None) -> ws.MergeResult:
    return run(ws.merge(workspace_, step_key=key, title=f"do {key}", save=saved if saved is not None else Saved()))


def parents(path: Path, rev: str) -> list[str]:
    return git(path, "rev-list", "--parents", "-n", "1", rev).split()[1:]


# --- The rules ---


def test_a_workspace_changes_state_only_along_the_table() -> None:
    assert {state: {s.value for s in targets} for state, targets in ALLOWED_TRANSITIONS.items()} == {
        StepWorkspaceState.ACTIVE: {"merging", "discarded"},
        StepWorkspaceState.MERGING: {"merged", "unmerged"},
        StepWorkspaceState.UNMERGED: {"discarded"},
        StepWorkspaceState.MERGED: set(),
        StepWorkspaceState.DISCARDED: set(),
    }
    active = StepWorkspace(id=1, task_id="t", step_id="s", repo_dir="/r", subdir="", target_branch="refs/heads/main",
                           path="/w", branch="looper/t/s-1", base_commit="abc")
    assert active.moved(StepWorkspaceState.MERGING).state is StepWorkspaceState.MERGING
    with pytest.raises(IllegalTransition):
        active.moved(StepWorkspaceState.MERGED)
    with pytest.raises(IllegalTransition):
        active.moved(StepWorkspaceState.DISCARDED).moved(StepWorkspaceState.ACTIVE)
    unmerged = active.moved(StepWorkspaceState.MERGING).moved(
        StepWorkspaceState.UNMERGED, merge_error=MergeError.CONFLICT, detail="a.py")
    assert StepWorkspace.from_row(unmerged.row()) == unmerged


REPO = Repo(toplevel="/r", subdir="", branch="refs/heads/main", head="abc")
NOT_GIT = Unusable("not in git", repository=False)
DETACHED = Unusable("the task directory's HEAD is detached", repository=True)


@pytest.mark.parametrize(("in_flight", "ready", "repo", "expected"), [
    # One step and nothing else in flight: the task directory, with or without git.
    ([], [False], REPO, Round(1, False)),
    ([], [False], DETACHED, Round(1, False)),
    # Two independent steps start together: both in worktrees.
    ([], [False, False], REPO, Round(2, True)),
    # A step that becomes ready while another works in its worktree gets one too.
    ([True], [False], REPO, Round(1, True)),
    # The step joining two others starts after both: alone, in the task directory.
    ([], [False], REPO, Round(1, False)),
    # A step works in the task directory (added from chat, say): the ready ones wait.
    ([False], [False], REPO, Round(0, True, "A step works in the task directory: the steps ready to run wait until "
                                            "it ends")),
    # A step that carries on in its worktree (a continued plan) keeps it.
    ([], [True], REPO, Round(1, True)),
    # Outside git nothing changes: steps share the directory as before.
    ([False], [False, False], NOT_GIT, Round(2, False)),
    # In git but without workspaces now: one at a time.
    ([], [False, False], DETACHED, Round(1, False, "Steps run one at a time: the task directory's HEAD is detached")),
    ([True], [False], DETACHED, Round(0, False, "Steps run one at a time: the task directory's HEAD is detached")),
])
def test_steps_that_run_together_get_worktrees(in_flight: list[bool], ready: list[bool], repo: Any,
                                               expected: Round) -> None:
    assert ws.plan_round(in_flight=in_flight, ready=ready, repo=repo) == expected


# --- The task directory ---


def test_the_task_directory_can_have_workspaces_on_a_branch_with_a_commit(repo: Path, tmp_path: Path,
                                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    found = inspected(repo)
    assert (found.branch, found.head, found.subdir) == ("refs/heads/main", git(repo, "rev-parse", "HEAD"), "")
    (repo / "pkg" / "sub").mkdir(parents=True)
    assert inspected(repo / "pkg" / "sub").subdir == "pkg/sub"

    plain = tmp_path / "plain"
    plain.mkdir()
    assert run(ws.inspect(str(plain))) == Unusable("the task directory is not in a git repository", repository=False)

    git(repo, "switch", "-q", "--detach")
    assert run(ws.inspect(str(repo))) == DETACHED
    git(repo, "switch", "-q", "main")
    (repo / ".git" / "rebase-merge").mkdir()
    assert run(ws.inspect(str(repo))) == Unusable("a rebase is in progress in the task directory", repository=True)
    (repo / ".git" / "rebase-merge").rmdir()
    (repo / ".git" / "MERGE_HEAD").write_text(git(repo, "rev-parse", "HEAD"))
    assert run(ws.inspect(str(repo))) == Unusable("a merge is in progress in the task directory", repository=True)
    (repo / ".git" / "MERGE_HEAD").unlink()

    empty = tmp_path / "empty"
    empty.mkdir()
    git(empty, "init", "-q", "-b", "trunk")
    assert run(ws.inspect(str(empty))) == Unusable("trunk has no commit yet", repository=True)

    monkeypatch.setattr(ws, "_version", (2, 39))
    assert run(ws.inspect(str(repo))) == Unusable("git 2.39 is older than 2.40", repository=True)


def test_a_checkpoint_commits_every_pending_change_of_the_task_directory(repo: Path) -> None:
    head = git(repo, "rev-parse", "HEAD")
    assert run(ws.checkpoint(inspected(repo), task_id="task-1")) == (head, 0)  # nothing pending: the tip

    (repo / "base.txt").write_text("base\nfrom an earlier step\n")
    (repo / "new.txt").write_text("untracked\n")
    (repo / "staged.txt").write_text("staged\n")
    git(repo, "add", "staged.txt")
    (repo / "shared.txt").write_text("a\nB\nc\n")
    git(repo, "add", "shared.txt")
    (repo / "shared.txt").write_text("a\nB\nC\n")  # staged in part
    (repo / ".env").write_text("SECRET=1\n")  # ignored
    commit, files = run(ws.checkpoint(inspected(repo), task_id="task-1"))

    assert files == 4 and commit == git(repo, "rev-parse", "HEAD") and parents(repo, commit) == [head]
    assert git(repo, "show", "--name-only", "--format=", commit).split() == ["base.txt", "new.txt", "shared.txt",
                                                                           "staged.txt"]
    assert git(repo, "show", f"{commit}:shared.txt") == "a\nB\nC"  # what the file is, not what was staged
    assert git(repo, "status", "--porcelain") == ""  # clean but for the ignored file
    assert (repo / ".env").exists()
    assert git(repo, "log", "-1", "--format=%an <%ae>|%cn <%ce>|%s|%(trailers:key=Looper-Task,valueonly)") == (
        "Looper <looper@localhost>|Looper <looper@localhost>|looper: checkpoint before parallel steps|task-1")
    assert git(repo, "log", "-1", "--format=%G?") == "N"  # not signed


def test_a_checkpoint_of_a_task_directory_inside_a_repository_leaves_the_rest_alone(repo: Path) -> None:
    (repo / "pkg").mkdir()
    (repo / "pkg" / "mod.py").write_text("x = 1\n")
    (repo / "base.txt").write_text("outside, unstaged\n")
    (repo / "outside.txt").write_text("outside, staged\n")
    git(repo, "add", "outside.txt")
    commit, files = run(ws.checkpoint(inspected(repo / "pkg"), task_id="task-1"))
    assert files == 1 and git(repo, "show", "--name-only", "--format=", commit) == "pkg/mod.py"
    assert git(repo, "status", "--porcelain").splitlines() == [" M base.txt", "A  outside.txt"]


# --- Worktrees and merges ---


def test_two_steps_merge_into_the_branch_with_a_merge_commit_each(repo: Path, tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    (repo / "base.txt").write_text("base\nserial step\n")  # an earlier step's change, not committed
    base, _ = run(ws.checkpoint(inspected(repo), task_id="task-1"))
    one = workspace(inspected(repo), root, 1, "s1", base, checkpointed=True)
    two = workspace(inspected(repo), root, 2, "s/2 two", base, checkpointed=True)
    assert (one.branch, Path(one.path)) == ("looper/task-1/s1-1", root / "worktrees" / "task-1" / "s1-1")
    assert two.branch == "looper/task-1/s-2-two-2"
    assert (Path(one.path) / "base.txt").read_text() == "base\nserial step\n"  # it starts from the checkpoint
    assert not (Path(one.path) / ".env").exists()

    # s1's Worker commits once and leaves a file; s2's leaves everything uncommitted.
    (Path(one.path) / "one.txt").write_text("one\n")
    git(one.path, "add", "one.txt")
    git(one.path, "commit", "-qm", "worker: one.txt")
    (Path(one.path) / "one-more.txt").write_text("more\n")
    (Path(two.path) / "two.txt").write_text("two\n")
    (Path(two.path) / "shared.txt").write_text("a\nb\nc2\n")
    # The user has unrelated work in the task directory, staged and not.
    (repo / "notes.txt").write_text("mine\n")
    (repo / "staged.txt").write_text("mine too\n")
    git(repo, "add", "staged.txt")

    saved = Saved()
    first = merge(one, "s1", saved)
    assert first.workspace.state is StepWorkspaceState.MERGED and first.leftovers
    assert [s.state.value for s in saved] == ["merging", "merging", "merging", "merged"]
    assert saved[2].merge_commit == first.workspace.merge_commit  # recorded before the branch moves
    second = merge(two, "s/2 two")
    assert sorted(first.files) == ["one-more.txt", "one.txt"] and sorted(second.files) == ["shared.txt", "two.txt"]

    x1, x2 = first.workspace.merge_commit, second.workspace.merge_commit
    assert git(repo, "rev-parse", "HEAD") == x2
    assert parents(repo, x2) == [x1, second.workspace.result_commit]
    assert parents(repo, x1) == [base, first.workspace.result_commit]
    assert git(repo, "log", "--first-parent", "--format=%s", "-3").splitlines() == [
        "looper: merge step s/2 two: do s/2 two", "looper: merge step s1: do s1",
        "looper: checkpoint before parallel steps"]
    assert git(repo, "log", "--format=%an %s", f"{base}..{first.workspace.result_commit}").splitlines() == [
        "Looper looper: commit what step s1 left uncommitted", "User worker: one.txt"]
    assert git(repo, "log", "-1", "--format=%(trailers:key=Looper-Step,valueonly)", x2) == "s/2 two"
    assert (repo / "one.txt").exists() and (repo / "two.txt").exists()
    assert (repo / "shared.txt").read_text() == "a\nb\nc2\n"
    # The user's work is as it was.
    assert git(repo, "status", "--porcelain", "--untracked-files=all").splitlines() == ["A  staged.txt",
                                                                                       "?? notes.txt"]
    assert git(repo, "reflog", "-1", "--format=%gs").startswith("looper: merge step s/2 two")


def test_a_step_that_changed_nothing_leaves_no_commit(repo: Path, tmp_path: Path) -> None:
    head = git(repo, "rev-parse", "HEAD")
    quiet = workspace(inspected(repo), tmp_path / "runtime", 1, "s1", head)
    outcome = merge(quiet, "s1")
    assert (outcome.workspace.state, outcome.workspace.merge_commit, outcome.files) == (
        StepWorkspaceState.MERGED, None, ())
    assert git(repo, "rev-parse", "HEAD") == head


def _conflict(repo: Path, root: Path) -> StepWorkspace:
    head = git(repo, "rev-parse", "HEAD")
    first = workspace(inspected(repo), root, 1, "s1", head)
    second = workspace(inspected(repo), root, 2, "s2", head)
    (Path(first.path) / "shared.txt").write_text("a\nfrom s1\nc\n")
    (Path(second.path) / "shared.txt").write_text("a\nfrom s2\nc\n")
    assert merge(first, "s1").workspace.state is StepWorkspaceState.MERGED
    return second


def _local_change(repo: Path, root: Path) -> StepWorkspace:
    step = workspace(inspected(repo), root, 1, "s1", git(repo, "rev-parse", "HEAD"))
    (Path(step.path) / "base.txt").write_text("from the step\n")
    (repo / "base.txt").write_text("the user's edit\n")
    return step


def _untracked_in_the_way(repo: Path, root: Path) -> StepWorkspace:
    step = workspace(inspected(repo), root, 1, "s1", git(repo, "rev-parse", "HEAD"))
    (Path(step.path) / "new.txt").write_text("from the step\n")
    (repo / "new.txt").write_text("the user's file\n")
    return step


def _switched(repo: Path, root: Path) -> StepWorkspace:
    step = workspace(inspected(repo), root, 1, "s1", git(repo, "rev-parse", "HEAD"))
    (Path(step.path) / "one.txt").write_text("one\n")
    git(repo, "switch", "-q", "-c", "elsewhere")
    return step


def _reset(repo: Path, root: Path) -> StepWorkspace:
    (repo / "base.txt").write_text("checkpointed\n")
    base, _ = run(ws.checkpoint(inspected(repo), task_id="task-1"))
    step = workspace(inspected(repo), root, 1, "s1", base, checkpointed=True)
    (Path(step.path) / "one.txt").write_text("one\n")
    git(repo, "reset", "-q", "--hard", f"{base}^")  # the user drops the checkpoint
    return step


def _step_switched(repo: Path, root: Path) -> StepWorkspace:
    step = workspace(inspected(repo), root, 1, "s1", git(repo, "rev-parse", "HEAD"))
    git(step.path, "switch", "-q", "-c", "mine")
    return step


def _step_rewritten(repo: Path, root: Path) -> StepWorkspace:
    step = workspace(inspected(repo), root, 1, "s1", git(repo, "rev-parse", "HEAD"))
    git(step.path, "commit", "-q", "--amend", "-m", "rewritten", "--allow-empty")
    return step


def _read_only(repo: Path, root: Path) -> StepWorkspace:
    step = workspace(inspected(repo), root, 1, "s1", git(repo, "rev-parse", "HEAD"))
    (Path(step.path) / "one.txt").write_text("one\n")
    git(step.path, "add", "-A")
    git(step.path, "commit", "-qm", "worker: one")
    subprocess.run(["chmod", "-R", "a-w", str(repo / ".git" / "objects")], check=True)  # git cannot write
    return step


@pytest.mark.parametrize(("arrange", "error", "files", "detail"), [
    (_conflict, MergeError.CONFLICT, ("shared.txt",), "conflicts with main in shared.txt"),
    (_local_change, MergeError.LOCAL_CHANGES, ("base.txt",), "in the way: base.txt"),
    (_untracked_in_the_way, MergeError.LOCAL_CHANGES, ("new.txt",), "in the way: new.txt"),
    (_switched, MergeError.BRANCH_CHANGED, (), "the task directory is on elsewhere, not main"),
    (_reset, MergeError.BRANCH_CHANGED, (), "main no longer contains the step's start"),
    (_step_switched, MergeError.STEP_BRANCH_CHANGED, (), "no longer on its branch looper/task-1/s1-1"),
    (_step_rewritten, MergeError.STEP_BRANCH_CHANGED, (), "its commits were rewritten"),
    (_read_only, MergeError.GIT_FAILED, (), "insufficient permission"),
])
def test_a_merge_that_cannot_be_made_leaves_the_task_directory_as_it_was(
    repo: Path, tmp_path: Path, arrange: Any, error: MergeError, files: tuple[str, ...], detail: str,
) -> None:
    step = arrange(repo, tmp_path / "runtime")
    before = snapshot(repo)
    try:
        outcome = merge(step, step.branch.rsplit("/", 1)[1].split("-")[0])
    finally:
        subprocess.run(["chmod", "-R", "u+w", str(repo / ".git" / "objects")], check=True)
    assert snapshot(repo) == before
    assert (outcome.workspace.state, outcome.workspace.merge_error, outcome.files) == (
        StepWorkspaceState.UNMERGED, error, files)
    assert outcome.workspace.merge_files == files  # kept on the workspace for the task page
    assert detail in outcome.workspace.detail


def test_a_branch_that_moves_meanwhile_is_merged_on_its_new_tip(repo: Path, tmp_path: Path) -> None:
    step = workspace(inspected(repo), tmp_path / "runtime", 1, "s1", git(repo, "rev-parse", "HEAD"))
    (Path(step.path) / "one.txt").write_text("one\n")
    moves = 0

    async def save(saved: StepWorkspace) -> None:
        nonlocal moves
        if saved.merge_commit and moves < 1:  # the user commits between the merge and the fast-forward
            moves += 1
            git(repo, "commit", "-q", "--allow-empty", "-m", "the user's commit")

    outcome = run(ws.merge(step, step_key="s1", title="do s1", save=save))
    assert outcome.workspace.state is StepWorkspaceState.MERGED
    assert git(repo, "log", "--first-parent", "--format=%s", "-2").splitlines() == [
        "looper: merge step s1: do s1", "the user's commit"]

    again = workspace(inspected(repo), tmp_path / "runtime", 2, "s2", git(repo, "rev-parse", "HEAD"))
    (Path(again.path) / "two.txt").write_text("two\n")

    async def always_moving(saved: StepWorkspace) -> None:
        if saved.merge_commit and saved.state is StepWorkspaceState.MERGING:
            git(repo, "commit", "-q", "--allow-empty", "-m", "busy")

    kept_moving = run(ws.merge(again, step_key="s2", title="do s2", save=always_moving))
    assert (kept_moving.workspace.merge_error, kept_moving.workspace.detail) == (
        MergeError.BRANCH_CHANGED, "main kept moving while the step was merged")
    assert not (repo / "two.txt").exists()


def test_a_merge_cut_off_by_a_restart_is_done_once(repo: Path, tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    head = git(repo, "rev-parse", "HEAD")
    # Cut off after the merge commit was recorded, before the branch moved.
    step = workspace(inspected(repo), root, 1, "s1", head)
    (Path(step.path) / "one.txt").write_text("one\n")
    saved = Saved()

    class Stop(Exception):
        pass

    async def stop_before_moving(state: StepWorkspace) -> None:
        saved.append(state)
        if state.merge_commit:
            raise Stop

    with pytest.raises(Stop):
        run(ws.merge(step, step_key="s1", title="do s1", save=stop_before_moving))
    assert git(repo, "rev-parse", "HEAD") == head
    done = merge(saved[-1], "s1")
    assert done.workspace.state is StepWorkspaceState.MERGED
    assert git(repo, "rev-list", "--count", f"{head}..HEAD") == "2"  # the leftovers commit, once, and one merge

    # Cut off after the branch moved, before the workspace was saved as merged.
    again = workspace(inspected(repo), root, 2, "s2", git(repo, "rev-parse", "HEAD"))
    (Path(again.path) / "two.txt").write_text("two\n")
    states = Saved()
    merge(again, "s2", states)
    tip = git(repo, "rev-parse", "HEAD")
    repeated = merge(states[-2], "s2")  # merging, with the merge commit that landed
    assert (repeated.workspace.state, repeated.files, git(repo, "rev-parse", "HEAD")) == (
        StepWorkspaceState.MERGED, (), tip)


def shell(cwd: Path, command: str) -> str:
    """A command the task page shows, run as the user would paste it (no hooks, no signing, in this test)."""
    env = {**os.environ, "GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": "/dev/null",
           "GIT_CONFIG_KEY_1": "commit.gpgSign", "GIT_CONFIG_VALUE_1": "false", "GIT_EDITOR": "true"}
    return subprocess.run(command, shell=True, cwd=cwd, env=env, capture_output=True, text=True,
                          check=True).stdout.rstrip()


def _two_merged_steps(repo: Path, root: Path) -> tuple[str, list[StepWorkspace], list[dict[str, str]]]:
    """A task on the repository's branch: a checkpoint and two merged steps; where it started, the steps and the
    undo commands the task page shows for them."""
    start = git(repo, "rev-parse", "HEAD")
    branch = git(repo, "symbolic-ref", "HEAD")
    (repo / "base.txt").write_text("serial\n")
    base, _ = run(ws.checkpoint(inspected(repo), task_id="task-1"))
    one, two = (workspace(inspected(repo), root, i, f"s{i}", base, checkpointed=True) for i in (1, 2))
    (Path(one.path) / "one.txt").write_text("one\n")
    (Path(two.path) / "two.txt").write_text("two\n")
    merged = [merge(one, "s1").workspace, merge(two, "s2").workspace]
    commands = ws.undo_commands("task-1", start_branch=branch, start_commit=start,
                                merged=[(f"s{i}", str(w.merge_commit)) for i, w in enumerate(merged, start=1)])
    return start, merged, commands


def test_the_undo_commands_undo_a_step_or_the_whole_task(repo: Path, tmp_path: Path) -> None:
    start, (first, second), commands = _two_merged_steps(repo, tmp_path / "runtime")
    command = {c["what"]: c["command"] for c in commands}
    assert list(command) == [
        "List the task's commits on main", "Undo step s1", "Undo step s2",
        "Undo the whole task, keeping its changes as uncommitted changes",
        "Undo the whole task and drop its changes (and any other uncommitted change)",
        "Move the task's commits to a new branch and put main back where the task started"]
    assert command["Undo step s2"] == f"git switch main && git revert -m 1 {second.merge_commit}"

    listed = shell(repo, command["List the task's commits on main"] + " --format=%s").splitlines()
    assert listed == ["looper: merge step s2: do s2", "looper: merge step s1: do s1",
                      "looper: checkpoint before parallel steps"]
    shell(repo, command["Undo step s2"] + " --no-edit")  # only that step
    assert (repo / "one.txt").exists() and not (repo / "two.txt").exists()
    git(repo, "reset", "-q", "--hard", "HEAD~1")

    shell(repo, command["Move the task's commits to a new branch and put main back where the task started"])
    assert git(repo, "symbolic-ref", "--short", "HEAD") == "main-task-1"
    assert git(repo, "rev-parse", "main") == start and git(repo, "rev-parse", "HEAD") == second.merge_commit
    git(repo, "switch", "-q", "main")
    git(repo, "merge", "-q", "--ff-only", "--no-verify-signatures", "main-task-1")

    shell(repo, command["Undo the whole task, keeping its changes as uncommitted changes"])
    assert git(repo, "rev-parse", "HEAD") == start
    assert git(repo, "status", "--porcelain", "--untracked-files=all").splitlines() == [
        " M base.txt", "?? one.txt", "?? two.txt"]
    git(repo, "reset", "-q", "--hard", str(second.merge_commit))  # the task's commits, back
    shell(repo, command["Undo the whole task and drop its changes (and any other uncommitted change)"])
    assert git(repo, "rev-parse", "HEAD") == start
    assert git(repo, "status", "--porcelain", "--untracked-files=all") == ""
    assert (repo / "base.txt").read_text() == "base\n"


def test_the_undo_commands_quote_a_branch_the_shell_would_split(tmp_path: Path) -> None:
    path = tmp_path / "odd"
    path.mkdir()
    git(path, "init", "-q", "-b", "fix;$HOME'x")
    git(path, "config", "user.name", "User")
    git(path, "config", "user.email", "user@example.com")
    (path / "shared.txt").write_text("a\n")
    git(path, "add", "-A")
    git(path, "commit", "-qm", "init")
    start, _, commands = _two_merged_steps(path, tmp_path / "runtime")
    assert len(shell(path, commands[0]["command"] + " --oneline").splitlines()) == 3
    shell(path, commands[-1]["command"])
    assert git(path, "rev-parse", "refs/heads/fix;$HOME'x") == start
    assert git(path, "symbolic-ref", "--short", "HEAD") == "fix;$HOME'x-task-1"
    assert shell(path, commands[0]["command"] + " --oneline") == ""  # the branch is back where the task started


def test_without_its_start_only_the_list_and_the_steps_can_be_undone() -> None:
    commands = ws.undo_commands("task-1", start_branch="refs/heads/main", start_commit="", merged=[("s1", "abc")])
    assert [c["command"] for c in commands] == ["git log --first-parent --grep='^Looper-Task: task-1$' main",
                                                "git switch main && git revert -m 1 abc"]


def test_the_undo_commands_change_the_task_s_branch_whatever_is_checked_out(repo: Path, tmp_path: Path) -> None:
    start, (_, second), commands = _two_merged_steps(repo, tmp_path / "runtime")
    command = {c["what"]: c["command"] for c in commands}
    git(repo, "switch", "-q", "-c", "feature")
    git(repo, "commit", "-q", "--allow-empty", "-m", "the user's own work")
    feature = git(repo, "rev-parse", "feature")

    shell(repo, command["Move the task's commits to a new branch and put main back where the task started"])
    assert git(repo, "rev-parse", "main-task-1") == second.merge_commit  # from main, not from feature
    assert git(repo, "rev-parse", "main") == start and git(repo, "rev-parse", "feature") == feature
    git(repo, "branch", "-f", "main", str(second.merge_commit))
    git(repo, "switch", "-q", "feature")
    shell(repo, command["Undo the whole task, keeping its changes as uncommitted changes"])
    assert git(repo, "symbolic-ref", "--short", "HEAD") == "main"
    assert (git(repo, "rev-parse", "main"), git(repo, "rev-parse", "feature")) == (start, feature)


# --- Worktrees lost and removed ---


def test_a_lost_worktree_comes_back_from_its_branch(repo: Path, tmp_path: Path) -> None:
    step = workspace(inspected(repo), tmp_path / "runtime", 1, "s1", git(repo, "rev-parse", "HEAD"))
    (Path(step.path) / "one.txt").write_text("one\n")
    git(step.path, "add", "-A")
    git(step.path, "commit", "-qm", "worker: one")
    subprocess.run(["rm", "-rf", step.path], check=True)
    run(ws.ensure_worktree(step))
    assert (Path(step.path) / "one.txt").read_text() == "one\n"
    assert git(step.path, "symbolic-ref", "HEAD") == "refs/heads/looper/task-1/s1-1"


def test_removing_a_workspace_deletes_its_directory_and_its_branch(repo: Path, tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    head = git(repo, "rev-parse", "HEAD")
    kept = run(ws.remove(workspace(inspected(repo), root, 1, "s1", head), keep_branch=True))
    assert kept.removed_at is None and not Path(kept.path).exists()
    assert git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/looper/") == "looper/task-1/s1-1"
    gone = run(ws.remove(kept))
    assert gone.removed_at is not None and git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/looper/") == ""
    assert git(repo, "worktree", "list", "--porcelain").count("worktree ") == 1
    assert not (root / "worktrees" / "task-1").exists()


def test_the_startup_sweep_keeps_what_tasks_keep(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import step_workspaces as db

    root = tmp_path / "runtime"
    head = git(repo, "rev-parse", "HEAD")
    done = workspace(inspected(repo), root, 1, "s1", head)  # its task completed
    kept = workspace(inspected(repo), root, 2, "s2", head)  # unmerged, its task stopped
    merged = workspace(inspected(repo), root, 3, "s3", head).moved(StepWorkspaceState.MERGING).moved(
        StepWorkspaceState.MERGED)  # merged, not removed yet
    orphan = workspace(inspected(repo), root, 4, "s4", head)  # no row
    kept = kept.moved(StepWorkspaceState.MERGING).moved(StepWorkspaceState.UNMERGED,
                                                        merge_error=MergeError.CONFLICT, detail="x")
    rows = [{**done.row(), "task_status": "completed"}, {**kept.row(), "task_status": "stopped"},
            {**merged.row(), "task_status": "running"}]
    written: list[dict[str, Any]] = []

    async def list_kept(**_: Any) -> list[dict[str, Any]]:
        return rows

    async def save(row: Any, **_: Any) -> None:
        written.append(dict(row))

    monkeypatch.setattr(db, "list_kept", list_kept)
    monkeypatch.setattr(db, "save", save)
    assert run(ws.sweep(root)) == 3
    assert [(w["id"], w["state"], w["removed_at"] is not None) for w in written] == [
        (1, "discarded", True), (3, "merged", True)]
    assert not Path(done.path).exists() and not Path(merged.path).exists() and not Path(orphan.path).exists()
    assert Path(kept.path).exists()
    assert git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/looper/") == "looper/task-1/s2-2"


# --- Links back to the task directory ---


def _local_state(repo: Path) -> None:
    """Ignored local state in the task directory: a dependencies directory under a directory-only rule (which does
    not match a link) and a virtualenv and an env file under plain rules."""
    (repo / ".gitignore").write_text(".env\n.venv\nnode_modules/\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "ignore local state")
    (repo / "node_modules" / "pkg").mkdir(parents=True)
    (repo / "node_modules" / "pkg" / "index.js").write_text("module\n")
    (repo / ".venv").mkdir()
    (repo / ".venv" / "pyvenv.cfg").write_text("home = /usr\n")
    (repo / ".env").write_text("TOKEN=local\n")


def test_a_worktree_links_the_listed_paths_it_does_not_have(repo: Path, tmp_path: Path) -> None:
    _local_state(repo)
    (repo / "lib").symlink_to(tmp_path)  # a tracked link out of the repository
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "lib")
    step = workspace(inspected(repo), tmp_path / "runtime", 1, "s1", git(repo, "rev-parse", "HEAD"))
    linked, skipped = ws.link(step, (".venv", "node_modules", ".env", "missing", "base.txt", "lib/x"))
    assert linked.links == (".venv", "node_modules", ".env")
    assert skipped == ["missing: the task directory has none", "base.txt: the repository tracks it",
                       "lib/x: the task directory has none"]
    for path in linked.links:
        assert os.readlink(Path(step.path) / path) == str(repo / path)
    assert (Path(step.path) / ".venv" / "pyvenv.cfg").read_text() == "home = /usr\n"
    assert (Path(step.path) / "base.txt").read_text() == "base\n"  # a checked-out file is never replaced

    (tmp_path / "x").write_text("outside\n")
    (repo / "lib").unlink()
    (repo / "lib").mkdir()  # the task directory has lib/x; the worktree's lib leads out of it
    (repo / "lib" / "x").write_text("in the task directory\n")
    assert ws.link(step, ("lib/x",))[1] == ["lib/x: its directory is outside the worktree"]


def test_a_task_directory_inside_the_repository_links_from_its_place(repo: Path, tmp_path: Path) -> None:
    (repo / "app").mkdir()
    (repo / "app" / "main.txt").write_text("app\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "app")
    (repo / "app" / ".env").write_text("TOKEN=app\n")
    step = workspace(inspected(repo / "app"), tmp_path / "runtime", 1, "s1", git(repo, "rev-parse", "HEAD"))
    linked, _ = ws.link(step, (".env", "deps/cache"))
    assert linked.links == (".env",)
    assert os.readlink(Path(step.work_dir) / ".env") == str(repo / "app" / ".env")


def test_a_link_is_never_part_of_the_step_s_result(repo: Path, tmp_path: Path) -> None:
    _local_state(repo)
    root = tmp_path / "runtime"
    step, _ = ws.link(workspace(inspected(repo), root, 1, "s1", git(repo, "rev-parse", "HEAD")),
                      (".venv", "node_modules", ".env"))
    (Path(step.path) / "one.txt").write_text("one\n")
    git(step.path, "add", "-A")  # node_modules/ does not match the link: the Worker commits it
    git(step.path, "commit", "-qm", "worker: one")
    assert "node_modules" in git(step.path, "ls-tree", "--name-only", "HEAD").split()
    (Path(step.path) / "two.txt").write_text("two\n")
    outcome = merge(step, "s1")
    assert outcome.workspace.state is StepWorkspaceState.MERGED
    assert git(repo, "ls-tree", "--name-only", "HEAD").split() == [".gitignore", "base.txt", "one.txt", "shared.txt",
                                                                   "two.txt"]
    # The task directory's local state is as it was, not a link to itself.
    assert (repo / "node_modules").is_dir() and not (repo / "node_modules").is_symlink()
    assert (repo / "node_modules" / "pkg" / "index.js").read_text() == "module\n"
    assert (repo / ".venv" / "pyvenv.cfg").exists() and (repo / ".env").read_text() == "TOKEN=local\n"

    # A step that only has its links changed nothing.
    quiet, _ = ws.link(workspace(inspected(repo), root, 2, "s2", git(repo, "rev-parse", "HEAD")), (".venv", "node_modules"))
    head = git(repo, "rev-parse", "HEAD")
    done = merge(quiet, "s2")
    assert (done.workspace.state, done.leftovers, git(repo, "rev-parse", "HEAD")) == (
        StepWorkspaceState.MERGED, None, head)


def test_removing_a_worktree_with_links_leaves_what_they_point_to(repo: Path, tmp_path: Path) -> None:
    _local_state(repo)
    step, _ = ws.link(workspace(inspected(repo), tmp_path / "runtime", 1, "s1", git(repo, "rev-parse", "HEAD")),
                      (".venv", "node_modules"))
    removed = run(ws.remove(step))
    assert removed.removed_at is not None and not Path(step.path).exists()
    assert (repo / "node_modules" / "pkg" / "index.js").exists() and (repo / ".venv" / "pyvenv.cfg").exists()


def test_a_lost_worktree_gets_its_links_back(repo: Path, tmp_path: Path) -> None:
    _local_state(repo)
    step, _ = ws.link(workspace(inspected(repo), tmp_path / "runtime", 1, "s1", git(repo, "rev-parse", "HEAD")),
                      (".venv",))
    subprocess.run(["rm", "-rf", step.path], check=True)  # rm -rf does not follow the link
    assert (repo / ".venv" / "pyvenv.cfg").exists()
    run(ws.ensure_worktree(step))
    assert os.readlink(Path(step.path) / ".venv") == str(repo / ".venv")


# --- The checkout size limit ---


def test_a_checkout_over_the_size_limit_has_no_workspaces(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    size = run(ws.checkout_size(str(repo)))
    assert size == sum(len(p.read_bytes()) for p in repo.iterdir() if p.is_file())
    monkeypatch.setattr(ws, "CHECKOUT_LIMIT_BYTES", size)
    assert isinstance(run(ws.inspect(str(repo))), Repo)  # at the limit
    (repo / "more.txt").write_text("x")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "one byte more")
    assert run(ws.inspect(str(repo))) == Unusable(
        f"the checkout is {size + 1} B, over the {size} B limit for step worktrees", repository=True)
    monkeypatch.setattr(ws, "CHECKOUT_LIMIT_BYTES", 1 << 30)
    assert isinstance(run(ws.inspect(str(repo))), Repo)


@pytest.mark.parametrize(("size", "text"), [(0, "0 B"), (1023, "1023 B"), (1 << 20, "1 MiB"),
                                           (int(1.5 * (1 << 30)), "1.5 GiB")])
def test_a_checkout_size_reads_in_binary_units(size: int, text: str) -> None:
    assert ws._size_text(size) == text


# --- Discarding one, and the row ---


def test_the_user_discards_a_workspace_a_stopped_task_keeps(repo: Path, tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    head = git(repo, "rev-parse", "HEAD")
    unmerged = merge(_conflict(repo, tmp_path / "other"), "s2").workspace  # workspaces 1 and 2
    active = workspace(inspected(repo), root, 5, "s5", head)
    kept_branch = run(ws.remove(workspace(inspected(repo), root, 3, "s3", head).moved(StepWorkspaceState.DISCARDED),
                                keep_branch=True))
    for found in (active, unmerged, kept_branch):
        assert found.discardable and run(ws.branch_exists(found.repo_dir, found.branch))
        gone = run(ws.discard(found))
        assert (gone.state, gone.removed_at is not None, gone.discardable) == (StepWorkspaceState.DISCARDED, True, False)
        assert not Path(found.path).exists() and not run(ws.branch_exists(found.repo_dir, found.branch))
        with pytest.raises(IllegalTransition, match="is gone"):
            run(ws.discard(gone))
    merged = merge(workspace(inspected(repo), root, 4, "s4", git(repo, "rev-parse", "HEAD")), "s4").workspace
    with pytest.raises(IllegalTransition, match="is merged"):
        run(ws.discard(merged))
    assert not run(ws.branch_exists(str(tmp_path / "nowhere"), "looper/task-1/s1-1"))


def test_a_workspace_row_reads_back_as_it_was_written() -> None:
    written = StepWorkspace(id=7, task_id="t", step_id="s", repo_dir="/r", subdir="app", target_branch="refs/heads/main",
                            path="/w", branch="looper/t/s-7", base_commit="abc", checkpoint=True, result_commit="def",
                            state=StepWorkspaceState.UNMERGED, merge_error=MergeError.CONFLICT, detail="conflicts",
                            links=(".venv", ".env"), merge_files=("a.py",))
    assert StepWorkspace.from_row(written.row()) == written
    assert written.view() == {
        "id": 7, "step_id": "s", "branch": "looper/t/s-7", "target_branch": "main", "base_commit": "abc",
        "checkpoint": True, "result_commit": "def", "merge_commit": None, "state": "unmerged",
        "merge_error": "conflict", "merge_files": ["a.py"], "detail": "conflicts", "links": [".venv", ".env"],
        "path": "/w", "removed_at": None, "discardable": True}
    view = ws.task_view("t", [written.row()], start_branch="refs/heads/main", start_commit="r0",
                        step_keys={"s": "s2"})
    assert view["start"] == {"branch": "main", "commit": "r0"}
    assert view["workspaces"] == [{**written.view(), "step_key": "s2"}]
    assert [c["command"] for c in view["undo"]][0] == "git log --first-parent --grep='^Looper-Task: t$' main"


def test_a_link_the_worker_replaced_with_its_own_file_is_its_change(repo: Path, tmp_path: Path) -> None:
    _local_state(repo)
    step, _ = ws.link(workspace(inspected(repo), tmp_path / "runtime", 1, "s1", git(repo, "rev-parse", "HEAD")),
                      (".env", "node_modules"))
    (Path(step.path) / ".env").unlink()
    (Path(step.path) / ".env").write_text("TOKEN=example\n")
    git(step.path, "add", "-f", ".env")  # the step's own example file, deliberately committed
    git(step.path, "commit", "-qm", "worker: .env example")
    outcome = merge(step, "s1")
    assert (outcome.workspace.state, outcome.replaced_links) == (StepWorkspaceState.MERGED, (".env",))
    assert git(repo, "show", "HEAD:.env") == "TOKEN=example"
    assert "node_modules" not in git(repo, "ls-tree", "--name-only", "HEAD").split()


def test_looper_forgets_only_its_own_worktrees(repo: Path, tmp_path: Path) -> None:
    """``git worktree prune`` would also drop the record of the user's worktree whose directory is gone."""
    users = tmp_path / "users-worktree"
    git(repo, "worktree", "add", "-q", "-b", "mine", str(users), "HEAD")
    subprocess.run(["rm", "-rf", str(users)], check=True)  # an unmounted disk, say
    root = tmp_path / "runtime"
    lost = workspace(inspected(repo), root, 1, "s1", git(repo, "rev-parse", "HEAD"))
    subprocess.run(["rm", "-rf", lost.path], check=True)
    run(ws.ensure_worktree(lost))  # its own stale record goes, so it comes back
    run(ws.remove(lost))
    run(ws.remove_orphan(Path(workspace(inspected(repo), root, 2, "s2", git(repo, "rev-parse", "HEAD")).path)))
    listed = git(repo, "worktree", "list", "--porcelain")
    assert f"worktree {users.resolve()}" in listed or f"worktree {users}" in listed
    assert "looper/" not in listed and str(root) not in listed
