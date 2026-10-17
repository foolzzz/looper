"""LLM invocation records — audit trail for agent sessions."""

from __future__ import annotations

import json
from datetime import datetime
from enum import Enum
from typing import Any

import asyncpg

from .connection import get_pool


class RecordStatus(str, Enum):
    """Where one run stands (``llm_records.status``). Every status but ``running`` is a run that has ended."""

    RUNNING = "running"
    COMPLETED = "completed"  # it ended with a successful result
    FAILED = "failed"  # it ended without one: an error, no result, its time limit or its spend cap
    # Looper stopped it before it ended (a kill, its task stopped or cancelled, the server stopping), or the server
    # running it died (``end_unfinished_records``): how it would have ended is not known.
    INTERRUPTED = "interrupted"


# Why a record found running at startup ended.
UNFINISHED_AT_START = "the server stopped before this run ended; it was found still running at the next start"


def _row_to_dict(row: asyncpg.Record) -> dict[str, Any]:
    d = dict(row)
    for key in ("created_at", "finished_at", "last_output_at"):
        if d.get(key) is not None:
            d[key] = d[key].isoformat()
    for key in ("metadata", "compactions"):
        if d.get(key) is not None:
            d[key] = json.loads(d[key])
    return d


async def start_record(
    *,
    agent_id: str,
    task_id: str | None = None,
    session_id: str | None = None,
    input_text: str | None = None,
    metadata: dict[str, Any] | None = None,
    pool: asyncpg.Pool | None = None,
) -> int:
    """Open a ``running`` record for one Claude invocation; returns its id."""
    pool = pool or get_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO llm_records
            (agent_id, task_id, session_id, input_summary, input_text, metadata, status)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb, 'running')
        RETURNING id
        """,
        agent_id, task_id, session_id,
        input_text[:500] if input_text else None, input_text,
        json.dumps(metadata) if metadata else None,
    )
    return row["id"]


async def end_unfinished_records(*, pool: asyncpg.Pool | None = None) -> int:
    """End every record still ``running`` as ``interrupted``; returns how many.

    Called at startup before any agent runs, when each such record is a run the
    previous server did not see end: it was killed, crashed or stopped first.
    It ended at its last output as far as anyone knows (its start without
    one). What the run had recorded stays; its usage, kept in memory until a
    run ends, is not known. Running it again changes nothing.
    """
    pool = pool or get_pool()
    result = await pool.execute(
        "UPDATE llm_records SET status = $1, finished_at = COALESCE(last_output_at, created_at), "
        "error = COALESCE(error, $2) WHERE status = $3",
        RecordStatus.INTERRUPTED.value, UNFINISHED_AT_START, RecordStatus.RUNNING.value,
    )
    return int(result.split()[-1])


async def append_events(
    record_id: int,
    events: list[tuple[int, dict[str, Any]]],
    *,
    pool: asyncpg.Pool | None = None,
) -> None:
    """Append ``(seq, event)`` stream events to a record's transcript."""
    if not events:
        return
    pool = pool or get_pool()
    await pool.executemany(
        "INSERT INTO llm_record_events (record_id, seq, event) VALUES ($1, $2, $3::jsonb)",
        [(record_id, seq, json.dumps(event, ensure_ascii=False)) for seq, event in events],
    )


async def update_event(
    record_id: int,
    seq: int,
    event: dict[str, Any],
    *,
    pool: asyncpg.Pool | None = None,
) -> None:
    """Replace a stored event in place (a progress counter that keeps growing)."""
    pool = pool or get_pool()
    await pool.execute(
        "UPDATE llm_record_events SET event = $3::jsonb WHERE record_id = $1 AND seq = $2",
        record_id, seq, json.dumps(event, ensure_ascii=False),
    )


async def touch_record(record_id: int, *, pool: asyncpg.Pool | None = None) -> None:
    """Mark that a running record just produced output (its heartbeat)."""
    pool = pool or get_pool()
    await pool.execute("UPDATE llm_records SET last_output_at = now() WHERE id = $1", record_id)


async def finish_record(
    record_id: int,
    *,
    status: str,
    output_text: str | None = None,
    session_id: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cache_read_tokens: int | None = None,
    cache_creation_tokens: int | None = None,
    cost_usd: float | None = None,
    context_tokens: int | None = None,
    compactions: list[dict[str, Any]] | None = None,
    duration_ms: int | None = None,
    model: str | None = None,
    error: str | None = None,
    cost_source: str | None = None,
    outcome: str | None = None,
    metadata: dict[str, Any] | None = None,
    pool: asyncpg.Pool | None = None,
) -> None:
    """Close a record with the run's outcome, its own usage and cost (and where the cost comes from), its final
    context and its compactions; a Worker's or Reviewer's run also keeps how its report or verdict read
    (``metadata.outcome``, ``worker.RunOutcome``), and ``metadata``'s keys are added to the record's (a run's
    refused tool calls, ``permission_denials``; the files a Planner's turn changed, ``planner_changed_files``)."""
    pool = pool or get_pool()
    await pool.execute(
        """
        UPDATE llm_records SET
            status = $2,
            output_text = $3,
            output_summary = $4,
            session_id = COALESCE($5, session_id),
            input_tokens = $6,
            output_tokens = $7,
            cost_usd = $8,
            duration_ms = $9,
            model = COALESCE($10, model),
            error = $11,
            cache_read_tokens = $12,
            cache_creation_tokens = $13,
            context_tokens = $14,
            compactions = $15::jsonb,
            cost_source = $16,
            metadata = CASE WHEN $17::text IS NULL AND $18::jsonb IS NULL THEN metadata
                            ELSE COALESCE(metadata, '{}'::jsonb)
                                 || CASE WHEN $17::text IS NULL THEN '{}'::jsonb
                                         ELSE jsonb_build_object('outcome', $17::text) END
                                 || COALESCE($18::jsonb, '{}'::jsonb) END,
            finished_at = now(),
            last_output_at = COALESCE(last_output_at, now())
        WHERE id = $1
        """,
        record_id, status, output_text, output_text[:500] if output_text else None,
        session_id, input_tokens, output_tokens, cost_usd, duration_ms, model, error,
        cache_read_tokens, cache_creation_tokens, context_tokens,
        json.dumps(compactions) if compactions is not None else None, cost_source, outcome,
        json.dumps(metadata, ensure_ascii=False) if metadata else None,
    )


async def list_events(
    record_id: int,
    *,
    after_seq: int = 0,
    limit: int = 1000,
    pool: asyncpg.Pool | None = None,
) -> list[dict[str, Any]]:
    """A record's stream events with ``seq`` greater than ``after_seq``, in order."""
    pool = pool or get_pool()
    rows = await pool.fetch(
        """
        SELECT seq, event, created_at FROM llm_record_events
        WHERE record_id = $1 AND seq > $2
        ORDER BY seq
        LIMIT $3
        """,
        record_id, after_seq, limit,
    )
    return [
        {"seq": r["seq"], "event": json.loads(r["event"]), "created_at": r["created_at"].isoformat()}
        for r in rows
    ]


# Which records belong to an agent role (the roles of role_configs). Each
# task's Planner records its turns as planner-<task_id>; Workers and Reviewers
# record under their per-run instance id.
_ROLE_FILTERS = {
    "manager": "agent_id = 'manager'",
    "planner": "agent_id LIKE 'planner-%'",
    "worker": "agent_id LIKE 'worker-%'",
    "reviewer": "agent_id LIKE 'reviewer-%'",
}
# A task's step runs: its Workers' and its Reviewers'.
_STEP_RUNS = f"({_ROLE_FILTERS['worker']} OR {_ROLE_FILTERS['reviewer']})"
# The agents that work on one task, whose runs make up its cost; the Manager is not one of them.
TASK_ROLES = ("planner", "worker", "reviewer")
_TASK_ROLE = (
    "CASE " + " ".join(f"WHEN {_ROLE_FILTERS[role]} THEN '{role}'" for role in TASK_ROLES) + " END"
)


async def list_task_runs(
    task_ids: list[str],
    *,
    pool: asyncpg.Pool | None = None,
) -> list[dict[str, Any]]:
    """Worker and Reviewer runs of several tasks, oldest first, with the step each ran and the role its task's
    Planner defined that it ran as (``task_role``, its ``prompt_layers`` entry; None for the built-in role)."""
    if not task_ids:
        return []
    pool = pool or get_pool()
    rows = await pool.fetch(
        "SELECT agent_id, task_id, status, created_at, metadata->>'step_id' AS step_id, "
        "metadata->'prompt_layers'->'task_role' AS task_role "
        f"FROM llm_records WHERE task_id = ANY($1::text[]) AND {_STEP_RUNS} "
        "ORDER BY created_at, id",
        task_ids,
    )
    return [{**dict(r), "task_role": json.loads(r["task_role"]) if r["task_role"] else None} for r in rows]


async def list_records(
    *,
    agent_id: str | None = None,
    task_id: str | None = None,
    role: str | None = None,
    limit: int = 100,
    offset: int = 0,
    pool: asyncpg.Pool | None = None,
) -> list[dict[str, Any]]:
    """Records newest first, filtered by agent instance, task and/or role."""
    pool = pool or get_pool()

    conditions = []
    params: list[Any] = []
    idx = 1

    if role is not None:
        if role not in _ROLE_FILTERS:
            return []
        conditions.append(_ROLE_FILTERS[role])

    if agent_id:
        conditions.append(f"agent_id = ${idx}")
        params.append(agent_id)
        idx += 1
    if task_id:
        conditions.append(f"task_id = ${idx}")
        params.append(task_id)
        idx += 1

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.extend([limit, offset])

    rows = await pool.fetch(
        f"SELECT * FROM llm_records {where} ORDER BY created_at DESC LIMIT ${idx} OFFSET ${idx + 1}",
        *params,
    )
    return [_row_to_dict(r) for r in rows]


async def get_record(
    record_id: int,
    *,
    pool: asyncpg.Pool | None = None,
) -> dict[str, Any] | None:
    pool = pool or get_pool()
    row = await pool.fetchrow("SELECT * FROM llm_records WHERE id = $1", record_id)
    if row is None:
        return None
    return _row_to_dict(row)


# A run with a runtime's own login (a subscription: Claude Code's, priced by Claude Code; Codex's, priced by Looper
# at the built-in prices): what that usage would cost at API prices, not money spent. A run names its Agent Profile
# in metadata.profile; one recorded before the rename names it in metadata.agent, and one recorded before Agent
# Profiles names none: it ran with Claude Code's login.
EQUIVALENT = ("COALESCE(metadata->'profile'->'provider'->>'auth', metadata->'agent'->'provider'->>'auth', "
              "'oauth') = 'oauth'")


async def task_costs(
    task_ids: list[str],
    *,
    pool: asyncpg.Pool | None = None,
) -> dict[str, dict[str, Any]]:
    """Each task's cost: the sum of its Planner's, Workers' and Reviewers' runs, in total and by role.

    Every given task id gets an entry. ``runs`` counts every run; ``running``
    those still going, whose cost is not known yet; ``unpriced_runs`` the
    ended ones without a cost (Claude exited or was interrupted before it
    reported one, or their model has no price); ``equivalent_runs`` those
    whose cost is what a subscription's usage would cost at API prices
    (``EQUIVALENT``), and ``provider_price_runs`` the others Looper priced at
    their provider's prices.
    """
    if not task_ids:
        return {}
    pool = pool or get_pool()
    rows = await pool.fetch(
        f"""
        SELECT task_id, {_TASK_ROLE} AS role, count(*) AS runs,
               count(*) FILTER (WHERE status = 'running') AS running,
               count(*) FILTER (WHERE status <> 'running' AND cost_usd IS NULL) AS unpriced_runs,
               count(*) FILTER (WHERE cost_usd IS NOT NULL AND {EQUIVALENT}) AS equivalent_runs,
               count(*) FILTER (WHERE cost_usd IS NOT NULL AND cost_source = 'provider_price' AND NOT {EQUIVALENT})
                   AS provider_price_runs,
               COALESCE(sum(cost_usd), 0) AS cost_usd
        FROM llm_records
        WHERE task_id = ANY($1::text[]) AND {_TASK_ROLE} IS NOT NULL
        GROUP BY task_id, role
        """,
        task_ids,
    )
    costs = {
        task_id: {"cost_usd": 0.0, "runs": 0, "running": 0, "unpriced_runs": 0, "equivalent_runs": 0,
                  "provider_price_runs": 0, "by_role": {role: {"cost_usd": 0.0, "runs": 0} for role in TASK_ROLES}}
        for task_id in task_ids
    }
    for row in rows:
        cost = costs[row["task_id"]]
        for key in ("runs", "running", "unpriced_runs", "equivalent_runs", "provider_price_runs"):
            cost[key] += row[key]
        cost["cost_usd"] += row["cost_usd"]
        cost["by_role"][row["role"]] = {"cost_usd": row["cost_usd"], "runs": row["runs"]}
    return costs


async def role_runs(task_id: str, *, pool: asyncpg.Pool | None = None) -> list[dict[str, Any]]:
    """Task ``task_id``'s Worker and Reviewer runs, counted by ``role`` (``worker`` or ``reviewer``), ``task_role``
    (the key of the role its Planner defined that the run ran as, '' for the built-in one), ``step_id`` and
    ``outcome`` (``metadata.outcome``, None before it was recorded or while it runs): ``runs``, ``running`` and
    ``cost_usd``."""
    pool = pool or get_pool()
    rows = await pool.fetch(
        f"""
        SELECT {_TASK_ROLE} AS role, COALESCE(metadata->'prompt_layers'->'task_role'->>'id', '') AS task_role,
               metadata->>'step_id' AS step_id, metadata->>'outcome' AS outcome, count(*) AS runs,
               count(*) FILTER (WHERE status = 'running') AS running, COALESCE(sum(cost_usd), 0) AS cost_usd
        FROM llm_records WHERE task_id = $1 AND {_STEP_RUNS}
        GROUP BY 1, 2, 3, 4
        """,
        task_id,
    )
    return [dict(row) for row in rows]


# ``_TASK_ROLE`` and ``EQUIVALENT`` of the runs ``r`` of a query that joins their tasks ``t``.
_RUN_TASK_ROLE = _TASK_ROLE.replace("agent_id", "r.agent_id")
_RUN_EQUIVALENT = EQUIVALENT.replace("metadata", "r.metadata")
# The runs started in [$1, $2) (a NULL bound is none) and, with $3, those of the tasks of project $3 that make up their
# cost (``task_costs``).
_IN_RANGE = (
    "($1::timestamptz IS NULL OR r.created_at >= $1) AND ($2::timestamptz IS NULL OR r.created_at < $2) "
    f"AND ($3::text IS NULL OR (t.project_id = $3 AND {_RUN_TASK_ROLE} IS NOT NULL))"
)
_SPEND = f"""count(*) AS runs,
               count(*) FILTER (WHERE r.status = 'running') AS running,
               count(*) FILTER (WHERE r.status <> 'running' AND r.cost_usd IS NULL) AS unpriced_runs,
               COALESCE(sum(r.cost_usd) FILTER (WHERE NOT {_RUN_EQUIVALENT}), 0) AS provider_priced_usd,
               COALESCE(sum(r.cost_usd) FILTER (WHERE {_RUN_EQUIVALENT}), 0) AS equivalent_usd"""


async def spend_since(since: datetime | None, *, until: datetime | None = None, project_id: str | None = None,
                      pool: asyncpg.Pool | None = None) -> dict[str, Any]:
    """What every agent's runs started at or after ``since`` (and before ``until``; None for no bound) cost, the
    Manager's included, split by basis; with ``project_id``, only the runs of that project's tasks that make up
    their cost (their Planners', Workers' and Reviewers', as ``task_costs`` counts them).

    ``provider_priced_usd`` is money: runs with an API key, at their
    provider's prices (Claude Code's figure for Anthropic's API, Looper's for
    any other). ``equivalent_usd`` is not: a runtime's own login (a
    subscription) at API prices (``EQUIVALENT``). ``runs`` counts every run,
    ``running`` those still going, ``unpriced_runs`` the finished ones without
    a cost.
    """
    pool = pool or get_pool()
    row = await pool.fetchrow(
        f"SELECT {_SPEND} FROM llm_records r LEFT JOIN tasks t ON t.id = r.task_id WHERE {_IN_RANGE}",
        since, until, project_id,
    )
    return dict(row)


async def task_spend(since: datetime | None, until: datetime | None = None, *, project_id: str | None = None,
                     pool: asyncpg.Pool | None = None) -> list[dict[str, Any]]:
    """What each task's runs started in [``since``, ``until``) cost (None for no bound), split by basis as
    ``spend_since`` splits it, most first: the runs that make up a task's cost (``task_costs``), of the tasks of
    ``project_id`` only when it is given. Each row has the task's ``name`` and its ``project_id`` and
    ``project_name`` now."""
    from .tasks import task_name

    pool = pool or get_pool()
    rows = await pool.fetch(
        f"""
        SELECT r.task_id, t.title, t.goal_id, t.requirement, t.project_id, p.name AS project_name, {_SPEND}
        FROM llm_records r JOIN tasks t ON t.id = r.task_id LEFT JOIN projects p ON p.id = t.project_id
        WHERE {_IN_RANGE} AND {_RUN_TASK_ROLE} IS NOT NULL
        GROUP BY r.task_id, t.title, t.goal_id, t.requirement, t.project_id, p.name
        ORDER BY sum(r.cost_usd) DESC NULLS LAST, r.task_id
        """,
        since, until, project_id,
    )
    return [{"task_id": row["task_id"], "name": task_name(title=row["title"], goal_id=row["goal_id"],
                                                          requirement=row["requirement"], task_id=row["task_id"]),
             **{key: row[key] for key in row.keys() if key not in ("task_id", "title", "goal_id", "requirement")}}
            for row in rows]


async def agent_usage(
    agent_id: str,
    *,
    turns: int = 100,
    pool: asyncpg.Pool | None = None,
) -> dict[str, Any]:
    """One agent's spending and context: totals, by session, by UTC day, and its latest ``turns`` runs.

    The turns come oldest first, each with its context at the end
    (``context_tokens``) and the compactions it went through; for a resident
    agent (the Manager, a task's Planner) they show its context over time.
    """
    pool = pool or get_pool()
    total = await pool.fetchrow(
        """
        SELECT count(*) AS runs,
               count(*) FILTER (WHERE status = 'running') AS running,
               count(*) FILTER (WHERE status <> 'running' AND cost_usd IS NULL) AS unpriced_runs,
               COALESCE(sum(cost_usd), 0) AS cost_usd,
               COALESCE(sum(jsonb_array_length(compactions)), 0) AS compactions
        FROM llm_records WHERE agent_id = $1
        """,
        agent_id,
    )
    sessions = await pool.fetch(
        """
        SELECT session_id, count(*) AS runs, COALESCE(sum(cost_usd), 0) AS cost_usd,
               min(created_at) AS first_at, max(created_at) AS last_at
        FROM llm_records WHERE agent_id = $1
        GROUP BY session_id ORDER BY max(created_at) DESC LIMIT 50
        """,
        agent_id,
    )
    days = await pool.fetch(
        """
        SELECT (created_at AT TIME ZONE 'UTC')::date AS day, count(*) AS runs, COALESCE(sum(cost_usd), 0) AS cost_usd
        FROM llm_records WHERE agent_id = $1
        GROUP BY day ORDER BY day DESC LIMIT 60
        """,
        agent_id,
    )
    latest = await pool.fetch(
        """
        SELECT id, created_at, status, session_id, cost_usd, context_tokens, compactions
        FROM llm_records WHERE agent_id = $1
        ORDER BY created_at DESC, id DESC LIMIT $2
        """,
        agent_id, turns,
    )
    turn_rows = [_row_to_dict(row) for row in reversed(latest)]
    return {
        "agent_id": agent_id,
        "total": dict(total),
        "by_session": [
            {**dict(row), "first_at": row["first_at"].isoformat(), "last_at": row["last_at"].isoformat()}
            for row in sessions
        ],
        "by_day": [{**dict(row), "day": row["day"].isoformat()} for row in days],
        "turns": turn_rows,
        "latest_context_tokens": next(
            (row["context_tokens"] for row in reversed(turn_rows) if row["context_tokens"] is not None), None,
        ),
    }
