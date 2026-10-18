"""Task workspaces in real git repositories: how a task runs, its worktree and branch, what it warns about.

Every repository signs commits with a gpg that always fails and has every
hook write to a marker file (failing the pre-commit ones): none of that may
touch what Looper does. The two tables (``task_worktrees``, the task's
``work_dir``) and the list of tasks in progress are in-memory stand-ins.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from looper.agents import step_workspaces as ws
from looper.agents import task_workspaces as tw
from looper.agents.project_execution import TaskWorkspaces
from looper.agents.task_workspaces import TaskWorkspaceError, WarningKind, WorktreeState, resolve_mode

HOOKS = ("pre-commit", "commit-msg", "post-commit", "post-checkout", "post-merge", "reference-transaction")
IN_PLACE, WORKTREE = TaskWorkspaces.IN_PLACE, TaskWorkspaces.WORKTREE


def git(cwd: Path | str, *args: str) -> str:
    """A git command of the user's (no hooks, no signing)."""
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgSign=false", *args], cwd=cwd,
                          capture_output=True, text=True, check=True).stdout.rstrip()


def run(coro: Any) -> Any:
    return asyncio.run(coro)


class Store:
    def __init__(self) -> None:
        self.worktrees: dict[str, dict[str, Any]] = {}
        self.work_dirs: dict[str, str] = {}
        self.active: list[dict[str, Any]] = []
        self.notes: list[tuple[str, str]] = []

    async def save(self, row: Any, **_: Any) -> None:
        self.worktrees[row["task_id"]] = dict(row)

    async def get(self, task_id: str, **_: Any) -> dict[str, Any] | None:
        row = self.worktrees.get(task_id)
        return dict(row) if row else None

    async def list_active(self, **_: Any) -> list[dict[str, Any]]:
        return [{**row, "task_status": "completed"} for row in self.worktrees.values() if row["state"] == "active"]

    async def set_work_dir(self, task_id: str, work_dir: str, **_: Any) -> None:
        self.work_dirs[task_id] = work_dir

    async def active_workspaces(self, *, besides: str | None = None, **_: Any) -> list[dict[str, Any]]:
        return [task for task in self.active if task["id"] != besides]

    async def note(self, kind: str, text: str) -> None:
        self.notes.append((kind, text))


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> Store:
    from looper.db import task_worktrees, tasks

    fake = Store()
    for name in ("save", "get", "list_active"):
        monkeypatch.setattr(task_worktrees, name, getattr(fake, name))
    monkeypatch.setattr(tasks, "set_work_dir", fake.set_work_dir)
    monkeypatch.setattr(tasks, "active_workspaces", fake.active_workspaces)
    return fake


@pytest.fixture(autouse=True)
def _git_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("LOOPER_OPERATOR_NOTES_ROOT", str(tmp_path / "notes"))
    yield
    marks = tmp_path / "hook-marks"
    assert not marks.exists(), f"hooks ran: {marks.read_text()}"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """The user's checkout on main, with a subdirectory, ignored local state, signing that fails and hooks."""
    path = tmp_path / "workspace"
    (path / "app").mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    for key, value in (("user.name", "User"), ("user.email", "user@example.com"), ("commit.gpgSign", "true"),
                       ("gpg.program", "false")):
        git(path, "config", key, value)
    (path / ".gitignore").write_text(".env\nnode_modules/\n")
    (path / "app" / "main.py").write_text("print('hi')\n")
    git(path, "add", "-A")
    git(path, "commit", "-qm", "init")
    git(path, "tag", "v1")
    (path / ".env").write_text("TOKEN=local\n")
    for hook in HOOKS:
        script = path / ".git" / "hooks" / hook
        fail = "exit 1" if hook in ("pre-commit", "commit-msg") else "exit 0"
        script.write_text(f"#!/bin/sh\necho {hook} >> {tmp_path / 'hook-marks'}\n{fail}\n")
        script.chmod(0o755)
    return path


def task(task_id: str, workspace: Path, *, mode: TaskWorkspaces = WORKTREE, base: str = "") -> dict[str, Any]:
    return {"id": task_id, "work_dir": str(workspace), "workspace_dir": str(workspace), "workspace_mode": mode.value,
            "workspace_base": base}


def prepare(store: Store, row: dict[str, Any], root: Path, links: tuple[str, ...] = ()) -> str:
    return run(tw.prepare(row, runtime_root=root, links=links, note=store.note))


def user_view(path: Path) -> tuple[str, ...]:
    """The user's checkout as they see it: its branch and commit, the index and the status."""
    return (git(path, "symbolic-ref", "HEAD"), git(path, "rev-parse", "HEAD"), git(path, "ls-files", "-s"),
            git(path, "status", "--porcelain", "--untracked-files=all"))


# --- How a task runs ---


@pytest.mark.parametrize(("requested", "named", "project_dir", "project_mode", "mode"), [
    ("in_place", "/w", "/p", WORKTREE, IN_PLACE),  # as asked
    ("worktree", "", "/p", IN_PLACE, WORKTREE),
    (None, "/elsewhere", "/p", IN_PLACE, WORKTREE),  # it names a workspace of its own
    (None, "/p", "/p", IN_PLACE, IN_PLACE),  # it names its project's directory again: as the project says
    (None, "", "/p", IN_PLACE, IN_PLACE),
    (None, "", "/p", WORKTREE, WORKTREE),
    (None, "/x", "", IN_PLACE, WORKTREE),  # a project without a directory
])
def test_a_task_runs_as_asked_else_in_a_worktree_of_a_workspace_of_its_own_else_as_its_project_says(
    requested: str | None, named: str, project_dir: str, project_mode: TaskWorkspaces, mode: TaskWorkspaces,
) -> None:
    assert resolve_mode(requested=requested, named=named, project_dir=project_dir, project_mode=project_mode) is mode


def test_an_unknown_mode_is_refused() -> None:
    with pytest.raises(ValueError, match="workspace_mode must be one of in_place, worktree"):
        resolve_mode(requested="copy", named="", project_dir="/p", project_mode=IN_PLACE)


# --- What a task is told before it starts ---


def test_the_check_warns_of_what_a_worktree_leaves_out_and_never_refuses(repo: Path, store: Store,
                                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    (repo / "app" / "main.py").write_text("print('changed')\n")
    (repo / "app" / "new.py").write_text("x = 1\n")
    found = run(tw.check(str(repo / "app"), mode=WORKTREE))
    assert (found["mode"], found["git"]["branch"], found["git"]["base_commit"]) == ("worktree", "main",
                                                                                    git(repo, "rev-parse", "HEAD"))
    [uncommitted] = found["warnings"]
    assert uncommitted["kind"] == WarningKind.UNCOMMITTED.value and uncommitted["files"] == ["app/main.py", "app/new.py"]
    assert run(tw.check(str(repo / "app"), mode=IN_PLACE))["warnings"] == []  # in place they are just there

    monkeypatch.setattr(ws, "CHECKOUT_LIMIT_BYTES", 10)
    assert WarningKind.LARGE_CHECKOUT.value in {w["kind"] for w in run(tw.check(str(repo), mode=WORKTREE))["warnings"]}
    assert run(tw.check(str(repo), mode=WORKTREE, base="v1"))["git"]["base_commit"] == git(repo, "rev-parse", "v1")
    with pytest.raises(TaskWorkspaceError, match="'nope' is not a branch, tag or commit"):
        run(tw.check(str(repo), mode=WORKTREE, base="nope"))


def test_outside_git_a_worktree_task_runs_in_place(tmp_path: Path, store: Store) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    found = run(tw.check(str(plain), mode=WORKTREE))
    assert (found["mode"], found["git"], [w["kind"] for w in found["warnings"]]) == ("in_place", None, ["not_git"])
    # The project's default worktree falling back to in place is warned of too, in softer words.
    default = run(tw.check(str(plain), mode=WORKTREE, asked=False))
    assert [w["kind"] for w in default["warnings"]] == ["not_git"] and default["mode"] == "in_place"
    assert default["warnings"][0]["text"] != found["warnings"][0]["text"] and "by default" in default["warnings"][0]["text"]
    assert run(tw.check(str(plain), mode=IN_PLACE, asked=False))["warnings"] == []  # in place there is nothing to say
    assert prepare(store, task("task-p", plain), tmp_path / "rt") == str(plain)
    assert store.worktrees == {} and "not in a git repository" in store.notes[-1][1]


@pytest.mark.parametrize("mode", [WORKTREE, IN_PLACE])
def test_other_tasks_on_the_same_repository_are_named(repo: Path, store: Store, tmp_path: Path,
                                                      mode: TaskWorkspaces) -> None:
    git(repo, "worktree", "add", "-q", "-b", "side", str(tmp_path / "side"))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    store.active = [{"id": "task-a", "name": "fix login", "status": "running", "workspace": str(tmp_path / "side")},
                    {"id": "task-b", "name": "other", "status": "running", "workspace": str(elsewhere)}]
    [shared] = run(tw.check(str(repo / "app"), mode=mode, besides="task-new"))["warnings"]
    assert shared["kind"] == "shared" and [t["task_id"] for t in shared["tasks"]] == ["task-a"]
    assert ("branch of its own" in shared["text"]) == (mode is WORKTREE)
    assert ("overwrite" in shared["text"]) == (mode is IN_PLACE)
    assert run(tw.check(str(repo), mode=mode, besides="task-a"))["warnings"] == []


def test_a_task_in_place_beside_one_in_a_worktree_is_no_overwrite_risk(repo: Path, store: Store) -> None:
    store.active = [{"id": "task-a", "name": "fix login", "status": "running", "workspace": str(repo),
                     "workspace_mode": "worktree"}]
    [shared] = run(tw.check(str(repo), mode=IN_PLACE))["warnings"]
    assert shared["tasks"][0]["mode"] == "worktree" and "overwrite" not in shared["text"]
    assert "may conflict when you merge" in shared["text"]


# --- The worktree, from the first start to the end ---


def test_a_worktree_task_runs_on_a_branch_of_its_own_and_hands_it_over(repo: Path, store: Store,
                                                                     tmp_path: Path) -> None:
    root = tmp_path / "rt"
    (repo / "app" / "draft.py").write_text("half done\n")  # the user's: not the task's
    before = user_view(repo)
    work_dir = prepare(store, task("task-1", repo / "app"), root, links=(".env", "node_modules"))
    path = root / "task-worktrees" / "task-1"
    assert work_dir == str(path / "app") == store.work_dirs["task-1"]
    assert git(path, "symbolic-ref", "HEAD") == "refs/heads/looper/task-1/task"
    assert git(path, "rev-parse", "HEAD") == git(repo, "rev-parse", "HEAD")
    assert not (path / "app" / "draft.py").exists()
    row = store.worktrees["task-1"]
    assert (row["state"], row["links"], row["subdir"]) == ("active", [], "app")  # the links are at the top level
    notes = [text for _, text in store.notes]
    assert notes[0].startswith(f"The task works in a worktree of {repo / 'app'}: branch looper/task-1/task, from main at")
    assert "did not link .env: the workspace has none; node_modules: the workspace has none" in notes[1]
    assert notes[2].startswith("1 uncommitted change(s) in the workspace are not in the task's worktree: app/draft.py")

    # The task works; a restart goes on in the same worktree.
    (path / "app" / "feature.py").write_text("done\n")
    git(path / "app", "add", "feature.py")
    git(path / "app", "commit", "-qm", "worker: feature")
    (path / "app" / "notes.md").write_text("left uncommitted\n")
    assert prepare(store, task("task-1", repo / "app"), root) == work_dir

    finished = run(tw.finish("task-1", completed=True))
    assert finished is not None and finished.state is WorktreeState.REMOVED and not path.exists()
    branch = "looper/task-1/task"
    assert git(repo, "log", "--format=%an|%s", branch, "-3").splitlines() == [
        "Looper|looper: commit what task task-1 left uncommitted", "User|worker: feature", "User|init"]
    assert finished.result_commit == git(repo, "rev-parse", branch)
    assert user_view(repo) == before  # never merged: the user's branch, index and files are as they were
    assert "task-worktrees" not in git(repo, "worktree", "list")  # its record went with it
    assert [c["command"] for c in finished.view()["commands"]] == [
        f"git log --oneline {finished.base_commit}..{branch}", f"git diff {finished.base_commit}...{branch}",
        f"git merge {branch}", f"git branch -D {branch}"]
    assert run(tw.finish("task-1", completed=True)) == finished  # once


def test_a_worktree_links_the_workspaces_local_state_and_never_commits_it(repo: Path, store: Store,
                                                                         tmp_path: Path) -> None:
    (repo / "node_modules" / "pkg").mkdir(parents=True)
    (repo / "node_modules" / "pkg" / "index.js").write_text("module\n")
    work_dir = Path(prepare(store, task("task-1", repo), tmp_path / "rt", links=(".env", "node_modules")))
    assert os.readlink(work_dir / ".env") == str(repo / ".env")
    assert store.worktrees["task-1"]["links"] == [".env", "node_modules"]
    git(work_dir, "add", "-A")  # node_modules/ does not match a link: the task commits it
    git(work_dir, "commit", "-qm", "worker: everything")
    run(tw.finish("task-1", completed=True))
    assert "node_modules" not in git(repo, "ls-tree", "--name-only", "looper/task-1/task").split()
    assert (repo / "node_modules" / "pkg" / "index.js").read_text() == "module\n" and (repo / ".env").exists()


def test_a_worktree_starts_where_the_task_says(repo: Path, store: Store, tmp_path: Path) -> None:
    git(repo, "commit", "-q", "--allow-empty", "-m", "after v1")
    prepare(store, task("task-1", repo, base="v1"), tmp_path / "rt")
    assert git(tmp_path / "rt" / "task-worktrees" / "task-1", "rev-parse", "HEAD") == git(repo, "rev-parse", "v1")
    with pytest.raises(TaskWorkspaceError, match="'gone' is not a branch"):
        prepare(store, task("task-2", repo, base="gone"), tmp_path / "rt")


def test_a_stopped_task_keeps_its_worktree_and_a_deleted_one_leaves_its_branch(repo: Path, store: Store,
                                                                             tmp_path: Path) -> None:
    root = tmp_path / "rt"
    path = Path(prepare(store, task("task-1", repo), root))
    (path / "wip.txt").write_text("work in progress\n")
    kept = run(tw.finish("task-1"))  # stopped, failed or cancelled
    assert kept is not None and kept.state is WorktreeState.ACTIVE and (path / "wip.txt").exists()
    gone = run(tw.finish("task-1", deleted=True))
    assert gone is not None and gone.state is WorktreeState.REMOVED and not path.exists()
    assert git(repo, "show", "looper/task-1/task:wip.txt") == "work in progress"


def test_a_lost_worktree_comes_back_from_its_branch_and_a_lost_branch_fails_the_start(repo: Path, store: Store,
                                                                                       tmp_path: Path) -> None:
    root = tmp_path / "rt"
    path = Path(prepare(store, task("task-1", repo), root, links=(".env",)))
    (path / "kept.txt").write_text("kept\n")
    git(path, "add", "kept.txt")
    git(path, "commit", "-qm", "worker: kept")
    subprocess.run(["rm", "-rf", str(path)], check=True)
    assert prepare(store, task("task-1", repo), root) == str(path)
    assert (path / "kept.txt").exists() and os.readlink(path / ".env") == str(repo / ".env")
    assert store.notes[-1][1] == "The task's worktree was gone: made again from its branch looper/task-1/task"

    subprocess.run(["rm", "-rf", str(path)], check=True)
    git(repo, "worktree", "remove", "--force", str(path))
    git(repo, "branch", "-D", "looper/task-1/task")
    with pytest.raises(TaskWorkspaceError, match="branch looper/task-1/task is gone"):
        prepare(store, task("task-1", repo), root)


def test_a_worktree_moved_off_its_branch_is_left_as_it_is(repo: Path, store: Store, tmp_path: Path) -> None:
    path = Path(prepare(store, task("task-1", repo), tmp_path / "rt"))
    git(path, "switch", "-q", "-c", "elsewhere")
    (path / "x.txt").write_text("x\n")
    with pytest.raises(TaskWorkspaceError, match="no longer on its branch looper/task-1/task"):
        run(tw.finish("task-1", completed=True))
    assert path.exists() and store.worktrees["task-1"]["state"] == "active"


def test_steps_that_run_together_nest_in_the_tasks_worktree_and_merge_into_its_branch(repo: Path, store: Store,
                                                                                     tmp_path: Path) -> None:
    root = tmp_path / "rt"
    work_dir = prepare(store, task("task-1", repo), root)
    found = run(ws.inspect(work_dir))
    assert isinstance(found, ws.Repo) and found.branch == "refs/heads/looper/task-1/task"
    step = ws.new_workspace(found, runtime_root=root, workspace_id=1, task_id="task-1", step_id="s1", step_key="s1",
                            base=found.head, checkpointed=False)
    run(ws.ensure_worktree(step))
    (Path(step.path) / "one.txt").write_text("one\n")
    saved: list[ws.StepWorkspace] = []

    async def save(state: ws.StepWorkspace) -> None:
        saved.append(state)

    assert run(ws.merge(step, step_key="s1", title="do s1", save=save)).workspace.state is ws.StepWorkspaceState.MERGED
    assert git(repo, "log", "--first-parent", "--format=%s", "looper/task-1/task", "-1") == "looper: merge step s1: do s1"
    assert git(repo, "rev-parse", "main") == git(repo, "rev-parse", "v1")  # the user's branch did not move


def test_the_startup_sweep_finishes_what_ended_and_removes_what_no_task_has(repo: Path, store: Store,
                                                                           tmp_path: Path) -> None:
    root = tmp_path / "rt"
    path = Path(prepare(store, task("task-1", repo), root))
    (path / "done.txt").write_text("done\n")
    orphan = root / "task-worktrees" / "task-gone"
    git(repo, "worktree", "add", "-q", "-b", "looper/task-gone/task", str(orphan))
    (orphan / "left.txt").write_text("left\n")
    assert run(tw.sweep(root)) == 2  # task-1 completed meanwhile (the stand-in lists it so); task-gone has no row
    assert not path.exists() and not orphan.exists()
    assert git(repo, "show", "looper/task-1/task:done.txt") == "done"
    assert git(repo, "show", "looper/task-gone/task:left.txt") == "left"  # its branch stays, with what it held


@pytest.mark.parametrize("command", ["location", "head", "branch"])
@pytest.mark.parametrize("failure", ["timeout", "os_error", "git_error", "exit_one"])
def test_repository_inspection_failures_never_fall_back_to_in_place(
    repo: Path, store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str, failure: str,
) -> None:
    original = ws._git

    async def broken(cwd: Any, *args: str, **kwargs: Any) -> Any:
        inspecting = ("location" if "--show-toplevel" in args else "head" if "HEAD^{commit}" in args
                      else "branch" if "symbolic-ref" in args else "")
        if inspecting == command:
            if failure == "timeout":
                raise ws.GitError("git inspection timed out")
            if failure == "os_error":
                raise OSError("git inspection unavailable")
            return ws._Run(1 if failure == "exit_one" else 128, b"", b"fatal: inspection failed")
        return await original(cwd, *args, **kwargs)

    monkeypatch.setattr(ws, "_git", broken)
    with pytest.raises(TaskWorkspaceError, match="inspection"):
        run(tw.check(str(repo), mode=WORKTREE))
    with pytest.raises(TaskWorkspaceError, match="inspection"):
        prepare(store, task("task-1", repo), tmp_path / "rt")
    assert not store.worktrees and not store.work_dirs and not store.notes


def test_prepare_retry_repairs_the_tasks_work_dir(repo: Path, store: Store, tmp_path: Path,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import tasks

    async def unavailable(*_: Any) -> None:
        raise RuntimeError("task update unavailable")

    root = tmp_path / "rt"
    row = task("task-1", repo / "app")
    monkeypatch.setattr(tasks, "set_work_dir", unavailable)
    with pytest.raises(RuntimeError, match="task update unavailable"):
        prepare(store, row, root)
    assert store.worktrees["task-1"]["state"] == "active" and not store.work_dirs
    monkeypatch.setattr(tasks, "set_work_dir", store.set_work_dir)
    work_dir = prepare(store, row, root)
    assert store.work_dirs.get("task-1") == work_dir == str(root / "task-worktrees" / "task-1" / "app")


@pytest.mark.parametrize("failure", ["exit", "timeout", "os_error", "detached", "wrong_branch", "pending",
                                     "common", "checkouts", "links"])
def test_sweep_preserves_unverifiable_orphans(repo: Path, store: Store, tmp_path: Path,
                                            monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
                                            failure: str) -> None:
    root = tmp_path / "rt"
    path = Path(prepare(store, task("task-1", repo), root))
    (path / "keep.txt").write_text("uncommitted work\n")
    store.worktrees.clear()
    original = ws._git

    async def broken(cwd: Any, *args: str, **kwargs: Any) -> Any:
        if "symbolic-ref" in args and failure not in ("pending", "common", "checkouts", "links"):
            if failure == "timeout":
                raise ws.GitError("verification timed out")
            if failure == "os_error":
                raise OSError("verification unavailable")
            return ws._Run(1 if failure == "detached" else 128 if failure == "exit" else 0,
                           b"refs/heads/other" if failure == "wrong_branch" else b"", b"verification failed")
        if ((failure == "pending" and "add" in args) or (failure == "common" and "--git-common-dir" in args)
                or (failure == "checkouts" and "--porcelain" in args) or (failure == "links" and "ls-files" in args)):
            raise ws.GitError("cannot inspect pending changes")
        return await original(cwd, *args, **kwargs)

    monkeypatch.setattr(ws, "_git", broken)
    assert run(tw.sweep(root)) == 0
    assert (path / "keep.txt").read_text() == "uncommitted work\n"
    assert "leaving" in caplog.text


@pytest.mark.parametrize("source_worktree", [False, True])
def test_sweep_never_commits_orphan_workspace_links(repo: Path, store: Store, tmp_path: Path,
                                                   source_worktree: bool) -> None:
    if source_worktree:
        source = tmp_path / "source"
        git(repo, "worktree", "add", "-q", "-b", "source", str(source))
    else:
        source = repo
    (source / "app" / "node_modules").mkdir()
    (source / "app" / ".venv").mkdir()
    root = tmp_path / "rt"
    path = Path(prepare(store, task("task-1", source / "app"), root, links=("node_modules", ".venv")))
    (path / "result.txt").write_text("retained result\n")
    git(path, "add", "-A")
    git(path, "commit", "-qm", "worker: result and links")
    store.worktrees.clear()
    assert run(tw.sweep(root)) == 1
    tracked = git(repo, "ls-tree", "-r", "--name-only", "looper/task-1/task").splitlines()
    assert "app/result.txt" in tracked
    assert "app/node_modules" not in tracked and "app/.venv" not in tracked


def test_a_detached_workspace_can_still_run_in_a_worktree(repo: Path, store: Store, tmp_path: Path) -> None:
    git(repo, "checkout", "-q", "--detach")
    found = run(tw.check(str(repo), mode=WORKTREE))
    assert found["mode"] == "worktree" and found["git"]["branch"] is None
    path = prepare(store, task("task-1", repo), tmp_path / "rt")
    assert path != str(repo) and store.work_dirs["task-1"] == path


def test_a_repository_without_a_commit_does_not_silently_run_in_place(tmp_path: Path, store: Store) -> None:
    path = tmp_path / "empty-repo"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    with pytest.raises(TaskWorkspaceError, match="no readable commit"):
        prepare(store, task("task-1", path), tmp_path / "rt")
    assert not store.work_dirs and not store.notes
