"""More ways into the capability catalog: upload, git and Settings (RFC decision 9, M8c).

Each acquires items into the catalog (``library_items``, ``library_files``)
outside any project, as the machine import does (``library``). None of them
installs anything; an item loads only where it is installed (``catalog``).

- **Upload** (``upload``, ``POST /api/catalog/upload``): one ``SKILL.md``
  (frontmatter ``name`` and ``description``, the body below), or a zip of a
  skill directory (``SKILL.md`` and its files) or of a plugin directory
  (``.claude-plugin/plugin.json`` with its ``skills/``, ``agents/``,
  ``commands/`` and so on), as base64 in JSON. Origin ``upload``.
- **Git** (``git_preview`` and ``git_import``, ``POST /api/catalog/git/*``):
  a shallow clone of an ``https://`` repository (optionally a branch or tag,
  and a subpath in it) is scanned for plugins and skills, the items found are
  previewed, and the chosen ones are imported. Origin ``git``, source
  ``<url>@<commit>``. Importing from the same URL again updates those items in
  place, keeping their ids and installs.
- **Settings** (``create_skill``, ``POST /api/catalog/skills``): a skill
  written in the UI. Origin ``ui``.

**Edits** (``update_item``, ``PUT /api/catalog/<id>``): a skill written in
Settings, uploaded or taken from git can be renamed and have its description
and the body of its ``SKILL.md`` changed; an uploaded or git skill is then
marked ``modified`` (re-importing it from its repository replaces the edit).
Items imported from this machine change only by re-importing it (an edit
would be overwritten by the next import), a project's own skills in their
project, and plugins by uploading or importing them again. ``delete_item``
removes an uploaded, git or Settings item together with its installs.

**Names**: a name is a lowercase slug, unique per kind among the items
outside projects. A clash is refused (409) and the request can give another
name; renaming rewrites the ``name`` in the item's ``SKILL.md`` or
``plugin.json`` so Claude Code calls it what the catalog does.

**Safety.** Files follow the machine import's rules (``library``): text only
(a binary file is left out and reported), at most ``FILE_MAX_BYTES`` each,
``ITEM_MAX_FILES`` and ``ITEM_MAX_BYTES`` per item, ``LIBRARY_MAX_BYTES`` for
the whole catalog. A zip is refused whole for an absolute path, a ``..``
part, a symbolic link, an encrypted entry, a file over the cap, or too many
or too large files in all; an upload is at most ``UPLOAD_MAX_BYTES``, which
as base64 JSON fits the server's request body cap. A repository URL is
``https://`` without credentials; the clone is ``--depth 1`` of one branch,
without tags, submodules, hooks, templates, fsmonitor, prompts, LFS
downloads or the operator's git configuration (its credential helpers, URL
rewrites and filters), stopped after ``GIT_CLONE_TIMEOUT_SECONDS`` or when
it grows over ``GIT_CLONE_MAX_BYTES``; nothing in it is run, only read, and
its temporary directory is removed afterwards.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import io
import json
import logging
import os
import re
import shutil
import signal
import stat
import tempfile
import urllib.parse
import zipfile
import zlib
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .catalog import CatalogConflict, CatalogError, CatalogNotFound, Origin, item_view
from .library import (
    _LEFT_OUT,
    FILE_MAX_BYTES,
    ITEM_MAX_BYTES,
    ITEM_MAX_FILES,
    LIBRARY_MAX_BYTES,
    PROJECT_SKILLS_PLUGIN,
    SKILLS_PLUGIN,
    ItemKind,
    Skipped,
    SnapshotFile,
    _collect,
    _ItemTooLarge,
    _sha256,
    item_sha256,
)

logger = logging.getLogger(__name__)

# The largest upload: its base64 in a JSON body (4 bytes for 3, 8 MB) stays under the server's 8 MiB request cap.
UPLOAD_MAX_BYTES = 6_000_000
# A clone is stopped when it takes longer or its directory grows larger (anthropics/skills is about 17 MiB).
GIT_CLONE_TIMEOUT_SECONDS = 120
GIT_CLONE_MAX_BYTES = 64 * 1024 * 1024
# How deep a repository is searched for plugins and skills, and how many it may hold.
GIT_SCAN_MAX_DEPTH = 4
GIT_SCAN_MAX_ITEMS = 300
# Tests clone from a file:// URL; the server never does.
ALLOW_FILE_URLS = False

NAME_MAX_CHARS = 64
DESCRIPTION_MAX_CHARS = 1024
SKILL_FILE = "SKILL.md"
PLUGIN_MANIFEST = ".claude-plugin/plugin.json"

_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
# A branch or tag: no leading '-', no whitespace, no '..'.
_REF = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._/-]{0,199}")
_KEY = re.compile(r"([A-Za-z_][\w-]*):(.*)$")
# Directories a repository scan never enters (dot-directories other than .claude are skipped too).
_SCAN_SKIP = frozenset({"node_modules", "__pycache__", ".venv", "venv"})
_UPLOAD_FIELDS = frozenset({"filename", "content_base64", "name"})
_GIT_FIELDS = frozenset({"url", "ref", "subpath"})
_IMPORT_FIELDS = _GIT_FIELDS | {"commit", "items"}
_SKILL_FIELDS = frozenset({"name", "description", "content"})
# At most two clones at a time.
_clones = asyncio.Semaphore(2)


# --- SKILL.md frontmatter ---


def _frontmatter(text: str) -> tuple[list[str], str] | None:
    """The lines between a ``SKILL.md``'s ``---`` fences and the text after them; None without frontmatter."""
    lines = text.removeprefix("\ufeff").split("\n")
    if lines[0].rstrip("\r").strip() != "---":
        return None
    for end in range(1, len(lines)):
        if lines[end].rstrip("\r").strip() == "---":
            return [line.rstrip("\r") for line in lines[1:end]], "\n".join(lines[end + 1:])
    return None


def _entries(lines: Sequence[str]) -> list[tuple[str | None, list[str]]]:
    """Top-level frontmatter entries, each a key and its lines: indented or blank lines go with the key above."""
    entries: list[tuple[str | None, list[str]]] = []
    for line in lines:
        match = _KEY.match(line)
        if match:
            entries.append((match.group(1), [line]))
        elif entries:
            entries[-1][1].append(line)
        else:
            entries.append((None, [line]))
    return entries


def _scalar(raw: str) -> str:
    value = raw.strip()
    if len(value) > 1 and value[0] == value[-1] == '"':
        try:
            return str(json.loads(value))
        except ValueError:
            return value[1:-1]
    if len(value) > 1 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    return value


def _value(lines: Sequence[str]) -> str:
    """An entry's value on one line: a plain or quoted scalar, or a block (``>`` or ``|``) joined with spaces."""
    match = _KEY.match(lines[0])
    assert match is not None
    first = match.group(2).strip()
    rest = [line.strip() for line in lines[1:] if line.strip()]
    if re.fullmatch(r"[>|][+-]?", first):
        return " ".join(rest)
    return " ".join([_scalar(first), *rest]) if rest else _scalar(first)


def split_skill_md(text: str) -> tuple[dict[str, str], str]:
    """A ``SKILL.md``'s top-level frontmatter fields (each read as one line) and the body below it.

    Without frontmatter, no fields and the whole text.
    """
    parsed = _frontmatter(text)
    if parsed is None:
        return {}, text
    lines, rest = parsed
    fields = {key: _value(entry) for key, entry in _entries(lines) if key is not None}
    return fields, re.sub(r"^(?:[ \t]*\r?\n)+", "", rest)


def with_fields(text: str, *, name: str | None = None, description: str | None = None,
                body: str | None = None) -> str:
    """``text`` (a ``SKILL.md``) with its frontmatter ``name``, ``description`` or body replaced; the other
    frontmatter lines stay as they are."""
    parsed = _frontmatter(text)
    lines, rest = parsed if parsed is not None else ([], "\n" + text)
    entries = _entries(lines)
    for key, line in (("description", None if description is None else
                       f"description: {json.dumps(description, ensure_ascii=False)}"),
                      ("name", None if name is None else f"name: {name}")):
        if line is None:
            continue
        at = next((i for i, (other, _) in enumerate(entries) if other == key), None)
        if at is None:
            entries.insert(0, (key, [line]))
        else:
            entries[at] = (key, [line])
    if body is not None:
        rest = "\n" + (body if body.endswith("\n") else body + "\n")
    return "---\n" + "".join(line + "\n" for _, entry in entries for line in entry) + "---\n" + rest


def _one_line(raw: Any) -> str:
    text = " ".join(raw.split()) if isinstance(raw, str) else ""
    return text[:DESCRIPTION_MAX_CHARS]


# --- What an acquired item is ---


@dataclass(frozen=True)
class NewItem:
    kind: ItemKind
    name: str
    description: str
    version: str
    files: tuple[SnapshotFile, ...]

    @property
    def sha256(self) -> str:
        return item_sha256(self.files)

    @property
    def size_bytes(self) -> int:
        return sum(len(f.content.encode("utf-8")) for f in self.files)

    def row(self, source: str) -> dict[str, Any]:
        """The item as ``db.library.insert_item`` stores it."""
        return {
            "kind": self.kind.value, "name": self.name, "source": source, "version": self.version,
            "description": self.description, "sha256": self.sha256, "size_bytes": self.size_bytes,
            "file_count": len(self.files),
            "files": [{"path": f.path, "content": f.content, "sha256": f.sha256, "executable": f.executable}
                      for f in self.files],
        }


@dataclass(frozen=True)
class Declared:
    """What an item says about itself: the name in its ``SKILL.md`` or ``plugin.json`` (None when it gives
    none), its description and its version."""

    name: str | None
    description: str
    version: str


def item_name(raw: Any, kind: ItemKind) -> str:
    """An item's name: a lowercase slug of at most ``NAME_MAX_CHARS``; a plugin's is not one LoopX generates."""
    if not isinstance(raw, str) or not raw.strip():
        raise CatalogError(f"a {kind.value} needs a name")
    name = raw.strip()
    if len(name) > NAME_MAX_CHARS or not _SLUG.fullmatch(name):
        raise CatalogError(
            f"{kind.value} name {name[:40]!r} must be lowercase letters, digits and hyphens, at most "
            f"{NAME_MAX_CHARS} characters, such as deploy-check; give it another name"
        )
    if kind is ItemKind.PLUGIN and name in (SKILLS_PLUGIN, PROJECT_SKILLS_PLUGIN):
        raise CatalogError(f"{name!r} is a plugin LoopX generates; give this plugin another name")
    return name


def _text_file(path: str, content: str, executable: bool = False) -> SnapshotFile:
    return SnapshotFile(path, content, _sha256(content.encode("utf-8")), executable)


def inspect(kind: ItemKind, files: Sequence[SnapshotFile], where: str) -> Declared:
    """Read an item's ``SKILL.md`` or ``plugin.json``; raises ``CatalogError`` when it is missing or invalid."""
    by_path = {f.path: f for f in files}
    if kind is ItemKind.SKILL:
        skill = by_path.get(SKILL_FILE)
        if skill is None:
            raise CatalogError(f"{where} has no SKILL.md (a text file at its top)")
        fields, body = split_skill_md(skill.content)
        if not fields:
            raise CatalogError(f"the SKILL.md of {where} has no frontmatter: it starts with a --- line, then "
                               "name: and description:, then ---")
        description = _one_line(fields.get("description"))
        if not description:
            raise CatalogError(f"the SKILL.md of {where} has no description in its frontmatter")
        if not body.strip():
            raise CatalogError(f"the SKILL.md of {where} has no instructions below its frontmatter")
        return Declared(fields.get("name") or None, description, "")
    manifest = by_path.get(PLUGIN_MANIFEST)
    if manifest is None:
        raise CatalogError(f"{where} has no {PLUGIN_MANIFEST}")
    try:
        data = json.loads(manifest.content)
    except ValueError:
        raise CatalogError(f"the {PLUGIN_MANIFEST} of {where} is not valid JSON") from None
    if not isinstance(data, dict) or not isinstance(data.get("name"), str) or not data["name"].strip():
        raise CatalogError(f"the {PLUGIN_MANIFEST} of {where} is not a JSON object with a name")
    version = data.get("version")
    return Declared(data["name"].strip(), _one_line(data.get("description")),
                    version if isinstance(version, str) else "")


def finish(kind: ItemKind, files: Sequence[SnapshotFile], declared: Declared, name: str) -> NewItem:
    """The item named ``name``; its ``SKILL.md`` or ``plugin.json`` is rewritten to say so when it says otherwise."""
    if declared.name != name:
        rewritten = []
        for f in files:
            if kind is ItemKind.SKILL and f.path == SKILL_FILE:
                f = _text_file(f.path, with_fields(f.content, name=name), f.executable)
            elif kind is ItemKind.PLUGIN and f.path == PLUGIN_MANIFEST:
                manifest = json.loads(f.content)
                manifest["name"] = name
                f = _text_file(f.path, json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", f.executable)
            rewritten.append(f)
        files = rewritten
    return NewItem(kind, name, declared.description, declared.version, tuple(files))


# --- Writing to the catalog ---


def _url_of(source: str) -> str:
    """The repository URL of a git item's source, ``<url>@<commit>``."""
    return source.rpartition("@")[0]


def _held_by(row: Mapping[str, Any]) -> str:
    origin = row["origin"]
    if origin == Origin.GIT.value:
        return f"imported from {_url_of(row['source'])}"
    return {Origin.MACHINE.value: "imported from this machine", Origin.UPLOAD.value: "uploaded",
            Origin.UI.value: "written in Settings"}.get(origin, origin)


def _taken(kind: str, name: str, row: Mapping[str, Any]) -> str:
    return f"the catalog already has a {kind} named {name!r} ({_held_by(row)}); give this one another name"


async def _named(conn: Any) -> dict[tuple[str, str], dict[str, Any]]:
    """The items outside projects by kind and name."""
    from ..db import library as db_library

    return {(row["kind"], row["name"]): row for row in await db_library.list_items(pool=conn)
            if row["owner_project_id"] is None}


async def _store(items: Sequence[NewItem], origin: Origin, source: str, *, repository: str | None = None,
                 ) -> dict[str, Any]:
    """Add ``items`` in one transaction; each is new, or (``repository`` given) a git item from that repository.

    Raises ``CatalogConflict`` naming every clash with another item and
    ``CatalogError`` when the catalog would grow over ``LIBRARY_MAX_BYTES``.
    Returns the items and which were ``added``, ``updated`` and ``unchanged``.
    """
    import asyncpg

    from ..db import library as db_library
    from ..db.connection import get_pool

    names = [(item.kind.value, item.name) for item in items]
    twice = sorted({f"{kind} {name!r}" for kind, name in names if names.count((kind, name)) > 1})
    if twice:
        raise CatalogConflict(f"two of the chosen items are both named {twice[0]}; give one another name")
    added: list[str] = []
    updated: list[str] = []
    unchanged: list[str] = []
    rows: list[dict[str, Any]] = []
    try:
        async with get_pool().acquire() as conn, conn.transaction():
            existing = await _named(conn)
            plan: list[tuple[NewItem, dict[str, Any] | None]] = []
            clashes: list[str] = []
            for item in items:
                current = existing.get((item.kind.value, item.name))
                if current is not None and not (repository is not None and current["origin"] == Origin.GIT.value
                                                and _url_of(current["source"]) == repository):
                    clashes.append(_taken(item.kind.value, item.name, current))
                plan.append((item, current))
            if clashes:
                raise CatalogConflict("; ".join(clashes))
            total = (await db_library.catalog_bytes(pool=conn)
                     - sum(int(current["size_bytes"]) for _, current in plan if current is not None)
                     + sum(item.size_bytes for item in items))
            if total > LIBRARY_MAX_BYTES:
                raise CatalogError(f"the catalog would hold {total} bytes, over its cap of {LIBRARY_MAX_BYTES}; "
                                   "nothing was added")
            for item, current in plan:
                label = f"{item.kind.value}:{item.name}"
                if current is None:
                    rows.append(await db_library.insert_item(conn, item.row(source), origin=origin.value))
                    added.append(label)
                    continue
                same = current["sha256"] == item.sha256 and not current["modified"]
                rows.append(await db_library.replace_item(conn, current["id"], item.row(source), modified=False))
                (unchanged if same else updated).append(label)
    except asyncpg.UniqueViolationError:
        raise CatalogConflict("another request took one of these names just now; try again") from None
    installs = await _installs([row["id"] for row in rows])
    return {
        "items": [item_view(row, installs.get(row["id"], ())) for row in rows],
        "added": added, "updated": updated, "unchanged": unchanged,
    }


async def _installs(item_ids: Sequence[str]) -> dict[str, list[dict[str, Any]]]:
    from ..db import library as db_library

    by_item: dict[str, list[dict[str, Any]]] = {}
    for install in await db_library.list_installs(item_ids=list(item_ids)):
        by_item.setdefault(install["item_id"], []).append(install)
    return by_item


def _fields(data: Any, allowed: frozenset[str]) -> Mapping[str, Any]:
    if not isinstance(data, Mapping):
        raise CatalogError("body must be a JSON object")
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise CatalogError(f"unknown field {unknown[0]!r}; the fields are {', '.join(sorted(allowed))}")
    return data


def _rename(raw: Any) -> str | None:
    """An optional new name: None when it is absent or empty."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    if not isinstance(raw, str):
        raise CatalogError("name must be a string")
    return raw.strip()


# --- Upload: a SKILL.md, or a zip of a skill or plugin directory ---


def _reject(text: str) -> CatalogError:
    return CatalogError(f"{text}; nothing was added")


def read_zip(data: bytes) -> dict[str, tuple[bytes, bool]]:
    """A zip's files by path, each its bytes and executable bit; the zip is refused whole when one entry is unsafe.

    Refused: an absolute path, a ``..`` or empty part, a symbolic link, an
    encrypted entry, a file over ``FILE_MAX_BYTES``, more than
    ``ITEM_MAX_FILES`` files or ``ITEM_MAX_BYTES`` in all. Directories,
    ``__MACOSX/`` and what the machine import leaves out (``.git``,
    ``.DS_Store``, ``__pycache__``) are dropped.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, ValueError):
        raise _reject("the upload is not a valid zip file") from None
    files: dict[str, tuple[bytes, bool]] = {}
    total = 0
    with archive:
        for info in archive.infolist():
            raw = info.filename
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise _reject(f"the zip holds a symbolic link, {raw!r}: links are not accepted")
            if raw.startswith(("/", "\\")) or "\\" in raw or re.match(r"[A-Za-z]:", raw):
                raise _reject(f"the zip holds an absolute or Windows path, {raw!r}")
            parts = raw.rstrip("/").split("/")
            if ".." in parts:
                raise _reject(f"the zip holds a path that leaves its directory, {raw!r}")
            if any(part in ("", ".") for part in parts):
                raise _reject(f"the zip holds an unusable path, {raw!r}")
            if info.is_dir() or parts[0] == "__MACOSX" or any(part in _LEFT_OUT for part in parts):
                continue
            path = "/".join(parts)
            if info.flag_bits & 0x1:
                raise _reject(f"{path} in the zip is encrypted")
            if info.file_size > FILE_MAX_BYTES:
                raise _reject(f"{path} has {info.file_size} bytes; a file has at most {FILE_MAX_BYTES}")
            total += info.file_size
            if total > ITEM_MAX_BYTES:
                raise _reject(f"the zip unpacks to more than {ITEM_MAX_BYTES} bytes, an item's cap")
            if len(files) >= ITEM_MAX_FILES:
                raise _reject(f"the zip holds more than {ITEM_MAX_FILES} files, an item's cap")
            if path in files:
                raise _reject(f"the zip holds {path} twice")
            try:
                content = archive.read(info)  # never more than its declared size, checked against its CRC
            except (zipfile.BadZipFile, zlib.error, NotImplementedError, EOFError, RuntimeError) as exc:
                raise _reject(f"{path} cannot be read from the zip: {exc}") from None
            files[path] = (content, bool(mode & 0o111))
    return files


def _text_files(raw: Mapping[str, tuple[bytes, bool]], label: str) -> tuple[list[SnapshotFile], list[Skipped]]:
    """The UTF-8 text files; a binary one is left out and reported, as the machine import does."""
    files: list[SnapshotFile] = []
    skipped: list[Skipped] = []
    for path in sorted(raw):
        data, executable = raw[path]
        try:
            if b"\x00" in data:
                raise UnicodeDecodeError("utf-8", data, 0, 1, "NUL byte")
            content = data.decode("utf-8")
        except UnicodeDecodeError:
            skipped.append(Skipped(label, path, "binary", len(data)))
            continue
        files.append(SnapshotFile(path, content, _sha256(data), executable))
    return files, skipped


def _from_zip(data: bytes, filename: str, rename: str | None) -> tuple[NewItem, list[Skipped]]:
    raw = read_zip(data)
    top: str | None = None
    if not {SKILL_FILE, PLUGIN_MANIFEST} & raw.keys():
        tops = {path.split("/", 1)[0] for path in raw}
        if len(tops) == 1 and all("/" in path for path in raw):
            top = tops.pop()
            raw = {path[len(top) + 1:]: value for path, value in raw.items()}
    if PLUGIN_MANIFEST in raw:
        kind = ItemKind.PLUGIN
    elif SKILL_FILE in raw:
        kind = ItemKind.SKILL
    else:
        raise _reject(f"the zip has neither {SKILL_FILE} (a skill) nor {PLUGIN_MANIFEST} (a plugin) at its top or "
                      "in its one top-level directory")
    files, skipped = _text_files(raw, filename)
    declared = inspect(kind, files, filename)
    fallback = top or PurePosixPath(filename).stem
    return finish(kind, files, declared, item_name(rename or declared.name or fallback, kind)), skipped


def _from_skill_md(data: bytes, filename: str, rename: str | None) -> NewItem:
    if len(data) > FILE_MAX_BYTES:
        raise _reject(f"{filename} has {len(data)} bytes; a file has at most {FILE_MAX_BYTES}")
    try:
        if b"\x00" in data:
            raise UnicodeDecodeError("utf-8", data, 0, 1, "NUL byte")
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise _reject(f"{filename} is not UTF-8 text") from None
    files = [_text_file(SKILL_FILE, text)]
    declared = inspect(ItemKind.SKILL, files, filename)
    if not (rename or declared.name):
        raise _reject(f"{filename} has no name in its frontmatter; give one")
    return finish(ItemKind.SKILL, files, declared, item_name(rename or declared.name, ItemKind.SKILL))


async def upload(data: Any) -> dict[str, Any]:
    """Add an uploaded ``SKILL.md`` or zip: ``{filename, content_base64, name?}``; ``name`` renames it.

    Returns the item and the files left out (``skipped``).
    """
    fields = _fields(data, _UPLOAD_FIELDS)
    filename = fields.get("filename")
    if not isinstance(filename, str) or not filename.strip():
        raise CatalogError("an upload needs its filename: a SKILL.md or a .zip")
    filename = PurePosixPath(filename.strip().replace("\\", "/")).name[:200]
    content = fields.get("content_base64")
    if not isinstance(content, str) or not content:
        raise CatalogError("an upload needs content_base64, the file's bytes in base64")
    try:
        raw = base64.b64decode(content, validate=True)
    except (binascii.Error, ValueError):
        raise CatalogError(f"content_base64 of {filename} is not valid base64") from None
    if len(raw) > UPLOAD_MAX_BYTES:
        raise _reject(f"{filename} has {len(raw)} bytes; an upload has at most {UPLOAD_MAX_BYTES} bytes")
    rename = _rename(fields.get("name"))
    lower = filename.lower()
    if lower.endswith(".zip"):
        item, skipped = await asyncio.to_thread(_from_zip, raw, filename, rename)
    elif lower.endswith(".md"):
        item, skipped = _from_skill_md(raw, filename, rename), []
    else:
        raise CatalogError(f"upload a SKILL.md or a .zip of a skill or plugin directory, not {filename}")
    stored = await _store([item], Origin.UPLOAD, filename)
    return {"item": stored["items"][0], "skipped": [s.view() for s in skipped]}


# --- Git: preview a repository's plugins and skills, then import the chosen ones ---


@dataclass(frozen=True)
class GitSource:
    url: str
    ref: str | None
    subpath: str  # '' for the repository's top

    @property
    def key(self) -> str:
        """The URL items are matched on when importing again: no trailing ``/`` or ``.git``, host in lowercase."""
        parts = urllib.parse.urlsplit(self.url)
        path = parts.path.rstrip("/").removesuffix(".git")
        return f"{parts.scheme}://{parts.netloc.lower()}{path}"


def git_source(data: Mapping[str, Any]) -> GitSource:
    url = data.get("url")
    if not isinstance(url, str) or not url.strip():
        raise CatalogError("give the repository's https:// URL")
    url = url.strip()
    if len(url) > 2048 or re.search(r"[\s\x00-\x1f\x7f]", url):
        raise CatalogError("the repository URL is too long or holds whitespace")
    parts = urllib.parse.urlsplit(url)
    if not (parts.scheme == "https" or (parts.scheme == "file" and ALLOW_FILE_URLS)):
        raise CatalogError("the repository URL must start with https:// (ssh, http, git:// and local paths are "
                           "not accepted)")
    if parts.scheme == "https" and not parts.hostname:
        raise CatalogError("the repository URL has no host")
    if "@" in parts.netloc:
        raise CatalogError("put no credentials in the repository URL: only public repositories are imported")
    if parts.query or parts.fragment:
        raise CatalogError("the repository URL cannot have a query or a fragment")
    ref = data.get("ref")
    if isinstance(ref, str):
        ref = ref.strip() or None
    if ref is not None and (not isinstance(ref, str) or not _REF.fullmatch(ref) or ".." in ref
                            or ref.endswith(("/", ".lock"))):
        raise CatalogError("ref is a branch or tag name, such as main or v1.2.0")
    subpath = data.get("subpath")
    if subpath is None or (isinstance(subpath, str) and not subpath.strip().strip("/")):
        subpath = ""
    elif not isinstance(subpath, str):
        raise CatalogError("subpath is a directory in the repository, such as skills/pdf")
    else:
        subpath = subpath.strip().strip("/")
        if subpath.startswith("~") or "\\" in subpath or any(
                part in ("", ".", "..") for part in subpath.split("/")):
            raise CatalogError("subpath is a relative directory in the repository, without . or .. parts")
    return GitSource(url, ref, subpath)


def _tree_bytes(path: Path) -> int:
    total = 0
    for directory, _, names in os.walk(path):
        for name in names:
            try:
                total += os.lstat(os.path.join(directory, name)).st_size
            except OSError:
                pass
    return total


def _remove(path: Path) -> None:
    for directory, _, _ in os.walk(path):
        try:
            os.chmod(directory, 0o755)
        except OSError:
            pass
    shutil.rmtree(path, ignore_errors=True)


def _git_args(git: str, scheme: str) -> list[str]:
    """``git`` with configuration that runs nothing and asks nothing: no hooks, fsmonitor or credential helper,
    and only the URL's own protocol."""
    return [git, "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "credential.helper=",
            "-c", "submodule.recurse=false", "-c", "protocol.allow=never", "-c", f"protocol.{scheme}.allow=always"]


def _git_env() -> dict[str, str]:
    """No prompts, and none of the operator's git configuration: its credential helpers (a private repository
    is not reached with the operator's token), URL rewrites, hooks and filters."""
    return {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
            "GIT_LFS_SKIP_SMUDGE": "1", "GCM_INTERACTIVE": "never"}


def _kill(process: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


async def _clone(git: str, source: GitSource, destination: Path) -> None:
    """``git clone --depth 1`` into ``destination``, stopped at ``GIT_CLONE_TIMEOUT_SECONDS`` or
    ``GIT_CLONE_MAX_BYTES``."""
    scheme = urllib.parse.urlsplit(source.url).scheme
    args = [*_git_args(git, scheme), "clone", "--depth", "1", "--single-branch", "--no-tags",
            "--no-recurse-submodules", "--template=", "--quiet"]
    if source.ref:
        args += ["--branch", source.ref]
    args += ["--", source.url, str(destination)]
    process = await asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE, env=_git_env(), start_new_session=True,
    )
    assert process.stderr is not None
    stream = process.stderr

    async def _errors() -> bytes:
        kept = b""
        while chunk := await stream.read(4096):
            kept = (kept + chunk)[-4096:]
        return kept

    errors = asyncio.create_task(_errors())
    loop = asyncio.get_running_loop()
    deadline = loop.time() + GIT_CLONE_TIMEOUT_SECONDS
    try:
        while True:
            try:
                await asyncio.wait_for(process.wait(), timeout=0.25)
                done = True
            except TimeoutError:
                done = False
            size = await asyncio.to_thread(_tree_bytes, destination) if destination.exists() else 0
            if size > GIT_CLONE_MAX_BYTES:
                raise CatalogError(f"the repository is over {GIT_CLONE_MAX_BYTES // (1024 * 1024)} MiB "
                                   f"({GIT_CLONE_MAX_BYTES} bytes) when cloned; the clone was stopped")
            if done:
                break
            if loop.time() > deadline:
                raise CatalogError(f"cloning took more than {GIT_CLONE_TIMEOUT_SECONDS} seconds and was stopped")
    finally:
        if process.returncode is None:
            _kill(process)
            await process.wait()
        stderr = await errors
    if process.returncode != 0:
        lines = [line for line in stderr.decode("utf-8", "replace").splitlines() if line.strip()]
        reason = lines[-1].strip()[:300] if lines else f"exit status {process.returncode}"
        raise CatalogError(f"git clone of {source.url} failed: {reason}")


async def _head(git: str, source: GitSource, repository: Path) -> str:
    scheme = urllib.parse.urlsplit(source.url).scheme
    process = await asyncio.create_subprocess_exec(
        *_git_args(git, scheme), "-C", str(repository), "rev-parse", "HEAD",
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        env=_git_env(), start_new_session=True,
    )
    try:
        out, _ = await asyncio.wait_for(process.communicate(), timeout=30)
    except TimeoutError:
        _kill(process)
        await process.wait()
        raise CatalogError("reading the cloned commit took too long") from None
    commit = out.decode("ascii", "replace").strip()
    if process.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40,64}", commit):
        raise CatalogError("cannot read the cloned commit")
    return commit


@asynccontextmanager
async def checkout(source: GitSource) -> AsyncIterator[tuple[Path, str]]:
    """A shallow clone of ``source`` in a temporary directory, its subpath and commit; removed on exit."""
    git = shutil.which("git")
    if git is None:
        raise CatalogError("git is not installed where LoopX runs")
    async with _clones:
        scratch = Path(tempfile.mkdtemp(prefix="loopx-git-"))
        try:
            repository = scratch / "repo"
            await _clone(git, source, repository)
            commit = await _head(git, source, repository)
            top = repository.joinpath(*source.subpath.split("/")) if source.subpath else repository
            if source.subpath and (top.is_symlink() or not top.is_dir()
                                   or not top.resolve().is_relative_to(repository.resolve())):
                raise CatalogError(f"the repository has no directory {source.subpath}")
            yield top, commit
        finally:
            await asyncio.to_thread(_remove, scratch)


@dataclass(frozen=True)
class Found:
    """A plugin or skill in a repository: its directory relative to the scanned top (``.`` for the top)."""

    path: str
    kind: ItemKind
    files: tuple[SnapshotFile, ...]
    declared: Declared

    @property
    def fallback(self) -> str:
        return PurePosixPath(self.path).name if self.path != "." else ""


def _kind_of(directory: Path) -> ItemKind | None:
    if (directory / ".claude-plugin" / "plugin.json").is_file():
        return ItemKind.PLUGIN
    if (directory / SKILL_FILE).is_file():
        return ItemKind.SKILL
    return None


def scan_repository(top: Path) -> tuple[list[Found], list[Skipped]]:
    """The plugins and skills under ``top``, read with the machine import's file rules; nothing is run.

    ``top`` is one item when it holds a ``plugin.json`` or ``SKILL.md``;
    otherwise each directory that does, up to ``GIT_SCAN_MAX_DEPTH`` levels
    down (an item's own subdirectories are part of it). An item with an
    invalid ``SKILL.md`` or ``plugin.json``, or over a cap, is reported in
    ``skipped`` with the files left out.
    """
    places: list[tuple[str, ItemKind, Path]] = []
    kind = _kind_of(top)
    if kind is not None:
        places.append((".", kind, top))
    else:
        def walk(directory: Path, depth: int) -> None:
            for child in sorted(directory.iterdir(), key=lambda p: p.name):
                if child.is_symlink() or not child.is_dir() or child.name in _SCAN_SKIP or child.name in _LEFT_OUT:
                    continue
                if child.name.startswith(".") and child.name != ".claude":
                    continue
                found = _kind_of(child)
                if found is not None:
                    places.append((child.relative_to(top).as_posix(), found, child))
                    if len(places) > GIT_SCAN_MAX_ITEMS:
                        raise CatalogError(f"the repository holds more than {GIT_SCAN_MAX_ITEMS} plugins and "
                                           "skills; give a subpath")
                elif depth < GIT_SCAN_MAX_DEPTH:
                    walk(child, depth + 1)

        walk(top, 1)
    found: list[Found] = []
    skipped: list[Skipped] = []
    for path, kind, directory in places:
        try:
            files = _collect(directory, path, skipped)
        except _ItemTooLarge as exc:
            skipped.append(Skipped(path, "", f"too large: {exc}"))
            continue
        except OSError as exc:
            skipped.append(Skipped(path, "", f"cannot be read: {exc}"))
            continue
        try:
            declared = inspect(kind, files, path)
        except CatalogError as exc:
            skipped.append(Skipped(path, "", str(exc)))
            continue
        found.append(Found(path, kind, files, declared))
    return found, skipped


async def _scan(source: GitSource) -> tuple[str, list[Found], list[Skipped]]:
    async with checkout(source) as (top, commit):
        found, skipped = await asyncio.to_thread(scan_repository, top)
    return commit, found, skipped


def _source_view(source: GitSource, commit: str) -> dict[str, Any]:
    return {"url": source.url, "ref": source.ref, "subpath": source.subpath, "commit": commit}


async def git_preview(data: Any) -> dict[str, Any]:
    """What a repository holds: each plugin or skill found, and whether importing it adds, updates or clashes.

    ``{url, ref?, subpath?}``. Each item has its ``path`` (what an import
    names), ``kind``, ``name``, ``description``, size and ``status``: ``new``,
    ``update`` (from this repository before; ``modified`` when edited since),
    ``unchanged``, or ``conflict`` with the ``reason`` (its name is taken or
    unusable: import it under another name).
    """
    source = git_source(_fields(data, _GIT_FIELDS))
    commit, found, skipped = await _scan(source)
    from ..db.connection import get_pool

    existing = await _named(get_pool())
    items = []
    for item in found:
        entry: dict[str, Any] = {
            "path": item.path, "kind": item.kind.value, "name": item.declared.name or item.fallback,
            "description": item.declared.description, "version": item.declared.version,
            "file_count": len(item.files), "size_bytes": sum(len(f.content.encode("utf-8")) for f in item.files),
        }
        try:
            name = item_name(item.declared.name or item.fallback, item.kind)
        except CatalogError as exc:
            items.append({**entry, "status": "conflict", "reason": str(exc)})
            continue
        current = existing.get((item.kind.value, name))
        if current is None:
            items.append({**entry, "status": "new"})
        elif current["origin"] == Origin.GIT.value and _url_of(current["source"]) == source.key:
            same = current["sha256"] == finish(item.kind, item.files, item.declared, name).sha256
            items.append({**entry, "status": "unchanged" if same and not current["modified"] else "update",
                          "item_id": current["id"], "modified": bool(current["modified"])})
        else:
            items.append({**entry, "status": "conflict", "reason": _taken(item.kind.value, name, current),
                          "item_id": current["id"]})
    return {**_source_view(source, commit), "items": items, "skipped": [s.view() for s in skipped]}


def _selection(raw: Any) -> list[tuple[str, str | None]]:
    if not isinstance(raw, list) or not raw:
        raise CatalogError("items is a non-empty list of {path, name?}: the previewed items to import")
    chosen: list[tuple[str, str | None]] = []
    for entry in raw:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("path"), str) or set(entry) - {"path", "name"}:
            raise CatalogError("each of items is {path, name?}: a previewed item's path and an optional new name")
        chosen.append((entry["path"], _rename(entry.get("name"))))
    paths = [path for path, _ in chosen]
    if len(set(paths)) != len(paths):
        raise CatalogError("items names one path twice")
    return chosen


async def git_import(data: Any) -> dict[str, Any]:
    """Import the chosen items of a repository: ``{url, ref?, subpath?, commit?, items: [{path, name?}]}``.

    The repository is cloned again; when ``commit`` (the preview's) is given
    and the ref has moved, nothing is imported (409). A new item is added; one
    imported from this URL before is updated in place, keeping its installs;
    a name taken by any other item is refused (409) and can be renamed.
    """
    fields = _fields(data, _IMPORT_FIELDS)
    source = git_source(fields)
    chosen = _selection(fields.get("items"))
    expected = fields.get("commit")
    if expected is not None and not (isinstance(expected, str) and re.fullmatch(r"[0-9a-f]{40,64}", expected)):
        raise CatalogError("commit is the previewed commit's full hash")
    commit, found, skipped = await _scan(source)
    if expected is not None and commit != expected:
        raise CatalogConflict(f"{source.ref or 'the default branch'} moved from {expected[:12]} to {commit[:12]} "
                              "since the preview; preview it again")
    by_path = {item.path: item for item in found}
    items = []
    for path, rename in chosen:
        item = by_path.get(path)
        if item is None:
            reason = next((s.reason for s in skipped if s.item == path and not s.path), None)
            raise CatalogError(f"{path}: {reason}" if reason else f"the repository has no plugin or skill at {path}")
        items.append(finish(item.kind, item.files, item.declared,
                            item_name(rename or item.declared.name or item.fallback, item.kind)))
    stored = await _store(items, Origin.GIT, f"{source.key}@{commit}", repository=source.key)
    chosen_paths = {path for path, _ in chosen}
    logger.info("git import from %s@%s: %d added, %d updated, %d unchanged", source.key, commit[:12],
                len(stored["added"]), len(stored["updated"]), len(stored["unchanged"]))
    return {**_source_view(source, commit), **stored,
            "skipped": [s.view() for s in skipped if s.item in chosen_paths]}


# --- Settings: write, change and delete skills ---


def _body(raw: Any, max_bytes: int) -> str:
    """A ``SKILL.md`` body: text without frontmatter (the fields make it) of at most ``max_bytes``."""
    if not isinstance(raw, str) or not raw.strip():
        raise CatalogError("a skill needs its content: the instructions below its frontmatter")
    if "\x00" in raw:
        raise CatalogError("a skill's content cannot hold NUL characters")
    if raw.lstrip("\ufeff").splitlines()[0].strip() == "---":
        raise CatalogError("give the content without frontmatter: the name and description fields make it")
    size = len(raw.encode("utf-8"))
    if size > max_bytes:
        raise CatalogError(f"a skill's content has at most {max_bytes} bytes; this has {size}")
    return raw


async def create_skill(data: Any) -> dict[str, Any]:
    """Write a skill in Settings: ``{name, description, content}`` (the body of its ``SKILL.md``); origin ``ui``."""
    from .project_skills import CONTENT_MAX_BYTES, skill_description, skill_md, skill_name

    fields = _fields(data, _SKILL_FIELDS)
    name = skill_name(fields.get("name"))
    description = skill_description(fields.get("description"))
    file, _ = skill_md(name, description, _body(fields.get("content"), CONTENT_MAX_BYTES))
    item = NewItem(ItemKind.SKILL, name, description, "",
                   (SnapshotFile(file["path"], file["content"], file["sha256"], False),))
    stored = await _store([item], Origin.UI, "")
    return {"item": stored["items"][0]}


def _not_editable(row: Mapping[str, Any], what: str) -> CatalogError | None:
    origin = row["origin"]
    if origin == Origin.MACHINE.value:
        return CatalogError(f"items imported from this machine are not {what} here: they follow its Claude Code "
                            "configuration and change on the next re-import")
    if origin == Origin.PROJECT.value:
        return CatalogError(f"a project's own skill is {what} in its project's Skills tab")
    return None


async def update_item(item_id: str, data: Any) -> dict[str, Any]:
    """Change a skill written in Settings, uploaded or taken from git: ``{name?, description?, content?}``.

    ``content`` is the body below the frontmatter. A Settings skill's
    ``SKILL.md`` is written from the fields; an uploaded or git one keeps its
    other frontmatter lines and files, and is marked ``modified``. Returns the
    item.
    """
    import asyncpg

    from ..db import library as db_library
    from ..db.connection import get_pool
    from .project_skills import CONTENT_MAX_BYTES, skill_description, skill_file, skill_name

    fields = _fields(data, _SKILL_FIELDS)
    if not fields:
        raise CatalogError(f"give at least one of {', '.join(sorted(_SKILL_FIELDS))}")
    try:
        async with get_pool().acquire() as conn, conn.transaction():
            if not await conn.fetchval("SELECT id FROM library_items WHERE id = $1 FOR UPDATE", item_id):
                raise CatalogNotFound(f"no catalog item {item_id!r}")
            row = await db_library.get_item(item_id, pool=conn)
            assert row is not None
            if (refusal := _not_editable(row, "edited")) is not None:
                raise refusal
            if row["kind"] != ItemKind.SKILL.value:
                raise CatalogError("a plugin is not edited here: upload it or import it from git again")
            name = skill_name(fields["name"]) if "name" in fields else row["name"]
            description = skill_description(fields["description"]) if "description" in fields else row["description"]
            ui = row["origin"] == Origin.UI.value
            content = _body(fields["content"], CONTENT_MAX_BYTES if ui else FILE_MAX_BYTES) if "content" in fields else None
            files = [SnapshotFile(f["path"], f["content"], f["sha256"], f["executable"])
                     for f in await db_library.get_item_files(item_id, pool=conn)]
            current = next((f for f in files if f.path == SKILL_FILE), None)
            text = current.content if current is not None else ""
            if ui:
                text = skill_file(name, description, content if content is not None else split_skill_md(text)[1])
            else:
                text = with_fields(text, name=name if name != row["name"] else None,
                                   description=description if "description" in fields else None, body=content)
            files = [f for f in files if f.path != SKILL_FILE] + [_text_file(SKILL_FILE, text)]
            item = NewItem(ItemKind.SKILL, name, description, row["version"], tuple(files))
            if name != row["name"] and (other := (await _named(conn)).get((row["kind"], name))) is not None:
                raise CatalogConflict(_taken(row["kind"], name, other))
            changed = item.sha256 != row["sha256"]
            row = await db_library.replace_item(conn, item_id, item.row(row["source"]),
                                                modified=bool(row["modified"]) or (changed and not ui))
    except asyncpg.UniqueViolationError:
        raise CatalogConflict(f"another request took the name {fields.get('name')!r} just now") from None
    installs = await _installs([item_id])
    return {"item": item_view(row, installs.get(item_id, ()))}


async def delete_item(item_id: str) -> dict[str, Any]:
    """Remove an uploaded, git or Settings item from the catalog, with its installs."""
    from ..db import library as db_library

    row = await db_library.get_item(item_id)
    if row is None:
        raise CatalogNotFound(f"no catalog item {item_id!r}")
    if (refusal := _not_editable(row, "deleted")) is not None:
        raise refusal
    if not await db_library.delete_item(item_id):
        raise CatalogNotFound(f"no catalog item {item_id!r}")
    return {"deleted": item_id}
