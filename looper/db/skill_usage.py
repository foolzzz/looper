"""Reads behind ``looper.agents.skill_usage``: Worker runs that loaded a project skill, their attempts and their Skill calls.

Every query is bounded by an index: the project's tasks (``tasks.project_id``), ``llm_records(task_id, created_at)``,
``step_attempts`` by task, and ``llm_record_events`` by its primary key.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Sequence

from .connection import get_pool

Database = Any


async def has_attempts(pool: Database | None = None) -> bool:
    """Whether the attempts of #1523 are recorded here at all (the table and the run link)."""
    return bool(await (pool or get_pool()).fetchval(
        "SELECT to_regclass('step_attempts') IS NOT NULL AND EXISTS (SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = 'llm_records' AND column_name = 'attempt_id')"))


async def owned_skills(project_id: str, pool: Database | None = None) -> list[dict[str, Any]]:
    rows = await (pool or get_pool()).fetch(
        "SELECT id, name, sha256, lifecycle, imported_at FROM library_items "
        "WHERE owner_project_id = $1 AND kind = 'skill' ORDER BY name COLLATE \"C\", id", project_id)
    return [dict(row) for row in rows]


async def version_hashes(item_ids: Sequence[str], pool: Database | None = None) -> dict[str, list[tuple[int, str]]]:
    rows = await (pool or get_pool()).fetch(
        "SELECT item_id, version, sha256 FROM skill_versions WHERE item_id = ANY($1::text[]) ORDER BY version",
        list(item_ids))
    found: dict[str, list[tuple[int, str]]] = {}
    for row in rows:
        found.setdefault(row["item_id"], []).append((int(row["version"]), row["sha256"]))
    return found


async def loaded_runs(project_id: str, since: datetime, until: datetime, *, attempts: bool,
                      pool: Database | None = None) -> list[dict[str, Any]]:
    """The project's Worker runs in the range whose record lists a project skill: id, attempt, runtime type, items."""
    rows = await (pool or get_pool()).fetch(
        f"SELECT r.id, {'r.attempt_id' if attempts else 'NULL::bigint AS attempt_id'}, r.created_at, "
        "COALESCE(r.metadata->'profile', r.metadata->'agent')->'runtime'->>'type' AS runtime, "
        "r.metadata->'library'->'items' AS items "
        "FROM llm_records r WHERE r.task_id IN (SELECT id FROM tasks WHERE project_id = $1) "
        "AND r.agent_id LIKE 'worker-%' AND r.created_at >= $2 AND r.created_at < $3 "
        "AND jsonb_path_exists(r.metadata, '$.library.items[*] ? (@.plugin == \"project\")') "
        "ORDER BY r.id", project_id, since, until)
    return [dict(row) for row in rows]


async def skill_calls(record_ids: Sequence[int], pool: Database | None = None) -> dict[int, set[str]]:
    """The ``skill`` argument of each Skill tool call in the stored events of ``record_ids``."""
    rows = await (pool or get_pool()).fetch(
        "SELECT e.record_id, b->'input'->>'skill' AS skill FROM llm_record_events e "
        "CROSS JOIN LATERAL jsonb_array_elements(CASE WHEN jsonb_typeof(e.event->'message'->'content') = 'array' "
        "THEN e.event->'message'->'content' ELSE '[]'::jsonb END) b "
        "WHERE e.record_id = ANY($1::bigint[]) AND e.event->>'type' = 'assistant' "
        "AND b->>'type' = 'tool_use' AND b->>'name' = 'Skill'", list(record_ids))
    found: dict[int, set[str]] = {}
    for row in rows:
        if row["skill"]:
            found.setdefault(int(row["record_id"]), set()).add(row["skill"])
    return found


async def attempts(project_id: str, since: datetime, until: datetime, pool: Database | None = None) -> list[dict[str, Any]]:
    """The project's attempts that started in the range and ran a Worker, with the facts the first-pass rule needs."""
    rows = await (pool or get_pool()).fetch(
        "SELECT a.id, a.trigger, a.decision, a.started_at, "
        "(SELECT count(*) FROM step_attempts b WHERE b.task_id = a.task_id AND b.task_run_no = a.task_run_no "
        " AND b.step_key = a.step_key) AS tries, "
        "(SELECT sum(r.cost_usd) FROM llm_records r WHERE r.attempt_id = a.id AND r.agent_id LIKE 'worker-%') AS cost "
        "FROM step_attempts a JOIN tasks t ON t.id = a.task_id "
        "WHERE t.project_id = $1 AND a.started_at >= $2 AND a.started_at < $3 AND a.worker_runs > 0 "
        "ORDER BY a.id", project_id, since, until)
    return [dict(row) for row in rows]


async def coverage_since(project_id: str, pool: Database | None = None) -> datetime | None:
    """When the project's first attempt was recorded: runs before it have no attempt."""
    return await (pool or get_pool()).fetchval(
        "SELECT min(a.started_at) FROM step_attempts a JOIN tasks t ON t.id = a.task_id WHERE t.project_id = $1",
        project_id)
