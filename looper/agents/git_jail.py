"""The macOS Seatbelt profile every ``git_safe`` command runs under.

A task Manager's direct session edits the user's repository itself, and what it leaves in ``.git`` stays there for
every later git call Looper makes on that repository. Checking the files git might open cannot cover them all, so on
macOS the kernel enforces the boundary instead, for every ``git_safe`` command (the probes too), whichever task or
code path starts it:

* reads: everything except the direct session's own read deny list (``task_manager_sandbox.read_deny_paths``, F10);
  a denied read kills git with SIGKILL;
* writes: only the repository's own directories and the typed write targets below, never the F11 parts of the common
  git directory; any other write kills git with SIGKILL;
* network: none.

The profile cannot see a hard link made to a denied file by a process outside the sandbox (Seatbelt judges paths),
and it does not cover what Looper reads in its own process. Other platforms are unchanged: direct mode is refused
there.
"""
from __future__ import annotations

import functools
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import task_manager_sandbox

# Read once at import: a test that fakes ``sys.platform`` must not switch the jail of a running process.
ENABLED = sys.platform == "darwin"
SANDBOX_EXEC = "/usr/bin/sandbox-exec"
# What ``sandbox-exec`` itself exits with: a profile it cannot apply (65), a program it cannot start (71). Git's own
# commands exit with neither.
UNAVAILABLE_EXIT_CODES = (65, 71)
# SIGKILL as ``subprocess`` reports it.
KILLED = -9
_SELF_CHECK_TIMEOUT_SEC = 10

# Where Looper creates checkouts, under a runtime root.
_WORKSPACE_AREAS = ("worktrees", "task-worktrees", ".local/consults", ".local/explorers", ".local/verification")
# The prefix of the temporary directories Looper keeps a private index in.
_PRIVATE_INDEX_PREFIX = "looper-"


class JailRefused(ValueError):
    """A path that may not become part of the profile; ``reason`` is ``sandbox_denied``."""

    reason = "sandbox_denied"


# --- Process settings: where Looper's runtime root is, and what else sessions were denied ---

_lock = threading.Lock()
_runtime_root: Path | None = None
_config_file: Path | None = None
_projects_root: str | None = None
_workspace_roots: set[str] = set()
_session_denies: set[str] = set()


def configure(runtime_root: Path, config_file: Path | None = None, projects_root: str = "") -> None:
    """The server's runtime root and ops file, which the deny list and the workspace places are made from, and its
    projects root (repositories there stay readable when a denied path holds them)."""
    global _runtime_root, _config_file, _projects_root
    _runtime_root, _config_file = Path(runtime_root), Path(config_file) if config_file else None
    _projects_root = os.path.realpath(projects_root) if projects_root else None
    add_workspace_root(runtime_root)


def add_workspace_root(runtime_root: str | Path) -> None:
    """Looper creates checkouts under ``runtime_root``: their places may be named as write targets."""
    with _lock:
        _workspace_roots.add(os.path.realpath(runtime_root))


def add_session_denies(paths: Iterable[str]) -> None:
    """A direct session was launched with these denied reads (its environment may name more than the server's):
    Looper's git is denied them too, for the life of the process."""
    with _lock:
        _session_denies.update(os.path.realpath(path) for path in paths)


def _configured_runtime_root() -> Path:
    if _runtime_root is not None:
        return _runtime_root
    from .. import ops_config

    return ops_config.runtime_root()[0]


def read_deny() -> tuple[str, ...]:
    """The denied reads: the session's list (F10) for the server's environment, and every launched session's."""
    found = task_manager_sandbox.read_deny_paths(os.environ, _configured_runtime_root(), _config_file)
    with _lock:
        return tuple(sorted({*found, *_session_denies}))


def _under(path: str, parent: str) -> bool:
    return path == parent or path.startswith(parent.rstrip("/") + "/")


def workspace_areas() -> tuple[str, ...]:
    with _lock:
        roots = set(_workspace_roots)
    roots.add(os.path.realpath(_configured_runtime_root()))
    return tuple(sorted(os.path.join(root, area) for root in roots for area in _WORKSPACE_AREAS))


def _own_places() -> tuple[str, ...]:
    """Places under which a repository stays readable even when a denied path holds them."""
    return (*workspace_areas(), *([_projects_root] if _projects_root else []))


def _inside_area(path: str) -> bool:
    return any(path != area and _under(path, area) for area in workspace_areas())


# --- Typed write targets: what a command may write outside the repository's own directories ---


@dataclass(frozen=True)
class WorktreeTarget:
    """``git worktree add``: the checkout, inside a Looper workspace place."""

    path: str


@dataclass(frozen=True)
class RemoveTarget:
    """``git worktree remove``: the checkout, inside a Looper workspace place, or a linked worktree this repository
    registered (a project move can take a task's worktree elsewhere)."""

    path: str


@dataclass(frozen=True)
class CheckoutPrefix:
    """``git checkout-index --prefix``: the snapshot directory, inside a Looper workspace place."""

    path: str


@dataclass(frozen=True)
class CloneDestination:
    """``git clone``: the new repository, inside a Looper workspace place."""

    path: str


@dataclass(frozen=True)
class OwnClone:
    """A clone Looper made in a workspace place, whose configuration it edits (``git remote remove``)."""

    path: str


@dataclass(frozen=True)
class RepairTarget:
    """``git worktree repair``: a checkout of the repository, whose ``.git`` link file alone is written."""

    path: str


@dataclass(frozen=True)
class PrivateIndex:
    """``GIT_INDEX_FILE`` in a temporary directory Looper made (git writes its lock beside it)."""

    path: str


@dataclass(frozen=True)
class BranchSections:
    """``git branch -d``/``-D``: the common git directory, whose configuration loses the deleted branch's section."""

    common: str


WriteTarget = (WorktreeTarget | RemoveTarget | CheckoutPrefix | CloneDestination | OwnClone | RepairTarget | PrivateIndex
               | BranchSections)


def _resolved(path: str) -> str:
    if not os.path.isabs(path) or '"' in path or "\0" in path:
        raise JailRefused("a write target must be an absolute path")
    return os.path.realpath(path)


def _registered(checkout: str, common: str) -> bool:
    """Whether ``checkout`` is a linked worktree of ``common``, as both of its links say."""
    link = os.path.join(checkout, ".git")
    try:
        if os.path.islink(link):
            return False
        line = Path(link).read_text().splitlines()[0]
        entry = Path(os.path.realpath(os.path.join(checkout, line.removeprefix("gitdir:").strip())))
        back = os.path.realpath((entry / "gitdir").read_text().strip())
    except (OSError, UnicodeError, IndexError):
        return False
    return str(entry.parent) == os.path.join(common, "worktrees") and back == link


def _validated(target: object, common: str | None = None) -> tuple[str, str]:
    """``(filter, real path)`` the target may write; ``TypeError`` for anything but a typed target, ``JailRefused``
    for a path outside the places its kind allows."""
    if isinstance(target, RemoveTarget):
        real = _resolved(target.path)
        if _inside_area(real) or (common is not None and _registered(real, os.path.realpath(common))
                                  and not any(_under(real, denied) or _under(denied, real) for denied in read_deny())):
            return "subpath", real
        raise JailRefused("the checkout to remove is not a worktree Looper may delete")
    if isinstance(target, (WorktreeTarget, CheckoutPrefix, CloneDestination, OwnClone)):
        real = _resolved(target.path)
        if not _inside_area(real):
            raise JailRefused("the write target is not in a place Looper creates checkouts in")
        return "subpath", real
    if isinstance(target, RepairTarget):
        link = os.path.join(_resolved(target.path), ".git")
        if os.path.islink(link) or not os.path.isfile(link):
            raise JailRefused("the checkout to repair has no .git link file")
        if any(_under(link, denied) for denied in read_deny()) and not _inside_area(link):
            raise JailRefused("the checkout to repair is under a denied path")
        return "literal", link
    if isinstance(target, PrivateIndex):
        directory = os.path.dirname(_resolved(target.path))
        temporary = os.path.realpath(tempfile.gettempdir())
        if os.path.dirname(directory) != temporary or not os.path.basename(directory).startswith(_PRIVATE_INDEX_PREFIX):
            raise JailRefused("a private index must be in a temporary directory Looper made")
        return "subpath", directory
    raise TypeError(f"not a typed write target: {type(target).__name__}")


# --- The profile ---


# The process, Mach, sysctl and ioctl rules of the session's own profile (Claude Code's Bash sandbox on macOS).
_BASE = """(version 1)
(deny default)
(allow process-exec)
(allow process-fork)
(allow process-info* (target same-sandbox))
(if (defined? 'process-info-listpids) (allow process-info-listpids))
(allow signal (target same-sandbox))
(allow mach-priv-task-port (target same-sandbox))
(allow user-preference-read)
(allow mach-lookup
  (global-name "com.apple.logd")
  (global-name "com.apple.system.logger")
  (global-name "com.apple.system.notification_center")
  (global-name "com.apple.system.opendirectoryd.libinfo")
  (global-name "com.apple.system.opendirectoryd.membership")
  (global-name "com.apple.bsd.dirhelper"))
(allow ipc-posix-shm)
(allow ipc-posix-sem)
(allow sysctl-read)
(deny sysctl-read (sysctl-name-prefix "kern.procargs"))
(allow file-ioctl (literal "/dev/null") (literal "/dev/zero") (literal "/dev/random") (literal "/dev/urandom")
  (literal "/dev/dtracehelper") (literal "/dev/tty"))
(allow file-ioctl file-read-data file-write-data (require-all (literal "/dev/null") (vnode-type CHARACTER-DEVICE)))"""

_KILL = "(with send-signal SIGKILL)"


def _quote(path: str) -> str:
    return json.dumps(path, ensure_ascii=False)


def _ordered(operation: str, allowed: Iterable[tuple[str, str]], denied: Iterable[str]) -> list[str]:
    """Rules from the shallowest path to the deepest (a later rule wins, so the most specific path decides; a deny
    wins over an allow of the same path)."""
    rules = [(path.count("/"), 0, f"(allow {operation} ({kind} {_quote(path)}))") for kind, path in allowed]
    rules += [(path.count("/"), 1, f"(deny {operation} (subpath {_quote(path)}) {_KILL})") for path in denied]
    return [rule for *_, rule in sorted(rules)]


@functools.lru_cache(maxsize=256)
def _compile(denied: tuple[str, ...], readable: tuple[str, ...], writable: tuple[tuple[str, str], ...],
             common: str | None, own_config: bool) -> str:
    lines = [_BASE, "(allow file-read*)"]
    lines += [f"(deny file-read* (subpath {_quote(path)}) {_KILL})" for path in denied]
    lines.append("(allow file-read-metadata (vnode-type DIRECTORY))")
    # The roots stay readable under a denied path; a denied path inside a root stays denied.
    nested = [path for path in denied if any(_under(path, root) for root in readable)]
    lines += _ordered("file-read*", [("subpath", root) for root in readable], nested)
    # /dev is left to the default deny: dyld opens /dev/dtracehelper for writing in every process.
    lines.append(f'(deny file-write* (require-not (subpath "/dev")) {_KILL})')
    lines.append('(allow file-write* (literal "/dev/null"))')
    protected = []
    if common is not None:
        protected = [f"{common}/{part}" for part in task_manager_sandbox.GIT_DENY_WRITE_PARTS
                     if part != "modules" and not (own_config and part == "config")]
    inside = [path for path in denied if any(_under(path, root) for _, root in writable)]
    lines += _ordered("file-write*", writable, [*protected, *inside])
    if common is not None:
        # A superproject command refreshes a submodule's index under modules/, so only what F11 protects in a
        # repository is denied there.
        parts = "|".join(re.escape(part) for part in (*(p for p in task_manager_sandbox.GIT_DENY_WRITE_PARTS
                                                        if p != "modules"), "config.worktree"))
        lines.append(f'(deny file-write* (regex #"^{re.escape(common)}/modules/(.*/)?({parts})(/|$)") {_KILL})')
    return "\n".join(lines)


def profile(roots: Sequence[str] = (), *, common: str | None = None, targets: Sequence[WriteTarget] = (),
            reads: Sequence[str] = ()) -> str:
    """The profile for a command that may write ``roots`` (the work tree and git directories, none for a probe) and
    ``targets``, and read ``reads`` besides everything not denied. A root, or a read, under a denied path is refused
    unless it is in one of Looper's own places."""
    denied = read_deny()
    own = _own_places()

    def checked(path: str) -> str:
        real = _resolved(path)
        if any(_under(real, entry) for entry in denied) and not any(_under(real, place) for place in own):
            raise JailRefused("the repository is under a path Looper's git may not read")
        return real

    real_roots = [checked(root) for root in roots]
    writable = [("subpath", root) for root in real_roots]
    own_config = False
    for target in targets:
        if isinstance(target, BranchSections):  # inside the roots already; only the protection of ``config`` goes
            if common is None or _resolved(target.common) != _resolved(common):
                raise JailRefused("the branch's configuration is not this repository's")
            own_config = True
            continue
        writable.append(_validated(target, common))
        own_config = own_config or isinstance(target, OwnClone)
    readable = {*real_roots, *workspace_areas(), *(path for _, path in writable)}
    readable.update(checked(path) for path in reads)
    return _compile(denied, tuple(sorted(readable)), tuple(sorted(set(writable))),
                    _resolved(common) if common else None, own_config)


@functools.lru_cache(maxsize=8)
def _program(search_path: str | None, program: str = "git") -> str:
    """``program`` (``git`` or a path to it) as the sandbox can run it: Apple's ``/usr/bin/git`` is a shim that writes
    a lookup cache, so the git it stands for is asked for once, outside the sandbox."""
    import shutil

    found = shutil.which(program, path=search_path)
    if found != "/usr/bin/git":
        return program
    done = subprocess.run(["/usr/bin/xcrun", "--find", "git"], capture_output=True, text=True,
                          timeout=_SELF_CHECK_TIMEOUT_SEC, stdin=subprocess.DEVNULL)
    return done.stdout.strip() if done.returncode == 0 and done.stdout.strip() else program


def wrap(command: Sequence[str], profile_text: str) -> list[str]:
    """``command`` (an argv starting with ``git`` or its path) under ``sandbox-exec`` with ``profile_text``."""
    return [SANDBOX_EXEC, "-p", profile_text, _program(os.environ.get("PATH"), command[0]), *command[1:]]


def self_check() -> str | None:
    """Why the jail does not work on this machine, or None. It must kill a read of a denied synthetic file, let a read
    of an allowed one through, and run git."""
    if not os.access(SANDBOX_EXEC, os.X_OK):
        return f"{SANDBOX_EXEC} is missing"
    try:
        with tempfile.TemporaryDirectory(prefix="looper-jail-check-") as directory:
            root = os.path.realpath(directory)
            allowed, denied = os.path.join(root, "allowed"), os.path.join(root, "denied")
            Path(allowed).write_text("allowed\n")
            Path(denied).write_text("synthetic\n")
            text = _compile((denied,), (), (), None, False)

            def confined(*command: str) -> subprocess.CompletedProcess:
                return subprocess.run([SANDBOX_EXEC, "-p", text, *command], capture_output=True,
                                      stdin=subprocess.DEVNULL, timeout=_SELF_CHECK_TIMEOUT_SEC)

            done = confined("/bin/cat", allowed)
            if done.returncode != 0 or done.stdout != b"allowed\n":
                return f"a confined read of an allowed file failed (exit {done.returncode})"
            done = confined("/bin/cat", denied)
            if done.returncode != KILLED or done.stdout:
                return f"a confined read of a denied file was not killed (exit {done.returncode})"
            done = confined(_program(os.environ.get("PATH")), "--version")
            if done.returncode != 0:
                return f"git does not run confined (exit {done.returncode})"
    except (OSError, subprocess.SubprocessError) as error:
        return f"the check could not run ({type(error).__name__})"
    return None
