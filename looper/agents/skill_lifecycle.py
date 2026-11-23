"""Deprecate and restore a project skill, and the displayed size of a run's skill list.

Deprecation changes only ``library_items.lifecycle``. Installs, versions and
files stay. A new run skips a deprecated skill; a run already going keeps
what it loaded at the start. The character budget is a display threshold.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any


class Lifecycle(StrEnum):
    ACTIVE = "active"
    DEPRECATED = "deprecated"


LIMIT = 8000
ROLES = ("planner", "worker", "reviewer")
REASON_MAX = 500


def partition_deprecated(rows: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], tuple[str, ...]]:
    """Drop rows whose lifecycle is exactly ``deprecated``. A missing value counts as active.

    Kept rows are copies without the ``lifecycle`` key, so a loaded item's view
    stays the fields it had before this column. Skipped names are unique and
    keep first-seen order. Any other value is kept.
    """
    kept: list[dict[str, Any]] = []
    skipped: list[str] = []
    seen: set[str] = set()
    for row in rows:
        data = dict(row)
        lifecycle = data.pop("lifecycle", Lifecycle.ACTIVE.value)
        if lifecycle == Lifecycle.DEPRECATED.value:
            name = str(data.get("name") or "")
            if name and name not in seen:
                seen.add(name)
                skipped.append(name)
            continue
        kept.append(data)
    return kept, tuple(skipped)


def _reason(reason: str) -> str:
    """Check a reason. It is not stored on the skill. The operator action record keeps the call's arguments."""
    from .project_skills import ProjectSkillError

    if not isinstance(reason, str):
        raise ProjectSkillError("reason must be a string")
    text = reason.strip()
    if len(text) > REASON_MAX:
        raise ProjectSkillError(f"a reason has at most {REASON_MAX} characters, got {len(text)}")
    return text


async def _set(project_id: str, skill_id: str, lifecycle: Lifecycle, *, reason: str = "") -> dict[str, Any]:
    from ..db import library as db_library
    from ..db import skill_versions as store
    from ..db.project_documents import locked_project
    from .project_skills import ProjectSkillNotFound, _answer, _owned, skill_view

    _reason(reason)
    async with locked_project(project_id) as (conn, project):
        if project is None:
            raise ProjectSkillNotFound(f"project {project_id!r} not found")
        row = _owned(await db_library.get_item(skill_id, pool=conn), project_id, skill_id)
        if row["lifecycle"] != lifecycle.value:
            await conn.execute(
                "UPDATE library_items SET lifecycle = $2, updated_at = now() WHERE id = $1",
                skill_id, lifecycle.value,
            )
            row = await db_library.get_item(skill_id, pool=conn)
            assert row is not None
        affected = await store.affected_projects(conn, skill_id)
    return {**await _answer(project_id, skill=skill_view(row)), "affected_projects": affected}


async def deprecate(project_id: str, skill_id: str, *, reason: str = "") -> dict[str, Any]:
    """Mark a project skill deprecated. Doing it again returns the same view."""
    return await _set(project_id, skill_id, Lifecycle.DEPRECATED, reason=reason)


async def restore(project_id: str, skill_id: str) -> dict[str, Any]:
    """Mark a project skill active. Restoring an active skill returns the same view."""
    return await _set(project_id, skill_id, Lifecycle.ACTIVE)


async def context_budget(project_id: str) -> dict[str, Any]:
    """Characters of skill name plus description a new run of each role would list.

    The same rows ``load_installed`` keeps for planner, worker and reviewer,
    after ``resolve_installed``, and only catalog skills. Plugin-bundled skills
    are not counted. A task role's ``items`` are not in this total: D13 counts
    what every run of that role lists, and those extras are named per task role.
    ``over`` is strictly above ``LIMIT``; nothing is refused.
    """
    from ..db import library as db_library
    from ..db import projects
    from .library import ItemKind, resolve_installed
    from .project_skills import ProjectSkillNotFound

    if await projects.get_project(project_id) is None:
        raise ProjectSkillNotFound(f"project {project_id!r} not found")
    roles: dict[str, dict[str, Any]] = {}
    for role in ROLES:
        rows, _skipped = partition_deprecated(await db_library.list_installed(role, project_id))
        resolved = resolve_installed(rows, project_id)
        skills = [(row, item) for row, item in resolved if item.kind is ItemKind.SKILL]
        chars = sum(len(item.name) + len(str(row.get("description") or "")) for row, item in skills)
        roles[role] = {"chars": chars, "skills": len(skills), "over": chars > LIMIT}
    return {"limit": LIMIT, "roles": roles, "counts_plugin_skills": False}
