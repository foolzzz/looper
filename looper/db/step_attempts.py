"""SQL writes for step attempts. Callers use the best-effort agents.attempts boundary."""

import json

import asyncpg

from .connection import get_pool

Database = asyncpg.Pool | asyncpg.Connection


async def task_run_no(task_id: str, *, pool: Database | None = None) -> int | None:
    return await (pool or get_pool()).fetchval('SELECT run_no FROM tasks WHERE id=$1', task_id)


async def step_attempted_before_run(task_id: str, step_id: str | None, run_no: int, *, pool: Database | None = None) -> bool:
    return await (pool or get_pool()).fetchval(
        "SELECT EXISTS (SELECT 1 FROM step_attempts "
        "WHERE task_id=$1 AND step_id=$2 AND task_run_no<$3)", task_id, step_id, run_no)


async def step_added_during_run(task_id: str, step_id: str | None, run_no: int, *, pool: Database | None = None) -> bool:
    return await (pool or get_pool()).fetchval(
        "SELECT EXISTS (SELECT 1 FROM task_steps s JOIN step_attempts a ON a.task_id=s.task_id "
        "WHERE s.id=$2 AND s.task_id=$1 AND a.task_run_no=$3 AND a.started_at<s.created_at)",
        task_id, step_id, run_no)


async def open_attempt(task_id: str, step_id: str | None, step_key: str, run_no: int, trigger: str, *, pool: Database | None = None) -> int:
    return await (pool or get_pool()).fetchval(
        "INSERT INTO step_attempts(task_id,step_id,step_key,task_run_no,attempt,trigger) "
        "SELECT $1,$2,$3,$4,COALESCE(max(attempt),0)+1,$5 FROM step_attempts "
        "WHERE task_id=$1 AND task_run_no=$4 AND step_key=$3 RETURNING id",
        task_id, step_id, step_key, run_no, trigger)


async def latest_attempt(task_id: str, step_key: str, run_no: int | None = None, *, open_only: bool = False, pool: Database | None = None) -> asyncpg.Record | None:
    return await (pool or get_pool()).fetchrow(
        "SELECT a.id,a.decision FROM step_attempts a JOIN tasks t ON t.id=a.task_id "
        "WHERE a.task_id=$1 AND a.step_key=$2 AND a.task_run_no=COALESCE($3::integer,t.run_no) "
        "AND (NOT $4::boolean OR a.decision IS NULL) ORDER BY a.attempt DESC LIMIT 1",
        task_id, step_key, run_no, open_only)


async def worker_started(attempt_id: int, record_id: int, profile_id: str | None, *, pool: Database | None = None) -> None:
    await (pool or get_pool()).execute(
        "WITH linked AS (UPDATE llm_records SET attempt_id=$1 WHERE id=$2 RETURNING id) "
        "UPDATE step_attempts SET worker_record_id=$2,profile_id=$3,worker_runs=worker_runs+1 "
        "WHERE id=$1 AND EXISTS (SELECT 1 FROM linked)", attempt_id, record_id, profile_id)


async def previous_worker_run(attempt_id: int, task_id: str, step_id: str, *, pool: Database | None = None) -> dict | None:
    """The attempt's latest Worker, before a new run replaces its pointer; never a Reviewer or an older attempt."""
    row = await (pool or get_pool()).fetchrow(
        "SELECT r.status,r.error,r.metadata FROM step_attempts a JOIN llm_records r ON r.id=a.worker_record_id "
        "WHERE a.id=$1 AND a.task_id=$2 AND a.step_id=$3 AND r.attempt_id=a.id", attempt_id, task_id, step_id)
    if row is None:
        return None
    result = dict(row)
    # asyncpg's default JSON codec returns text.
    if isinstance(result["metadata"], str):
        result["metadata"] = json.loads(result["metadata"])
    return result


async def link_record(attempt_id: int, record_id: int, *, pool: Database | None = None) -> None:
    await (pool or get_pool()).execute('UPDATE llm_records SET attempt_id=$1 WHERE id=$2', attempt_id, record_id)


async def link_review_record(attempt_id: int, task_id: str, agent_id: str, *, pool: Database | None = None) -> None:
    await (pool or get_pool()).execute(
        "UPDATE llm_records SET attempt_id=$1 WHERE task_id=$2 AND agent_id=$3",
        attempt_id, task_id, agent_id)


async def link_advice(attempt_id: int, spawn_id: int, *, pool: Database | None = None) -> None:
    await (pool or get_pool()).execute(
        "WITH linked AS (UPDATE advisor_advice SET attempt_id=$1 WHERE spawn_id=$2 RETURNING spawn_id) "
        "UPDATE llm_records r SET attempt_id=$1 FROM agent_spawns s "
        "WHERE s.id=$2 AND r.id=s.parent_record_id AND r.attempt_id IS NULL "
        "AND EXISTS (SELECT 1 FROM linked)", attempt_id, spawn_id)


async def worker_finished(attempt_id: int, result: str, *, pool: Database | None = None) -> None:
    await (pool or get_pool()).execute('UPDATE step_attempts SET run_outcome=$2 WHERE id=$1', attempt_id, result)


async def review_finished(attempt_id: int, passed: bool, blocking_findings: int, *, required: bool = True, ran: bool = True, pool: Database | None = None) -> None:
    await (pool or get_pool()).execute(
        "UPDATE step_attempts SET review_runs=review_runs+$5::integer,blocking_findings=blocking_findings+$3, "
        "review_outcome=CASE WHEN NOT $4::boolean THEN review_outcome "
        "WHEN NOT $2::boolean OR review_outcome='not_passed' THEN 'not_passed' ELSE 'passed' END WHERE id=$1",
        attempt_id, passed, blocking_findings, required, int(ran))


async def decide_attempt(attempt_id: int, decision: str, record_id: int | None = None, adjudicated: bool = False,
                         failure_kind: str | None = None, *, pool: Database | None = None) -> None:
    await (pool or get_pool()).execute(
        "WITH settled AS (UPDATE step_attempts SET decision=$2,decided_at=now(), "
        "failure_kind=CASE WHEN $2='accept' THEN NULL "
        "WHEN $2 IN ('retry','replan','fail') AND $5::text IS NOT NULL THEN $5::text "
        "WHEN $2 IN ('retry','replan','fail') AND review_outcome='not_passed' THEN 'review_rejected' "
        "WHEN run_outcome IN ('contract','files_mismatch','reported_failed','run_error','timeout','run_cap','crash') "
        "THEN run_outcome END, adjudicated=adjudicated OR $4, "
        "review_outcome=CASE WHEN $4 AND $2='accept' AND review_outcome='not_passed' "
        "THEN 'passed_by_adjudication' ELSE COALESCE(review_outcome,'none') END WHERE id=$1 AND decision IS NULL RETURNING id) "
        "UPDATE llm_records SET attempt_id=$1 WHERE id=$3 AND EXISTS (SELECT 1 FROM settled)",
        attempt_id, decision, record_id, adjudicated, failure_kind)


async def close_attempts(task_id: str, decision: str, failure_kind: str | None = None, step_key: str | None = None,
                         *, step_keys: list[str] | None = None, pool: Database | None = None) -> None:
    await close_attempts_batch([task_id], decision, failure_kind,
                               step_keys=[step_key] if step_key is not None else step_keys, pool=pool)


async def close_attempts_batch(task_ids: list[str], decision: str, failure_kind: str | None = None,
                               *, step_keys: list[str] | None = None, pool: Database | None = None) -> None:
    await (pool or get_pool()).execute(
        "UPDATE step_attempts a SET decision=$2,decided_at=now(),failure_kind=$3 FROM tasks t "
        "WHERE t.id=ANY($1::text[]) AND a.task_id=t.id AND a.task_run_no=t.run_no AND a.decision IS NULL "
        "AND ($4::text[] IS NULL OR a.step_key=ANY($4::text[]))", task_ids, decision, failure_kind, step_keys)
