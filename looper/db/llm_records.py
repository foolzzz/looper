"""LLM invocation records — audit trail for agent sessions."""

from __future__ import annotations

import json
import logging
from collections.abc import Collection, Sequence
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Any

import asyncpg

from ..agents.run_usage import CostStatus, Usage
from .connection import get_pool

logger = logging.getLogger(__name__)


class RecordStatus(str, Enum):
    """Where one run stands (``llm_records.status``). Every status but ``running`` is a run that has ended."""

    RUNNING = "running"
    COMPLETED = "completed"  # it ended with a successful result
    FAILED = "failed"  # it ended without one: an error, no result, its time limit or its spend cap
    # Looper stopped it before it ended (a kill, its task stopped or cancelled, the server stopping), or the server
    # running it died (``end_unfinished_records``): how it would have ended is not known.
    INTERRUPTED = "interrupted"


class UsageStatus(str, Enum):
    """Coverage of an interrupted bill (``metadata.usage_status``); absent on ordinary completed runs."""

    PARTIAL = "partial"
    UNREPORTED = "unreported"


NO_INTERRUPTED_USAGE = "no_usage_reported_before_interruption"
_BILL_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens", "cost_usd")
_NO_USAGE = " AND ".join(f"{field} IS NULL" for field in _BILL_FIELDS)
_INTERRUPTED_USAGE_METADATA = (
    "COALESCE(metadata, '{}'::jsonb) || jsonb_build_object('usage_status', "
    f"CASE WHEN {_NO_USAGE} THEN '{UsageStatus.UNREPORTED.value}' ELSE '{UsageStatus.PARTIAL.value}' END) "
    f"|| CASE WHEN {_NO_USAGE} THEN jsonb_build_object('usage_reason', '{NO_INTERRUPTED_USAGE}') "
    "ELSE '{}'::jsonb END")


# Why a record found running at startup ended.
UNFINISHED_AT_START = "the server stopped before this run ended; it was found still running at the next start"


def normalize_record_metadata(metadata: Any) -> Any:
    """Add a read-only presentation of bridge-era evidence; never repair the stored record."""
    from ..agents.agent_settings import Layer

    if not isinstance(metadata, dict):
        return metadata
    metadata = {key: value for key, value in metadata.items()
                if key not in ("reservation", "cost_guard", "cost_guard_schedule_id")}
    def obj(value):
        return value if isinstance(value, dict) else {}

    def array(value):
        return value if isinstance(value, list) else []

    sources = obj(metadata.get("settings_sources"))
    layers = obj(metadata.get("prompt_layers"))
    profile, model = obj(metadata.get("profile")), obj(metadata.get("model_choice"))
    labels = {"builtin_default": "built-in default", "system_default": "Settings default",
              "manager_override": "Manager override", "agent_override": "agent override",
              "project_override": "project override", "agent": "the Agent Profile's default"}
    codes = {layer.value for layer in Layer}
    def source(value):
        return {"code": value if isinstance(value, str) and value in codes else None,
                "label": labels.get(value, value) if isinstance(value, str) and value not in codes else None}

    selected = {
        "harness": sources.get("harness", obj(layers.get("harness")).get("source")),
        "profile": sources.get("profile", profile.get("source", sources.get("agent"))),
        "model": model.get("source", sources.get("model")),
        "effort": sources.get("effort"), "run_budget_usd": sources.get("run_budget_usd"),
    }
    prompt = {key: metadata.get(field) for key, field in
              (("text", "system_prompt"), ("mode", "system_prompt_mode"), ("sha256", "system_prompt_sha256"))}
    command = metadata.get("command")
    if isinstance(command, list) and "--append-system-prompt" in command:
        at = command.index("--append-system-prompt") + 1
        if at < len(command) and isinstance(command[at], str):
            if "system_prompt" not in metadata:
                prompt["text"] = command[at]
            if "system_prompt_mode" not in metadata:
                prompt["mode"] = "append"
    library, project = obj(metadata.get("library")), obj(metadata.get("project_skills"))
    fields = ("kind", "name", "plugin", "scope", "project_id", "sha256")
    items = [{key: item.get(key) for key in fields} for item in array(library.get("items")) if isinstance(item, dict)]
    for item in items:
        if item["scope"] == "project" and item["project_id"] is None:
            item["project_id"] = library.get("project_id")
    for skill in array(project.get("skills")):
        if isinstance(skill, dict) and not any(i["kind"] == "skill" and i["name"] == skill.get("name")
                and i["plugin"] in (None, project.get("plugin", "project")) for i in items):
            items.append({"kind": "skill", "name": skill.get("name"), "plugin": project.get("plugin", "project"),
                          "scope": "project", "project_id": project.get("project_id"), "sha256": skill.get("sha256")})
    return {**metadata, "presentation": {"sources": {key: source(value) for key, value in selected.items()},
            "system_prompt": prompt, "library": {"items": items, "sha256": library.get("sha256"),
                "error": library.get("error"), "project_error": project.get("error")}}}


# Internal run attribution, which existing record views do not expose.
_UNVIEWED = ("parent_record_id", "run_kind", "attempt_id")


def _row_to_dict(row: asyncpg.Record) -> dict[str, Any]:
    d = {key: value for key, value in dict(row).items() if key not in _UNVIEWED}
    for key in ("created_at", "finished_at", "last_output_at"):
        if d.get(key) is not None:
            d[key] = d[key].isoformat()
    for key in ("metadata", "compactions"):
        if d.get(key) is not None:
            d[key] = json.loads(d[key])
    if "metadata" in d:
        d["metadata"] = normalize_record_metadata(d["metadata"])
    return d


# What kind of run a record is (``llm_records.run_kind``), and the agent ids that say it: every record but a spawned
# run's (an advisor's or a judge's, which names its kind) takes its kind from these. Records from before the run tree
# have no run_kind (its migration rewrites no row), and readers take theirs from these too (``RUN_KIND``). An agent
# id none of them matches has no kind.
RUN_KINDS = ("manager", "planner", "worker", "reviewer", "processor", "consult", "advisor", "advice_check",
             "outbound_check", "explorer", "leads_check", "memory_draft", "memory_retro")
_RUN_KIND_IDS: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    # (kind, exact ids, id prefixes)
    ("manager", ("manager",), ("manager-",)),
    ("planner", (), ("planner-",)),
    ("worker", (), ("worker-",)),
    ("reviewer", (), ("reviewer-",)),
    ("processor", (), ("processor:",)),
    ("leads_check", (), ("leads_check:",)),
    ("consult", (), ("consult-",)),
)


def run_kind_of(agent_id: str) -> str | None:
    """The kind of a run recorded as ``agent_id`` (``_RUN_KIND_IDS``); None when its id does not say."""
    return next((kind for kind, exact, prefixes in _RUN_KIND_IDS
                 if agent_id in exact or agent_id.startswith(prefixes)), None)


def run_kind_case(column: str) -> str:
    """``run_kind_of`` as SQL over ``column``."""
    def like(prefix: str) -> str:
        return prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"

    whens = []
    for kind, exact, prefixes in _RUN_KIND_IDS:
        tests = [f"{column} = '{value}'" for value in exact] + [f"{column} LIKE '{like(value)}'" for value in prefixes]
        whens.append(f"WHEN {' OR '.join(tests)} THEN '{kind}'")
    return f"CASE {' '.join(whens)} END"


# A record's kind for a reader that filters or groups on it: its run_kind, else what its agent id says.
RUN_KIND = f"COALESCE(run_kind, {run_kind_case('agent_id')})"


async def start_record(
    *,
    agent_id: str,
    task_id: str | None = None,
    session_id: str | None = None,
    input_text: str | None = None,
    metadata: dict[str, Any] | None = None,
    run_kind: str | None = None,
    parent_record_id: int | None = None,
    pool: asyncpg.Pool | None = None,
) -> int:
    """Open a ``running`` record for one Claude invocation; returns its id. Its kind is ``run_kind``, else what its
    agent id says (``run_kind_of``); ``parent_record_id`` is the run that started it."""
    if run_kind is not None and run_kind not in RUN_KINDS:
        raise ValueError(f"unknown run kind {run_kind!r}")
    pool = pool or get_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO llm_records
            (agent_id, task_id, session_id, input_summary, input_text, metadata, status, run_kind, parent_record_id)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb, 'running', $7, $8)
        RETURNING id
        """,
        agent_id, task_id, session_id,
        input_text[:500] if input_text else None, input_text,
        json.dumps(metadata) if metadata else None,
        run_kind or run_kind_of(agent_id), parent_record_id,
    )
    return row["id"]


async def link_task(record_id: int, task_id: str, *, pool: asyncpg.Pool | None = None) -> None:
    """A Manager turn whose calls all named one task belongs to it."""
    pool = pool or get_pool()
    await pool.execute("UPDATE llm_records SET task_id = $2 WHERE id = $1 AND task_id IS NULL", record_id, task_id)


async def record_state(record_id: int, *, pool: asyncpg.Pool | None = None) -> dict[str, Any] | None:
    """The record's status and original start time, without presentation conversion."""
    pool = pool or get_pool()
    row = await pool.fetchrow("SELECT status, created_at FROM llm_records WHERE id = $1", record_id)
    return dict(row) if row is not None else None


async def max_event_seq(record_id: int, *, pool: asyncpg.Pool | None = None) -> int:
    """The last persisted transcript position, or zero before its first event."""
    pool = pool or get_pool()
    return await pool.fetchval(
        "SELECT COALESCE(MAX(seq), 0) FROM llm_record_events WHERE record_id = $1", record_id,
    )


async def _write_applied(result: str, record_id: int, target: RecordStatus, pool: asyncpg.Pool) -> bool:
    if result == "UPDATE 1":
        return True
    current = await pool.fetchval("SELECT status FROM llm_records WHERE id = $1", record_id)
    if current is not None:
        logger.info("skipped llm record %s write: %s -> %s (requires %s)",
                    record_id, current, target.value, RecordStatus.RUNNING.value)
    return False


async def end_record_interrupted(record_id: int, reason: str, *, pool: asyncpg.Pool | None = None) -> bool:
    """Interrupt a running record with its last checkpoint; return whether it transitioned."""
    pool = pool or get_pool()
    result = await pool.execute(
        "UPDATE llm_records SET status = 'interrupted', finished_at = COALESCE(last_output_at, created_at), "
        f"error = $2, metadata = {_INTERRUPTED_USAGE_METADATA} WHERE id = $1 AND status = 'running'", record_id, reason,
    )
    return await _write_applied(result, record_id, RecordStatus.INTERRUPTED, pool)


async def checkpoint_usage(record_id: int, usage: Usage, cost_source: str, *,
                           pool: asyncpg.Pool | None = None) -> bool:
    """Replace a running record's cumulative bill; return whether the checkpoint applied."""
    pool = pool or get_pool()
    result = await pool.execute(
        "UPDATE llm_records SET input_tokens=$2, output_tokens=$3, cache_read_tokens=$4, "
        "cache_creation_tokens=$5, cost_usd=$6, cost_source=$7 WHERE id=$1 AND status='running'",
        record_id, *(getattr(usage, field) for field in _BILL_FIELDS), cost_source)
    return await _write_applied(result, record_id, RecordStatus.RUNNING, pool)


async def end_unfinished_records(*, keep: Collection[int] = (), pool: asyncpg.Pool | None = None) -> int:
    """End unkept records still ``running`` as ``interrupted``; returns how many.

    Called at startup before any agent runs, when each such record is a run the
    previous server did not see end: it was killed, crashed or stopped first.
    It ended at its last output as far as anyone knows (its start without
    one). Reported usage checkpoints stay, marked partial; runs without one
    retain NULL usage and say why. Running it again changes nothing.
    """
    pool = pool or get_pool()
    statement = (
        "UPDATE llm_records SET status = $1, finished_at = COALESCE(last_output_at, created_at), "
        f"error = COALESCE(error, $2), metadata = {_INTERRUPTED_USAGE_METADATA} WHERE status = $3")
    args = [RecordStatus.INTERRUPTED.value, UNFINISHED_AT_START, RecordStatus.RUNNING.value]
    if keep:
        statement += " AND NOT (id = ANY($4::bigint[]))"
        args.append(list(keep))
    result = await pool.execute(statement, *args)
    return int(result.split()[-1])


def _event_json(event: dict[str, Any]) -> str:
    """Keep otherwise PostgreSQL-incompatible NUL text, marking the stored event."""
    sanitized = False

    def clean(value: Any) -> Any:
        nonlocal sanitized
        if isinstance(value, str) and "\0" in value:
            sanitized = True
            return value.replace("\0", "\\u0000")
        if isinstance(value, list):
            return [clean(item) for item in value]
        if isinstance(value, dict):
            return {clean(key): clean(item) for key, item in value.items()}
        return value

    stored = clean(event)
    if sanitized:
        stored["body_sanitized"] = True
    return json.dumps(stored, ensure_ascii=False)


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
        [(record_id, seq, _event_json(event)) for seq, event in events],
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
        record_id, seq, _event_json(event),
    )


async def touch_record(record_id: int, *, pool: asyncpg.Pool | None = None) -> None:
    """Mark that a running record just produced output (its heartbeat)."""
    pool = pool or get_pool()
    await pool.execute("UPDATE llm_records SET last_output_at = now() WHERE id = $1", record_id)


async def finish_record(
    record_id: int,
    *,
    status: RecordStatus | str,
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
    failure_class: str | None = None,
    failure_cause: str | None = None,
    pool: asyncpg.Pool | None = None,
) -> bool:
    """Close a record with the run's outcome, its own usage and cost (and where the cost comes from), its final
    context and its compactions; a Worker's or Reviewer's run also keeps how its report or verdict read
    (``metadata.outcome``, ``worker.RunOutcome``), and ``metadata``'s keys are added to the record's (a run's
    refused tool calls, ``permission_denials``). A failed run's class and cause (``failures``) are NULL while
    failure typing is off. Only a running record can settle; returns whether it did."""
    status = RecordStatus(status)
    if status is RecordStatus.RUNNING:
        raise ValueError("finish_record requires a terminal status")
    if status is RecordStatus.INTERRUPTED:
        reported = any(value is not None for value in
                       (input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens, cost_usd))
        metadata = {**(metadata or {}), "usage_status": (
            UsageStatus.PARTIAL if reported else UsageStatus.UNREPORTED).value}
        if not reported:
            metadata["usage_reason"] = NO_INTERRUPTED_USAGE
    pool = pool or get_pool()
    result = await pool.execute(
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
            metadata = (CASE WHEN $17::text IS NULL AND $18::jsonb IS NULL THEN metadata
                            ELSE COALESCE(metadata, '{}'::jsonb)
                                 || CASE WHEN $17::text IS NULL THEN '{}'::jsonb
                                         ELSE jsonb_build_object('outcome', $17::text) END
                                 || COALESCE($18::jsonb, '{}'::jsonb) END)
                       - CASE WHEN $2 = 'interrupted' THEN ARRAY[]::text[]
                              ELSE ARRAY['usage_status', 'usage_reason'] END,
            finished_at = now(),
            last_output_at = COALESCE(last_output_at, now()),
            failure_class = $19,
            failure_cause = $20
        WHERE id = $1 AND status = 'running'
        """,
        record_id, status.value, output_text, output_text[:500] if output_text else None,
        session_id, input_tokens, output_tokens, cost_usd, duration_ms, model, error,
        cache_read_tokens, cache_creation_tokens, context_tokens,
        json.dumps(compactions) if compactions is not None else None, cost_source, outcome,
        json.dumps(metadata, ensure_ascii=False) if metadata else None, failure_class, failure_cause,
    )
    return await _write_applied(result, record_id, status, pool)


async def add_metadata(record_id: int, metadata: dict[str, Any], *, pool: asyncpg.Pool | None = None) -> None:
    """Add ``metadata``'s keys to a record's metadata, after it ended: how a read-only Planner's turn was checked."""
    pool = pool or get_pool()
    await pool.execute(
        "UPDATE llm_records SET metadata = COALESCE(metadata, '{}'::jsonb) || $2::jsonb WHERE id = $1",
        record_id, json.dumps(metadata, ensure_ascii=False),
    )


async def increment_contract_rejections(
    record_id: int, contract_kind: str, *, pool: asyncpg.Pool | None = None,
) -> None:
    """Count a parser rejection on its turn, atomically and without retaining the rejected reply."""
    pool = pool or get_pool()
    await pool.execute(
        """UPDATE llm_records
           SET metadata = COALESCE(metadata, '{}'::jsonb) || jsonb_build_object(
               'contract_rejections', COALESCE(metadata->'contract_rejections', '{}'::jsonb)
               || jsonb_build_object($2::text,
                   COALESCE((metadata->'contract_rejections'->>$2::text)::integer, 0) + 1))
           WHERE id = $1""",
        record_id, contract_kind,
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
# record under their per-run instance id; the processor as processor:<owner>.
_ROLE_FILTERS = {
    "manager": "(agent_id = 'manager' OR agent_id LIKE 'manager-%')",
    "planner": "agent_id LIKE 'planner-%'",
    "worker": "agent_id LIKE 'worker-%'",
    "reviewer": "agent_id LIKE 'reviewer-%'",
    "processor": "agent_id LIKE 'processor:%'",
    "task_manager": "agent_id LIKE 'task-manager-%'",
}
# A task's step runs: its Workers' and its Reviewers'.
_STEP_RUNS = f"({_ROLE_FILTERS['worker']} OR {_ROLE_FILTERS['reviewer']})"
# The agents that work on one task, whose runs make up its cost; the operator's Manager is not one of them. The runs the Planner's
# consults of the advisor start (the advisor's and its judge's) and the reviews of a task's outbound actions are the
# Planner's.
TASK_ROLES = ("planner", "worker", "reviewer")
_PLANNER_SPAWNS = f"{RUN_KIND} IN ('advisor', 'advice_check', 'outbound_check', 'memory_draft', 'memory_retro')"
_ROLE_BRANCHES = " ".join(f"WHEN {_ROLE_FILTERS[role]} THEN '{role}'" for role in TASK_ROLES + ("task_manager",))
_TASK_ROLE = (
    f"CASE WHEN {RUN_KIND} = 'explorer' THEN 'explorer' WHEN {_PLANNER_SPAWNS} THEN 'planner' "
    f"{_ROLE_BRANCHES} END"
)
# A run of one of a task's agents, as task_costs counts them; the operator's Manager is not one.
TASK_RUN = f"{_TASK_ROLE} IS NOT NULL"
# The same runs, plus a room consult. The consult branch is before the id prefixes, so an explicit consult kind
# wins over an agent id that also matches a role prefix. Run listings stay on ``TASK_RUN``.
_SPEND_ROLE = (
    f"CASE WHEN {RUN_KIND} = 'explorer' THEN 'explorer' WHEN {_PLANNER_SPAWNS} THEN 'planner' "
    f"WHEN {RUN_KIND} = 'consult' THEN 'consult' {_ROLE_BRANCHES} END"
)
TASK_SPEND = f"{_SPEND_ROLE} IS NOT NULL"


async def manager_spend(
    manager_ids: Sequence[str],
    *,
    since: datetime,
    pool: asyncpg.Pool | None = None,
) -> dict[str, dict[str, Any]]:
    """Per Manager, its spend since ``since`` (today's start), over the last 7 days and its runs since ``since``;
    amounts are known subtotals; cost_status and unpriced_runs expose missing costs. Every id is answered."""
    pool = pool or get_pool()
    result = {m: {"today_usd": 0.0, "last_7d_usd": 0.0, "runs_today": 0, "unpriced_runs": 0,
                  "cost_status": CostStatus.KNOWN.value} for m in manager_ids}
    if not result:
        return result
    # A Manager's runs, and the outbound reviews of its calls that no task owns (``outbound_reviews.record_id``).
    rows = await pool.fetch(
        """
        SELECT agent_id,
               COALESCE(sum(cost_usd) FILTER (WHERE created_at >= $2), 0)::float8 AS today_usd,
               COALESCE(sum(cost_usd) FILTER (WHERE created_at >= $3), 0)::float8 AS last_7d_usd,
               count(*) FILTER (WHERE created_at >= $2) AS runs_today,
               count(*) FILTER (WHERE cost_usd IS NULL AND status <> 'running') AS unpriced_runs
        FROM (
            SELECT agent_id, cost_usd, created_at, status FROM llm_records WHERE agent_id = ANY($1::text[])
            UNION ALL
            SELECT o.agent_id, r.cost_usd, r.created_at, r.status FROM outbound_reviews o JOIN llm_records r ON r.id = o.record_id
            WHERE o.agent_id = ANY($1::text[]) AND o.task_id IS NULL
              AND o.created_at >= LEAST($2::timestamptz, $3::timestamptz)
        ) runs
        WHERE created_at >= LEAST($2, $3)
        GROUP BY agent_id
        """,
        list(result), since, datetime.now(timezone.utc) - timedelta(days=7),
    )
    for row in rows:
        result[row["agent_id"]] = {"today_usd": row["today_usd"], "last_7d_usd": row["last_7d_usd"],
                                   "runs_today": row["runs_today"], "unpriced_runs": row["unpriced_runs"],
                                   "cost_status": (CostStatus.UNKNOWN if row["unpriced_runs"] else CostStatus.KNOWN).value}
    return result


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


async def task_run_rows(task_id: str, *, pool: asyncpg.Pool | None = None) -> list[dict[str, Any]]:
    """A task's Planner, Worker and Reviewer runs, oldest first, as compact rows (no metadata body, prompts or
    events): ``id``, ``role``, ``agent_id``, ``step_id``, ``status``, ``created_at``, ``runtime`` ({type, name}),
    ``model`` (the one the run reported, else the one it chose or its profile named), ``effort`` and ``profile``
    ({name, source}) and ``task_role`` (the role its Planner defined that it ran as, else None), ``kind``
    (``RUN_KIND``: an advisor's or a judge's run among the Planner's) and ``parent_record_id`` (the run that started
    it); a run recorded before Agent Profiles has None for what it did not record."""
    pool = pool or get_pool()
    profile = "COALESCE(metadata->'profile', metadata->'agent')"
    rows = await pool.fetch(
        f"""
        SELECT id, {_TASK_ROLE} AS role, agent_id, metadata->>'step_id' AS step_id, status, created_at,
               {profile}->'runtime'->>'type' AS runtime_type, {profile}->'runtime'->>'name' AS runtime_name,
               COALESCE(model, metadata->'model_choice'->>'model', {profile}->>'model') AS model,
               metadata->'model_choice'->>'effort' AS effort,
               {profile}->>'name' AS profile_name, {profile}->>'source' AS profile_source,
               metadata->'prompt_layers'->'task_role' AS task_role, {RUN_KIND} AS kind, parent_record_id
        FROM llm_records WHERE task_id = $1 AND {TASK_RUN}
        ORDER BY created_at, id
        """,
        task_id,
    )
    return [
        {"id": r["id"], "role": r["role"], "agent_id": r["agent_id"], "step_id": r["step_id"],
         "status": r["status"], "created_at": r["created_at"].isoformat(),
         "runtime": {"type": r["runtime_type"], "name": r["runtime_name"]},
         "model": r["model"], "effort": r["effort"],
         "profile": {"name": r["profile_name"], "source": r["profile_source"]},
         "task_role": json.loads(r["task_role"]) if r["task_role"] else None,
         "kind": r["kind"], "parent_record_id": r["parent_record_id"]}
        for r in rows
    ]


async def process_record_runs(task_id: str, *, pool: asyncpg.Pool | None = None) -> list[dict[str, Any]]:
    """All task runs, with model requests separate from reports and no prompt/event bodies."""
    pool = pool or get_pool()
    rows = await pool.fetch(
        f"""SELECT id, {_TASK_ROLE} AS role, model,
            jsonb_build_object('profile',
                COALESCE(NULLIF(NULLIF(metadata->'profile', 'null'::jsonb), '{{}}'::jsonb), metadata->'agent'),
                'model_choice', metadata->'model_choice', 'fallback_from', metadata->'fallback_from',
                'prompt_layers', jsonb_build_object('task_role', metadata->'prompt_layers'->'task_role'))
            || CASE WHEN metadata ? 'model_reported' THEN
                jsonb_build_object('model_reported', metadata->'model_reported') ELSE '{{}}'::jsonb END
            || CASE WHEN metadata ? 'model_display' THEN
                jsonb_build_object('model_display', metadata->'model_display') ELSE '{{}}'::jsonb END
            || CASE WHEN metadata ? 'routed_model' THEN
                jsonb_build_object('routed_model', metadata->'routed_model') ELSE '{{}}'::jsonb END AS metadata
            FROM llm_records WHERE task_id = $1 AND {TASK_RUN} ORDER BY created_at, id""", task_id)
    return [{**dict(row), "metadata": json.loads(row["metadata"])} for row in rows]


async def list_records(
    *,
    agent_id: str | None = None,
    task_id: str | None = None,
    role: str | None = None,
    limit: int = 100,
    offset: int = 0,
    pool: asyncpg.Pool | None = None,
) -> list[dict[str, Any]]:
    """Record summaries newest first; full bodies and launch metadata are read with get_record."""
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
        f"""SELECT id, agent_id, task_id, session_id,
            LEFT(COALESCE(input_text, input_summary), 200) AS input_summary,
            LEFT(COALESCE(output_text, output_summary), 200) AS output_summary,
            COALESCE(char_length(input_text), 0) AS input_chars,
            COALESCE(char_length(output_text), 0) AS output_chars,
            input_tokens, output_tokens, duration_ms, model, error, metadata, created_at, status,
            cost_usd, finished_at, last_output_at, cache_read_tokens, cache_creation_tokens,
            context_tokens, compactions, cost_source, failure_class, failure_cause
            FROM llm_records {where} ORDER BY created_at DESC LIMIT ${idx} OFFSET ${idx + 1}""",
        *params,
    )
    records = [_row_to_dict(r) for r in rows]
    for record in records:
        metadata = record["metadata"]
        if isinstance(metadata, dict):
            # Normalize first so historical inline commands still supply the evidenced prompt mode.
            metadata.pop("system_prompt", None)
            metadata.pop("command", None)
            metadata["presentation"]["system_prompt"].pop("text", None)
    return records


async def get_record(
    record_id: int,
    *,
    include_attempt_id: bool = False,
    pool: asyncpg.Pool | None = None,
) -> dict[str, Any] | None:
    """Read an existing record view; only internal run recovery opts into attempt attribution."""
    pool = pool or get_pool()
    row = await pool.fetchrow("SELECT * FROM llm_records WHERE id = $1", record_id)
    if row is None:
        return None
    record = _row_to_dict(row)
    if include_attempt_id:
        record["attempt_id"] = row["attempt_id"]
    return record


# A run with a runtime's own login (a subscription: Claude Code's, priced by Claude Code; Codex's, priced by Looper
# at the built-in prices): what that usage would cost at API prices, not money spent. A run names its Agent Profile
# in metadata.profile, or metadata.agent on historical rows; explicit accounting evidence takes precedence.
EQUIVALENT = ("COALESCE(metadata->>'accounting_basis' = 'subscription_equivalent', "
              "COALESCE(metadata->'profile', metadata->'agent')->'provider'->>'auth' = 'oauth', false)")


# A hit ratio needs the whole prompt denominator from the same runs as its cache reads.
_CACHE_USAGE_REPORTED = "(input_tokens IS NOT NULL AND cache_read_tokens IS NOT NULL AND cache_creation_tokens IS NOT NULL)"


async def task_costs(
    task_ids: list[str],
    *,
    pool: asyncpg.Pool | None = None,
) -> dict[str, dict[str, Any]]:
    """Each task's cost: its task Manager, Planner, Workers, Reviewers and room consults, in total and by role.

    Every given task id gets an entry. ``runs`` counts every run; ``running``
    those still going, whose reported usage may already have a cost checkpoint; ``unpriced_runs`` the
    ended ones without a cost (Claude exited or was interrupted before it
    reported one, or their model has no price); ``equivalent_runs`` those
    whose cost is what a subscription's usage would cost at API prices
    (``EQUIVALENT``), and ``provider_price_runs`` the others Looper priced at
    their provider's prices.
    An ended run without a cost adds ``cost_status=unknown``;
    ``cost_usd`` remains the known subtotal. ``by_model`` uses the same status for its unknown groups.
    ``by_model`` splits those runs and tokens by model, with costs adding up to ``cost_usd``.
    ``cache_reported_runs`` and ``cache_missing_runs`` count complete and incomplete prompt usage;
    ``cache_hit_read_tokens`` / ``cache_hit_prompt_tokens`` uses only the complete runs, unlike token totals.
    ``manager_by_model`` splits the Manager's turns about the task by model, not in its cost or its spend cap.
    Money is rounded to a millionth of a dollar per group and summed exactly, so the totals, the roles and the
    models agree.
    """
    if not task_ids:
        return {}
    pool = pool or get_pool()
    rows = await pool.fetch(
        f"""
        SELECT task_id, CASE WHEN {_ROLE_FILTERS['manager']} THEN 'manager' ELSE {_SPEND_ROLE} END AS role,
               model, count(*) AS runs,
               count(*) FILTER (WHERE status = 'running') AS running,
               count(*) FILTER (WHERE status <> 'running' AND cost_usd IS NULL) AS unpriced_runs,
               array_agg(DISTINCT CASE
                   WHEN jsonb_typeof(metadata->'unknown_price_models') = 'array'
                        AND metadata->'unknown_price_models' <> '[]'::jsonb
                   THEN metadata->'unknown_price_models'
                   ELSE jsonb_build_array(COALESCE(metadata->>'model_reported', model, 'unknown model')) END)
                   FILTER (WHERE status <> 'running' AND cost_usd IS NULL AND (
                       COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0) + COALESCE(cache_read_tokens, 0)
                           + COALESCE(cache_creation_tokens, 0) > 0
                       OR metadata->>'cost_status' = 'unknown'
                       OR (jsonb_typeof(metadata->'unknown_price_models') = 'array'
                           AND metadata->'unknown_price_models' <> '[]'::jsonb))) AS unknown_models,
               count(*) FILTER (WHERE status = $2) AS interrupted_runs,
               count(*) FILTER (WHERE cost_usd IS NOT NULL AND {EQUIVALENT}) AS equivalent_runs,
               count(*) FILTER (WHERE cost_usd IS NOT NULL AND cost_source = 'provider_price' AND NOT {EQUIVALENT})
                   AS provider_price_runs,
               COALESCE(sum(input_tokens), 0)::bigint AS input_tokens,
               COALESCE(sum(output_tokens), 0)::bigint AS output_tokens,
               COALESCE(sum(cache_read_tokens), 0)::bigint AS cache_read_tokens,
               COALESCE(sum(cache_creation_tokens), 0)::bigint AS cache_creation_tokens,
               count(*) FILTER (WHERE {_CACHE_USAGE_REPORTED}) AS cache_reported_runs,
               count(*) FILTER (WHERE NOT {_CACHE_USAGE_REPORTED}) AS cache_missing_runs,
               COALESCE(sum(input_tokens::bigint + cache_read_tokens + cache_creation_tokens)
                   FILTER (WHERE {_CACHE_USAGE_REPORTED}), 0)::bigint AS cache_hit_prompt_tokens,
               COALESCE(sum(cache_read_tokens) FILTER (WHERE {_CACHE_USAGE_REPORTED}), 0)::bigint AS cache_hit_read_tokens,
               round(sum(cost_usd::numeric), 6) AS cost_usd
        FROM llm_records
        WHERE task_id = ANY($1::text[]) AND ({TASK_SPEND} OR {_ROLE_FILTERS['manager']})
        GROUP BY 1, 2, 3
        """,
        task_ids, RecordStatus.INTERRUPTED.value,
    )
    # An unused/off project keeps its existing response shape. Historical
    # explorer spend remains visible if an operator later disables the project.
    explorer_tasks = {row["task_id"] for row in rows if row["role"] == "explorer"}
    explorer_tasks.update(row["id"] for row in await pool.fetch(
        "SELECT t.id FROM tasks t JOIN projects p ON p.id=t.project_id "
        "WHERE t.id=ANY($1::text[]) AND p.subagents='on'", task_ids))
    # Consult is a key only for a task that has consult rows, not for every task.
    consult_tasks = {row["task_id"] for row in rows if row["role"] == "consult"}
    task_manager_tasks = {row["task_id"] for row in rows if row["role"] == "task_manager"}
    costs = {
        task_id: {"cost_usd": Decimal(0), "unknown_models": set(), "runs": 0, "running": 0, "unpriced_runs": 0, "equivalent_runs": 0,
                  "provider_price_runs": 0, "by_role": {role: {"cost_usd": Decimal(0), "runs": 0}
                                                      for role in TASK_ROLES
                                                      + (("explorer",) if task_id in explorer_tasks else ())
                                                      + (("task_manager",) if task_id in task_manager_tasks else ())
                                                      + (("consult",) if task_id in consult_tasks else ())},
                  "by_model": {}, "manager_by_model": {}}
        for task_id in task_ids
    }
    for row in rows:
        cost = costs[row["task_id"]]
        manager = row["role"] == "manager"
        if not manager:
            for key in ("runs", "running", "unpriced_runs", "equivalent_runs", "provider_price_runs"):
                cost[key] += row[key]
            cost["cost_usd"] += row["cost_usd"] or Decimal(0)
            for models in row["unknown_models"] or ():
                cost["unknown_models"].update(json.loads(models) if isinstance(models, str) else models)
            role = cost["by_role"][row["role"]]
            role["cost_usd"] += row["cost_usd"] or Decimal(0)
            role["runs"] += row["runs"]
        models = cost["manager_by_model" if manager else "by_model"]
        counts = ("runs", "running", "unpriced_runs", "interrupted_runs", "input_tokens", "output_tokens",
                  "cache_read_tokens", "cache_creation_tokens", "cache_reported_runs", "cache_missing_runs",
                  "cache_hit_prompt_tokens", "cache_hit_read_tokens")
        model = models.setdefault(row["model"], {"model": row["model"], "cost_usd": None,
                                                **dict.fromkeys(counts, 0)})
        for key in counts:
            model[key] += row[key]
        if row["cost_usd"] is not None:
            model["cost_usd"] = (model["cost_usd"] or Decimal(0)) + row["cost_usd"]
    for task_id, cost in costs.items():
        cost["unknown_models"] = sorted(cost["unknown_models"])
        cost["cost_status"] = (CostStatus.UNKNOWN if cost["unpriced_runs"] else CostStatus.KNOWN).value
        cost["cost_usd"] = float(cost["cost_usd"])
        for role in cost["by_role"].values():
            role["cost_usd"] = float(role["cost_usd"])
        for key in ("by_model", "manager_by_model"):
            cost[key] = sorted(cost[key].values(), key=lambda model: (
                model["cost_usd"] is None, -(model["cost_usd"] or Decimal(0)), -model["runs"],
                model["model"] is None, model["model"] or ""))
            for model in cost[key]:
                model["cost_status"] = (CostStatus.UNKNOWN if model["unpriced_runs"] else CostStatus.KNOWN).value
                if model["cost_usd"] is not None:
                    model["cost_usd"] = float(model["cost_usd"])
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


# ``_SPEND_ROLE`` and ``EQUIVALENT`` of the runs ``r`` of a query that joins their tasks ``t``.
_RUN_SPEND_ROLE = _SPEND_ROLE.replace("agent_id", "r.agent_id")
_RUN_EQUIVALENT = EQUIVALENT.replace("metadata", "r.metadata")
# The runs started in [$1, $2) (a NULL bound is none). With $3, only the runs of project $3's tasks that make up
# their cost, room consults included, the same way ``task_costs`` counts them. A null project does not filter by
# role, so that total already includes room consults and the Manager.
_IN_RANGE = (
    "($1::timestamptz IS NULL OR r.created_at >= $1) AND ($2::timestamptz IS NULL OR r.created_at < $2) "
    f"AND ($3::text IS NULL OR (t.project_id = $3 AND {_RUN_SPEND_ROLE} IS NOT NULL))"
)
_SPEND = f"""count(*) AS runs,
               count(*) FILTER (WHERE r.status = 'running') AS running,
               count(*) FILTER (WHERE r.status <> 'running' AND r.cost_usd IS NULL) AS unpriced_runs,
               COALESCE(sum(r.cost_usd) FILTER (WHERE NOT {_RUN_EQUIVALENT}), 0) AS provider_priced_usd,
               COALESCE(sum(r.cost_usd) FILTER (WHERE {_RUN_EQUIVALENT}), 0) AS equivalent_usd"""


async def spend_since(since: datetime | None, *, until: datetime | None = None, project_id: str | None = None,
                      manager_id: str | None = None, project_ids: Sequence[str] | None = None,
                      pool: asyncpg.Pool | None = None) -> dict[str, Any]:
    """What every agent's runs started at or after ``since`` (and before ``until``; None for no bound) cost, the
    Manager's included, split by basis. With no ``project_id`` every run in the window is counted, so room consults
    are already in that total. With ``project_id``, only the runs of that project's tasks that make up their cost,
    room consults included, the same way ``task_costs`` counts them. With ``manager_id``, only the runs of that
    Manager and of its tasks.

    ``provider_priced_usd`` is money: runs with an API key, at their
    provider's prices (Claude Code's figure for Anthropic's API, Looper's for
    any other). ``equivalent_usd`` is not: a runtime's own login (a
    subscription) at API prices (``EQUIVALENT``). ``runs`` counts every run,
    ``running`` those still going, ``unpriced_runs`` the finished ones without
    a cost.
    """
    pool = pool or get_pool()
    by_manager, args = (("", ()) if manager_id is None
                        else (" AND (r.agent_id = $4 OR t.manager_id = $4)", (manager_id,)))
    if project_ids is not None:
        if manager_id is None:
            by_manager, args = " AND t.project_id = ANY($4::text[])", (project_ids,)
        else:
            by_manager, args = " AND (r.agent_id = $4 OR t.project_id = ANY($5::text[]))", (manager_id, project_ids)
    row = await pool.fetchrow(
        f"SELECT {_SPEND} FROM llm_records r LEFT JOIN tasks t ON t.id = r.task_id WHERE {_IN_RANGE}{by_manager}",
        since, until, project_id, *args,
    )
    result = dict(row)
    result["cost_status"] = (CostStatus.UNKNOWN if result["unpriced_runs"] else CostStatus.KNOWN).value
    return result


async def task_spend(since: datetime | None, until: datetime | None = None, *, project_id: str | None = None,
                     manager_id: str | None = None, pool: asyncpg.Pool | None = None) -> list[dict[str, Any]]:
    """What each task's runs started in [``since``, ``until``) cost (None for no bound), split by basis as
    ``spend_since`` splits it, most first: the runs that make up a task's cost, room consults included, the same
    way ``task_costs`` counts them, of the tasks of ``project_id`` only when it is given, of the tasks of
    ``manager_id`` only when that is. Each row has the task's ``name`` and its ``project_id`` and
    ``project_name`` now."""
    from .tasks import task_name

    pool = pool or get_pool()
    by_manager, args = ("", ()) if manager_id is None else (" AND t.manager_id = $4", (manager_id,))
    rows = await pool.fetch(
        f"""
        SELECT r.task_id, t.title, t.goal_id, t.requirement, t.project_id, p.name AS project_name, {_SPEND}
        FROM llm_records r JOIN tasks t ON t.id = r.task_id LEFT JOIN projects p ON p.id = t.project_id
        WHERE {_IN_RANGE} AND {_RUN_SPEND_ROLE} IS NOT NULL{by_manager}
        GROUP BY r.task_id, t.title, t.goal_id, t.requirement, t.project_id, p.name
        ORDER BY sum(r.cost_usd) DESC NULLS LAST, r.task_id
        """,
        since, until, project_id, *args,
    )
    return [{"cost_status": (CostStatus.UNKNOWN if row["unpriced_runs"] else CostStatus.KNOWN).value,
             "task_id": row["task_id"], "name": task_name(title=row["title"], goal_id=row["goal_id"],
                                                          requirement=row["requirement"], task_id=row["task_id"]),
             **{key: row[key] for key in row.keys() if key not in ("task_id", "title", "goal_id", "requirement")}}
            for row in rows]


async def failure_stats(since: datetime, *, pool: asyncpg.Pool | None = None) -> dict[str, Any]:
    """The runs classified since ``since`` (``failures``): counted by UTC day, run kind, runtime, class and cause; by
    class; and, of the Workers' and Reviewers' runs (a step's failures, which go to its Planner), how many were not a
    judgment."""
    pool = pool or get_pool()
    rows = await pool.fetch(
        f"""SELECT (created_at AT TIME ZONE 'UTC')::date AS day, {RUN_KIND} AS run_kind,
                   COALESCE(metadata->'profile', metadata->'agent')->'runtime'->>'type' AS runtime,
                   failure_class, failure_cause, count(*) AS runs
            FROM llm_records WHERE failure_class IS NOT NULL AND created_at >= $1
            GROUP BY 1, 2, 3, 4, 5 ORDER BY 1, 2, 3, 4, 5""", since)
    from ..agents.failures import FailureClass

    by_class = {cls.value: 0 for cls in FailureClass}
    steps = {"failed": 0, "not_judgment": 0}
    for row in rows:
        by_class[row["failure_class"]] += row["runs"]
        if row["run_kind"] in ("worker", "reviewer"):
            steps["failed"] += row["runs"]
            steps["not_judgment"] += row["runs"] if row["failure_class"] != "judgment" else 0
    return {"runs": sum(by_class.values()), "by_class": by_class, "steps": steps,
            "rows": [{**dict(row), "day": row["day"].isoformat()} for row in rows]}


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
