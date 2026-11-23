"""Durable operator decisions when task or daily spend admission waits."""

from . import OnlineStep, _widen_check

name = "cost_holds"
order = 1430
pending_order = 1430
_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS cost_holds (
    id BIGSERIAL PRIMARY KEY,
    scope TEXT NOT NULL CHECK (scope IN ('task', 'schedule_day', 'role_day', 'global_day')),
    scope_ref TEXT NOT NULL,
    currency TEXT NOT NULL CHECK (currency IN ('provider_priced_usd', 'equivalent_usd')),
    limit_usd DOUBLE PRECISION NOT NULL CHECK (limit_usd >= 0),
    spent_usd DOUBLE PRECISION NOT NULL CHECK (spent_usd >= 0),
    reason TEXT NOT NULL CHECK (reason IN ('budget', 'unanswered', 'review_budget')),
    state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'continued', 'stopped', 'expired')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at TIMESTAMPTZ,
    decided_via TEXT CHECK (decided_via IN ('dashboard', 'api', 'lark', 'telegram')),
    task_ids TEXT[] NOT NULL DEFAULT '{}',
    launch_estimate_usd DOUBLE PRECISION NOT NULL DEFAULT 0 CHECK (launch_estimate_usd >= 0),
    applied_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_cost_holds_pending_scope
    ON cost_holds (scope, scope_ref) WHERE state = 'pending';
"""
_ASK_DDL = _widen_check.ddl('projects', 'task_budget_action', 'projects_task_budget_action_check', ('ask',))
fresh_ddl = _TABLE_DDL + _ASK_DDL + 'ALTER TABLE projects VALIDATE CONSTRAINT projects_task_budget_action_check;'
online_order = 1430
online = (OnlineStep(name, 'cost_holds_online', validate=(('projects', 'projects_task_budget_action_check'),)),)


async def pending(conn):
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name=$1)", name):
        return False
    return (not await conn.fetchval("SELECT to_regclass('cost_holds') IS NOT NULL")
            or not await conn.fetchval("SELECT EXISTS (SELECT 1 FROM pg_constraint "
                "WHERE conrelid=to_regclass('projects') AND conname='projects_task_budget_action_check' "
                "AND strpos(pg_get_constraintdef(oid), $1)>0)", "'ask'"))


async def apply(conn, *, sql, state):
    if await pending(conn):
        await conn.execute(_TABLE_DDL)
        await conn.execute(_ASK_DDL)
        await conn.execute("INSERT INTO schema_migrations(name) VALUES ($1)", name)
