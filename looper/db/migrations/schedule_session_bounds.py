"""The schedule_session_bounds migration, in its released execution order."""

name = "schedule_session_bounds"
order = 550
pending_order = 30


async def pending(conn):
    from .. import connection as legacy

    return await legacy._schedule_session_bounds_pending(conn)


async def apply(conn, *, sql, state) -> bool:
    did_work = False
    from .. import connection as legacy

    if await legacy._schedule_session_bounds_pending(conn):
        did_work = True
        await conn.execute("""
                ALTER TABLE schedules
                  ADD COLUMN planner_session_context_tokens BIGINT CHECK (planner_session_context_tokens >= 0),
                  ADD COLUMN planner_session_run_cost_usd DOUBLE PRECISION CHECK (planner_session_run_cost_usd >= 0);
                ALTER TABLE schedule_runs
                  ADD COLUMN planner_context_tokens BIGINT CHECK (planner_context_tokens >= 0),
                  ADD COLUMN planner_cost_usd DOUBLE PRECISION CHECK (planner_cost_usd >= 0),
                  ADD COLUMN planner_session_action TEXT CHECK (planner_session_action IN ('compacted', 'rotated')),
                  ADD COLUMN planner_session_reason TEXT CHECK (planner_session_reason IN ('context', 'cost')),
                  ADD COLUMN planner_session_before_tokens BIGINT CHECK (planner_session_before_tokens >= 0),
                  ADD COLUMN planner_session_after_tokens BIGINT CHECK (planner_session_after_tokens >= 0),
                  ADD COLUMN failure_kind TEXT CHECK (failure_kind IN ('task_budget'));
            """)
        await conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ($1)",
            legacy._SCHEDULE_SESSION_BOUNDS,
        )
    return did_work
