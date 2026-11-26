"""Run git in a directory whose content Looper does not own, without running the repository's own code.

An agent can write everything in its task directory, including ``.git/config``, ``.git/hooks`` and
``.git/info/attributes`` (a linked worktree shares the main repository's). Git runs code those files name:
hooks, ``core.hooksPath``, ``core.fsmonitor``, filter, merge and diff drivers, textconv, the signing program,
``core.sshCommand``, the pager, the editor, askpass and credential helpers. When ``looper serve`` runs git there,
that code would run as the service, after the agent's run (and its time budget and process-tree kill) is over.

Every git command Looper runs in a task, step or worktree directory goes through this module:

* ``argv`` puts fixed overrides in front of the command: ``-c`` for each setting that names a program. A filter,
  merge or diff driver is named by the repository's own ``.gitattributes`` and defined in a configuration file,
  so ``.gitattributes`` stays in effect (``eol=``, ``text`` and a user's LFS filter keep working) and the
  *definitions* that come from the repository (its ``config`` and ``config.worktree``, and what they include) are
  overridden: a driver defined only there runs nothing, and one the user's or the system's configuration also
  defines gets that definition back. ``check_repository`` finds them, with ``git config --show-scope``. The
  repository's ``info/attributes`` can assign a driver without a definition of its own that we can see, so
  ``check_repository`` refuses a repository whose file assigns a filter, diff or merge driver.
* ``environment`` builds the child's environment from an allowlist: the server's database credentials, tokens
  and provider keys never reach git or anything it runs.
* ``check_repository`` refuses, with ``RepositoryRefused``, a directory whose ``.git`` points somewhere that does
  not point back (another repository, a ``core.worktree`` elsewhere, a ``gitdir:`` link that is not a linked
  worktree's or a submodule's), so the checks above are made on the repository the directory claims to be.

The drivers are found before the command runs, so an agent that writes a definition in between is not stopped
(the fixed overrides, which do not depend on the repository, are). ``cached`` reuses one check for a directory
inside a block, for an operation that runs many commands and has no agent running meanwhile.

``tests/test_git_safe.py`` fails when a direct ``git`` call appears elsewhere in ``looper/`` without a reason
listed there.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import functools
import os
import re
import subprocess
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Each setting names a program or a place one is read from; the command line beats every configuration file,
# so the repository's value never applies.
OVERRIDES = (
    "core.hooksPath=/dev/null", "core.fsmonitor=false",
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


def argv(*args: str, directory: str | Path | None = None, overrides: Sequence[str] = ()) -> list[str]:
    """``git`` with the fixed overrides, then ``overrides`` (``Inspection.overrides``), ``-C directory`` when given,
    then ``args``."""
    command = ["git"]
    for setting in (*OVERRIDES, *overrides):
        command += ["-c", setting]
    if directory is not None:
        command += ["-C", str(directory)]
    return [*command, *args]


def _probe(directory: str | Path) -> list[str]:
    return argv("rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir", "--show-toplevel",
                directory=directory)


# What the config probe asks for: every driver definition, and where attributes are read from.
_CONFIG_KEYS = r"^(filter|diff|merge)\.|^core\.attributesfile$"
# The settings of a driver that name a program (git runs them), by section.
_PROGRAMS = {"filter": ("clean", "smudge", "process"), "diff": ("textconv", "command"), "merge": ("driver",)}
# Configuration the operator owns; everything else (the repository's config, its worktree config, what they include)
# is the agent's.
_TRUSTED_SCOPES = frozenset({"system", "global"})


def _config_probe(directory: str | Path) -> list[str]:
    return argv("config", "--show-scope", "--show-origin", "-z", "--get-regexp", _CONFIG_KEYS, directory=directory)


MAX_SUBMODULE_DEPTH = 8


def _run_probe(command: list[str]) -> str:
    return subprocess.run(command, capture_output=True, text=True, timeout=PREFLIGHT_TIMEOUT_SEC,
                          env=_probe_environment(), stdin=subprocess.DEVNULL).stdout


def _submodules(top: str, depth: int = 0) -> Iterator[Path]:
    """The checked-out submodules under ``top``, nested ones too: git runs a submodule's own filters and drivers
    when a command in the superproject recurses into it, so its definitions are overridden as well."""
    if depth >= MAX_SUBMODULE_DEPTH:
        return
    entries = _run_probe(argv("ls-files", "--stage", "-z", directory=top)).split("\0")
    for entry in entries:
        mode, _, rest = entry.partition(" ")
        path = Path(top, rest.partition("\t")[2])
        if mode == "160000" and path.joinpath(".git").exists():
            yield path
            yield from _submodules(str(path), depth + 1)


def _git_dir_of(checkout: Path) -> Path:
    dot_git = checkout / ".git"
    if dot_git.is_dir():
        return dot_git
    link = _gitdir_link(dot_git)
    return Path(link) if link else dot_git


def _refuse_attributes(directory: str | Path, text: str) -> None:
    for line in text.splitlines():
        if not line.lstrip().startswith("#") and _DRIVER.search(line):
            raise RepositoryRefused("attributes_driver", directory,
                                    "info/attributes assigns a filter, diff or merge driver")


@dataclass(frozen=True)
class Inspection:
    """What ``check_repository`` found: the ``-c`` settings that replace the repository's own driver definitions."""

    overrides: tuple[str, ...] = ()


def _overrides(output: str) -> tuple[str, ...]:
    """``-c`` settings for the program-naming driver settings the repository defines: the value the user's or the
    system's configuration gives the same setting, else empty (``required`` false). Where the repository sets
    ``core.attributesFile``, the operator's value or ``/dev/null``."""
    fields = output.split("\0")
    values: dict[str, list[tuple[str, str]]] = {}
    for scope, _origin, entry in zip(fields[0::3], fields[1::3], fields[2::3]):
        key, _, value = entry.partition("\n")
        values.setdefault(key, []).append((scope, value))
    settings = []
    for key, entries in values.items():
        section, _, rest = key.partition(".")
        name, _, variable = rest.rpartition(".")
        if key == "core.attributesfile":
            is_program, empty = True, "/dev/null"
        elif section in _PROGRAMS and name and (variable in _PROGRAMS[section] or
                                                 (section == "filter" and variable == "required")):
            is_program, empty = True, "false" if variable == "required" else ""
        else:
            is_program, empty = False, ""
        if not is_program or all(scope in _TRUSTED_SCOPES or scope == "command" for scope, _ in entries):
            continue
        trusted = [value for scope, value in entries if scope in _TRUSTED_SCOPES]
        settings.append(f"{key}={trusted[-1] if trusted else empty}")
    return tuple(sorted(settings))


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


def _judge(directory: str | Path, stdout: str, layout: bool = True) -> tuple[str | None, bool]:
    """Check what the probe printed: the work tree's top (None without one) and whether ``directory`` is in a
    repository at all.

    ``layout`` False skips the checks of where the ``.git`` points (for a directory being relocated, which the
    caller owns); ``info/attributes`` is still checked. Raises ``RepositoryRefused``."""
    lines = stdout.splitlines()
    if len(lines) < 2:
        return None, False  # no repository: the command itself will say so
    git_dir, common = _real(lines[0]), _real(lines[1])
    top = _real(lines[2]) if len(lines) > 2 else None
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
    return top, True


_cache: contextvars.ContextVar[dict | None] = contextvars.ContextVar("git_safe_cache", default=None)


@contextlib.contextmanager
def cached() -> Iterator[None]:
    """Within the block, a directory is checked once: for an operation that runs many commands in one directory
    while no agent writes there. A block inside a block shares the outer one's results."""
    if _cache.get() is not None:
        yield
        return
    token = _cache.set({})
    try:
        yield
    finally:
        _cache.reset(token)


def cached_operation(function):
    """``function`` runs inside ``cached()``."""
    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        with cached():
            return function(*args, **kwargs)
    return wrapper


def _cache_key(directory: str | Path, layout: bool) -> tuple[str, bool]:
    return _real(directory), layout


def check_repository(directory: str | Path, *, layout: bool = True) -> Inspection | None:
    """What git_safe found in the repository ``directory`` is in, or None outside one.
    Raises ``RepositoryRefused``. Blocking: from async code use ``check_repository_async``."""
    store = _cache.get()
    key = _cache_key(directory, layout)
    if store is not None and key in store:
        return store[key]
    top, inside = _judge(directory, _run_probe(_probe(directory)), layout)
    found = None
    if inside:
        config = _run_probe(_config_probe(directory))
        for module in _submodules(top) if top is not None else ():  # not gated on .gitmodules: the agent writes it
            config += _run_probe(_config_probe(module))
            try:
                attributes = (_git_dir_of(module) / "info" / "attributes").read_text(errors="replace")
            except OSError:
                continue
            _refuse_attributes(directory, attributes)
        found = Inspection(_overrides(config))
    if store is not None:
        store[key] = found
    return found


async def check_repository_async(directory: str | Path, *, layout: bool = True) -> Inspection | None:
    return await asyncio.wait_for(asyncio.to_thread(check_repository, directory, layout=layout),
                                  PREFLIGHT_TIMEOUT_SEC * 4)


def run(directory: str | Path | None, *args: str, extra_env: Mapping[str, str] | None = None,
        **kwargs: Any) -> subprocess.CompletedProcess:
    """``subprocess.run`` of ``git -C directory args`` with the fixed overrides and the allowlisted environment;
    ``kwargs`` are ``subprocess.run``'s (not ``env``, ``cwd`` or ``shell``). ``directory`` None runs where the
    process is, without a repository check (for a ``clone`` into a new directory: check its source first).

    Raises ``RepositoryRefused`` (a ``subprocess.SubprocessError``) before running anything."""
    if {"env", "cwd", "shell"} & kwargs.keys():
        raise ValueError("git_safe.run: env, cwd and shell are not accepted")
    inspection = check_repository(directory) if directory is not None else None
    return subprocess.run(argv(*args, directory=directory, overrides=inspection.overrides if inspection else ()),
                          env=environment(extra_env), **kwargs)


async def spawn(directory: str | Path, *args: str, extra_env: Mapping[str, str] | None = None,
                layout: bool = True, **kwargs: Any) -> asyncio.subprocess.Process:
    """``asyncio.create_subprocess_exec`` of ``git -C directory args``, as ``run`` does; ``layout`` False for a
    directory Looper is relocating (see ``check_repository``)."""
    if {"env", "cwd", "shell"} & kwargs.keys():
        raise ValueError("git_safe.spawn: env, cwd and shell are not accepted")
    inspection = await check_repository_async(directory, layout=layout)
    return await asyncio.create_subprocess_exec(
        *argv(*args, directory=directory, overrides=inspection.overrides if inspection else ()), env=environment(extra_env), **kwargs)
