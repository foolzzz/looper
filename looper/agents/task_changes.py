"""Bounded file metadata for a task; every command uses the shared read-only git runner."""

from collections.abc import Iterator
from itertools import chain
from pathlib import Path, PurePosixPath

from .. import git_safe
from ..db import tasks, task_worktrees
from . import step_workspaces as git
from .projects import HTTP_404, RequestError


def _entries(data: bytes, subdir: str, *, porcelain: bool = False,
             untracked: bool = False) -> Iterator[dict]:
    """Parse NUL records lazily, keeping rename pairs together and paths inside the task."""
    def fields():
        start = 0
        while start < len(data):
            end = data.find(b"\0", start)
            if end < 0:
                raise git.GitError("incomplete git path record")
            yield data[start:end].decode("utf-8", "surrogateescape")
            start = end + 1

    def relative(path):
        parts = PurePosixPath(path)
        if not path or parts.is_absolute() or ".." in parts.parts:
            return None
        try:
            value = parts.relative_to(subdir) if subdir else parts
        except ValueError:
            return None
        return str(value) if str(value) != "." else None

    records = iter(fields())
    statuses = {"A": "added", "M": "modified", "D": "deleted", "R": "renamed", "C": "copied",
                "T": "type_changed", "U": "conflicted", "?": "untracked"}
    for record in records:
        old = None
        if untracked:
            status, path = "untracked", record
        elif porcelain:
            xy, path = record[:2], record[3:]
            code = next((c for c in xy if c in "RC"), None)
            if code:
                old = next(records, None)  # porcelain -z puts the destination first
                if old is None:
                    raise git.GitError("incomplete git rename")
            if "U" in xy or xy in ("AA", "DD"):
                status = "conflicted"
            else:
                status = statuses.get(code or xy.strip()[:1], "unknown")
        else:
            code, path = record[:1], next(records, None)
            if path is None:
                raise git.GitError("incomplete git change")
            status = statuses.get(code, "unknown")
            if code in ("R", "C"):
                old, path = path, next(records, None)
                if path is None:
                    raise git.GitError("incomplete git rename")
        path, old_path = relative(path), relative(old) if old is not None else None
        if old is not None and path is None and old_path is not None and status == "renamed":
            path, old_path, status = old_path, None, "deleted"
        elif old is not None and path is not None and old_path is None:
            status = "added"
        if path is not None:
            yield {"path": path, "status": status, "old_path": old_path}


@git_safe.attributed
async def read(task_id: str, *, limit: int = 100) -> dict:
    """Read at most limit files (hard maximum 200); never prepare or reopen a workspace.

    The shared runner still buffers stdout. Only parsed/returned entries are bounded.
    """
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("limit must be an integer from 1 to 200")
    task = await tasks.get_task(task_id)
    if task is None:
        raise RequestError(HTTP_404, "task not found")
    row = await task_worktrees.get(task_id)
    unavailable = {"task_id": task_id, "kind": "unavailable", "scope": None, "files": [],
                   "truncated": False, "reason": "workspace_missing"}
    if row is None and task.get("workspace_mode") == "worktree":
        return unavailable
    removed = row is not None and row["state"] == "removed"
    directory = (row["repo_dir"] if removed else row["path"]) if row else (
        task.get("workspace_dir") or task.get("work_dir"))
    if not directory or not Path(directory).is_dir():
        return unavailable
    # Disable optional index refreshes for status; inherited repository redirects are cleared by _git.
    env = {"GIT_OPTIONAL_LOCKS": "0", "GIT_LITERAL_PATHSPECS": "0", "GIT_GLOB_PATHSPECS": "0",
           "GIT_NOGLOB_PATHSPECS": "0", "GIT_ICASE_PATHSPECS": "0"}
    try:
        detected = await git._git(directory, "rev-parse", "--show-toplevel", check=False, env=env)
        if detected.returncode:
            return {**unavailable, "reason": "not_git" if detected.returncode == 128 else "git_error"}
        top = Path(detected.stdout.decode("utf-8", "surrogateescape").removesuffix("\n")).resolve()
        subdir = str(row.get("subdir") or "") if row else Path(directory).resolve().relative_to(top).as_posix()
        if subdir == ".":
            subdir = ""
        if PurePosixPath(subdir).is_absolute() or ".." in PurePosixPath(subdir).parts:
            return unavailable
        if row and not removed and not (top / subdir).is_dir():
            return unavailable
        pathspec = f":(top,literal){subdir}" if subdir else ":(top)"
        if row:
            ref = "refs/heads/" + row["branch"].removeprefix("refs/heads/")
            tip = await git._git(top, "rev-parse", "--verify", "--quiet", "--end-of-options",
                                 f"{ref}^{{commit}}", check=False, env=env)
            if tip.returncode:
                return {**unavailable, "reason": "branch_missing" if tip.returncode == 1 else "git_error"}
            if not removed:
                attached = await git._git(top, "symbolic-ref", "--quiet", "HEAD", check=False, env=env)
                if attached.returncode not in (0, 1):
                    raise git.GitError("cannot read HEAD branch")
                head = await git._git(top, "rev-parse", "--verify", "HEAD^{commit}", env=env)
                if (attached.returncode == 0 and attached.text != ref) or (
                        attached.returncode == 1 and head.text != tip.text):
                    return {**unavailable, "reason": "branch_changed"}
            base = await git._git(top, "rev-parse", "--verify", "--end-of-options",
                                  f"{row['base_commit']}^{{commit}}", env=env)
            diff = await git._git(top, "diff", "--no-ext-diff", "--no-textconv", "--name-status", "-z",
                                  "--find-renames", base.text, *((tip.text,) if removed else ()),
                                  "--", pathspec, env=env)
            entries = _entries(diff.stdout, subdir)
            if not removed:
                conflicts = await git._git(top, "diff", "--no-ext-diff", "--no-textconv", "--name-status", "-z",
                                           "--diff-filter=U", "--", pathspec, env=env)
                others = await git._git(top, "ls-files", "--others", "--exclude-standard", "-z",
                                        "--", pathspec, env=env)
                # Diff against a commit reports conflicted paths as M; preserve the index's U before deduplication.
                entries = chain(_entries(conflicts.stdout, subdir), entries,
                                _entries(others.stdout, subdir, untracked=True))
            scope = "task_branch"
        else:
            pending = await git._git(top, "status", "--porcelain=v1", "-z", "--untracked-files=all",
                                     "--", pathspec, env=env)
            entries, scope = _entries(pending.stdout, subdir, porcelain=True), "working_tree"
        files = {}
        for entry in entries:
            files.setdefault(entry["path"], entry)
            if len(files) > limit:
                break
        return {"task_id": task_id, "kind": "ok", "scope": scope, "files": list(files.values())[:limit],
                "truncated": len(files) > limit, "reason": None}
    except (git.GitError, OSError, ValueError):
        return {**unavailable, "reason": "git_error"}
