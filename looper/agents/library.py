"""The library: the capability catalog's items, how they are acquired, and how an agent run loads the ones installed.

Agents never load the operator's personal Claude Code configuration
(``claude_cli.isolation_args``). What they load comes from the catalog
(``library_items`` and ``library_files``) instead, and only where an item is
installed (``library_installs``, ``looper.agents.catalog``):

- **Import** (``import_from_machine``, ``POST /api/catalog/import``) acquires
  items from this machine. It reads, and only reads, the operator's Claude
  Code configuration (``$CLAUDE_CONFIG_DIR``, else ``~/.claude``): every plugin
  ``settings.json`` enables, from its user-scope install in
  ``plugins/installed_plugins.json``, taken whole (its skills, agents, commands
  and hooks); and every personal skill, ``skills/<name>/`` with a
  ``SKILL.md`` (a linked directory is followed). Personal settings, hooks,
  MCP servers and memory are not read. Each item and its files go into the
  database (``looper.db.library``), which is the source of truth; an item's
  version is the hash of its files. A re-import replaces the items that
  changed, keeps every item's installs and removes the items that are gone.
  A new item is installed nowhere. Items of other origins (a project's own
  skills, ``project_skills``) are left alone.
- **Caps**: a file over ``FILE_MAX_BYTES``, a binary file (not UTF-8 text) and
  a link out of its item are left out; an item over ``ITEM_MAX_BYTES`` or
  ``ITEM_MAX_FILES`` is left out whole; all of them are reported. A library
  over ``LIBRARY_MAX_BYTES`` is refused and nothing changes.
- **Launch** (``load_installed``): every agent run reads the items installed
  for its role, globally or in its project, and gets a read-only copy of each
  under ``<runtime_root>/.local/library/<sha256>/``, written once from the
  database. Each plugin is one ``--plugin-dir``; the skills are linked into
  generated plugins: a project's own skills into ``project`` (Claude Code
  names them ``project:<skill>``), every other skill into ``personal``
  (``personal:<skill>``). The MCP servers installed for the run go into one
  read-only ``--mcp-config`` file (``looper.agents.mcp``). The flags come from
  ``claude_cli.library_args`` and the run records the items (an MCP server by
  its name, never its values) and their hash.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any

logger = logging.getLogger(__name__)

# The plugins Claude Code loads the installed skills from: a project's own skills are named project:<skill>,
# every other skill personal:<skill>.
SKILLS_PLUGIN = "personal"
PROJECT_SKILLS_PLUGIN = "project"
_PLUGIN_DESCRIPTIONS = {
    SKILLS_PLUGIN: "Skills installed from the LoopX catalog.",
    PROJECT_SKILLS_PLUGIN: "This project's own skills, managed by LoopX.",
}

# Caps, measured against a real configuration (3 plugins and 104 skills, about
# 22 MiB of text in 2,200 files; the largest file 0.7 MiB, the largest item 9 MiB).
FILE_MAX_BYTES = 2 * 1024 * 1024
ITEM_MAX_BYTES = 32 * 1024 * 1024
ITEM_MAX_FILES = 5_000
LIBRARY_MAX_BYTES = 128 * 1024 * 1024

# Version control, Claude Code's own bookkeeping in its plugin cache, and OS clutter.
_LEFT_OUT = frozenset({".git", ".in_use", ".DS_Store", "__pycache__"})
# A plugin's or skill's directory name: what Claude Code names it by.
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
# How many skipped files an import report keeps.
_REPORT_SKIPPED_MAX = 200
# system_settings key of the last import's report.
LAST_IMPORT_KEY = "library.last_import"


class ItemKind(str, Enum):
    PLUGIN = "plugin"
    SKILL = "skill"
    MCP = "mcp"


class LibraryError(ValueError):
    """An import, a request or a stored item the library refuses (answered with 400)."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def item_sha256(files: Iterable[Any]) -> str:
    """An item's version: the hash of its files' paths, hashes and executable bits.

    ``files`` are ``SnapshotFile``s or mappings with the same fields.
    """
    def fields(f: Any) -> list[Any]:
        if isinstance(f, Mapping):
            return [f["path"], f["sha256"], bool(f["executable"])]
        return [f.path, f.sha256, f.executable]

    listing = sorted(fields(f) for f in files)
    return _sha256(json.dumps(listing, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def directory_name(kind: ItemKind, name: str) -> str:
    """The directory an item is materialized as: a plugin by its name (before ``@marketplace``), a skill by its own."""
    return name.partition("@")[0] if kind is ItemKind.PLUGIN else name


# --- Scanning the operator's Claude Code (read-only) ---


@dataclass(frozen=True)
class SnapshotFile:
    path: str  # relative, with "/"
    content: str
    sha256: str
    executable: bool


@dataclass(frozen=True)
class Skipped:
    """Something the import left out: a whole item (``path`` is ``""``) or one of its files."""

    item: str
    path: str
    reason: str
    size_bytes: int | None = None

    def view(self) -> dict[str, Any]:
        return {"item": self.item, "path": self.path, "reason": self.reason, "size_bytes": self.size_bytes}


@dataclass(frozen=True)
class ScannedItem:
    kind: ItemKind
    name: str
    source: str  # where it was read from, with ~ for the home directory
    version: str  # a plugin's declared version; '' for a skill
    files: tuple[SnapshotFile, ...]

    @property
    def sha256(self) -> str:
        return item_sha256(self.files)

    @property
    def size_bytes(self) -> int:
        return sum(len(f.content.encode("utf-8")) for f in self.files)

    def row(self) -> dict[str, Any]:
        """The item as ``looper.db.library.replace_snapshot`` stores it."""
        return {
            "kind": self.kind.value, "name": self.name, "source": self.source, "version": self.version,
            "sha256": self.sha256, "size_bytes": self.size_bytes, "file_count": len(self.files),
            "files": [{"path": f.path, "content": f.content, "sha256": f.sha256, "executable": f.executable}
                      for f in self.files],
        }


@dataclass
class Scan:
    items: list[ScannedItem] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)


class _ItemTooLarge(Exception):
    pass


def claude_config_dir() -> Path:
    """Where the operator's Claude Code keeps its configuration."""
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(configured).expanduser() if configured else Path.home() / ".claude"


def _label(path: Path) -> str:
    """``path`` for people: under the home directory it starts with ``~``."""
    try:
        return "~/" + path.relative_to(Path.home()).as_posix()
    except ValueError:
        return str(path)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LibraryError(f"cannot read {_label(path)}: {exc}") from None


def _collect(root: Path, item: str, skipped: list[Skipped]) -> tuple[SnapshotFile, ...]:
    """Every file of the item in ``root``; what is left out goes to ``skipped``.

    Raises ``_ItemTooLarge`` when the item is over a cap.
    """
    real_root = root.resolve()
    files: list[SnapshotFile] = []
    total = 0
    for directory, subdirectories, names in os.walk(real_root):
        here = Path(directory)
        kept = []
        for name in sorted(subdirectories):
            if name in _LEFT_OUT:
                continue
            if (here / name).is_symlink():  # os.walk does not follow it; say so
                skipped.append(Skipped(item, (here / name).relative_to(real_root).as_posix(), "linked directory"))
                continue
            kept.append(name)
        subdirectories[:] = kept
        for name in sorted(names):
            if name in _LEFT_OUT:
                continue
            path = here / name
            relative = path.relative_to(real_root).as_posix()
            target = path
            if path.is_symlink():
                try:
                    target = path.resolve(strict=True)
                except (OSError, RuntimeError):
                    skipped.append(Skipped(item, relative, "broken link"))
                    continue
                if not target.is_relative_to(real_root):
                    skipped.append(Skipped(item, relative, "link out of the item"))
                    continue
            info = target.stat()
            if not stat.S_ISREG(info.st_mode):
                skipped.append(Skipped(item, relative, "not a regular file"))
                continue
            if info.st_size > FILE_MAX_BYTES:
                skipped.append(Skipped(item, relative, "oversized", info.st_size))
                continue
            data = target.read_bytes()
            try:
                if b"\x00" in data:
                    raise UnicodeDecodeError("utf-8", data, 0, 1, "NUL byte")
                content = data.decode("utf-8")
            except UnicodeDecodeError:
                skipped.append(Skipped(item, relative, "binary", len(data)))
                continue
            files.append(SnapshotFile(relative, content, _sha256(data), bool(info.st_mode & 0o111)))
            total += len(data)
            if len(files) > ITEM_MAX_FILES:
                raise _ItemTooLarge(f"more than {ITEM_MAX_FILES} files")
            if total > ITEM_MAX_BYTES:
                raise _ItemTooLarge(f"more than {ITEM_MAX_BYTES} bytes")
    return tuple(files)


def _add_item(scan: Scan, kind: ItemKind, name: str, root: Path, source: str, version: str) -> None:
    if not _NAME.fullmatch(directory_name(kind, name)):
        scan.skipped.append(Skipped(name, "", "name is not usable as a directory name"))
        return
    skipped: list[Skipped] = []
    try:
        files = _collect(root, name, skipped)
    except _ItemTooLarge as exc:
        scan.skipped.append(Skipped(name, "", f"too large: {exc}"))
        return
    except OSError as exc:
        scan.skipped.append(Skipped(name, "", f"cannot be read: {exc}"))
        return
    scan.skipped.extend(skipped)
    scan.items.append(ScannedItem(kind, name, source, version, files))


def _scan_plugins(config: Path, scan: Scan) -> None:
    settings = _read_json(config / "settings.json") or {}
    enabled = settings.get("enabledPlugins") if isinstance(settings, dict) else None
    if not isinstance(enabled, dict):
        return
    registry = _read_json(config / "plugins" / "installed_plugins.json") or {}
    installed = registry.get("plugins") if isinstance(registry, dict) else None
    installed = installed if isinstance(installed, dict) else {}
    for key in sorted(k for k, on in enabled.items() if on is True):
        installs = [i for i in installed.get(key) or [] if isinstance(i, dict) and i.get("scope") == "user"]
        if not installs or not isinstance(installs[-1].get("installPath"), str):
            scan.skipped.append(Skipped(key, "", "not installed for the user"))
            continue
        install = installs[-1]
        root = Path(install["installPath"]).expanduser()
        if not root.is_dir():
            scan.skipped.append(Skipped(key, "", f"install directory {_label(root)} is missing"))
            continue
        _add_item(scan, ItemKind.PLUGIN, key, root, _label(root), str(install.get("version") or ""))


def _scan_skills(config: Path, scan: Scan) -> None:
    skills = config / "skills"
    if not skills.is_dir():
        return
    for entry in sorted(skills.iterdir(), key=lambda p: p.name):
        if entry.name.startswith(".") or not entry.is_dir():
            continue
        if not (entry / "SKILL.md").is_file():
            scan.skipped.append(Skipped(entry.name, "", "no SKILL.md"))
            continue
        _add_item(scan, ItemKind.SKILL, entry.name, entry, _label(entry), "")


def scan_machine(config_dir: Path | None = None) -> Scan:
    """Read the operator's enabled plugins and personal skills; nothing is written.

    Raises ``LibraryError`` when there is no configuration, ``settings.json``
    cannot be read, or the whole library is over ``LIBRARY_MAX_BYTES``.
    """
    config = config_dir or claude_config_dir()
    if not config.is_dir():
        raise LibraryError(f"no Claude Code configuration at {_label(config)}")
    scan = Scan()
    _scan_plugins(config, scan)
    _scan_skills(config, scan)
    total = sum(item.size_bytes for item in scan.items)
    if total > LIBRARY_MAX_BYTES:
        raise LibraryError(
            f"the library would hold {total} bytes, over the cap of {LIBRARY_MAX_BYTES}; nothing was imported"
        )
    return scan


# --- Acquiring from this machine ---

_import_lock = asyncio.Lock()


async def import_from_machine(config_dir: Path | None = None) -> dict[str, Any]:
    """Import (or re-import) the operator's plugins and skills; returns the catalog view with the report."""
    from ..db import library as db_library
    from ..db import system_settings

    async with _import_lock:
        scan = await asyncio.to_thread(scan_machine, config_dir)
        # An uploaded, git or Settings item may hold the name already: it stays, and this one is reported.
        held = {(row["kind"], row["name"]): row["origin"] for row in await db_library.list_items()
                if row["owner_project_id"] is None and row["origin"] != "machine"}
        for item in [item for item in scan.items if (item.kind.value, item.name) in held]:
            scan.items.remove(item)
            scan.skipped.append(Skipped(item.name, "", f"the catalog has a {item.kind.value} of this name from "
                                                       f"another source ({held[(item.kind.value, item.name)]})"))
        changes = await db_library.replace_snapshot([item.row() for item in scan.items])
        report = {
            "imported_at": datetime.now(UTC).isoformat(),
            "source": _label(config_dir or claude_config_dir()),
            "plugins": sum(item.kind is ItemKind.PLUGIN for item in scan.items),
            "skills": sum(item.kind is ItemKind.SKILL for item in scan.items),
            **changes,
            "skipped": [s.view() for s in scan.skipped[:_REPORT_SKIPPED_MAX]],
            "skipped_count": len(scan.skipped),
        }
        await system_settings.put_settings({LAST_IMPORT_KEY: json.dumps(report, ensure_ascii=False)})
    logger.info("library import: %s", {k: v for k, v in report.items() if k != "skipped"})
    from .catalog import catalog_view

    return await catalog_view()


# --- Loading the library for an agent run ---


@dataclass(frozen=True)
class LoadedItem:
    kind: ItemKind
    name: str
    sha256: str
    # The plugin it loads in: a plugin's own directory name, or the generated plugin of a skill; '' for an MCP
    # server, which loads from the run's --mcp-config file.
    plugin: str = SKILLS_PLUGIN
    scope: str = "global"  # where it is installed for the run: "global", or "project" when its project has it

    def view(self) -> dict[str, Any]:
        return {"kind": self.kind.value, "name": self.name, "sha256": self.sha256,
                **({"plugin": self.plugin} if self.plugin else {}), "scope": self.scope}


def items_label(items: Sequence[LoadedItem]) -> str:
    """``1 plugin, 2 skills, 1 MCP server``; a kind with none is left out (``0 skills`` for nothing)."""
    counts = [(sum(item.kind is kind for item in items), word)
              for kind, word in ((ItemKind.PLUGIN, "plugin"), (ItemKind.SKILL, "skill"), (ItemKind.MCP, "MCP server"))]
    return ", ".join(f"{n} {word}{'' if n == 1 else 's'}" for n, word in counts if n) or "0 skills"


def library_sha256(items: Sequence[LoadedItem]) -> str | None:
    """One hash of the items an agent loads and the plugins they load in; None for none."""
    if not items:
        return None
    listing = sorted([item.kind.value, item.plugin, item.name, item.sha256] for item in items)
    return _sha256(json.dumps(listing, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


@dataclass(frozen=True)
class AgentLibrary:
    """What one agent run loads from the catalog: the items installed for it and the plugin directories naming
    them."""

    items: tuple[LoadedItem, ...] = ()
    plugin_dirs: tuple[str, ...] = ()  # each one --plugin-dir, absolute
    mcp_config: str | None = None  # the --mcp-config file of its MCP servers, absolute; None without any
    error: str | None = None  # why the run goes without its installed items
    role: str | None = None
    project_id: str | None = None

    @property
    def sha256(self) -> str | None:
        return library_sha256(self.items)

    def record(self) -> dict[str, Any] | None:
        """Its entry in a run's metadata (``library``); None when nothing was to be loaded."""
        if not self.items and self.error is None:
            return None
        return {
            "sha256": self.sha256,
            "role": self.role,
            "project_id": self.project_id,
            "items": [item.view() for item in self.items],
            **({"mcp_servers": servers} if (servers := [i.name for i in self.items if i.kind is ItemKind.MCP])
               else {}),
            **({"error": self.error} if self.error is not None else {}),
        }


def _library_dir(runtime_root: Path) -> Path:
    return runtime_root.absolute() / ".local" / "library"


def item_directory(runtime_root: Path, item: LoadedItem) -> Path:
    """Where an item is materialized: ``<runtime_root>/.local/library/<sha256>/<name>``."""
    return _library_dir(runtime_root) / item.sha256 / directory_name(item.kind, item.name)


def _relative_path(raw: str) -> PurePosixPath:
    path = PurePosixPath(raw)
    if not raw or path.is_absolute() or any(part in ("", ".", "..") for part in raw.split("/")):
        raise LibraryError(f"a library file has an unusable path: {raw!r}")
    return path


def _make_writable(path: Path) -> None:
    for directory, _, _ in os.walk(path):
        os.chmod(directory, 0o755)


def write_once(target: Path, build: Callable[[Path], None]) -> None:
    """Make ``target`` a read-only directory filled by ``build(directory)``, unless it exists.

    It is built in a temporary directory beside it and renamed into place, so
    a reader never sees a partial one; when another run made it first, that
    one stays. Its directories end up 0555; ``build`` sets its files' modes.
    The catalog's items and the generated skill plugins are written this
    way, each under its content's hash.
    """
    if target.is_dir():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    building: Path | None = Path(tempfile.mkdtemp(prefix=f".{target.name[:16]}.", dir=target.parent))
    try:
        build(building)
        for directory, subdirectories, _ in os.walk(building, topdown=False):
            if Path(directory) != building:
                os.chmod(directory, 0o555)
        try:
            os.rename(building, target)
        except OSError:
            if not target.is_dir():
                raise
        else:
            building = None
            os.chmod(target, 0o555)
    finally:
        if building is not None:
            _make_writable(building)
            shutil.rmtree(building, ignore_errors=True)


def materialize_item(runtime_root: Path, item: LoadedItem, files: Sequence[Mapping[str, Any]]) -> Path:
    """Write an item's ``files`` (as stored) read-only under its hash; returns its directory.

    Raises ``LibraryError`` when the files do not match the item's hash.
    """
    for f in files:
        if _sha256(str(f["content"]).encode("utf-8")) != f["sha256"]:
            raise LibraryError(f"the files of {item.kind.value} {item.name} do not match their hashes")
    if item_sha256(files) != item.sha256:
        raise LibraryError(f"the files of {item.kind.value} {item.name} do not match its version {item.sha256[:12]}")
    paths = [_relative_path(str(f["path"])) for f in files]
    name = directory_name(item.kind, item.name)

    def build(directory: Path) -> None:
        base = directory / name
        base.mkdir()
        for path, f in zip(paths, files):
            destination = base.joinpath(*path.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(str(f["content"]).encode("utf-8"))
            os.chmod(destination, 0o555 if f["executable"] else 0o444)

    target = _library_dir(runtime_root) / item.sha256
    write_once(target, build)
    return target / name


def skills_plugin(runtime_root: Path, skills: Sequence[LoadedItem], plugin: str = SKILLS_PLUGIN) -> Path:
    """The generated ``plugin`` holding ``skills``: a manifest and a link to each materialized skill.

    It is named by the plugin and its set of skills, so another set is another directory.
    """
    listing = sorted([item.name, item.sha256] for item in skills)
    key = _sha256(("skills-plugin\n" + json.dumps([plugin, listing], separators=(",", ":"))).encode("utf-8"))

    def build(directory: Path) -> None:
        base = directory / plugin
        (base / ".claude-plugin").mkdir(parents=True)
        (base / ".claude-plugin" / "plugin.json").write_text(json.dumps({
            "name": plugin, "description": _PLUGIN_DESCRIPTIONS.get(plugin, "Skills managed by LoopX."),
        }, indent=2) + "\n", encoding="utf-8")
        (base / "skills").mkdir()
        for name, sha in listing:
            os.symlink(f"../../../{sha}/{name}", base / "skills" / name)

    target = _library_dir(runtime_root) / key
    write_once(target, build)
    return target / plugin


EMPTY_LIBRARY = AgentLibrary()


def skill_plugin(origin: str) -> str:
    """The generated plugin a skill of ``origin`` loads in."""
    return PROJECT_SKILLS_PLUGIN if origin == "project" else SKILLS_PLUGIN


def resolve_installed(rows: Sequence[Mapping[str, Any]], project_id: str | None) -> list[tuple[Mapping[str, Any], LoadedItem]]:
    """The items a run loads from its install rows (``db.library.list_installed``), each once.

    An item installed globally and in the project loads once, as the
    project's. Two project-authored skills of one name would both be
    ``project:<name>``: the project's own one loads, else the first by owner
    (the API refuses such a pair; this only settles a race).
    """
    chosen: dict[str, tuple[Mapping[str, Any], LoadedItem]] = {}
    for row in rows:
        kind = ItemKind(row["kind"])
        plugin = (directory_name(kind, row["name"]) if kind is ItemKind.PLUGIN
                  else skill_plugin(row["origin"]) if kind is ItemKind.SKILL else "")
        scope = "project" if row["scope"] == "project" or row["id"] in chosen and \
            chosen[row["id"]][1].scope == "project" else "global"
        chosen[row["id"]] = (row, LoadedItem(kind, row["name"], row["sha256"], plugin, scope))
    loaded: dict[tuple[str, str, str], tuple[Mapping[str, Any], LoadedItem]] = {}
    for row, item in chosen.values():
        key = (item.kind.value, item.plugin, item.name)
        if item.kind is ItemKind.SKILL and key in loaded:
            other = loaded[key][0]
            if other.get("owner_project_id") == project_id or row.get("owner_project_id") != project_id:
                logger.warning("skill %s:%s of item %s is shadowed by item %s", item.plugin, item.name, row["id"],
                               other["id"])
                continue
            logger.warning("skill %s:%s of item %s is shadowed by item %s", item.plugin, item.name, other["id"],
                           row["id"])
        loaded[key] = (row, item)
    return list(loaded.values())


async def load_installed(runtime_root: Path, role: str, project_id: str | None = None) -> AgentLibrary:
    """The items installed for one run of ``role`` in ``project_id``, materialized under ``runtime_root``.

    They are the enabled installs naming the role, global or in the project
    (``resolve_installed``). An item is written from the database the first
    time a run needs it. The plugins come first, then ``personal`` and
    ``project``. The MCP servers go into one ``--mcp-config`` file
    (``mcp.write_config``). When the catalog cannot be read or written, the
    run goes without its items and its record says why.
    """
    from ..db import library as db_library

    project_id = project_id or None
    try:
        rows = await db_library.list_installed(role, project_id)
    except Exception:
        logger.warning("cannot read the installed items; the agent runs without them", exc_info=True)
        return AgentLibrary(error="the installed items could not be read", role=role, project_id=project_id)
    if not rows:
        return AgentLibrary(role=role, project_id=project_id)
    resolved = resolve_installed(rows, project_id)
    items = tuple(item for _, item in resolved)
    try:
        plugin_dirs: list[str] = []
        servers = [(row, item) for row, item in resolved if item.kind is ItemKind.MCP]
        mcp_config = await _mcp_config(runtime_root, servers) if servers else None
        for row, item in resolved:
            if item.kind is ItemKind.MCP:
                continue
            directory = item_directory(runtime_root, item)
            if not directory.is_dir():
                files = await db_library.get_item_files(row["id"])
                directory = await asyncio.to_thread(materialize_item, runtime_root, item, files)
            if item.kind is ItemKind.PLUGIN:
                plugin_dirs.append(str(directory))
        for plugin in (SKILLS_PLUGIN, PROJECT_SKILLS_PLUGIN):
            skills = [item for item in items if item.kind is ItemKind.SKILL and item.plugin == plugin]
            if skills:
                plugin_dirs.append(str(await asyncio.to_thread(skills_plugin, runtime_root, skills, plugin)))
    except Exception as exc:
        logger.warning("cannot prepare the installed items; the agent runs without them", exc_info=True)
        return AgentLibrary(error=f"the installed items could not be prepared: {exc}", role=role,
                            project_id=project_id)
    return AgentLibrary(items, tuple(plugin_dirs), mcp_config=mcp_config, role=role, project_id=project_id)


async def _mcp_config(runtime_root: Path, servers: Sequence[tuple[Mapping[str, Any], LoadedItem]]) -> str:
    """The ``--mcp-config`` file of the MCP servers a run loads, written if missing.

    Raises ``LibraryError`` when a server changed since its install was read.
    """
    from ..db import library as db_library
    from .mcp import Server, write_config

    stored = await db_library.get_mcp_servers([row["id"] for row, _ in servers])
    configs = []
    for row, item in servers:
        server = Server.from_row(stored[row["id"]]) if row["id"] in stored else None
        if server is None or server.sha256 != item.sha256:
            raise LibraryError(f"MCP server {item.name} changed while the run was being prepared")
        configs.append((item.name, server.config()))
    return str(await asyncio.to_thread(write_config, runtime_root, configs))
