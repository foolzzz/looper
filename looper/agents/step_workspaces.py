"""Step workspaces: a git worktree for each step that runs alongside others, merged into the task's branch.

A project with ``step_workspaces`` set to ``worktree`` (``project_execution``)
runs a step in a worktree of its own when other steps of the task are in
flight, or start in the same round, and the task directory is a git checkout
on a branch (``inspect``). Its Worker and Reviewer work there. The design is
the step workspaces RFC (``docs/architecture/rfcs/step-workdir-isolation-v0``):

- the round's steps start from a commit on the task directory's branch: the
  branch's tip, or a checkpoint commit of the task directory's pending changes
  (``checkpoint``);
- a step's worktree is on a branch of its own, ``looper/<task>/<key>-<id>``,
  under ``<runtime_root>/worktrees`` (``ensure_worktree``);
- when the step is accepted, ``merge`` commits what its Worker left
  uncommitted, computes the merge with the branch in memory, writes a merge
  commit and fast-forwards the branch, its index and working tree to it. A
  merge that cannot be made leaves the task directory as it was and says why
  (``MergeError``);
- Looper's own commits have the ``Looper`` identity and ``Looper-Task`` and
  ``Looper-Step`` trailers, are not signed, and no git command Looper runs
  runs a hook.

A workspace's state changes only along ``ALLOWED_TRANSITIONS``
(``StepWorkspace.moved``); the rows are ``db.step_workspaces``'.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

GIT_MIN_VERSION = (2, 40)
# Every git command Looper runs has this time limit.
GIT_TIMEOUT_SEC = 120
# How often a merge is computed again when the branch moves between computing it and moving the branch.
MERGE_ATTEMPTS = 3
LOOPER_NAME = "Looper"
LOOPER_EMAIL = "looper@localhost"
_IDENTITY = {"GIT_AUTHOR_NAME": LOOPER_NAME, "GIT_AUTHOR_EMAIL": LOOPER_EMAIL,
             "GIT_COMMITTER_NAME": LOOPER_NAME, "GIT_COMMITTER_EMAIL": LOOPER_EMAIL}
# No hooks run for Looper's commands; the Worker's own git commands run the repository's.
_GIT = ("git", "-c", "core.hooksPath=/dev/null")
# The files whose presence means the user is in the middle of an operation in the repository.
_IN_PROGRESS = {"MERGE_HEAD": "a merge", "CHERRY_PICK_HEAD": "a cherry-pick", "REVERT_HEAD": "a revert",
                "rebase-merge": "a rebase", "rebase-apply": "a rebase", "BISECT_LOG": "a bisect"}
WORKTREES_DIR = "worktrees"
# Inherited variables that would point Looper's commands at another repository or index.
_REDIRECTS = frozenset({"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"})


class StepWorkspaceState(str, Enum):
    ACTIVE = "active"  # the step works in it
    MERGING = "merging"  # the step was accepted; its branch is being merged
    MERGED = "merged"
    UNMERGED = "unmerged"  # the merge could not be made (merge_error says why)
    DISCARDED = "discarded"  # dropped without a merge


ALLOWED_TRANSITIONS: dict[StepWorkspaceState, frozenset[StepWorkspaceState]] = {
    StepWorkspaceState.ACTIVE: frozenset({StepWorkspaceState.MERGING, StepWorkspaceState.DISCARDED}),
    StepWorkspaceState.MERGING: frozenset({StepWorkspaceState.MERGED, StepWorkspaceState.UNMERGED}),
    StepWorkspaceState.UNMERGED: frozenset({StepWorkspaceState.DISCARDED}),
    StepWorkspaceState.MERGED: frozenset(),
    StepWorkspaceState.DISCARDED: frozenset(),
}


class MergeError(str, Enum):
    """Why an accepted step's branch could not be merged."""

    CONFLICT = "conflict"  # its changes conflict with the branch's
    LOCAL_CHANGES = "local_changes"  # uncommitted changes in the task directory are in the way
    BRANCH_CHANGED = "branch_changed"  # the task directory left the branch, or the branch lost the step's start
    STEP_BRANCH_CHANGED = "step_branch_changed"  # the step's checkout left its branch, or the branch was rewritten
    GIT_FAILED = "git_failed"  # another git error


class IllegalTransition(ValueError):
    """A workspace state change that ``ALLOWED_TRANSITIONS`` does not allow."""


class GitError(Exception):
    """A git command failed or timed out; the message has its output."""


@dataclass(frozen=True)
class StepWorkspace:
    """One worktree of one step (a retry reuses it; a retry after a failed merge gets a new one)."""

    id: int
    task_id: str
    step_id: str
    repo_dir: str  # the repository's top level: the task directory, or a directory above it
    subdir: str  # the task directory relative to repo_dir, '' when it is the top level
    target_branch: str  # the task directory's branch, refs/heads/<name>
    path: str  # the worktree's top level
    branch: str  # the step's branch, looper/<task_id>/<key>-<id>
    base_commit: str  # where the step's branch starts
    checkpoint: bool = False  # base_commit is the checkpoint of the round the step started in
    result_commit: str | None = None  # the step's branch when it was merged
    merge_commit: str | None = None  # written before the branch moves to it
    state: StepWorkspaceState = StepWorkspaceState.ACTIVE
    merge_error: MergeError | None = None  # set when state is unmerged
    detail: str = ""  # the conflicting or blocking files, or git's error
    removed_at: datetime | None = None  # its directory and branch are gone

    @property
    def work_dir(self) -> str:
        """Where the step's Worker and Reviewer run: the task directory's place in the worktree."""
        return str(Path(self.path) / self.subdir) if self.subdir else self.path

    @property
    def target(self) -> str:
        return short_branch(self.target_branch)

    def moved(self, state: StepWorkspaceState, **changes: Any) -> StepWorkspace:
        """This workspace in ``state``; raises ``IllegalTransition`` for a change the table does not allow."""
        if state not in ALLOWED_TRANSITIONS[self.state]:
            raise IllegalTransition(f"step workspace {self.id} cannot go from {self.state.value} to {state.value}")
        return replace(self, state=state, **changes)

    def context(self) -> dict[str, str]:
        """What a Worker's or Reviewer's dispatch carries about it (``context.workspace``)."""
        return {"path": self.path, "branch": self.branch, "base_commit": self.base_commit,
                "target_branch": self.target}

    def row(self) -> dict[str, Any]:
        """As ``db.step_workspaces.save`` writes it."""
        return {"id": self.id, "task_id": self.task_id, "step_id": self.step_id, "repo_dir": self.repo_dir,
                "subdir": self.subdir, "target_branch": self.target_branch, "path": self.path,
                "branch": self.branch, "base_commit": self.base_commit, "checkpoint": self.checkpoint,
                "result_commit": self.result_commit, "merge_commit": self.merge_commit, "state": self.state.value,
                "merge_error": self.merge_error.value if self.merge_error else None, "detail": self.detail,
                "removed_at": self.removed_at}

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> StepWorkspace:
        error = row.get("merge_error")
        return cls(
            id=int(row["id"]), task_id=str(row["task_id"]), step_id=str(row["step_id"]),
            repo_dir=str(row["repo_dir"]), subdir=str(row.get("subdir") or ""),
            target_branch=str(row["target_branch"]), path=str(row["path"]), branch=str(row["branch"]),
            base_commit=str(row["base_commit"]), checkpoint=bool(row.get("checkpoint")),
            result_commit=row.get("result_commit"), merge_commit=row.get("merge_commit"),
            state=StepWorkspaceState(row["state"]), merge_error=MergeError(error) if error else None,
            detail=str(row.get("detail") or ""), removed_at=row.get("removed_at"),
        )


def short_branch(ref: str) -> str:
    return ref.removeprefix("refs/heads/")


def now() -> datetime:
    return datetime.now(timezone.utc)


# --- Running git ---


@dataclass(frozen=True)
class _Run:
    returncode: int
    stdout: bytes
    stderr: bytes

    @property
    def text(self) -> str:
        return self.stdout.decode("utf-8", "replace").strip()

    @property
    def fields(self) -> list[str]:
        """The output's NUL-separated fields (of a ``-z`` command)."""
        return self.stdout.decode("utf-8", "surrogateescape").split("\0")


async def _git(cwd: str | Path, *args: str, identity: bool = False, check: bool = True,
               env: Mapping[str, str] | None = None) -> _Run:
    """Run one git command in ``cwd``: no hooks, no prompts, with the time limit; ``identity`` commits as Looper.

    Raises ``GitError`` when git is missing, times out or (with ``check``) fails.
    """
    command_env = {name: value for name, value in os.environ.items() if name not in _REDIRECTS}
    command_env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_MERGE_AUTOEDIT": "no", **(env or {})})
    if identity:
        command_env.update(_IDENTITY)
    try:
        process = await asyncio.create_subprocess_exec(
            *_GIT, *args, cwd=str(cwd), env=command_env, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise GitError(f"cannot run git in {cwd}: {exc}") from None
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), GIT_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise GitError(f"git {args[0]} timed out after {GIT_TIMEOUT_SEC}s") from None
    run = _Run(process.returncode or 0, stdout, stderr)
    if check and run.returncode != 0:
        output = (stderr or stdout).decode("utf-8", "replace").strip()
        raise GitError(f"git {' '.join(args[:3])} failed: {output[:500] or f'exit {run.returncode}'}")
    return run


async def _rev(cwd: str | Path, rev: str) -> str:
    return (await _git(cwd, "rev-parse", "--verify", "-q", f"{rev}^{{commit}}")).text


async def _is_ancestor(cwd: str | Path, ancestor: str, descendant: str) -> bool:
    run = await _git(cwd, "merge-base", "--is-ancestor", ancestor, descendant, check=False)
    if run.returncode not in (0, 1):
        raise GitError(f"git merge-base failed: {run.stderr.decode('utf-8', 'replace').strip()[:500]}")
    return run.returncode == 0


async def _pending(cwd: str | Path, *pathspec: str) -> list[str]:
    """The paths with uncommitted changes or untracked, not ignored, files: ``git status``, every file listed."""
    run = await _git(cwd, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", *pathspec)
    fields = [f for f in run.fields if f]
    paths: list[str] = []
    index = 0
    while index < len(fields):
        entry = fields[index]
        paths.append(entry[3:])
        if entry[:1] in ("R", "C"):  # a rename or copy: its source path follows
            index += 1
            paths.append(fields[index])
        index += 1
    return paths


async def _changed(cwd: str | Path, old: str, new: str) -> list[str]:
    return [f for f in (await _git(cwd, "diff", "--name-only", "-z", old, new)).fields if f]


_version: tuple[int, int] | None = None


async def git_version() -> tuple[int, int] | None:
    """The installed git's (major, minor), None without git; read once."""
    global _version
    if _version is None:
        if shutil.which("git") is None:
            return None
        match = re.search(r"(\d+)\.(\d+)", (await _git("/", "--version")).text)
        _version = (int(match.group(1)), int(match.group(2))) if match else (0, 0)
    return _version


# --- The task directory ---


@dataclass(frozen=True)
class Repo:
    """A task directory that can have step workspaces: a git checkout on a branch with a commit."""

    toplevel: str
    subdir: str  # the task directory relative to toplevel, '' at the top level
    branch: str  # refs/heads/<name>
    head: str


@dataclass(frozen=True)
class Unusable:
    """A task directory that cannot have step workspaces now, and why."""

    reason: str
    repository: bool  # it is in a git repository (False: steps share it, as without workspaces)


def _in_repository(directory: Path) -> bool:
    return any((candidate / ".git").exists() for candidate in (directory, *directory.parents))


async def inspect(work_dir: str) -> Repo | Unusable:
    """Whether the task directory can have step workspaces (the RFC's D1), and its repository if it can."""
    directory = Path(work_dir)
    if not directory.is_dir() or not _in_repository(directory):
        return Unusable("the task directory is not in a git repository", repository=False)
    try:
        version = await git_version()
        if version is None:
            return Unusable("git is not installed", repository=True)
        if version < GIT_MIN_VERSION:
            return Unusable(f"git {version[0]}.{version[1]} is older than "
                            f"{GIT_MIN_VERSION[0]}.{GIT_MIN_VERSION[1]}", repository=True)
        where = await _git(directory, "rev-parse", "--show-toplevel", "--show-prefix", check=False)
        if where.returncode != 0:
            return Unusable("the task directory is not in a git work tree", repository=True)
        lines = where.stdout.decode("utf-8", "surrogateescape").split("\n")
        toplevel, subdir = lines[0], (lines[1] if len(lines) > 1 else "").rstrip("/")
        branch = await _git(toplevel, "symbolic-ref", "-q", "HEAD", check=False)
        if branch.returncode != 0:
            return Unusable("the task directory's HEAD is detached", repository=True)
        head = await _git(toplevel, "rev-parse", "--verify", "-q", "HEAD^{commit}", check=False)
        if head.returncode != 0:
            return Unusable(f"{short_branch(branch.text)} has no commit yet", repository=True)
        names = list(_IN_PROGRESS)
        paths = (await _git(toplevel, "rev-parse", *(arg for name in names for arg in ("--git-path", name)))).text
        for name, path in zip(names, paths.split("\n")):
            if (Path(toplevel) / path).exists():
                return Unusable(f"{_IN_PROGRESS[name]} is in progress in the task directory", repository=True)
    except GitError as exc:
        return Unusable(str(exc), repository=True)
    return Repo(toplevel=toplevel, subdir=subdir, branch=branch.text, head=head.text)


@dataclass(frozen=True)
class Round:
    """Which of the ready steps start now, and where."""

    start: int  # how many of the ready steps start now, in order
    isolate: bool  # they start in worktrees of their own
    note: str = ""  # why fewer start than are ready


def plan_round(*, in_flight: Sequence[bool], ready: Sequence[bool], repo: Repo | Unusable) -> Round:
    """The RFC's D2 for a project with step workspaces: steps that run together run in worktrees of their own.

    ``in_flight`` says, for each step in flight (running, awaiting a decision or in review), whether it works in
    a worktree; ``ready``, for each ready step, whether it has one already (a step that carries on). A step that
    starts alone while nothing else is in flight works in the task directory, and while one works there the
    others wait. A task directory outside git runs its steps as without step workspaces; one in git that cannot
    have workspaces now (``inspect``) runs them one at a time.
    """
    if isinstance(repo, Unusable) and not repo.repository:
        return Round(len(ready), False)
    if not in_flight and len(ready) <= 1 and not any(ready):
        return Round(len(ready), False)
    if isinstance(repo, Unusable):
        return Round(0 if in_flight else 1, False, f"Steps run one at a time: {repo.reason}")
    if not all(in_flight):
        return Round(0, True, "A step works in the task directory: the steps ready to run wait until it ends")
    return Round(len(ready), True)


def _trailers(task_id: str, step_key: str = "") -> str:
    return f"\n\nLooper-Task: {task_id}" + (f"\nLooper-Step: {step_key}" if step_key else "")


async def checkpoint(repo: Repo, *, task_id: str) -> tuple[str, int]:
    """The commit a round of steps starts from: the branch's tip, or, when the task directory has pending changes,
    a checkpoint commit of all of them on the branch (the user's own included). Returns it and how many files the
    checkpoint took (0: no checkpoint). Changes outside the task directory are left alone."""
    spec = repo.subdir or "."
    pending = await _pending(repo.toplevel, spec)
    if pending:
        await _git(repo.toplevel, "add", "-A", "--", spec)
        await _git(repo.toplevel, "commit", "-q", "--no-verify", "--no-gpg-sign",
                   "-m", "looper: checkpoint before parallel steps" + _trailers(task_id), "--", spec, identity=True)
    return await _rev(repo.toplevel, "HEAD"), len(pending)


# --- A step's worktree ---


def _ref_component(text: str) -> str:
    """``text`` as one component of a branch name."""
    name = re.sub(r"\.{2,}", ".", re.sub(r"[^A-Za-z0-9._-]+", "-", text)).strip(".-")
    return name[:40] or "step"


def new_workspace(repo: Repo, *, runtime_root: Path, workspace_id: int, task_id: str, step_id: str,
                  step_key: str, base: str, checkpointed: bool) -> StepWorkspace:
    """The workspace a step gets, before its worktree exists."""
    name = f"{_ref_component(step_key)}-{workspace_id}"
    return StepWorkspace(
        id=workspace_id, task_id=task_id, step_id=step_id, repo_dir=repo.toplevel, subdir=repo.subdir,
        target_branch=repo.branch, path=str(runtime_root / WORKTREES_DIR / task_id / name),
        branch=f"looper/{task_id}/{name}", base_commit=base, checkpoint=checkpointed,
    )


async def ensure_worktree(workspace: StepWorkspace) -> None:
    """Make the workspace's worktree exist: from its start the first time, from its branch after it was lost."""
    path = Path(workspace.path)
    if not (path / ".git").exists():
        await _git(workspace.repo_dir, "worktree", "prune")
        path.parent.mkdir(parents=True, exist_ok=True)
        branch = await _git(workspace.repo_dir, "rev-parse", "--verify", "-q", f"refs/heads/{workspace.branch}",
                            check=False)
        if branch.returncode == 0:
            await _git(workspace.repo_dir, "worktree", "add", "-q", str(path), workspace.branch)
        else:
            await _git(workspace.repo_dir, "worktree", "add", "-q", "-b", workspace.branch, str(path),
                       workspace.base_commit)
    Path(workspace.work_dir).mkdir(parents=True, exist_ok=True)


# --- Merging an accepted step ---


@dataclass(frozen=True)
class MergeResult:
    workspace: StepWorkspace  # merged or unmerged
    files: tuple[str, ...]  # merged: the files the merge changed; unmerged: the conflicting or blocking ones
    leftovers: str | None = None  # the commit of what the Worker left uncommitted, if it left anything


class _Unmerged(Exception):
    def __init__(self, kind: MergeError, detail: str, files: Sequence[str] = ()) -> None:
        super().__init__(detail)
        self.kind = kind
        self.files = tuple(files)


def _files_detail(files: Sequence[str], limit: int = 20) -> str:
    shown = ", ".join(files[:limit])
    return shown + (f" (+{len(files) - limit} more)" if len(files) > limit else "")


async def merge(workspace: StepWorkspace, *, step_key: str, title: str,
                save: Callable[[StepWorkspace], Awaitable[None]]) -> MergeResult:
    """Merge an accepted step's branch into the task directory's branch, with a merge commit of its own.

    ``workspace`` is active (just accepted) or merging (a merge a restart cut off; doing it again makes no
    second merge commit). Every change of the workspace goes through ``save`` before the next git step. A merge
    that cannot be made leaves the task directory's branch, index and working tree as they were, and the
    workspace unmerged with the reason.
    """
    if workspace.state is StepWorkspaceState.ACTIVE:
        workspace = workspace.moved(StepWorkspaceState.MERGING)
        await save(workspace)
    elif workspace.state is not StepWorkspaceState.MERGING:
        raise IllegalTransition(f"step workspace {workspace.id} is {workspace.state.value}; it cannot be merged")
    leftovers: str | None = None
    try:
        workspace, leftovers = await _close_step_branch(workspace, step_key)
        await save(workspace)
        workspace, files = await _merge_into_target(workspace, step_key, title, save)
    except _Unmerged as failure:
        workspace = workspace.moved(StepWorkspaceState.UNMERGED, merge_error=failure.kind, detail=str(failure))
        await save(workspace)
        return MergeResult(workspace, failure.files, leftovers)
    except GitError as exc:
        workspace = workspace.moved(StepWorkspaceState.UNMERGED, merge_error=MergeError.GIT_FAILED,
                                    detail=str(exc))
        await save(workspace)
        return MergeResult(workspace, (), leftovers)
    workspace = workspace.moved(StepWorkspaceState.MERGED)
    await save(workspace)
    return MergeResult(workspace, files, leftovers)


async def _close_step_branch(workspace: StepWorkspace, step_key: str) -> tuple[StepWorkspace, str | None]:
    """Commit what the step's Worker left uncommitted; the workspace with its result commit, and that commit."""
    path = Path(workspace.path)
    leftovers: str | None = None
    if (path / ".git").exists():
        head = await _git(path, "symbolic-ref", "-q", "HEAD", check=False)
        if head.text != f"refs/heads/{workspace.branch}":
            raise _Unmerged(MergeError.STEP_BRANCH_CHANGED,
                            f"the step's checkout is no longer on its branch {workspace.branch}")
        if await _pending(path):
            await _git(path, "add", "-A")
            await _git(path, "commit", "-q", "--no-verify", "--no-gpg-sign", "-m",
                       f"looper: commit what step {step_key} left uncommitted"
                       + _trailers(workspace.task_id, step_key), identity=True)
            leftovers = await _rev(path, "HEAD")
    tip = await _git(workspace.repo_dir, "rev-parse", "--verify", "-q", f"refs/heads/{workspace.branch}^{{commit}}",
                     check=False)
    if tip.returncode != 0:
        raise _Unmerged(MergeError.STEP_BRANCH_CHANGED, f"the step's branch {workspace.branch} is gone")
    if not await _is_ancestor(workspace.repo_dir, workspace.base_commit, tip.text):
        raise _Unmerged(MergeError.STEP_BRANCH_CHANGED,
                        f"the step's branch {workspace.branch} no longer contains its start "
                        f"{workspace.base_commit[:12]}: its commits were rewritten")
    return replace(workspace, result_commit=tip.text), leftovers


async def _merge_into_target(workspace: StepWorkspace, step_key: str, title: str,
                             save: Callable[[StepWorkspace], Awaitable[None]]) -> tuple[StepWorkspace, tuple[str, ...]]:
    repo, target, result = workspace.repo_dir, workspace.target, workspace.result_commit
    assert result is not None
    for _ in range(MERGE_ATTEMPTS):
        head_ref = await _git(repo, "symbolic-ref", "-q", "HEAD", check=False)
        if head_ref.text != workspace.target_branch:
            now_on = short_branch(head_ref.text) if head_ref.returncode == 0 else "a detached HEAD"
            raise _Unmerged(MergeError.BRANCH_CHANGED, f"the task directory is on {now_on}, not {target}")
        head = await _rev(repo, "HEAD")
        if not await _is_ancestor(repo, workspace.base_commit, head):
            raise _Unmerged(MergeError.BRANCH_CHANGED,
                            f"{target} no longer contains the step's start {workspace.base_commit[:12]}")
        if await _is_ancestor(repo, result, head):
            return workspace, ()  # nothing to merge: the step changed nothing, or its merge already landed
        computed = await _git(repo, "merge-tree", "--write-tree", "--name-only", "-z", head, result, check=False)
        fields = computed.fields
        if computed.returncode == 1:
            names: list[str] = []
            for name in fields[1:]:  # the conflicting paths, up to an empty field (messages follow)
                if not name:
                    break
                names.append(name)
            conflicts = list(dict.fromkeys(names))
            raise _Unmerged(MergeError.CONFLICT, f"conflicts with {target} in {_files_detail(conflicts)}", conflicts)
        if computed.returncode != 0:
            raise GitError(f"git merge-tree failed: {computed.stderr.decode('utf-8', 'replace').strip()[:500]}")
        merge_commit = (await _git(
            repo, "commit-tree", "--no-gpg-sign", fields[0], "-p", head, "-p", result,
            "-m", f"looper: merge step {step_key}: {title}" + _trailers(workspace.task_id, step_key),
            identity=True)).text
        workspace = replace(workspace, merge_commit=merge_commit)
        await save(workspace)
        files = await _changed(repo, head, merge_commit)
        blocked = sorted(set(files) & set(await _pending(repo)))
        if blocked:
            raise _Unmerged(MergeError.LOCAL_CHANGES,
                            f"uncommitted changes in the task directory are in the way: {_files_detail(blocked)}",
                            blocked)
        moved = await _git(repo, "merge", "-q", "--ff-only", "--no-verify-signatures", merge_commit, check=False,
                           env={"GIT_REFLOG_ACTION": f"looper: merge step {step_key}"})
        if moved.returncode == 0:
            return workspace, tuple(files)
        if await _rev(repo, "HEAD") == head:
            raise GitError(f"git merge --ff-only failed: {moved.stderr.decode('utf-8', 'replace').strip()[:500]}")
        # The branch moved meanwhile: compute the merge again on its new tip.
    raise _Unmerged(MergeError.BRANCH_CHANGED, f"{target} kept moving while the step was merged")


# --- Removing workspaces ---


async def remove(workspace: StepWorkspace, *, keep_branch: bool = False) -> StepWorkspace:
    """Delete the workspace's worktree and, unless ``keep_branch``, its branch; removed when both are gone.

    A repository that no longer exists leaves only the directory to delete.
    """
    path = Path(workspace.path)
    if Path(workspace.repo_dir).is_dir():
        if path.exists():
            await _git(workspace.repo_dir, "worktree", "remove", "--force", str(path), check=False)
        await _git(workspace.repo_dir, "worktree", "prune", check=False)
        if not keep_branch:
            await _git(workspace.repo_dir, "branch", "-D", workspace.branch, check=False)
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    _remove_empty(path.parent)
    return workspace if keep_branch else replace(workspace, removed_at=now())


def _remove_empty(directory: Path) -> None:
    try:
        directory.rmdir()
    except OSError:
        pass


async def remove_orphan(path: Path) -> None:
    """Delete a worktree directory no workspace row knows: through its repository when git still knows it."""
    if (path / ".git").is_file():
        common = await _git(path, "rev-parse", "--path-format=absolute", "--git-common-dir", check=False)
        branch = await _git(path, "symbolic-ref", "-q", "HEAD", check=False)
        if common.returncode == 0:
            repo = str(Path(common.text).parent)
            await _git(repo, "worktree", "remove", "--force", str(path), check=False)
            await _git(repo, "worktree", "prune", check=False)
            if branch.returncode == 0 and branch.text.startswith("refs/heads/looper/"):
                await _git(repo, "branch", "-D", short_branch(branch.text), check=False)
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    _remove_empty(path.parent)


# --- A task's workspaces, and the sweep at startup ---


async def remove_task(task_id: str) -> int:
    """Remove every workspace of the task still there, branches included: the task completed, is deleted or is
    planned anew. Returns how many."""
    from ..db import step_workspaces as db_step_workspaces

    removed = 0
    for row in await db_step_workspaces.list_for_task(task_id):
        workspace = StepWorkspace.from_row(row)
        if workspace.removed_at is not None:
            continue
        if StepWorkspaceState.DISCARDED in ALLOWED_TRANSITIONS[workspace.state]:
            workspace = workspace.moved(StepWorkspaceState.DISCARDED)
        workspace = await remove(workspace)
        await db_step_workspaces.save(workspace.row())
        removed += 1
    return removed


async def sweep(runtime_root: Path) -> int:
    """At startup: remove the workspaces no task keeps. A completed task keeps none; a running, stopped or failed
    one keeps those not merged (until it is restarted or deleted); a directory no row knows goes. Returns how many
    were removed."""
    from ..db import step_workspaces as db_step_workspaces

    kept = await db_step_workspaces.list_kept()
    known = {Path(row["path"]).resolve() for row in kept}
    removed = 0
    for row in kept:
        workspace = StepWorkspace.from_row(row)
        if row.get("task_status") == "completed":
            if StepWorkspaceState.DISCARDED in ALLOWED_TRANSITIONS[workspace.state]:
                workspace = workspace.moved(StepWorkspaceState.DISCARDED)
            workspace = await remove(workspace)
        elif workspace.state in (StepWorkspaceState.MERGED, StepWorkspaceState.DISCARDED) and \
                Path(workspace.path).exists():
            # A merged one's commits are on the branch; a discarded one keeps its branch for its task.
            workspace = await remove(workspace, keep_branch=workspace.state is StepWorkspaceState.DISCARDED)
        else:
            continue
        await db_step_workspaces.save(workspace.row())
        removed += 1
    root = runtime_root / WORKTREES_DIR
    if root.is_dir():
        for task_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            for path in sorted(p for p in task_dir.iterdir() if p.is_dir()):
                if path.resolve() not in known:
                    await remove_orphan(path)
                    removed += 1
            _remove_empty(task_dir)
    return removed
