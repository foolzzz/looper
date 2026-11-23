"""Versions of a project skill: append a row, roll back by appending, and keep a tombstone after delete.

Only ``project_skills`` add, update and delete write these rows. An upload, a
git import, a machine import, a Settings skill and ``catalog`` update do not.
``proposal`` marks a version written by a confirmed skill proposal (``skill_proposals``), with its id as ``author_ref``.
"""

from __future__ import annotations

import difflib
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

from ..db import skill_versions as store

RETENTION = timedelta(days=90)


class AuthorKind(str, Enum):
    """Who wrote a version. The database check allows the same three values."""

    OPERATOR = "operator"
    PROPOSAL = "proposal"
    ROLLBACK = "rollback"


def _body(name: str, description: str, content: str) -> str:
    """The body ``skill_file`` writes, so a later ``skill_md`` of it hashes the same."""
    from .project_skills import skill_body, skill_file

    return skill_body(skill_file(name, description, content), name, description)


async def append(
    conn: Any, *, item_id: str, name: str, description: str, content: str, sha256: str,
    author: AuthorKind, author_ref: str | None, reason: str,
) -> int:
    """Append the next version. The caller moves ``library_items.skill_version`` in the same transaction."""
    from .project_skills import ProjectSkillError, skill_md

    content = _body(name, description, content)
    _, computed = skill_md(name, description, content)
    if computed != sha256:
        raise ProjectSkillError("the version hash does not match its content")
    number = await store.next_number(conn, item_id)
    await store.insert_version(
        conn, item_id=item_id, version=number, name=name, description=description, content=content,
        sha256=sha256, author_kind=author.value, author_ref=author_ref, reason=reason,
    )
    return number


async def point(conn: Any, item_id: str, version: int) -> None:
    await store.point(conn, item_id, version)


async def snapshot_if_missing(conn: Any, row: Any) -> None:
    """Keep one version when a skill that never got one is deleted. The delete then marks it a tombstone."""
    if await store.has_versions(conn, row["id"]):
        return
    from ..db import library as db_library
    from .project_skills import SKILL_FILE, skill_body

    text = await db_library.get_file(row["id"], SKILL_FILE, pool=conn) or ""
    await store.insert_version(
        conn, item_id=row["id"], version=1, name=row["name"], description=row["description"],
        content=skill_body(text, row["name"], row["description"]), sha256=row["sha256"],
        author_kind=AuthorKind.OPERATOR.value, author_ref=None, reason="",
    )


def _public(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": int(row["version"]),
        "name": row["name"],
        "description": row["description"],
        "sha256": row["sha256"],
        "author_kind": AuthorKind(row["author_kind"]).value,
        "author_ref": row["author_ref"],
        "reason": row["reason"],
        "created_at": row["created_at"],
    }


def _diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    return {
        "from_version": int(before["version"]),
        "to_version": int(after["version"]),
        "name": {"from": before["name"], "to": after["name"]},
        "description": {"from": before["description"], "to": after["description"]},
        "content": "".join(difflib.unified_diff(
            str(before["content"]).splitlines(keepends=True),
            str(after["content"]).splitlines(keepends=True),
            fromfile=f"version {before['version']}",
            tofile=f"version {after['version']}",
        )),
    }


async def versions(
    project_id: str, skill_id: str, *, from_version: int | None = None, to_version: int | None = None,
) -> dict[str, Any]:
    """One project skill's versions, without their bodies, and the difference of two versions when both are given."""
    from ..db import library as db_library
    from ..db.connection import get_pool
    from .project_skills import ProjectSkillError, _owned

    if (from_version is None) != (to_version is None):
        raise ProjectSkillError("from_version and to_version are given together")
    pool = get_pool()
    _owned(await db_library.get_item(skill_id, pool=pool), project_id, skill_id)
    rows = await store.list_versions(pool, skill_id)
    by_number = {int(row["version"]): row for row in rows}
    diff = None
    if from_version is not None and to_version is not None:
        for number in (from_version, to_version):
            if number not in by_number:
                raise ProjectSkillError(f"no version {number}")
        diff = _diff(by_number[from_version], by_number[to_version])
    return {
        "skill_id": skill_id,
        "current_version": await store.pointer(pool, skill_id),
        "versions": [_public(row) for row in rows],
        "affected_projects": await store.affected_projects(pool, skill_id),
        "diff": diff,
    }


async def rollback(project_id: str, skill_id: str, version: int) -> dict[str, Any]:
    """Append a new version whose content and hash are an older version's, then point the item at it."""
    import asyncpg

    from ..db import library as db_library
    from ..db.project_documents import locked_project
    from .project_skills import (
        ProjectSkillNotFound, ProjectSkillError, _answer, _check_rename, _name_taken, _owned, skill_md, skill_view,
    )

    async with locked_project(project_id) as (conn, project):
        if project is None:
            raise ProjectSkillNotFound(f"project {project_id!r} not found")
        row = _owned(await db_library.get_item(skill_id, pool=conn), project_id, skill_id)
        target = await store.get_version(conn, skill_id, version)
        if target is None:
            raise ProjectSkillError(f"no version {version}")
        if await store.pointer(conn, skill_id) == version:
            raise ProjectSkillError("that version is already current")
        name, description, content = target["name"], target["description"], target["content"]
        await _check_rename(conn, project_id, skill_id, row, name)
        file, sha256 = skill_md(name, description, content)
        if sha256 != target["sha256"]:
            raise ProjectSkillError("the stored version does not match its content hash")
        number = await append(
            conn, item_id=skill_id, name=name, description=description, content=content, sha256=sha256,
            author=AuthorKind.ROLLBACK, author_ref=str(version), reason="",
        )
        try:
            row = await db_library.update_owned_skill(
                conn, skill_id, name=name, description=description, file=file, sha256=sha256, skill_version=number,
            )
        except asyncpg.UniqueViolationError:  # pragma: no cover - the project's row lock orders the rename
            raise _name_taken(name) from None
        affected = await store.affected_projects(conn, skill_id)
    return {**await _answer(project_id, skill=skill_view(row)), "version": number, "affected_projects": affected}


async def purge_tombstones(conn: Any, *, now: datetime) -> int:
    """Delete tombstones older than ``RETENTION``. A failure leaves them for the next housekeeping pass."""
    return await store.purge_before(conn, now - RETENTION)


async def backfill(conn: Any) -> int:
    """Version 1 for each project skill that has a ``SKILL.md`` and no version yet. The item hash is copied."""
    from .project_skills import SKILL_FILE, skill_body

    rows = await conn.fetch(
        "SELECT i.id, i.name, i.description, i.sha256, f.content FROM library_items i "
        "JOIN library_files f ON f.item_id = i.id AND f.path = $1 "
        "WHERE i.origin = 'project' AND i.kind = 'skill' "
        "AND NOT EXISTS (SELECT 1 FROM skill_versions v WHERE v.item_id = i.id)",
        SKILL_FILE,
    )
    for row in rows:
        await store.insert_version(
            conn, item_id=row["id"], version=1, name=row["name"], description=row["description"],
            content=skill_body(row["content"], row["name"], row["description"]), sha256=row["sha256"],
            author_kind=AuthorKind.OPERATOR.value, author_ref=None, reason="imported",
        )
        await conn.execute(
            "UPDATE library_items SET skill_version = 1 WHERE id = $1 AND skill_version IS NULL", row["id"],
        )
    return len(rows)
