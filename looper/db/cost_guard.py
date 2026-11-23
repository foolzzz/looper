"""Raw accounting evidence for the cost guard; never a model-visible record view."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from .connection import get_pool
from .llm_records import EQUIVALENT, RUN_KIND


async def history(now: datetime, *, pool=None) -> list[dict]:
    pool = pool or get_pool()
    # Active tasks may have spent money before the sampling window. Running records from any day still reserve.
    rows = await pool.fetch(f"""
        SELECT r.id, COALESCE(r.metadata->'cost_guard'->>'run_kind',
                    {RUN_KIND.replace('run_kind', 'r.run_kind').replace('agent_id', 'r.agent_id')},
                    r.metadata->'prompt_layers'->'role'->>'config_id') AS run_kind,
               r.task_id, COALESCE(t.schedule_id, r.metadata->>'cost_guard_schedule_id') AS schedule_id,
               r.created_at, r.status, r.cost_usd, r.metadata,
               CASE WHEN {EQUIVALENT.replace('metadata', 'r.metadata')} THEN 'equivalent_usd'
                    ELSE 'provider_priced_usd' END AS currency
        FROM llm_records r LEFT JOIN tasks t ON t.id = r.task_id
        WHERE r.created_at >= $1 OR r.status = 'running' OR r.task_id IN
              (SELECT id FROM tasks WHERE status NOT IN ('completed', 'failed', 'cancelled', 'stopped'))
        ORDER BY r.created_at, r.id
    """, now - timedelta(days=30))
    result = []
    for row in rows:
        item = dict(row)
        item['metadata'] = json.loads(item['metadata']) if isinstance(item['metadata'], str) else item['metadata'] or {}
        result.append(item)
    return result


async def limits(*, pool=None) -> dict:
    pool = pool or get_pool()
    tasks = await pool.fetch("SELECT t.id, t.schedule_id, COALESCE(t.budget_usd, p.task_budget_usd) AS cap "
                             "FROM tasks t JOIN projects p ON p.id = t.project_id")
    schedules = await pool.fetch("SELECT id, daily_budget_usd FROM schedules")
    return {'tasks': {r['id']: {'cap': float(r['cap']) if r['cap'] is not None else None,
                               'schedule_id': r['schedule_id']} for r in tasks},
            'schedules': {r['id']: float(r['daily_budget_usd']) if r['daily_budget_usd'] is not None else None
                          for r in schedules}}


async def context(task_id: str | None, schedule_id: str | None, *, pool=None) -> dict:
    """Read small configuration rows before admission, including tasks created after startup."""
    pool = pool or get_pool()
    result = {'tasks': {}, 'schedules': {}}
    if task_id:
        row = await pool.fetchrow(
            "SELECT t.id, t.schedule_id, COALESCE(t.budget_usd, p.task_budget_usd) AS cap "
            "FROM tasks t JOIN projects p ON p.id = t.project_id WHERE t.id = $1", task_id)
        if row:
            result['tasks'][task_id] = {'cap': float(row['cap']) if row['cap'] is not None else None,
                                         'schedule_id': row['schedule_id']}
            schedule_id = schedule_id or row['schedule_id']
    if schedule_id:
        cap = await pool.fetchval("SELECT daily_budget_usd FROM schedules WHERE id = $1", schedule_id)
        result['schedules'][schedule_id] = float(cap) if cap is not None else None
    return result
