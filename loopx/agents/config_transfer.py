"""Configuration export and import: one file that recreates an installation's agent configuration (M8e).

**The file** is one JSON document, ``format: loopx-config/v1``. Every catalog
file is UTF-8 text (acquiring an item refuses binary files), so JSON holds it
as it is, and one readable file can be checked before it is shared. Its
sections:

- ``settings``: the system and role layers of the agent settings, and each
  role's prompt (``null`` while it follows the recommended one), timeout,
  concurrency and enabled flag;
- ``channels``: each chat channel's configuration;
- ``installs``: the global installs, naming their item by kind and name;
- ``projects`` (``--include-projects``): each project's fields, execution
  settings, layers, knowledge, the items it wrote and its installs;
- ``catalog``: every item outside projects with its files, or an MCP
  server's configuration.

**Secrets** are an MCP server's environment and header values and a channel
config's keys other than its known public ones (``_CHANNEL_PUBLIC_KEYS``; a
token itself lives in the environment variable a ``*_env`` key names).
Unless the export includes them, each is ``PLACEHOLDER``. **Machine paths**
stay out: a project's directory is not exported, an item's source that is a
local path is ``null``, and a path document is a reference (relative to its
project's directory) flagged ``repoint``.

**Import** matches entries by natural key (an item by kind and name, a
project-written one within its project; a project by name; a layer by scope,
role and project; a role by name; a channel by type), and reports each as
added, changed (with the fields), unchanged or skipped (with the reason),
plus the secrets still to fill in. Applying writes the same plan in one
transaction: an entry with the same key is updated in place (an item keeps
its id and installs), nothing missing from the file is removed, and a
placeholder never replaces a stored secret (without one, the value is left
empty). Importing the same file again changes nothing.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections import Counter, defaultdict
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

FORMAT = "loopx-config/v1"
# What the file holds in place of a secret value it leaves out.
PLACEHOLDER = {"secret": "omitted"}

# The channel config keys that are not secrets: the sender allowlist, the names of the environment variables the
# channel reads its credentials from, and the description. Any other key is treated as a secret.
_CHANNEL_PUBLIC_KEYS = {
    "telegram": frozenset({"allowed_user_ids", "bot_token_env", "description"}),
    "lark": frozenset({"allowed_open_ids", "app_id_env", "app_secret_env", "description"}),
}
_ROLE_FIELDS = ("prompt", "timeout_seconds", "max_concurrency", "enabled")
_LAYER_FIELDS = ("harness_prompt", "model", "effort", "env", "run_budget_usd")
_ITEM_FIELDS = ("origin", "source", "version", "description", "modified")
_DOCUMENT_FIELDS = ("summary", "source_kind", "filename", "path", "sha256", "size_bytes")
_CHANNEL_ID = re.compile(r"[a-zA-Z0-9_-]{1,64}")
_ITEM_NAME_MAX = 255
_PATH_MAX = 1024
_PROMPT_MAX_CHARS = 100_000
_TIMEOUT_MAX_SECONDS = 365 * 86400
_CONCURRENCY_MAX = 64


class ConfigFileError(ValueError):
    """A file the import cannot use at all; nothing is written."""


class SchemaChangePending(RuntimeError):
    """The database's schema is older than this LoopX; ``loopx serve`` backs it up, then changes it."""


class Action(str, Enum):
    ADDED = "added"
    CHANGED = "changed"
    UNCHANGED = "unchanged"
    SKIPPED = "skipped"


SECTIONS = ("settings.layers", "settings.roles", "channels", "catalog", "installs", "projects", "project.layers",
            "project.items", "project.knowledge", "project.installs")


@dataclass(frozen=True)
class Change:
    section: str
    key: str
    action: Action
    fields: tuple[str, ...] = ()
    reason: str = ""


@dataclass
class Plan:
    """What an import does (or did): each entry's change, the secrets left to fill in, and notes."""

    changes: list[Change] = field(default_factory=list)
    need_secret: list[tuple[str, str, tuple[str, ...]]] = field(default_factory=list)  # section, key, names
    notes: list[str] = field(default_factory=list)
    applied: bool = False

    def counts(self) -> dict[str, dict[str, int]]:
        counts: dict[str, Counter[str]] = defaultdict(Counter)
        for change in self.changes:
            counts[change.section][change.action.value] += 1
        return {section: {a.value: counts[section][a.value] for a in Action} for section in SECTIONS
                if section in counts}

    @property
    def writes(self) -> int:
        return sum(1 for c in self.changes if c.action in (Action.ADDED, Action.CHANGED))

    def view(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "counts": self.counts(),
            "changes": [{"section": c.section, "key": c.key, "action": c.action.value,
                         **({"fields": list(c.fields)} if c.fields else {}),
                         **({"reason": c.reason} if c.reason else {})}
                        for c in self.changes if c.action is not Action.UNCHANGED],
            "need_secret": [{"section": s, "key": k, "names": list(n)} for s, k, n in self.need_secret],
            "notes": list(self.notes),
        }


class _Skip(Exception):
    """An entry the import leaves out, with the reason."""


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def is_placeholder(value: Any) -> bool:
    return isinstance(value, Mapping) and dict(value) == PLACEHOLDER


# --- Export: database rows as file entries ---


def _portable_source(source: str) -> str | None:
    """An item's source label; None when it has none or it is a path on this machine (an import keeps the
    stored one)."""
    if not source or source.startswith(("/", "file:")) or re.match(r"[A-Za-z]:[\\/]", source):
        return None
    return source


def layer_entry(row: Mapping[str, Any]) -> dict[str, Any]:
    entry = {"scope": row["scope"]}
    if row.get("role"):
        entry["role"] = row["role"]
    return {**entry, **{name: row.get(name) for name in _LAYER_FIELDS}, "env": list(row.get("env") or [])}


def role_entry(row: Mapping[str, Any]) -> dict[str, Any]:
    prompt = row["system_prompt"]
    edited = prompt not in ("", row["default_prompt"])
    return {"role": row["agent_id"], "prompt": prompt if edited else None,
            "timeout_seconds": row["timeout_seconds"], "max_concurrency": row["max_concurrency"],
            "enabled": row["enabled"]}


def _secret(value: Any, include_secrets: bool) -> Any:
    return value if include_secrets else dict(PLACEHOLDER)


def channel_entry(row: Mapping[str, Any], include_secrets: bool) -> dict[str, Any]:
    from .channels.allowlist import AllowlistError, normalized_config

    public = _CHANNEL_PUBLIC_KEYS.get(row["channel_type"], frozenset())
    config = dict(row["config"] or {})
    try:  # the allowlist as the API stores it (an older row may hold a comma-separated string)
        config = normalized_config(row["channel_type"], config)
    except AllowlistError:
        pass
    config = {key: value if key in public else _secret(value, include_secrets) for key, value in config.items()}
    return {"channel_type": row["channel_type"], "channel_id": row["channel_id"],
            "display_name": row["display_name"], "enabled": row["enabled"], "config": config}


def _server_entry(row: Mapping[str, Any], include_secrets: bool) -> dict[str, Any]:
    def values(raw: Any) -> dict[str, Any]:
        return {name: _secret(value, include_secrets) for name, value in sorted(dict(raw or {}).items())}

    return {"transport": row["transport"], "command": row["command"], "args": list(row["args"] or ()),
            "env": values(row["env"]), "url": row["url"], "headers": values(row["headers"])}


def item_entry(row: Mapping[str, Any], files: Sequence[Mapping[str, Any]] | None, include_secrets: bool,
               ) -> dict[str, Any]:
    entry = {"kind": row["kind"], "name": row["name"], "origin": row["origin"],
             "source": _portable_source(row["source"]), "version": row["version"],
             "description": row["description"], "modified": row["modified"]}
    if row["kind"] == "mcp":
        return {**entry, "server": _server_entry(row, include_secrets)}
    return {**entry, "sha256": row["sha256"],
            "files": [{"path": f["path"], "executable": f["executable"], "sha256": f["sha256"],
                       "content": f["content"]} for f in files or ()]}


def _item_ref(kind: str, name: str, owner: str | None) -> dict[str, Any]:
    return {"kind": kind, "name": name, **({"project": owner} if owner else {})}


def install_entry(row: Mapping[str, Any]) -> dict[str, Any]:
    return {"item": _item_ref(row["kind"], row["name"], row["owner_project_name"]), "roles": list(row["roles"]),
            "enabled": row["enabled"]}


def document_entry(row: Mapping[str, Any]) -> dict[str, Any]:
    entry = {"title": row["title"], "summary": row["summary"], "source_kind": row["source_kind"],
             "filename": row["filename"], "size_bytes": row["size_bytes"], "sha256": row["sha256"]}
    if row["source_kind"] == "path":
        # A file in the project's directory, read live: on another machine it is wherever that directory is.
        return {**entry, "path": row["path"], "repoint": True}
    return {**entry, "content": row["content"]}


@dataclass
class ExportSummary:
    counts: dict[str, int] = field(default_factory=dict)
    secrets: int = 0  # the secret values written, or left out as placeholders
    repoint: int = 0
    bytes: int = 0


def _secret_count(entry: Mapping[str, Any]) -> int:
    """How many secret values a channel or MCP entry holds, or stands in for."""
    if "server" in entry:
        return len(entry["server"]["env"]) + len(entry["server"]["headers"])
    public = _CHANNEL_PUBLIC_KEYS.get(entry["channel_type"], frozenset())
    return sum(1 for key in entry["config"] if key not in public)


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


async def export_chunks(
    conn: Any, *, include_projects: bool, include_secrets: bool, summary: ExportSummary | None = None,
) -> AsyncIterator[str]:
    """The file, piece by piece: each catalog item and each project is read and written on its own.

    ``conn`` should be in a read-only, repeatable-read transaction so every
    piece comes from one snapshot.
    """
    from .. import __version__
    from ..db import config_transfer as db
    from ..db.projects import EXECUTION_COLUMNS

    summary = summary if summary is not None else ExportSummary()
    layers = await db.read_layers(conn)
    items = await db.read_items(conn)
    installs = await db.read_installs(conn)
    channels = [channel_entry(row, include_secrets) for row in await db.read_channels(conn)]
    head = {
        "format": FORMAT,
        "loopx_version": __version__,
        "exported_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "secrets": "included" if include_secrets else "omitted",
        "settings": {"layers": [layer_entry(r) for r in layers if r["project_id"] is None],
                     "roles": [role_entry(r) for r in await db.read_roles(conn)]},
        "channels": channels,
        "installs": [install_entry(r) for r in installs if r["scope"] == "global"],
    }
    summary.counts.update({"settings.layers": len(head["settings"]["layers"]),
                           "settings.roles": len(head["settings"]["roles"]), "channels": len(channels),
                           "installs": len(head["installs"])})
    summary.secrets += sum(_secret_count(entry) for entry in channels)
    yield "{" + ",\n".join(f"{_dump(k)}:{_dump(v)}" for k, v in head.items())

    if include_projects:
        documents = defaultdict(list)
        for row in await db.read_documents(conn, content=True):
            documents[row["project_id"]].append(row)
        projects = await db.read_projects(conn)
        summary.counts["projects"] = len(projects)
        yield ',\n"projects":['
        for index, project in enumerate(projects):
            owned = []
            for row in items:
                if row["owner_project_id"] == project["id"]:
                    owned.append(item_entry(row, await db.read_item_files(conn, row["id"]), include_secrets))
            entry = {
                "name": project["name"], "description": project["description"], "background": project["background"],
                "execution": {column: project[column] for column in EXECUTION_COLUMNS},
                "layers": [layer_entry(r) for r in layers if r["project_id"] == project["id"]],
                "knowledge": [document_entry(r) for r in documents[project["id"]]],
                "items": owned,
                "installs": [install_entry(r) for r in installs if r["project_id"] == project["id"]],
            }
            summary.repoint += sum(1 for d in entry["knowledge"] if d.get("repoint"))
            yield ("\n" if index == 0 else ",\n") + _dump(entry)
        yield "]"

    catalog = [row for row in items if row["owner_project_id"] is None]
    summary.counts["catalog"] = len(catalog)
    yield ',\n"catalog":['
    for index, row in enumerate(catalog):
        files = None if row["kind"] == "mcp" else await db.read_item_files(conn, row["id"])
        entry = item_entry(row, files, include_secrets)
        if row["kind"] == "mcp":
            summary.secrets += _secret_count(entry)
        yield ("\n" if index == 0 else ",\n") + _dump(entry)
    yield "]}\n"


async def _read_only(dsn: str) -> Any:
    import asyncpg

    return await asyncpg.connect(dsn, server_settings={"default_transaction_read_only": "on"})


async def export_to_file(dsn: str, path: Path, *, include_projects: bool, include_secrets: bool) -> ExportSummary:
    """Write the configuration file from a read-only connection; the file appears whole or not at all."""
    summary = ExportSummary()
    conn = await _read_only(dsn)
    path = path.expanduser()
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent.resolve(), prefix=f".{path.name}.",
                                         delete=False)
    try:
        if not include_secrets:
            os.chmod(handle.name, 0o644 & ~_umask())
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            async for chunk in export_chunks(conn, include_projects=include_projects,
                                             include_secrets=include_secrets, summary=summary):
                handle.write(chunk)
        handle.close()
        os.replace(handle.name, path)
    except BaseException:
        handle.close()
        Path(handle.name).unlink(missing_ok=True)
        raise
    finally:
        await conn.close()
    summary.bytes = path.stat().st_size
    return summary


def _umask() -> int:
    mask = os.umask(0)
    os.umask(mask)
    return mask


def read_file(path: Path) -> dict[str, Any]:
    """The configuration file at ``path``; raises ``ConfigFileError`` when it is not one."""
    try:
        with path.expanduser().open(encoding="utf-8") as handle:
            data = json.load(handle)
    except OSError as exc:
        raise ConfigFileError(f"cannot read {path}: {exc.strerror or exc}") from None
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ConfigFileError(f"{path} is not a LoopX configuration file: {exc}") from None
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        found = data.get("format") if isinstance(data, dict) else None
        raise ConfigFileError(f"{path} is not a {FORMAT} file (format: {found!r})")
    for section, kind in (("settings", dict), ("channels", list), ("installs", list), ("catalog", list),
                          ("projects", list)):
        if section in data and not isinstance(data[section], kind):
            raise ConfigFileError(f"{section} must be a JSON {'object' if kind is dict else 'list'}")
    for section in ("layers", "roles"):
        if not isinstance(data.get("settings", {}).get(section, []), list):
            raise ConfigFileError(f"settings.{section} must be a JSON list")
    return data


# --- Import: validating one entry (each raises _Skip) ---


def _mapping(raw: Any, what: str) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise _Skip(f"{what} is not a JSON object")
    return raw


def _string(raw: Any, what: str, *, max_chars: int | None = None, required: bool = False) -> str:
    if not isinstance(raw, str) or "\x00" in raw:
        raise _Skip(f"{what} must be a string")
    if required and not raw.strip():
        raise _Skip(f"{what} is empty")
    if max_chars is not None and len(raw) > max_chars:
        raise _Skip(f"{what} is over {max_chars} characters")
    return raw


def _boolean(raw: Any, what: str) -> bool:
    if not isinstance(raw, bool):
        raise _Skip(f"{what} must be true or false")
    return raw


def _whole(raw: Any, what: str, low: int, high: int) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or not low <= raw <= high:
        raise _Skip(f"{what} must be a whole number from {low} to {high}")
    return raw


def _relative_path(raw: Any, what: str) -> str:
    path = _string(raw, what, max_chars=_PATH_MAX, required=True)
    if path.startswith("/") or "\\" in path or any(part in ("", ".", "..") for part in path.split("/")):
        raise _Skip(f"{what} {path[:80]!r} is not a relative path without . or .. parts")
    return path


def _item_name(kind: str, raw: Any) -> str:
    from .catalog import CatalogError
    from .mcp import _name as mcp_name

    if kind == "mcp":
        try:
            return mcp_name(raw)
        except CatalogError as exc:
            raise _Skip(str(exc)) from None
    name = _string(raw, "name", max_chars=_ITEM_NAME_MAX, required=True)
    if name in (".", "..") or "/" in name or "\\" in name:
        raise _Skip(f"name {name[:80]!r} cannot name a directory")
    return name


def _layer_values(entry: Mapping[str, Any], scope: str) -> dict[str, Any]:
    from .agent_settings import Layer, SettingsError, layer_update

    unknown = sorted(set(entry) - {"scope", "role", *_LAYER_FIELDS})
    if unknown:
        raise _Skip(f"unknown field {unknown[0]!r}")
    try:
        return layer_update({name: entry.get(name) for name in _LAYER_FIELDS}, Layer(scope))
    except SettingsError as exc:
        raise _Skip(str(exc)) from None


def _stored_layer(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {name: row.get(name) for name in _LAYER_FIELDS} | {"env": list(row.get("env") or [])}


def _differs(new: Mapping[str, Any], old: Mapping[str, Any] | None, names: Iterable[str]) -> tuple[str, ...]:
    if old is None:
        return ()
    return tuple(name for name in names if new.get(name) != old.get(name))


def _fill_placeholders(given: Mapping[str, Any], stored: Mapping[str, Any], *, strings: str = "",
                       ) -> tuple[dict[str, Any], list[str]]:
    """``given`` with each placeholder replaced by the stored value ('' when there is none), and the names whose
    value is still empty. With ``strings`` (what the values are), every other value must be a string."""
    resolved, missing = {}, []
    for name, value in given.items():
        if is_placeholder(value):
            value = stored.get(name) or ""
            if value == "":
                missing.append(str(name))
        elif strings and not isinstance(value, str):
            raise _Skip(f"{strings} {name}: the value must be a string or the secret placeholder")
        resolved[str(name)] = value
    return resolved, missing


def _files(raw: Any) -> list[dict[str, Any]]:
    from .library import FILE_MAX_BYTES, ITEM_MAX_BYTES, ITEM_MAX_FILES

    if not isinstance(raw, list) or not raw:
        raise _Skip("files must be a non-empty list")
    if len(raw) > ITEM_MAX_FILES:
        raise _Skip(f"more than {ITEM_MAX_FILES} files")
    files, seen, total = [], set(), 0
    for entry in raw:
        entry = _mapping(entry, "a file")
        path = _relative_path(entry.get("path"), "a file path")
        content = _string(entry.get("content"), f"the content of {path}")
        size = len(content.encode("utf-8"))
        if size > FILE_MAX_BYTES:
            raise _Skip(f"{path} has {size} bytes, over {FILE_MAX_BYTES}")
        if path in seen:
            raise _Skip(f"{path} is given twice")
        if entry.get("sha256") != _sha256(content):
            raise _Skip(f"{path} does not match its sha256")
        seen.add(path)
        total += size
        files.append({"path": path, "content": content, "sha256": entry["sha256"],
                      "executable": _boolean(entry.get("executable", False), f"{path}: executable")})
    if total > ITEM_MAX_BYTES:
        raise _Skip(f"{total} bytes, over {ITEM_MAX_BYTES}")
    return files


@dataclass
class _Item:
    """One catalog entry, validated: its row fields and its files or server."""

    key: tuple[str, str, str | None]  # kind, name, owner project id (or the new project's key)
    values: dict[str, Any]
    files: list[dict[str, Any]] | None
    server: dict[str, Any] | None
    missing: list[str]


def _item(entry: Any, owner: str | None, stored: Mapping[tuple[str, str, str | None], Mapping[str, Any]],
          ) -> _Item:
    from .catalog import CatalogError, Origin
    from .library import item_sha256
    from .mcp import parse as mcp_parse

    entry = _mapping(entry, "an item")
    kind = entry.get("kind")
    if kind not in ("plugin", "skill", "mcp"):
        raise _Skip(f"kind {kind!r} is not plugin, skill or mcp")
    name = _item_name(kind, entry.get("name"))
    origin = entry.get("origin")
    allowed = {Origin.PROJECT.value} if owner else {o.value for o in Origin} - {Origin.PROJECT.value}
    if origin not in allowed:
        raise _Skip(f"origin {origin!r} is not one of {', '.join(sorted(allowed))}")
    if owner and kind != "skill":
        raise _Skip("a project writes skills only")
    key = (kind, name, owner)
    current = stored.get(key)
    source = entry.get("source")
    source = (current["source"] if current else "") if source is None else _string(source, "source", max_chars=4096)
    values = {"kind": kind, "name": name, "origin": origin, "source": source,
              "version": _string(entry.get("version", ""), "version", max_chars=256),
              "description": _string(entry.get("description", ""), "description", max_chars=4096),
              "modified": _boolean(entry.get("modified", False), "modified")}
    if kind == "mcp":
        server = _mapping(entry.get("server"), "server")
        stored_server = current if current and current["kind"] == "mcp" else {}
        env, missing_env = _fill_placeholders(_mapping(server.get("env", {}), "env"),
                                              dict(stored_server.get("env") or {}), strings="env")
        headers, missing_headers = _fill_placeholders(_mapping(server.get("headers", {}), "headers"),
                                                      dict(stored_server.get("headers") or {}), strings="header")
        try:
            parsed = mcp_parse({**server, "env": env, "headers": headers})
        except CatalogError as exc:
            raise _Skip(str(exc)) from None
        values |= {"sha256": parsed.sha256, "size_bytes": parsed.size_bytes, "file_count": 0}
        return _Item(key, values, None, parsed.row(),
                     [f"env {n}" for n in missing_env] + [f"header {n}" for n in missing_headers])
    files = _files(entry.get("files"))
    sha256 = item_sha256(files)
    if entry.get("sha256") != sha256:
        raise _Skip("its sha256 does not match its files")
    values |= {"sha256": sha256, "size_bytes": sum(len(f["content"].encode("utf-8")) for f in files),
               "file_count": len(files)}
    return _Item(key, values, files, None, [])


def _item_changes(item: _Item, current: Mapping[str, Any] | None) -> tuple[str, ...]:
    if current is None:
        return ()
    changed = [name for name in _ITEM_FIELDS if item.values[name] != current[name]]
    if item.server is not None:
        stored = {"transport": current["transport"], "command": current["command"], "args": list(current["args"] or ()),
                  "env": dict(current["env"] or {}), "url": current["url"], "headers": dict(current["headers"] or {})}
        changed += [f"server.{name}" for name in item.server if item.server[name] != stored.get(name)]
    elif item.values["sha256"] != current["sha256"]:
        changed.append("files")
    return tuple(changed)


def _document(entry: Any) -> dict[str, Any]:
    from .knowledge import DOCUMENT_MAX_BYTES, FILENAME_MAX_CHARS, KnowledgeError, document_summary, document_title

    entry = _mapping(entry, "a document")
    try:
        title, summary = document_title(entry.get("title")), document_summary(entry.get("summary"))
    except KnowledgeError as exc:
        raise _Skip(str(exc)) from None
    kind = entry.get("source_kind")
    values = {"title": title, "summary": summary, "source_kind": kind}
    if kind == "path":
        sha256 = _string(entry.get("sha256"), "sha256")
        if not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise _Skip("sha256 is not a SHA-256 hex digest")
        return values | {"filename": "", "content": None, "path": _relative_path(entry.get("path"), "path"),
                         "sha256": sha256, "size_bytes": _whole(entry.get("size_bytes"), "size_bytes", 0, 2**40)}
    if kind not in ("text", "upload"):
        raise _Skip(f"source_kind {kind!r} is not text, upload or path")
    content = _string(entry.get("content"), "content")
    data = content.encode("utf-8")
    if len(data) > DOCUMENT_MAX_BYTES:
        raise _Skip(f"its content has {len(data)} bytes, over {DOCUMENT_MAX_BYTES}")
    if entry.get("sha256") != hashlib.sha256(data).hexdigest():
        raise _Skip("its content does not match its sha256")
    filename = _string(entry.get("filename"), "filename", max_chars=FILENAME_MAX_CHARS, required=True)
    return values | {"filename": filename, "content": content, "path": None, "sha256": entry["sha256"],
                     "size_bytes": len(data)}


# --- Import: the target and the plan ---


@dataclass
class _Target:
    """What the database holds now, by natural key."""

    layers: dict[tuple[str, str | None, str | None], dict[str, Any]] = field(default_factory=dict)
    roles: dict[str, dict[str, Any]] = field(default_factory=dict)
    channels: dict[str, dict[str, Any]] = field(default_factory=dict)
    items: dict[tuple[str, str, str | None], dict[str, Any]] = field(default_factory=dict)
    installs: dict[tuple[str, str | None], dict[str, Any]] = field(default_factory=dict)
    projects: dict[str, dict[str, Any]] = field(default_factory=dict)  # by lower-case name
    documents: dict[str, list[dict[str, Any]]] = field(default_factory=lambda: defaultdict(list))

    @classmethod
    async def read(cls, conn: Any) -> _Target:
        from ..db import config_transfer as db

        target = cls()
        if not await db.has_schema(conn):
            return target
        for row in await db.read_layers(conn):
            target.layers[(row["scope"], row["role"], row["project_id"])] = row
        target.roles = {row["agent_id"]: row for row in await db.read_roles(conn)}
        for row in await db.read_channels(conn):
            target.channels.setdefault(row["channel_type"], row)
        target.items = {(row["kind"], row["name"], row["owner_project_id"]): row for row in await db.read_items(conn)}
        target.installs = {(row["item_id"], row["project_id"]): row for row in await db.read_installs(conn)}
        target.projects = {row["name"].lower(): row for row in await db.read_projects(conn)}
        for row in await db.read_documents(conn, content=False):
            target.documents[row["project_id"]].append(row)
        return target


class _Importer:
    def __init__(self, conn: Any, target: _Target, apply: bool) -> None:
        self.conn = conn
        self.target = target
        self.apply = apply
        self.plan = Plan(applied=apply)
        # Item ids by key, for installs: stored ones and the ones this import adds ("new:..." in a preview).
        self.item_ids = {key: row["id"] for key, row in target.items.items()}
        self.item_sizes = {key: int(row["size_bytes"]) for key, row in target.items.items()}
        self.project_ids = {name: row["id"] for name, row in target.projects.items()}
        self.seen_items: set[tuple[str, str, str | None]] = set()
        self.seen_projects: set[str] = set()

    def record(self, section: str, key: str, action: Action, fields: Sequence[str] = (), reason: str = "") -> None:
        self.plan.changes.append(Change(section, key, action, tuple(fields), reason))

    def outcome(self, section: str, key: str, current: Any, changed: Sequence[str]) -> Action:
        action = Action.ADDED if current is None else Action.CHANGED if changed else Action.UNCHANGED
        self.record(section, key, action, changed)
        return action

    # --- settings ---

    async def layer(self, entry: Any, project_id: str | None, project_label: str = "") -> None:
        section = "project.layers" if project_label else "settings.layers"
        raw = entry if isinstance(entry, Mapping) else {}
        scope, role = raw.get("scope"), raw.get("role")
        label = f"{project_label}{scope}" + (f":{role}" if role else "")
        try:
            _mapping(entry, "a layer")
            scopes = ("project", "role_project") if project_label else ("system", "role")
            if scope not in scopes:
                raise _Skip(f"scope {scope!r} is not {' or '.join(scopes)} here")
            roles = ("planner", "worker", "reviewer") if scope == "role_project" else (
                "manager", "planner", "worker", "reviewer")
            if scope in ("role", "role_project") and role not in roles:
                raise _Skip(f"role {role!r} is not one of {', '.join(roles)}")
            if scope in ("system", "project") and role is not None:
                raise _Skip(f"a {scope} layer has no role")
            values = _layer_values(raw, scope)
        except _Skip as exc:
            self.record(section, label, Action.SKIPPED, reason=str(exc))
            return
        current = _stored_layer(self.target.layers.get((scope, role, project_id)))
        action = self.outcome(section, label, current, _differs(values, current, _LAYER_FIELDS))
        if self.apply and action in (Action.ADDED, Action.CHANGED):
            from ..db import config_transfer as db

            await db.write_layer(self.conn, scope, role, project_id, values)

    async def role(self, entry: Any) -> None:
        from ..db import config_transfer as db

        raw = entry if isinstance(entry, Mapping) else {}
        role = raw.get("role")
        try:
            entry = _mapping(entry, "a role")
            if role not in db.ROLE_AGENTS:
                raise _Skip(f"role {role!r} is not one of {', '.join(db.ROLE_AGENTS)}")
            prompt = entry.get("prompt")
            values = {
                "prompt": None if prompt is None else _string(prompt, "prompt", max_chars=_PROMPT_MAX_CHARS),
                "timeout_seconds": _whole(entry.get("timeout_seconds"), "timeout_seconds", 0, _TIMEOUT_MAX_SECONDS),
                "max_concurrency": _whole(entry.get("max_concurrency"), "max_concurrency", 1, _CONCURRENCY_MAX),
                "enabled": _boolean(entry.get("enabled"), "enabled"),
            }
        except _Skip as exc:
            self.record("settings.roles", str(role), Action.SKIPPED, reason=str(exc))
            return
        row = self.target.roles.get(role)
        if row is not None and values["prompt"] == row["default_prompt"]:
            values["prompt"] = None  # this installation's recommended prompt, which it then follows
        current = role_entry(row) if row is not None else None
        action = self.outcome("settings.roles", role, current, _differs(values, current, _ROLE_FIELDS))
        if self.apply and action in (Action.ADDED, Action.CHANGED):
            await db.write_role(self.conn, role, values, exists=row is not None)

    async def channel(self, entry: Any) -> None:
        from .channels.allowlist import AllowlistError, normalized_config

        raw = entry if isinstance(entry, Mapping) else {}
        kind = raw.get("channel_type")
        try:
            entry = _mapping(entry, "a channel")
            if kind not in _CHANNEL_PUBLIC_KEYS:
                raise _Skip(f"channel type {kind!r} is not one of {', '.join(_CHANNEL_PUBLIC_KEYS)}")
            row = self.target.channels.get(kind)
            channel_id = row["channel_id"] if row else entry.get("channel_id") or f"channel-{kind}"
            if not isinstance(channel_id, str) or not _CHANNEL_ID.fullmatch(channel_id):
                raise _Skip(f"channel_id {channel_id!r} is not usable")
            config, missing = _fill_placeholders(_mapping(entry.get("config", {}), "config"),
                                                 dict(row["config"] or {}) if row else {})
            try:
                config = normalized_config(kind, config)
            except AllowlistError as exc:
                raise _Skip(str(exc)) from None
            values = {"display_name": _string(entry.get("display_name", ""), "display_name", max_chars=200),
                      "enabled": _boolean(entry.get("enabled", False), "enabled"), "config": config}
        except _Skip as exc:
            self.record("channels", str(kind), Action.SKIPPED, reason=str(exc))
            return
        current = {"display_name": row["display_name"], "enabled": row["enabled"],
                   "config": dict(row["config"] or {})} if row else None
        action = self.outcome("channels", kind, current, _differs(values, current, ("display_name", "enabled",
                                                                                      "config")))
        needed = [f"config {name}" for name in missing]
        if values["enabled"]:
            needed += [f"environment {value}" for name, value in sorted(config.items())
                       if name.endswith("_env") and isinstance(value, str) and value]
        if needed:
            self.plan.need_secret.append(("channels", kind, tuple(needed)))
        if self.apply and action in (Action.ADDED, Action.CHANGED):
            from ..db import config_transfer as db

            await db.write_channel(self.conn, channel_id, kind, values, exists=row is not None)

    # --- catalog ---

    async def item(self, entry: Any, owner_id: str | None, project_label: str = "") -> None:
        section = "project.items" if project_label else "catalog"
        raw = entry if isinstance(entry, Mapping) else {}
        label = f"{project_label}{raw.get('kind')}:{raw.get('name')}"
        try:
            item = _item(entry, owner_id, self.target.items)
        except _Skip as exc:
            self.record(section, label, Action.SKIPPED, reason=str(exc))
            return
        if item.key in self.seen_items:
            self.record(section, label, Action.SKIPPED, reason="the file gives this item twice")
            return
        self.seen_items.add(item.key)
        current = self.target.items.get(item.key)
        changed = _item_changes(item, current)
        action = self.outcome(section, label, current, changed)
        if item.missing:
            self.plan.need_secret.append((section, label, tuple(item.missing)))
        self.item_sizes[item.key] = item.values["size_bytes"]
        self.item_ids[item.key] = current["id"] if current else f"new:{label}"
        if self.apply and action in (Action.ADDED, Action.CHANGED):
            from ..db import config_transfer as db

            owner = owner_id if owner_id and not owner_id.startswith("new:") else None
            files = item.files if current is None or "files" in changed else None
            self.item_ids[item.key] = await db.write_item(
                self.conn, current["id"] if current else None, item.values, owner_project_id=owner,
                files=files, server=item.server)

    def check_caps(self) -> None:
        from .library import LIBRARY_MAX_BYTES
        from .mcp import SERVERS_MAX

        total = sum(self.item_sizes.values())
        if total > LIBRARY_MAX_BYTES:
            raise ConfigFileError(f"the catalog would hold {total} bytes, over its cap of {LIBRARY_MAX_BYTES}; "
                                  "nothing was imported")
        servers = sum(1 for kind, _, _ in self.item_ids if kind == "mcp")
        if servers > SERVERS_MAX:
            raise ConfigFileError(f"the catalog would hold {servers} MCP servers, over its cap of {SERVERS_MAX}; "
                                  "nothing was imported")

    async def install(self, entry: Any, project_id: str | None, project_label: str = "") -> None:
        from .catalog import CatalogError, Scope, install_roles

        section = "project.installs" if project_label else "installs"
        label = f"{project_label}?"
        try:
            entry = _mapping(entry, "an install")
            ref = _mapping(entry.get("item"), "item")
            owner_name = ref.get("project")
            label = f"{project_label}{ref.get('kind')}:{ref.get('name')}" + (f"@{owner_name}" if owner_name else "")
            if owner_name is not None and project_id is None:
                raise _Skip("a project's own item is installed in projects only")
            owner = None
            if owner_name is not None:
                owner = self.project_ids.get(str(owner_name).lower())
                if owner is None:
                    raise _Skip(f"no project {owner_name!r} here or in the file")
            item_id = self.item_ids.get((ref.get("kind"), ref.get("name"), owner))
            if item_id is None:
                raise _Skip("no such item here or in the file")
            try:
                roles = install_roles(entry.get("roles"), Scope.PROJECT if project_id else Scope.GLOBAL)
            except CatalogError as exc:
                raise _Skip(str(exc)) from None
            values = {"roles": roles, "enabled": _boolean(entry.get("enabled", True), "enabled")}
        except _Skip as exc:
            self.record(section, label, Action.SKIPPED, reason=str(exc))
            return
        row = self.target.installs.get((item_id, project_id))
        current = {"roles": list(row["roles"]), "enabled": row["enabled"]} if row else None
        action = self.outcome(section, label, current, _differs(values, current, ("roles", "enabled")))
        if self.apply and action in (Action.ADDED, Action.CHANGED):
            from ..db import config_transfer as db

            await db.write_install(self.conn, row["id"] if row else None, item_id=item_id, project_id=project_id,
                                   roles=values["roles"], enabled=values["enabled"])

    # --- projects ---

    async def project(self, entry: Mapping[str, Any]) -> str | None:
        """Add or update a project's own fields; its id (``new:...`` in a preview), or None when skipped."""
        from ..db.projects import EXECUTION_COLUMNS
        from .project_execution import execution_fields
        from .projects import project_background, project_name

        raw_name = entry.get("name") if isinstance(entry, Mapping) else None
        try:
            entry = _mapping(entry, "a project")
            try:
                name = project_name(entry.get("name"))
                background = project_background(entry.get("background", ""))
                execution = execution_fields({column: _mapping(entry.get("execution", {}), "execution").get(column)
                                              for column in EXECUTION_COLUMNS})
            except ValueError as exc:
                raise _Skip(str(exc)) from None
            if name.lower() in self.seen_projects:
                raise _Skip("the file gives this project twice")
            self.seen_projects.add(name.lower())
            values = {"description": _string(entry.get("description", ""), "description", max_chars=10_000),
                      "background": background, **execution}
        except _Skip as exc:
            self.record("projects", str(raw_name), Action.SKIPPED, reason=str(exc))
            return None
        row = self.target.projects.get(name.lower())
        current = {k: row[k] for k in values} if row else None
        action = self.outcome("projects", name, current, _differs(values, current, values))
        project_id = row["id"] if row else f"new:{name}"
        if self.apply and action is Action.ADDED:
            from ..db.projects import create_project

            project_id = (await create_project(name=name, description=values["description"], background=background,
                                               execution=execution, pool=self.conn))["id"]
        elif self.apply and action is Action.CHANGED:
            from ..db.projects import update_project

            await update_project(row["id"], description=values["description"], background=background,
                                 execution=execution, pool=self.conn)
        self.project_ids[name.lower()] = project_id
        repoint = sum(1 for d in entry.get("knowledge") or () if isinstance(d, Mapping) and d.get("source_kind") == "path")
        if repoint and not (row and row.get("work_dir")):
            self.plan.notes.append(f"project {name}: {repoint} path document(s) refer to files in the project's "
                                   "directory; set its directory on this machine to re-point them")
        return project_id

    async def documents(self, entries: Any, project_id: str, label: str) -> None:
        if not isinstance(entries, list):
            self.record("project.knowledge", f"{label}knowledge", Action.SKIPPED, reason="knowledge is not a list")
            return
        stored = defaultdict(list)
        for row in self.target.documents.get(project_id, []):
            stored[row["title"]].append(row)
        seen: Counter[str] = Counter()
        for entry in entries:
            try:
                values = _document(entry)
            except _Skip as exc:
                title = entry.get("title") if isinstance(entry, Mapping) else None
                self.record("project.knowledge", f"{label}{title}", Action.SKIPPED, reason=str(exc))
                continue
            title = values["title"]
            index = seen[title]
            seen[title] += 1
            key = f"{label}{title}" + (f" #{index + 1}" if index else "")
            row = stored[title][index] if index < len(stored[title]) else None
            current = {name: row[name] for name in _DOCUMENT_FIELDS} if row else None
            action = self.outcome("project.knowledge", key, current, _differs(values, current, _DOCUMENT_FIELDS))
            if self.apply and action in (Action.ADDED, Action.CHANGED):
                from ..db import config_transfer as db

                await db.write_document(self.conn, row["id"] if row else None, project_id, values)


async def import_config(conn: Any, data: Mapping[str, Any], *, apply: bool) -> Plan:
    """The import's plan for ``data`` (from ``read_file``); with ``apply`` also written, in the caller's
    transaction. Raises ``ConfigFileError`` when the result would break a catalog cap."""
    if apply:
        from ..db.connection import ensure_schema, schema_change_pending

        if await schema_change_pending(conn):
            raise SchemaChangePending(
                "the database schema is older than this LoopX; start `loopx serve` once (it backs the database up "
                "before changing the schema), then import again")
        await ensure_schema(conn)
    importer = _Importer(conn, await _Target.read(conn), apply)
    settings = data.get("settings", {})
    for entry in settings.get("layers", []):
        await importer.layer(entry, None)
    for entry in settings.get("roles", []):
        await importer.role(entry)
    for entry in data.get("channels", []):
        await importer.channel(entry)
    for entry in data.get("catalog", []):
        await importer.item(entry, None)

    projects: list[tuple[Mapping[str, Any], str, str]] = []
    for entry in data.get("projects", []):
        project_id = await importer.project(entry)
        if project_id is not None:
            projects.append((entry, project_id, f"{entry['name'].strip()} / "))
    for entry, project_id, label in projects:
        for layer in entry.get("layers") or ():
            await importer.layer(layer, project_id, label)
        for item in entry.get("items") or ():
            await importer.item(item, project_id, label)
        await importer.documents(entry.get("knowledge") or [], project_id, label)
    importer.check_caps()

    for entry in data.get("installs", []):
        await importer.install(entry, None)
    for entry, project_id, label in projects:
        for install in entry.get("installs") or ():
            await importer.install(install, project_id, label)
    return importer.plan


async def import_file(dsn: str, path: Path, *, apply: bool) -> Plan:
    """Preview (read-only) or apply (one transaction) the file at ``path`` on the database at ``dsn``."""
    import asyncpg

    data = read_file(path)
    conn = await (asyncpg.connect(dsn) if apply else _read_only(dsn))
    try:
        async with conn.transaction(readonly=not apply):
            return await import_config(conn, data, apply=apply)
    finally:
        await conn.close()


# --- Reports for the CLI ---


def format_plan(plan: Plan, path: Path) -> str:
    lines = [f"{'Applied' if plan.applied else 'Preview of'} {path}:"]
    marks = {Action.ADDED: "+", Action.CHANGED: "~", Action.SKIPPED: "!"}
    for section, counts in plan.counts().items():
        lines.append(f"  {section:<18} " + "  ".join(f"{a.value} {counts[a.value]}" for a in Action))
        for change in plan.changes:
            if change.section == section and change.action in marks:
                detail = f" ({', '.join(change.fields)})" if change.fields else ""
                detail += f": {change.reason}" if change.reason else ""
                lines.append(f"    {marks[change.action]} {change.key}{detail}")
    if not plan.changes:
        lines.append("  the file holds nothing to import")
    if plan.need_secret:
        lines.append("Secrets to fill in on this machine:")
        lines += [f"  {section} {key}: {', '.join(names)}" for section, key, names in plan.need_secret]
    lines += [f"Note: {note}" for note in plan.notes]
    if plan.applied:
        lines.append(f"{plan.writes} entries written. Restart the server for channel changes to take effect.")
    else:
        lines.append(f"Nothing was written. Run again with --apply to write these {plan.writes} entries."
                     if plan.writes else "Nothing was written, and applying would change nothing.")
    return "\n".join(lines)


def format_export(summary: ExportSummary, path: Path, *, include_secrets: bool) -> str:
    counts = ", ".join(f"{section} {count}" for section, count in summary.counts.items())
    secrets = (f"{summary.secrets} secret values included" if include_secrets
               else f"{summary.secrets} secret values left out as placeholders")
    lines = [f"Wrote {path} ({summary.bytes} bytes, {FORMAT}): {counts}; {secrets}."]
    if summary.repoint:
        lines.append(f"{summary.repoint} path document(s) are references to re-point on the new machine.")
    lines.append(f"On the new machine: loopx config import {path.name} (preview), then add --apply.")
    return "\n".join(lines)
