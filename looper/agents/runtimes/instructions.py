"""A project's own instruction files, ``CLAUDE.md`` and ``AGENTS.md``, and which of them a run's runtime is given.

The runtimes read different ones. A run's project files are those in the
directories from its work directory's git repository root down to the work
directory (``project_dirs``; the work directory alone outside a repository,
never one above it). Verified against Claude Code 2.1.285, Codex 0.162.0 and
omp 18.4.3 under Looper's isolation (the multi-runtime RFC, section 9):

- **Claude Code** reads every ``CLAUDE.md`` of the project and, when the
  project has none, its ``AGENTS.md`` files instead: its built-in default
  (``claude_cli``'s notes);
- **Codex** reads one file a directory: ``AGENTS.md``, else ``CLAUDE.md``,
  the fallback name Looper's config gives it (``codex.FALLBACK_NAMES``);
- **omp** reads one file a directory, ``AGENTS.md`` before ``CLAUDE.md``.
  Outside a repository its discovery is off (it would climb to the
  filesystem's root, ``omp.CLIMBING_SOURCES``), so the work directory's file
  goes into the system prompt Looper gives it instead (``Delivery.INJECTED``,
  ``injected_section``), up to ``INJECTED_MAX_BYTES``.

So each runtime sees the file it reads itself, and the other one when the
project has only that; no file is given twice. Each run's record names the
files and how each was given (``record``). Only these two names are looked
at: a ``.claude/CLAUDE.md``, Codex's ``AGENTS.override.md`` or a project's own
``instructionFiles`` setting for Claude Code are left to the runtime.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from ..claude_cli import repository_root

CLAUDE_MD = "CLAUDE.md"
AGENTS_MD = "AGENTS.md"
# How much of an injected file a system prompt carries: as much as Codex reads of a project's own files
# (its project_doc_max_bytes default).
INJECTED_MAX_BYTES = 32 * 1024


class Delivery(str, Enum):
    """How a runtime is given one of a project's instruction files."""

    NATIVE = "native"  # the runtime reads the file itself
    INJECTED = "injected"  # Looper puts its text in the system prompt it gives the runtime


@dataclass(frozen=True)
class InstructionFile:
    """One of a project's instruction files a run's runtime is given, and how."""

    path: Path
    delivery: Delivery = Delivery.NATIVE


def project_dirs(cwd: str | Path) -> list[Path]:
    """The directories whose instruction files a run in ``cwd`` is given: its repository's root down to ``cwd``."""
    directories = [Path(cwd).absolute()]
    root = repository_root(directories[0])
    while directories[-1] != root:
        directories.append(directories[-1].parent)
    return directories[::-1]


def _is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


def named(cwd: str | Path, name: str) -> list[Path]:
    """Every file called ``name`` in the project of a run in ``cwd``, the root's first."""
    return [directory / name for directory in project_dirs(cwd) if _is_file(directory / name)]


def first_per_directory(cwd: str | Path, names: Sequence[str]) -> list[Path]:
    """In each directory of the project of a run in ``cwd``, the root first, the first of ``names`` it has."""
    found = []
    for directory in project_dirs(cwd):
        if (path := next((directory / name for name in names if _is_file(directory / name)), None)) is not None:
            found.append(path)
    return found


@dataclass(frozen=True)
class Injected:
    """What a system prompt carries of an injected file: its text up to ``INJECTED_MAX_BYTES`` and how many bytes
    were left out; or why the file could not be read."""

    text: str = ""
    size: int = 0
    clipped: int = 0
    error: str | None = None


def read_injected(path: Path) -> Injected:
    """The part of ``path`` a system prompt carries."""
    try:
        data = path.read_bytes()
    except OSError as exc:
        return Injected(error=f"{type(exc).__name__}: {exc.strerror or exc}")
    return Injected(data[:INJECTED_MAX_BYTES].decode("utf-8", errors="replace"), len(data),
                    max(len(data) - INJECTED_MAX_BYTES, 0))


def injected_section(files: Sequence[InstructionFile], runtime_label: str) -> str:
    """The system prompt's part for the injected ones of ``files``, saying where each came from; ``''`` without
    any (or when none could be read)."""
    sections = []
    for file in files:
        if file.delivery is not Delivery.INJECTED or (given := read_injected(file.path)).error is not None:
            continue
        lines = [f"# Project instructions: {file.path.name}", "",
                 f"The work directory's own {file.path.name} ({file.path}), which {runtime_label} does not load "
                 "here, so Looper gives it to you:", "", given.text.strip()]
        if given.clipped:
            lines += ["", f"[clipped: {given.clipped} more bytes; read the file for the rest]"]
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def record(files: Sequence[InstructionFile], cwd: str | Path) -> list[dict[str, Any]]:
    """``files`` in a run's record: each one's path from the work directory ``cwd`` and how it was given; for an
    injected one, the sha256 of the text given, the file's size and how many bytes were clipped, or why it could
    not be read."""
    entries = []
    for file in files:
        entry: dict[str, Any] = {"path": os.path.relpath(file.path, Path(cwd).absolute()),
                                 "delivery": file.delivery.value}
        if file.delivery is Delivery.INJECTED:
            given = read_injected(file.path)
            entry.update({"error": given.error} if given.error is not None else {
                "sha256": hashlib.sha256(given.text.strip().encode("utf-8")).hexdigest(), "bytes": given.size,
                "clipped_bytes": given.clipped})
        entries.append(entry)
    return entries
