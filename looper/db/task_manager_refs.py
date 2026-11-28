"""The task branch of a direct session and the reports of refs a turn changed (``agents.task_manager_refs``)."""
from __future__ import annotations

import json
from typing import Any

import asyncpg

from .connection import get_pool


async def get_branch(task_id: str, *, pool: asyncpg.Pool | None = None) -> str | None:
    return await (pool or get_pool()).fetchval(
        "SELECT branch FROM task_manager_branches WHERE task_id = $1", task_id)


async def record_first_branch(task_id: str, branch: str, *, pool: asyncpg.Pool | None = None) -> bool:
    """Record ``branch`` unless one is already recorded; True when this call recorded it."""
    return await (pool or get_pool()).fetchval(
        "INSERT INTO task_manager_branches (task_id, branch, recorded_by) VALUES ($1, $2, 'first_enable') "
        "ON CONFLICT (task_id) DO NOTHING RETURNING true", task_id, branch) is True


async def set_branch(task_id: str, branch: str, *, pool: asyncpg.Pool | None = None) -> str | None:
    """Record ``branch`` as the operator's choice; returns the previous branch, if any."""
    async with (pool or get_pool()).acquire() as conn, conn.transaction():
        previous = await conn.fetchval("SELECT branch FROM task_manager_branches WHERE task_id = $1 FOR UPDATE", task_id)
        await conn.execute(
            "INSERT INTO task_manager_branches (task_id, branch, recorded_by) VALUES ($1, $2, 'operator') "
            "ON CONFLICT (task_id) DO UPDATE SET branch = EXCLUDED.branch, recorded_by = 'operator', "
            "recorded_at = now()", task_id, branch)
    return previous


async def insert_report(task_id: str, message_id: str | None, changes: dict[str, Any], needs_you: bool, *,
                        pool: asyncpg.Pool | None = None) -> int:
    return await (pool or get_pool()).fetchval(
        "INSERT INTO task_manager_ref_reports (task_id, message_id, changes, needs_you) "
        "VALUES ($1, $2, $3::jsonb, $4) RETURNING id", task_id, message_id, json.dumps(changes), needs_you)


async def pending_reports(*, pool: asyncpg.Pool | None = None) -> list[dict[str, Any]]:
    """Unresolved reports that need the operator, oldest first, with the task's own columns for the item."""
    rows = await (pool or get_pool()).fetch(
        "SELECT r.id, r.task_id, r.created_at, r.changes, t.title, t.goal_id, t.requirement "
        "FROM task_manager_ref_reports r JOIN tasks t ON t.id = r.task_id "
        "WHERE r.needs_you AND r.resolved_at IS NULL ORDER BY r.created_at, r.id")
    found = []
    for row in rows:
        value = dict(row)
        if isinstance(value["changes"], str):
            value["changes"] = json.loads(value["changes"])
        found.append(value)
    return found


async def resolve_report(report_id: int, resolved_by: str, *, pool: asyncpg.Pool | None = None) -> Any:
    """Mark a report handled; its ``resolved_at`` (an already handled report keeps its first time), None when unknown."""
    return await (pool or get_pool()).fetchval(
        "UPDATE task_manager_ref_reports SET resolved_at = COALESCE(resolved_at, now()), "
        "resolved_by = COALESCE(resolved_by, $2) WHERE id = $1 RETURNING resolved_at", report_id, resolved_by)
