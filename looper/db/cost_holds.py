"""Atomic, durable spend holds. Only operator services expose these rows."""

from __future__ import annotations

from .connection import get_pool


async def create_hold(scope, ref, currency, limit, spent, reason, *, task_id=None, estimate_usd=0, pushes=None,
                      pool=None):
    pool = pool or get_pool()
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            "INSERT INTO cost_holds(scope,scope_ref,currency,limit_usd,spent_usd,reason,task_ids,"
            "launch_estimate_usd,pushes) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) ON CONFLICT (scope,scope_ref) WHERE state='pending' "
            "DO UPDATE SET task_ids=ARRAY(SELECT DISTINCT unnest(cost_holds.task_ids || EXCLUDED.task_ids)), "
            "launch_estimate_usd=GREATEST(cost_holds.launch_estimate_usd,EXCLUDED.launch_estimate_usd) "
            "RETURNING *, (xmax=0) AS created", scope, ref, currency, limit, spent, reason,
            [task_id] if task_id else [], estimate_usd, pushes)
        if task_id:
            await conn.execute("UPDATE tasks SET status='awaiting_budget' WHERE id=$1 "
                               "AND status NOT IN ('completed','failed','cancelled','stopped','draft','init')", task_id)
        result = dict(row)
        created = result.pop('created')
        return result, created


async def list_pending(*, pool=None):
    return [dict(row) for row in await (pool or get_pool()).fetch(
        "SELECT h.*, s.name AS schedule_name FROM cost_holds h "
        "LEFT JOIN schedules s ON h.scope = 'schedule' AND s.id = h.scope_ref "
        "WHERE h.state = 'pending' ORDER BY h.created_at, h.id")]


async def get(hold_id, *, pool=None):
    row = await (pool or get_pool()).fetchrow("SELECT * FROM cost_holds WHERE id=$1", hold_id)
    return dict(row) if row else None


async def task_ids(hold_id, *, pool=None):
    return await (pool or get_pool()).fetchval("SELECT task_ids FROM cost_holds WHERE id=$1", hold_id) or []
