"""A project's knowledge: reference documents its task agents read on demand.

A project can carry material too large to put in a prompt (RFC section 6,
M3). Each document has a title, a one-line summary and one of three sources
(RFC decision 3; there are no URLs):

- ``text``: written on the dashboard and stored in ``project_documents``;
- ``upload``: a text file uploaded as base64 in JSON, stored the same way;
- ``path``: a file in the project's default directory, referenced by its path
  relative to that directory and read live.

Agents do not get the documents in their prompt. The project layer (L3) of the
Planner's, the Workers' and the Reviewers' system prompt gains an index after
the background (``## Project knowledge``): under a line naming each directory
once, one line per document with its title, its summary and its path relative
to that directory; a path document whose file is gone is listed as missing.
Claude Code resolves a relative path against the working directory only, not
against an ``--add-dir`` root, so the index names the root and the agent joins
the two. Short paths keep the index near its document cap: the absolute path
was about 150 characters a line. Text and upload documents are materialized
read-only together, one directory per set of documents named by their hash:
``<runtime_root>/.local/projects/<project_id>/knowledge/<set_sha256>/<filename>``
(a file name two documents share gets ``-2``, ``-3``). Every run of those
agents mounts that directory with ``--add-dir`` (``claude_cli.knowledge_args``,
through ``agent_settings.Launch``); it also mounts the project's directory
when the project has a path document and the task runs in another directory.
The Manager spans every project and gets neither. Everything is read when a
run starts, so a change reaches the next Worker or Reviewer, and the resident
Planner restarts on its next turn. A set no current run uses is removed when
the server starts (``read_only_copies``).

Caps, each refused with a message that states it: a document has at most
``DOCUMENT_MAX_BYTES``; a project at most ``PROJECT_MAX_DOCUMENTS`` documents
and ``PROJECT_MAX_BYTES`` of stored content (a path document is not stored);
the composed index at most ``INDEX_MAX_CHARS`` characters. Only text is
accepted: content that is not UTF-8 or holds a NUL byte is refused as binary.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import os
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DOCUMENT_MAX_BYTES = 2 * 1024 * 1024
PROJECT_MAX_DOCUMENTS = 100
PROJECT_MAX_BYTES = 50 * 1024 * 1024
INDEX_MAX_CHARS = 4_000
TITLE_MAX_CHARS = 120
SUMMARY_MAX_CHARS = 300
FILENAME_MAX_CHARS = 100

INDEX_HEADING = "## Project knowledge"
INDEX_INTRO = (
    "Reference documents for this project. They are not in this prompt: read the ones your work needs and "
    "say which ones you used. A document's path is relative to the directory named above it."
)
# Names the layout of a set's directory: change it when the layout changes.
_SET_LAYOUT_ID = "knowledge-set/v1"

_PROJECT_ID = re.compile(r"proj-[A-Za-z0-9]+")
_UNSAFE_FILENAME = re.compile(r"[^\w.-]+")


class SourceKind(str, Enum):
    TEXT = "text"
    UPLOAD = "upload"
    PATH = "path"


# The kinds whose content the database holds and runs read from a materialized copy.
STORED_KINDS = frozenset({SourceKind.TEXT, SourceKind.UPLOAD})


class KnowledgeError(ValueError):
    """A document or a request the API refuses (answered with 400)."""


class KnowledgeNotFound(LookupError):
    """No such project, or no such document in it (answered with 404)."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _size(limit: int) -> str:
    """A cap for people: ``2 MiB (2097152 bytes)``."""
    return f"{limit // (1024 * 1024)} MiB ({limit} bytes)"


# --- Validation: each raises KnowledgeError ---


def _one_line(raw: Any, field: str, max_chars: int) -> str:
    if not isinstance(raw, str):
        raise KnowledgeError(f"a document needs a {field} (a string)")
    text = raw.strip()
    if not text:
        raise KnowledgeError(f"a document needs a {field}")
    if "\n" in text or "\r" in text:
        raise KnowledgeError(f"a document's {field} is one line")
    if len(text) > max_chars:
        raise KnowledgeError(f"a document's {field} has at most {max_chars} characters, got {len(text)}")
    return text


def document_title(raw: Any) -> str:
    return _one_line(raw, "title", TITLE_MAX_CHARS)


def document_summary(raw: Any) -> str:
    """The required one-line summary the index shows beside the title."""
    return _one_line(raw, "summary", SUMMARY_MAX_CHARS)


def _check_size(size: int, label: str) -> None:
    if size > DOCUMENT_MAX_BYTES:
        raise KnowledgeError(f"a document has at most {_size(DOCUMENT_MAX_BYTES)}; {label} has {size} bytes")


def text_from_bytes(data: bytes, label: str) -> str:
    """``data`` as text; content that is not UTF-8, or holds a NUL byte, is refused as binary."""
    _check_size(len(data), label)
    try:
        if b"\x00" in data:
            raise UnicodeDecodeError("utf-8", data, data.index(b"\x00"), data.index(b"\x00") + 1, "NUL byte")
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise KnowledgeError(
            f"{label} is not a text file (it is not UTF-8 text or holds NUL bytes); knowledge takes text such "
            "as Markdown, plain text, source code, CSV or JSON"
        ) from None


def text_content(raw: Any) -> str:
    """A text document's content: a non-empty string of at most ``DOCUMENT_MAX_BYTES``."""
    if not isinstance(raw, str) or not raw.strip():
        raise KnowledgeError("a text document needs its content (a non-empty string)")
    if "\x00" in raw:
        raise KnowledgeError("a text document cannot hold NUL characters")
    _check_size(len(raw.encode("utf-8")), "the content")
    return raw


def safe_filename(raw: str, *, default: str) -> str:
    """A file name to materialize under: the last path component, with only letters, digits, ``.``, ``-`` and ``_``."""
    name = re.split(r"[/\\]", raw)[-1]
    name = _UNSAFE_FILENAME.sub("-", name).replace("-.", ".").strip(".-")
    if not name:
        return default
    if len(name) > FILENAME_MAX_CHARS:
        stem, dot, extension = name.rpartition(".")
        keep = extension if dot and 0 < len(extension) <= 16 else ""
        name = name[: FILENAME_MAX_CHARS - len(keep) - (1 if keep else 0)].rstrip(".-") + (f".{keep}" if keep else "")
    return name


def text_filename(title: str) -> str:
    """A text document's file name, from its title."""
    return safe_filename(f"{title}.md", default="document.md")


def upload(filename_raw: Any, content_raw: Any) -> tuple[str, str]:
    """An upload's safe file name and its text, from ``filename`` and ``content_base64``."""
    if not isinstance(filename_raw, str) or not filename_raw.strip():
        raise KnowledgeError("an upload needs its filename")
    if not isinstance(content_raw, str):
        raise KnowledgeError("an upload needs content_base64, the file's bytes in base64")
    label = filename_raw.strip()
    # Base64 is 4 characters per 3 bytes: refuse an oversized file before decoding it.
    if len(content_raw) > (DOCUMENT_MAX_BYTES + 2) // 3 * 4 + 4:
        raise KnowledgeError(f"a document has at most {_size(DOCUMENT_MAX_BYTES)}; {label} is larger")
    try:
        data = base64.b64decode(content_raw, validate=True)
    except (binascii.Error, ValueError):
        raise KnowledgeError(f"content_base64 of {label} is not valid base64") from None
    text = text_from_bytes(data, label)
    if not text.strip():
        raise KnowledgeError(f"{label} is empty")
    return safe_filename(label, default="document.txt"), text


def _inside(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def project_file(raw: Any, project_dir: str) -> tuple[str, Path]:
    """A path document's path: normalized and relative to ``project_dir``, and the file it names now.

    The file must exist, be a regular file and, links followed, stay inside
    the project's directory. An absolute path inside it is taken as relative.
    """
    if not project_dir:
        raise KnowledgeError("the project has no default directory; set one to reference a file in it")
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
        raise KnowledgeError("a path document needs its path, relative to the project's default directory")
    root = Path(project_dir)
    real_root = root.resolve()
    given = Path(os.path.expanduser(raw.strip()))
    if given.is_absolute():
        normalized = Path(os.path.normpath(given))
        for base in (root.absolute(), real_root):
            if _inside(normalized, base):
                given = normalized.relative_to(base)
                break
        else:
            raise KnowledgeError(f"{raw.strip()} is outside the project's default directory {project_dir}")
    relative = os.path.normpath(given)
    if relative == "." or relative == ".." or relative.startswith("../"):
        raise KnowledgeError(f"{raw.strip()} is outside the project's default directory {project_dir}")
    target = root / relative
    try:
        real = target.resolve(strict=True)
    except (OSError, RuntimeError):
        raise KnowledgeError(f"{relative} does not exist in the project's default directory {project_dir}") from None
    if not _inside(real, real_root):
        raise KnowledgeError(f"{relative} leads outside the project's default directory {project_dir}")
    if not real.is_file():
        raise KnowledgeError(f"{relative} is not a file; reference a file in the project's default directory")
    return Path(relative).as_posix(), real


def _readable_project_file(project_dir: str, relative: str) -> Path | None:
    """The file a path document names now, when it exists and stays inside the project's directory."""
    if not project_dir:
        return None
    root = Path(project_dir)
    try:
        real = (root / relative).resolve(strict=True)
        return real if _inside(real, root.resolve()) and real.is_file() else None
    except (OSError, RuntimeError):
        return None


# --- Where documents are read from, and the index ---


def knowledge_dir(runtime_root: Path, project_id: str) -> Path:
    """``<runtime_root>/.local/projects/<project_id>/knowledge``: the project's materialized documents."""
    if not _PROJECT_ID.fullmatch(project_id):
        raise KnowledgeError(f"not a project id: {project_id!r}")
    return runtime_root.absolute() / ".local" / "projects" / project_id / "knowledge"


@dataclass(frozen=True)
class StoredSet:
    """Where a project's text and upload documents are materialized: one directory and a file name each."""

    directory: Path | None  # None without stored documents
    names: Mapping[str, str]  # document id -> file name in ``directory``


def stored_set(runtime_root: Path, project_id: str, rows: Sequence[Mapping[str, Any]]) -> StoredSet:
    """The set directory of the stored documents in ``rows``: ``<knowledge_dir>/<set_sha256>``.

    Each document keeps its file name; a later document whose name an earlier
    one took (ignoring case, as macOS does) gets ``-2``, ``-3`` before its
    extension. The hash covers the names and the contents' hashes.
    """
    names: dict[str, str] = {}
    taken: set[str] = set()
    for row in rows:
        if SourceKind(row["source_kind"]) not in STORED_KINDS:
            continue
        stem, dot, extension = str(row["filename"]).rpartition(".")
        if not dot:
            stem, extension = str(row["filename"]), ""
        name, number = str(row["filename"]), 1
        while name.casefold() in taken:
            number += 1
            name = f"{stem}-{number}{dot}{extension}"
        taken.add(name.casefold())
        names[str(row["id"])] = name
    if not names:
        return StoredSet(None, {})
    listing = [[names[str(row["id"])], str(row["sha256"])] for row in rows if str(row["id"]) in names]
    key = _sha256(json.dumps([_SET_LAYOUT_ID, listing], separators=(",", ":")).encode("utf-8"))
    return StoredSet(knowledge_dir(runtime_root, project_id) / key, names)


@dataclass(frozen=True)
class IndexEntry:
    title: str
    summary: str
    root: str  # the directory the path is relative to
    path: str
    missing: bool = False

    def line(self) -> str:
        return f"- {self.title}: {self.summary} ({'missing: ' if self.missing else ''}{self.path})"

    @property
    def read_path(self) -> str:
        return str(Path(self.root) / self.path) if self.root else self.path


def compose_index(entries: Sequence[IndexEntry]) -> str:
    """The ``## Project knowledge`` section; ``''`` without documents.

    The entries are grouped by their directory, which a line names once.
    """
    if not entries:
        return ""
    lines = [INDEX_HEADING, INDEX_INTRO]
    for root in dict.fromkeys(entry.root for entry in entries):
        lines.append(f"In {root or 'the project directory, which is not set'}:")
        lines.extend(entry.line() for entry in entries if entry.root == root)
    return "\n".join(lines)


def clipped_index(entries: Sequence[IndexEntry]) -> str:
    """The index, cut to ``INDEX_MAX_CHARS`` with a note when it no longer fits.

    Saving refuses a document that would make the index too long, so this only
    clips an index that grew afterwards (a longer project directory, say).
    """
    index = compose_index(entries)
    if len(index) <= INDEX_MAX_CHARS:
        return index
    for kept in range(len(entries) - 1, -1, -1):
        note = (f"- ({len(entries) - kept} more documents are not listed: the index is over its limit of "
                f"{INDEX_MAX_CHARS} characters)")
        clipped = "\n".join([compose_index(entries[:kept]) or f"{INDEX_HEADING}\n{INDEX_INTRO}", note])
        if len(clipped) <= INDEX_MAX_CHARS:
            return clipped
    return ""


def _entry(stored: StoredSet, project: Mapping[str, Any], row: Mapping[str, Any]) -> IndexEntry:
    project_dir = str(project.get("work_dir") or "")
    if SourceKind(row["source_kind"]) is SourceKind.PATH:
        relative = str(row["path"])
        missing = _readable_project_file(project_dir, relative) is None
        return IndexEntry(str(row["title"]), str(row["summary"]), project_dir, relative, missing)
    return IndexEntry(str(row["title"]), str(row["summary"]), str(stored.directory), stored.names[str(row["id"])])


def _index_entries(runtime_root: Path, project: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> list[IndexEntry]:
    """The index's entries: the stored documents first, then the path documents, each in the rows' order."""
    stored = stored_set(runtime_root, str(project["id"]), rows)
    entries = [_entry(stored, project, row) for row in rows]
    return sorted(entries, key=lambda entry: entry.root != str(stored.directory))


# --- Materializing a stored document ---


def materialize(target: Path, row: Mapping[str, Any], content: str) -> Path:
    """Write a stored document read-only at ``target``, in its set's directory; returns ``target``.

    A file whose content already matches the hash is kept; a damaged one is
    replaced. Writes go through a temporary file, so a reader never sees a
    partial one. Raises ``KnowledgeError`` when ``content`` does not match the
    document's hash.
    """
    data = content.encode("utf-8")
    if _sha256(data) != row["sha256"]:
        raise KnowledgeError(f"the content of document {row['id']} does not match its hash")
    try:
        if _sha256(target.read_bytes()) == row["sha256"]:
            return target
        logger.warning("knowledge file %s does not match its hash; writing it again", target)
    except FileNotFoundError:
        pass
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.parent / f".{uuid.uuid4().hex}.tmp"
    try:
        temporary.write_bytes(data)
        os.chmod(temporary, 0o444)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def _is_materialized(path: Path, sha256: str) -> bool:
    try:
        return _sha256(path.read_bytes()) == sha256
    except OSError:
        return False


# --- What one agent run gets ---


@dataclass(frozen=True)
class AgentKnowledge:
    """A run's knowledge: the index for its system prompt and the directories its command mounts."""

    index: str = ""
    documents: int = 0
    add_dirs: tuple[str, ...] = ()  # each one --add-dir, absolute
    error: str | None = None  # why the run goes without its project's knowledge

    @property
    def sha256(self) -> str | None:
        return _sha256(self.index.encode("utf-8")) if self.index else None


EMPTY_KNOWLEDGE = AgentKnowledge()


def documents_label(count: int) -> str:
    """``1 document``, ``2 documents``."""
    return f"{count} document{'' if count == 1 else 's'}"


def _same_directory(a: str, b: str) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except (OSError, RuntimeError):
        return a == b


async def load_knowledge(runtime_root: Path, project: Mapping[str, Any] | None, work_dir: str) -> AgentKnowledge:
    """The knowledge of ``project`` (its row: ``id`` and ``work_dir``) for one run in ``work_dir``.

    Stored documents are written from the database the first time a run
    needs them. When the documents cannot be read or written, the run goes
    without its knowledge and ``error`` says why.
    """
    from ..db import project_documents as db_documents

    project_id = str((project or {}).get("id") or "")
    if not project or not project_id:
        return EMPTY_KNOWLEDGE
    try:
        rows = await db_documents.list_documents(project_id)
    except Exception:
        logger.warning("cannot read the knowledge of project %s; the run goes without it", project_id, exc_info=True)
        return AgentKnowledge(error="the project's knowledge could not be read")
    if not rows:
        return EMPTY_KNOWLEDGE
    stored = stored_set(runtime_root, project_id, rows)
    try:
        for row in rows:
            if (name := stored.names.get(str(row["id"]))) is None:
                continue
            target = stored.directory / name
            if not _is_materialized(target, str(row["sha256"])):
                content = await db_documents.get_content(str(row["id"]))
                if content is None:
                    raise KnowledgeError(f"document {row['id']} has no content")
                await asyncio.to_thread(materialize, target, row, content)
    except Exception as exc:
        logger.warning("cannot prepare the knowledge of project %s; the run goes without it", project_id,
                       exc_info=True)
        return AgentKnowledge(error=f"the project's knowledge could not be prepared: {exc}")
    project_dir = str(project.get("work_dir") or "")
    add_dirs: list[str] = []
    if stored.directory is not None:
        add_dirs.append(str(stored.directory))
    if (any(SourceKind(row["source_kind"]) is SourceKind.PATH for row in rows) and project_dir
            and Path(project_dir).is_dir() and not _same_directory(project_dir, work_dir)):
        add_dirs.append(project_dir)
    index = clipped_index(_index_entries(runtime_root, project, rows))
    return AgentKnowledge(index=index, documents=len(rows), add_dirs=tuple(add_dirs))


# --- The API: list, add, change and delete a project's documents ---


def document_view(stored: StoredSet, project: Mapping[str, Any], row: Mapping[str, Any]) -> dict[str, Any]:
    """A document as the API shows it: no content; ``read_path`` is where agents read it."""
    entry = _entry(stored, project, row)
    kind = SourceKind(row["source_kind"])
    size = int(row["size_bytes"])
    if kind is SourceKind.PATH and (live := _readable_project_file(str(project.get("work_dir") or ""), str(row["path"]))):
        size = live.stat().st_size
    return {
        "document_id": row["id"],
        "title": row["title"],
        "summary": row["summary"],
        "source_kind": kind.value,
        "filename": row["filename"] or None,
        "path": row["path"],
        "read_path": entry.read_path,
        "missing": entry.missing,
        "size_bytes": size,
        "sha256": row["sha256"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _view(runtime_root: Path, project: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    stored = stored_set(runtime_root, str(project["id"]), rows)
    index = compose_index(_index_entries(runtime_root, project, rows))
    return {
        "project_id": project["id"],
        "documents": [document_view(stored, project, row) for row in rows],
        "count": len(rows),
        "stored_bytes": _stored_bytes(rows),
        "index": index,
        "index_chars": len(index),
        "caps": {
            "document_max_bytes": DOCUMENT_MAX_BYTES,
            "project_max_documents": PROJECT_MAX_DOCUMENTS,
            "project_max_bytes": PROJECT_MAX_BYTES,
            "index_max_chars": INDEX_MAX_CHARS,
        },
    }


def _stored_bytes(rows: Sequence[Mapping[str, Any]]) -> int:
    return sum(int(row["size_bytes"]) for row in rows if SourceKind(row["source_kind"]) in STORED_KINDS)


async def knowledge_view(project_id: str, runtime_root: Path) -> dict[str, Any]:
    """The project's documents, their totals, the index agents get and the caps."""
    from ..db import project_documents as db_documents
    from ..db import projects as db_projects

    project = await db_projects.get_project(project_id)
    if project is None:
        raise KnowledgeNotFound(f"project {project_id!r} not found")
    return _view(runtime_root, project, await db_documents.list_documents(project_id))


async def get_document(project_id: str, document_id: str, runtime_root: Path) -> dict[str, Any]:
    """One document with its ``content``: stored for text and upload, the file as it is now for a path document.

    ``content`` is None, with ``content_error`` saying why, when a path document's file is missing or not text.
    """
    from ..db import project_documents as db_documents
    from ..db import projects as db_projects

    project = await db_projects.get_project(project_id)
    if project is None:
        raise KnowledgeNotFound(f"project {project_id!r} not found")
    rows = await db_documents.list_documents(project_id)
    row = next((row for row in rows if row["id"] == document_id), None)
    if row is None:
        raise KnowledgeNotFound(f"project {project_id!r} has no document {document_id!r}")
    view = document_view(stored_set(runtime_root, project_id, rows), project, row)
    if SourceKind(row["source_kind"]) in STORED_KINDS:
        return {"document": view, "content": await db_documents.get_content(document_id)}
    live = _readable_project_file(str(project.get("work_dir") or ""), str(row["path"]))
    if live is None:
        return {"document": view, "content": None,
                "content_error": f"{row['path']} is missing from the project's default directory"}
    try:
        return {"document": view, "content": text_from_bytes(live.read_bytes(), str(row["path"]))}
    except (KnowledgeError, OSError) as exc:
        return {"document": view, "content": None, "content_error": str(exc)}


_COMMON_FIELDS = {"title", "summary"}
_SOURCE_FIELDS = {
    SourceKind.TEXT: {"content"},
    SourceKind.UPLOAD: {"filename", "content_base64"},
    SourceKind.PATH: {"path"},
}


def _source_kind(raw: Any) -> SourceKind:
    try:
        return SourceKind(raw)
    except ValueError:
        raise KnowledgeError(
            f"source_kind must be one of {', '.join(kind.value for kind in SourceKind)}; URLs are not supported"
        ) from None


def _check_fields(data: Mapping[str, Any], kind: SourceKind, *, extra: set[str] = frozenset()) -> None:
    allowed = _COMMON_FIELDS | _SOURCE_FIELDS[kind] | extra
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise KnowledgeError(f"unknown field {unknown[0]!r} for a {kind.value} document; the fields are "
                             f"{', '.join(sorted(allowed))}")


def _source(kind: SourceKind, data: Mapping[str, Any], project: Mapping[str, Any], *, title: str) -> dict[str, Any]:
    """The source columns of a document from the request: filename, content or path, size and hash."""
    if kind is SourceKind.TEXT:
        content = text_content(data.get("content"))
        encoded = content.encode("utf-8")
        return {"filename": text_filename(title), "content": content, "size_bytes": len(encoded),
                "sha256": _sha256(encoded)}
    if kind is SourceKind.UPLOAD:
        filename, content = upload(data.get("filename"), data.get("content_base64"))
        encoded = content.encode("utf-8")
        return {"filename": filename, "content": content, "size_bytes": len(encoded), "sha256": _sha256(encoded)}
    relative, real = project_file(data.get("path"), str(project.get("work_dir") or ""))
    data_bytes = real.read_bytes()
    text_from_bytes(data_bytes, relative)
    return {"path": relative, "size_bytes": len(data_bytes), "sha256": _sha256(data_bytes)}


def _check_caps(
    runtime_root: Path, project: Mapping[str, Any], rows: Sequence[Mapping[str, Any]],
) -> None:
    """Refuse ``rows`` (the project's documents after the change) when they break a project cap."""
    if len(rows) > PROJECT_MAX_DOCUMENTS:
        raise KnowledgeError(f"a project has at most {documents_label(PROJECT_MAX_DOCUMENTS)}")
    stored = _stored_bytes(rows)
    if stored > PROJECT_MAX_BYTES:
        raise KnowledgeError(
            f"a project stores at most {_size(PROJECT_MAX_BYTES)} of documents; this would make {stored} bytes"
        )
    index = compose_index(_index_entries(runtime_root, project, rows))
    if len(index) > INDEX_MAX_CHARS:
        raise KnowledgeError(
            f"the knowledge index agents get has at most {INDEX_MAX_CHARS} characters; this would make it "
            f"{len(index)}: shorten titles or summaries, or remove a document"
        )


def _pending_row(project_id: str, fields: Mapping[str, Any], *, like: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """A row as it would be stored, to check the caps before writing it."""
    row = dict(like or {"id": "doc-pending", "project_id": project_id, "filename": "", "path": None,
                        "created_at": None, "updated_at": None})
    row.update({key: value for key, value in fields.items() if key != "content"})
    return row


async def _with_document(project_id: str, document_id: str, runtime_root: Path) -> dict[str, Any]:
    """The knowledge view after a change, with the changed document's view as ``document``."""
    view = await knowledge_view(project_id, runtime_root)
    return {"document": next(doc for doc in view["documents"] if doc["document_id"] == document_id), **view}


async def add_document(project_id: str, data: Mapping[str, Any], runtime_root: Path) -> dict[str, Any]:
    """Add a document (``source_kind``, ``title``, ``summary`` and its source's fields); returns the view."""
    from ..db import project_documents as db_documents

    if not isinstance(data, Mapping):
        raise KnowledgeError("body must be a JSON object")
    kind = _source_kind(data.get("source_kind"))
    _check_fields(data, kind, extra={"source_kind"})
    title, summary = document_title(data.get("title")), document_summary(data.get("summary"))
    async with db_documents.locked_project(project_id) as (conn, project):
        if project is None:
            raise KnowledgeNotFound(f"project {project_id!r} not found")
        fields = {"title": title, "summary": summary, "source_kind": kind.value,
                  **_source(kind, data, project, title=title)}
        rows = await db_documents.list_documents(project_id, pool=conn)
        _check_caps(runtime_root, project, [*rows, _pending_row(project_id, fields)])
        row = await db_documents.insert_document(conn, project_id=project_id, **fields)
    return await _with_document(project_id, str(row["id"]), runtime_root)


async def update_document(
    project_id: str, document_id: str, data: Mapping[str, Any], runtime_root: Path,
) -> dict[str, Any]:
    """Change a document's title, summary or source content (its kind stays); returns the view."""
    from ..db import project_documents as db_documents

    if not isinstance(data, Mapping) or not data:
        raise KnowledgeError("body must be a JSON object with the fields to change")
    async with db_documents.locked_project(project_id) as (conn, project):
        if project is None:
            raise KnowledgeNotFound(f"project {project_id!r} not found")
        rows = await db_documents.list_documents(project_id, pool=conn)
        current = next((row for row in rows if row["id"] == document_id), None)
        if current is None:
            raise KnowledgeNotFound(f"project {project_id!r} has no document {document_id!r}")
        kind = SourceKind(current["source_kind"])
        if "source_kind" in data and data["source_kind"] != kind.value:
            raise KnowledgeError("a document's source cannot change; delete it and add a new one")
        _check_fields(data, kind, extra={"source_kind"})
        fields: dict[str, Any] = {}
        if "title" in data:
            fields["title"] = document_title(data["title"])
        if "summary" in data:
            fields["summary"] = document_summary(data["summary"])
        if _SOURCE_FIELDS[kind] & set(data):
            fields.update(_source(kind, data, project, title=fields.get("title", current["title"])))
        elif kind is SourceKind.TEXT and "title" in fields:
            fields["filename"] = text_filename(fields["title"])
        if not fields:
            raise KnowledgeError("give at least one field to change")
        changed = _pending_row(project_id, fields, like=current)
        _check_caps(runtime_root, project, [changed if row["id"] == document_id else row for row in rows])
        row = await db_documents.update_document(conn, project_id, document_id, **fields)
        if row is None:  # pragma: no cover - the row is locked through its project
            raise KnowledgeNotFound(f"project {project_id!r} has no document {document_id!r}")
    return await _with_document(project_id, str(row["id"]), runtime_root)


async def delete_document(project_id: str, document_id: str, runtime_root: Path) -> dict[str, Any]:
    """Remove a document; returns the view. Its materialized copy stays until a server start reclaims it."""
    from ..db import project_documents as db_documents

    async with db_documents.locked_project(project_id) as (conn, project):
        if project is None:
            raise KnowledgeNotFound(f"project {project_id!r} not found")
        if not await db_documents.delete_document(project_id, document_id, pool=conn):
            raise KnowledgeNotFound(f"project {project_id!r} has no document {document_id!r}")
    return {"deleted": document_id, **await knowledge_view(project_id, runtime_root)}
