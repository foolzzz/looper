"""Reclaiming the read-only copies agent runs mount from under the runtime root.

Agent runs read four kinds of content from directories or files written once,
named by a SHA-256 of their content, under ``<runtime_root>/.local``; a change
writes a new one and leaves the old one:

- the catalog's items and the generated skill plugins (``personal``,
  ``project``): ``library/<sha256>/`` (``library.py``);
- a project's text and upload documents: ``projects/<id>/knowledge/<sha256>/``
  (``knowledge.py``);
- a project's skills plugin as versions before the catalog wrote it:
  ``projects/<id>/skills/<sha256>/``; nothing writes these any more;
- the MCP servers installed for a run: ``mcp/<sha256>.json`` (``mcp.py``);
- the homes OMP runs with, one per set of generated settings:
  ``omp/homes/<sha256>/`` (``runtimes.omp``).

``reclaim_unused`` removes the ones nothing uses. ``looper serve`` runs it at
start, before any agent launches. It keeps a directory that the database's
current content uses (what the next runs of every role in every project mount,
and those of a task agent without a project; loading it also writes it) or
that the command line of a running process names, as every run's
``--plugin-dir``, ``--add-dir`` and ``--mcp-config`` do (and an OMP run's
``--config`` its home), which covers agents an earlier server
left running and those of another server on the same runtime root; a
directory that a kept one links into stays too. When the current content or the running
processes cannot be read, nothing is removed.

The Manager and every Planner on a runtime that runs a process per turn (Codex,
OMP) keep their sessions in a directory of their own,
``<runtime_type>/sessions/<agent>``. ``reclaim_sessions`` removes the one of an
agent whose saved session (``agent_sessions``) is not of that runtime type, or
which has none, unless a running process names it.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from collections.abc import Iterable
from pathlib import Path

logger = logging.getLogger(__name__)

_HASHED = re.compile(r"[0-9a-f]{64}")
_HASHED_FILE = re.compile(r"[0-9a-f]{64}\.json")


def hashed_directories(runtime_root: Path) -> list[Path]:
    """Every content-hashed directory of the library, the projects' knowledge and the projects' skills, and
    every content-hashed MCP configuration file."""
    local = runtime_root.absolute() / ".local"
    parents = [local / "library", *sorted((local / "projects").glob("proj-*/knowledge")),
               *sorted((local / "projects").glob("proj-*/skills")), local / "omp" / "homes"]
    directories = [child for parent in parents if parent.is_dir() for child in sorted(parent.iterdir())
                   if _HASHED.fullmatch(child.name) and child.is_dir() and not child.is_symlink()]
    mcp = local / "mcp"
    files = [child for child in sorted(mcp.iterdir()) if _HASHED_FILE.fullmatch(child.name)
             and child.is_file() and not child.is_symlink()] if mcp.is_dir() else []
    return directories + files


def _forms(path: Path) -> tuple[str, ...]:
    """The spellings of ``path`` a command line may use: as given and with its links resolved."""
    return tuple(dict.fromkeys((str(path), os.path.realpath(path))))


def _named(lines: str, path: str, *, inside: bool) -> bool:
    """Whether a command line names ``path`` (or, with ``inside``, a path in it) as one argument."""
    return re.search(re.escape(path) + (r"(?=$|[/\s'\"])" if inside else r"(?=$|[\s'\"])"), lines) is not None


def _used(directories: list[Path], keep: Iterable[str | Path], command_lines: list[str]) -> set[Path]:
    """The directories in use.

    One is in use when a kept path is in it, when a command line names it or
    a path in it, or names its parent itself (the whole knowledge directory
    was mounted before it had a directory per set), and when a directory in
    use links into it.
    """
    kept = [os.path.realpath(path) for path in keep]
    lines = "\n".join(command_lines)
    by_real = {os.path.realpath(directory): directory for directory in directories}

    def in_use(real: str, directory: Path) -> bool:
        return (any(path == real or path.startswith(real + os.sep) for path in kept)
                or any(_named(lines, form, inside=True) for form in _forms(directory))
                or any(_named(lines, form, inside=False) for form in _forms(directory.parent)))

    used = {directory for real, directory in by_real.items() if in_use(real, directory)}
    pending = list(used)
    while pending:
        for walked, subdirectories, files in os.walk(pending.pop()):
            for name in [*subdirectories, *files]:
                entry = Path(walked) / name
                if not entry.is_symlink():
                    continue
                target = os.path.realpath(entry)
                for real, directory in by_real.items():
                    if directory not in used and (target == real or target.startswith(real + os.sep)):
                        used.add(directory)
                        pending.append(directory)
    return used


def _remove(directory: Path) -> None:
    if directory.is_file():
        directory.unlink()
        return
    for walked, _, _ in os.walk(directory):
        os.chmod(walked, 0o755)
    shutil.rmtree(directory)


def reclaim(runtime_root: Path, *, keep: Iterable[str | Path], command_lines: list[str]) -> list[Path]:
    """Remove the hashed directories not in use (see ``_used``); returns them.

    The project directories it empties go too.
    """
    directories = hashed_directories(runtime_root)
    used = _used(directories, keep, command_lines)
    removed = [directory for directory in directories if directory not in used]
    for directory in removed:
        try:
            _remove(directory)
        except OSError:
            logger.warning("cannot remove the unused copy %s", directory, exc_info=True)
    projects = runtime_root.absolute() / ".local" / "projects"
    for directory in sorted({d.parent for d in removed if d.parent.parent.parent == projects}):
        for empty in (directory, directory.parent):
            try:
                empty.rmdir()
            except OSError:
                break
    return removed


def command_lines() -> list[str]:
    """The command line of every running process; raises ``OSError`` when they cannot be listed."""
    try:
        listing = subprocess.run(["ps", "-A", "-ww", "-o", "args="], capture_output=True, text=True, check=True,
                                 timeout=30)
    except (subprocess.SubprocessError, OSError) as exc:
        raise OSError(f"cannot list the running processes: {exc}") from exc
    return listing.stdout.splitlines()


async def current_copies(runtime_root: Path) -> list[str] | None:
    """The directories the installed items of every role and every project's knowledge use, written if missing.

    None when any of them cannot be read or written.
    """
    from ..db import projects as db_projects
    from .catalog import ROLES, TASK_ROLES
    from .knowledge import load_knowledge
    from .library import load_installed

    paths: list[str] = []
    projects = await db_projects.list_projects()
    places = [(role, None) for role in ROLES] + [(role, str(p["id"])) for p in projects for role in TASK_ROLES]
    for role, project_id in places:
        library = await load_installed(runtime_root, role, project_id)
        if library.error is not None:
            return None
        paths += [*library.plugin_dirs, *([library.mcp_config] if library.mcp_config else [])]
    for project in projects:
        knowledge = await load_knowledge(runtime_root, project, str(project.get("work_dir") or ""))
        if knowledge.error is not None:
            return None
        paths += knowledge.add_dirs
    return paths


# The runtime types whose resident agents keep their sessions in a directory of their own (``per_turn``).
SESSION_RUNTIMES = ("codex", "omp", "grok")


def reclaim_sessions(runtime_root: Path, *, saved: Iterable[tuple[str, str | None]],
                     command_lines: list[str]) -> list[Path]:
    """Remove the session directories under ``runtime_root`` whose agent has no saved session of that runtime
    type (``saved``: each agent with a saved session and its runtime type) and that no running process names;
    returns them."""
    from .runtimes.per_turn import session_dir_name

    keep = {(runtime_type, session_dir_name(agent_id)) for agent_id, runtime_type in saved}
    lines = "\n".join(command_lines)
    removed = []
    for runtime_type in SESSION_RUNTIMES:
        sessions = runtime_root.absolute() / ".local" / runtime_type / "sessions"
        for directory in sorted(sessions.iterdir()) if sessions.is_dir() else []:
            if (runtime_type, directory.name) in keep or not directory.is_dir() or directory.is_symlink():
                continue
            if any(_named(lines, form, inside=True) for form in _forms(directory)):
                continue
            try:
                _remove(directory)
            except OSError:
                logger.warning("cannot remove the unused session directory %s", directory, exc_info=True)
                continue
            removed.append(directory)
    return removed


async def reclaim_unused(runtime_root: Path) -> list[Path]:
    """Remove the copies under ``runtime_root`` that neither the current content nor a running process uses, and
    the session directories of agents whose saved session is not of their runtime type."""
    from ..db import agent_sessions

    try:
        keep = await current_copies(runtime_root)
        if keep is None:
            logger.warning("not reclaiming unused copies: the installed items or project content cannot be read")
            return []
        saved = await agent_sessions.saved_sessions()
        lines = command_lines()
        removed = reclaim(runtime_root, keep=keep, command_lines=lines)
        removed += reclaim_sessions(runtime_root, saved=saved, command_lines=lines)
    except Exception:
        logger.warning("not reclaiming unused copies", exc_info=True)
        return []
    if removed:
        logger.info("reclaimed %d unused read-only copies under %s", len(removed), runtime_root / ".local")
    return removed
