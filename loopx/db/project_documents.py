"""The ``project_documents`` table: a project's knowledge, and ``project_document_drafts``: drafts of it.

``loopx.agents.knowledge`` owns what a document is (validation, caps, the
index, materialization); this module stores and reads it. A text or upload
document holds its ``content``; a path document holds its ``path``, relative
to its project's default directory. A draft holds a text document's title,
summary and content until the user confirms or discards it; no run reads it.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import asyncpg

from .connection import get_pool

# Every column but the content, which can be 2 MiB: it is read only to materialize a document.
_COLUMNS = (
    "id, project_id, title, summary, source_kind, filename, path, size_bytes, sha256, created_at, updated_at"
)
_UPDATABLE = ("title", "summary", "filename", "content", "path", "size_bytes", "sha256")


@asynccontextmanager
async def locked_project(
    project_id: str, *, pool: asyncpg.Pool | None = None,
) -> AsyncIterator[tuple[asyncpg.Connection, dict[str, Any] | None]]:
    """A connection in a transaction that holds the project's row lock, and the project (None when there is none).

    Changes to one project's documents go through it, so two of them cannot
    both pass a cap that only one of them fits.
    """
    pool = pool or get_pool()
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow("SELECT id, name, work_dir FROM projects WHERE id = $1 FOR UPDATE", project_id)
        yield conn, dict(row) if row is not None else None


async def list_documents(
    project_id: str, *, pool: asyncpg.Pool | asyncpg.Connection | None = None,
) -> list[dict[str, Any]]:
    """The project's documents without their content, oldest first."""
    pool = pool or get_pool()
    rows = await pool.fetch(
        f"SELECT {_COLUMNS} FROM project_documents WHERE project_id = $1 ORDER BY created_at, id", project_id,
    )
    return [dict(row) for row in rows]


async def get_content(document_id: str, *, pool: asyncpg.Pool | None = None) -> str | None:
    """A text or upload document's content; None for a path document or no such document."""
    pool = pool or get_pool()
    return await pool.fetchval("SELECT content FROM project_documents WHERE id = $1", document_id)


async def insert_document(
    conn: asyncpg.Connection,
    *,
    project_id: str,
    title: str,
    summary: str,
    source_kind: str,
    size_bytes: int,
    sha256: str,
    filename: str = "",
    content: str | None = None,
    path: str | None = None,
) -> dict[str, Any]:
    """Add a document; returns its row without the content."""
    row = await conn.fetchrow(
        f"""
        INSERT INTO project_documents (id, project_id, title, summary, source_kind, filename, content, path,
                                       size_bytes, sha256)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        RETURNING {_COLUMNS}
        """,
        f"doc-{uuid.uuid4().hex[:12]}", project_id, title, summary, source_kind, filename, content, path,
        size_bytes, sha256,
    )
    return dict(row)


async def update_document(
    conn: asyncpg.Connection, project_id: str, document_id: str, **fields: Any,
) -> dict[str, Any] | None:
    """Set the given fields (from title, summary, filename, content, path, size_bytes and sha256).

    Returns the row without the content; None when the project has no such document.
    """
    unknown = set(fields) - set(_UPDATABLE)
    if unknown:
        raise ValueError(f"project_documents has no updatable column {sorted(unknown)[0]!r}")
    names = [name for name in _UPDATABLE if name in fields]
    assignments = ", ".join(f"{name} = ${index}" for index, name in enumerate(names, start=3))
    row = await conn.fetchrow(
        f"UPDATE project_documents SET {assignments}{', ' if names else ''}updated_at = now() "
        f"WHERE project_id = $1 AND id = $2 RETURNING {_COLUMNS}",
        project_id, document_id, *(fields[name] for name in names),
    )
    return dict(row) if row is not None else None


async def delete_document(project_id: str, document_id: str, *, pool: asyncpg.Pool | None = None) -> bool:
    """Remove a document; False when the project has no such document."""
    pool = pool or get_pool()
    result = await pool.execute("DELETE FROM project_documents WHERE project_id = $1 AND id = $2", project_id,
                                document_id)
    return result == "DELETE 1"


# --- Drafts: documents waiting for the user, which no run reads ---

_DRAFT_COLUMNS = "id, project_id, task_id, title, summary, content, created_at"


async def list_drafts(
    project_id: str, *, pool: asyncpg.Pool | asyncpg.Connection | None = None,
) -> list[dict[str, Any]]:
    """The project's drafts with their content (a lessons draft is a few KiB), oldest first."""
    pool = pool or get_pool()
    rows = await pool.fetch(
        f"SELECT {_DRAFT_COLUMNS} FROM project_document_drafts WHERE project_id = $1 ORDER BY created_at, id",
        project_id,
    )
    return [dict(row) for row in rows]


async def insert_draft(
    conn: asyncpg.Connection, *, project_id: str, task_id: str | None, title: str, summary: str, content: str,
) -> dict[str, Any]:
    """Add a draft; returns its row."""
    row = await conn.fetchrow(
        f"INSERT INTO project_document_drafts (id, project_id, task_id, title, summary, content) "
        f"VALUES ($1, $2, $3, $4, $5, $6) RETURNING {_DRAFT_COLUMNS}",
        f"draft-{uuid.uuid4().hex[:12]}", project_id, task_id, title, summary, content,
    )
    return dict(row)


async def delete_draft(
    project_id: str, draft_id: str, *, pool: asyncpg.Pool | asyncpg.Connection | None = None,
) -> dict[str, Any] | None:
    """Remove a draft; returns its row, None when the project has no such draft."""
    pool = pool or get_pool()
    row = await pool.fetchrow(
        f"DELETE FROM project_document_drafts WHERE project_id = $1 AND id = $2 RETURNING {_DRAFT_COLUMNS}",
        project_id, draft_id,
    )
    return dict(row) if row is not None else None
