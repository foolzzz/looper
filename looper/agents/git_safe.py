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
* An object id means the object the repository itself holds: ``argv`` and ``environment`` turn replacement refs
  (``refs/replace``) off, and ``check_repository`` refuses a repository (or a checked-out submodule) that borrows
  another object store (a non-empty ``objects/info/alternates`` or ``http-alternates``). ``environment`` also
  turns lazy fetches off: in a partial clone, objects not yet fetched are reported missing by Looper's git calls
  instead of being fetched.
* ``environment`` builds the child's environment from an allowlist: the server's database credentials, tokens
  and provider keys never reach git or anything it runs.
* ``check_repository`` refuses, with ``RepositoryRefused``, a directory whose ``.git`` points somewhere that does
  not point back (another repository, a ``core.worktree`` elsewhere, a ``gitdir:`` link that is not a linked
  worktree's or a submodule's), so the checks above are made on the repository the directory claims to be.
* A repository (or a checked-out submodule) whose git directory holds a symbolic link where git reads refs or its own
  files is refused (``ref_links``): git would read, write and delete through it, outside the repository. This part is
  never remembered, inside ``cached()`` either. Before every command ``run`` and ``spawn`` start, a fixed check
  (``ref_links.check``) looks at the git directory and the common directory; a command that writes refs
  (``_WRITING``) also has the reflogs it writes checked. ``check_repository`` without ``args`` walks everything
  (``ref_links.find`` besides the fixed check): at the start and end of a direct task Manager turn and of a Worker
  step, for a command that reads reflogs, and when this process first judges a repository (and again after a walk
  that refused it, until it is repaired).
* ``environment`` points ``GIT_GRAFT_FILE`` at ``/dev/null``: a graft file changes no history Looper reads.

**Known race limits.** A check and the command it precedes are not atomic: a process writing the repository at the same
time can put a link in place after the check, and git follows it. A direct task Manager session checks again before and
after each turn, while it holds the directory's lock. In orchestrated mode this risk remains: the Worker is not
sandboxed and can already read and write such files itself, but what a git command that succeeds then prints could
still reach Looper's records (a failed command's output does not, ``GitFailure``).
The check before a command is not the full walk: a link placed deeper under ``logs``, or among another worktree's own
refs and root pseudo-refs, between two full walks is found at the next full walk, or at the next command that writes
that reflog. Reading commands in between do not go through it (a command given a reflog walks everything; revisions
passed on standard input are not looked at for that), and no command Looper runs reads another worktree's root
pseudo-refs (``ORIG_HEAD``, ...).
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
import inspect
import logging
import os
import re
import stat
import subprocess
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import ref_links

# Each setting names a program or a place one is read from; the command line beats every configuration file,
# so the repository's value never applies.
OVERRIDES = (
    "core.hooksPath=/dev/null", "core.fsmonitor=false",
    "core.sshCommand=false", "core.pager=cat", "core.editor=true", "core.askPass=true",
    "credential.helper=", "commit.gpgsign=false", "tag.gpgsign=false", "push.gpgSign=false",
    "gpg.program=false", "gpg.ssh.program=false", "gpg.x509.program=false",
    "diff.external=", "protocol.ext.allow=never", "submodule.recurse=false",
    "gc.auto=0", "maintenance.auto=false",
    # Not a program: a replacement ref would make git answer with another object than the one asked for.
    "core.useReplaceRefs=false",
    # Printed for any graft file git opens, the empty one ``environment`` names too.
    "advice.graftFileDeprecated=false",
)
PREFLIGHT_TIMEOUT_SEC = 10
# The ``RepositoryRefused`` reasons about links in the git directory; ``path`` names the place inside the repository.
REF_REASONS = ("ref_symlink", "ref_unchecked")
logger = logging.getLogger(__name__)

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
# Fixed: no prompt, no pager, an editor that edits nothing, no askpass, no external diff, no replacement refs, no
# grafts, and no lazy fetch (for a missing object git would read a promisor remote the repository names, through a
# transport program its configuration chooses).
_FIXED_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat", "GIT_EDITOR": "true", "GIT_ASKPASS": "true",
              "GIT_NO_REPLACE_OBJECTS": "1", "GIT_NO_LAZY_FETCH": "1", "GIT_GRAFT_FILE": os.devnull}

# An attribute line that assigns a filter, diff or merge driver (``-diff`` and ``diff`` assign none).
_DRIVER = re.compile(r"(?:^|\s)(filter|diff|merge)=")
# The files under ``objects/info`` that point a repository at other object stores.
_ALTERNATES = ("alternates", "http-alternates")


class RepositoryRefused(subprocess.SubprocessError):
    """Looper will not run git here: the repository's layout or its ``info/attributes`` could run its own code, or
    it borrows another repository's objects.

    ``reason`` is ``attributes_driver`` (``info/attributes`` assigns a driver), ``git_dir_mismatch`` (the ``.git``
    does not belong to this directory), ``outside_worktree`` (the directory is not inside the work tree git
    reports), ``object_alternates`` (``objects/info/alternates`` or ``http-alternates`` is not empty),
    ``ref_symlink`` (the git directory holds a symbolic link where git reads refs or its own files) or
    ``ref_unchecked`` (such a place could not be looked at).
    For the last two, ``path`` names the place (``.git/refs/heads/x``), never what a link points at.
    """

    def __init__(self, reason: str, directory: str | Path, detail: str, *, path: str | None = None):
        super().__init__(f"git is not run in {directory}: {detail}")
        self.reason = reason
        self.directory = str(directory)
        self.path = path


@dataclass(frozen=True)
class GitFailure:
    """A git command that failed, as it may be said outside git: its subcommand and exit code, never what it
    printed. Git's messages can quote file content that Looper does not control, so a failed command's output must
    not reach an exception, a returned value, an event, a prompt, the database or the log."""

    subcommand: str
    exit_code: int

    @classmethod
    def of(cls, args: Sequence[str], exit_code: int) -> GitFailure:
        """The failure of ``git args``, which are Looper's own: the subcommand is the first that is no option."""
        return cls(next((arg for arg in args if not arg.startswith("-")), "command"), exit_code)

    def __str__(self) -> str:
        return f"git_failed:{self.subcommand} (exit {self.exit_code})"


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


def _drivers(checkout: str | Path, depth: int = 0) -> tuple[str, list[Path]]:
    """The driver-definition answers (``git config --show-scope`` output) for the checkout and the submodules
    under it, nested ones too, with every gitlink path seen; and a refusal when a submodule's ``info/attributes``
    assigns a driver or the submodule borrows another object store: git runs a submodule's own filters and drivers,
    and reads its objects, when a command in the superproject recurses into it. Cached per checkout inside
    ``cached()``, so a submodule seen from its superproject and on its own is asked about once."""
    store = _cache.get()
    key = ("drivers", _real(checkout))
    if store is not None and key in store:
        return store[key]
    answer, links = _run_probe(_config_probe(checkout)), []
    if depth < MAX_SUBMODULE_DEPTH:
        for entry in _run_probe(argv("ls-files", "--stage", "-z", directory=checkout)).split("\0"):
            mode, _, rest = entry.partition(" ")
            module = Path(checkout, rest.partition("\t")[2])
            if mode != "160000":
                continue
            links.append(module)
            if module.joinpath(".git").exists():
                _refuse_alternates(checkout, _common_dir_of(_git_dir_of(module)))
                text, nested = _drivers(module, depth + 1)
                answer, links = answer + text, links + nested
                try:
                    attributes = (_git_dir_of(module) / "info" / "attributes").read_text(errors="replace")
                except OSError:
                    continue
                _refuse_attributes(checkout, attributes)
    if store is not None:
        store[key] = (answer, links)
    return answer, links


# --- Remembering a check while the files it read are unchanged ---

MEMO_SECONDS = 120
MEMO_LIMIT = 256
_memo: dict[tuple[str, bool], tuple[float, tuple, Inspection, tuple]] = {}
_memo_lock = threading.Lock()
_INCLUDE = re.compile(rb"^\s*\[\s*include", re.IGNORECASE | re.MULTILINE)


def _stat(path: Path) -> tuple[int, int, int, int] | None:
    """Modification time, change time, size and inode: ``os.utime`` can set the first, never the change time."""
    try:
        info = os.stat(path)
    except OSError:
        return None
    return info.st_mtime_ns, info.st_ctime_ns, info.st_size, info.st_ino


def _repository_files(git_dir: Path, common: Path) -> list[Path]:
    return [git_dir / "config", common / "config", git_dir / "config.worktree", common / "info" / "attributes",
            git_dir / "info" / "attributes", git_dir / "index", git_dir / "gitdir", git_dir / "commondir",
            *_alternates_files(common)]


def _files_state(sources: Sequence[tuple[Path, Path]]) -> list[Any] | None:
    parts: list[Any] = []
    for repository, shared in sources:
        for path in _repository_files(repository, shared):
            parts.append(_stat(path))
            if path.name in ("config", "config.worktree"):
                try:
                    if _INCLUDE.search(path.read_bytes()):
                        return None  # an included file is not tracked
                except OSError:
                    pass
    return parts


def _signature(directory: str | Path, top: str | None, git_dir: str, common: str,
               links: Sequence[Path] | None) -> tuple | None:
    """What a check depends on, as the state of the files it read: every ancestor's ``.git`` from the directory
    to the top and the repository's configuration, attributes, index, link and alternates files; with ``links``,
    the same for each gitlink (checked out or not). None when a configuration includes other files, which are not
    tracked."""
    parts: list[Any] = []
    here = Path(_real(directory))
    for ancestor in (here, *here.parents):
        parts.append(_stat(ancestor / ".git"))
        if top is None or str(ancestor) == top:
            break
    sources = [(Path(git_dir), Path(common))]
    for module in links or ():
        parts.append(_stat(module / ".git"))
        if module.joinpath(".git").exists():
            dir_of = _git_dir_of(module)
            sources.append((dir_of, _common_dir_of(dir_of)))
    state = _files_state(sources)
    return None if state is None else tuple(parts + state)


_MISS = object()
# The common directories this process has walked in full and found no link in, oldest first.
_walked: dict[str, None] = {}


def _remembered(key: tuple[str, bool], directory: str | Path) -> Any:
    with _memo_lock:
        entry = _memo.get(key)
    if entry is None or time.monotonic() - entry[0] > MEMO_SECONDS:
        return _MISS
    _, signature, inspection, args = entry
    return inspection if _signature(directory, *args) == signature else _MISS


def _remember(key: tuple[str, bool], signature: tuple, inspection: Inspection, args: tuple) -> None:
    with _memo_lock:
        _memo[key] = (time.monotonic(), signature, inspection, args)
        while len(_memo) > MEMO_LIMIT:
            _memo.pop(next(iter(_memo)))


def _git_dir_of(checkout: Path) -> Path:
    dot_git = checkout / ".git"
    if dot_git.is_dir():
        return dot_git
    link = _gitdir_link(dot_git)
    return Path(link) if link else dot_git


def _common_dir_of(git_dir: Path) -> Path:
    """The repository that holds ``git_dir``'s objects: what its ``commondir`` file names, else itself."""
    try:
        line = (git_dir / "commondir").read_text().splitlines()[0]
    except (OSError, UnicodeError, IndexError):
        return git_dir
    return Path(_real(git_dir / line))


def _alternates_files(common: Path) -> list[Path]:
    return [common / "objects" / "info" / name for name in _ALTERNATES]


def _refuse_alternates(directory: str | Path, common: str | Path) -> None:
    """Refuse unless each alternates file of the repository ``common`` is missing or an empty file."""
    for path in _alternates_files(Path(common)):
        try:
            info = os.stat(path)
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError:
            pass  # there, and unreadable: refused
        else:
            if stat.S_ISREG(info.st_mode) and not info.st_size:
                continue
        raise RepositoryRefused("object_alternates", directory,
                                f"objects/info/{path.name} is not empty: the repository borrows another object store "
                                "(`git repack -a -d`, then removing the file, makes it self-contained)")


def _refuse_attributes(directory: str | Path, text: str) -> None:
    for line in text.splitlines():
        if not line.lstrip().startswith("#") and _DRIVER.search(line):
            raise RepositoryRefused("attributes_driver", directory,
                                    "info/attributes assigns a filter, diff or merge driver")


@dataclass(frozen=True)
class Inspection:
    """What ``check_repository`` found: the ``-c`` settings that replace the repository's own driver definitions, and
    where the git directories it looks for links in on every call are."""

    overrides: tuple[str, ...] = ()
    top: str | None = field(default=None, compare=False)
    git_dir: str = field(default="", compare=False)
    common: str = field(default="", compare=False)
    modules: tuple[Path, ...] = field(default=(), compare=False)  # every gitlink, checked out or not


def _ref_directories(directory: str | Path, found: Inspection) -> Iterator[tuple[str, str]]:
    """Each git directory whose ref storage git may use for ``found``, with the name its places are reported under:
    the repository's own, and those of its checked-out submodules. A linked worktree's entry is walked as part of the
    repository that holds it."""
    def places(git_dir: str, common: str) -> tuple[str, ...]:
        covered = os.path.dirname(git_dir) == os.path.join(common, "worktrees")
        return (common,) if covered or git_dir == common else (common, git_dir)

    for place in places(found.git_dir, found.common):
        yield place, ".git"
    for module in found.modules:
        if not module.joinpath(".git").exists():
            continue
        git_dir = _real(_git_dir_of(module))
        for place in places(git_dir, str(_common_dir_of(Path(git_dir)))):
            if place.startswith(found.common + os.sep):
                yield place, f".git/{os.path.relpath(place, found.common)}"
            else:
                yield place, f"{os.path.relpath(module, found.top or directory)}/.git"


# The subcommands that can write a ref, and with it a reflog. ``worktree`` only with one of ``_WORKTREE_WRITES``.
_WRITING = frozenset({"am", "branch", "checkout", "cherry-pick", "commit", "fetch", "merge", "pull", "rebase",
                      "remote", "reset", "revert", "switch", "tag", "update-ref", "worktree"})
_WORKTREE_WRITES = frozenset({"add", "move", "prune", "remove", "repair"})
# Writing subcommands whose arguments do not name the refs written: every reflog is walked.
_UNNAMED_WRITES = frozenset({"fetch", "pull", "remote"})
# git's own options that take the next argument as their value.
_VALUE_OPTIONS = frozenset({"-c", "-C", "--git-dir", "--work-tree", "--namespace", "--config-env"})


def _subcommand(args: Sequence[str]) -> tuple[str, Sequence[str]]:
    """The subcommand of ``git *args`` and what follows it (``""`` without one)."""
    index = 0
    while index < len(args) and args[index].startswith("-"):
        index += 2 if args[index] in _VALUE_OPTIONS else 1
    return (args[index], args[index + 1:]) if index < len(args) else ("", ())


def _reads_reflogs(args: Sequence[str]) -> bool:
    """Whether the command can read any reflog: everything is walked before it."""
    return (_subcommand(args)[0] in ("reflog", "stash")
            or any("@{" in arg or arg in ("-g", "--reflog", "--walk-reflogs") for arg in args))


def _written_reflogs(found: Inspection, args: Sequence[str]) -> tuple[bool, tuple[str, ...]] | None:
    """For a command that can write refs: whether every reflog is to be walked, and else the reflogs (below ``logs``)
    of the refs it may write: ``HEAD``'s branch and every ref an argument may name. None for any other command."""
    command, rest = _subcommand(args)
    if command not in _WRITING or (command == "worktree" and not _WORKTREE_WRITES & set(rest[:1])):
        return None
    if command in _UNNAMED_WRITES or "--stdin" in rest:
        return True, ()
    words = []
    for arg in rest:
        if arg.startswith("--"):
            arg = arg.partition("=")[2]
        elif arg.startswith("-"):
            arg = arg[2:]  # a value joined to a short option
        words += arg.removeprefix("+").split(":")
    names = [f"{prefix}{word}" for word in words if word
             for prefix in ("", "refs/", "refs/tags/", "refs/heads/", "refs/remotes/")]
    if (target := ref_links.head_target(found.git_dir)) is not None:
        names.append(target)
    return False, tuple(dict.fromkeys(names))


def _own_directories(directory: str | Path, found: Inspection) -> Iterator[tuple[str, str]]:
    """The common directory and the git directory of the repository and of each checked-out submodule, with the name
    their places are reported under."""
    repositories = [(found.common, found.git_dir, None)]
    for module in found.modules:
        if module.joinpath(".git").exists():
            git_dir = _real(_git_dir_of(module))
            repositories.append((str(_common_dir_of(Path(git_dir))), git_dir, module))
    for common, git_dir, module in repositories:
        for place in dict.fromkeys((common, git_dir)):
            if place == found.common:
                yield place, ".git"
            elif place.startswith(found.common + os.sep):
                yield place, f".git/{os.path.relpath(place, found.common)}"
            elif module is None:
                yield place, ".git"
            else:
                yield place, f"{os.path.relpath(module, found.top or directory)}/.git"


def _raise_for(directory: str | Path, finding: ref_links.Finding | None) -> None:
    if finding is None:
        return
    if finding.link:
        raise RepositoryRefused("ref_symlink", directory,
                                f"{finding.path} is a symbolic link, which git would follow out of the repository "
                                "(replace it with a real file or directory)", path=finding.path)
    raise RepositoryRefused("ref_unchecked", directory,
                            f"{finding.path} could not be checked for symbolic links", path=finding.path)


def _refuse_ref_links(directory: str | Path, found: Inspection, args: Sequence[str] | None) -> None:
    """Refuse when a git directory holds a symbolic link where git reads refs or its own files, or such a place cannot
    be checked: the fixed check for the command ``args``, and the full walk without ``args``, for a command that reads
    reflogs and for a repository this process has not walked in full. Reads the file system every time."""
    full = args is None or _reads_reflogs(args) or found.common not in _walked
    writes = None if full or args is None else _written_reflogs(found, args)
    try:
        for place, label in _own_directories(directory, found):
            log_tree, reflogs = False, ()
            if writes is not None:
                every, named = writes
                # This worktree's own reflogs are all walked; the shared ones, those of the refs named.
                log_tree = every or place == found.git_dir
                reflogs = named if place == found.common else ()
            _raise_for(directory, ref_links.check(place, label, log_tree=log_tree, reflogs=reflogs))
        if full:
            for place, label in _ref_directories(directory, found):
                _raise_for(directory, ref_links.find(place, label))
    except RepositoryRefused:
        _walked.pop(found.common, None)  # walked in full again, until a walk passes
        raise
    if full:
        with _memo_lock:
            _walked[found.common] = None
            while len(_walked) > MEMO_LIMIT:
                _walked.pop(next(iter(_walked)))


def _refuse_links_without_a_repository(directory: str | Path) -> None:
    """git found no repository (a link may be why, and the command would still read through it): the fixed check of
    the git directory the nearest ``.git`` above ``directory`` names, and of its common directory."""
    here = Path(_real(directory))
    for ancestor in (here, *here.parents):
        if not os.path.lexists(ancestor / ".git"):
            continue
        git_dir = _real(_git_dir_of(ancestor))
        _raise_for(directory, ref_links.check(git_dir, ".git"))  # before its ``commondir`` is read
        common = str(_common_dir_of(Path(git_dir)))
        if common != git_dir:
            _raise_for(directory, ref_links.check(common, ".git"))
        return


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


def _judge(directory: str | Path, stdout: str, layout: bool = True) -> tuple[str | None, bool, str, str]:
    """Check what the probe printed: the work tree's top (None without one) and whether ``directory`` is in a
    repository at all.

    ``layout`` False skips the checks of where the ``.git`` points (for a directory being relocated, which the
    caller owns); ``info/attributes`` and the alternates files are still checked. Raises ``RepositoryRefused``."""
    lines = stdout.splitlines()
    if len(lines) < 2:
        return None, False, "", ""  # no repository: the command itself will say so
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
    _refuse_alternates(directory, common)
    for place in dict.fromkeys((common, git_dir)):
        try:
            text = Path(place, "info", "attributes").read_text(errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if not line.lstrip().startswith("#") and _DRIVER.search(line):
                raise RepositoryRefused("attributes_driver", directory,
                                        "info/attributes assigns a filter, diff or merge driver")
    return top, True, git_dir, common


_cache: contextvars.ContextVar[dict | None] = contextvars.ContextVar("git_safe_cache", default=None)


@contextlib.contextmanager
def cached() -> Iterator[None]:
    """Within the block, a directory is checked once: for an operation that runs many commands in one directory
    while no agent writes there. The commands in a block are not re-checked afterwards (``recheck``), so a driver
    that appears during one logs no warning; the worker's verification snapshot, baseline, restore and checkout
    run in blocks. A block inside a block shares the outer one's results."""
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


def check_repository(directory: str | Path, *, layout: bool = True,
                     args: Sequence[str] | None = None) -> Inspection | None:
    """What git_safe found in the repository ``directory`` is in, or None outside one.
    Raises ``RepositoryRefused``. Blocking: from async code use ``check_repository_async``.

    Links are looked for on every call, whatever else is reused: without ``args`` the full walk, with the arguments
    of the git command about to run the check that command needs (``_refuse_ref_links``)."""
    found = _inspect(directory, layout)
    if found is not None:
        _refuse_ref_links(directory, found, args)
    else:
        _refuse_links_without_a_repository(directory)
    return found


def _inspect(directory: str | Path, layout: bool) -> Inspection | None:
    """The part of ``check_repository`` that may be reused.

    An answer is remembered (up to ``MEMO_SECONDS``) while the files it read are unchanged (``_signature``): a
    configuration that includes other files is never remembered, and any change to a ``.git``, a configuration,
    ``info/attributes``, the index, an alternates file or a submodule's, gives a fresh check."""
    store = _cache.get()
    key = _cache_key(directory, layout)
    if store is not None and key in store:
        return store[key]
    found = _remembered(key, directory)
    if found is _MISS:
        found = None
        output = _run_probe(_probe(directory))
        top, inside, git_dir, common = _judge(directory, output, layout)
        if inside:
            before = _signature(directory, top, git_dir, common, None)
            text, links = _drivers(top if top is not None else directory)
            found = Inspection(_overrides(text), top, git_dir, common, tuple(links))
            args = (top, git_dir, common, tuple(links))
            # Only what was read unchanged since before the probes is remembered.
            if before is not None and _signature(directory, top, git_dir, common, None) == before:
                signature = _signature(directory, *args)
                if signature is not None:
                    _remember(key, signature, found, args)
    if store is not None:
        store[key] = found
    return found


async def check_repository_async(directory: str | Path, *, layout: bool = True,
                                 args: Sequence[str] | None = None) -> Inspection | None:
    return await asyncio.wait_for(asyncio.to_thread(check_repository, directory, layout=layout, args=args),
                                  PREFLIGHT_TIMEOUT_SEC * 4)


_attribution: contextvars.ContextVar[tuple[str, str]] = contextvars.ContextVar("git_safe_attribution",
                                                                              default=("unknown", "unknown"))


@contextlib.contextmanager
def running_for(task_id: object = None, step_id: object = None) -> Iterator[None]:
    """Name the task and step the git commands inside the block are run for (``"unknown"`` where not given), for
    the warning ``recheck`` logs."""
    token = _attribution.set((str(task_id) if task_id else "unknown", str(step_id) if step_id else "unknown"))
    try:
        yield
    finally:
        _attribution.reset(token)


def attributed(function):
    """``function`` (sync or async) runs inside ``running_for`` with the ids found in its arguments: ``task_id``,
    ``step_id`` or ``step_key``, a mapping ``task`` with an ``id``, or an object (a workspace) with ``task_id``
    and ``step_id``."""
    signature = inspect.signature(function)

    def ids(args, kwargs) -> tuple[object, object]:
        bound = signature.bind_partial(*args, **kwargs).arguments
        task = bound.get("task_id") or (bound["task"].get("id") if isinstance(bound.get("task"), Mapping) else None)
        step = bound.get("step_id") or bound.get("step_key")
        for value in bound.values():
            task = task or getattr(value, "task_id", None)
            step = step or getattr(value, "step_id", None)
        return task, step

    if inspect.iscoroutinefunction(function):
        @functools.wraps(function)
        async def awrapper(*args, **kwargs):
            with running_for(*ids(args, kwargs)):
                return await function(*args, **kwargs)
        return awrapper

    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        with running_for(*ids(args, kwargs)):
            return function(*args, **kwargs)
    return wrapper


def recheck(directory: str | Path, before: Inspection | None) -> None:
    """After a command: ask for the driver definitions again (the repository's own configuration only, one git
    process) and log one WARNING when a repository-defined driver appeared that ``before`` did not find: someone
    wrote it while the command ran, which the check before the command cannot stop. Skipped inside ``cached()``,
    whose blocks run where no agent writes. Never raises."""
    if before is None or _cache.get() is not None:
        return
    try:
        appeared = sorted({entry.partition("=")[0] for entry in _overrides(_run_probe(_config_probe(directory)))}
                          - {entry.partition("=")[0] for entry in before.overrides})
    except (OSError, subprocess.SubprocessError):
        return
    if appeared:
        task_id, step_id = _attribution.get()
        logger.warning("git_safe: a repository-defined driver appeared while git ran in %s (task %s, step %s): %s",
                       directory, task_id, step_id, ", ".join(appeared))


async def recheck_async(process: asyncio.subprocess.Process) -> None:
    """``recheck`` for a process ``spawn`` started, once it has finished."""
    target = getattr(process, "git_safe", None)
    if target is not None:
        await asyncio.to_thread(recheck, *target)


def run(directory: str | Path | None, *args: str, extra_env: Mapping[str, str] | None = None,
        **kwargs: Any) -> subprocess.CompletedProcess:
    """``subprocess.run`` of ``git -C directory args`` with the fixed overrides and the allowlisted environment;
    ``kwargs`` are ``subprocess.run``'s (not ``env``, ``cwd`` or ``shell``). ``directory`` None runs where the
    process is, without a repository check (for a ``clone`` into a new directory: check its source first).

    Raises ``RepositoryRefused`` (a ``subprocess.SubprocessError``) before running anything."""
    if {"env", "cwd", "shell"} & kwargs.keys():
        raise ValueError("git_safe.run: env, cwd and shell are not accepted")
    inspection = check_repository(directory, args=args) if directory is not None else None
    done = subprocess.run(argv(*args, directory=directory, overrides=inspection.overrides if inspection else ()),
                          env=environment(extra_env), **kwargs)
    if directory is not None:
        recheck(directory, inspection)
    return done


async def spawn(directory: str | Path, *args: str, extra_env: Mapping[str, str] | None = None,
                layout: bool = True, **kwargs: Any) -> asyncio.subprocess.Process:
    """``asyncio.create_subprocess_exec`` of ``git -C directory args``, as ``run`` does; ``layout`` False for a
    directory Looper is relocating (see ``check_repository``)."""
    if {"env", "cwd", "shell"} & kwargs.keys():
        raise ValueError("git_safe.spawn: env, cwd and shell are not accepted")
    inspection = await check_repository_async(directory, layout=layout, args=args)
    process = await asyncio.create_subprocess_exec(
        *argv(*args, directory=directory, overrides=inspection.overrides if inspection else ()),
        env=environment(extra_env), **kwargs)
    process.git_safe = (directory, inspection)  # for recheck_async, once the caller has the command's result
    return process
