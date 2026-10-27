"""Manual, retention-gated removal of one deleted Manager's database history.

Manager and thread rows are permanent identity tombstones. In particular, deleting a thread owner would make
its old session available to the default Manager. Runtime retirement is the service caller's responsibility;
``before_purge`` can recheck it while the Manager row is locked, before any deletion.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import asyncpg

from .connection import get_pool


class CleanupIdentityError(ValueError):
    """The default Manager or an unknown identity cannot be a cleanup target."""


class CleanupConflict(ValueError):
    """The target is not eligible; ``result`` is the current preview, with no deletion."""

    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result
        super().__init__("; ".join(result["blockers"]))


def _validate(manager_id: str, retention_days: int) -> None:
    if not isinstance(manager_id, str) or not manager_id:
        raise ValueError("manager_id must be a nonempty string")
    if type(retention_days) is not int or not 30 <= retention_days <= 3650:
        raise ValueError("retention_days must be an integer between 30 and 3650")


@asynccontextmanager
async def _transaction(pool: asyncpg.Pool | asyncpg.Connection | None):
    source = pool if pool is not None else get_pool()
    if not isinstance(source, asyncpg.Pool):
        async with source.transaction():
            yield source
    else:
        async with source.acquire() as conn, conn.transaction():
            yield conn


async def _preview(conn: asyncpg.Connection, manager_id: str, retention_days: int) -> dict[str, Any]:
    row = await conn.fetchrow(
        "SELECT deleted_at, enabled, deleted_at <= transaction_timestamp() - $2::int * interval '1 day' AS old_enough "
        "FROM managers WHERE id = $1 FOR UPDATE", manager_id, retention_days)
    if manager_id == "manager":
        raise CleanupIdentityError("The default Manager cannot be cleaned up.")
    if row is None:
        raise CleanupIdentityError("The Manager does not exist.")
    blockers: list[str] = []
    if row is not None:
        if row["deleted_at"] is None:
            blockers.append("The Manager has not been deleted.")
        if row["enabled"]:
            blockers.append("The Manager is enabled.")
        if row["deleted_at"] is not None and not row["old_enough"]:
            blockers.append("The deletion retention window has not elapsed.")
        for table, label in (("tasks", "tasks"), ("channel_configs", "bots"),
                             ("manager_chat_bindings", "chat bindings"), ("schedules", "schedules")):
            if await conn.fetchval(f"SELECT EXISTS (SELECT 1 FROM {table} WHERE manager_id = $1)", manager_id):
                blockers.append(f"The Manager still has {label}.")
        if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM llm_records WHERE agent_id = $1 AND status = 'running')",
                               manager_id):
            blockers.append("The Manager still has running LLM records.")
    counts = dict(await conn.fetchrow(
        "SELECT (SELECT count(*) FROM chat_threads WHERE manager_id = $1) AS threads, "
        "(SELECT count(*) FROM chat_messages m JOIN chat_threads t USING (session_id) WHERE t.manager_id = $1) "
        "AS chat_messages, (SELECT count(*) FROM llm_records WHERE agent_id = $1) AS llm_records, "
        "(SELECT count(*) FROM llm_record_events e JOIN llm_records r ON r.id = e.record_id WHERE r.agent_id = $1) "
        "AS llm_record_events, (SELECT count(*) FROM manager_actions WHERE manager_id = $1) AS manager_actions",
        manager_id))
    return {"manager_id": manager_id, "deleted_at": row["deleted_at"].isoformat() if row and row["deleted_at"] else None,
            "retention_days": retention_days, "eligible": not blockers, "blockers": blockers,
            "counts": counts, "purged": False}


async def preview(manager_id: str, retention_days: int = 30, *,
                  pool: asyncpg.Pool | asyncpg.Connection | None = None) -> dict[str, Any]:
    """Read current eligibility and counts. ``counts.threads`` counts retained ownership tombstones."""
    _validate(manager_id, retention_days)
    async with _transaction(pool) as conn:
        return await _preview(conn, manager_id, retention_days)


async def purge(manager_id: str, retention_days: int = 30, *,
                pool: asyncpg.Pool | asyncpg.Connection | None = None,
                before_purge: Callable[[], Awaitable[None]] | None = None) -> dict[str, Any]:
    """Recheck, then delete exact-owner contents in one transaction; errors roll every deletion back.

    The caller must exclude live agents, turns and retries before calling. An optional ``before_purge`` rechecks
    fleet retirement under the row lock. A previous preview grants no authority and supplies no cutoff time.
    """
    _validate(manager_id, retention_days)
    async with _transaction(pool) as conn:
        result = await _preview(conn, manager_id, retention_days)
        if not result["eligible"]:
            raise CleanupConflict(result)
        if before_purge is not None:
            await before_purge()
        # Count events before the record FK cascades them away; no other owner is selected by these predicates.
        result["counts"]["chat_messages"] = await conn.fetchval(
            "WITH removed AS (DELETE FROM chat_messages m USING chat_threads t "
            "WHERE m.session_id = t.session_id AND t.manager_id = $1 RETURNING m.id) SELECT count(*) FROM removed",
            manager_id)
        result["counts"]["llm_records"] = await conn.fetchval(
            "WITH removed AS (DELETE FROM llm_records WHERE agent_id = $1 RETURNING id) SELECT count(*) FROM removed",
            manager_id)
        result["counts"]["manager_actions"] = await conn.fetchval(
            "WITH removed AS (DELETE FROM manager_actions WHERE manager_id = $1 RETURNING id) "
            "SELECT count(*) FROM removed", manager_id)
        result["purged"] = True
        return result
