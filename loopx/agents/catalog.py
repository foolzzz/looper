"""The capability catalog's API: list and read its items, and install them (RFC decision 9).

Acquiring and installing are separate. **Acquiring** puts an item into the
catalog (``library_items``): imported from this machine
(``library.import_from_machine``) or written in a project
(``project_skills``); its ``origin`` says which (``Origin``). An item does
nothing by itself. **Installing** puts it on a scope (``library_installs``):
``global``, or one project, for some roles (``ROLES``). A project install never
names the Manager, which spans every project, and a project's own skill is
installed in projects only. Every agent run loads the enabled installs naming
its role, global or in its project (``library.load_installed``), from its next
run on; individual Workers or Reviewers are never configured.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from enum import Enum
from typing import Any

ROLES = ("manager", "planner", "worker", "reviewer")
TASK_ROLES = ("planner", "worker", "reviewer")
_INSTALL_FIELDS = frozenset({"item_id", "scope", "project_id", "roles", "enabled"})
_CHANGE_FIELDS = frozenset({"roles", "enabled"})


class Scope(str, Enum):
    GLOBAL = "global"
    PROJECT = "project"


class Origin(str, Enum):
    """How an item was acquired. Only ``machine`` and ``project`` are acquired so far."""

    MACHINE = "machine"
    UPLOAD = "upload"
    GIT = "git"
    UI = "ui"
    PROJECT = "project"


class CatalogError(ValueError):
    """A request the catalog refuses (answered with 400)."""


class CatalogNotFound(LookupError):
    """No such item, install or project (answered with 404)."""


class CatalogConflict(Exception):
    """The item is installed there already, or its name is taken there (answered with 409)."""


def scope_roles(scope: Scope) -> tuple[str, ...]:
    """The roles an install on ``scope`` may name."""
    return ROLES if scope is Scope.GLOBAL else TASK_ROLES


def install_roles(raw: Any, scope: Scope) -> list[str]:
    """The roles of an install: a non-empty list from ``scope_roles``, kept in ``ROLES`` order."""
    allowed = scope_roles(scope)
    if not isinstance(raw, list) or not raw or not all(isinstance(role, str) for role in raw):
        raise CatalogError(f"roles is a non-empty list of {', '.join(allowed)}")
    unknown = [role for role in raw if role not in allowed]
    if unknown:
        if unknown[0] == "manager":
            raise CatalogError("a project install cannot include the manager: the Manager spans every project")
        raise CatalogError(f"unknown role {unknown[0]!r}; the roles are {', '.join(allowed)}")
    return [role for role in ROLES if role in raw]


def _scope(raw: Any) -> Scope:
    try:
        return Scope(raw)
    except ValueError:
        raise CatalogError("scope is 'global' or 'project'") from None


def _enabled(raw: Any) -> bool:
    if not isinstance(raw, bool):
        raise CatalogError("enabled must be true or false")
    return raw


def qualified_name(row: Mapping[str, Any]) -> str:
    """How agents name the item: a plugin by its directory, a skill as ``<plugin>:<name>``."""
    from .library import ItemKind, directory_name, skill_plugin

    if row["kind"] == ItemKind.PLUGIN.value:
        return directory_name(ItemKind.PLUGIN, row["name"])
    return f"{skill_plugin(row['origin'])}:{row['name']}"


def install_view(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "install_id": row["id"],
        "item_id": row["item_id"],
        "scope": row["scope"],
        "project_id": row["project_id"],
        "project_name": row["project_name"],
        "roles": list(row["roles"]),
        "enabled": bool(row["enabled"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def item_view(row: Mapping[str, Any], installs: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """An item without its files, and where it is installed."""
    return {
        "item_id": row["id"],
        "kind": row["kind"],
        "name": row["name"],
        "qualified_name": qualified_name(row),
        "origin": row["origin"],
        "owner_project_id": row["owner_project_id"],
        "owner_project_name": row["owner_project_name"],
        "description": row["description"],
        "source": row["source"],
        "version": row["version"],
        "sha256": row["sha256"],
        "size_bytes": int(row["size_bytes"]),
        "file_count": int(row["file_count"]),
        "imported_at": row["imported_at"],
        "updated_at": row["updated_at"],
        "installs": [install_view(install) for install in installs],
    }


async def _item(item_id: str) -> dict[str, Any]:
    """The item's view with its installs; raises ``CatalogNotFound``."""
    from ..db import library as db_library

    row = await db_library.get_item(item_id)
    if row is None:
        raise CatalogNotFound(f"no catalog item {item_id!r}")
    return item_view(row, await db_library.list_installs(item_ids=[item_id]))


async def catalog_view() -> dict[str, Any]:
    """Every item with its installs, the last import's report and the caps."""
    from ..db import library as db_library
    from ..db import system_settings
    from .library import (
        FILE_MAX_BYTES, ITEM_MAX_BYTES, ITEM_MAX_FILES, LAST_IMPORT_KEY, LIBRARY_MAX_BYTES,
    )

    rows = await db_library.list_items()
    by_item: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for install in await db_library.list_installs():
        by_item[install["item_id"]].append(install)
    stored = await system_settings.get_settings([LAST_IMPORT_KEY])
    try:
        last_import = json.loads(stored[LAST_IMPORT_KEY]) if LAST_IMPORT_KEY in stored else None
    except ValueError:
        last_import = None
    return {
        "items": [item_view(row, by_item.get(row["id"], ())) for row in rows],
        "installed_count": sum(1 for row in rows if by_item.get(row["id"])),
        "total_bytes": sum(int(row["size_bytes"]) for row in rows),
        "last_import": last_import,
        "roles": list(ROLES),
        "task_roles": list(TASK_ROLES),
        "caps": {
            "file_max_bytes": FILE_MAX_BYTES, "item_max_bytes": ITEM_MAX_BYTES, "item_max_files": ITEM_MAX_FILES,
            "library_max_bytes": LIBRARY_MAX_BYTES,
        },
    }


async def item_detail(item_id: str) -> dict[str, Any]:
    """One item with its installs, its files (no content) and, for a skill, its ``SKILL.md``."""
    from ..db import library as db_library

    item = await _item(item_id)
    files = await db_library.get_file_listing(item_id)
    skill_md = await db_library.get_file(item_id, "SKILL.md") if item["kind"] == "skill" else None
    return {
        "item": item,
        "files": [{"path": f["path"], "size_bytes": int(f["size_bytes"]), "executable": bool(f["executable"])}
                  for f in files],
        "skill_md": skill_md,
    }


def _unknown(data: Mapping[str, Any], fields: frozenset[str]) -> None:
    unknown = sorted(set(data) - fields)
    if unknown:
        raise CatalogError(f"unknown field {unknown[0]!r}; the fields are {', '.join(sorted(fields))}")


async def name_conflict(conn: Any, project_id: str, item_id: str | None, name: str) -> str | None:
    """Why a project-authored skill named ``name`` cannot load in ``project_id``: another one there has the name.

    Both would load as ``project:<name>``. None when the name is free there.
    """
    from ..db import library as db_library

    for other in await db_library.project_skill_names(project_id, pool=conn):
        if other["id"] != item_id and other["name"] == name:
            return (f"project {project_id!r} already has a project skill named {name!r}; "
                    f"both would load as project:{name}")
    return None


async def install(data: Any) -> dict[str, Any]:
    """Install an item: ``{item_id, scope, project_id (a project scope), roles?, enabled?}``.

    ``roles`` defaults to every role the scope allows. Returns the install and the item.
    """
    import asyncpg

    from ..db import library as db_library
    from ..db.project_documents import locked_project

    if not isinstance(data, Mapping):
        raise CatalogError("body must be a JSON object")
    _unknown(data, _INSTALL_FIELDS)
    if not isinstance(data.get("item_id"), str) or not data["item_id"]:
        raise CatalogError("give the item_id of the catalog item to install")
    scope = _scope(data.get("scope"))
    roles = install_roles(data["roles"], scope) if "roles" in data else list(scope_roles(scope))
    enabled = _enabled(data["enabled"]) if "enabled" in data else True
    project_id = data.get("project_id")
    if scope is Scope.PROJECT and (not isinstance(project_id, str) or not project_id):
        raise CatalogError("a project install needs a project_id")
    if scope is Scope.GLOBAL and project_id is not None:
        raise CatalogError("a global install has no project_id")
    item = await db_library.get_item(data["item_id"])
    if item is None:
        raise CatalogNotFound(f"no catalog item {data['item_id']!r}")
    if item["origin"] == Origin.PROJECT.value and scope is Scope.GLOBAL:
        raise CatalogError("a project's own skill is installed in projects only, not globally")
    taken = CatalogConflict("the item is installed there already; change that install instead")
    try:
        if scope is Scope.PROJECT:
            async with locked_project(project_id) as (conn, project):
                if project is None:
                    raise CatalogNotFound(f"project {project_id!r} not found")
                if item["origin"] == Origin.PROJECT.value and (
                        reason := await name_conflict(conn, project_id, item["id"], item["name"])):
                    raise CatalogConflict(reason)
                row = await db_library.insert_install(conn, item_id=item["id"], scope=scope.value,
                                                      project_id=project_id, roles=roles, enabled=enabled)
        else:
            from ..db.connection import get_pool

            row = await db_library.insert_install(get_pool(), item_id=item["id"], scope=scope.value, project_id=None,
                                                  roles=roles, enabled=enabled)
    except asyncpg.UniqueViolationError:
        raise taken from None
    return {"install": install_view(row), "item": await _item(item["id"])}


async def update_install(install_id: str, data: Any) -> dict[str, Any]:
    """Change an install's ``roles`` or ``enabled``; returns the install and the item."""
    from ..db import library as db_library

    if not isinstance(data, Mapping):
        raise CatalogError("body must be a JSON object")
    _unknown(data, _CHANGE_FIELDS)
    if not data:
        raise CatalogError("give roles or enabled")
    current = await db_library.get_install(install_id)
    if current is None:
        raise CatalogNotFound(f"no install {install_id!r}")
    roles = install_roles(data["roles"], Scope(current["scope"])) if "roles" in data else None
    enabled = _enabled(data["enabled"]) if "enabled" in data else None
    row = await db_library.update_install(install_id, roles=roles, enabled=enabled)
    if row is None:
        raise CatalogNotFound(f"no install {install_id!r}")
    return {"install": install_view(row), "item": await _item(row["item_id"])}


async def uninstall(install_id: str) -> dict[str, Any]:
    """Remove an install; the item stays in the catalog. Returns the item."""
    from ..db import library as db_library

    current = await db_library.get_install(install_id)
    if current is None or not await db_library.delete_install(install_id):
        raise CatalogNotFound(f"no install {install_id!r}")
    return {"deleted": install_id, "item": await _item(current["item_id"])}


async def project_view(project_id: str) -> dict[str, Any]:
    """What applies to a project: its own installs and the global ones that reach its task roles.

    Each entry is an install with its item; ``inherited`` marks a global one,
    which is changed in Settings. ``owned_count`` is how many skills the
    project wrote (``project_skills``) and ``skill_caps`` their caps.
    """
    from ..db import library as db_library
    from ..db import projects as db_projects
    from .project_skills import skill_caps

    if await db_projects.get_project(project_id) is None:
        raise CatalogNotFound(f"project {project_id!r} not found")
    installs = [row for row in await db_library.list_installs(project_id=project_id)
                if row["scope"] == Scope.PROJECT.value or set(row["roles"]) & set(TASK_ROLES)]
    items = {row["id"]: row for row in await db_library.list_items()}
    entries = [{**install_view(row), "inherited": row["scope"] == Scope.GLOBAL.value,
                "item": item_view(items[row["item_id"]])} for row in installs if row["item_id"] in items]
    # The project's own installs first, then the inherited ones; each group plugins first, then by name.
    entries.sort(key=lambda e: (e["inherited"], e["item"]["kind"], e["item"]["name"]))
    owned = [row for row in items.values() if row["owner_project_id"] == project_id]
    return {
        "project_id": project_id,
        "installs": entries,
        "owned_count": len(owned),
        "task_roles": list(TASK_ROLES),
        "skill_caps": skill_caps(),
    }
