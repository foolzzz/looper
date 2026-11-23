"""Reads behind the task-quality metrics. Task and step figures come from the ``task_outcomes`` and
``step_attempts`` snapshots. ``llm_records`` is read by task id and a run's time span (``idx_llm_records_task``), never
by a scan of the whole table."""

from datetime import datetime
from typing import Any

import asyncpg

from .connection import get_pool
from .llm_records import EQUIVALENT, RUN_KIND, TASK_RUN

Database = asyncpg.Pool | asyncpg.Connection

# The runs that ended in a window and match the filters: $1 since, $2 until, $3 project, $4 profile, $5 difficulty. A run
# has a profile or a difficulty when one of its attempts does.
OUTCOMES_IN_WINDOW = """
SELECT o.* FROM task_outcomes o JOIN tasks t ON t.id = o.task_id
WHERE o.ended_at >= $1 AND o.ended_at < $2 AND ($3::text IS NULL OR t.project_id = $3)
  AND ($4::text IS NULL OR EXISTS (SELECT 1 FROM step_attempts a WHERE a.task_id = o.task_id
                                   AND a.task_run_no = o.run_no AND a.profile_id = $4))
  AND ($5::text IS NULL OR EXISTS (SELECT 1 FROM step_attempts a JOIN task_steps s ON s.id = a.step_id
                                   WHERE a.task_id = o.task_id AND a.task_run_no = o.run_no AND s.difficulty = $5))
"""
TASKS_IN_WINDOW = f"SELECT task_id FROM ({OUTCOMES_IN_WINDOW}) w"

_ATTEMPT_COLUMNS = ("a.id, a.task_id, a.task_run_no, a.step_key, a.step_id, a.attempt, a.trigger, a.profile_id, "
                    "a.worker_runs, a.run_outcome, a.review_outcome, a.review_runs, a.blocking_findings, "
                    "a.adjudicated, a.decision, a.failure_kind, a.started_at, a.decided_at, s.difficulty")


async def coverage_since(*, pool: Database | None = None) -> datetime | None:
    """When the records began: nothing is backfilled, so earlier tasks have none. The first rows by id."""
    pool = pool or get_pool()
    return await pool.fetchval(
        "SELECT LEAST((SELECT started_at FROM step_attempts ORDER BY id LIMIT 1),"
        "(SELECT ended_at FROM task_outcomes ORDER BY id LIMIT 1))")


async def task_rows(since: datetime, until: datetime, project: str | None, profile: str | None,
                    difficulty: str | None, *, pool: Database | None = None) -> list[dict[str, Any]]:
    """Every run that ended in [since, until), with ``last`` set on each task's latest one."""
    rows = await (pool or get_pool()).fetch(
        f"""
        WITH ended AS (
            SELECT o.*, row_number() OVER (PARTITION BY o.task_id ORDER BY o.run_no DESC) = 1 AS last
            FROM ({OUTCOMES_IN_WINDOW}) o)
        SELECT e.id, e.task_id, e.run_no, e.outcome, e.stop_cause, e.failure_kind, e.started_at, e.ended_at,
               e.human_wait_ms, e.provider_priced_usd, e.equivalent_usd, e.unpriced_runs, e.last,
               t.created_at AS task_created_at,
               EXISTS (SELECT 1 FROM task_journal j WHERE j.task_id = e.task_id AND j.kind = 'supplement'
                       AND j.created_at < e.ended_at) AS supplemented,
               EXISTS (SELECT 1 FROM task_requirement_versions v WHERE v.task_id = e.task_id AND v.version > 1
                       AND v.created_at < e.ended_at) AS revised,
               f.verdict AS feedback
        FROM ended e JOIN tasks t ON t.id = e.task_id LEFT JOIN task_feedback f ON f.task_id = e.task_id
        """, since, until, project, profile, difficulty)
    return [dict(row) for row in rows]


async def attempt_rows(since: datetime, until: datetime, project: str | None,
                       *, pool: Database | None = None) -> list[dict[str, Any]]:
    """All attempts of the steps that had one decided in [since, until): the decision index picks the steps, the
    unique key lists their attempts."""
    rows = await (pool or get_pool()).fetch(
        f"""
        WITH hit AS (SELECT DISTINCT task_id, task_run_no, step_key FROM step_attempts
                     WHERE decided_at >= $1 AND decided_at < $2)
        SELECT {_ATTEMPT_COLUMNS}
        FROM hit h JOIN step_attempts a USING (task_id, task_run_no, step_key)
             JOIN tasks t ON t.id = a.task_id LEFT JOIN task_steps s ON s.id = a.step_id
        WHERE $3::text IS NULL OR t.project_id = $3
        ORDER BY a.task_id, a.task_run_no, a.step_key, a.attempt
        """, since, until, project)
    return [dict(row) for row in rows]


async def contract_counts(since: datetime, until: datetime, project: str | None, profile: str | None,
                          difficulty: str | None, floor: datetime | None, *, pool: Database | None = None) -> dict[str, int]:
    """Worker and Reviewer runs that reported an outcome and how many broke the contract, the Planner's turns and the
    rejections of its replies, over the runs that ended in the window: a run owns records from its start until the next
    run of the task starts. ``missing_attempts`` is separate, the Worker runs of those tasks since ``floor`` with no
    attempt (a failed write can predate the run)."""
    row = await (pool or get_pool()).fetchrow(
        f"""
        WITH window_runs AS (
            SELECT o.task_id, o.run_no, COALESCE(o.started_at, t.created_at) AS lower_at,
                   COALESCE((SELECT COALESCE(n.started_at, n.ended_at) FROM task_outcomes n
                             WHERE n.task_id = o.task_id AND n.run_no > o.run_no
                             ORDER BY n.run_no LIMIT 1), 'infinity'::timestamptz) AS upper_at
            FROM ({OUTCOMES_IN_WINDOW}) o JOIN tasks t ON t.id = o.task_id),
        runs AS (
            SELECT {RUN_KIND} AS kind, r.metadata->>'outcome' AS outcome, r.attempt_id, r.created_at,
                   CASE WHEN jsonb_typeof(r.metadata->'contract_rejections') = 'object' THEN
                        (SELECT COALESCE(sum(e.value::integer), 0) FROM jsonb_each_text(r.metadata->'contract_rejections') e)
                        ELSE 0 END AS rejections
            FROM window_runs w JOIN LATERAL (
                SELECT metadata, attempt_id, created_at, run_kind, agent_id FROM llm_records r
                WHERE r.task_id = w.task_id AND r.created_at >= w.lower_at AND r.created_at < w.upper_at
                  AND {TASK_RUN}) r ON true)
        SELECT count(*) FILTER (WHERE kind = 'worker' AND outcome IS NOT NULL) AS worker_runs,
               count(*) FILTER (WHERE kind = 'worker' AND outcome = 'contract') AS worker_contract,
               count(*) FILTER (WHERE kind = 'reviewer' AND outcome IS NOT NULL) AS reviewer_runs,
               count(*) FILTER (WHERE kind = 'reviewer' AND outcome = 'contract') AS reviewer_contract,
               count(*) FILTER (WHERE kind = 'planner') AS planner_turns,
               COALESCE(sum(rejections) FILTER (WHERE kind = 'planner'), 0)::bigint AS planner_rejections,
               (SELECT count(*) FROM (SELECT DISTINCT task_id FROM window_runs) picked JOIN LATERAL (
                    SELECT 1 FROM llm_records r
                    WHERE r.task_id = picked.task_id AND {TASK_RUN} AND {RUN_KIND} = 'worker'
                      AND r.attempt_id IS NULL AND $6::timestamptz IS NOT NULL AND r.created_at >= $6) m ON true)
               AS missing_attempts
        FROM runs
        """, since, until, project, profile, difficulty, floor)
    return dict(row)


async def task_records(task_ids: list[str], *, pool: Database | None = None) -> list[dict[str, Any]]:
    """The runs that make up these tasks' cost (``task_costs``), as the compact rows the cost tree is built from."""
    if not task_ids:
        return []
    rows = await (pool or get_pool()).fetch(
        f"""SELECT id, task_id, parent_record_id, attempt_id, {RUN_KIND} AS kind, cost_usd, {EQUIVALENT} AS equivalent
            FROM llm_records WHERE task_id = ANY($1::text[]) AND {TASK_RUN}""", task_ids)
    return [dict(row) for row in rows]


async def task_attempt_rows(task_id: str, *, pool: Database | None = None) -> list[dict[str, Any]]:
    rows = await (pool or get_pool()).fetch(
        f"""SELECT {_ATTEMPT_COLUMNS} FROM step_attempts a LEFT JOIN task_steps s ON s.id = a.step_id
            WHERE a.task_id = $1 ORDER BY a.task_run_no, a.started_at, a.id""", task_id)
    return [dict(row) for row in rows]


async def task_state(task_id: str, *, pool: Database | None = None) -> dict[str, Any] | None:
    row = await (pool or get_pool()).fetchrow("SELECT id, status, run_no FROM tasks WHERE id = $1", task_id)
    return dict(row) if row else None


async def task_missing_attempts(task_id: str, floor: datetime | None, *, pool: Database | None = None) -> int:
    if floor is None:
        return 0
    return await (pool or get_pool()).fetchval(
        f"""SELECT count(*) FROM llm_records WHERE task_id = $1 AND {TASK_RUN} AND {RUN_KIND} = 'worker'
            AND attempt_id IS NULL AND created_at >= $2""", task_id, floor)
