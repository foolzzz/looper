"""Reclaiming the read-only copies agent runs mount from under the runtime root.

Agent runs read three kinds of content from directories written once, named by
a SHA-256 of their content, under ``<runtime_root>/.local``; a change writes a
new directory and leaves the old one:

- the library's items and the generated ``personal`` skills plugin:
  ``library/<sha256>/`` (``library.py``);
- a project's text and upload documents: ``projects/<id>/knowledge/<sha256>/``
  (``knowledge.py``);
- a project's skills plugin: ``projects/<id>/skills/<sha256>/``
  (``project_skills.py``).

``reclaim_unused`` removes the ones nothing uses. ``loopx serve`` runs it at
start, before any agent launches. It keeps a directory that the database's
current content uses (what the next runs mount; loading it also writes it) or
that the command line of a running process names, as every run's
``--plugin-dir`` and ``--add-dir`` do, which covers agents an earlier server
left running and those of another server on the same runtime root; a
directory that a kept one links into stays too. When the current content or the running
processes cannot be read, nothing is removed.
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


def hashed_directories(runtime_root: Path) -> list[Path]:
    """Every content-hashed directory of the library, the projects' knowledge and the projects' skills."""
    local = runtime_root.absolute() / ".local"
    parents = [local / "library", *sorted((local / "projects").glob("proj-*/knowledge")),
               *sorted((local / "projects").glob("proj-*/skills"))]
    return [child for parent in parents if parent.is_dir() for child in sorted(parent.iterdir())
            if _HASHED.fullmatch(child.name) and child.is_dir() and not child.is_symlink()]


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
    """The directories the current library and every project's knowledge and skills use, written if missing.

    None when any of them cannot be read or written.
    """
    from ..db import projects as db_projects
    from .knowledge import load_knowledge
    from .library import load_library
    from .project_skills import load_project_skills

    library = await load_library(runtime_root)
    if library.error is not None:
        return None
    paths = list(library.plugin_dirs)
    for project in await db_projects.list_projects():
        knowledge = await load_knowledge(runtime_root, project, str(project.get("work_dir") or ""))
        skills = await load_project_skills(runtime_root, project)
        if knowledge.error is not None or skills.error is not None:
            return None
        paths += [*knowledge.add_dirs, *([skills.plugin_dir] if skills.plugin_dir else [])]
    return paths


async def reclaim_unused(runtime_root: Path) -> list[Path]:
    """Remove the copies under ``runtime_root`` that neither the current content nor a running process uses."""
    try:
        keep = await current_copies(runtime_root)
        if keep is None:
            logger.warning("not reclaiming unused copies: the current library or project content cannot be read")
            return []
        removed = reclaim(runtime_root, keep=keep, command_lines=command_lines())
    except Exception:
        logger.warning("not reclaiming unused copies", exc_info=True)
        return []
    if removed:
        logger.info("reclaimed %d unused read-only copies under %s", len(removed), runtime_root / ".local")
    return removed
