"""Task workspaces: a task runs in a git worktree of the directory the user named, on a branch of its own.

The design is the task workspaces RFC (``docs/architecture/rfcs/task-workspaces-v0``):

- a task names a workspace (``tasks.workspace_dir``, else its project's
  directory) and runs in it in place or in a worktree of it
  (``tasks.workspace_mode``, chosen at creation by ``resolve_mode``);
- before its Planner starts, the TaskRouter calls ``prepare``: a worktree
  task gets ``<runtime_root>/task-worktrees/<task_id>`` on branch
  ``looper/<task_id>/task`` from its base (``tasks.workspace_base``, else the
  workspace's commit), with the project's ``workspace_links``, and its
  ``work_dir`` becomes the workspace's place in it; a later start uses the
  same worktree, made again from its branch when the directory was lost; a
  workspace outside git runs in place;
- ``finish`` commits what the task left uncommitted on its branch (as Looper,
  never the links) and removes the worktree when the task completes or is
  deleted; the branch stays in the user's repository and is never merged. A
  failed, stopped or cancelled task keeps its worktree;
- ``check`` says, before a task is created, what its workspace is and warns
  (never refuses): outside git, uncommitted changes left out, other tasks on
  the same repository, a large checkout.

Every git command goes through ``step_workspaces``' runner: no hooks, no
prompts, a time limit, Looper's identity for its commits.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import shutil
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from . import step_workspaces as ws
from .operator_notes import notes_key, notes_view
from .project_execution import TaskWorkspaces

logger = logging.getLogger(__name__)

TASK_WORKTREES_DIR = "task-worktrees"
FILES_SHOWN = 10


class TaskWorkspaceError(Exception):
    """A task's worktree cannot be made or used; the message says why."""


class WarningKind(str, Enum):
    NOT_GIT = "not_git"  # a worktree was asked for, but the workspace is not in git: the task runs in place
    UNCOMMITTED = "uncommitted"  # the workspace has changes its worktree will not have
    SHARED = "shared"  # other tasks in progress work on the same repository
    LARGE_CHECKOUT = "large_checkout"  # each worktree checks out this much


class WorktreeState(str, Enum):
    ACTIVE = "active"
    REMOVED = "removed"  # its directory is gone; its branch stays


def task_branch(task_id: str) -> str:
    """The task's branch: not ``looper/<task_id>``, which git could not have beside its steps' branches
    ``looper/<task_id>/<key>-<n>``."""
    return f"looper/{task_id}/task"


@dataclass(frozen=True)
class TaskWorktree:
    task_id: str
    repo_dir: str  # the workspace's repository's top level
    subdir: str  # the workspace relative to repo_dir, '' at its top level
    path: str  # the worktree's top level
    branch: str
    base_commit: str
    links: tuple[str, ...] = ()  # relative to the workspace
    state: WorktreeState = WorktreeState.ACTIVE
    result_commit: str | None = None  # the branch's tip when the worktree was removed
    removed_at: datetime | None = None

    @property
    def work_dir(self) -> str:
        """Where the task runs: the workspace's place in the worktree."""
        return str(Path(self.path) / self.subdir) if self.subdir else self.path

    @property
    def workspace(self) -> str:
        return str(Path(self.repo_dir) / self.subdir) if self.subdir else self.repo_dir

    def row(self) -> dict[str, Any]:
        return {"task_id": self.task_id, "repo_dir": self.repo_dir, "subdir": self.subdir, "path": self.path,
                "branch": self.branch, "base_commit": self.base_commit, "links": list(self.links),
                "state": self.state.value, "result_commit": self.result_commit, "removed_at": self.removed_at}

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> TaskWorktree:
        return cls(task_id=str(row["task_id"]), repo_dir=str(row["repo_dir"]), subdir=str(row.get("subdir") or ""),
                   path=str(row["path"]), branch=str(row["branch"]), base_commit=str(row["base_commit"]),
                   links=tuple(row.get("links") or ()), state=WorktreeState(row["state"]),
                   result_commit=row.get("result_commit"), removed_at=row.get("removed_at"))

    def view(self) -> dict[str, Any]:
        """As the task page shows it, with the commands that take the work over (run in the workspace)."""
        return {"branch": self.branch, "base_commit": self.base_commit, "path": self.path,
                "repository": self.repo_dir, "links": list(self.links), "state": self.state.value,
                "result_commit": self.result_commit, "removed_at": self.removed_at,
                "commands": branch_commands(self)}


def branch_commands(worktree: TaskWorktree) -> list[dict[str, str]]:
    """What the user runs in the workspace to see, take or drop the task's work; Looper never merges it."""
    q = shlex.quote
    branch, base = q(worktree.branch), worktree.base_commit
    commands = [
        {"what": "See the task's commits", "command": f"git log --oneline {base}..{branch}"},
        {"what": "See its changes", "command": f"git diff {base}...{branch}"},
        {"what": "Merge it into the branch you have checked out", "command": f"git merge {branch}"},
    ]
    if worktree.state is WorktreeState.REMOVED:
        commands.append({"what": "Delete the branch, once merged or to drop the work",
                         "command": f"git branch -D {branch}"})
    return commands


def notice_line(worktree: TaskWorktree) -> str:
    """The line a completed task's notice adds (飞书、Telegram): where its work is, and that the user merges it."""
    return (f"结果在分支 {worktree.branch}（{worktree.workspace} 所在的仓库），确认后自己合并："
            f"git merge {worktree.branch}")


def same_directory(a: str, b: str) -> bool:
    return bool(a) and bool(b) and os.path.realpath(a) == os.path.realpath(b)


def resolve_mode(*, requested: str | None, named: str, project_dir: str,
                 project_mode: TaskWorkspaces) -> TaskWorkspaces:
    """How a new task runs (the RFC's W1): as asked; in a worktree when it names a workspace other than its
    project's directory; else as its project says. Raises ``ValueError`` for a mode that is not one."""
    if requested:
        try:
            return TaskWorkspaces(requested)
        except ValueError:
            choices = ", ".join(mode.value for mode in TaskWorkspaces)
            raise ValueError(f"workspace_mode must be one of {choices}") from None
    if named and not same_directory(named, project_dir):
        return TaskWorkspaces.WORKTREE
    return project_mode


# --- The workspace's repository ---


@dataclass(frozen=True)
class _Repository:
    toplevel: str
    subdir: str
    head: str
    branch: str  # refs/heads/<name>, '' on a detached HEAD


async def _repository(workspace: str) -> _Repository | None:
    """The workspace's repository, or None when it is not in a git work tree with a commit."""
    if not Path(workspace).is_dir():
        return None
    try:
        where = await ws._git(workspace, "rev-parse", "--show-toplevel", "--show-prefix", check=False)
        if where.returncode != 0:
            return None
        lines = where.stdout.decode("utf-8", "surrogateescape").split("\n")
        toplevel, subdir = lines[0], (lines[1] if len(lines) > 1 else "").rstrip("/")
        head = await ws._git(toplevel, "rev-parse", "--verify", "-q", "HEAD^{commit}", check=False)
        if head.returncode != 0:
            return None
        branch = await ws._git(toplevel, "symbolic-ref", "-q", "HEAD", check=False)
    except ws.GitError:
        return None
    return _Repository(toplevel, subdir, head.text, branch.text if branch.returncode == 0 else "")


async def _base_commit(repo: _Repository, base: str) -> str:
    if not base:
        return repo.head
    run = await ws._git(repo.toplevel, "rev-parse", "--verify", "-q", f"{base}^{{commit}}", check=False)
    if run.returncode != 0:
        raise TaskWorkspaceError(f"the base {base!r} is not a branch, tag or commit of {repo.toplevel}")
    return run.text


def _files(paths: Sequence[str]) -> str:
    shown = ", ".join(paths[:FILES_SHOWN])
    return shown + (f" (+{len(paths) - FILES_SHOWN} more)" if len(paths) > FILES_SHOWN else "")


async def _shared(workspace: str, besides: str | None) -> list[dict[str, Any]]:
    """The tasks in progress whose workspace is on the same repository (its main checkout) as ``workspace``."""
    from ..db import tasks as db_tasks

    key = await asyncio.to_thread(notes_key, workspace)
    shared = []
    for task in await db_tasks.active_workspaces(besides=besides):
        if task["workspace"] and await asyncio.to_thread(notes_key, task["workspace"]) == key:
            shared.append({"task_id": task["id"], "name": task["name"], "status": task["status"],
                           "mode": task.get("workspace_mode") or TaskWorkspaces.IN_PLACE.value})
    return shared


def overwrites(mode: TaskWorkspaces | str, shared: Sequence[Mapping[str, Any]]) -> bool:
    """Whether a task running as ``mode`` works in the same files as one of the ``shared`` tasks: both in place."""
    return TaskWorkspaces(mode) is TaskWorkspaces.IN_PLACE and any(
        task.get("mode") == TaskWorkspaces.IN_PLACE.value for task in shared)


async def check(workspace: str, *, mode: TaskWorkspaces, base: str = "", besides: str | None = None,
                asked: bool = True) -> dict[str, Any]:
    """What a task in ``workspace`` would run in, and the warnings (the RFC's W5); outside git a worktree task runs
    in place, said only when the worktree was ``asked`` for (not when it is just the project's default). Raises
    ``TaskWorkspaceError`` for a base the repository does not have."""
    repo = await _repository(workspace)
    warnings: list[dict[str, Any]] = []
    effective = mode
    git: dict[str, Any] | None = None
    if repo is None:
        if mode is TaskWorkspaces.WORKTREE:
            effective = TaskWorkspaces.IN_PLACE
        if mode is TaskWorkspaces.WORKTREE and asked:
            warnings.append({"kind": WarningKind.NOT_GIT.value,
                             "text": "The workspace is not in a git repository with a commit: the task runs in "
                                     "place, in the directory itself."})
    else:
        git = {"repository": repo.toplevel, "branch": ws.short_branch(repo.branch) or None, "head": repo.head}
        if mode is TaskWorkspaces.WORKTREE:
            git["base_commit"] = await _base_commit(repo, base)
            try:
                pending = await ws._pending(repo.toplevel, repo.subdir or ".")
                size = await ws.checkout_size(repo.toplevel)
            except ws.GitError:  # a warning git cannot say is left out, not the check
                logger.warning("cannot read the state of %s", repo.toplevel, exc_info=True)
                pending, size = [], 0
            if pending:
                warnings.append({"kind": WarningKind.UNCOMMITTED.value, "files": pending[:50],
                                 "text": f"{len(pending)} uncommitted change(s) in the workspace will not be in the "
                                         f"task's worktree: {_files(pending)}. Commit them first if the task "
                                         "needs them."})
            if size > ws.CHECKOUT_LIMIT_BYTES:
                warnings.append({"kind": WarningKind.LARGE_CHECKOUT.value, "bytes": size,
                                 "text": f"The checkout is {ws._size_text(size)}: the task's worktree is a copy of "
                                         "it."})
    shared = await _shared(workspace, besides)
    if shared:
        names = ", ".join(f"{task['name']} ({task['status']})" for task in shared[:5])
        if overwrites(effective, shared):
            text = (f"Also in progress on this repository: {names}. Tasks in place work in the same files and "
                    "may overwrite each other's changes.")
        else:
            text = (f"Also in progress on this repository: {names}. A task in a worktree works on a branch of its "
                    "own, so they do not get in each other's way; their work may conflict when you merge it.")
        warnings.append({"kind": WarningKind.SHARED.value, "tasks": shared, "text": text})
    return {"workspace": workspace, "mode": effective.value, "requested_mode": mode.value, "git": git,
            "warnings": warnings, "notes": await asyncio.to_thread(notes_view, workspace)}


# --- A task's worktree ---

Note = Callable[[str, str], Awaitable[None]]  # (kind, text) into the task's activity


def _worktree_path(runtime_root: Path, task_id: str) -> Path:
    return runtime_root / TASK_WORKTREES_DIR / task_id


async def _save(worktree: TaskWorktree) -> None:
    from ..db import task_worktrees as db_task_worktrees

    await db_task_worktrees.save(worktree.row())


def _link(worktree: TaskWorktree, relative: str) -> str:
    return ws.link_path(Path(worktree.workspace) / relative, Path(worktree.work_dir) / relative, Path(worktree.path),
                        missing="the workspace has none")


async def prepare(task: Mapping[str, Any], *, runtime_root: Path, links: Sequence[str], note: Note) -> str:
    """Where the task runs from now on (its ``work_dir``): for a worktree task its worktree, made now the first time
    and again from its branch when its directory was lost. Raises ``TaskWorkspaceError``."""
    from ..db import task_worktrees as db_task_worktrees, tasks as db_tasks

    task_id, work_dir = str(task["id"]), str(task.get("work_dir") or "")
    if task.get("workspace_mode") != TaskWorkspaces.WORKTREE.value:
        return work_dir
    row = await db_task_worktrees.get(task_id)
    if row is not None and row["state"] == WorktreeState.ACTIVE.value:
        worktree = TaskWorktree.from_row(row)
        if not (Path(worktree.path) / ".git").exists():
            await _restore(worktree)
            await note("phase", f"The task's worktree was gone: made again from its branch {worktree.branch}")
        return worktree.work_dir
    workspace = str(task.get("workspace_dir") or work_dir)
    repo = await _repository(workspace)
    if repo is None:
        await note("phase", "The workspace is not in a git repository with a commit: the task runs in place, in "
                            f"{workspace}")
        return work_dir
    base = await _base_commit(repo, str(task.get("workspace_base") or ""))
    branch = task_branch(task_id)
    path = _worktree_path(runtime_root, task_id)
    try:
        await ws._git(repo.toplevel, "worktree", "remove", "--force", str(path), check=False)  # a stale record
        path.parent.mkdir(parents=True, exist_ok=True)
        if await ws.branch_exists(repo.toplevel, branch):  # made before a restart cut this off
            await ws._git(repo.toplevel, "worktree", "add", "-q", str(path), branch)
        else:
            await ws._git(repo.toplevel, "worktree", "add", "-q", "-b", branch, str(path), base)
    except (ws.GitError, OSError) as exc:
        raise TaskWorkspaceError(f"cannot make the task's worktree of {workspace}: {exc}") from None
    worktree = TaskWorktree(task_id=task_id, repo_dir=repo.toplevel, subdir=repo.subdir, path=str(path),
                            branch=branch, base_commit=base)
    await _save(worktree)
    linked, skipped = [], []
    for relative in links:
        if problem := _link(worktree, relative):
            skipped.append(f"{relative}: {problem}")
        else:
            linked.append(relative)
    if linked:
        worktree = replace(worktree, links=tuple(linked))
        await _save(worktree)
    Path(worktree.work_dir).mkdir(parents=True, exist_ok=True)
    await db_tasks.set_work_dir(task_id, worktree.work_dir)
    started = str(task.get("workspace_base") or "") or ws.short_branch(repo.branch) or "a detached HEAD"
    await note("phase", f"The task works in a worktree of {workspace}: branch {branch}, from {started} at "
                        f"{base[:12]}" + (f"; linked {', '.join(linked)}" if linked else ""))
    if skipped:
        await note("phase", f"The task's worktree did not link {'; '.join(skipped)}"[:500])
    try:
        pending = await ws._pending(repo.toplevel, repo.subdir or ".")
    except ws.GitError:
        pending = []  # only a note goes without it
    if pending:
        await note("phase", f"{len(pending)} uncommitted change(s) in the workspace are not in the task's worktree: "
                            f"{_files(pending)}"[:500])
    return worktree.work_dir


async def _restore(worktree: TaskWorktree) -> None:
    """Make a lost worktree again from its branch, with its links; its branch must still be there."""
    if not await ws.branch_exists(worktree.repo_dir, worktree.branch):
        raise TaskWorkspaceError(f"the task's branch {worktree.branch} is gone from {worktree.repo_dir}, and with it "
                                 "the task's commits: start the task over in a new task")
    try:
        await ws._git(worktree.repo_dir, "worktree", "remove", "--force", worktree.path, check=False)
        Path(worktree.path).parent.mkdir(parents=True, exist_ok=True)
        await ws._git(worktree.repo_dir, "worktree", "add", "-q", worktree.path, worktree.branch)
    except (ws.GitError, OSError) as exc:
        raise TaskWorkspaceError(f"cannot make the task's worktree again: {exc}") from None
    for relative in worktree.links:
        _link(worktree, relative)


async def finish(task_id: str, *, completed: bool = False, deleted: bool = False) -> TaskWorktree | None:
    """At the task's end: when it completed or is deleted, commit what it left uncommitted on its branch and remove
    its worktree (the branch stays); a stopped or failed task keeps it. The worktree as it is now, None without one.
    Raises ``TaskWorkspaceError`` when what was left cannot be committed (the worktree then stays)."""
    from ..db import task_worktrees as db_task_worktrees

    row = await db_task_worktrees.get(task_id)
    if row is None:
        return None
    worktree = TaskWorktree.from_row(row)
    if worktree.state is not WorktreeState.ACTIVE or not (completed or deleted):
        return worktree
    path = Path(worktree.path)
    if (path / ".git").exists() and Path(worktree.repo_dir).is_dir():
        head = await ws._git(path, "symbolic-ref", "-q", "HEAD", check=False)
        if head.text != f"refs/heads/{worktree.branch}":
            raise TaskWorkspaceError(f"the task's worktree {worktree.path} is no longer on its branch "
                                     f"{worktree.branch}; it is left as it is")
        top = Path(worktree.subdir) if worktree.subdir else Path()
        still = [str(top / p) for p in worktree.links if (Path(worktree.work_dir) / p).is_symlink()]
        try:
            await ws.commit_pending(path, still, f"looper: commit what task {task_id} left uncommitted"
                                                 f"\n\nLooper-Task: {task_id}")
        except ws.GitError as exc:
            raise TaskWorkspaceError(f"cannot commit what the task left in its worktree: {exc}") from None
    tip = None
    if Path(worktree.repo_dir).is_dir():
        found = await ws._git(worktree.repo_dir, "rev-parse", "--verify", "-q",
                              f"refs/heads/{worktree.branch}^{{commit}}", check=False)
        tip = found.text if found.returncode == 0 else None
        await ws.forget_worktree(worktree.repo_dir, path)
    if path.exists():
        await asyncio.to_thread(shutil.rmtree, path, True)
    ws._remove_empty(path.parent)
    worktree = replace(worktree, state=WorktreeState.REMOVED, result_commit=tip, removed_at=ws.now())
    await _save(worktree)
    return worktree


async def sweep(runtime_root: Path) -> int:
    """At startup: finish the worktrees of tasks that completed meanwhile, and remove the directories no task has
    (committing what they hold on their branch first). Returns how many were removed."""
    from ..db import task_worktrees as db_task_worktrees

    removed = 0
    known: set[Path] = set()
    for row in await db_task_worktrees.list_active():
        known.add(Path(row["path"]).resolve())
        if row.get("task_status") == "completed":
            try:
                await finish(str(row["task_id"]), completed=True)
                removed += 1
            except TaskWorkspaceError:
                logger.warning("cannot finish the worktree of task %s", row["task_id"], exc_info=True)
    root = runtime_root / TASK_WORKTREES_DIR
    if root.is_dir():
        for path in sorted(p for p in root.iterdir() if p.is_dir()):
            if path.resolve() in known:
                continue
            branch = await ws._git(path, "symbolic-ref", "-q", "HEAD", check=False) if (path / ".git").is_file() \
                else None
            if branch is not None and branch.returncode == 0 and branch.text.endswith("/task") \
                    and branch.text.startswith("refs/heads/looper/"):
                try:
                    await ws.commit_pending(path, [], f"looper: commit what task {path.name} left uncommitted"
                                                      f"\n\nLooper-Task: {path.name}")
                except ws.GitError:
                    logger.warning("cannot commit what %s holds; leaving it", path, exc_info=True)
                    continue
            await ws.remove_orphan(path, keep_branch=True)
            removed += 1
    return removed
