"""The operator's notes for a directory: the Claude Code auto memory of the directory a task works in, read-only.

A project with ``operator_notes`` on (``project_execution``) gives its
Planners, Workers and Reviewers these notes in their system prompt, after the
project's knowledge (``knowledge.load_knowledge``), on every runtime. The
design is the task workspaces RFC (``docs/architecture/rfcs/task-workspaces-v0``,
W6):

- the notes are where the operator's Claude Code keeps the directory's auto
  memory: ``<config>/projects/<key>/memory/`` (``notes_dir``), with ``<config>``
  ``$CLAUDE_CONFIG_DIR`` or ``~/.claude``; the key is the top level of the
  repository's main checkout (so a subdirectory, a task worktree and a step
  worktree all have the same notes), or the directory itself outside git,
  symbolic links resolved, made a directory name as Claude Code makes it
  (``directory_name``; verified against Claude Code 2.1.285);
- ``MEMORY.md`` comes first, then the ``.md`` files of the memory directory
  it links to, in its order, within ``NOTES_MAX_BYTES``; a file past the
  limit is named, not included;
- Looper only reads: it never creates, changes, moves or deletes anything
  under the operator's Claude Code configuration, and the agents get the
  text, not the path.

``LOOPER_OPERATOR_NOTES_ROOT`` stands in for ``<config>/projects`` (tests and
evals).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

NOTES_ROOT_ENV = "LOOPER_OPERATOR_NOTES_ROOT"
NOTES_MAX_BYTES = 24 * 1024
INDEX = "MEMORY.md"
INDEX_MAX_LINES = 200  # as much of the index as Claude Code itself loads
_NAME_MAX = 200
_GIT_TIMEOUT_SEC = 10
# A Markdown link to a file in the memory directory: [title](name.md), the name without a directory.
_LINK = re.compile(r"\[[^\]]*\]\(\s*<?([^)\s/<>]+\.md)>?\s*\)")

HEADING = "## The operator's notes for this directory"
LEAD = ("The operator's own Claude Code keeps these notes about this directory; Looper gives you a read-only copy. "
        "They are background from earlier sessions and may be out of date: where they disagree with your task or "
        "with what the files say now, the task and the files win. Do not try to change them.")


@dataclass(frozen=True)
class OperatorNotes:
    """The notes a run gets: the section for its system prompt, and what it holds."""

    text: str = ""
    files: tuple[str, ...] = ()  # included, MEMORY.md first
    omitted: tuple[str, ...] = ()  # linked, but past the limit or unreadable
    error: str | None = None  # why they could not be read

    @property
    def sha256(self) -> str | None:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest() if self.text else None

    def record(self) -> dict[str, object]:
        """What a run's ``prompt_layers.project.operator_notes`` records."""
        return {"sha256": self.sha256, "files": list(self.files), "omitted": list(self.omitted),
                **({"error": self.error} if self.error else {})}


NO_NOTES = OperatorNotes()


def notes_root() -> Path:
    """Where the operator's Claude Code keeps a directory per project (``<config>/projects``)."""
    if configured := os.environ.get(NOTES_ROOT_ENV):
        return Path(configured).expanduser()
    from .library import claude_config_dir

    return claude_config_dir() / "projects"


def _djb2(text: str) -> int:
    """JavaScript's 32-bit djb2 over UTF-16 code units, as Claude Code hashes a long directory name."""
    value = 0
    data = text.encode("utf-16-le")
    for index in range(0, len(data), 2):
        value = ((value << 5) - value + int.from_bytes(data[index:index + 2], "little")) & 0xFFFFFFFF
    return value - (1 << 32) if value >= 1 << 31 else value


def _base36(number: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    text = ""
    while True:
        number, rest = divmod(number, 36)
        text = digits[rest] + text
        if number == 0:
            return text


def directory_name(key: str) -> str:
    """``key`` as Claude Code names its directory: every character but a letter or digit a ``-``; past 200
    characters, the first 200, ``-`` and the key's hash."""
    name = re.sub(r"[^a-zA-Z0-9]", "-", key)
    if len(name) <= _NAME_MAX:
        return name
    return f"{name[:_NAME_MAX]}-{_base36(abs(_djb2(key)))}"


def notes_key(work_dir: str) -> str:
    """The directory Claude Code keys ``work_dir``'s auto memory by: the top level of its repository's main checkout,
    else ``work_dir`` itself; symbolic links resolved."""
    directory = os.path.realpath(work_dir)
    env = {name: value for name, value in os.environ.items()
           if name not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR")}
    try:
        run = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=directory,
                             env=env, capture_output=True, text=True, timeout=_GIT_TIMEOUT_SEC,
                             stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return directory
    common = Path(run.stdout.strip())
    if run.returncode != 0 or common.name != ".git":
        return directory  # outside git, or a repository without a main checkout of its own
    return os.path.realpath(common.parent)


def notes_dir(work_dir: str) -> Path:
    return notes_root() / directory_name(notes_key(work_dir)) / "memory"


def _read(path: Path, root: Path, budget: int) -> str | None:
    """The file's text when it is a regular file in ``root`` (through links too) within ``budget`` bytes."""
    try:
        real = path.resolve(strict=True)
        if not real.is_relative_to(root) or not real.is_file() or real.stat().st_size > budget:
            return None
        return real.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def read_notes(work_dir: str) -> OperatorNotes:
    """The operator's notes for ``work_dir`` (``NO_NOTES`` when there are none), read only."""
    try:
        directory = notes_dir(work_dir)
        root = directory.resolve(strict=True)
    except OSError:
        return NO_NOTES
    index_path = root / INDEX
    try:
        real = index_path.resolve(strict=True)
        if not real.is_relative_to(root) or not real.is_file():
            return NO_NOTES
        lines = real.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return NO_NOTES
    except OSError as exc:
        return OperatorNotes(error=f"cannot read the operator's notes: {exc.strerror or exc}")
    prefix = "\n".join([HEADING, LEAD, "", f"### {INDEX}", ""])
    budget = NOTES_MAX_BYTES - len(prefix.encode("utf-8"))
    index = "\n".join(lines[:INDEX_MAX_LINES]).strip().encode("utf-8")[:budget].decode("utf-8", "ignore")
    footer = f"\n\nNot included (past the {NOTES_MAX_BYTES // 1024} KiB limit, or not readable): "
    names = [name for name in dict.fromkeys(_LINK.findall(index)) if name != INDEX]
    # Reserve the whole possible footer before fitting the index and linked files.
    reserved = len((footer + ", ".join(names)).encode("utf-8")) if names else 0
    index = index.encode("utf-8")[:max(0, budget - reserved)].decode("utf-8", "ignore").strip()
    if not index:
        return NO_NOTES
    parts = [prefix + index]
    used = len(parts[0].encode("utf-8"))
    files, omitted = [INDEX], []
    for name in dict.fromkeys(_LINK.findall(index)):
        if name == INDEX:
            continue
        text = _read(root / name, root, NOTES_MAX_BYTES - used - reserved)
        block = f"\n\n### {name}\n{text.strip()}" if text is not None else ""
        if not block or used + len(block.encode("utf-8")) + reserved > NOTES_MAX_BYTES:
            omitted.append(name)
            continue
        parts.append(block)
        used += len(block.encode("utf-8"))
        files.append(name)
    if omitted:
        parts.append(footer + ", ".join(omitted))
    return OperatorNotes(text="".join(parts), files=tuple(files), omitted=tuple(omitted))


async def load_notes(work_dir: str) -> OperatorNotes:
    """``read_notes`` off the event loop; a failure is logged and the run goes without them."""
    try:
        return await asyncio.to_thread(read_notes, work_dir)
    except Exception as exc:
        logger.warning("cannot read the operator's notes for %s", work_dir, exc_info=True)
        return OperatorNotes(error=f"cannot read the operator's notes: {exc}")


def notes_note(notes: OperatorNotes) -> str:
    """The activity line a run's notes get: which it applied, or why it went without them; '' for none."""
    if notes.error:
        return f"Warning: {notes.error}; this run goes without them"[:500]
    if not notes.text:
        return ""
    count = len(notes.files)
    return (f"The operator's notes for this directory applied ({count} file{'' if count == 1 else 's'}"
            + (f", {len(notes.omitted)} left out" if notes.omitted else "") + ")")


def notes_view(work_dir: str) -> dict[str, object]:
    """For the workspace check: whether ``work_dir`` has notes, and which files."""
    notes = read_notes(work_dir)
    return {"found": bool(notes.text), "files": list(notes.files), "omitted": list(notes.omitted),
            **({"error": notes.error} if notes.error else {})}
