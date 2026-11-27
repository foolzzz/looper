"""Directory checks and the one-session lock for a direct task Manager.

A direct session starts only for an in-place task whose directory is its own
non-bare git repository root, is not the operator's home or a system directory,
does not contain trusted configuration, and does not overlap another live
direct session. Comparisons use the directory's device and inode, so two
spellings of one directory are the same directory. The lock file lives under
the runtime root, outside the task directory and outside the directory the
session may write. Its name is ``<device>-<inode>.lock``. The descriptor is
close-on-exec: if this process dies while the Claude process is still alive,
the kernel releases the lock at this process's exit. That early release is
accepted for v1.
"""
from __future__ import annotations

import fcntl
import os
import stat
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

# macOS temporary directories, where test and scratch repositories live, are
# under /private/var and /tmp. Those are not system directories.
SYSTEM_DIRS: tuple[str, ...] = (
    "/System", "/Library", "/usr", "/bin", "/sbin", "/etc", "/private/etc", "/dev", "/Applications", "/cores",
)


class Reason(StrEnum):
    """Refusal reason. The value is the string a direct session records."""

    WORKTREE_UNSUPPORTED = "worktree_unsupported"
    WORKSPACE_IS_HOME = "workspace_is_home"
    WORKSPACE_IN_SYSTEM_DIR = "workspace_in_system_dir"
    WORKSPACE_CONTAINS_TRUSTED_CONFIG = "workspace_contains_trusted_config"
    WORKSPACE_NOT_REPO_ROOT = "workspace_not_repo_root"
    WORKSPACE_OVERLAPS_SESSION = "workspace_overlaps_session"


class Kind(StrEnum):
    SUBDIRECTORY = "subdirectory"
    BARE = "bare"
    NOT_GIT = "not_git"
    LINKED_WORKTREE = "linked_worktree"
    WORKSPACE_MODE = "workspace_mode"


@dataclass(frozen=True)
class Problem:
    reason: Reason
    kind: Kind | None
    text: str


class LockHeld(Exception):
    def __init__(self, holder: str) -> None:
        super().__init__(holder)
        self.holder = holder


def _realpath(path: Path | str) -> Path:
    return Path(os.path.realpath(os.path.expanduser(str(path))))


def _home(environment: Mapping[str, str]) -> Path:
    raw = (environment.get("HOME") or "").strip()
    return Path(raw).expanduser() if raw else Path.home()


def _claude_dir(environment: Mapping[str, str]) -> Path:
    raw = (environment.get("CLAUDE_CONFIG_DIR") or "").strip()
    return Path(raw).expanduser() if raw else _home(environment) / ".claude"


def _config_home(environment: Mapping[str, str]) -> Path:
    raw = (environment.get("XDG_CONFIG_HOME") or "").strip()
    return Path(raw).expanduser() if raw else _home(environment) / ".config"


def _ops_file(runtime_root: Path) -> Path:
    from .. import ops_config

    return Path(ops_config.config_path(None, Path(runtime_root)))


def _dedupe(paths: Iterable[Path]) -> tuple[Path, ...]:
    found: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        real = _realpath(path)
        if real not in seen:
            seen.add(real)
            found.append(real)
    return tuple(found)


def trusted_paths(environment: Mapping[str, str], runtime_root: Path) -> tuple[Path, ...]:
    """The sandbox read-deny list, plus directories that list does not name on its own.

    ``read_deny_paths`` is the credential list, so the two cannot drift. It names files inside the Claude
    directory and some children of the configuration directory, and the runtime root rather than the ops file.
    A directory is refused when it contains an entry, and a descendant does not contain its parent, so the
    Claude directory, the configuration directory and the ops file are named here too. A missing path is still
    returned: the check compares paths and does not require the entry to exist.
    """
    from . import task_manager_sandbox

    return _dedupe((
        *(Path(path) for path in task_manager_sandbox.read_deny_paths(environment, Path(runtime_root))),
        _ops_file(runtime_root),  # configuration file; a child of the runtime root is not covered by naming the root
        _claude_dir(environment),  # the whole Claude configuration directory
        _config_home(environment),  # XDG_CONFIG_HOME, or the operator's .config
    ))


def _identity(path: Path) -> tuple[int, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def _same(left: Path, right: Path) -> bool:
    left_id = _identity(left)
    return left_id is not None and left_id == _identity(right)


def _directory_identity(path: Path) -> tuple[int, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    if not stat.S_ISDIR(st.st_mode):
        return None
    return (st.st_dev, st.st_ino)


def _flip_first_letter(name: str) -> str:
    for index, char in enumerate(name):
        if char.isalpha():
            flipped = char.lower() if char.isupper() else char.upper()
            return name[:index] + flipped + name[index + 1:]
    return name


def _folds_case(directory: Path) -> bool:
    current = directory
    while True:
        name = current.name
        if any(char.isalpha() for char in name):
            variant = current.with_name(_flip_first_letter(name))
            ident = _directory_identity(current)
            return ident is not None and ident == _directory_identity(variant)
        parent = current.parent
        if parent == current:
            return False
        current = parent


def _locate(path: Path) -> tuple[Path | None, tuple[str, ...]]:
    """The nearest existing directory and the path parts below it."""
    missing: list[str] = []
    current = path
    while True:
        if _directory_identity(current) is not None:
            return current, tuple(reversed(missing))
        parent = current.parent
        if parent == current:
            return None, tuple(reversed(missing))
        missing.append(current.name)
        current = parent


def _parts_equal(left: tuple[str, ...], right: tuple[str, ...], *, fold: bool) -> bool:
    if fold:
        left = tuple(part.casefold() for part in left)
        right = tuple(part.casefold() for part in right)
    return left == right


def _is_ancestor(ancestor: Path, descendant: Path) -> bool:
    """True when ``ancestor`` is a strict ancestor of ``descendant``.

    ``descendant`` may be missing. Existing directories on the walk are compared
    by device and inode, not by the spelling of the path.
    """
    ancestor_id = _directory_identity(ancestor)
    if ancestor_id is None:
        return False
    current = descendant
    while True:
        parent = current.parent
        if parent == current:
            return False
        if _directory_identity(parent) == ancestor_id:
            return True
        current = parent


def _contains(place: Path, entry: Path) -> bool:
    """True when ``place`` is ``entry`` or an ancestor of it.

    A missing path is its nearest existing directory plus the remaining parts.
    Those parts fold case only when that directory's filesystem does.
    """
    if _same(place, entry):
        return True
    if _is_ancestor(place, entry):
        return True
    place_dir, place_rest = _locate(place)
    entry_dir, entry_rest = _locate(entry)
    if place_dir is None or entry_dir is None or not _same(place_dir, entry_dir):
        return False
    if len(place_rest) > len(entry_rest):
        return False
    return _parts_equal(place_rest, entry_rest[:len(place_rest)], fold=_folds_case(place_dir))


def _is_system(place: Path) -> bool:
    if _same(place, Path("/")):
        return True
    system_ids = {ident for root in SYSTEM_DIRS if (ident := _directory_identity(Path(root))) is not None}
    current = place
    while True:
        ident = _directory_identity(current)
        if ident is not None and ident in system_ids:
            return True
        parent = current.parent
        if parent == current:
            return False
        current = parent


def _git_marker(place: Path) -> str | None:
    try:
        mode = (place / ".git").lstat().st_mode
    except OSError:
        return None
    if stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISLNK(mode) or stat.S_ISREG(mode):
        return "link"
    return None


def _holds_git(directory: Path) -> bool:
    return _git_marker(directory) in ("dir", "link")


def _is_bare(place: Path) -> bool:
    return (place / "HEAD").is_file() and (place / "objects").is_dir() and (place / "refs").is_dir()


def _ancestor_git(place: Path) -> Path | None:
    current = place.parent
    while True:
        if _holds_git(current):
            return current
        parent = current.parent
        if parent == current:
            return None
        current = parent


def _overlaps(place: Path, other: Path) -> bool:
    return _contains(place, other) or _contains(other, place)


def check(task: Mapping[str, Any], work_dir: str, *, environment: Mapping[str, str], runtime_root: Path,
          live: Iterable[tuple[str, str]]) -> Problem | None:
    """The first directory check that fails, or None when a direct session may use ``work_dir``."""
    if task["workspace_mode"] == "worktree":
        return Problem(
            Reason.WORKTREE_UNSUPPORTED, Kind.WORKSPACE_MODE,
            f"{work_dir}: v1 runs the task Manager's direct mode only for in-place tasks; this task works in its "
            "own worktree. Use the background (orchestrated) mode for it.")
    place = _realpath(work_dir)
    if _same(place, _realpath(_home(environment))):
        return Problem(
            Reason.WORKSPACE_IS_HOME, None,
            f"{place} is the operator's home directory. Open the task on a repository that is not the home directory.")
    if _is_system(place):
        return Problem(
            Reason.WORKSPACE_IN_SYSTEM_DIR, None,
            f"{place} is a system directory. Open the task on a repository outside system directories.")
    for entry in trusted_paths(environment, runtime_root):
        if _contains(place, entry):
            return Problem(
                Reason.WORKSPACE_CONTAINS_TRUSTED_CONFIG, None,
                f"{place} contains trusted configuration {entry}. "
                f"Open the task on a repository that does not contain {entry}.")
    marker = _git_marker(place)
    if marker == "link":
        return Problem(
            Reason.WORKTREE_UNSUPPORTED, Kind.LINKED_WORKTREE,
            f"{place}: the repository's git directory is elsewhere (a linked worktree or a submodule); the sandbox "
            "would open the shared .git. Open an in-place task whose .git is a directory inside the repository, "
            "or use the background (orchestrated) mode.")
    if marker != "dir":
        if _is_bare(place):
            return Problem(
                Reason.WORKSPACE_NOT_REPO_ROOT, Kind.BARE,
                f"{place} is a bare git repository, not a working tree. Open the task on a non-bare repository root.")
        if (root := _ancestor_git(place)) is not None:
            return Problem(
                Reason.WORKSPACE_NOT_REPO_ROOT, Kind.SUBDIRECTORY,
                f"{place} is a subdirectory of the git repository {root}. Open the task on the repository root.")
        return Problem(
            Reason.WORKSPACE_NOT_REPO_ROOT, Kind.NOT_GIT,
            f"{place} is not a git repository. Open the task on the root of a non-bare git repository.")
    return overlap(str(place), live)


def overlap(work_dir: str, live: Iterable[tuple[str, str]]) -> Problem | None:
    """The live-session overlap for ``work_dir``, or None.

    Called again after the directory lock is taken, inside admission, so a parent
    and a child that both passed the earlier check cannot both start.
    """
    place = _realpath(work_dir)
    for task_id, other in live:
        other_place = _realpath(other)
        if _overlaps(place, other_place):
            return Problem(
                Reason.WORKSPACE_OVERLAPS_SESSION, None,
                f"{place} overlaps the work directory of task {task_id} ({other_place}). Wait until that task "
                "Manager session stops, or open this task on a directory that does not contain it and is not inside it.")
    return None


async def before_directory_lock() -> None:
    """Return once the early check has passed and before admission takes its lock.

    The body does not wait. A test replaces this to hold two prepares in the window
    where both have passed the early check and neither resident has started.
    """
    return None


class DirectoryLock:
    """An exclusive lock for one directory. The file is never deleted: unlinking a flock file races."""

    def __init__(self, fd: int) -> None:
        self._fd: int | None = fd

    @classmethod
    def acquire(cls, runtime_root: Path, work_dir: str, task_id: str) -> DirectoryLock:
        directory = Path(runtime_root) / "task-manager-locks"
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        resolved = _realpath(work_dir)
        ident = _directory_identity(resolved)
        if ident is None:
            raise NotADirectoryError(str(resolved))
        device, inode = ident
        path = directory / f"{device}-{inode}.lock"
        # close-on-exec: the Claude child does not inherit the lock. If this process
        # dies first, the kernel releases the lock while that child may still be writing.
        # Accepted for v1.
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
        try:
            os.chmod(path, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                holder = os.read(fd, 4096).decode("utf-8", "replace").strip()
                raise LockHeld(holder) from None
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, task_id.encode("utf-8"))
        except Exception:
            os.close(fd)
            raise
        return cls(fd)

    def release(self) -> None:
        fd = self._fd
        if fd is None:
            return
        self._fd = None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
