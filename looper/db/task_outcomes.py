"""Immutable task-run snapshots, built with SQL from typed source records."""

from collections.abc import Sequence

import asyncpg

from .connection import get_pool
from .llm_records import EQUIVALENT, RUN_KIND, TASK_RUN


async def record_outcomes(
    task_ids: Sequence[str], outcome: str, stop_cause: str, failure_kind: str | None = None,
    *, pool: asyncpg.Pool | asyncpg.Connection | None = None,
) -> None:
    await (pool or get_pool()).execute(
        f"""
        WITH bounds AS (
            SELECT t.id AS task_id,t.run_no,now() AS ended_at,
                   (SELECT max(o.ended_at) FROM task_outcomes o
                    WHERE o.task_id=t.id AND o.run_no<t.run_no) AS previous_end,t.created_at
            FROM tasks t WHERE t.id=ANY($1::text[])
        ), runs AS (
            SELECT b.*,(SELECT min(created_at) FROM llm_records
                        WHERE task_id=b.task_id AND {RUN_KIND}='planner'
                        AND created_at>=COALESCE(b.previous_end,b.created_at)
                        AND created_at<b.ended_at) AS started_at FROM bounds b
        ), spans AS (
            SELECT *,COALESCE(started_at,previous_end,created_at) AS lower_at FROM runs
        ), step_counts AS (
            SELECT a.task_id,a.step_key,count(*) AS attempts,
                   (array_agg(a.decision ORDER BY a.attempt DESC))[1] AS decision,
                   bool_and(a.trigger='first' AND a.decision='accept') AS first_pass
            FROM step_attempts a JOIN spans s ON s.task_id=a.task_id AND s.run_no=a.task_run_no
            GROUP BY a.task_id,a.step_key
        ), steps AS (
            SELECT task_id,sum(attempts)::integer AS attempts,
                   count(*) FILTER (WHERE decision='accept') AS accepted,
                   count(*) FILTER (WHERE decision='fail') AS failed,
                   count(*) FILTER (WHERE attempts=1 AND first_pass) AS first_pass
            FROM step_counts GROUP BY task_id
        ), costs AS (
            SELECT task_id,
                   COALESCE(sum(cost_usd) FILTER (WHERE NOT {EQUIVALENT}),0) AS provider_priced_usd,
                   COALESCE(sum(cost_usd) FILTER (WHERE {EQUIVALENT}),0) AS equivalent_usd,
                   count(*) FILTER (WHERE status<>'running' AND cost_usd IS NULL) AS unpriced_runs
            FROM llm_records r WHERE r.task_id=ANY($1::text[]) AND {TASK_RUN} AND EXISTS (
                SELECT 1 FROM spans s WHERE s.task_id=r.task_id
                AND r.created_at>=s.lower_at AND r.created_at<s.ended_at)
            GROUP BY task_id
        ), waits AS (
            SELECT task_id,opened_at,closed_at FROM task_user_gates
            UNION ALL SELECT task_id,opened_at,closed_at FROM task_waits
        ), intervals AS (
            SELECT s.task_id,GREATEST(w.opened_at,s.lower_at) AS lo,
                   LEAST(COALESCE(w.closed_at,s.ended_at),s.ended_at) AS hi
            FROM spans s JOIN waits w ON w.task_id=s.task_id
            WHERE w.opened_at<s.ended_at AND COALESCE(w.closed_at,s.ended_at)>s.lower_at
        ), preceding AS (
            SELECT *,max(hi) OVER (PARTITION BY task_id ORDER BY lo,hi
                        ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS previous_hi FROM intervals
        ), grouped AS (
            SELECT *,sum(CASE WHEN previous_hi IS NULL OR lo>previous_hi THEN 1 ELSE 0 END)
                   OVER (PARTITION BY task_id ORDER BY lo,hi) AS grp FROM preceding WHERE hi>lo
        ), merged AS (
            SELECT task_id,grp,min(lo) AS lo,max(hi) AS hi FROM grouped GROUP BY task_id,grp
        ), human AS (
            SELECT task_id,(extract(epoch FROM sum(hi-lo))*1000)::bigint AS ms FROM merged GROUP BY task_id
        )
        INSERT INTO task_outcomes(task_id,run_no,outcome,stop_cause,failure_kind,started_at,ended_at,
            human_wait_ms,steps_accepted,steps_failed,attempts,first_pass_steps,
            provider_priced_usd,equivalent_usd,unpriced_runs)
        SELECT s.task_id,s.run_no,$2,$3,$4,s.started_at,s.ended_at,COALESCE(h.ms,0),
               COALESCE(p.accepted,0),COALESCE(p.failed,0),COALESCE(p.attempts,0),COALESCE(p.first_pass,0),
               COALESCE(c.provider_priced_usd,0),COALESCE(c.equivalent_usd,0),COALESCE(c.unpriced_runs,0)
        FROM spans s LEFT JOIN steps p USING(task_id) LEFT JOIN costs c USING(task_id)
                     LEFT JOIN human h USING(task_id)
        ON CONFLICT (task_id,run_no) DO NOTHING
        """, list(task_ids), outcome, stop_cause, failure_kind)
