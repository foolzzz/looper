"""Branch and tag changes of a direct session's turn: recorded outside the task directory, reported, never restored.

In an in-place direct session the model's Bash writes the real checkout, ``.git`` included, so a turn can move a
branch, delete a tag or switch ``HEAD``. Looper cannot prevent that inside the sandbox (RFC 25.5), so it records
``refs/heads`` and ``refs/tags`` and ``HEAD`` before every turn, in memory (the sandbox cannot reach it), compares them
after, and says in the task conversation what changed, with one restore command per ref. It never restores anything:
the operator's checkout is the operator's. A change to a branch or tag other than the task branch also goes to "needs
you"; it is not an invariant violation and raises no alert (RFC 21, 25.10).

The task branch is recorded once, at the first enablement. A turn refuses to start while ``HEAD`` is not on it.
Every git call goes through ``git_safe`` (no repository code runs). Only ``refs/heads`` and ``refs/tags`` are listed
and compared; any other ref (``refs/remotes`` too) is read only as a hop of a symbolic ref's chain.

Looper opens ref files itself only through descriptors under the git directory, never through a link
(``_ref_files``). git does follow links there, so a ref whose file or directory Looper refuses, and every link that
reads through one, is left out of the snapshot (``Snapshot.unreadable``): nothing git said about it is kept. git
also opens a symbolic ref's target outside ``refs/`` (``x/y``, ``worktrees/w/y``); Looper reads none but ``HEAD``
and the other root refs, so every other such name is refused the same way.

A symbolic ref is recorded hop by hop (``Snapshot.hops``). Refs that such chains tie together are restored by one
command: a single ``git update-ref`` transaction that checks every hop it reads through and puts back every hop that
changed, or changes nothing. When the chains cannot be known, the report says so (``NotRestorable``) instead; a
change is still listed, told apart by what Looper's own read of the ref files shows (``Snapshot.spelled``).

Snapshots cannot tell which of two turns on one repository changed a ref, so a turn whose time overlapped another
task's turn there gets no command at all (``open_turn``, ``close_turn``).
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import stat
import unicodedata
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import git_safe, safe_read, task_manager_safety
from ..db import task_manager_refs as store

if TYPE_CHECKING:
    from .tiers.task_manager import TurnSink

logger = logging.getLogger(__name__)

GIT_TIMEOUT_SEC = 30
_LOCKLESS = {"GIT_OPTIONAL_LOCKS": "0"}
HEADS, TAGS, LOOPER = "refs/heads/", "refs/tags/", "refs/heads/looper/"
COMPARED = (HEADS, TAGS)
FINISHED_STATUSES = frozenset({"completed", "failed", "cancelled", "stopped"})
FIRST_ENABLE_NOTICE = "first_enable_in_place"
REFS_UNREADABLE_NOTICE = "refs_unreadable"
NOTICES = (FIRST_ENABLE_NOTICE, REFS_UNREADABLE_NOTICE)
REFS_UNREADABLE_DETAIL = ("The branches and tags could not be read again after this turn, so any change this turn made "
                          "to them is not reported. Check them yourself.")
# What ``str.splitlines`` splits on (besides \n).
_LINE_BREAKS = frozenset("\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029")
WALK_LIMIT = 20000  # commits read when deciding fast-forward
OBJECT_LIMIT = 1 << 20  # a commit object larger than this ends the walk undecided
SYMREF_LIMIT = 200  # symbolic refs asked for their direct target (one git process each) in one snapshot
SYMREF_GIT = (2, 46)  # the first git whose ``update-ref --stdin`` checks and writes symbolic refs
REF_FILE_LIMIT = 4096  # bytes of a loose ref file; a larger one is not read
_ROOTS = ("refs/heads", "refs/tags")
_PER_WORKTREE = ("refs/bisect/", "refs/worktree/", "refs/rewritten/")  # in the git directory, not the common one
_ROOT_REF = re.compile(r"[A-Z0-9_-]+")  # a ref beside ``HEAD``, such as ``ORIG_HEAD``
# What git's ref-name rules forbid, as far as a name used as a path must be checked.
_NOT_A_REF_NAME = re.compile(r"\.\.|//|/\.|\.lock(/|$)|[\x00-\x20\x7f~^:?*\[\\]|[/.]$")
_OID = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


def _odd(ch: str) -> bool:
    """A control or format character (bidirectional controls, zero-width characters): invisible or reordering."""
    return unicodedata.category(ch) in ("Cc", "Cf")
FIRST_ENABLE_DETAIL = ("The task Manager edits files directly in your real checkout. v1 only reports changes to "
                       "branches and tags; it never restores them, and deleted objects cannot be recovered. Push or "
                       "back up important branches first.")


class Change(str, Enum):
    CREATED = "created"
    DELETED = "deleted"
    MOVED = "moved"
    NON_FAST_FORWARD = "non_fast_forward"
    FAST_FORWARD_UNVERIFIED = "fast_forward_unverified"  # the bounded walk gave up: neither a reset nor a fast-forward is claimed
    UNREADABLE = "unreadable"  # not read, so nothing is claimed about it


class Ancestry(Enum):
    DESCENDS = "descends"
    DIVERGES = "diverges"
    UNDECIDED = "undecided"


class NotRestorable(str, Enum):
    """Why a report lists a change without a restore command."""

    TOO_MANY_SYMREFS = "too_many_symrefs"  # over ``SYMREF_LIMIT``: their direct targets are not read
    GIT_TOO_OLD = "git_too_old"  # older than ``SYMREF_GIT``: no command can check and write a symbolic ref
    CHAIN_OUTSIDE_REFS = "chain_outside_refs"  # a hop outside ``refs/``, such as ``HEAD``: Looper never writes those
    CHAIN_UNREADABLE = "chain_unreadable"  # git could not read a hop of the chain
    REF_FILE_UNREADABLE = "ref_file_unreadable"  # Looper refused a ref file or directory the ref is read from
    OVERLAPPING_TURN = "overlapping_turn"  # another task's turn on this repository ran meanwhile; it may be its change
    OTHER_TASK_BRANCH = "other_task_branch"  # information only: another unfinished task's branch, a command would undo it


@dataclass(eq=False)
class Window:
    """One task's turn on a repository, from before its first ref snapshot until it is closed."""

    repository: tuple[int, int]  # the common git directory's device and inode: one for all its worktrees
    overlapped: bool = False  # another task's window on this repository was open at the same time


_windows: dict[str, Window] = {}  # task id -> its open window; a task runs one turn at a time


@dataclass(frozen=True)
class Snapshot:
    refs: Mapping[str, str]  # branch or tag -> the commit or tag it resolves to; a dangling symbolic ref is not here
    head: str | None  # the full name of the branch HEAD is on; None when detached
    symrefs: Mapping[str, str] = field(default_factory=dict)  # ref -> the ref its chain ends at, as far as git lists it
    hops: Mapping[str, str] = field(default_factory=dict)  # any ref read -> the ref it points to directly
    # Refs outside branches and tags that a chain reads through and that are not symbolic: the value, None when missing.
    outside: Mapping[str, str | None] = field(default_factory=dict)
    gap: NotRestorable | None = None  # why symbolic refs here have no direct target in ``hops``
    # Ref files and directories Looper refused, and the links that read through one; nothing else here holds them.
    unreadable: frozenset[str] = frozenset()
    # Symbolic ref -> the target its own file spells, read before git: tells a retarget apart where ``hops`` cannot.
    spelled: Mapping[str, str] = field(default_factory=dict)
    window: Window | None = field(default=None, compare=False)  # the turn this is the first snapshot of


@dataclass(frozen=True)
class RefChange:
    ref: str
    change: Change
    old: str | None
    new: str | None
    restore: str | None  # None exactly when ``not_restorable`` says why
    not_restorable: NotRestorable | None = None

    def to_json(self) -> dict[str, Any]:
        found = {"ref": self.ref, "change": self.change.value, "old": self.old, "new": self.new,
                 "restore": self.restore}
        if self.not_restorable is not None:
            found["not_restorable"] = self.not_restorable.value
        return found


@dataclass(frozen=True)
class Report:
    changes: tuple[RefChange, ...]
    info: tuple[RefChange, ...]
    head_before: str | None
    head_after: str | None
    own_commits: int
    needs_you: bool

    @property
    def head_changed(self) -> bool:
        return self.head_before != self.head_after

    @property
    def worth_saying(self) -> bool:
        return bool(self.changes or self.info or self.head_changed or self.own_commits)


def display(name: str | None) -> str | None:
    """A ref name for the conversation: control and format characters (bidirectional controls, zero-width
    characters) written as ``\\uXXXX``."""
    if name is None:
        return None
    return "".join(f"\\u{ord(ch):04x}" if _odd(ch) else ch for ch in name)


def _quote(name: str) -> str:
    """``name`` as one shell word; control and format characters are spelled out in a ``$'...'`` word."""
    if not any(_odd(ch) for ch in name):
        return shlex.quote(name)
    # UTF-8 bytes as \\xHH: the one spelling every POSIX shell (bash 3.2 included) reads inside $'...'.
    body = "".join("".join(f"\\x{byte:02x}" for byte in ch.encode()) if _odd(ch)
                   else ("\\" + ch if ch in "'\\" else ch) for ch in name)
    return f"$'{body}'"


# What a ref itself holds: ("oid", id), ("ref", direct target), ("end", chain end: the direct target is unknown), or
# None when there is no such ref.
Value = tuple[str, str] | None


def _value(snap: Snapshot, name: str) -> Value:
    if name in snap.hops:
        return "ref", snap.hops[name]
    if name in snap.symrefs:
        return "end", snap.symrefs[name]
    oid = snap.refs.get(name) or snap.outside.get(name)
    return ("oid", oid) if oid else None


def _differs(before: Snapshot, after: Snapshot, name: str) -> bool:
    old, new = _value(before, name), _value(after, name)
    kinds = {(old or ("",))[0], (new or ("",))[0]}
    if "end" in kinds and name in before.symrefs and name in after.symrefs:
        return before.symrefs[name] != after.symrefs[name]  # one side has no direct target: compare the chain's end
    if kinds == {"end", "ref"} and name not in before.refs and name not in after.refs:
        return old[1] != new[1]  # dangling: without direct targets, its "end" is the target its own file spells
    return old != new


def _chain(snap: Snapshot, name: str) -> list[str]:
    """``name`` and every ref it reads through, in order, as far as ``snap`` knows the hops."""
    chain = [name]
    while (target := snap.hops.get(chain[-1])) is not None and target not in chain:
        chain.append(target)
    return chain


def _spelled_retarget(before: Snapshot, after: Snapshot, name: str) -> bool:
    """Whether the ref files show another direct target for ``name``, or for a ref outside branches and tags that it
    reads through (such a hop has no entry of its own). Without direct targets from git, only this shows a retarget
    that leaves the end of the chain where it was."""
    seen: set[str] = set()
    while name not in seen:
        seen.add(name)
        target = before.spelled.get(name)
        if target != after.spelled.get(name):
            return True
        if target is None or target.startswith(COMPARED):
            return False
        name = target
    return False


def _resolved(snap: Snapshot, name: str) -> str | None:
    end = _chain(snap, name)[-1]
    return snap.refs.get(end) or snap.outside.get(end)


def _within(name: str, refused: Iterable[str]) -> bool:
    """Whether ``name`` is one of ``refused``, or is in a refused directory. Only a path below ``refs/`` stands for
    what is in it: a refused target such as ``refs`` or an empty one names itself alone."""
    return any(name == path or (path.startswith("refs/") and name.startswith(path + "/")) for path in refused)


def _gap(before: Snapshot, after: Snapshot, names: Iterable[str]) -> NotRestorable | None:
    """Why no command can check and restore the refs ``names``, or None."""
    if any(_within(name, before.unreadable | after.unreadable) for name in names):
        return NotRestorable.REF_FILE_UNREADABLE
    for name in sorted(names):
        if not name.startswith("refs/"):
            return NotRestorable.CHAIN_OUTSIDE_REFS
        for snap in (before, after):
            if name in snap.symrefs and name not in snap.hops:
                return snap.gap or NotRestorable.CHAIN_UNREADABLE
        if not name.startswith(COMPARED) and name not in after.hops and name not in after.outside:
            return NotRestorable.CHAIN_UNREADABLE
    return None


def _direct_command(ref: str, old: str | None, new: str | None) -> str:
    """The command for a ref that no symbolic ref ties to another: it applies only while the ref still reads ``new``
    (for a deleted ref, while it still does not exist: the zero object id).
    --no-deref: if the name has become a symbolic ref since, only that name changes."""
    name = _quote(ref)
    if old is None:
        return f"git update-ref --no-deref -d {name} {new}"
    if new is None:
        return f"git update-ref --no-deref {name} {old} {'0' * len(old)}"
    return f"git update-ref --no-deref {name} {old} {new}"


def _transaction(before: Snapshot, after: Snapshot, names: Iterable[str], kept: Mapping[str, Value]) -> str:
    """One ``git update-ref`` transaction over ``names``: each is put back to its value before the turn (``kept`` says
    otherwise for the task branch), and each one that stays is checked to still hold its value after the turn. git
    applies all of it or none, so no order between commands matters."""
    guards, lines = [], []
    for name in sorted(names):
        # A name only the end-of-turn chains read has no recorded earlier value: it is checked, never written.
        known = name.startswith(COMPARED) or name in before.hops or name in before.outside
        now = _value(after, name)
        want = kept[name] if name in kept else _value(before, name) if known else now
        if now is None or now[0] == "oid":
            # git checks a value through a symbolic ref, so that the name still is none is the shell's to check.
            guards.append(f"! git symbolic-ref -q --no-recurse {_quote(name)} >/dev/null")
        if want == now:
            if now is None:
                lines.append(("verify", name))
            else:
                lines.append(("symref-verify" if now[0] == "ref" else "verify", name, now[1]))
        elif want is None:
            lines.append(("symref-delete" if now[0] == "ref" else "delete", name, now[1]))
        elif want[0] == "ref":
            if now is None:
                lines.append(("symref-create", name, want[1]))
            else:
                lines.append(("symref-update", name, want[1], "ref" if now[0] == "ref" else "oid", now[1]))
        elif now is None:
            lines.append(("create", name, want[1]))
        elif now[0] == "oid":
            lines.append(("update", name, want[1], now[1]))
        else:
            # A transaction cannot check a link it turns back into a direct ref: the shell checks its direct target,
            # git the value read through it (a dangling link has none; the rest of its chain is checked below).
            guards.append(f'[ "$(git symbolic-ref -q --no-recurse {_quote(name)})" = {_quote(now[1])} ]')
            current = _resolved(after, name)
            lines.append(("update", name, want[1], *([current] if current else [])))
    words = " ".join(_quote(" ".join(line)) for line in lines)
    return " && ".join([*guards, f"printf '%s\\n' {words} | git update-ref --no-deref --stdin"])


def _commands(before: Snapshot, after: Snapshot, scopes: list[set[str]],
              kept: Mapping[str, Value]) -> list[tuple[str | None, NotRestorable | None]]:
    """For each reported entry (``scopes``: its ref and every ref it reads through before and after the turn), its
    restore command or why there is none. Entries whose scopes meet share one command."""
    root: dict[str, str] = {}

    def find(name: str) -> str:
        while root.setdefault(name, name) != name:
            name = root[name]
        return name

    for scope in scopes:
        first, *rest = sorted(scope)
        for name in rest:
            root[find(name)] = find(first)
    groups: dict[str, set[str]] = {}
    for scope in scopes:
        for name in scope:
            groups.setdefault(find(name), set()).add(name)
    made: dict[str, tuple[str | None, NotRestorable | None]] = {}
    for key, names in groups.items():
        if (why := _gap(before, after, names)) is not None:
            made[key] = None, why
        elif any(name in before.symrefs or name in before.hops or name in after.symrefs or name in after.hops
                 for name in names):
            made[key] = _transaction(before, after, names, kept), None
        else:  # direct refs only, and so a single one
            name, = names
            made[key] = _direct_command(name, before.refs.get(name), after.refs.get(name)), None
    return [made[find(next(iter(scope)))] for scope in scopes]


def _refused(exc: git_safe.RepositoryRefused):
    from .tiers.task_manager import RefuseReason, Refused

    return Refused(RefuseReason.WORKSPACE, str(exc))


async def _git(work_dir: str | Path, *args: str) -> tuple[int, str]:
    """``git args`` in ``work_dir`` through ``git_safe``: the exit code and stdout."""
    try:
        process = await git_safe.spawn(work_dir, *args, extra_env=_LOCKLESS, stdin=asyncio.subprocess.DEVNULL,
                                       stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except git_safe.RepositoryRefused as exc:
        raise _refused(exc) from None
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), GIT_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise RuntimeError(f"git {args[0]} timed out reading the task's refs") from None
    try:
        await git_safe.recheck_async(process)
    except git_safe.RepositoryRefused as exc:
        raise _refused(exc) from None
    return process.returncode, stdout.decode("utf-8", "surrogateescape")


def _unreadable(detail: str):
    from .tiers.task_manager import RefuseReason, Refused

    return Refused(RefuseReason.WORKSPACE, detail)


@dataclass
class _Files:
    """What Looper itself read of the loose ref files."""

    links: dict[str, str] = field(default_factory=dict)  # symbolic ref -> the target its file (or link) spells
    unreadable: dict[str, str] = field(default_factory=dict)  # ref or directory -> why it was refused
    head: str | None = None  # the ref ``HEAD`` itself points to; None when detached


async def _git_dirs(work_dir: str | Path) -> tuple[str, str]:
    """The git directory and the common one (they differ in a linked worktree)."""
    found = []
    for option in ("--git-dir", "--git-common-dir"):
        code, path = await _git(work_dir, "rev-parse", option)
        if code:
            raise _unreadable(f"{work_dir} is not a git repository Looper can read (git exit {code}).")
        found.append(os.path.join(str(work_dir), path.removesuffix("\n")))
    return found[0], found[1]


def _check_top_files(git_dir: str, common: str) -> str | None:
    """``HEAD`` and ``packed-refs``, which git reads whole, must be files of their own (``HEAD`` may be the old
    symbolic-ref link): a link to elsewhere would have git report what is there. Returns the ref ``HEAD`` itself
    points to; nothing of ``packed-refs`` is read here."""
    head = None
    for folder, name in ((git_dir, "HEAD"), (common, "packed-refs")):
        refused = _unreadable(f"Looper does not read the repository's {name}: it is not a regular file of its own.")
        try:
            with safe_read.open_directory(folder, []) as directory:
                if isinstance(directory, safe_read.Skipped):
                    raise refused
                found = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if name == "HEAD" and stat.S_ISLNK(found.st_mode):
                    if not _is_ref_name(head := os.readlink(name, dir_fd=directory)):
                        raise refused
                elif not stat.S_ISREG(found.st_mode) or found.st_nlink > 1:
                    raise refused
                elif name == "HEAD":
                    read = safe_read.read_at(directory, name, REF_FILE_LIMIT)
                    if isinstance(read, safe_read.Skipped):
                        raise refused
                    if read.data.startswith(b"ref:"):
                        head = _spelled(read.data)
        except FileNotFoundError:
            continue
    return head


def _is_plain_file(common: str, path: str) -> bool:
    """Whether ``path`` below ``common`` is a regular file, reached through no link."""
    *parts, leaf = path.split("/")
    with safe_read.open_directory(common, parts) as directory:
        return (not isinstance(directory, safe_read.Skipped)
                and stat.S_ISREG(os.stat(leaf, dir_fd=directory, follow_symlinks=False).st_mode))


def _is_ref_name(name: str) -> bool:
    return name.startswith("refs/") and not _NOT_A_REF_NAME.search(name)


def _spelled(data: bytes) -> str:
    """The target a ``ref:`` file spells, ending where git ends it, at a NUL, without the white space git strips."""
    return os.fsdecode(data[4:].split(b"\0", 1)[0].strip(b" \t\r\n"))


def _read_entry(directory: int, entry: str, name: str, files: _Files) -> None:
    """One name in an open ref directory that is not a directory: record the link it spells, or why it is refused."""
    try:
        if stat.S_ISLNK(os.stat(entry, dir_fd=directory, follow_symlinks=False).st_mode):
            target = os.readlink(entry, dir_fd=directory)  # the old spelling of a symbolic ref
            if _is_ref_name(target):
                files.links[name] = target
            else:
                files.unreadable[name] = safe_read.Skip.SYMLINK.value
            return
    except OSError as exc:
        files.unreadable[name] = type(exc).__name__
        return
    read = safe_read.read_at(directory, entry, REF_FILE_LIMIT)
    if isinstance(read, safe_read.Skipped):
        files.unreadable[name] = read.reason.value
    elif read.data.startswith(b"ref:"):
        files.links[name] = _spelled(read.data)


def _walk(common: str, files: _Files) -> None:
    """Every loose ref file under ``refs/heads`` and ``refs/tags``. Each directory is opened from ``common`` one
    component at a time and listed through that descriptor, so a directory swapped for a link is refused."""
    pending = list(_ROOTS)
    while pending:
        folder = pending.pop()
        try:
            with safe_read.open_directory(common, folder.split("/")) as directory:
                if isinstance(directory, safe_read.Skipped):
                    # A regular file where a root belongs (a reftable repository): nothing can be read under it.
                    if not (folder in _ROOTS and _is_plain_file(common, folder)):
                        files.unreadable[folder] = directory.reason.value
                    continue
                with os.scandir(directory) as listing:
                    entries = [(entry.name, entry.is_dir(follow_symlinks=False)) for entry in listing]
                for entry, is_directory in entries:
                    if entry.endswith(".lock"):  # no ref name ends so
                        continue
                    if is_directory:
                        pending.append(f"{folder}/{entry}")
                    else:
                        _read_entry(directory, entry, f"{folder}/{entry}", files)
        except FileNotFoundError:
            if folder not in _ROOTS:  # it was listed a moment ago
                files.unreadable[folder] = "FileNotFoundError"
        except OSError as exc:
            files.unreadable[folder] = type(exc).__name__


def _probe_chains(git_dir: str, common: str, names: Iterable[str], files: _Files) -> None:
    """The files of the refs ``names``, and of what those spell as targets, by the spelling that reaches them (on a
    case-insensitive disk another case of a walked name opens the same file). One that does not exist is packed or
    missing. ``HEAD`` is checked with the top files; any other root ref is a file of the git directory. git opens
    other names outside ``refs/`` too (``x/y``, ``worktrees/w/y``): those are refused unread, existing or not."""
    queue, seen = deque(names), set()
    while queue:
        name = queue.popleft()
        if name in seen or name == "HEAD" or _within(name, files.unreadable):
            continue
        seen.add(name)
        in_refs = name.startswith("refs/")
        if in_refs and {"", ".", ".."} & set(name.split("/")):  # not a path below the git directory
            files.unreadable[name] = "not_a_ref_name"
            continue
        if not in_refs and not _ROOT_REF.fullmatch(name):
            files.unreadable[name] = "outside_refs"
            continue
        base = git_dir if not in_refs or name.startswith(_PER_WORKTREE) else common
        *parts, leaf = name.split("/")
        try:
            with safe_read.open_directory(base, parts) as directory:
                if isinstance(directory, safe_read.Skipped):
                    # A regular file where a directory would be: there is no such ref (a reftable repository's HEAD).
                    if not any(_is_plain_file(base, "/".join(parts[:end])) for end in range(1, len(parts) + 1)):
                        files.unreadable[name] = directory.reason.value
                    continue
                if stat.S_ISDIR(os.stat(leaf, dir_fd=directory, follow_symlinks=False).st_mode):
                    continue  # no ref: git opens nothing in it for this name
                _read_entry(directory, leaf, name, files)
        except FileNotFoundError:
            continue
        except OSError as exc:
            files.unreadable[name] = type(exc).__name__
            continue
        if name in files.links:
            queue.append(files.links[name])


async def _ref_files(work_dir: str | Path, names: Iterable[str] = (), *, walk: bool = True) -> _Files:
    """The loose ref files of ``work_dir``'s repository: all branches and tags (``walk``), and the chains through
    ``names`` and through the links found. git does not list a symbolic ref that dangles, and a symbolic ref is never
    packed, so the files are the only place to find those (not in a reftable repository). git reads the targets a
    command is built from; a spelled one only tells changes apart where git is not asked."""
    git_dir, common = await _git_dirs(work_dir)
    wanted = list(names)

    def read() -> _Files:
        files = _Files(head=_check_top_files(git_dir, common))
        if walk:
            _walk(common, files)
        head = [files.head] if files.head else []
        _probe_chains(git_dir, common, [*wanted, *head, *files.links.values()], files)
        return files

    try:
        return await asyncio.to_thread(read)
    except OSError as exc:
        raise _unreadable(f"Looper cannot read the branch and tag files ({type(exc).__name__}).") from None


def _without_unreadable(snap: Snapshot, *found: _Files) -> Snapshot:
    """``snap`` without any ref Looper refused, or that reads through one, by any hop either git or a file spells:
    git follows links, so what it said about those may come from outside the git directory."""
    refused = set(snap.unreadable).union(*(files.unreadable for files in found))
    if not refused:
        return snap
    # One entry for a refused directory, not one more for each name found under it later.
    directories = {name for name in refused if name.startswith("refs/")}
    refused = {name for name in refused if not _within(name.rpartition("/")[0], directories)}
    edges = [pair for links in (*(files.links for files in found), snap.hops, snap.symrefs) for pair in links.items()]
    # ``HEAD``'s own target is in none of those, and a ref can read through ``HEAD``.
    edges += [("HEAD", files.head) for files in found if files.head]
    grew = True
    while grew:
        tainted = {name for name, target in edges if _within(target, refused) and not _within(name, refused)}
        refused |= tainted
        grew = bool(tainted)

    def keep(values: Mapping[str, Any]) -> dict[str, Any]:
        return {name: value for name, value in values.items() if not _within(name, refused)}

    if snap.unreadable != refused:
        logger.warning("task Manager refs: %d ref files, directories or links through them are not read", len(refused))
    # git names HEAD after the end of its chain: where that chain is refused, only what HEAD itself holds is kept.
    head = next((files.head if files.head.startswith(HEADS) else None for files in found
                 if files.head and _within(files.head, refused)), snap.head)
    return replace(snap, refs=keep(snap.refs), head=head, symrefs=keep(snap.symrefs), hops=keep(snap.hops),
                   outside=keep(snap.outside), spelled=keep(snap.spelled), unreadable=frozenset(refused))


async def _symref_gap(work_dir: str | Path, count: int) -> NotRestorable | None:
    """Why the direct targets of ``count`` symbolic refs are not read, or None."""
    if count > SYMREF_LIMIT:
        logger.warning("task Manager refs: %d symbolic refs, over the limit of %d; their direct targets are not read",
                       count, SYMREF_LIMIT)
        return NotRestorable.TOO_MANY_SYMREFS
    _, text = await _git(work_dir, "version")
    found = re.search(r"(\d+)\.(\d+)", text)
    if found is None or (int(found[1]), int(found[2])) < SYMREF_GIT:
        logger.warning("task Manager refs: this git cannot restore symbolic refs; their direct targets are not read")
        return NotRestorable.GIT_TOO_OLD
    return None


async def _follow(work_dir: str | Path, snap: Snapshot, names: Iterable[str]) -> Snapshot:
    """``snap`` with every ref in ``names`` outside the compared namespaces read, hop by hop: its direct target, or
    its value. A name outside ``refs/`` is never read, and one git fails on stays unread."""
    hops, outside, queue = dict(snap.hops), dict(snap.outside), deque(names)
    while queue:
        name = queue.popleft()
        if (name.startswith(COMPARED) or not name.startswith("refs/") or name in hops or name in outside
                or _within(name, snap.unreadable)):
            continue
        code, target = await _git(work_dir, "symbolic-ref", "-q", "--no-recurse", name)
        if code == 0:
            hops[name] = target.removesuffix("\n")
            queue.append(hops[name])
        elif code == 1:  # not a symbolic ref
            code, value = await _git(work_dir, "show-ref", "--verify", "--hash", name)
            outside[name] = value.strip() if code == 0 else None
        else:
            logger.warning("task Manager refs: git could not read a ref a symbolic ref goes through (exit %d)", code)
    return replace(snap, hops=hops, outside=outside)


async def snapshot(work_dir: str | Path) -> Snapshot:
    """The logical value of every branch and tag, every hop of each symbolic ref's chain (dangling ones included),
    and where ``HEAD`` points. Packed refs, ``gc``, reflogs and index changes do not show in these values.

    A listing that cannot be told apart from another (a ref name holding a character ``str.splitlines`` breaks
    lines on, or two records of one name) is a read failure, never a last-write-wins value.

    The ref files are read before git and again after it, so one swapped for a link meanwhile is refused too."""
    first = await _ref_files(work_dir)
    code, listing = await _git(work_dir, "for-each-ref", "--format=%(refname)%00%(objectname)%00%(symref)",
                               "refs/heads", "refs/tags")
    if code:
        raise _unreadable(f"{work_dir} is not a git repository Looper can read (git exit {code}).")
    refs: dict[str, str] = {}
    symrefs: dict[str, str] = {}
    for line in listing.split("\n"):
        if not line:
            continue
        parts = line.split("\0")
        if len(parts) != 3 or not parts[0] or not parts[1]:
            raise _unreadable("The branch and tag listing has a record Looper cannot read.")
        ref, sha, target = parts
        if _LINE_BREAKS & set(ref) or ref in refs:
            raise _unreadable("A branch or tag name holds a line-break character or appears twice; "
                              "Looper cannot read the refs safely.")
        refs[ref] = sha
        if target:
            symrefs[ref] = target
    dangling = {name: target for name, target in sorted(first.links.items())
                if name.startswith(COMPARED) and name not in refs}
    listed = (*dangling, *(name for name in first.unreadable if name.startswith("refs/")))  # the rest never shows
    if any((_LINE_BREAKS | {"\n"}) & set(name) for name in listed):
        raise _unreadable("A branch or tag name holds a line-break character; Looper cannot read the refs safely.")
    code, head = await _git(work_dir, "symbolic-ref", "-q", "HEAD")
    # Only the trailing newline goes: ref names may end in (or hold) Unicode whitespace.
    head = head.removesuffix("\n") if code == 0 else ""
    found = Snapshot(refs, head if head.startswith(HEADS) else None, symrefs, spelled=first.links)
    found = await _read_hops(work_dir, _without_unreadable(found, first), dangling)
    second = await _ref_files(work_dir, [*found.hops.values(), *found.outside])
    return _without_unreadable(found, first, second)


async def _read_hops(work_dir: str | Path, found: Snapshot, dangling: Mapping[str, str]) -> Snapshot:
    """``found`` with every hop of its symbolic refs, the ``dangling`` ones included, or with the reason there are
    none."""
    dangling = {name: target for name, target in dangling.items() if not _within(name, found.unreadable)}
    symrefs = found.symrefs
    links = [*symrefs, *dangling]
    if not links:
        return found
    gap = await _symref_gap(work_dir, len(links))
    hops: dict[str, str] = {}
    if gap is None:
        for ref in links:
            code, target = await _git(work_dir, "symbolic-ref", "-q", "--no-recurse", ref)
            if code == 0:
                hops[ref] = target.removesuffix("\n")
            elif ref in symrefs:  # a file git does not take for a symbolic ref is no ref at all
                logger.warning("task Manager refs: git could not read a direct symbolic-ref target (exit %d)", code)
                gap, hops = NotRestorable.CHAIN_UNREADABLE, {}
                break
    if gap is not None:  # still notice a dangling link, without a git process for each
        return replace(found, symrefs={**symrefs, **dangling}, gap=gap)
    return await _follow(work_dir, replace(found, hops=hops), hops.values())


async def branch_problem(work_dir: str | Path, branch: str) -> str | None:
    """Why ``branch`` cannot be the task branch (git's own branch-name check; a leading ``-`` would read as an
    option), or None."""
    if branch.startswith("-"):
        return "a branch name that starts with '-' cannot be used as the task branch"
    code, _ = await _git(work_dir, "check-ref-format", "--branch", branch)
    return None if code == 0 else "git does not accept this as a branch name"


def _parents(body: bytes) -> list[str] | None:
    """The parents of a raw commit object, read the way git does: the first line is ``tree <oid>`` and the
    ``parent <oid>`` lines are the consecutive ones right after it. None when the header is not that shape."""
    lines = body.split(b"\n\n", 1)[0].decode("utf-8", "replace").split("\n")
    if not lines or not re.fullmatch(r"tree " + _OID.pattern, lines[0]):
        return None
    parents = []
    for line in lines[1:]:
        if not line.startswith("parent "):
            break
        oid = line[len("parent "):]
        if not _OID.fullmatch(oid):
            return None
        parents.append(oid)
    return parents


async def _reaches(work_dir: str | Path, new: str, old: str) -> Ancestry:
    """Whether ``old`` is an ancestor of ``new``, from the raw commit objects (``cat-file --batch`` honours neither
    ``info/grafts`` nor replace refs), breadth first so a merge finds ``old`` among its first parents.
    ``UNDECIDED`` when the walk gave up (a limit, an object that is not a well-formed commit, a timeout)."""
    process = await git_safe.spawn(work_dir, "-c", "core.useReplaceRefs=false", "cat-file", "--batch",
                                   extra_env=_LOCKLESS, stdin=asyncio.subprocess.PIPE,
                                   stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    found = Ancestry.UNDECIDED

    async def read() -> Ancestry:
        seen: set[str] = set()
        queue = deque([new])
        while queue:
            sha = queue.popleft()
            if sha == old:
                return Ancestry.DESCENDS
            if sha in seen:
                continue
            if len(seen) >= WALK_LIMIT:
                return Ancestry.UNDECIDED
            seen.add(sha)
            process.stdin.write(sha.encode() + b"\n")
            await process.stdin.drain()
            header = (await process.stdout.readline()).decode("ascii", "replace").split()
            if len(header) != 3 or header[1] != "commit" or not header[2].isdigit():
                return Ancestry.UNDECIDED
            size = int(header[2])
            if size > OBJECT_LIMIT:
                return Ancestry.UNDECIDED
            parents = _parents(await process.stdout.readexactly(size + 1))
            if parents is None:
                return Ancestry.UNDECIDED
            queue.extend(parents)
        return Ancestry.DIVERGES

    try:
        found = await asyncio.wait_for(read(), GIT_TIMEOUT_SEC)
    except (asyncio.TimeoutError, asyncio.IncompleteReadError, ValueError):
        found = Ancestry.UNDECIDED
    finally:
        try:
            process.stdin.close()
        except Exception:
            pass
        process.kill()
        await process.wait()
    return found


async def fast_forward(work_dir: str | Path, old: str, new: str) -> tuple[Ancestry, int]:
    """Whether ``new`` descends from ``old`` and, when it does, the number of commits ``old..new``. The decision
    reads raw parents, so a graft or a replace ref cannot make a reset look like an ancestor; only the count comes
    from ``rev-list`` and a graft could skew that number, never the decision. ``UNDECIDED`` when it cannot be
    decided: that is reported as unverified, never as a reset and never as a fast-forward."""
    try:
        verdict = await _reaches(work_dir, new, old)
        if verdict is not Ancestry.DESCENDS:
            return verdict, 0
        code, count = await _git(work_dir, "-c", "core.useReplaceRefs=false", "rev-list", "--count", f"{old}..{new}")
        return Ancestry.DESCENDS, (int(count.strip()) if code == 0 and count.strip().isdigit() else 0)
    except Exception:
        logger.warning("task Manager refs: could not decide fast-forward", exc_info=True)
        return Ancestry.UNDECIDED, 0


def _short(ref: str | None) -> str:
    return "detached" if ref is None else display(ref.removeprefix(HEADS))


async def open_turn(work_dir: str | Path, task_id: str) -> Snapshot:
    """Open ``task_id``'s window on the repository of ``work_dir`` and take its first snapshot. Every window open on
    the same repository at the same time is marked as overlapped, this one included. ``close_turn`` ends it."""
    _, common = await _git_dirs(work_dir)
    try:
        found = os.stat(common)
    except OSError as exc:
        raise _unreadable(f"Looper cannot read the git directory ({type(exc).__name__}).") from None
    close_turn(task_id)
    window = Window((found.st_dev, found.st_ino))
    for other in _windows.values():
        if other.repository == window.repository:
            other.overlapped = window.overlapped = True
    _windows[task_id] = window
    try:
        return replace(await snapshot(work_dir), window=window)
    except BaseException:
        close_turn(task_id)
        raise


def close_turn(task_id: str) -> None:
    """End the task's window, if one is open."""
    _windows.pop(task_id, None)


async def begin_turn(task: Mapping[str, Any], work_dir: str | Path, *, sink: TurnSink,
                     phase: str = "turn") -> tuple[Snapshot, str]:
    """Record the refs, and settle the task branch: record it at the first enablement, refuse when ``HEAD`` is not on
    it. Returns the snapshot (kept in memory by the running turn) and the task branch. The turn's window stays open
    until ``end_turn`` or ``close_turn``."""
    task_id = str(task["id"])
    before = await open_turn(work_dir, task_id)
    try:
        return before, await _task_branch(task_id, work_dir, before, sink=sink, phase=phase)
    except BaseException:
        close_turn(task_id)
        raise


async def _task_branch(task_id: str, work_dir: str | Path, before: Snapshot, *, sink: TurnSink, phase: str) -> str:
    from .tiers.task_manager import RefuseReason, Refused, TaskManagerEvent

    recorded = await store.get_branch(task_id)
    if recorded is None:
        if before.head is None:
            task_manager_safety.record_refusal(task_manager_safety.Reason.HEAD_DETACHED, phase)
            raise Refused(RefuseReason.HEAD_DETACHED,
                          "任务目录的 HEAD 是分离的（detached）。先检出一个分支再发消息，管家会把这个分支记为任务分支。")
        branch = before.head.removeprefix(HEADS)
        if (problem := await branch_problem(work_dir, branch)) is not None:
            raise Refused(RefuseReason.WORKSPACE, f"任务分支 {display(branch)} 不能记录：{problem}。换一个分支名再发消息。")
        if await store.record_first_branch(task_id, branch):
            await sink.event(TaskManagerEvent.SANDBOX_NOTICE, {"notice": FIRST_ENABLE_NOTICE, "detail": FIRST_ENABLE_DETAIL})
        recorded = await store.get_branch(task_id) or branch
    if before.head != HEADS + recorded:
        task_manager_safety.record_refusal(task_manager_safety.Reason.HEAD_MOVED, phase)
        raise Refused(RefuseReason.HEAD_MOVED,
                      f"任务分支记录的是 {display(recorded)}，目录现在在 {_short(before.head)}。"
                      f"把 {display(recorded)} 重新检出（git switch -- {_quote(recorded)}），"
                      "或者用 tasks.task_manager_branch_set 把当前分支记为任务分支，再发消息。")
    return recorded


async def _active_task(task_id: str) -> bool:
    from ..db import tasks

    row = await tasks.get_task(task_id)
    return row is not None and row["status"] not in FINISHED_STATUSES


async def compare(work_dir: str | Path, before: Snapshot, after: Snapshot, *, task_branch: str, task_id: str,
                  active_task: Callable[[str], Awaitable[bool]] = _active_task) -> Report:
    """What changed between two snapshots, with no side effect (it may read more refs: the ones a chain read through
    before the turn and no chain does now)."""
    earlier = [*before.hops.values(), *before.outside]
    first = await _ref_files(work_dir, earlier, walk=False)
    after = await _follow(work_dir, _without_unreadable(after, first), earlier)
    after = _without_unreadable(after, first, await _ref_files(work_dir, earlier, walk=False))
    refused = before.unreadable | after.unreadable
    # (entry without its command, the refs the command concerns, whether it is information only)
    entries: list[tuple[RefChange, set[str], bool]] = []
    task_ref, own_commits, needs_you = HEADS + task_branch, 0, False
    kept: dict[str, Value] = {}  # the task branch, where a command leaves other than its value before the turn
    names = {name for snap in (before, after) for name in (*snap.refs, *snap.symrefs, *snap.hops, *snap.unreadable)
             if name.startswith(COMPARED) or name in _ROOTS}
    for ref in sorted(names, key=lambda name: (name != task_ref, name)):  # the task branch first
        was, now = _value(before, ref), _value(after, ref)
        old, new = before.refs.get(ref), after.refs.get(ref)
        owner = ref.removeprefix(LOOPER).split("/", 1) if ref.startswith(LOOPER) else None
        informs = bool(ref != task_ref and owner and len(owner) == 2 and owner[0] != task_id
                       and await active_task(owner[0]))
        if _within(ref, refused):  # whether it changed cannot be known: said in every report, never skipped
            needs_you = needs_you or (ref != task_ref and not informs)
            entries.append((RefChange(display(ref), Change.UNREADABLE, old, new, None), {ref}, informs))
            continue
        changed = _differs(before, after, ref) or _spelled_retarget(before, after, ref)
        path = _chain(before, ref)
        linked = ref in before.symrefs or ref in before.hops
        # A hop outside branches and tags has no entry of its own: its move shows only through a link that reads it.
        moved_outside = any(not name.startswith(COMPARED) and _differs(before, after, name) for name in path[1:])
        end = before.symrefs.get(ref)
        if linked and ref not in before.hops:  # no direct targets: only the end of the chain can be judged
            moved_outside = old != new and not (end in before.refs or end in after.refs)
        if not changed and not (linked and (moved_outside or (ref == task_ref and old != new))):
            continue
        if ref == task_ref:
            # Ancestry is decided from the two commits whatever the task branch is linked to.
            verdict, count = ((await fast_forward(work_dir, old, new))
                              if old is not None and new is not None else (Ancestry.DIVERGES, 0))
            relinked = changed or path != _chain(after, ref)
            if not linked and now is not None and now[0] != "oid" and was is not None:
                # It became a link: direct again, on the new commit when its commits descend, else where it was.
                kept[ref] = ("oid", new) if verdict is Ancestry.DESCENDS else was
            if verdict is Ancestry.DESCENDS:
                own_commits = count
                if now is not None and now[0] == "oid" and not linked:
                    kept[ref] = now  # its own commits stay, also when a command checks this branch
                # A changed chain is always its own report; an intact one only when it moved outside.
                if not (moved_outside or (relinked and (linked or ref in after.symrefs or ref in after.hops))):
                    continue
        kind = (Change.CREATED if was is None
                else Change.DELETED if now is None or (new is None and old is not None)
                else Change.MOVED if ref != task_ref or verdict is Ancestry.DESCENDS
                else Change.FAST_FORWARD_UNVERIFIED if verdict is Ancestry.UNDECIDED
                else Change.NON_FAST_FORWARD)
        needs_you = needs_you or (ref != task_ref and not informs)
        entries.append((RefChange(display(ref), kind, old, new, None), {*path, *_chain(after, ref)}, informs))
    commands = _commands(before, after, [scope for _, scope, _ in entries], kept)
    if before.window is not None and before.window.overlapped:
        # Any of these changes may be the other turn's: a command would undo it.
        commands = [(None, why or NotRestorable.OVERLAPPING_TURN) for _, why in commands]
    done = [replace(entry, restore=command, not_restorable=why)
            for (entry, _, _), (command, why) in zip(entries, commands)]
    # An information entry whose command also restores a change is listed with the changes.
    shared = {change.restore for change, (_, _, informs) in zip(done, entries) if not informs and change.restore}
    changes, info = [], []
    for change, (_, _, informs) in zip(done, entries):
        if informs and change.restore not in shared:
            # Another task is at work on it: a command of its own is not offered.
            info.append(replace(change, restore=None,
                                not_restorable=change.not_restorable or NotRestorable.OTHER_TASK_BRANCH))
        else:
            changes.append(change)
    return Report(tuple(changes), tuple(info), before.head, after.head, own_commits, needs_you)


def payload(report: Report, report_id: int | None, message_id: str | None = None) -> dict[str, Any]:
    """The ``ref_report`` event; ``message_id`` (the turn's) keeps two otherwise identical reports apart."""
    return {"message_id": message_id, "report_id": report_id, "changes": [c.to_json() for c in report.changes],
            "info": [c.to_json() for c in report.info],
            "head": ({"before": display(report.head_before), "after": display(report.head_after)}
                     if report.head_changed else None),
            "own_commits": report.own_commits, "needs_you": report.needs_you}


async def end_turn(task: Mapping[str, Any], work_dir: str | Path, before: Snapshot, branch: str, *, sink: TurnSink,
                   message_id: str | None = None) -> Report:
    """Compare now with ``before`` and say what changed. Nothing is restored and no alert is raised."""
    from .tiers.task_manager import TaskManagerEvent

    task_id = str(task["id"])
    try:
        report = await compare(work_dir, before, await snapshot(work_dir), task_branch=branch, task_id=task_id)
    except Exception:
        await sink.event(TaskManagerEvent.SANDBOX_NOTICE, {"notice": REFS_UNREADABLE_NOTICE,
                                                          "detail": REFS_UNREADABLE_DETAIL, "message_id": message_id})
        raise
    finally:
        close_turn(task_id)
    if not report.worth_saying:
        return report
    report_id = None
    if report.changes or report.info:
        body = payload(report, None, message_id)
        report_id = await store.insert_report(
            task_id, message_id, {key: body[key] for key in ("changes", "info", "head", "own_commits")},
            report.needs_you)
    await sink.event(TaskManagerEvent.REF_REPORT, payload(report, report_id, message_id))
    return report
