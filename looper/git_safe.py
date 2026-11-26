"""Run git in a directory whose content Looper does not own, without running the repository's own code.

An agent can write everything in its task directory, including ``.git/config``, ``.git/hooks`` and
``.git/info/attributes`` (a linked worktree shares the main repository's). Git runs code those files name:
hooks, ``core.hooksPath``, ``core.fsmonitor``, filter, merge and diff drivers, textconv, the signing program,
``core.sshCommand``, the pager, the editor, askpass and credential helpers. When ``looper serve`` runs git there,
that code would run as the service, after the agent's run (and its time budget and process-tree kill) is over.

Every git command Looper runs in a task, step or worktree directory goes through this module:

* ``argv`` puts fixed overrides in front of the command: ``-c`` for each setting that names a program, and
  ``--attr-source`` with the empty tree, so the work tree's ``.gitattributes`` assign no drivers. That does not
  cover ``.git/info/attributes``, so ``check_repository`` refuses a repository whose file assigns a filter, diff
  or merge driver. The overrides also make git ignore ``.gitattributes`` for text conversion: a repository that
  relies on it (LFS, ``eol=``) is read and written as plain content.
* ``environment`` builds the child's environment from an allowlist: the server's database credentials, tokens
  and provider keys never reach git or anything it runs.
* ``check_repository`` refuses, with ``RepositoryRefused``, a directory whose ``.git`` points somewhere that does
  not point back (another repository, a ``core.worktree`` elsewhere, a ``gitdir:`` link that is not a linked
  worktree's), so the checks above are made on the repository the directory claims to be.

``tests/test_git_safe.py`` fails when a direct ``git`` call appears elsewhere in ``looper/`` without a reason
listed there.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

# The empty tree's id, per object format: with it as the attribute source, no attributes apply.
EMPTY_TREE = {"sha1": "4b825dc642cb6eb9a060e54bf8d69288fbee4904",
              "sha256": "6ef19b41225c5369f1c104d45d8d85efa9b057b53b14b4b9b939dd74decc5321"}
# Each setting names a program or a place one is read from; the command line beats every configuration file,
# so the repository's value never applies.
OVERRIDES = (
    "core.hooksPath=/dev/null", "core.fsmonitor=false", "core.attributesFile=/dev/null",
    "core.sshCommand=false", "core.pager=cat", "core.editor=true", "core.askPass=true",
    "credential.helper=", "commit.gpgsign=false", "tag.gpgsign=false", "push.gpgSign=false",
    "gpg.program=false", "gpg.ssh.program=false", "gpg.x509.program=false",
    "diff.external=", "protocol.ext.allow=never", "submodule.recurse=false",
    "gc.auto=0", "maintenance.auto=false",
)
PREFLIGHT_TIMEOUT_SEC = 10

# What reaches git. Where programs and the user's files are (PATH, HOME, USER, LOGNAME), temporary files (TMPDIR),
# the locale (LANG, LANGUAGE, LC_*), where git looks for the operator's own configuration (XDG_CONFIG_HOME, and
# the GIT_CONFIG_* variables the operator, not the repository, sets). Nothing else: no LOOPER_*, database URL,
# PG*, token or key, no proxy and no ssh agent (these commands do not reach a remote).
_PASSTHROUGH = frozenset({"PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "LANGUAGE", "XDG_CONFIG_HOME",
                          "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM"})
# What a caller may add, because a call needs it: who commits, a private index, no optional lock, how reflog
# entries and merge messages read, and pathspec and discovery behavior.
CALLER_VARIABLES = frozenset({
    "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "GIT_INDEX_FILE",
    "GIT_OPTIONAL_LOCKS", "GIT_MERGE_AUTOEDIT", "GIT_REFLOG_ACTION", "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    "GIT_LITERAL_PATHSPECS", "GIT_GLOB_PATHSPECS", "GIT_NOGLOB_PATHSPECS", "GIT_ICASE_PATHSPECS",
})
# Fixed: no prompt, no pager, an editor that edits nothing, no askpass, no external diff.
_FIXED_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat", "GIT_EDITOR": "true", "GIT_ASKPASS": "true"}

# An attribute line that assigns a filter, diff or merge driver (``-diff`` and ``diff`` assign none).
_DRIVER = re.compile(r"(?:^|\s)(filter|diff|merge)=")


class RepositoryRefused(subprocess.SubprocessError):
    """Looper will not run git here: the repository's layout or its ``info/attributes`` could run its own code.

    ``reason`` is ``attributes_driver`` (``info/attributes`` assigns a driver), ``git_dir_mismatch`` (the ``.git``
    does not belong to this directory) or ``outside_worktree`` (the directory is not inside the work tree git
    reports).
    """

    def __init__(self, reason: str, directory: str | Path, detail: str):
        super().__init__(f"git is not run in {directory}: {detail}")
        self.reason = reason
        self.directory = str(directory)


def environment(extra: Mapping[str, str] | None = None, *, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment git starts with: the allowlisted variables of ``environ`` (the server's), the fixed ones, and
    ``extra`` (only names in ``CALLER_VARIABLES``; anything else is a programming error)."""
    source = os.environ if environ is None else environ
    env = {name: value for name, value in source.items() if name in _PASSTHROUGH or name.startswith("LC_")}
    env.update(_FIXED_ENV)
    for name, value in (extra or {}).items():
        if name not in CALLER_VARIABLES:
            raise ValueError(f"git_safe: {name} is not a variable a git call may set")
        env[name] = value
    return env


def argv(*args: str, directory: str | Path | None = None, object_format: str = "sha1",
         attributes: bool = True) -> list[str]:
    """``git`` with the fixed overrides, ``-C directory`` when given, then ``args``. ``attributes=False`` leaves
    out ``--attr-source`` (for a command that reads no attributes and may run outside a repository)."""
    command = ["git"]
    if attributes:
        command.append(f"--attr-source={EMPTY_TREE[object_format]}")
    for setting in OVERRIDES:
        command += ["-c", setting]
    if directory is not None:
        command += ["-C", str(directory)]
    return [*command, *args]


def _probe(directory: str | Path) -> list[str]:
    return argv("rev-parse", "--path-format=absolute", "--show-object-format", "--git-dir", "--git-common-dir",
                "--show-toplevel", directory=directory, attributes=False)


def _probe_environment() -> dict[str, str]:
    """The probe finds a repository wherever the command itself would, including across a file system boundary."""
    return environment({"GIT_DISCOVERY_ACROSS_FILESYSTEM": "1"})


def _real(path: str | Path) -> str:
    return os.path.realpath(path)


def _gitdir_link(dot_git: Path) -> str | None:
    """Where the ``gitdir:`` file ``dot_git`` points, resolved; None when it is not a readable link."""
    try:
        line = dot_git.read_text().splitlines()[0]
    except (OSError, UnicodeError, IndexError):
        return None
    target = line.removeprefix("gitdir:").strip() if line.startswith("gitdir:") else None
    return _real(dot_git.parent / target) if target else None


def _judge(directory: str | Path, stdout: str, layout: bool = True) -> str | None:
    """Check what the probe printed; the repository's object format, or None when ``directory`` is no repository.

    ``layout`` False skips the checks of where the ``.git`` points (for a directory being relocated, which the
    caller owns); ``info/attributes`` is still checked. Raises ``RepositoryRefused``."""
    lines = stdout.splitlines()
    if len(lines) < 3:
        return None  # no repository: the command itself will say so
    object_format, git_dir, common = lines[0], _real(lines[1]), _real(lines[2])
    top = _real(lines[3]) if len(lines) > 3 else None
    if object_format not in EMPTY_TREE:
        raise RepositoryRefused("git_dir_mismatch", directory, f"unknown object format {object_format}")
    if top is not None and layout:
        dot_git = Path(top, ".git")
        if dot_git.is_symlink() or not dot_git.exists():
            raise RepositoryRefused("git_dir_mismatch", directory, "the work tree has no .git of its own")
        if dot_git.is_dir():
            if not (_real(dot_git) == git_dir == common):
                raise RepositoryRefused("git_dir_mismatch", directory, "its .git is not the repository git uses")
        else:
            # A gitdir link to the repository git uses, and one of two shapes: a linked worktree, whose entry in the
            # main repository points back at this link, or a submodule's own repository under ``modules``.
            entry = Path(git_dir)
            try:
                points_back = _real(Path(entry / "gitdir").read_text().strip()) == _real(dot_git)
            except OSError:
                points_back = False
            linked = entry.parent.name == "worktrees" and _real(entry.parent.parent) == common and points_back
            submodule = git_dir == common and entry.parent.name == "modules"
            if _gitdir_link(dot_git) != git_dir or not (linked or submodule):
                raise RepositoryRefused("git_dir_mismatch", directory,
                                        "its .git file points at a repository that does not belong to this directory")
        here = _real(directory)
        if os.path.commonpath([here, top]) != top:
            raise RepositoryRefused("outside_worktree", directory, "the directory is outside the work tree git reports")
    for place in dict.fromkeys((common, git_dir)):
        try:
            text = Path(place, "info", "attributes").read_text(errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if not line.lstrip().startswith("#") and _DRIVER.search(line):
                raise RepositoryRefused("attributes_driver", directory,
                                        "info/attributes assigns a filter, diff or merge driver")
    return object_format


def check_repository(directory: str | Path, *, layout: bool = True) -> str | None:
    """The object format of the repository ``directory`` is in, or None outside one. Raises ``RepositoryRefused``.

    Blocking: from async code use ``check_repository_async``."""
    done = subprocess.run(_probe(directory), capture_output=True, text=True, timeout=PREFLIGHT_TIMEOUT_SEC,
                          env=_probe_environment(), stdin=subprocess.DEVNULL)
    return _judge(directory, done.stdout, layout)


async def check_repository_async(directory: str | Path, *, layout: bool = True) -> str | None:
    process = await asyncio.create_subprocess_exec(
        *_probe(directory), env=_probe_environment(), stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), PREFLIGHT_TIMEOUT_SEC)
    except BaseException:
        process.kill()
        await process.wait()
        raise
    return _judge(directory, stdout.decode("utf-8", "replace"), layout)


def run(directory: str | Path | None, *args: str, extra_env: Mapping[str, str] | None = None,
        **kwargs: Any) -> subprocess.CompletedProcess:
    """``subprocess.run`` of ``git -C directory args`` with the fixed overrides and the allowlisted environment;
    ``kwargs`` are ``subprocess.run``'s (not ``env``, ``cwd`` or ``shell``). ``directory`` None runs where the
    process is, without a repository check (for a ``clone`` into a new directory: check its source first).

    Raises ``RepositoryRefused`` (a ``subprocess.SubprocessError``) before running anything."""
    if {"env", "cwd", "shell"} & kwargs.keys():
        raise ValueError("git_safe.run: env, cwd and shell are not accepted")
    object_format = (check_repository(directory) if directory is not None else None) or "sha1"
    return subprocess.run(argv(*args, directory=directory, object_format=object_format),
                          env=environment(extra_env), **kwargs)


async def spawn(directory: str | Path, *args: str, extra_env: Mapping[str, str] | None = None,
                layout: bool = True, **kwargs: Any) -> asyncio.subprocess.Process:
    """``asyncio.create_subprocess_exec`` of ``git -C directory args``, as ``run`` does; ``layout`` False for a
    directory Looper is relocating (see ``check_repository``)."""
    if {"env", "cwd", "shell"} & kwargs.keys():
        raise ValueError("git_safe.spawn: env, cwd and shell are not accepted")
    object_format = await check_repository_async(directory, layout=layout) or "sha1"
    return await asyncio.create_subprocess_exec(
        *argv(*args, directory=directory, object_format=object_format), env=environment(extra_env), **kwargs)
