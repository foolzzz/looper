"""Find a symbolic link in a git directory's ref storage.

git follows a link placed at ``packed-refs`` or anywhere under ``refs`` or ``logs``: it reads refs through it, and
writes and deletes them through it, outside the repository. ``find`` looks at everything git reads or writes as a ref
in one git directory: ``HEAD``, the root refs (``ORIG_HEAD``, ``MERGE_HEAD``, ...), ``packed-refs``, the whole
``refs``, ``logs`` and ``reftable`` trees, and the same places of each linked worktree under ``worktrees``. Links
elsewhere in the git directory (hooks, configuration) are not its subject.

The walk goes from a descriptor of the git directory, one component at a time with ``O_NOFOLLOW`` (``safe_read``'s
open), so a directory swapped for a link while it is walked is found, never followed. Nothing is read through a link,
and its target is not looked at. Every call walks the file system again: nothing is remembered. What cannot be listed,
or lies deeper than ``MAX_DEPTH``, is reported too (git could still open a ref there by name).

The cost grows with the number of loose refs and reflogs (one directory listing per directory).
"""
from __future__ import annotations

import os
import re
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from . import safe_read

MAX_DEPTH = 64  # directories below a ``refs``, ``logs`` or ``reftable``: also the descriptors held at once
PATH_MAX_CHARS = 200
_TREES = ("refs", "logs", "reftable")
_NAMED = ("HEAD", "packed-refs", *_TREES, "worktrees")
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
