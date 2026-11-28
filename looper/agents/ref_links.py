"""Find what would make git leave a git directory while it reads refs or its own files: a symbolic link, a hard
link, or a symbolic ref that points outside ``refs``.

git follows a symbolic link placed at the top of a git directory (``HEAD``, ``packed-refs``, ``config``, ``index``,
``shallow``, ...), in ``info``, or anywhere under ``refs`` or ``logs``, and a hard link there is the same file as one
elsewhere. A symbolic ref (``ref: <target>``) makes git open the target as a path in the git directory, wherever the
links on that path lead; so a target must be a valid name under ``refs`` (walked here) or a root ref (an entry at the
top, looked at here). Two looks at one git directory:

* ``check``, made before every git command (``git_safe``): every entry at the top and in ``info`` (``objects`` and
  ``hooks`` as entries only, not walked), the whole ``refs`` and ``reftable`` trees, for each linked worktree under
  ``worktrees`` the entry itself and its ``HEAD``, ``refs``, ``logs``, ``gitdir`` and ``commondir``, and ``logs``,
  ``logs/HEAD`` and ``logs/refs`` as entries. For a command that writes refs it also walks ``logs``, or looks at
  every component of the reflogs of the refs named. The root refs at the top and every file under ``refs`` are read
  for a symbolic ref's target; with ``worktree_heads``, each linked worktree's ``HEAD`` too.
* ``find``, the full walk: ``HEAD``, the root refs (``ORIG_HEAD``, ``MERGE_HEAD``, ...), ``packed-refs``, the whole
  ``refs``, ``logs`` and ``reftable`` trees, and the same places of each linked worktree under ``worktrees``, with the
  root refs and the files under ``refs`` read for their targets.

Every regular file among the places looked at must have no other hard link. ``objects`` is not walked: git itself
hard-links objects (a local clone).

Directories and files are opened from a descriptor of the git directory, one component at a time with ``O_NOFOLLOW``
(``safe_read``'s open), so a directory swapped for a link while it is walked is found, never followed. Nothing is
read through a link, and its target is not looked at; of a file read for a symbolic ref, nothing is reported but its
own path. Every call reads the file system again: nothing is remembered. What cannot be listed or read, or lies
deeper than ``MAX_DEPTH``, is reported too (git could still open a ref there by name).

The cost grows with the number of directories listed and files read, and ``check`` also looks at each linked worktree.
"""
from __future__ import annotations

import contextlib
import errno
import os
import re
import stat
import unicodedata
from collections.abc import Collection, Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from . import safe_read

MAX_DEPTH = 64  # directories below a ``refs``, ``logs`` or ``reftable``: also the descriptors held at once
PATH_MAX_CHARS = 200
_TREES = ("refs", "logs", "reftable")
_NAMED = ("HEAD", "packed-refs", *_TREES, "worktrees")
# What ``check`` looks at in each linked worktree's entry.
_WORKTREE_ENTRIES = ("HEAD", "refs", "logs", "gitdir", "commondir")
_REF_LIMIT = 4096  # the most read of a file for a symbolic ref's target
# Names at the top of a git directory that git reads as refs, matched without case: on a case-insensitive file system
# git opens ``orig_head`` as ``ORIG_HEAD``. (The fixed names above are looked up by name, which has the same effect.)
_ROOT_REF = re.compile(r"[a-z0-9_-]*head|auto_merge|bisect_expected_rev|notes_merge_(partial|ref)|merge_autostash")
# A root ref a symbolic ref may name, by git's ``is_root_ref`` (refs.c; gitglossary(7), "root ref"): capitals and
# ``_``, being ``HEAD``, ending in ``_HEAD``, or one of its irregular root refs.
_ROOT_TARGET = re.compile(r"HEAD|[A-Z_]*_HEAD|AUTO_MERGE|BISECT_EXPECTED_REV|NOTES_MERGE_PARTIAL|NOTES_MERGE_REF|"
                          r"MERGE_AUTOSTASH")
# The ``HEAD`` git itself writes in a reftable repository (setup.c, git's "invalid HEAD" stub), whose refs are not files:
# an invalid name, under ``refs``.
_REFTABLE_HEAD = "refs/heads/.invalid"
# What no ref name holds (git-check-ref-format(1)).
_BAD_IN_REF = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]|\.\.|@\{")
_SPACE = b" \t\r\n"  # git's ``isspace``


class Kind(StrEnum):
    """Why a place is refused; the values are ``git_safe.RepositoryRefused``'s reasons."""

    SYMLINK = "ref_symlink"
    HARD_LINK = "ref_hardlink"
    SYMREF_TARGET = "ref_symref_target"  # a symbolic ref pointing outside ``refs`` and the root refs
    UNCHECKED = "ref_unchecked"


@dataclass(frozen=True)
class Finding:
    """``path`` names the place inside the repository (``.git/refs/heads/x``), escaped and bounded."""

    path: str
    kind: Kind


class _Found(Exception):
    def __init__(self, path: str, kind: Kind = Kind.SYMLINK) -> None:
        super().__init__(path)
        self.path, self.kind = path, kind


def printable(path: str) -> str:
    """``path`` as one bounded line: control, format, separator and undecodable characters as ``\\uXXXX``."""
    text = "".join(f"\\u{ord(ch):04x}" if unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Zl", "Zp") else ch
                   for ch in path)
    return text if len(text) <= PATH_MAX_CHARS else text[:PATH_MAX_CHARS - 1] + "…"


def _open_directory(name: str, parent: int | None) -> int:
    return safe_read._open(name, os.O_DIRECTORY, parent)


def _mode(name: str, parent: int, path: str) -> int | None:
    """The type of ``name`` itself (a link is not followed), None when there is nothing."""
    try:
        return os.stat(name, dir_fd=parent, follow_symlinks=False).st_mode
    except FileNotFoundError:
        return None
    except OSError:
        raise _Found(path, Kind.UNCHECKED) from None


def _enter(name: str, parent: int | None, path: str) -> int | None:
    """A descriptor of the directory ``name``; None when it is gone or is an ordinary file."""
    try:
        return _open_directory(name, parent)
    except safe_read._LeftTheBoundary:  # a link, or not a directory
        mode = _mode(name, parent, path)
        if mode is not None and stat.S_ISLNK(mode):
            raise _Found(path) from None
        if mode is not None and stat.S_ISDIR(mode):  # it changed twice under us
            raise _Found(path, Kind.UNCHECKED) from None
        return None
    except FileNotFoundError:
        return None
    except OSError:
        raise _Found(path, Kind.UNCHECKED) from None


def _entries(directory: int, path: str) -> tuple[list[str], list[str], list[os.DirEntry]]:
    """The names of the links and of the directories in ``directory``, and the entries of its regular files."""
    links, directories, files = [], [], []
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.is_symlink():
                    links.append(entry.name)
                elif entry.is_dir(follow_symlinks=False):
                    directories.append(entry.name)
                elif entry.is_file(follow_symlinks=False):
                    files.append(entry)
    except OSError:
        raise _Found(path, Kind.UNCHECKED) from None
    return links, directories, files


def _refuse_hard_link(name: str, parent: int, path: str) -> None:
    """Refuse ``name`` when it is a regular file with another hard link, or a symbolic link."""
    try:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError:
        raise _Found(path, Kind.UNCHECKED) from None
    if stat.S_ISLNK(info.st_mode):
        raise _Found(path)
    if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
        raise _Found(path, Kind.HARD_LINK)


def _read(name: str, parent: int | None, path: str) -> bytes | None:
    """Up to ``_REF_LIMIT + 1`` bytes of the regular file ``name``, refused when it is a link or has another hard
    link; None when there is no such regular file."""
    try:
        descriptor = safe_read._open(name, os.O_NONBLOCK, parent)
    except safe_read._LeftTheBoundary:
        raise _Found(path) from None
    except FileNotFoundError:
        return None
    except OSError:
        raise _Found(path, Kind.UNCHECKED) from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            return None
        if info.st_nlink > 1:
            raise _Found(path, Kind.HARD_LINK)
        return safe_read._read_bytes(descriptor, _REF_LIMIT + 1)
    except OSError:
        raise _Found(path, Kind.UNCHECKED) from None
    finally:
        os.close(descriptor)


def _target(data: bytes) -> str | None:
    """What a symbolic ref's content names, read as git's ``parse_loose_ref_contents`` does; None for any other
    content."""
    if not data.startswith(b"ref:"):
        return None
    return os.fsdecode(data[len(b"ref:"):].strip(_SPACE))


def _allowed_target(target: str) -> bool:
    if target == _REFTABLE_HEAD or _ROOT_TARGET.fullmatch(target):
        return True
    if not target.startswith("refs/") or target.endswith(".") or _BAD_IN_REF.search(target):
        return False
    return all(part and not part.startswith(".") and not part.endswith(".lock") for part in target.split("/"))


def _ref_file(name: str, parent: int, path: str) -> None:
    """Refuse the file git may read as a ref when it is linked, or is a symbolic ref git must not follow: one whose
    target is not allowed, or too long to be read whole."""
    data = _read(name, parent, path)
    if data is None or (target := _target(data)) is None:
        return
    if len(data) > _REF_LIMIT or not _allowed_target(target):
        raise _Found(path, Kind.SYMREF_TARGET)


def _tree(directory: int, path: str, depth: int, *, refs: bool) -> None:
    """Refuse a link anywhere below ``directory``; with ``refs`` every file is read as a ref."""
    links, directories, files = _entries(directory, path)
    if links:
        raise _Found(f"{path}/{min(links)}")
    for entry in files:
        if refs:
            _ref_file(entry.name, directory, f"{path}/{entry.name}")
        else:
            _refuse_hard_link(entry.name, directory, f"{path}/{entry.name}")
    for name in directories:
        below = f"{path}/{name}"
        if depth >= MAX_DEPTH:
            raise _Found(below, Kind.UNCHECKED)
        child = _enter(name, directory, below)
        if child is None:
            continue
        try:
            _tree(child, below, depth + 1, refs=refs)
        finally:
            os.close(child)


def _git_directory(directory: int, path: str, *, worktrees: bool) -> None:
    modes = {name: _mode(name, directory, f"{path}/{name}") for name in _NAMED}
    for name, mode in modes.items():
        if mode is not None and stat.S_ISLNK(mode):
            raise _Found(f"{path}/{name}")
    links, _, files = _entries(directory, path)
    for name in sorted(links):
        if _ROOT_REF.fullmatch(name.casefold()):
            raise _Found(f"{path}/{name}")
    for entry in files:
        if _ROOT_REF.fullmatch(entry.name.casefold()):
            _ref_file(entry.name, directory, f"{path}/{entry.name}")
    for name in ("packed-refs", "gitdir", "commondir"):
        _refuse_hard_link(name, directory, f"{path}/{name}")
    for name in (*_TREES, "worktrees") if worktrees else _TREES:
        if modes[name] is None:
            continue
        below = f"{path}/{name}"
        child = _enter(name, directory, below)
        if child is None:
            continue
        try:
            if name != "worktrees":
                _tree(child, below, 1, refs=name == "refs")
                continue
            links, entries, _ = _entries(child, below)
            if links:
                raise _Found(f"{below}/{min(links)}")
            for entry in entries:
                linked = _enter(entry, child, f"{below}/{entry}")
                if linked is None:
                    continue
                try:
                    _git_directory(linked, f"{below}/{entry}", worktrees=False)
                finally:
                    os.close(linked)
        finally:
            os.close(child)


def find(git_dir: str | Path, label: str) -> Finding | None:
    """The first link (or place that cannot be checked) in the ref storage of the resolved git directory ``git_dir``,
    named below ``label``; None when there is none, also when there is no such directory."""
    try:
        directory = _enter(str(git_dir), None, label)
        if directory is None:
            return None
        try:
            _git_directory(directory, label, worktrees=True)
        finally:
            os.close(directory)
    except _Found as found:
        return Finding(printable(found.path), found.kind)
    return None


@contextlib.contextmanager
def _below(name: str, parent: int | None, path: str) -> Iterator[int | None]:
    """``_enter``, closed on the way out."""
    directory = _enter(name, parent, path)
    try:
        yield directory
    finally:
        if directory is not None:
            os.close(directory)


def _refuse_links(directory: int, path: str) -> tuple[list[str], list[os.DirEntry]]:
    """Refuse a symbolic or hard link among ``directory``'s entries; the names of the directories among them and the
    entries of the files."""
    links, directories, files = _entries(directory, path)
    if links:
        raise _Found(f"{path}/{min(links)}")
    for entry in files:
        _refuse_hard_link(entry.name, directory, f"{path}/{entry.name}")
    return directories, files


def _reflog(logs: int, name: str, path: str) -> None:
    """Refuse a link at any component of the reflog ``name`` below ``logs``. A ``name`` that no file can have (it
    leaves ``logs``, or is too long: an argument that is not a ref) names nothing."""
    parts = name.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return
    for end in range(1, len(parts) + 1):
        below = "/".join(parts[:end])
        try:
            mode = os.stat(below, dir_fd=logs, follow_symlinks=False).st_mode
        except FileNotFoundError:
            return
        except OSError as exc:
            if exc.errno == errno.ENAMETOOLONG:
                return
            raise _Found(f"{path}/{below}", Kind.UNCHECKED) from None
        if stat.S_ISLNK(mode):
            raise _Found(f"{path}/{below}")
        if not stat.S_ISDIR(mode):
            _refuse_hard_link(below, logs, f"{path}/{below}")
            return


def _fixed(directory: int, path: str, log_tree: bool, reflogs: Collection[str], worktree_heads: bool) -> None:
    for entry in _refuse_links(directory, path)[1]:
        if _ROOT_REF.fullmatch(entry.name.casefold()):
            _ref_file(entry.name, directory, f"{path}/{entry.name}")
    with _below("info", directory, f"{path}/info") as info:
        if info is not None:
            _refuse_links(info, f"{path}/info")
    for name in ("refs", "reftable"):
        with _below(name, directory, f"{path}/{name}") as tree:
            if tree is not None:
                _tree(tree, f"{path}/{name}", 1, refs=name == "refs")
    with _below("worktrees", directory, f"{path}/worktrees") as worktrees:
        if worktrees is not None:
            for entry in _refuse_links(worktrees, f"{path}/worktrees")[0]:
                for name in _WORKTREE_ENTRIES:
                    below = f"{entry}/{name}"
                    _refuse_hard_link(below, worktrees, f"{path}/worktrees/{below}")
                if not worktree_heads:
                    continue
                with _below(entry, worktrees, f"{path}/worktrees/{entry}") as linked:
                    if linked is not None:
                        _ref_file("HEAD", linked, f"{path}/worktrees/{entry}/HEAD")
    with _below("logs", directory, f"{path}/logs") as logs:
        if logs is None:
            return
        for name in ("HEAD", "refs"):
            _refuse_hard_link(name, logs, f"{path}/logs/{name}")
        if log_tree:
            _tree(logs, f"{path}/logs", 1, refs=False)
        else:
            for name in reflogs:
                _reflog(logs, name, f"{path}/logs")


def check(git_dir: str | Path, label: str, *, log_tree: bool = False, reflogs: Collection[str] = (),
          worktree_heads: bool = False) -> Finding | None:
    """The fixed check of the resolved git directory ``git_dir`` (see the module), named below ``label``; with
    ``log_tree`` the whole ``logs`` tree too, else every component of each of ``reflogs`` (paths below ``logs``);
    with ``worktree_heads``, each linked worktree's ``HEAD`` is read as a ref too.
    None when nothing is found, also when there is no such directory."""
    try:
        with _below(str(git_dir), None, label) as directory:
            if directory is not None:
                _fixed(directory, label, log_tree, reflogs, worktree_heads)
    except _Found as found:
        return Finding(printable(found.path), found.kind)
    return None


def head_target(git_dir: str | Path) -> str | None:
    """The ref ``git_dir``'s ``HEAD`` names (``refs/heads/main``), None when it is detached, linked or unreadable."""
    try:
        data = _read(os.path.join(git_dir, "HEAD"), None, "HEAD")
    except _Found:
        return None
    return None if data is None else _target(data.partition(b"\n")[0])
