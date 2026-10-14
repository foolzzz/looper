"""MCP servers in the capability catalog (RFC decision 9, M8b).

An MCP server is a catalog item of kind ``mcp``, installed like a plugin or a
skill (``catalog``): globally or in one project, for some roles. How Claude
Code starts or reaches it is a ``Server``: ``stdio`` (a command, its arguments
and its environment) or ``http`` / ``sse`` (a URL and its headers), stored in
``library_mcp_servers``.

- **Secrets.** Every environment value and every header value is a secret. The
  database holds it; the API shows its name with ``MASK`` in place of the
  value (``public_view``), and a change that sends ``MASK`` back for a name
  keeps the stored value (``parse``). An item's version is the hash of its
  whole configuration, so a new value is a new version.
- **Acquiring.** Added and edited through the catalog API (origin ``ui``), or
  imported, read-only, from the user-level ``mcpServers`` of this machine's
  Claude Code (``claude_json_path``; origin ``machine``). A re-import replaces
  the imported servers that changed, keeps their installs and removes the ones
  that are gone; a new one is installed nowhere.
- **Launch.** The servers installed for a run become one read-only JSON file,
  ``<runtime_root>/.local/mcp/<sha256>.json`` named by its content
  (``write_config``), passed as ``--mcp-config`` beside the isolation flags'
  ``--strict-mcp-config`` (``claude_cli.library_args``). The run's record names
  the servers, never their values.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import tempfile
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .catalog import CatalogConflict, CatalogError, CatalogNotFound, Origin

logger = logging.getLogger(__name__)

# What the API shows in place of a stored environment or header value; sent back, it keeps the value.
MASK = "***"

# Claude Code names a server's tools mcp__<name>__<tool>.
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")  # an HTTP token

# Caps, generous for a hand-written configuration.
DESCRIPTION_MAX = 1024
COMMAND_MAX = 4096
ARGS_MAX = 100
ARG_MAX = 4096
URL_MAX = 2048
VARIABLES_MAX = 50  # environment variables, or headers, of one server
VALUE_MAX = 8192
SERVERS_MAX = 200  # MCP servers in the catalog

_ITEM_FIELDS = frozenset({"name", "description"})
_SERVER_FIELDS = frozenset({"transport", "command", "args", "env", "url", "headers"})
# system_settings key of the last import's report.
LAST_IMPORT_KEY = "library.last_mcp_import"


class Transport(str, Enum):
    STDIO = "stdio"
    HTTP = "http"
    SSE = "sse"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class Server:
    """How Claude Code starts (``stdio``) or reaches (``http``, ``sse``) one MCP server."""

    transport: Transport
    command: str = ""
    args: tuple[str, ...] = ()
    env: tuple[tuple[str, str], ...] = ()  # by name
    url: str = ""
    headers: tuple[tuple[str, str], ...] = ()  # by name

    def config(self) -> dict[str, Any]:
        """Its entry in Claude Code's ``mcpServers``, values included."""
        if self.transport is Transport.STDIO:
            return {"type": "stdio", "command": self.command, "args": list(self.args), "env": dict(self.env)}
        return {"type": self.transport.value, "url": self.url, "headers": dict(self.headers)}

    @property
    def sha256(self) -> str:
        """The item's version: the hash of the whole configuration."""
        return _sha256(_canonical(self.config()))

    @property
    def size_bytes(self) -> int:
        return len(_canonical(self.config()).encode("utf-8"))

    def row(self) -> dict[str, Any]:
        """Its ``library_mcp_servers`` columns."""
        return {"transport": self.transport.value, "command": self.command, "args": list(self.args),
                "env": dict(self.env), "url": self.url, "headers": dict(self.headers)}

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> Server:
        def pairs(raw: Any) -> tuple[tuple[str, str], ...]:
            mapping = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
            return tuple(sorted((str(k), str(v)) for k, v in mapping.items()))

        return cls(Transport(row["transport"]), str(row["command"] or ""), tuple(row["args"] or ()),
                   pairs(row["env"]), str(row["url"] or ""), pairs(row["headers"]))


def public_view(row: Mapping[str, Any]) -> dict[str, Any]:
    """An MCP item's configuration for the API, from its catalog row: the names of its values, each ``MASK``.

    The catalog row (``db.library``) carries the names only, so the values
    cannot reach this view.
    """
    return {
        "transport": row["mcp_transport"],
        "command": row["mcp_command"] or "",
        "args": list(row["mcp_args"] or ()),
        "env": {name: MASK for name in row["mcp_env_keys"] or ()},
        "url": row["mcp_url"] or "",
        "headers": {name: MASK for name in row["mcp_header_keys"] or ()},
    }


# --- Validating what a request or an import gives ---


def _text(raw: Any, what: str, cap: int, *, required: bool = False) -> str:
    if not isinstance(raw, str):
        raise CatalogError(f"{what} must be a string")
    if "\x00" in raw:
        raise CatalogError(f"{what} must not contain a NUL character")
    if len(raw) > cap:
        raise CatalogError(f"{what} is over {cap} characters")
    if required and not raw.strip():
        raise CatalogError(f"give the server's {what}")
    return raw


def _values(raw: Any, stored: Mapping[str, str], what: str, pattern: re.Pattern[str]) -> tuple[tuple[str, str], ...]:
    """``{name: value}`` (``env`` or ``headers``); ``MASK`` for a name keeps its stored value."""
    if not isinstance(raw, Mapping):
        raise CatalogError(f"{what} is an object of names to values")
    if len(raw) > VARIABLES_MAX:
        raise CatalogError(f"{what} has more than {VARIABLES_MAX} entries")
    pairs = []
    for name, value in raw.items():
        if not isinstance(name, str) or not pattern.fullmatch(name):
            raise CatalogError(f"{what} name {name!r} is not usable")
        if value == MASK:
            if name not in stored:
                raise CatalogError(f"give the value of {what} {name}: {MASK} stands for a stored value, and it has "
                                   "none")
            value = stored[name]
        value = _text(value, f"the value of {what} {name}", VALUE_MAX)
        if what == "headers" and ("\r" in value or "\n" in value):
            raise CatalogError(f"the value of header {name} must be one line")
        pairs.append((name, value))
    return tuple(sorted(pairs))


def parse(data: Mapping[str, Any], current: Server | None = None) -> Server:
    """The server a request's fields describe; a field it leaves out keeps ``current``'s.

    Changing the transport starts from nothing: the other transport's fields
    go. Raises ``CatalogError``.
    """
    try:
        transport = Transport(data["transport"]) if "transport" in data else (
            current.transport if current is not None else None)
    except ValueError:
        raise CatalogError("transport is 'stdio', 'http' or 'sse'") from None
    if transport is None:
        raise CatalogError("give the transport: 'stdio', 'http' or 'sse'")
    if current is not None and current.transport is not transport:
        current = None
    base = current or Server(transport)
    if transport is Transport.STDIO:
        for other in ("url", "headers"):
            if data.get(other):
                raise CatalogError(f"a stdio server has no {other}")
        command = _text(data.get("command", base.command), "command", COMMAND_MAX, required=True)
        args = data.get("args", list(base.args))
        if not isinstance(args, list) or len(args) > ARGS_MAX:
            raise CatalogError(f"args is a list of at most {ARGS_MAX} strings")
        args = tuple(_text(arg, "an argument", ARG_MAX) for arg in args)
        env = _values(data.get("env", {k: MASK for k, _ in base.env}), dict(base.env), "env", _ENV_NAME)
        return Server(transport, command=command, args=args, env=env)
    for other in ("command", "args", "env"):
        if data.get(other):
            raise CatalogError(f"an {transport.value} server has no {other}")
    url = _text(data.get("url", base.url), "url", URL_MAX, required=True).strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise CatalogError("url must be an http:// or https:// URL")
    headers = _values(data.get("headers", {k: MASK for k, _ in base.headers}), dict(base.headers), "headers",
                      _HEADER_NAME)
    return Server(transport, url=url, headers=headers)


def _name(raw: Any) -> str:
    if not isinstance(raw, str) or not NAME.fullmatch(raw):
        raise CatalogError("name is 1 to 64 letters, digits, '-' or '_', starting with a letter or digit")
    return raw


def _description(raw: Any) -> str:
    return _text(raw, "description", DESCRIPTION_MAX).strip()


def _fields(data: Any) -> Mapping[str, Any]:
    if not isinstance(data, Mapping):
        raise CatalogError("body must be a JSON object")
    fields = _ITEM_FIELDS | _SERVER_FIELDS
    unknown = sorted(set(data) - fields)
    if unknown:
        raise CatalogError(f"unknown field {unknown[0]!r}; the fields are {', '.join(sorted(fields))}")
    return data


# --- The catalog API ---


async def _item(item_id: str) -> dict[str, Any]:
    from .catalog import _item as catalog_item

    return await catalog_item(item_id)


async def _locked_count(conn: Any) -> int:
    """How many MCP servers the catalog holds; adding or importing them is serialized on this lock."""
    await conn.execute("SELECT pg_advisory_xact_lock(hashtext('loopx.catalog.mcp'))")
    return int(await conn.fetchval("SELECT count(*) FROM library_items WHERE kind = 'mcp'"))


async def create(data: Any) -> dict[str, Any]:
    """Add an MCP server to the catalog, installed nowhere: ``{name, description?, transport, ...}``."""
    import asyncpg

    from ..db import library as db_library
    from ..db.connection import get_pool

    data = _fields(data)
    name = _name(data.get("name"))
    description = _description(data.get("description", ""))
    server = parse(data)
    item_id = f"mcp-{uuid.uuid4().hex[:12]}"
    try:
        async with get_pool().acquire() as conn, conn.transaction():
            if await _locked_count(conn) >= SERVERS_MAX:
                raise CatalogError(f"the catalog holds {SERVERS_MAX} MCP servers, its cap; delete one first")
            await db_library.insert_mcp(conn, item_id=item_id, name=name, description=description,
                                        origin=Origin.UI.value, source="", server=server.row(),
                                        sha256=server.sha256, size_bytes=server.size_bytes)
    except asyncpg.UniqueViolationError:
        raise CatalogConflict(f"the catalog already has an MCP server named {name!r}") from None
    return {"item": await _item(item_id)}


async def update(item_id: str, data: Any) -> dict[str, Any]:
    """Change an MCP server's name, description or configuration; a field left out stays as it is."""
    import asyncpg

    from ..db import library as db_library
    from ..db.connection import get_pool

    data = _fields(data)
    if not data:
        raise CatalogError(f"give at least one of {', '.join(sorted(_ITEM_FIELDS | _SERVER_FIELDS))}")
    try:
        async with get_pool().acquire() as conn, conn.transaction():
            row = await db_library.get_mcp_server(item_id, pool=conn, for_update=True)
            if row is None:
                item = await db_library.get_item(item_id, pool=conn)
                if item is None:
                    raise CatalogNotFound(f"no catalog item {item_id!r}")
                raise CatalogError(f"{item['kind']} {item['name']} is not an MCP server; only those are edited here")
            name = _name(data["name"]) if "name" in data else row["name"]
            description = _description(data["description"]) if "description" in data else row["description"]
            server = parse(data, Server.from_row(row)) if set(data) & _SERVER_FIELDS else Server.from_row(row)
            await db_library.update_mcp(conn, item_id, name=name, description=description, server=server.row(),
                                        sha256=server.sha256, size_bytes=server.size_bytes)
    except asyncpg.UniqueViolationError:
        raise CatalogConflict(f"the catalog already has an MCP server named {data.get('name')!r}") from None
    return {"item": await _item(item_id)}


# --- Importing from this machine (read-only) ---


def claude_json_path() -> Path:
    """Where this machine's Claude Code keeps its user-level MCP servers: ``.claude.json`` in
    ``$CLAUDE_CONFIG_DIR`` when that is set, else in the home directory."""
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(configured).expanduser() if configured else Path.home()) / ".claude.json"


@dataclass
class McpScan:
    servers: list[tuple[str, Server]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)


def scan_claude_json(path: Path) -> McpScan:
    """The user-level ``mcpServers`` of a Claude Code ``.claude.json``; nothing is written.

    A server whose name or configuration LoopX cannot use is left out and
    reported, and so is a field it does not support (the server is kept
    without it). Raises ``CatalogError`` when the file cannot be read.
    """
    from .library import _label

    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise CatalogError(f"no Claude Code configuration at {_label(path)}") from None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CatalogError(f"cannot read {_label(path)}: {exc}") from None
    servers = document.get("mcpServers") if isinstance(document, dict) else None
    scan = McpScan()
    if not isinstance(servers, dict):
        return scan
    for name in sorted(servers):
        entry = servers[name]
        if not NAME.fullmatch(name):
            scan.skipped.append({"item": name, "path": "", "reason": "name is not usable as an MCP server name"})
            continue
        if not isinstance(entry, dict):
            scan.skipped.append({"item": name, "path": "", "reason": "not an object"})
            continue
        given = {"transport": entry.get("type") or ("stdio" if "command" in entry else None)}
        for key, value in entry.items():
            if key in _SERVER_FIELDS:
                given[key] = value
            elif key != "type":
                scan.skipped.append({"item": name, "path": key, "reason": "not supported; left out"})
        try:
            scan.servers.append((name, parse(given)))
        except CatalogError as exc:
            scan.skipped.append({"item": name, "path": "", "reason": str(exc)})
    return scan


_import_lock = asyncio.Lock()


async def import_from_machine(path: Path | None = None) -> dict[str, Any]:
    """Import (or re-import) this machine's user-level MCP servers; returns the catalog view."""
    from ..db import library as db_library
    from ..db import system_settings
    from .catalog import catalog_view
    from .library import _label

    path = path or claude_json_path()
    async with _import_lock:
        scan = await asyncio.to_thread(scan_claude_json, path)
        changes = await db_library.replace_mcp_snapshot(
            [{"name": name, "source": _label(path), "server": server.row(), "sha256": server.sha256,
              "size_bytes": server.size_bytes} for name, server in scan.servers],
            servers_max=SERVERS_MAX,
        )
        taken = changes.pop("taken")
        skipped = [*scan.skipped, *({"item": name, "path": "", "reason": "the catalog has an MCP server of this "
                                     "name that was added here"} for name in taken)]
        report = {
            "imported_at": datetime.now(UTC).isoformat(),
            "source": _label(path),
            "servers": len(scan.servers) - len(taken),
            **changes,
            "skipped": skipped,
            "skipped_count": len(skipped),
        }
        await system_settings.put_settings({LAST_IMPORT_KEY: json.dumps(report, ensure_ascii=False)})
    logger.info("MCP import: %s", {k: v for k, v in report.items() if k != "skipped"})
    return await catalog_view()


# --- Launch ---


def config_directory(runtime_root: Path) -> Path:
    return runtime_root.absolute() / ".local" / "mcp"


def write_config(runtime_root: Path, servers: Sequence[tuple[str, Mapping[str, Any]]]) -> Path:
    """Write the ``--mcp-config`` file of ``servers`` (name, ``Server.config()``) once; returns its path.

    It is ``<runtime_root>/.local/mcp/<sha256 of its content>.json``, readable
    by this user only (it holds the values), written beside it and renamed
    into place so a reader never sees a partial one.
    """
    content = json.dumps({"mcpServers": {name: dict(config) for name, config in servers}}, ensure_ascii=False,
                         indent=2, sort_keys=True) + "\n"
    directory = config_directory(runtime_root)
    target = directory / f"{_sha256(content)}.json"
    if target.is_file():
        return target
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    fd, building = tempfile.mkstemp(prefix=".building.", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.chmod(building, 0o400)
        os.replace(building, target)
    except BaseException:
        Path(building).unlink(missing_ok=True)
        raise
    return target
