"""Per-project policy of the task Manager's sandbox: the stored allowlist, its revision and its audit trail."""
from __future__ import annotations

import json
from typing import Any

import asyncpg

from .connection import get_pool

FIELD = "network_allowed_domains"


class RevisionConflict(Exception):
    """``expected_revision`` is not the stored one; ``revision`` is what is stored."""

    def __init__(self, revision: int) -> None:
        super().__init__(f"the stored revision is {revision}")
        self.revision = revision


def _list(value: Any) -> list[str]:
    return json.loads(value) if isinstance(value, str) else list(value)


async def get(project_id: str, *, pool: asyncpg.Pool | None = None) -> tuple[list[str], int]:
    """The stored list and revision; a project without a row has the default (no domains, revision 0)."""
    row = await (pool or get_pool()).fetchrow(
        "SELECT network_allowed_domains, revision FROM task_manager_sandbox_policies WHERE project_id = $1",
        project_id)
    return ([], 0) if row is None else (_list(row["network_allowed_domains"]), int(row["revision"]))


async def update(project_id: str, domains: list[str], *, actor: str, expected_revision: int | None = None,
                 pool: asyncpg.Pool | None = None) -> tuple[list[str], list[str], int]:
    """Replace the list in one transaction with the audit row: ``(old, new, revision)``.

    ``expected_revision`` that differs from the stored one raises ``RevisionConflict`` and writes nothing.
    """
    async with (pool or get_pool()).acquire() as conn, conn.transaction():
        # The default row (revision 0) is created only inside this transaction, so a refused call leaves nothing.
        await conn.execute(
            "INSERT INTO task_manager_sandbox_policies (project_id, revision, updated_by) VALUES ($1, 0, $2) "
            "ON CONFLICT (project_id) DO NOTHING", project_id, actor)
        row = await conn.fetchrow(
            "SELECT network_allowed_domains, revision FROM task_manager_sandbox_policies "
            "WHERE project_id = $1 FOR UPDATE", project_id)
        old, revision = _list(row["network_allowed_domains"]), int(row["revision"]) + 1
        if expected_revision is not None and expected_revision != row["revision"]:
            raise RevisionConflict(int(row["revision"]))
        await conn.execute(
            "UPDATE task_manager_sandbox_policies SET network_allowed_domains = $2::jsonb, revision = $3, "
            "updated_at = now(), updated_by = $4 WHERE project_id = $1",
            project_id, json.dumps(domains), revision, actor)
        await conn.execute(
            "INSERT INTO task_manager_sandbox_policy_audit (project_id, actor, field, old, new, revision) "
            "VALUES ($1, $2, $3, $4::jsonb, $5::jsonb, $6)",
            project_id, actor, FIELD, json.dumps(old), json.dumps(domains), revision)
        return old, list(domains), revision
