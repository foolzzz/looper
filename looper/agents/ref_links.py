"""Find a symbolic link where git reads refs or its own files in a git directory.

git follows a link placed at the top of a git directory (``HEAD``, ``packed-refs``, ``config``, ``index``,
``shallow``, ...), in ``info``, or anywhere under ``refs`` or ``logs``: it reads through it, and writes and deletes
refs through it, outside the repository. Two looks at one git directory:

* ``check``, made before every git command (``git_safe``): every entry at the top and in ``info`` (``objects`` and
  ``hooks`` as entries only, not walked), the whole ``refs`` and ``reftable`` trees, for each linked worktree under
  ``worktrees`` the entry itself and its ``HEAD``, ``refs``, ``logs``, ``gitdir`` and ``commondir``, and ``logs``,
  ``logs/HEAD`` and ``logs/refs`` as entries. For a command that writes refs it also walks ``logs``, or looks at
  every component of the reflogs of the refs named.
* ``find``, the full walk: ``HEAD``, the root refs (``ORIG_HEAD``, ``MERGE_HEAD``, ...), ``packed-refs``, the whole
  ``refs``, ``logs`` and ``reftable`` trees, and the same places of each linked worktree under ``worktrees``.

Directories are opened from a descriptor of the git directory, one component at a time with ``O_NOFOLLOW``
(``safe_read``'s open), so a directory swapped for a link while it is walked is found, never followed. Nothing is
read through a link, and its target is not looked at. Every call reads the file system again: nothing is remembered.
What cannot be listed, or lies deeper than ``MAX_DEPTH``, is reported too (git could still open a ref there by name).

The cost grows with the number of directories listed, and ``check`` also looks at each linked worktree.
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
from pathlib import Path

from . import safe_read

MAX_DEPTH = 64  # directories below a ``refs``, ``logs`` or ``reftable``: also the descriptors held at once
PATH_MAX_CHARS = 200
_TREES = ("refs", "logs", "reftable")
_NAMED = ("HEAD", "packed-refs", *_TREES, "worktrees")
# What ``check`` looks at in each linked worktree's entry.
_WORKTREE_ENTRIES = ("HEAD", "refs", "logs", "gitdir", "commondir")
_HEAD_LIMIT = 4096
# Names at the top of a git directory that git reads as refs, matched without case: on a case-insensitive file system
# git opens ``orig_head`` as ``ORIG_HEAD``. (The fixed names above are looked up by name, which has the same effect.)
_ROOT_REF = re.compile(r"[a-z0-9_-]*head|auto_merge|bisect_expected_rev|notes_merge_(partial|ref)|merge_autostash")


@dataclass(frozen=True)
class Finding:
    """``path`` names the place inside the repository (``.git/refs/heads/x``), escaped and bounded; ``link`` is False
    for a place that could not be checked."""

    path: str
    link: bool


class _Found(Exception):
    def __init__(self, path: str, link: bool = True) -> None:
        super().__init__(path)
        self.path, self.link = path, link


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
        raise _Found(path, link=False) from None


def _enter(name: str, parent: int | None, path: str) -> int | None:
    """A descriptor of the directory ``name``; None when it is gone or is an ordinary file."""
    try:
        return _open_directory(name, parent)
    except safe_read._LeftTheBoundary:  # a link, or not a directory
        mode = _mode(name, parent, path)
        if mode is not None and stat.S_ISLNK(mode):
            raise _Found(path) from None
        if mode is not None and stat.S_ISDIR(mode):  # it changed twice under us
            raise _Found(path, link=False) from None
        return None
    except FileNotFoundError:
        return None
    except OSError:
        raise _Found(path, link=False) from None


def _entries(directory: int, path: str) -> tuple[list[str], list[str]]:
    """The names of the links and of the directories in ``directory``."""
    links, directories = [], []
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.is_symlink():
                    links.append(entry.name)
                elif entry.is_dir(follow_symlinks=False):
                    directories.append(entry.name)
    except OSError:
        raise _Found(path, link=False) from None
    return links, directories


def _tree(directory: int, path: str, depth: int) -> None:
    links, directories = _entries(directory, path)
    if links:
        raise _Found(f"{path}/{min(links)}")
    for name in directories:
        below = f"{path}/{name}"
        if depth >= MAX_DEPTH:
            raise _Found(below, link=False)
        child = _enter(name, directory, below)
        if child is None:
            continue
        try:
            _tree(child, below, depth + 1)
        finally:
            os.close(child)


def _git_directory(directory: int, path: str, *, worktrees: bool) -> None:
    modes = {name: _mode(name, directory, f"{path}/{name}") for name in _NAMED}
    for name, mode in modes.items():
        if mode is not None and stat.S_ISLNK(mode):
            raise _Found(f"{path}/{name}")
    for name in sorted(_entries(directory, path)[0]):
        if _ROOT_REF.fullmatch(name.casefold()):
            raise _Found(f"{path}/{name}")
    for name in (*_TREES, "worktrees") if worktrees else _TREES:
        if modes[name] is None:
            continue
        below = f"{path}/{name}"
        child = _enter(name, directory, below)
        if child is None:
            continue
        try:
            if name != "worktrees":
                _tree(child, below, 1)
                continue
            links, entries = _entries(child, below)
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
        return Finding(printable(found.path), found.link)
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


def _refuse_links(directory: int, path: str) -> list[str]:
    """Refuse a link among ``directory``'s entries; the names of the directories among them."""
    links, directories = _entries(directory, path)
    if links:
        raise _Found(f"{path}/{min(links)}")
    return directories


def _refuse_link(name: str, parent: int, path: str) -> None:
    mode = _mode(name, parent, path)
    if mode is not None and stat.S_ISLNK(mode):
        raise _Found(path)


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
            raise _Found(f"{path}/{below}", link=False) from None
        if stat.S_ISLNK(mode):
            raise _Found(f"{path}/{below}")
        if not stat.S_ISDIR(mode):
            return


def _fixed(directory: int, path: str, log_tree: bool, reflogs: Collection[str]) -> None:
    _refuse_links(directory, path)
    with _below("info", directory, f"{path}/info") as info:
        if info is not None:
            _refuse_links(info, f"{path}/info")
    for name in ("refs", "reftable"):
        with _below(name, directory, f"{path}/{name}") as tree:
            if tree is not None:
                _tree(tree, f"{path}/{name}", 1)
    with _below("worktrees", directory, f"{path}/worktrees") as worktrees:
        if worktrees is not None:
            for entry in _refuse_links(worktrees, f"{path}/worktrees"):
                for name in _WORKTREE_ENTRIES:
                    below = f"{entry}/{name}"
                    _refuse_link(below, worktrees, f"{path}/worktrees/{below}")
    with _below("logs", directory, f"{path}/logs") as logs:
        if logs is None:
            return
        for name in ("HEAD", "refs"):
            _refuse_link(name, logs, f"{path}/logs/{name}")
        if log_tree:
            _tree(logs, f"{path}/logs", 1)
        else:
            for name in reflogs:
                _reflog(logs, name, f"{path}/logs")


def check(git_dir: str | Path, label: str, *, log_tree: bool = False,
          reflogs: Collection[str] = ()) -> Finding | None:
    """The fixed check of the resolved git directory ``git_dir`` (see the module), named below ``label``; with
    ``log_tree`` the whole ``logs`` tree too, else every component of each of ``reflogs`` (paths below ``logs``).
    None when nothing is found, also when there is no such directory."""
    try:
        with _below(str(git_dir), None, label) as directory:
            if directory is not None:
                _fixed(directory, label, log_tree, reflogs)
    except _Found as found:
        return Finding(printable(found.path), found.link)
    return None


def head_target(git_dir: str | Path) -> str | None:
    """The ref ``git_dir``'s ``HEAD`` names (``refs/heads/main``), None when it is detached, a link or unreadable."""
    try:
        descriptor = safe_read._open(os.path.join(git_dir, "HEAD"), os.O_NONBLOCK)
    except (OSError, safe_read._LeftTheBoundary):
        return None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return None
        line = os.fsdecode(os.read(descriptor, _HEAD_LIMIT)).partition("\n")[0]
    except OSError:
        return None
    finally:
        os.close(descriptor)
    return line.removeprefix("ref:").strip() if line.startswith("ref:") else None
