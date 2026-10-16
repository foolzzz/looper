"""Where the files a Worker reports are: the evidence ``worker.judge_result`` accepts a completed report on.

A Worker reports the files it changed and deleted, relative to its working directory or absolute. Each file is
checked in this order, and the first check that holds is its evidence (``Evidence``):

1. ``on_disk``: where the report says (the working directory, or the absolute path), a changed file exists and
   a deleted one does not. A working directory outside git stops here: any other file is missing;
2. ``in_worktree``: a relative path, at the same place in another worktree of the working directory's
   repository (``git worktree list``). A changed file exists there; a deleted one is gone there and is in that
   worktree's ``HEAD``, so it was deleted there;
3. ``committed``: a commit made during the run, on any branch, tag or remote-tracking branch (``git log
   --all``), added or modified the changed file, or deleted the deleted one. A path is the repository path at
   the same place, and an absolute changed path in no worktree of the repository (its worktree was removed)
   is matched by its last components.

A file none of these accounts for is ``missing``: the step fails, and the failure says where the check looked.
Git is asked only when the disk does not account for every file: at most four commands, each with
``GIT_TIMEOUT_SEC``, over at most ``MAX_WORKTREES`` worktrees and ``MAX_COMMITS`` commits.
"""

from __future__ import annotations

import os
import posixpath
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path

GIT_TIMEOUT_SEC = 10
MAX_WORKTREES = 64
MAX_COMMITS = 500
# Commit times are whole seconds and may come from another clock: a commit this long before the run's start counts.
CLOCK_SLACK = timedelta(seconds=60)
# Inherited variables that would point the commands at another repository or index.
_REDIRECTS = frozenset({"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"})


class Evidence(str, Enum):
    ON_DISK = "on_disk"
    IN_WORKTREE = "in_worktree"
    COMMITTED = "committed"
    MISSING = "missing"  # a changed file found nowhere, or a deleted file still present


@dataclass(frozen=True)
class FileCheck:
    path: str  # as reported
    deleted: bool  # reported in deleted_files (else in changed_files)
    evidence: Evidence
    where: str = ""  # in_worktree: the worktree's path; committed: the commit's hash


@dataclass(frozen=True)
class FilesCheck:
    files: tuple[FileCheck, ...]
    looked: str = ""  # where git was asked, when it was: for the failure message

    def missing(self, *, deleted: bool) -> list[str]:
        return [f.path for f in self.files if f.evidence is Evidence.MISSING and f.deleted is deleted]

    def elsewhere(self) -> dict[str, str]:
        """The files found outside where the report says, and where: for the run's activity."""
        return {f.path: ("deleted " if f.deleted else "")
                + (f"in worktree {f.where}" if f.evidence is Evidence.IN_WORKTREE else f"in commit {f.where[:12]}")
                for f in self.files if f.evidence in (Evidence.IN_WORKTREE, Evidence.COMMITTED)}


@dataclass(frozen=True)
class _Worktree:
    path: Path
    head: str  # '' on an unborn branch


def check(changed: Sequence[str], deleted: Sequence[str], *, work_dir: Path,
          since: datetime | None) -> FilesCheck:
    """Each reported file's evidence; ``since`` is the run's start (None: commits are not looked at)."""
    reported = list(dict.fromkeys([(str(p), False) for p in changed] + [(str(p), True) for p in deleted]))
    found: dict[tuple[str, bool], FileCheck] = {}

    def settle(path: str, gone: bool, evidence: Evidence, where: str = "") -> None:
        found[(path, gone)] = FileCheck(path, gone, evidence, where)

    def result(looked: str = "") -> FilesCheck:
        return FilesCheck(tuple(found.get(item) or FileCheck(*item, Evidence.MISSING) for item in reported), looked)

    for path, gone in reported:
        if _at(work_dir, path).exists() is not gone:
            settle(path, gone, Evidence.ON_DISK)
    if len(found) == len(reported):
        return result()
    where = _git(work_dir, "rev-parse", "--show-toplevel", "--show-prefix")
    if where is None or where.returncode != 0:
        return result()  # outside git: the disk is the only evidence
    lines = where.stdout.decode("utf-8", "surrogateescape").split("\n")
    toplevel, prefix = Path(lines[0]).resolve(), lines[1] if len(lines) > 1 else ""

    looked = ["the working directory"]
    worktrees, listed = _worktrees(work_dir, toplevel)
    looked.append(listed)
    in_head = _in_heads(work_dir, worktrees, [_repo_path(prefix, path) for path, gone in reported
                                               if gone and (path, gone) not in found and not Path(path).is_absolute()])
    for path, gone in reported:
        repo_path = _repo_path(prefix, path) if not Path(path).is_absolute() else None
        if (path, gone) in found or repo_path is None:
            continue
        for worktree in worktrees:
            there = (worktree.path / repo_path).exists()
            if (there and not gone) or (gone and not there and (worktree.head, repo_path) in in_head):
                settle(path, gone, Evidence.IN_WORKTREE, str(worktree.path))
                break

    if since is not None and len(found) < len(reported):
        commits, scanned = _commits(work_dir, since - CLOCK_SLACK)
        looked.append(scanned)
        roots = sorted([toplevel, *(w.path for w in worktrees)], key=lambda root: len(root.parts), reverse=True)
        for path, gone in reported:
            if (path, gone) in found:
                continue
            names = _commit_names(prefix, path, gone, roots)
            for sha, files in commits:
                if any(files.get(name) is not None and (files[name] == "D") is gone for name in names):
                    settle(path, gone, Evidence.COMMITTED, sha)
                    break
    return result("looked in " + "; ".join(looked))


def _at(work_dir: Path, path: str) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else work_dir / candidate


def _repo_path(prefix: str, path: str) -> str | None:
    """A path relative to the working directory, relative to its repository's top level; None outside it."""
    name = posixpath.normpath(posixpath.join(prefix, Path(path).as_posix()))
    return None if name == "." or name == ".." or name.startswith("../") else name


def _commit_names(prefix: str, path: str, gone: bool, roots: Sequence[Path]) -> set[str]:
    """The repository paths a commit could have changed ``path`` as."""
    if not Path(path).is_absolute():
        name = _repo_path(prefix, path)
        return {name} if name else set()
    resolved = Path(path).parent.resolve() / Path(path).name  # not through a symlink the path itself is
    for root in roots:  # the deepest first: a worktree can be inside another
        if resolved.is_relative_to(root):
            return {resolved.relative_to(root).as_posix()}
    if gone:  # still present where reported, so not in a removed worktree
        return set()
    parts = resolved.parts[1:]
    return {"/".join(parts[i:]) for i in range(len(parts))}


def _worktrees(work_dir: Path, toplevel: Path) -> tuple[list[_Worktree], str]:
    """The repository's other worktrees with a checkout, and how many were looked in."""
    listed = _git(work_dir, "worktree", "list", "--porcelain", "-z")
    if listed is None or listed.returncode != 0:
        return [], f"could not look in other worktrees: {_failure('worktree list', listed)}"
    worktrees: list[_Worktree] = []
    entry: dict[str, str] = {}
    for field in listed.stdout.decode("utf-8", "surrogateescape").split("\0") + [""]:
        if field:
            key, _, value = field.partition(" ")
            entry[key] = value
            continue
        path = Path(entry.get("worktree") or "/").resolve()
        # A bare repository has no checkout; a prunable worktree's directory is gone.
        if "worktree" in entry and "bare" not in entry and path != toplevel and path.is_dir():
            head = entry.get("HEAD", "")
            worktrees.append(_Worktree(path, head if head.strip("0") else ""))
        entry = {}
    if not worktrees:
        return [], "in no other worktree of its repository"
    shown = worktrees[:MAX_WORKTREES]
    total = f"the first {len(shown)} of {len(worktrees)}" if len(worktrees) > len(shown) else str(len(shown))
    paths = ", ".join(str(w.path) for w in shown[:5]) + (f" (+{len(shown) - 5} more)" if len(shown) > 5 else "")
    return shown, f"in {total} other worktree{'s' if len(worktrees) > 1 else ''} of its repository: {paths}"


def _in_heads(work_dir: Path, worktrees: Sequence[_Worktree], names: Sequence[str | None]) -> set[tuple[str, str]]:
    """The (HEAD, path) pairs where the worktree's HEAD has the path: one ``git cat-file`` for all of them."""
    pairs = [(w.head, name) for name in dict.fromkeys(names) if name and "\n" not in name
             for w in worktrees if w.head]
    if not pairs:
        return set()
    batch = _git(work_dir, "cat-file", "--batch-check", input="".join(f"{head}:{name}\n" for head, name in pairs))
    if batch is None or batch.returncode != 0:
        return set()
    answers = batch.stdout.decode("utf-8", "surrogateescape").split("\n")
    return {pair for pair, answer in zip(pairs, answers) if answer and not answer.endswith(" missing")}


def _commits(work_dir: Path, since: datetime) -> tuple[list[tuple[str, dict[str, str]]], str]:
    """The commits made since ``since`` on any ref, newest first, each with its paths' status (A, M, T or D)."""
    when = since.astimezone(timezone.utc)
    logged = _git(work_dir, "log", "--all", f"--max-age={int(when.timestamp())}", "--no-renames", "--name-status",
                  "-z", "--format=%H", f"--max-count={MAX_COMMITS}")
    if logged is None or logged.returncode != 0:
        return [], f"could not look in commits: {_failure('log', logged)}"
    commits: list[tuple[str, dict[str, str]]] = []
    fields = logged.stdout.decode("utf-8", "surrogateescape").split("\0")
    index = 0
    while index < len(fields):
        field = fields[index].lstrip("\n")
        index += 1
        if not field:
            continue
        if len(field) <= 2 and commits:  # a status; its path follows
            commits[-1][1].setdefault(fields[index] if index < len(fields) else "", field[0])
            index += 1
        else:
            commits.append((field, {}))
    count = f"the latest {MAX_COMMITS}" if len(commits) >= MAX_COMMITS else str(len(commits))
    return commits, (f"in {count} commit{'' if len(commits) == 1 else 's'} made on any branch, tag or "
                     f"remote-tracking branch since {when:%Y-%m-%dT%H:%M:%SZ}")


def _git(cwd: Path, *args: str, input: str | None = None) -> subprocess.CompletedProcess[bytes] | None:
    """One read-only git command in ``cwd``, with the time limit; None when git cannot run or times out."""
    env = {name: value for name, value in os.environ.items() if name not in _REDIRECTS}
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"})
    try:
        return subprocess.run(["git", *args], cwd=str(cwd), env=env, capture_output=True, timeout=GIT_TIMEOUT_SEC,
                              input=input.encode("utf-8", "surrogateescape") if input is not None else None,
                              stdin=None if input is not None else subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _failure(command: str, run: subprocess.CompletedProcess[bytes] | None) -> str:
    if run is None:
        return f"git {command} did not run in {GIT_TIMEOUT_SEC} s"
    return f"git {command} failed: {(run.stderr or run.stdout).decode('utf-8', 'replace').strip()[:200]}"
