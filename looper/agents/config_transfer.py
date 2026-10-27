"""Configuration export and import: one file that recreates an installation's agent configuration (M8e).

**The file** is one JSON document, ``looper-config/v4`` when it includes a
portable Manager graph, otherwise the unchanged ``looper-config/v3`` shape. A v1 file
(``looper-config/v1``, or ``loopx-config/v1`` from before the rename to Looper)
is read too, as the same file under its older names (``_from_v1``): its Agent
Profiles were ``agents``, and a layer named its profile ``agent``. V1 is first
renamed to v2; v2 registry objects then use the same pure converter as the
database migration, before v3 natural-key matching. Every catalog
file is UTF-8 text (acquiring an item refuses binary files), so JSON holds it
as it is, and one readable file can be checked before it is shared. Its
sections:

- ``runtimes``, ``providers``, ``models`` and ``profiles``: the registered ones (the
  built-ins exist everywhere). Models name their provider; profiles reference a
  model by provider name, exact model name and protocol. Providers carry their
  protocol endpoints, without a model catalog. The runtimes
  are the ones registered by hand before runtimes were built in, which
  profiles still use; an import never adds or changes a runtime (see below);
- ``settings``: the system and role layers of the agent settings (a layer
  names its Agent Profile, ``profile``), and each role's prompt (``null`` while
  it follows the recommended one), timeout and enabled flag. A role's
  ``max_concurrency`` from before the layers held it is ignored, with a note,
  and so is a layer's from before concurrency was per task (any but the
  Workers' or the Reviewers' role layer: a project's, a role's in a project or
  the Planner's); a layer's ``effort`` from before the effort was the Agent
  Profile's moves onto profiles as the upgrade moved it (``layer_effort``; see
  **Import**);
- ``channels``: each chat channel's configuration;
- ``managers`` (v4 only): live scenario definitions and task templates, with
  immutable Manager ids, natural profile/project references and no machine paths;
- ``installs``: the global installs, naming their item by kind and name;
- ``projects`` (``--include-projects``): each project's fields, execution
  settings, layers, knowledge, the items it wrote, its installs and its
  ``schedules`` (Manager tools RFC, section 7.1; see **Schedules**);
- ``catalog``: every item outside projects with its files, or an MCP
  server's configuration.

**Secrets** are an MCP server's environment and header values, a provider's
API key and a channel config's keys other than its known public ones
(``_CHANNEL_PUBLIC_KEYS``). Unless the export includes them, each is
``PLACEHOLDER``. Channel credentials in ``channel_secrets`` never travel,
even with secrets included. Local chat bindings and policy never travel.
**Machine paths** stay out: a project's directory is not
exported, an item's source or a runtime's executable that is a local path is
``null``, and a path document is a reference (relative to its project's
directory) flagged ``repoint``.

**Import** matches entries by natural key (an item by kind and name, a
project-written one within its project; a project, runtime, provider or
profile by name; a layer by scope, role and project; a role by name; a channel
by its immutable channel_id), and reports each as
added, changed (with the fields), unchanged or skipped (with the reason),
plus the secrets still to fill in. Applying writes the same plan in one
transaction: an entry with the same key is updated in place (an item keeps
its id and installs), nothing missing from the file is removed, and a
placeholder never replaces a stored secret (without one, the value is left
empty). Importing the same file again changes nothing.

**A layer's effort** in an older file is converted like the upgrade converts
a database (``layer_effort.convert``), over the layers as the import leaves
them (this installation's, each replaced by the file's of the same key): every
role then runs the profile and effort it would have run with the file's
layers, through the profiles they select, added as needed (``<profile> ·
<effort>``). A layer this changes that the file does not give, such as a
role's in a project, is changed too; a note says each.

**Schedules** carry their settings, their Agent Profiles by name and their
task limits, not whether they are enabled, their runs or counters, nor their
own directory (a machine path: ``repoint`` marks one that had it). An import
creates a schedule it adds, and rewrites one whose settings differ, disabled
(enabling is the approval, scheduled-tasks RFC D6), with a note; one whose
settings match is left as it is. A ``notify_target`` this machine's sender
allowlists (as the import leaves the channels) do not allow is dropped, with a
note.

**Runtimes** are built in, so a file's runtime only says what its profiles run
on here: the runtime of its name when one of its type is here, else the
built-in runtime of its type (a note says so), else none (the entry and its
profiles are skipped, with the reason).
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

from ..db.channel_configs import BUILTIN_CHANNEL_IDS
from .message import builtin_channel_id

FORMAT = "looper-config/v3"
V3_FORMAT = FORMAT
V4_FORMAT = "looper-config/v4"
# v1, and v1 under its name from before the rename to Looper (LoopX): read as v2 (``_from_v1``), never written.
V2_FORMAT = "looper-config/v2"
V1_FORMATS = ("looper-config/v1", "loopx-config/v1")
# What the file holds in place of a secret value it leaves out.
PLACEHOLDER = {"secret": "omitted"}

# The channel config keys that are not secrets: the sender allowlist (Lark's with the app it was made for), the
# name of the environment variable Telegram may read its token from, Lark's lark-cli profile, and the description.
# Any other key is treated as a secret.
_CHANNEL_PUBLIC_KEYS = {
    "telegram": frozenset({"allowed_user_ids", "bot_token_env", "description"}),
    "lark": frozenset({"allowed_open_ids", "allowlist_app_id", "profile", "description"}),
    "lark_bot": frozenset({"allowed_open_ids", "allowlist_app_id", "app_id", "domain", "description"}),
}

# A channel whose credential is a DB-stored secret (channel_secrets), not a config key: it is not
# carried in the file, so an import of an enabled such channel still needs it set on this machine.
_CHANNEL_TOKEN_SECRET = {"telegram": "bot token (Settings → Channels)",
                         "lark_bot": "app secret (Settings → Channels)"}
_ROLE_FIELDS = ("prompt", "timeout_seconds", "enabled")
_LAYER_FIELDS = ("harness_prompt", "profile", "env", "run_budget_usd", "max_concurrency", "boundaries",
                 "system_prompt_mode")
# The roles whose role layer takes a concurrency limit: the default for every task.
_LIMIT_ROLES = ("worker", "reviewer")
_PROVIDER_FIELDS = ("protocols", "auth", "oauth_runtime_type", "base_url", "models", "env", "max_concurrency")
_ITEM_FIELDS = ("origin", "source", "version", "description", "modified")
_DOCUMENT_FIELDS = ("summary", "source_kind", "filename", "path", "sha256", "size_bytes")
_ITEM_NAME_MAX = 255
_PATH_MAX = 1024
_PROMPT_MAX_CHARS = 100_000
_TIMEOUT_MAX_SECONDS = 365 * 86400


class ConfigFileError(ValueError):
    """A file the import cannot use at all; nothing is written."""


class ConfigFileConflict(ConfigFileError):
    """A valid file conflicts with immutable destination identities or past allocations."""


class SchemaChangePending(RuntimeError):
    """The database's schema is older than this Looper; ``looper serve`` backs it up, then changes it."""


class Action(str, Enum):
    ADDED = "added"
    CHANGED = "changed"
    UNCHANGED = "unchanged"
    SKIPPED = "skipped"


SECTIONS = ("runtimes", "providers", "models", "profiles", "settings.layers", "settings.roles", "settings.scheduler", "channels",
            "catalog", "installs",
            "projects", "project.layers", "project.items", "project.knowledge", "project.role_templates",
            "project.schedules", "project.installs", "managers", "manager.templates", "manager.installs")
# A schedule's settings the file carries as they are; its notify target, Agent Profiles and task limits are
# ``notify_target``, ``profiles`` (by name) and ``task_limits``.
_SCHEDULE_FIELDS = ("name", "cron", "timezone", "requirement", "overlap", "max_parallel_runs", "catch_up",
                    "run_timeout_seconds", "run_budget_usd", "gate_command", "gate_timeout_seconds", "daily_budget_usd",
                    "notify")


@dataclass(frozen=True)
class Change:
    section: str
    key: str
    action: Action
    fields: tuple[str, ...] = ()
    reason: str = ""
    code: str = ""


@dataclass(frozen=True)
class NeededSecret:
    """Secrets an entry still needs on this machine. ``target`` is what holds them here, for a link to its form:
    ``("provider" | "mcp" | "channel", id)``. New channels already have immutable ids in a preview; other new
    entries have ``None`` until stored."""

    section: str
    key: str
    names: tuple[str, ...]
    target: tuple[str, str] | None = None


def _stored(kind: str, row_id: str | None) -> tuple[str, str] | None:
    return None if row_id is None or row_id.startswith("new:") else (kind, row_id)


@dataclass
class Plan:
    """What an import does (or did): each entry's change, the secrets left to fill in, and notes."""

    changes: list[Change] = field(default_factory=list)
    need_secret: list[NeededSecret] = field(default_factory=list)
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

    def view(self, *, targets: bool = False) -> dict[str, Any]:
        """The plan as JSON; ``targets`` adds each needed secret's ``target`` (``{"kind", "id"}``) where known."""
        return {
            "applied": self.applied,
            "counts": self.counts(),
            "changes": [{"section": c.section, "key": c.key, "action": c.action.value,
                         **({"fields": list(c.fields)} if c.fields else {}),
                         **({"reason": c.reason} if c.reason else {}),
                         **({"code": c.code} if c.code else {})}
                        for c in self.changes if c.action is not Action.UNCHANGED],
            "need_secret": [{"section": n.section, "key": n.key, "names": list(n.names),
                             **({"target": {"kind": n.target[0], "id": n.target[1]}}
                                if n.target and (targets or n.target[0] == "channel"
                                                 and n.target[1] not in BUILTIN_CHANNEL_IDS) else {})}
                            for n in self.need_secret],
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


def runtime_entry(row: Mapping[str, Any]) -> dict[str, Any]:
    return {"name": row["name"], "type": row["type"], "executable": _portable_source(row["executable"])
            if row["executable"] else ""}


def provider_entry(row: Mapping[str, Any], include_secrets: bool) -> dict[str, Any]:
    endpoints = {
        endpoint["protocol"]: {"base_url": endpoint["base_url"],
                               **({"use_runtime_default": True} if endpoint["use_runtime_default"] else {})}
        for endpoint in row["endpoints"]
    }
    return {**{name: row[name] for name in ("name", "vendor_type", "auth", "oauth_runtime_type", "env",
                                          "max_concurrency")}, "endpoints": endpoints,
            "api_key": None if row["api_key"] is None else _secret(row["api_key"], include_secrets)}


def model_entry(row: Mapping[str, Any]) -> dict[str, Any]:
    from .profile_registry import MODEL_FIELDS

    return {"provider": row["provider"], "protocol": row["protocol"],
            **{name: row[name] for name in MODEL_FIELDS}, "catalog_order": row["catalog_order"]}


def profile_entry(row: Mapping[str, Any]) -> dict[str, Any]:
    return {**{name: row[name] for name in ("name", "runtime", "effort", "agent_teams")},
            "model": {"provider": row["provider"], "name": row["model"], "protocol": row["protocol"]},
            "custom_prompt": row.get("custom_prompt", "")}


def layer_entry(row: Mapping[str, Any]) -> dict[str, Any]:
    entry = {"scope": row["scope"]}
    if row.get("role"):
        entry["role"] = row["role"]
    entry = {**entry, **{name: row.get(name) for name in _LAYER_FIELDS}, "env": list(row.get("env") or [])}
    # Only the shared defaults may hold boundaries, and only the Workers' and the Reviewers' role layers a
    # concurrency limit, and only role layers a system prompt mode; a file without one has none.
    for name in ("boundaries", "max_concurrency", "system_prompt_mode"):
        if entry[name] is None:
            del entry[name]
    return entry


def role_entry(row: Mapping[str, Any]) -> dict[str, Any]:
    prompt = row["system_prompt"]
    edited = prompt not in ("", row["default_prompt"])
    return {"role": row["role"], "prompt": prompt if edited else None,
            "timeout_seconds": row["timeout_seconds"], "enabled": row["enabled"]}


def _secret(value: Any, include_secrets: bool) -> Any:
    return value if include_secrets else dict(PLACEHOLDER)


def channel_entry(row: Mapping[str, Any], include_secrets: bool, *, portable_graph: bool = False) -> dict[str, Any]:
    from .channels.allowlist import AllowlistError, normalized_config

    public = _CHANNEL_PUBLIC_KEYS.get(row["channel_type"], frozenset())
    config = dict(row["config"] or {})
    try:  # the allowlist as the API stores it (an older row may hold a comma-separated string)
        config = normalized_config(row["channel_type"], config)
    except AllowlistError:
        pass
    config = {key: value if key in public else _secret(value, include_secrets) for key, value in config.items()}
    return {**({"manager_id": row.get("manager_id", "manager")} if portable_graph else {}),
            "channel_type": row["channel_type"], "channel_id": row["channel_id"],
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


def role_template_entry(row: Mapping[str, Any], items: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """A template, its profile by name and its items (``items``, the catalog's rows by id) by reference."""
    entry = {"key": row["template_key"], "kind": row["kind"], "description": row["description"],
             "role_prompt": row["role_prompt"]}
    if row["harness"]:
        entry["harness"] = row["harness"]
    if row["profile"]:
        entry["profile"] = row["profile"]
    named = [items[item_id] for item_id in row.get("items") or () if item_id in items]
    if named:
        entry["items"] = [_item_ref(item["kind"], item["name"], item["owner_project_name"]) for item in named]
    return entry


def manager_template_entry(row: Mapping[str, Any], profiles: Mapping[str, str],
                           projects: Mapping[str, str]) -> dict[str, Any]:
    """A portable task template, with natural references and an explicit local-directory reminder."""
    entry = {name: row[name] for name in ("key", "name", "description", "workspace_mode", "requirement",
                                         "planner_instructions", "role_templates", "inputs")}
    entry["project"] = projects[row["project_id"]] if row["project_id"] else None
    entry["profiles"] = {role: profiles[row[f"{role}_profile_id"]] for role in ("planner", "worker", "reviewer")
                         if row[f"{role}_profile_id"]}
    if row["work_dir"]:
        entry["repoint"] = True
    return entry


def manager_entry(row: Mapping[str, Any], templates: Sequence[Mapping[str, Any]],
                  profiles: Mapping[str, str], projects: Mapping[str, str],
                  installs: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    entry = {name: row[name] for name in ("id", "name", "description")}
    entry["idle_timeout_seconds"] = row.get("idle_timeout_seconds", 0 if row["id"] == "manager" else 1800)
    extras = [{"item": {"kind": s["kind"], "name": s["name"]}, "enabled": s["enabled"]}
              for s in installs if s["manager_id"] == row["id"]]
    if extras:
        entry["installs"] = extras
    if row["id"] != "manager":
        entry.update({name: row[name] for name in ("system_prompt", "tools", "task_scope", "task_policy", "enabled")})
        entry["profile"] = profiles[row["profile_id"]] if row["profile_id"] else None
        entry["templates"] = [manager_template_entry(t, profiles, projects) for t in templates
                              if t["manager_id"] == row["id"]]
    return entry


def document_entry(row: Mapping[str, Any]) -> dict[str, Any]:
    entry = {"title": row["title"], "summary": row["summary"], "source_kind": row["source_kind"],
             "filename": row["filename"], "size_bytes": row["size_bytes"], "sha256": row["sha256"]}
    if row["source_kind"] == "path":
        # A file in the project's directory, read live: on another machine it is wherever that directory is.
        return {**entry, "path": row["path"], "repoint": True}
    return {**entry, "content": row["content"]}


def schedule_timeout(row: Mapping[str, Any]) -> Any:
    """A schedule's run timeout as the file holds it: ``"default"`` (the system default), seconds, or null."""
    from .schedules import TIMEOUT_DEFAULT

    if row["run_timeout_mode"] == "inherit":
        return TIMEOUT_DEFAULT
    return row["run_timeout_seconds"] if row["run_timeout_mode"] == "seconds" else None


async def stored_scheduler_settings(conn: Any) -> dict[str, int]:
    """The scheduler's system settings that are stored here (a setting left at its built-in is not carried, so
    the machine that imports keeps its own)."""
    from ..db import system_settings as db_settings
    from . import scheduler_settings

    keys = {"min_interval_seconds": scheduler_settings.MIN_INTERVAL_KEY,
            "run_timeout_seconds": scheduler_settings.RUN_TIMEOUT_KEY}
    if not await conn.fetchval("SELECT to_regclass('system_settings') IS NOT NULL"):
        return {}  # a database not set up yet: a preview before the first start
    stored = await db_settings.get_settings(list(keys.values()), pool=conn)
    return {name: int(stored[key]) for name, key in keys.items() if key in stored}


def schedule_entry(row: Mapping[str, Any], profile_names: Mapping[str, str]) -> dict[str, Any]:
    """A schedule as the file holds it: no enabled flag, runs or counters, and no directory (``repoint`` when it had
    one of its own)."""
    entry = {name: row[name] for name in _SCHEDULE_FIELDS}
    entry["run_timeout_seconds"] = schedule_timeout(row)
    entry["notify_target"] = ({"channel": row["notify_channel"], "chat": row["notify_chat"]}
                              if row["notify_channel"] else None)
    entry["profiles"] = {role: profile_names.get(pid, pid) for role, pid in sorted(row["profile_overrides"].items())}
    entry["task_limits"] = dict(sorted(row["task_limits"].items()))
    if row.get("template_key"):
        entry["template_key"] = row["template_key"]
        entry["template_inputs"] = dict(row.get("template_inputs") or {})
    if row["manager_id"] != "manager":
        entry["manager"] = row["manager_id"]
    if row["work_dir"]:
        entry["repoint"] = True
    return entry


@dataclass
class ExportSummary:
    counts: dict[str, int] = field(default_factory=dict)
    secrets: int = 0  # the secret values written, or left out as placeholders
    repoint: int = 0
    bytes: int = 0
    bots_left_out: int = 0  # retained for callers of the older summary; all bots now travel
    format: str = V3_FORMAT


def _secret_count(entry: Mapping[str, Any]) -> int:
    """How many secret values a channel, MCP or provider entry holds, or stands in for."""
    if "api_key" in entry:
        return int(entry["api_key"] is not None)
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
    channel_rows = await db.read_channels(conn)
    managers = await db.read_managers(conn)
    manager_installs = await db.read_manager_installs(conn)
    portable_graph = any(row["id"] != "manager" for row in managers) or any(
        row["channel_id"] not in BUILTIN_CHANNEL_IDS for row in channel_rows) or bool(manager_installs)
    channels = [channel_entry(row, include_secrets, portable_graph=portable_graph) for row in channel_rows]
    summary.format = V4_FORMAT if portable_graph else V3_FORMAT
    providers = [provider_entry(r, include_secrets) for r in await db.read_providers(conn) if not r["builtin"]]
    head = {
        "format": summary.format,
        "looper_version": __version__,
        "exported_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "secrets": "included" if include_secrets else "omitted",
        "runtimes": [runtime_entry(r) for r in await db.read_runtimes(conn) if not r["builtin"]],
        "providers": providers,
        "models": [model_entry(r) for r in await db.read_models(conn) if not r["builtin"]],
        "profiles": [profile_entry(r) for r in await db.read_profiles(conn) if not r["builtin"]],
        "settings": {"layers": [layer_entry(r) for r in layers if r["project_id"] is None],
                     "roles": [role_entry(r) for r in await db.read_roles(conn)]},
        "channels": channels,
        "installs": [install_entry(r) for r in installs if r["scope"] == "global"],
    }
    if portable_graph:
        manager_templates = await db.read_manager_templates(conn)
        profile_names = {row["id"]: row["name"] for row in await db.read_profiles(conn)}
        project_names = {row["id"]: row["name"] for row in await db.read_projects(conn)}
        head["managers"] = [manager_entry(row, manager_templates, profile_names, project_names, manager_installs)
                            for row in managers]
        summary.counts["managers"] = len(managers)
        summary.counts["manager.templates"] = len(manager_templates)
        summary.counts["manager.installs"] = len(manager_installs)
    if scheduler := await stored_scheduler_settings(conn):  # only a stored setting is carried
        head["settings"]["scheduler"] = scheduler
    summary.counts.update({"runtimes": len(head["runtimes"]), "providers": len(providers),
                           "models": len(head["models"]), "profiles": len(head["profiles"]),
                           "settings.layers": len(head["settings"]["layers"]),
                           "settings.roles": len(head["settings"]["roles"]),
                           "settings.scheduler": len(scheduler), "channels": len(channels),
                           "installs": len(head["installs"])})
    summary.secrets += sum(_secret_count(entry) for entry in [*channels, *providers])
    yield "{" + ",\n".join(f"{_dump(k)}:{_dump(v)}" for k, v in head.items())

    if include_projects:
        from ..db.schedules import list_schedules

        documents, templates = defaultdict(list), defaultdict(list)
        for row in await db.read_documents(conn, content=True):
            documents[row["project_id"]].append(row)
        for row in await db.read_role_templates(conn):
            templates[row["project_id"]].append(row)
        schedules = defaultdict(list)
        for row in await list_schedules(pool=conn):
            schedules[row["project_id"]].append(row)
        profile_names = {row["id"]: row["name"] for row in await db.read_profiles(conn)}
        projects = await db.read_projects(conn)
        summary.counts["projects"] = len(projects)
        summary.counts["schedules"] = sum(len(rows) for rows in schedules.values())
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
            if templates[project["id"]]:
                # Only a project that has templates names them, so a file of a project without is as before.
                by_id = {row["id"]: row for row in items}
                entry["role_templates"] = [role_template_entry(r, by_id) for r in templates[project["id"]]]
            if schedules[project["id"]]:  # likewise its schedules
                entry["schedules"] = [schedule_entry(r, profile_names) for r in schedules[project["id"]]]
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
    conn = await _read_only(dsn)
    try:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            return await write_export(conn, path, include_projects=include_projects, include_secrets=include_secrets)
    finally:
        await conn.close()


async def write_export(conn: Any, path: Path, *, include_projects: bool, include_secrets: bool) -> ExportSummary:
    """Write the configuration file from ``conn`` (in a read-only, repeatable-read transaction) to ``path``, whole or
    not at all; a file without secrets is readable as the umask allows, one with them by its owner only."""
    summary = ExportSummary()
    path = path.expanduser()
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent.resolve(), prefix=f".{path.name}.",
                                         delete=False)
    try:
        if not include_secrets:
            os.chmod(handle.name, 0o644 & ~_umask())
        async for chunk in export_chunks(conn, include_projects=include_projects, include_secrets=include_secrets,
                                         summary=summary):
            handle.write(chunk)
        handle.close()
        os.replace(handle.name, path)
    except BaseException:
        handle.close()
        Path(handle.name).unlink(missing_ok=True)
        raise
    summary.bytes = path.stat().st_size
    return summary


def _umask() -> int:
    mask = os.umask(0)
    os.umask(mask)
    return mask


def read_file(path: Path, *, label: str | None = None) -> dict[str, Any]:
    """The configuration file at ``path``; raises ``ConfigFileError`` when it is not one. Errors name the file
    ``label`` (default: its path)."""
    label = label or str(path)
    try:
        with path.expanduser().open(encoding="utf-8") as handle:
            data = json.load(handle)
    except OSError as exc:
        raise ConfigFileError(f"cannot read {label}: {exc.strerror or exc}") from None
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ConfigFileError(f"{label} is not a Looper configuration file: {exc}") from None
    if not isinstance(data, dict) or data.get("format") not in (V4_FORMAT, V3_FORMAT, V2_FORMAT, *V1_FORMATS):
        found = data.get("format") if isinstance(data, dict) else None
        raise ConfigFileError(f"{label} is not a {V4_FORMAT}, {V3_FORMAT}, {V2_FORMAT} or {V1_FORMATS[0]} file "
                              f"(format: {found!r})")
    _check_manager_document(data)
    profiles = "agents" if data["format"] in V1_FORMATS else "profiles"
    for section, kind in (("settings", dict), ("channels", list), ("installs", list), ("catalog", list),
                          ("projects", list), ("runtimes", list), ("providers", list), (profiles, list), ("models", list)):
        if section in data and not isinstance(data[section], kind):
            raise ConfigFileError(f"{section} must be a JSON {'object' if kind is dict else 'list'}")
    for section in ("layers", "roles"):
        if not isinstance(data.get("settings", {}).get(section, []), list):
            raise ConfigFileError(f"settings.{section} must be a JSON list")
    if not isinstance(data.get("settings", {}).get("scheduler", {}), dict):
        raise ConfigFileError("settings.scheduler must be a JSON object")
    _check_channel_document(data)
    return _from_v1(data) if data["format"] in V1_FORMATS else data


def _check_manager_document(data: Mapping[str, Any]) -> None:
    """Version-gate the new graph even for callers that do not use the HTTP schema."""
    if data.get("format") != V4_FORMAT:
        if "managers" in data:
            raise ConfigFileError("managers requires looper-config/v4; legacy files cannot carry a Manager graph")
        return
    if not isinstance(data.get("managers", []), list):
        raise ConfigFileError("managers must be a JSON list")


_BOT_ID = re.compile(r"channel-(telegram|lark|lark_bot)-([1-9][0-9]*)\Z")
_BINDING_FIELDS = frozenset({"manager_chat_bindings", "channel_seen_chats", "channel_unbound_chats"})
_BOT_FIELDS = frozenset({"channel_id", "channel_type", "manager_id", "display_name", "enabled", "config"})


def _bot_suffix(channel_id: Any, kind: Any) -> int | None:
    if not isinstance(channel_id, str) or not isinstance(kind, str) or kind not in _CHANNEL_PUBLIC_KEYS:
        raise ConfigFileError("channels: invalid bot id or channel type")
    if channel_id == builtin_channel_id(kind):
        return None
    match = _BOT_ID.fullmatch(channel_id)
    if not match or match[1] != kind or len(match[2]) > 19 or int(match[2]) >= 2**63 - 1:
        raise ConfigFileError("channels: malformed bot id or id/type mismatch")
    return int(match[2])


def _check_channel_document(data: Mapping[str, Any]) -> None:
    """Reject identity ambiguity and nonportable state before importing any of the graph."""
    if _BINDING_FIELDS & data.keys():
        raise ConfigFileError("chat bindings, seen chats and unbound chats are local state and cannot be imported")
    entries = data.get("channels", [])
    if not isinstance(entries, list):
        raise ConfigFileError("channels must be a JSON list")
    graph = data.get("format") == V4_FORMAT
    seen, suffixes = set(), set()
    for entry in entries:
        if not isinstance(entry, Mapping):
            if graph:
                raise ConfigFileError("channels: a bot must be a JSON object")
            continue
        config = entry.get("config", {})
        if _BINDING_FIELDS & entry.keys() or isinstance(config, Mapping) and (
                (_BINDING_FIELDS | {"bound_chats_only"}) & config.keys()):
            raise ConfigFileError("channels: binding and policy fields are local configuration and cannot be imported")
        identity, kind = entry.get("channel_id"), entry.get("channel_type")
        if not graph:
            if (identity is not None and identity not in BUILTIN_CHANNEL_IDS) or entry.get("manager_id", "manager") != "manager":
                raise ConfigFileError("channels: this is a bot of a Manager and requires looper-config/v4")
            continue
        required = {"channel_id", "channel_type", "config"}
        if not isinstance(identity, str) or identity not in BUILTIN_CHANNEL_IDS:
            required.add("manager_id")
        if set(entry) - _BOT_FIELDS or not required <= entry.keys():
            raise ConfigFileError("channels: BotConfig has unknown or missing fields; enter credentials only in Settings → Channels")
        suffix = _bot_suffix(identity, kind)
        owner = entry.get("manager_id", "manager")
        if not isinstance(owner, str) or not owner:
            raise ConfigFileError("channels: manager_id must reference a Manager")
        if suffix is None and owner != "manager":
            raise ConfigFileError("channels: the built-in bot's immutable owner must be manager")
        if identity in seen or suffix is not None and suffix in suffixes:
            raise ConfigFileError("channels: duplicate bot ids or numeric suffixes in the file")
        seen.add(identity)
        if suffix is not None:
            suffixes.add(suffix)


def _from_v1(data: Mapping[str, Any]) -> dict[str, Any]:
    """A v1 file under v2's names (its ``format`` stays the file's): its ``agents`` are ``profiles``, and each
    layer's ``agent`` (the system's and roles', and each project's) its ``profile``. Nothing else differs."""
    def layers(entries: Any) -> Any:
        if not isinstance(entries, list):
            return entries
        return [{("profile" if key == "agent" else key): value for key, value in entry.items()}
                if isinstance(entry, Mapping) else entry for entry in entries]

    upgraded = {("profiles" if key == "agents" else key): value for key, value in data.items()}
    if "layers" in upgraded.get("settings", {}):
        upgraded["settings"] = {**upgraded["settings"], "layers": layers(upgraded["settings"]["layers"])}
    if isinstance(upgraded.get("projects"), list):
        upgraded["projects"] = [{**project, "layers": layers(project["layers"])}
                                if isinstance(project, Mapping) and "layers" in project else project
                                for project in upgraded["projects"]]
    return upgraded


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


def _layer_values(entry: Mapping[str, Any], scope: str, role: str | None) -> tuple[dict[str, Any], str | None]:
    """A layer's values, its Agent Profile by name (``None`` for none), and the effort a file from before the effort
    was the Agent Profile's gives it (``None`` for none). A file from before Agent Profiles gives a ``model``: that
    is the profile ``claude-code/<model>``, as the upgrade made it."""
    from .agent_settings import AgentType, Layer, SettingsError, effort_level, layer_update, model_name

    unknown = sorted(set(entry) - {"scope", "role", "model", "effort", *_LAYER_FIELDS})
    if unknown:
        raise _Skip(f"unknown field {unknown[0]!r}")
    profile = entry.get("profile")
    try:
        values = layer_update({name: entry.get(name) for name in _LAYER_FIELDS if name != "profile"},
                              Layer(scope), AgentType(role) if role else None)
        effort = effort_level(entry.get("effort")) or None
        if "model" in entry and "profile" not in entry:
            model = model_name(entry["model"])
            profile = f"{_MODEL_PROFILE_PREFIX}{model}" if model else None
    except SettingsError as exc:
        raise _Skip(str(exc)) from None
    if profile is not None and not isinstance(profile, str):
        raise _Skip("profile must be an Agent Profile's name, or null")
    return {**values, "profile": profile or None}, effort


# The name the upgrade gave the Agent Profile of each model layers used (Claude Code, its own login, the model).
_MODEL_PROFILE_PREFIX = "claude-code/"


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
    role_templates: dict[str, dict[str, dict[str, Any]]] = field(default_factory=lambda: defaultdict(dict))
    runtimes: dict[str, dict[str, Any]] = field(default_factory=dict)  # by name
    providers: dict[str, dict[str, Any]] = field(default_factory=dict)
    profiles: dict[str, dict[str, Any]] = field(default_factory=dict)
    models: dict[tuple[str, str, str], dict[str, Any]] = field(default_factory=dict)
    schedules: dict[str, list[dict[str, Any]]] = field(default_factory=lambda: defaultdict(list))  # by project id
    managers: dict[str, dict[str, Any]] = field(default_factory=dict)  # immutable full id, including tombstones
    manager_templates: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    manager_installs: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    bot_sequence: tuple[int, bool] = (1, False)

    @classmethod
    async def read(cls, conn: Any) -> _Target:
        from ..db import config_transfer as db

        target = cls()
        if not await db.has_schema(conn):
            return target
        for row in await db.read_layers(conn):
            target.layers[(row["scope"], row["role"], row["project_id"])] = row
        target.roles = {row["role"]: row for row in await db.read_roles(conn)}
        target.channels = {row["channel_id"]: row for row in await db.read_channels(conn)}
        target.bot_sequence = await db.read_bot_sequence(conn)
        target.items = {(row["kind"], row["name"], row["owner_project_id"]): row for row in await db.read_items(conn)}
        target.installs = {(row["item_id"], row["project_id"]): row for row in await db.read_installs(conn)}
        target.projects = {row["name"].lower(): row for row in await db.read_projects(conn)}
        for row in await db.read_documents(conn, content=False):
            target.documents[row["project_id"]].append(row)
        for row in await db.read_role_templates(conn):
            target.role_templates[row["project_id"]][row["template_key"]] = row
        target.runtimes = {row["name"]: row for row in await db.read_runtimes(conn)}
        target.providers = {row["name"]: row for row in await db.read_providers(conn)}
        target.models = {(row["provider"], row["name"], row["protocol"]): row for row in await db.read_models(conn)}
        target.profiles = {row["name"]: row for row in await db.read_profiles(conn)}
        target.managers = {row["id"]: row for row in await db.read_managers(conn, include_deleted=True)}
        target.manager_templates = {(row["manager_id"], row["key"]): row for row in
                                    await db.read_manager_templates(conn)}
        target.manager_installs = {(row["manager_id"], row["item_id"]): row for row in
                                   await db.read_manager_installs(conn)}
        from ..db.schedules import list_schedules

        for row in await list_schedules(pool=conn):
            target.schedules[row["project_id"]].append(row)
        return target


@dataclass(frozen=True)
class _FileLayer:
    """A layer of the file, checked: its values (its profile by name), its profile's id and the effort it carries."""

    section: str
    label: str
    key: tuple[str, str | None, str | None]  # scope, role, project id
    values: dict[str, Any]
    profile_id: str | None
    effort: str | None


def _effort_key(key: tuple[str, str | None, str | None]) -> Any:
    """A layer's key as ``layer_effort`` names it."""
    from .agent_settings import AgentType, Layer

    scope, role, project_id = key
    return Layer(scope), AgentType(role) if role else None, project_id


class _Importer:
    def __init__(self, conn: Any, target: _Target, apply: bool) -> None:
        self.conn = conn
        self.target = target
        self.apply = apply
        self.plan = Plan(applied=apply)
        # Item ids by key, for installs: stored ones and the ones this import adds ("new:..." in a preview).
        self.item_ids = {key: row["id"] for key, row in target.items.items()}
        self.item_origins = {key: row["origin"] for key, row in target.items.items()}
        self.item_sizes = {key: int(row["size_bytes"]) for key, row in target.items.items()}
        self.project_ids = {name: row["id"] for name, row in target.projects.items()}
        self.project_labels = {row["id"]: f"{row['name']} / " for row in target.projects.values()}
        self.file_layers: list[_FileLayer] = []
        self.seen_items: set[tuple[str, str, str | None]] = set()
        self.seen_projects: set[str] = set()
        # Each channel's configuration as the import leaves it (the file's over this machine's): its sender allowlist
        # says which chats a schedule may tell.
        self.channel_configs = {row["channel_type"]: dict(row["config"] or {}) for row in target.channels.values()
                                if row["channel_id"] in BUILTIN_CHANNEL_IDS}
        # Imported configs by immutable id; identity checks see the final graph even in a preview.
        self.bot_configs: dict[str, dict[str, Any]] = {}
        self.bot_imports: list[tuple[str, str, str, dict[str, Any], Any, list[str]]] = []
        self.channel_graph = False
        self.imported_managers: set[str] = set()
        # The schedules this import adds or rewrites, disabled; those that had a directory of their own.
        self.disabled_schedules: list[str] = []
        self.min_interval: int | None = None  # the file's, once read
        self.repointed_schedules: list[str] = []
        self.role_text_limits: dict[str, int] = {}  # by project id: how long its roles' texts may be
        self.role_template_values = {pid: dict(rows) for pid, rows in target.role_templates.items()}
        self.invalid_role_templates: set[tuple[str, str]] = set()
        from .profile_registry import (Endpoint, ModelSpec, ResolvedModel, builtin_resolved_profile,
                                       provider_spec, runtime_spec)

        builtin = builtin_resolved_profile()
        self.runtimes = {name: (row["id"], runtime_spec(row)) for name, row in target.runtimes.items()}
        self.runtimes.setdefault(builtin.runtime.name, (builtin.runtime.id, builtin.runtime))
        self.builtin_runtimes = {spec.type: (identity, spec) for name, (identity, spec) in self.runtimes.items()
                                 if name not in target.runtimes or target.runtimes[name]["builtin"]}
        self.providers = {name: (row["id"], provider_spec(row)) for name, row in target.providers.items()}
        self.providers.setdefault(builtin.resolved_model.provider.name,
                                  (builtin.resolved_model.provider.id, builtin.resolved_model.provider))
        self.endpoints = {(name, e["protocol"]): Endpoint(identity, e["protocol"], e["base_url"],
                                                        e.get("use_runtime_default", False))
                          for name, row in target.providers.items() for e in row["endpoints"]
                          for identity in [row["id"]]}
        self.endpoints.setdefault((builtin.provider.name, builtin.resolved_model.model.protocol.value),
                                  builtin.resolved_model.endpoint)
        self.models = {}
        for key, row in target.models.items():
            model = ModelSpec.from_entry({k: row[k] for k in ModelSpec.__dataclass_fields__})
            self.models[key] = ResolvedModel(model, self.providers[key[0]][1], self.endpoints[(key[0], key[2])])
        self.models.setdefault((builtin.provider.name, builtin.model, builtin.resolved_model.model.protocol.value),
                               builtin.resolved_model)
        self.profiles = {builtin.name: (builtin.id, builtin)}
        for name, row in target.profiles.items():
            key = (row["provider"], row["model"], row["protocol"])
            self.profiles[name] = (row["id"], self._profile_spec(
                row["id"], name, row["runtime"], key, row["effort"], row["agent_teams"], row.get("custom_prompt", "")))
        self.blocked_runtimes: set[str] = set()
        self.blocked_providers: set[str] = set()
        self.blocked_models: set[tuple[str, str, str]] = set()
        self.blocked_profiles: set[str] = set()
        self.seen_registry: dict[str, set[Any]] = defaultdict(set)

    def _profile_spec(self, profile_id: str, name: str, runtime: str, model_key: tuple[str, str, str],
                      effort: str | None, agent_teams: bool, custom_prompt: str = "") -> Any:
        from .profile_registry import ResolvedAgentProfile

        resolved = self.models[model_key]
        return ResolvedAgentProfile(profile_id, name, self.runtimes[runtime][1], resolved.model.id, effort,
                                    agent_teams, custom_prompt, resolved_model=resolved)

    def _unique(self, section: str, key: Any) -> None:
        if key in self.seen_registry[section]:
            raise _Skip("duplicate natural key in the file")
        self.seen_registry[section].add(key)

    def registry_entries(self, section: str, entries: Sequence[Any]) -> list[Any]:
        """Reject every occurrence of an ambiguous natural key before writing."""
        def key(entry: Any) -> Any:
            if not isinstance(entry, Mapping):
                return None
            fields = ("provider", "name", "protocol") if section == "models" else ("name",)
            values = tuple(entry.get(k) for k in fields)
            if not all(isinstance(v, str) for v in values):
                return None
            return values if section == "models" else tuple(v.strip() for v in values)

        counts = Counter(key(entry) for entry in entries)
        duplicates = {identity for identity, count in counts.items() if identity is not None and count > 1}
        for identity in sorted(duplicates):
            label = " / ".join(identity)
            self.record(section, label, Action.SKIPPED, reason="duplicate natural key in the file")
            if section == "models":
                self.blocked_models.add(identity)
            elif section == "providers":
                self.blocked_providers.add(label)
            else:
                self.blocked_profiles.add(label)
        return [entry for entry in entries if key(entry) not in duplicates]

    def record(self, section: str, key: str, action: Action, fields: Sequence[str] = (), reason: str = "",
               code: str = "") -> None:
        self.plan.changes.append(Change(section, key, action, tuple(fields), reason, code))

    def legacy_failure(self, section: str, key: str, exc: Exception) -> None:
        from .registry_migration import MigrationBlocked
        from .profile_registry import RegistryError

        code = getattr(exc, "code", "invalid_legacy_value" if isinstance(exc, RegistryError) else "")
        reason = str(exc)
        if isinstance(exc, MigrationBlocked):
            reason = {
                "invalid_legacy_value": "a legacy value is invalid; check the provider and Agent Profile fields",
                "bad_provider_reference": "the legacy Agent Profile refers to a missing provider",
                "bad_runtime_reference": "the legacy Agent Profile refers to a missing runtime",
                "incompatible_auth": "the runtime cannot use this provider's authentication kind",
                "incompatible_login": "a login provider can only use its own runtime",
                "incompatible_protocol": "the runtime and provider share no supported protocol",
                "model_outside_catalog": "the Agent Profile's model is not in its provider's model catalog",
                "invalid_protocols": "the provider needs a non-empty list of supported protocols",
                "unknown_protocol": "the provider uses an unsupported protocol",
                "unknown_runtime": "the Agent Profile uses an unsupported runtime",
                "unknown_login": "the provider uses an unsupported runtime login",
                "unknown_default_endpoint": "the provider needs an explicit endpoint URL on this machine",
                "default_endpoint_conflict": "the provider's default endpoint conflicts with its saved configuration",
                "invalid_id": "a legacy registry entry needs a non-empty identifier",
                "duplicate_id": "the file gives the same legacy registry identifier twice",
            }.get(code, "the legacy registry cannot be converted safely; check its provider and Agent Profile fields")
        self.record(section, key, Action.SKIPPED, reason=reason, code=code)

    def outcome(self, section: str, key: str, current: Any, changed: Sequence[str]) -> Action:
        action = Action.ADDED if current is None else Action.CHANGED if changed else Action.UNCHANGED
        self.record(section, key, action, changed)
        return action

    # --- runtimes, providers and agents ---

    async def runtime(self, entry: Any) -> None:
        """Map a runtime of the file to the one its profiles run on here; runtimes are built in, so nothing is
        written (the module's **Runtimes**)."""
        from .profile_registry import RegistryError, _executable, entry_name
        from .runtimes import RuntimeUnsupported, adapter

        raw = entry if isinstance(entry, Mapping) else {}
        label = str(raw.get("name"))
        try:
            entry = _mapping(entry, "a runtime")
            unknown = sorted(set(entry) - {"name", "type", "executable"})
            if unknown:
                raise _Skip(f"unknown field {unknown[0]!r}")
            try:
                name = label = entry_name(entry.get("name"), "runtime")
                runtime = adapter(entry.get("type") if isinstance(entry.get("type"), str) else "")
                if entry.get("executable") is not None:
                    _executable(entry["executable"])
            except (RegistryError, RuntimeUnsupported) as exc:
                raise _Skip(str(exc)) from None
            row = self.target.runtimes.get(name)
            if row is not None and row["type"] != runtime.type:
                raise _Skip(f"a {row['type']} runtime has this name here")
            if row is None and runtime.type not in self.builtin_runtimes:
                if runtime.in_process:
                    raise _Skip(f"runtimes are built in, and {runtime.label} cannot run here: "
                                f"{runtime.probe('').error}; then Re-detect it in Settings, Agent Runtimes")
                raise _Skip(f"runtimes are built in, and {runtime.label} ({runtime.default_executable}) is not on "
                            "this machine: install it, then Re-detect it in Settings, Agent Runtimes")
        except _Skip as exc:
            self.blocked_runtimes.add(label)
            self.record("runtimes", label, Action.SKIPPED, reason=str(exc))
            return
        if row is not None:
            self.record("runtimes", name, Action.UNCHANGED)
            return  # self.runtimes has it already
        self.runtimes[name] = self.builtin_runtimes[runtime.type]
        self.plan.notes.append(f"runtime {name}: runtimes are built in; its Agent Profiles run on the built-in "
                               f"{self.runtimes[name][1].name} runtime here")

    def legacy(self, data: Mapping[str, Any]) -> Mapping[str, Any]:
        """Normalize only this file's old objects, then use the schema converter.

        Target credentials fill omitted keys in memory. Target accounts are
        never candidates for opportunistic account merging.
        """
        from . import registry_migration as migration
        from .profile_registry import AgentProfile, RegistryError, default_agent_teams, entry_name, _model

        runtimes = list({spec.id: spec for _, spec in self.runtimes.values()}.values())
        accounts, rows, sources = {}, [], {}
        for entry in data.get("providers", []):
            label = str(entry.get("name")) if isinstance(entry, Mapping) else "None"
            try:
                entry = _mapping(entry, "a legacy provider")
                name = entry_name(entry.get("name"), "provider")
                if name in sources:
                    raise _Skip("duplicate natural key in the file")
                sources[name] = entry
                unknown = set(entry) - {"name", "api_key", *_PROVIDER_FIELDS}
                if unknown:
                    raise _Skip(f"unknown field {sorted(unknown)[0]!r}")
                stored = self.target.providers.get(name, {})
                row = {"id": stored.get("id", f"file:provider:{name}"), "oauth_runtime_type": None,
                       "base_url": "", "models": [], "env": [], "max_concurrency": None, **entry}
                if is_placeholder(row.get("api_key")):
                    row["api_key"] = stored.get("api_key")
                migration.convert(runtimes, [row], [])
                accounts[name] = row
            except (RegistryError, _Skip) as exc:
                self.blocked_providers.add(label)
                self.legacy_failure("providers", label, exc)
        profile_sources = {}
        profile_entries = list(data.get("profiles", []))
        profile_names = {entry["name"] for entry in profile_entries
                         if isinstance(entry, Mapping) and isinstance(entry.get("name"), str)}
        layers = list(data.get("settings", {}).get("layers", []))
        for project in data.get("projects", []):
            if isinstance(project, Mapping) and isinstance(project.get("layers"), list):
                layers.extend(project["layers"])
        # Before profiles existed, a layer explicitly selected a Claude model.
        # Convert that choice through the same registry converter as v1 profiles.
        for layer in layers:
            if not isinstance(layer, Mapping) or "profile" in layer or not isinstance(layer.get("model"), str):
                continue
            model = layer["model"]
            name = f"{_MODEL_PROFILE_PREFIX}{model}"
            if name not in self.profiles and name not in profile_names and name not in self.blocked_profiles:
                profile_entries.append({"name": name, "runtime": "claude-code", "provider": "claude-subscription",
                                        "model": model})
                profile_names.add(name)
        for entry in profile_entries:
            label = str(entry.get("name")) if isinstance(entry, Mapping) else "None"
            try:
                entry = _mapping(entry, "a legacy Agent Profile")
                name = entry_name(entry.get("name"), "Agent Profile")
                if name in profile_sources:
                    raise _Skip("duplicate natural key in the file")
                profile_sources[name] = entry
                if "system_prompt_mode" in entry:
                    self.plan.notes.append(f"profiles {name}: its system_prompt_mode is ignored; set the mode per role")
                unknown = set(entry) - {"name", "runtime", "provider", "model", "effort", "agent_teams",
                                        "custom_prompt", "system_prompt_mode"}
                if unknown:
                    raise _Skip(f"unknown field {sorted(unknown)[0]!r}")
                runtime = self.runtimes.get(_string(entry.get("runtime"), "runtime", required=True))
                provider = _string(entry.get("provider"), "provider", required=True)
                if runtime is None or entry["runtime"] in self.blocked_runtimes:
                    raise _Skip(f"no runtime {entry['runtime']!r} here or in the file")
                if provider in self.blocked_providers:
                    raise _Skip("provider was skipped")
                if provider not in accounts:
                    # Legacy exports omit built-in accounts. Only explicitly
                    # referenced target accounts enter, protected from merging.
                    stored = self.target.providers.get(provider)
                    if stored is not None:
                        endpoints = stored["endpoints"]
                        urls = {e["base_url"] for e in endpoints}
                        if len(urls) != 1:
                            raise _Skip("legacy profile needs an explicit model protocol on this target")
                        row = {"id": stored["id"], "name": provider, "vendor_type": stored["vendor_type"],
                               "auth": stored["auth"], "oauth_runtime_type": stored["oauth_runtime_type"],
                               "protocols": [e["protocol"] for e in endpoints], "base_url": next(iter(urls)),
                               "env": stored["env"], "api_key": stored["api_key"], "models": [], "builtin": True}
                    elif provider in self.providers and self.providers[provider][1].builtin:
                        spec = self.providers[provider][1]
                        row = {"id": spec.id, "name": spec.name, "vendor_type": spec.vendor_type.value,
                               "auth": spec.auth.value, "oauth_runtime_type": spec.oauth_runtime_type,
                               "protocols": [spec.vendor_type.login_protocol.value], "base_url": "", "builtin": True}
                    else:
                        raise _Skip("no provider here or in the file")
                    accounts[provider] = row
                stored_profile = self.target.profiles.get(name, {})
                row = {"id": stored_profile.get("id", f"file:profile:{name}"), "name": name,
                       "runtime_id": runtime[0], "provider_id": accounts[provider]["id"],
                       "model": entry.get("model"), "effort": entry.get("effort"),
                       "agent_teams": entry.get("agent_teams", default_agent_teams(runtime[1])),
                       "custom_prompt": entry.get("custom_prompt", "")}
                # Keep the public validators' actionable messages before the
                # migration converter reduces failures to safe report codes.
                _model(row["model"])
                AgentProfile(row["id"], name, runtime[1], "legacy-model", row["effort"],
                             row["agent_teams"], row["custom_prompt"])
                migration.convert(runtimes, [accounts[provider]], [row])
                rows.append(row)
            except (RegistryError, _Skip) as exc:
                self.blocked_profiles.add(label)
                self.legacy_failure("profiles", label, exc)
        converted = migration.convert(runtimes, list(accounts.values()), rows)
        for name, row in accounts.items():
            canonical = converted.provider_map[row["id"]]
            if canonical != row["id"]:
                self.plan.notes.append(f"providers {name}: converted to {converted.providers[canonical].name}")
        providers = []
        for spec in converted.providers.values():
            if spec.name not in sources or spec.name in self.blocked_providers:
                continue
            endpoints = [{"protocol": p.value, "base_url": e.base_url,
                          "use_runtime_default": e.use_runtime_default}
                         for (pid, p), e in converted.endpoints.items() if pid == spec.id]
            source = sources[spec.name]
            providers.append(provider_entry({"name": spec.name, "vendor_type": spec.vendor_type.value,
                "auth": spec.auth.value, "oauth_runtime_type": spec.oauth_runtime_type,
                "env": [{"key": k, "value": v} for k, v in spec.env], "max_concurrency": spec.max_concurrency,
                "endpoints": endpoints, "api_key": source.get("api_key")}, True))
        models = []
        from dataclasses import asdict

        for spec in converted.models.values():
            provider = converted.providers[spec.provider_id].name
            key = (provider, spec.name, spec.protocol.value)
            if provider not in sources and key in self.models:
                continue
            models.append(model_entry({**asdict(spec), "provider": provider}))
        profiles = []
        for spec in converted.profiles.values():
            if spec.name in self.blocked_profiles:
                continue
            model = converted.models[spec.model_id]
            provider = converted.providers[model.provider_id].name
            profiles.append({"name": spec.name, "runtime": spec.runtime.name, "effort": spec.effort,
                             "agent_teams": spec.agent_teams, "custom_prompt": spec.custom_prompt,
                             "model": {"provider": provider, "name": model.name, "protocol": model.protocol.value}})
        return {**data, "format": FORMAT, "providers": providers, "models": models, "profiles": profiles}

    async def provider(self, entry: Any) -> None:
        from .profile_registry import Endpoint, ProviderSpec, RegistryError, ResolvedModel
        from ..db import config_transfer as db

        label = str(entry.get("name")) if isinstance(entry, Mapping) else "None"
        try:
            entry = _mapping(entry, "a provider")
            unknown = set(entry) - {"name", "vendor_type", "auth", "oauth_runtime_type", "endpoints", "env",
                                    "max_concurrency", "api_key"}
            if unknown:
                raise _Skip(f"unknown field {sorted(unknown)[0]!r}")
            from .profile_registry import entry_name

            label = entry_name(entry.get("name"), "provider")
            self._unique("providers", label)
            row = self.target.providers.get(label)
            if row and row["builtin"] or label in self.providers and row is None:
                raise _Skip("a built-in provider has this name here")
            provider_id = row["id"] if row else f"new:{label}"
            key = entry.get("api_key")
            missing = is_placeholder(key) and not (row or {}).get("api_key")
            if is_placeholder(key):
                key = (row or {}).get("api_key")
            fields = {k: v for k, v in entry.items() if k not in ("endpoints", "api_key")}
            spec = ProviderSpec.from_entry({**fields, "id": provider_id, "api_key": key})
            label = spec.name
            if row and row["vendor_type"] != spec.vendor_type.value:
                raise _Skip("vendor_type conflicts with the existing provider")
            raw_endpoints = _mapping(entry.get("endpoints"), "endpoints")
            if not raw_endpoints:
                raise _Skip("a provider needs an endpoint")
            endpoints = {}
            for protocol, raw in raw_endpoints.items():
                raw = _mapping(raw, "an endpoint")
                unknown = set(raw) - {"base_url", "use_runtime_default"}
                if unknown:
                    raise _Skip(f"unknown endpoint field {sorted(unknown)[0]!r}")
                endpoint = Endpoint.from_entry({"provider_id": provider_id, "protocol": protocol, **raw})
                endpoint.check_provider(spec)
                endpoints[protocol] = endpoint
            # Preserve endpoints absent from the file. A changed endpoint must
            # prove every existing profile keeps its launch semantics.
            combined = {protocol: endpoint for (name, protocol), endpoint in self.endpoints.items() if name == label}
            combined.update(endpoints)
            for name, (_, profile) in self.profiles.items():
                old = profile.resolved_model
                if old.provider.id != provider_id:
                    continue
                resolved = ResolvedModel(old.model, spec, combined[old.model.protocol.value])
                resolved.check_runtime(profile.runtime, profile.effort)
                if resolved.endpoint != old.endpoint or resolved.provider.env != old.provider.env:
                    raise _Skip(f"endpoint or environment conflicts with Agent Profile {name!r}")
            values = {"name": spec.name, "vendor_type": spec.vendor_type.value, "auth": spec.auth.value,
                      "oauth_runtime_type": spec.oauth_runtime_type,
                      "env": [{"key": k, "value": v} for k, v in spec.env],
                      "max_concurrency": spec.max_concurrency,
                      "endpoints": [{"protocol": e.protocol.value, "base_url": e.base_url,
                                     "use_runtime_default": e.use_runtime_default} for e in combined.values()]}
            current = None if row is None else {**{k: row[k] for k in values if k != "endpoints"},
                        "endpoints": [{k: e.get(k, False) for k in ("protocol", "base_url", "use_runtime_default")}
                                      for e in row["endpoints"]], "api_key": row["api_key"]}
            values["endpoints"].sort(key=lambda e: e["protocol"])
            if current:
                current["endpoints"].sort(key=lambda e: e["protocol"])
            action = self.outcome("providers", label, current, _differs({**values, "api_key": key}, current,
                                                                       (*values, "api_key")))
            if self.apply and action in (Action.ADDED, Action.CHANGED):
                provider_id = await db.write_provider(self.conn, row["id"] if row else None, values, key)
            from dataclasses import replace

            spec = replace(spec, id=provider_id)
            self.providers[label] = (provider_id, spec)
            self.endpoints.update({(label, p): replace(e, provider_id=provider_id) for p, e in combined.items()})
            # Refresh models before subsequent profile/model validation.
            for model_key, resolved in list(self.models.items()):
                if model_key[0] == label:
                    self.models[model_key] = ResolvedModel(resolved.model, spec, self.endpoints[(label, model_key[2])])
            if missing:
                self.plan.need_secret.append(NeededSecret("providers", label, ("api_key",), _stored("provider", provider_id)))
        except (RegistryError, _Skip) as exc:
            self.blocked_providers.add(label)
            self.record("providers", label, Action.SKIPPED, reason=str(exc))

    async def model(self, entry: Any) -> None:
        from .profile_registry import MODEL_FIELDS, ModelSpec, RegistryError, ResolvedModel
        from ..db import config_transfer as db

        key = (None, None, None)
        label = str(entry)
        try:
            entry = _mapping(entry, "a model")
            key = tuple(_string(entry.get(k), k, required=True) for k in ("provider", "name", "protocol"))
            label = " / ".join(str(k) for k in key)
            self._unique("models", key)
            provider, name, protocol = key
            if provider in self.blocked_providers or provider not in self.providers:
                raise _Skip(f"no imported provider {provider!r}")
            if (provider, protocol) not in self.endpoints:
                raise _Skip(f"no endpoint for protocol {protocol!r}")
            row = self.target.models.get(key)
            if (row and row["builtin"]) or (key in self.models and row is None):
                raise _Skip("a built-in model has this natural key here")
            model = ModelSpec.from_entry({"id": row["id"] if row else f"new:{label}",
                                         "provider_id": self.providers[provider][0],
                                         **{k: v for k, v in entry.items() if k != "provider"}})
            resolved = ResolvedModel(model, self.providers[provider][1], self.endpoints[(provider, protocol)])
            for _, profile in self.profiles.values():
                if profile.model_id == model.id:
                    resolved.check_runtime(profile.runtime, profile.effort)
            values = {k: getattr(model, k) for k in (*MODEL_FIELDS, "provider_id", "protocol", "catalog_order")}
            values["protocol"] = model.protocol.value
            values["efforts"] = list(model.efforts) if model.efforts is not None else None
            current = {k: row[k] for k in values} if row else None
            action = self.outcome("models", label, current, _differs(values, current, values))
            if self.apply and action in (Action.ADDED, Action.CHANGED):
                from dataclasses import replace

                model_id = await db.write_model(self.conn, row["id"] if row else None, values)
                resolved = ResolvedModel(replace(model, id=model_id), resolved.provider, resolved.endpoint)
            self.models[key] = resolved
        except (RegistryError, _Skip) as exc:
            self.blocked_models.add(key)
            self.record("models", label, Action.SKIPPED, reason=str(exc))

    async def profile(self, entry: Any) -> None:
        from .profile_registry import RegistryError, entry_name, default_agent_teams

        label = str(entry.get("name")) if isinstance(entry, Mapping) else "None"
        try:
            entry = _mapping(entry, "an Agent Profile")
            unknown = set(entry) - {"name", "runtime", "model", "effort", "agent_teams", "custom_prompt"}
            if unknown:
                raise _Skip(f"unknown field {sorted(unknown)[0]!r}")
            name = label = entry_name(entry.get("name"), "Agent Profile")
            self._unique("profiles", name)
            runtime = _string(entry.get("runtime"), "runtime", required=True)
            if runtime in self.blocked_runtimes or runtime not in self.runtimes:
                raise _Skip(f"no runtime {runtime!r} here or in the file")
            ref = _mapping(entry.get("model"), "model reference")
            if set(ref) != {"provider", "name", "protocol"} or any(not isinstance(v, str) for v in ref.values()):
                raise _Skip("model needs provider, exact name and protocol")
            key = (ref["provider"], ref["name"], ref["protocol"])
            if key not in self.models or key in self.blocked_models or key[0] in self.blocked_providers:
                raise _Skip("no valid model here or in the file")
            row = self.target.profiles.get(name)
            if row and row["builtin"] or name in self.profiles and row is None:
                raise _Skip("a built-in Agent Profile has this name here")
            spec = self._profile_spec(row["id"] if row else f"new:{name}", name, runtime, key,
                                      entry.get("effort"), entry.get("agent_teams", default_agent_teams(self.runtimes[runtime][1])),
                                      entry.get("custom_prompt", ""))
            spec.resolved_model.check_runtime(spec.runtime, spec.effort)
            values = {"runtime": spec.runtime.name, "provider": key[0], "model": key[1], "protocol": key[2],
                      "effort": spec.effort, "agent_teams": spec.agent_teams, "custom_prompt": spec.custom_prompt}
            current = {k: row[k] for k in values} if row else None
            action = self.outcome("profiles", name, current, _differs(values, current, values))
            profile_id = spec.id
            if self.apply and action in (Action.ADDED, Action.CHANGED):
                profile_id = await self._write_profile(row["id"] if row else None, name, spec)
            self.profiles[name] = (profile_id, spec)
        except (RegistryError, _Skip) as exc:
            self.blocked_profiles.add(label)
            self.record("profiles", label, Action.SKIPPED, reason=str(exc))

    async def _write_profile(self, profile_id: str | None, name: str, spec: Any) -> str:
        from ..db import config_transfer as db

        return await db.write_profile(self.conn, profile_id, {
            "name": name, "runtime_id": spec.runtime.id, "model_id": spec.model_id,
            "effort": spec.effort, "agent_teams": spec.agent_teams, "custom_prompt": spec.custom_prompt})

    async def _layer_profile(self, name: str | None, scope: str, role: str | None) -> str | None:
        """Resolve only a valid named profile; an omitted or invalid model never selects a default."""
        from .profile_registry import RegistryError, check_roles

        if name is None:
            return None
        if name in self.blocked_profiles or name not in self.profiles:
            raise _Skip(f"no valid Agent Profile {name!r} here or in the file")
        profile_id, spec = self.profiles[name]
        roles = {"system": ("manager", "planner", "worker", "reviewer"),
                 "project": ("planner", "worker", "reviewer")}.get(scope, (role,))
        try:
            check_roles(spec, roles)
        except RegistryError as exc:
            raise _Skip(str(exc)) from None
        return profile_id

    # --- settings ---

    async def layer(self, entry: Any, project_id: str | None, project_label: str = "") -> None:
        """Check one layer of the file; ``settle_layers`` records and writes it."""
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
            raw = self._without_agent_teams_env(raw, section, label)
            raw = self._without_project_concurrency(raw, section, label)
            values, effort = _layer_values(raw, scope, role)
            profile_id = await self._layer_profile(values["profile"], scope, role)
        except _Skip as exc:
            self.record(section, label, Action.SKIPPED, reason=str(exc))
            return
        self.file_layers.append(_FileLayer(section, label, (scope, role, project_id), values, profile_id, effort))

    async def settle_layers(self) -> None:
        """Record and write the file's layers. When one carries an effort, the effort first moves onto profiles
        (the module's **A layer's effort**): the profiles that takes are added, and each layer it changes selects
        its profile."""
        from .layer_effort import Derived

        conversion = self._effort_conversion() if any(layer.effort for layer in self.file_layers) else None
        changes = dict(conversion.layers) if conversion else {}
        ids: dict[str, str] = {}
        for derived in conversion.derived if conversion else ():
            ids[derived.name] = await self._add_derived(derived)

        def selected(choice: Any) -> tuple[str | None, str | None]:
            if choice is None:
                return None, None
            return (ids[choice.name] if isinstance(choice, Derived) else choice.id), choice.name

        for layer in self.file_layers:
            values, profile_id = layer.values, layer.profile_id
            key = _effort_key(layer.key)
            if key in changes:
                profile_id, name = selected(changes.pop(key))
                values = {**values, "profile": name}
                self._effort_note(layer.section, layer.label, layer.effort, name, layer.values["profile"])
            current = _stored_layer(self.target.layers.get(layer.key))
            await self._write_layer(layer.section, layer.label, layer.key, current, values, profile_id)
        for key, choice in changes.items():  # the layers of this installation the conversion changes
            scope, role, project_id = key[0].value, key[1].value if key[1] else None, key[2]
            section = "project.layers" if project_id else "settings.layers"
            label = f"{self.project_labels.get(project_id or '', '')}{scope}" + (f":{role}" if role else "")
            current = _stored_layer(self.target.layers.get((scope, role, project_id)))
            profile_id, name = selected(choice)
            empty = {"harness_prompt": None, "env": [], "run_budget_usd": None, "max_concurrency": None,
                     "boundaries": None, "system_prompt_mode": None}
            self._effort_note(section, label, None, name, current["profile"] if current else None)
            await self._write_layer(section, label, (scope, role, project_id), current,
                                    {**(current or empty), "profile": name}, profile_id)

    async def _write_layer(self, section: str, label: str, key: tuple[str, str | None, str | None],
                           current: Mapping[str, Any] | None, values: Mapping[str, Any], profile_id: str | None,
                           ) -> None:
        action = self.outcome(section, label, current, _differs(values, current, _LAYER_FIELDS))
        if self.apply and action in (Action.ADDED, Action.CHANGED):
            from ..db import config_transfer as db

            await db.write_layer(self.conn, *key, {**values, "profile_id": profile_id})

    def _effort_conversion(self) -> Any:
        """``layer_effort.convert`` over this installation's layers, each replaced by the file's of the same key."""
        from dataclasses import replace

        from .layer_effort import Before, convert

        # A profile this import adds keeps its "new:..." spec id in self.profiles; the conversion names it by its id.
        specs = {profile_id: replace(spec, id=profile_id) for profile_id, spec in self.profiles.values()}
        layers = {_effort_key(key): Before(specs.get(row["profile_id"])) for key, row in self.target.layers.items()}
        for layer in self.file_layers:
            layers[_effort_key(layer.key)] = Before(specs.get(layer.profile_id), layer.effort)
        return convert(layers, specs.values())

    async def _add_derived(self, derived: Any) -> str:
        """Add a profile the effort conversion takes; its id (``new:...`` in a preview)."""
        from dataclasses import replace

        base = derived.base
        spec = replace(base, id=f"new:{derived.name}", name=derived.name, effort=derived.effort)
        self.record("profiles", derived.name, Action.ADDED)
        profile_id = await self._write_profile(None, derived.name, spec) if self.apply else spec.id
        self.profiles[derived.name] = (profile_id, replace(spec, id=profile_id))
        self.plan.notes.append(f"profiles {derived.name}: added, {base.name} with effort {derived.effort}, for the "
                               "effort the file's layers set (the effort is the Agent Profile's now)")
        return profile_id

    def _effort_note(self, section: str, label: str, effort: str | None, name: str | None,
                     before: str | None) -> None:
        selects = f"selects {name}" if name else "selects no Agent Profile of its own"
        if effort:
            self.plan.notes.append(f"{section} {label}: its effort {effort} is the Agent Profile's now: the layer "
                                   f"{selects}")
        elif name != before:
            self.plan.notes.append(f"{section} {label}: {selects}, so its roles keep the effort the file's layers "
                                   "give them (the effort is the Agent Profile's now)")

    def _without_agent_teams_env(self, raw: Mapping[str, Any], section: str, label: str) -> Mapping[str, Any]:
        """The layer without Claude Code's agent teams variable, which an Agent Profile's switch now owns (a file
        from before it may carry it), with a note; the rest of the layer imports as it is."""
        from .runtimes.claude_code import AGENT_TEAMS

        env = raw.get("env")
        if not isinstance(env, list) or not any(isinstance(e, Mapping) and e.get("key") == AGENT_TEAMS for e in env):
            return raw
        self.plan.notes.append(f"{section} {label}: {AGENT_TEAMS} was left out of the environment; agent teams are "
                               "switched on each Agent Profile")
        return {**raw, "env": [e for e in env if not (isinstance(e, Mapping) and e.get("key") == AGENT_TEAMS)]}

    def _without_project_concurrency(self, raw: Mapping[str, Any], section: str, label: str) -> Mapping[str, Any]:
        """The layer without a ``max_concurrency`` that only the Workers' and the Reviewers' role layers take now (a
        file from before concurrency was per task may give a project's, a role's in a project or the Planner's), with
        a note; the rest of the layer imports as it is."""
        if raw.get("max_concurrency") is None or (raw.get("scope") == "role" and raw.get("role") in _LIMIT_ROLES):
            return raw
        self.plan.notes.append(
            f"{section} {label}: its max_concurrency {raw['max_concurrency']} was ignored: concurrency is per task, so "
            "nothing limits how many tasks run and a project has no limits; set the default for every task on the "
            "worker or reviewer role (Settings → Roles), or a task's own limit")
        rest = {name: value for name, value in raw.items() if name != "max_concurrency"}
        if all(rest.get(name) in (None, [], "") for name in (*_LAYER_FIELDS, "model", "effort")):
            raise _Skip("it sets nothing but a max_concurrency, which is ignored (see the notes)")
        return rest

    async def role(self, entry: Any) -> None:
        from ..db import config_transfer as db

        raw = entry if isinstance(entry, Mapping) else {}
        role = raw.get("role")
        try:
            entry = _mapping(entry, "a role")
            if role not in db.ROLES:
                raise _Skip(f"role {role!r} is not one of {', '.join(db.ROLES)}")
            prompt = entry.get("prompt")
            values = {
                "prompt": None if prompt is None else _string(prompt, "prompt", max_chars=_PROMPT_MAX_CHARS),
                "timeout_seconds": _whole(entry.get("timeout_seconds"), "timeout_seconds", 0, _TIMEOUT_MAX_SECONDS),
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

    async def scheduler(self, entry: Any) -> None:
        """The scheduler's system settings of the file: each one it names is set here (a number only)."""
        from . import scheduler_settings

        try:
            entry = _mapping(entry, "scheduler")
        except _Skip as exc:
            self.record("settings.scheduler", "scheduler", Action.SKIPPED, reason=str(exc))
            return
        current = await stored_scheduler_settings(self.conn)
        for name in scheduler_settings.FIELDS:
            if name not in entry:
                continue
            try:
                if entry[name] is None:
                    raise ValueError(f"{name} must be a number of seconds")
                value = scheduler_settings.checked({name: entry[name]})[name]
            except ValueError as exc:
                self.record("settings.scheduler", name, Action.SKIPPED, reason=str(exc))
                continue
            if name == "min_interval_seconds":
                self.min_interval = value  # the file's schedules are checked against it
            action = self.outcome("settings.scheduler", name, current.get(name),
                                  (name,) if current.get(name) != value else ())
            if self.apply and action in (Action.ADDED, Action.CHANGED):
                await scheduler_settings.store(name, value, conn=self.conn)
        for name in sorted(set(entry) - set(scheduler_settings.FIELDS)):
            self.record("settings.scheduler", name, Action.SKIPPED, reason="unknown scheduler setting")

    async def channel(self, entry: Any) -> None:
        from .channels.allowlist import AllowlistError, normalized_config

        raw = entry if isinstance(entry, Mapping) else {}
        kind = raw.get("channel_type")
        try:
            entry = _mapping(entry, "a channel")
            if kind not in _CHANNEL_PUBLIC_KEYS:
                raise _Skip(f"channel type {kind!r} is not one of {', '.join(_CHANNEL_PUBLIC_KEYS)}")
            channel_id = entry.get("channel_id") or builtin_channel_id(kind)
            suffix = _bot_suffix(channel_id, kind)
            owner = entry.get("manager_id", "manager")
            row = self.target.channels.get(channel_id)
            if row and (row["channel_type"] != kind or row["manager_id"] != owner):
                raise ConfigFileConflict("channels: immutable bot type or owner conflict; existing bots cannot be reassigned")
            if self.channel_graph:
                manager = self.target.managers.get(owner)
                if manager is not None and manager["deleted_at"] is not None or (
                        manager is None and owner not in self.imported_managers and owner != "manager"):
                    raise ConfigFileError("channels: missing or deleted Manager reference")
                if suffix is not None and row is None:
                    high, called = self.target.bot_sequence
                    occupied = any(identity.endswith(f"-{suffix}") for identity in self.target.channels)
                    if suffix < high or suffix == high and called or occupied:
                        raise ConfigFileConflict("channels: bot id conflict: this suffix was already allocated; "
                                                 "add a new bot locally in Settings → Channels")
            config, missing = _fill_placeholders(_mapping(entry.get("config", {}), "config"),
                                                 dict(row["config"] or {}) if row else {})
            try:
                config = normalized_config(kind, config)
                if kind == "lark":
                    # A file from before lark-cli's profile chose the app names the app's env vars: dropped.
                    from .channels.lark_cli import lark_config

                    config = lark_config(config)
                elif kind == "lark_bot":
                    from .channels.lark_bot_settings import stored_config

                    config = stored_config(config)
            except (AllowlistError, ValueError) as exc:
                raise _Skip(str(exc)) from None
            name = entry.get("display_name", None if self.channel_graph else "")
            values = {"display_name": "" if name is None and self.channel_graph else
                      _string(name, "display_name", max_chars=200),
                      "enabled": _boolean(entry.get("enabled", False), "enabled"), "config": config}
            if suffix is not None:
                from .channels.bot_api import BotValidationReason

                if kind == "telegram" and config.get("bot_token_env"):
                    raise _Skip("a bot's token is entered only in Settings → Channels, not read from the environment")
                if kind == "lark" and not config.get("profile"):
                    raise _Skip(BotValidationReason.LARK_PROFILE.value)
                if kind == "lark_bot" and not config.get("app_id"):
                    raise _Skip(BotValidationReason.LARK_APP_ID.value)
                settings_changed = row is None or any(values[key] != row[key] for key in ("display_name", "config"))
                # File enablement is never activation approval. Identical rows keep local enablement.
                values["enabled"] = False if settings_changed else row["enabled"]
                if settings_changed:
                    self.plan.notes.append(
                        f"{channel_id}: imported disabled; review credentials, local policy and chat bindings "
                        "in Settings → Channels before activation, then restart the server")
            if "bound_chats_only" in entry:
                self.plan.notes.append(f"{channel_id}: legacy bound_chats_only was ignored; the destination's "
                                       "local policy and chat bindings are preserved")
            if not self.channel_graph:
                await self._refuse_same_bot(channel_id, kind, config)
        except _Skip as exc:
            if self.channel_graph:
                raise ConfigFileError(f"channels: {exc}") from None
            self.record("channels", str(raw.get("channel_id") or builtin_channel_id(kind)) if kind in _CHANNEL_PUBLIC_KEYS
                        else str(kind), Action.SKIPPED, reason=str(exc))
            return
        self.bot_configs[channel_id] = config
        if suffix is None:
            self.channel_configs[kind] = config
        self.bot_imports.append((channel_id, kind, owner, values, row, missing))
        if not self.channel_graph:
            await self._write_channel(self.bot_imports.pop())

    async def finish_channels(self) -> None:
        """Validate against the final imported + stored configs, including rows not yet inserted."""
        from ..db import config_transfer as db
        from ..db import channel_configs as db_channels
        from .channels.bot_api import _LOCK
        from .channels.bots import SAME_BOT_TYPES, bot_keys, builtin_env_token, builtin_lark_pair, lark_app_id

        if not self.bot_imports:
            return
        await self.conn.execute(_LOCK)
        final = {**self.target.channels, **{identity: {**values, "channel_type": kind}
            for identity, kind, _, values, _, _ in self.bot_imports}}
        relevant = {peer for _, kind, *_ in self.bot_imports for peer in SAME_BOT_TYPES[kind]}
        has_secrets = await self.conn.fetchval("SELECT to_regclass('channel_secrets')") is not None
        keys: dict[str, set[str]] = {}
        unresolved: set[str] = set()
        for identity, row in final.items():
            kind, config = row["channel_type"], row["config"] or {}
            if kind not in relevant:
                continue
            # An untouched, disabled default does not act as lark-cli's active profile. Apply this to
            # competitors too; configured disabled rows still resolve their actual app identity.
            if (identity == "channel-lark" and not row["enabled"] and not any(
                    config.get(key) for key in ("profile", "allowed_open_ids", "allowlist_app_id"))):
                continue
            secret = await db_channels.get_bot_token(identity, pool=self.conn) if has_secrets else None
            env = builtin_env_token(config) if kind == "telegram" and identity in BUILTIN_CHANNEL_IDS and not secret else ""
            app_id = await lark_app_id(kind, config)
            if kind == "lark" and not app_id:
                unresolved.add(identity)
            keys[identity] = bot_keys(kind, config, secret, env_token=env, channel_id=identity, app_id=app_id)
        for identity, kind, _, values, _, _ in self.bot_imports:
            if not keys.get(identity):
                continue
            peers = {other for other, row in final.items() if other != identity
                     and row["channel_type"] in SAME_BOT_TYPES[kind] and not builtin_lark_pair(identity, other)}
            if any(keys.get(identity, set()) & keys.get(other, set()) for other in peers):
                raise ConfigFileError("channels: actual bot identity conflict; each added bot must use a different bot or Lark app")
            if identity in unresolved or peers & unresolved:
                # Restoring a graph is not activation approval. Keep unresolved candidates inert while
                # restoring their Manager and the other bots; known profile/app collisions still fail above.
                values["enabled"] = False
                self.plan.notes.append(f"{identity}: imported disabled; verify lark-cli profiles and authentication "
                                       "in Settings → Channels before activation, then restart the server")
                self.plan.need_secret.append(NeededSecret("channels", identity,
                    ("lark-cli profile authentication and identity verification",), ("channel", identity)))
        if self.apply:
            await db.reserve_bot_ids(self.conn, [_bot_suffix(identity, kind)
                for identity, kind, _, _, row, _ in self.bot_imports
                if identity not in BUILTIN_CHANNEL_IDS])
        for entry in self.bot_imports:
            await self._write_channel(entry)

    async def _write_channel(self, entry: tuple[str, str, str, dict[str, Any], Any, list[str]]) -> None:
        channel_id, kind, owner, values, row, missing = entry
        added_bot = channel_id not in BUILTIN_CHANNEL_IDS
        current = {"display_name": row["display_name"], "enabled": row["enabled"],
                   "config": dict(row["config"] or {})} if row else None
        action = self.outcome("channels", channel_id, current, _differs(values, current, ("display_name", "enabled",
                                                                                      "config")))
        if added_bot and action in (Action.ADDED, Action.CHANGED):
            self.plan.changes[-1] = Change("channels", channel_id, action, self.plan.changes[-1].fields,
                                          "effective enabled: false; local activation and restart required")
        needed = [f"config {name}" for name in missing]
        if values["enabled"]:
            needed += [f"environment {value}" for name, value in sorted(values["config"].items())
                       if name.endswith("_env") and isinstance(value, str) and value]
            # A DB-stored token is not in the file; an enabled channel still needs it set here.
            if kind in _CHANNEL_TOKEN_SECRET and not added_bot:
                needed.append(_CHANNEL_TOKEN_SECRET[kind])
        if added_bot and kind in _CHANNEL_TOKEN_SECRET:
            from ..db import channel_configs as db_channels

            has_secret = row is not None and await db_channels.get_bot_token(channel_id, pool=self.conn)
            if not has_secret:
                needed.append(_CHANNEL_TOKEN_SECRET[kind])
        if self.apply and action in (Action.ADDED, Action.CHANGED):
            from ..db import config_transfer as db

            await db.write_channel(self.conn, channel_id, kind, values, exists=row is not None, manager_id=owner)
        if needed:
            self.plan.need_secret.append(NeededSecret("channels", channel_id, tuple(needed),
                                                      ("channel", channel_id) if added_bot or row or self.apply else None))

    async def _refuse_same_bot(self, channel_id: str, kind: str, config: Mapping[str, Any]) -> None:
        """Legacy files use strict per-channel admission; v4 validates the final graph in finish_channels."""
        from ..db import channel_configs as db_channel_configs
        from . import projects
        from .channels.bot_api import _LOCK, _admission_app_id, refuse_same_bot
        from .channels.bots import builtin_env_token

        try:
            has_secrets = await self.conn.fetchval("SELECT to_regclass('channel_secrets')") is not None
            secret = None
            if has_secrets:
                await self.conn.execute(_LOCK)
                secret = await db_channel_configs.get_bot_token(channel_id, pool=self.conn)
            env = builtin_env_token(config) if kind == "telegram" and channel_id in BUILTIN_CHANNEL_IDS and not secret else ""
            if has_secrets:
                await refuse_same_bot(self.conn, kind, config, secret, env_token=env, exclude=channel_id,
                                      configs=self.bot_configs)
            await _admission_app_id(kind, config)
        except projects.RequestError as exc:
            raise _Skip(str(exc)) from None

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
        self.item_sizes[item.key] = item.values["size_bytes"]
        self.item_ids[item.key] = current["id"] if current else f"new:{label}"
        self.item_origins[item.key] = item.values["origin"]
        if self.apply and action in (Action.ADDED, Action.CHANGED):
            from ..db import config_transfer as db

            owner = owner_id if owner_id and not owner_id.startswith("new:") else None
            files = item.files if current is None or "files" in changed else None
            self.item_ids[item.key] = await db.write_item(
                self.conn, current["id"] if current else None, item.values, owner_project_id=owner,
                files=files, server=item.server)
        if item.missing:  # an MCP server's (a project writes skills only)
            self.plan.need_secret.append(NeededSecret(section, label, tuple(item.missing),
                                                      _stored("mcp", self.item_ids[item.key])))

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
        self.project_labels[project_id] = f"{name} / "
        from .project_execution import ProjectExecution

        self.role_text_limits[project_id] = ProjectExecution.from_row(execution).role_text_limit
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


    async def role_templates(self, entries: Any, project_id: str, label: str) -> None:
        """Add or update the project's role templates, checked as the dashboard checks one; a template the file
        does not give stays. A profile that is not here, or cannot run the kind, is left out with a note."""
        from .profile_registry import RegistryError, check_roles
        from .task_roles import DESCRIPTION_MAX_CHARS, RoleError, check_definition, role_key

        section = "project.role_templates"
        if not isinstance(entries, list):
            self.record(section, f"{label}role_templates", Action.SKIPPED, reason="role_templates is not a list")
            return
        stored = self.target.role_templates.get(project_id, {})
        seen: set[str] = set()
        for entry in entries:
            raw_key = entry.get("key") if isinstance(entry, Mapping) else None
            try:
                entry = _mapping(entry, "a role template")
                try:
                    key = role_key(entry.get("key"))
                    kind, prompt, harness = check_definition(entry.get("kind"), entry.get("role_prompt"),
                                                             entry.get("harness"), self.role_text_limits[project_id])
                except RoleError as exc:
                    raise _Skip(str(exc)) from None
                if key in seen:
                    raise _Skip("the file gives this template twice")
                seen.add(key)
                description = " ".join(_string(entry.get("description", ""), "description").split())
                if len(description) > DESCRIPTION_MAX_CHARS:
                    raise _Skip(f"description is over {DESCRIPTION_MAX_CHARS} characters")
                profile = entry.get("profile")
                if profile is not None:
                    profile = _string(profile, "profile", required=True)
                refs = entry.get("items") or []
                if not isinstance(refs, list):
                    raise _Skip("items is not a list")
            except _Skip as exc:
                self.record(section, f"{label}{raw_key}", Action.SKIPPED, reason=str(exc))
                if isinstance(raw_key, str):
                    self.invalid_role_templates.add((project_id, raw_key))
                continue
            items = []
            for ref in refs:
                owner_name = ref.get("project") if isinstance(ref, Mapping) else None
                owner = self.project_ids.get(str(owner_name).lower()) if owner_name is not None else None
                item_id = self.item_ids.get((ref.get("kind"), ref.get("name"), owner)) if isinstance(ref, Mapping) \
                    and (owner_name is None or owner is not None) else None
                if item_id is None:
                    self.invalid_role_templates.add((project_id, key))
                    shown = f"{ref.get('kind')}:{ref.get('name')}" if isinstance(ref, Mapping) else repr(ref)
                    self.plan.notes.append(f"{label}role template {key}: no item {shown} here or in the file; "
                                           "imported without it")
                elif item_id not in items:
                    items.append(item_id)
            profile_id = None
            if profile is not None:
                if profile in self.blocked_profiles:
                    self.invalid_role_templates.add((project_id, key))
                    self.record(section, f"{label}{key}", Action.SKIPPED,
                                reason=f"no valid Agent Profile {profile!r} here or in the file")
                    continue
                elif profile not in self.profiles:
                    self.invalid_role_templates.add((project_id, key))
                    self.plan.notes.append(f"{label}role template {key}: no Agent Profile {profile!r} here or in "
                                           "the file; imported without one")
                else:
                    try:
                        check_roles(self.profiles[profile][1], (kind.agent.value,))
                        profile_id = self.profiles[profile][0]
                    except RegistryError as exc:
                        self.invalid_role_templates.add((project_id, key))
                        self.plan.notes.append(f"{label}role template {key}: {exc}; imported without a profile")
            values = {"kind": kind.value, "description": description, "role_prompt": prompt, "harness": harness,
                      "profile_id": profile_id, "items": items}
            row = stored.get(key)
            current = {**{name: row[name] for name in values}, "items": list(row["items"] or ())} if row else None
            action = self.outcome(section, f"{label}{key}", current, _differs(values, current, values))
            self.role_template_values.setdefault(project_id, {})[key] = values
            if self.apply and action in (Action.ADDED, Action.CHANGED):
                from ..db import role_templates as db_templates

                await db_templates.put_template(project_id, key, values, pool=self.conn)

    # --- Manager definitions and their task templates ---

    def manager_profile(self, name: Any, role: str) -> str | None:
        from .profile_registry import RegistryError, check_roles

        if name is None:
            return None
        if not isinstance(name, str) or name in self.blocked_profiles or name not in self.profiles:
            raise ConfigFileError(f"{role}: no valid Agent Profile {name!r} here or in the file; import it first")
        identity, spec = self.profiles[name]
        try:
            check_roles(spec, (role,))
        except RegistryError as exc:
            raise ConfigFileError(f"{role} profile {name!r}: {exc}") from None
        return identity

    async def managers(self, entries: Sequence[Any]) -> None:
        """Validate the complete graph before writing any Manager, then merge by immutable identity."""
        from ..db import managers as db_managers, projects as db_projects
        from . import managers_api
        from .projects import RequestError

        checked: dict[str, dict[str, Any]] = {}
        templates: list[tuple[str, str, dict[str, Any], bool]] = []
        names: dict[str, str] = {}
        profile_names = {pid: name for name, (pid, _) in self.profiles.items()}
        project_names = {pid: name for name, pid in self.project_ids.items()}
        for raw in entries:
            try:
                entry = _mapping(raw, "a Manager")
                allowed = {"id", "name", "description", "system_prompt", "profile", "tools", "task_scope",
                           "task_policy", "enabled", "templates", "idle_timeout_seconds", "installs"}
                unknown = set(entry) - allowed
                if unknown:
                    raise ConfigFileError(f"managers: unknown field {sorted(unknown)[0]!r}")
                identity = entry.get("id")
                if not isinstance(identity, str) or (identity != "manager" and not re.fullmatch(
                        "manager-" + managers_api.MANAGER_KEY_PATTERN.removeprefix("^"), identity)):
                    raise ConfigFileError("managers.id must be 'manager' or the full immutable 'manager-<key>' id")
                if identity in checked:
                    raise ConfigFileError(f"managers: duplicate id {identity!r}")
                row = self.target.managers.get(identity)
                if row and row["deleted_at"] is not None:
                    raise ConfigFileError(f"Manager {identity!r} was deleted; choose a new immutable id")
                if "name" not in entry:
                    raise ConfigFileError(f"Manager {identity}: name is required")
                if identity == "manager" and set(entry) - {"id", "name", "description", "idle_timeout_seconds", "installs"}:
                    raise ConfigFileError("the default Manager entry may only set id, name, description, installs and idle_timeout_seconds: 0")
                values = {k: v for k, v in entry.items() if k not in ("id", "profile", "templates", "installs")}
                for name in ("name", "description", "system_prompt"):
                    if name in values:
                        _string(values[name], f"{identity}.{name}")
                if identity != "manager":
                    if row is None:
                        values = {"tools": "tasks", "task_scope": "own", "task_policy": "free", "enabled": True,
                                  "description": "", "profile_id": None, "idle_timeout_seconds": 1800, **values}
                        if "system_prompt" not in values:
                            raise ConfigFileError(f"Manager {identity}: system_prompt is required for a new definition")
                    if "profile" in entry:
                        values["profile_id"] = self.manager_profile(entry["profile"], "manager")
                values = await managers_api.checked_values(values, own_id=identity, conn=self.conn if self.apply else None,
                                                            check_references=self.apply)
                name_key = values["name"].casefold()
                if name_key in names:
                    raise ConfigFileError(f"managers: duplicate name {values['name']!r}")
                names[name_key] = identity
                checked[identity] = values
                raw_templates = entry.get("templates", [])
                if not isinstance(raw_templates, list):
                    raise ConfigFileError(f"Manager {identity}: templates must be a list")
                seen: set[str] = set()
                for template in raw_templates:
                    template = _mapping(template, f"{identity} task template")
                    fields = {"key", "name", "description", "requirement", "planner_instructions", "project",
                              "workspace_mode", "profiles", "role_templates", "repoint", "inputs"}
                    if set(template) - fields:
                        raise ConfigFileError(f"{identity} template: unknown field {sorted(set(template) - fields)[0]!r}")
                    key = template.get("key")
                    if not isinstance(key, str) or key in seen:
                        raise ConfigFileError(f"{identity}: missing or duplicate template key {key!r}")
                    seen.add(key)
                    current = self.target.manager_templates.get((identity, key))
                    if current:
                        # Overlay before reference resolution and validation so preview and apply agree.
                        stored = manager_template_entry(current, profile_names, project_names)
                        stored.pop("repoint", None)  # Only the file asks for a directory reminder.
                        template = {**stored, **template}
                    if not isinstance(template.get("name"), str) or not template["name"].strip():
                        raise ConfigFileError(f"{identity}/{key}: name must be non-empty")
                    project = template.get("project")
                    project_id = self.project_ids.get(project.strip().lower()) if isinstance(project, str) else None
                    if project is not None and project_id is None:
                        raise ConfigFileError(f"{identity}/{key}: no project {project!r}; include projects or create it first")
                    raw_profiles = template.get("profiles", {})
                    if not isinstance(raw_profiles, Mapping):
                        raise ConfigFileError(f"{identity}/{key}: profiles must be an object")
                    profiles = {}
                    for role, profile in raw_profiles.items():
                        if role not in ("planner", "worker", "reviewer") or not isinstance(profile, str):
                            raise ConfigFileError(f"{identity}/{key}: profiles.{role} must name a task Agent Profile")
                        profiles[role] = self.manager_profile(profile, role)
                    allowed = []
                    if project_id and not project_id.startswith("new:"):
                        allowed = await db_projects.allowed_profiles(project_id, pool=self.conn)
                        if allowed:
                            ids = {p["id"] for p in allowed}
                            for role, profile_id in profiles.items():
                                if profile_id not in ids:
                                    raise ConfigFileError(f"{identity}/{key}: profiles.{role} is not on project {project!r}'s list")
                    for name in ("name", "description", "requirement", "planner_instructions"):
                        if name in template:
                            _string(template[name], f"{identity}/{key}.{name}")
                    if "role_templates" in template and not isinstance(template["role_templates"], list):
                        raise ConfigFileError(f"{identity}/{key}: role_templates must be a list")
                    # Never resolve or normalize a machine path carried by a file. Preserve the target's exact path.
                    data = {k: v for k, v in template.items() if k not in ("key", "project", "repoint", "profiles")}
                    data.update(project_id=project_id, profiles=profiles, work_dir="")
                    values_template = await managers_api.checked_template_values(
                        identity, key, data, conn=self.conn if self.apply else None, check_references=self.apply)
                    values_template["work_dir"] = current["work_dir"] if current else ""
                    for role_key in values_template["role_templates"]:
                        if (project_id, role_key) in self.invalid_role_templates or role_key not in \
                                self.role_template_values.get(project_id, {}):
                            raise ConfigFileError(f"{identity}/{key}: project {project!r} has no valid role template {role_key!r}")
                        from .project_execution import ProjectExecution
                        from .task_roles import RoleError, check_definition

                        fixed = self.role_template_values[project_id][role_key]
                        project_row = next((p for p in self.target.projects.values() if p["id"] == project_id), {})
                        limit = self.role_text_limits.get(project_id, ProjectExecution.from_row(project_row).role_text_limit)
                        try:
                            kind, _, _ = check_definition(fixed["kind"], fixed["role_prompt"], fixed.get("harness"), limit)
                        except RoleError as exc:
                            raise ConfigFileError(f"{identity}/{key}: role template {role_key!r}: {exc}") from None
                        if fixed["profile_id"]:
                            named = next((name for name, (pid, _) in self.profiles.items()
                                          if pid == fixed["profile_id"]), None)
                            if named is None:
                                raise ConfigFileError(f"{identity}/{key}: role template {role_key!r} has no valid profile")
                            self.manager_profile(named, kind.agent.value)
                            if allowed and fixed["profile_id"] not in {p["id"] for p in allowed}:
                                raise ConfigFileError(f"{identity}/{key}: role template {role_key!r}'s profile is not "
                                                      f"on project {project!r}'s list")
                    repoint = template.get("repoint", False)
                    if not isinstance(repoint, bool):
                        raise ConfigFileError(f"{identity}/{key}: repoint must be true or false")
                    templates.append((identity, key, values_template, repoint))
            except (RequestError, _Skip) as exc:
                raise ConfigFileError(f"managers: {exc}") from None
        for identity, row in self.target.managers.items():
            owner = names.get(" ".join(row["name"].split()).casefold())
            if row["deleted_at"] is None and owner is not None and owner != identity:
                raise ConfigFileError(f"Manager name {row['name']!r} is already used by {identity}; "
                                      "rename its current owner before importing another Manager with that name")
        enabled = sum(bool(checked.get(identity, {}).get("enabled", row["enabled"]))
                      for identity, row in self.target.managers.items() if row["deleted_at"] is None)
        enabled += sum(bool(v.get("enabled", True)) for identity, v in checked.items()
                       if identity not in self.target.managers)
        if enabled > managers_api.MAX_ENABLED_MANAGERS:
            raise ConfigFileError(f"at most {managers_api.MAX_ENABLED_MANAGERS} Managers may be enabled; disable one first")
        # Disables precede enables so the maximum is respected throughout, too.
        for identity, values in sorted(checked.items(), key=lambda pair: (bool(pair[1].get("enabled", True)), pair[0])):
            row = self.target.managers.get(identity)
            action = self.outcome("managers", identity, row, _differs(values, row, values))
            if self.apply and action is Action.ADDED:
                await db_managers.insert_manager({"id": identity, **values}, conn=self.conn)
            elif self.apply and action is Action.CHANGED:
                await db_managers.update_manager(identity, values, conn=self.conn, only_if_changed=True)
        self.schedule_templates = {**self.target.manager_templates,
                                   **{(identity, key): {"manager_id": identity, "key": key, **values}
                                      for identity, key, values, _ in templates}}
        for identity, key, values, repoint in templates:
            row = self.target.manager_templates.get((identity, key))
            self.outcome("manager.templates", f"{identity}/{key}", row, _differs(values, row, values))
            if repoint:
                self.plan.notes.append(f"{identity}/{key}: local directory requires repointing on this machine; "
                                       + ("the existing target directory is preserved" if row and row["work_dir"] else
                                          "no local directory is invented; set one before using this template"))
            if self.apply:
                await db_managers.put_template(identity, key, values, conn=self.conn, only_if_changed=True)

    async def manager_installs(self, entries: Sequence[Any]) -> None:
        """Merge explicit extras after catalog and Manager identity resolution, in the import transaction."""
        from ..db import manager_installs as db_installs

        for entry in entries:
            identity = entry["id"]  # The Manager phase already validated identity and deletion state.
            raw = entry.get("installs", [])
            if not isinstance(raw, list):
                raise ConfigFileError(f"Manager {identity}: installs must be a list")
            seen = set()
            for install in raw:
                try:
                    install = _mapping(install, "a Manager install")
                    if set(install) != {"item", "enabled"}:
                        raise _Skip("a Manager install requires only item and enabled")
                    ref = _mapping(install["item"], "item")
                    if set(ref) != {"kind", "name"} or ref.get("kind") not in ("plugin", "skill"):
                        raise _Skip("Manager installs require a global plugin or skill natural reference")
                    name = _string(ref["name"], "item.name", required=True)
                    key = (ref["kind"], name, None)
                    item_id = self.item_ids.get(key)
                    if item_id is None or self.item_origins.get(key) == "project":
                        raise _Skip(f"no global {ref['kind']} {name!r} here or in the file")
                    if item_id in seen:
                        raise _Skip(f"duplicate Manager install {name!r}")
                    seen.add(item_id)
                    enabled = _boolean(install["enabled"], "enabled")
                except _Skip as exc:
                    raise ConfigFileError(f"Manager {identity} installs: {exc}") from None
                row = self.target.manager_installs.get((identity, item_id))
                values = {"enabled": enabled}
                action = self.outcome("manager.installs", f"{identity}/{ref['kind']}:{name}", row,
                                      _differs(values, row, values))
                if self.apply and action in (Action.ADDED, Action.CHANGED):
                    await db_installs.put_install(identity, item_id, enabled, conn=self.conn)

    # --- schedules ---

    def _notify_allowed(self, channel: Any, chat: Any) -> bool:
        from .channels.allowlist import SENDER_ID_FORMATS, AllowlistError, sender_ids

        if channel not in SENDER_ID_FORMATS or not isinstance(chat, str):
            return False
        config = self.channel_configs.get(channel) or {}
        try:
            return chat in sender_ids(channel, config.get(SENDER_ID_FORMATS[channel].field))
        except AllowlistError:
            return False

    def _schedule_values(self, entry: Any, label: str, managers: Mapping[str, Mapping[str, Any]],
                         ) -> tuple[dict[str, Any], dict[str, tuple[str, str]], dict[str, int]]:
        """A schedule of the file, checked as the API checks one: its settings (``schedules.FIELDS`` but its
        directory), its Agent Profiles as role -> (name, id; ``new:...`` in a preview) and its task limits.
        ``_Skip`` otherwise; a notify target this machine's allowlists do not allow is dropped, with a note."""
        from . import cron, schedules
        from .profile_registry import RegistryError, check_roles
        from .projects import TASK_PROFILE_ROLES, RequestError, _concurrency_limits

        entry = _mapping(entry, "a schedule")
        unknown = sorted(set(entry) - {*_SCHEDULE_FIELDS, "notify_target", "profiles", "task_limits", "repoint", "manager", "template_key", "template_inputs"})
        if unknown:
            raise _Skip(f"unknown field {unknown[0]!r}")
        owner = entry.get("manager", "manager")
        if not isinstance(owner, str) or owner not in managers:
            raise _Skip(f"Manager owner conflict: no nondeleted Manager {owner!r} here or in the file")
        defaults = {"overlap": "skip", "max_parallel_runs": 3, "catch_up": "once", "notify": "failures"}
        values: dict[str, Any] = {"manager_id": owner}
        try:
            from .schedule_templates import selection_patch
            values.update(selection_patch(entry))
            for name in _SCHEDULE_FIELDS:
                if name not in ("cron", "timezone", "run_timeout_seconds"):
                    values[name] = schedules._PARSERS[name](entry.get(name, defaults.get(name)))
            values.update(schedules._timeout(entry.get("run_timeout_seconds", schedules.TIMEOUT_DEFAULT)))
            zone = entry.get("timezone") or cron.local_zone_name()
            rule, _ = schedules.checked_rule(entry.get("cron"), zone, now=datetime.now(UTC),
                                             gated=bool(values["gate_command"]), base_interval=self.min_interval)
            values.update(cron=rule.text, timezone=zone)
            limits = _concurrency_limits(entry.get("task_limits") or {}, field="task_limits")
        except ValueError as exc:
            raise _Skip(str(exc)) from None
        except RequestError as exc:
            raise _Skip(str(exc)) from None
        target = entry.get("notify_target")
        values["notify_channel"] = values["notify_chat"] = None
        if target is not None:
            target = _mapping(target, "notify_target")
            if self._notify_allowed(target.get("channel"), target.get("chat")):
                values["notify_channel"], values["notify_chat"] = target["channel"], target["chat"]
            else:
                self.plan.notes.append(f"schedule {label}{values['name']}: {target.get('channel')} chat "
                                       f"{target.get('chat')!r} is not in this machine's sender allowlist, so it "
                                       "tells the dashboard only; add the sender in Settings → Channels, then pick it")
        profiles: dict[str, tuple[str, str]] = {}
        for role, name in _mapping(entry.get("profiles") or {}, "profiles").items():
            if role not in TASK_PROFILE_ROLES:
                raise _Skip(f"profiles: a schedule chooses the Agent Profile of its {', '.join(TASK_PROFILE_ROLES)} "
                            f"only, not {role!r}")
            if not isinstance(name, str) or name in self.blocked_profiles or name not in self.profiles:
                raise _Skip(f"profiles.{role}: no Agent Profile {name!r} here or in the file")
            profile_id, spec = self.profiles[name]
            try:
                check_roles(spec, (role,))
            except RegistryError as exc:
                raise _Skip(f"profiles.{role}: {exc}") from None
            profiles[role] = (name, profile_id)
        return values, profiles, {role: limit for role, limit in limits.items() if limit is not None}

    async def schedules(self, entries: Any, project_id: str, label: str, *,
                        manager_entries: Sequence[Mapping[str, Any]] = ()) -> None:
        """Add or rewrite the project's schedules of the file, disabled; leave those whose settings match."""
        from ..db import schedules as db_schedules

        if not isinstance(entries, list):
            self.record("project.schedules", f"{label}schedules", Action.SKIPPED, reason="schedules is not a list")
            return
        stored = {row["name"].lower(): row for row in self.target.schedules.get(project_id, [])}
        names = {row["id"]: name for name, row in self.target.profiles.items()}
        managers = {identity: row for identity, row in self.target.managers.items() if row["deleted_at"] is None}
        managers.setdefault("manager", {})
        for manager in manager_entries:
            identity = manager["id"]
            managers[identity] = {**managers.get(identity, {}), **manager}
        seen: set[str] = set()
        for entry in entries:
            raw_name = entry.get("name") if isinstance(entry, Mapping) else None
            try:
                values, profiles, limits = self._schedule_values(entry, label, managers)
                if values["name"].lower() in seen:
                    raise _Skip("the file gives this schedule twice")
                seen.add(values["name"].lower())
            except _Skip as exc:
                self.record("project.schedules", f"{label}{raw_name}", Action.SKIPPED, reason=str(exc))
                continue
            key = f"{label}{values['name']}"
            row = stored.get(values["name"].lower())
            if row is not None and row["manager_id"] != values["manager_id"]:
                self.record("project.schedules", key, Action.SKIPPED,
                            reason="Manager owner conflict: an existing schedule's owner is immutable")
                continue
            from .schedule_templates import merge_selection
            from .task_templates import TaskTemplate, check_values, render_requirement, TemplateError
            try:
                values.update(merge_selection(row or {}, values))
                template_key = values["template_key"]
                raw_template = self.schedule_templates.get((values["manager_id"], template_key)) if template_key else None
                if template_key and raw_template is None:
                    raise TemplateError(f"Manager {values['manager_id']} has no template {template_key!r}")
                if not template_key and (values["template_inputs"] or
                                        managers[values["manager_id"]].get("task_policy") == "templates_only"):
                    raise TemplateError("template inputs and this Manager's policy require a template_key")
                if raw_template is not None:
                    template = TaskTemplate.from_row(raw_template)
                    checked_values = await check_values(template, values["template_inputs"])
                    from .schedule_templates import template_directory
                    from .task_template_execution import fixed_profiles
                    template_directory(template, {"project_id": project_id, "work_dir": (row or {}).get("work_dir")})
                    fixed_profiles(template, {role: identity for role, (_, identity) in profiles.items()})
                    from .schedules import REQUIREMENT_MAX_CHARS
                    if len(render_requirement(template, checked_values, values["requirement"])) > REQUIREMENT_MAX_CHARS:
                        raise TemplateError("the rendered schedule requirement is too long")
            except ValueError as exc:
                self.record("project.schedules", key, Action.SKIPPED, reason=str(exc))
                continue
            new = {**values, "profiles": {role: name for role, (name, _) in profiles.items()}, "task_limits": limits}
            profile_ids = {role: profile_id for role, (_, profile_id) in profiles.items()}
            current = None
            if row is not None:
                current = {name: row[name] for name in (*_SCHEDULE_FIELDS, "run_timeout_mode", "notify_channel",
                                                        "notify_chat", "manager_id", "template_key", "template_inputs")}
                current["profiles"] = {role: names.get(pid, pid) for role, pid in row["profile_overrides"].items()}
                current["task_limits"] = dict(row["task_limits"])
            changed = _differs(new, current, new)
            if self.apply and row is not None and changed:
                if error := await db_schedules.owner_error(self.conn, values["manager_id"], enabled=False,
                                                           template_key=values.get("template_key")):
                    self.record("project.schedules", key, Action.SKIPPED, reason=error)
                    continue
            action = self.outcome("project.schedules", key, current, changed)
            if action in (Action.ADDED, Action.CHANGED):
                self.disabled_schedules.append(key)
                if isinstance(entry, Mapping) and entry.get("repoint") and row is None:
                    self.repointed_schedules.append(key)
            if not self.apply or action not in (Action.ADDED, Action.CHANGED):
                continue
            if row is None:
                await db_schedules.create_schedule(self.conn, project_id=project_id, fields=values,
                                                   manager_id=values["manager_id"],
                                                   profiles=profile_ids, task_limits=limits, created_via="import")
            else:
                fields = {name: value for name, value in values.items() if name != "manager_id"}
                fields.update(enabled=False, next_fire_at=None)
                if (values["cron"], values["timezone"]) != (row["cron"], row["timezone"]):
                    fields["rule_changed_at"] = datetime.now(UTC)
                await db_schedules.update_schedule(self.conn, row["id"], fields, profiles=profile_ids,
                                                   task_limits=limits)

    def schedule_notes(self) -> None:
        if self.disabled_schedules:
            self.plan.notes.append(
                f"{len(self.disabled_schedules)} schedule(s) come in disabled ({', '.join(self.disabled_schedules)}): "
                "check each one's directory and gate command on this machine, then enable it")
        if self.repointed_schedules:
            self.plan.notes.append(
                f"{', '.join(self.repointed_schedules)}: had a directory of its own, which is not carried; it runs in "
                "its project's directory until you set one")


async def import_config(conn: Any, data: Mapping[str, Any], *, apply: bool) -> Plan:
    """The import's plan for ``data`` (from ``read_file``); with ``apply`` also written, in the caller's
    transaction. Raises ``ConfigFileError`` when the result would break a catalog cap."""
    _check_manager_document(data)
    _check_channel_document(data)
    if apply:
        from ..db.connection import ensure_schema, schema_change_pending

        if await schema_change_pending(conn):
            raise SchemaChangePending(
                "the database schema is older than this Looper; start `looper serve` once (it backs the database up "
                "before changing the schema), then import again")
        await ensure_schema(conn)
        # Take the same locks as registry writes before reading target rows.
        # The outer apply transaction uses READ COMMITTED, so a lock wait
        # cannot leave conversion on a snapshot from before another writer.
        await conn.execute("LOCK TABLE llm_providers, llm_provider_endpoints, llm_models, agent_profiles "
                           "IN SHARE ROW EXCLUSIVE MODE")
        # Firing locks a schedule before its owner. Import takes those locks before locking Managers, too.
        schedule_projects, schedule_names = [], []
        for project in data.get("projects", []):
            if (not isinstance(project, Mapping) or not isinstance(project.get("name"), str)
                    or not isinstance(project.get("schedules"), list)):
                continue
            for schedule in project["schedules"]:
                if isinstance(schedule, Mapping) and isinstance(schedule.get("name"), str):
                    schedule_projects.append(project["name"].strip().lower())
                    schedule_names.append(schedule["name"].strip().lower())
        if schedule_names:
            await conn.fetch("SELECT s.id FROM schedules s JOIN projects p ON p.id = s.project_id "
                             "JOIN unnest($1::text[], $2::text[]) AS incoming(project, schedule) "
                             "ON lower(p.name) = incoming.project AND lower(s.name) = incoming.schedule "
                             "ORDER BY s.project_id, s.id FOR UPDATE OF s", schedule_projects, schedule_names)
        if data.get("channels"):
            from .channels.bot_api import _LOCK

            await conn.execute(_LOCK)
        if data.get("managers"):
            from ..db import managers as db_managers

            await db_managers.lock_managers(conn)
            # Every Manager writer takes the global limit lock before these target row locks.
            for identity in sorted(entry["id"] for entry in data["managers"]
                                   if isinstance(entry, Mapping) and isinstance(entry.get("id"), str)):
                await db_managers.lock_for_update(identity, conn=conn)
        if data.get("format") == V4_FORMAT and data.get("channels"):
            for identity in sorted({entry.get("manager_id", "manager") for entry in data["channels"]}):
                await conn.fetchrow("SELECT id FROM managers WHERE id = $1 FOR KEY SHARE", identity)
        if data.get("managers") or data.get("format") == V4_FORMAT and data.get("channels"):
            # Resolve the projected dependencies and reject the complete graph before any import writes.
            await import_config(conn, data, apply=False)
    if data.get("format") in V1_FORMATS and "agents" in data:
        data = _from_v1(data)
    importer = _Importer(conn, await _Target.read(conn), apply)
    importer.channel_graph = data.get("format") == V4_FORMAT
    importer.imported_managers = {entry.get("id") for entry in data.get("managers", [])
                                  if isinstance(entry, Mapping) and isinstance(entry.get("id"), str)}
    for entry in data.get("runtimes", []):
        await importer.runtime(entry)
    data = {**data, **{section: importer.registry_entries(section, data.get(section, []))
                      for section in ("providers", "models", "profiles")}}
    if data.get("format") in (*V1_FORMATS, V2_FORMAT):
        data = importer.legacy(data)
    for entry in data.get("providers", []):
        await importer.provider(entry)
    for entry in data.get("models", []):
        await importer.model(entry)
    for entry in data.get("profiles", []):
        await importer.profile(entry)
    settings = data.get("settings", {})
    for entry in settings.get("layers", []):
        await importer.layer(entry, None)
    for entry in settings.get("roles", []):
        await importer.role(entry)
    if "scheduler" in settings:
        await importer.scheduler(settings["scheduler"])
    if any(isinstance(entry, Mapping) and "max_concurrency" in entry for entry in settings.get("roles", [])):
        importer.plan.notes.append(
            "settings.roles: a role's max_concurrency is no longer a role setting and was ignored; how many of a "
            "task's Workers and Reviewers run at once is the task's own limit, else the max_concurrency of their "
            "role layers")
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
        await importer.role_templates(entry.get("role_templates") or [], project_id, label)
    await importer.managers(data.get("managers", []))
    await importer.manager_installs(data.get("managers", []))
    for entry in data.get("channels", []):
        await importer.channel(entry)
    if importer.channel_graph:
        await importer.finish_channels()
    for entry, project_id, label in projects:
        await importer.schedules(entry.get("schedules") or [], project_id, label,
                                 manager_entries=data.get("managers", []))
    importer.schedule_notes()
    await importer.settle_layers()
    importer.check_caps()

    for entry in data.get("installs", []):
        await importer.install(entry, None)
    for entry, project_id, label in projects:
        for install in entry.get("installs") or ():
            await importer.install(install, project_id, label)
    return importer.plan


async def run_import(conn: Any, data: Mapping[str, Any], *, apply: bool) -> Plan:
    """Preview ``data`` in a read-only transaction, or apply it in one transaction, on ``conn``: what both
    v1 and the dashboard's import run."""
    async with conn.transaction(isolation="read_committed" if apply else "repeatable_read", readonly=not apply):
        plan = await import_config(conn, data, apply=apply)
    if apply:
        from .managers_api import _on_change

        touched = {change.key.split("/", 1)[0] for change in plan.changes
                   if change.section in ("managers", "manager.templates")
                   and change.action in (Action.ADDED, Action.CHANGED)}
        for identity in sorted(touched):
            await _on_change(identity)
    return plan


async def import_file(dsn: str, path: Path, *, apply: bool) -> Plan:
    """Preview (read-only) or apply (one transaction) the file at ``path`` on the database at ``dsn``."""
    import asyncpg

    data = read_file(path)
    conn = await (asyncpg.connect(dsn) if apply else _read_only(dsn))
    try:
        return await run_import(conn, data, apply=apply)
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
        lines += [f"  {n.section} {n.key}: {', '.join(n.names)}" for n in plan.need_secret]
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
    lines = [f"Wrote {path} ({summary.bytes} bytes, {summary.format}): {counts}; {secrets}."]
    if summary.repoint:
        lines.append(f"{summary.repoint} path document(s) are references to re-point on the new machine.")
    lines.append(f"On the new machine: loopercli config import {path.name} (preview), then "
                 "loopercli config import --apply <import_id> and loopercli confirm <confirmation_id>.")
    return "\n".join(lines)
