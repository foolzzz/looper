"""Task CRUD operations."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any

import asyncpg

from . import journal as db_journal
from .connection import get_pool

# A ready task: created (from the dashboard or by the Manager) and never started. Only a user starts it
# (``start_ready_task``); a server start never resumes it.
READY = "init"


class TaskGroup(str, Enum):
    """What a task's status means at a glance: the dashboard counts tasks by group, the Tasks page filters by one."""

    NEEDS_YOU = "needs_you"  # its Planner runs, and a user gate of its plan waits for the user
    IN_PROGRESS = "in_progress"  # its Planner runs
    QUEUED = "queued"  # waiting for room under its project's Planner limit
    READY = "ready"  # created, never started
    ATTENTION = "attention"  # failed or stopped: it needs a look, a restart or a continue
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    OTHER = "other"  # a status this mapping does not know: counted and listed, never dropped


# Every status a task row can hold, and its group; the only place that groups them.
STATUS_GROUPS: Mapping[str, TaskGroup] = MappingProxyType({
    "assigned": TaskGroup.IN_PROGRESS,
    "running": TaskGroup.IN_PROGRESS,
    # The Planner's own statuses, which the TaskRouter stores as the task's ("stopping": winding down at its cap).
    "planning": TaskGroup.IN_PROGRESS,
    "executing": TaskGroup.IN_PROGRESS,
    "verifying": TaskGroup.IN_PROGRESS,
    "stopping": TaskGroup.IN_PROGRESS,
    # What the Planner reports while a user gate of the plan waits for the user (other steps may run meanwhile).
    "waiting_user": TaskGroup.NEEDS_YOU,
    "pending": TaskGroup.QUEUED,
    READY: TaskGroup.READY,
    "failed": TaskGroup.ATTENTION,
    "stopped": TaskGroup.ATTENTION,
    "completed": TaskGroup.COMPLETED,
    "cancelled": TaskGroup.CANCELLED,
})


# The statuses a Planner ends its task with (its final GOAL_STATUS); a user's stop or cancel ends it otherwise.
PLANNER_END_STATUSES = frozenset({"completed", "failed"})

# A task in one of these is queued or running: the sidebar's active count, the guard on deleting, and what a server
# start resumes (with a live plan) or else marks stopped.
ACTIVE_STATUSES = tuple(status for status, group in STATUS_GROUPS.items()
                        if group in (TaskGroup.NEEDS_YOU, TaskGroup.IN_PROGRESS, TaskGroup.QUEUED))

# A task in one of these ended without completing (failed, stopped or cancelled): a restart runs it again, from its
# saved plan or planned anew.
RESTARTABLE_STATUSES = tuple(status for status, group in STATUS_GROUPS.items()
                             if group in (TaskGroup.ATTENTION, TaskGroup.CANCELLED))


def task_group(status: str | None) -> TaskGroup:
    return STATUS_GROUPS.get(status or "", TaskGroup.OTHER)


def _group_filter(group: TaskGroup | None) -> tuple[list[str] | None, bool]:
    """``group`` as the statuses a row's status must be in (True) or, for ``OTHER``, not in (False)."""
    if group is None:
        return None, True
    if group is TaskGroup.OTHER:
        return list(STATUS_GROUPS), False
    return [status for status, of in STATUS_GROUPS.items() if of is group], True


# A task row plus its project's name, its spend cap (its own, a scheduled run's, over its project's) and the name of
# the schedule that created it.
_WITH_PROJECT = ("SELECT t.*, p.name AS project_name, COALESCE(t.budget_usd, p.task_budget_usd) AS task_budget_usd, "
                 "s.name AS schedule_name "
                 "FROM tasks t JOIN projects p ON p.id = t.project_id LEFT JOIN schedules s ON s.id = t.schedule_id")


def planner_agent_id(task_id: str) -> str:
    """The agent id of ``task_id``'s Planner, which keys its saved session."""
    return f"planner-{task_id}"


class FailKind(str, Enum):
    """Why a failed task failed, when code can tell (``tasks.fail_kind``)."""

    TASK_BUDGET = "task_budget"  # it reached its project's task spend cap (task_budget_usd)


# The most times a stored config is unwrapped from a JSON string (see _read_config).
_CONFIG_UNWRAP_LIMIT = 8


def _config_json(config: Mapping[str, Any] | None) -> str:
    """``config`` for the JSONB ``config`` column: an object, never a string."""
    if config is None:
        return "{}"
    if not isinstance(config, Mapping):
        raise TypeError(f"a task config must be an object, not {type(config).__name__}")
    return json.dumps(dict(config))


def _read_config(value: Any) -> dict[str, Any]:
    """A stored config as an object.

    asyncpg reads JSONB as its JSON text. Rows saved from the task page before
    configs were checked hold the object as a JSON string, wrapped once more
    on every save, so strings are unwrapped; anything that is not an object
    in the end reads as ``{}``.
    """
    for _ in range(_CONFIG_UNWRAP_LIMIT):
        if isinstance(value, dict):
            return value
        if not isinstance(value, str):
            break
        try:
            value = json.loads(value)
        except ValueError:
            break
    return {}


TITLE_MAX_CHARS = 60


def task_title(raw: Any) -> str | None:
    """A task's ``title`` as stored: its words on one line, at most ``TITLE_MAX_CHARS``; None for none."""
    text = " ".join(raw.split())[:TITLE_MAX_CHARS] if isinstance(raw, str) else ""
    return text or None


def task_name(*, title: str | None, goal_id: str | None, requirement: str | None, task_id: str) -> str:
    """What a task is called where it is listed or announced: its title (the Manager's, in the user's language),
    else its goal id, else the first line of its requirement, else its id."""
    for value in (title, goal_id):
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())
    first = (requirement or "").strip().split("\n", 1)[0].strip()
    return first or task_id


def _task(row: Mapping[str, Any]) -> dict[str, Any]:
    task = dict(row)
    if "config" in task:
        task["config"] = _read_config(task["config"])
    task["name"] = task_name(title=task.get("title"), goal_id=task.get("goal_id"),
                             requirement=task.get("requirement"), task_id=str(task.get("id") or ""))
    return task


async def create_task(
    *,
    task_id: str,
    requirement: str,
    project_id: str,
    goal_id: str = "",
    work_dir: str = "",
    config: dict[str, Any] | None = None,
    status: str = "pending",
    correlation_id: str = "",
    origin_channel_id: str = "",
    chat_id: str | None = None,
    chat_kind: str | None = None,
    chat_mention: str | None = None,
    original_user_text: str | None = None,
    title: str | None = None,
    schedule_id: str | None = None,
    budget_usd: float | None = None,
    pool: asyncpg.Pool | asyncpg.Connection | None = None,
) -> dict[str, Any]:
    """Insert a task; ``schedule_id`` names the schedule that fired it, ``budget_usd`` its own spend cap.

    ``chat_id``, ``chat_kind`` and ``chat_mention`` are who to tell when the task ends (``agents.task_notice``): a
    Telegram chat, a Lark sender's open_id, or a Lark group and the sender to @-mention there; None for a task that
    did not come from either. ``original_user_text`` is the user's message the
    Manager's task JSON answered (kept as history); ``title`` the few words the Manager names the task with.
    """
    pool = pool or get_pool()
    now = datetime.now(timezone.utc)
    config_json = _config_json(config)
    await pool.execute(
        """
        INSERT INTO tasks (id, requirement, goal_id, work_dir, project_id,
                           config, status, correlation_id, origin_channel_id, chat_id, original_user_text, title,
                           created_at, updated_at, schedule_id, budget_usd, chat_kind, chat_mention)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $13, $14, $15, $16, $17)
        ON CONFLICT (id) DO NOTHING
        """,
        task_id, requirement, goal_id, work_dir, project_id,
        config_json, status, correlation_id, origin_channel_id, chat_id, original_user_text, task_title(title), now,
        schedule_id, budget_usd, chat_kind, chat_mention,
    )
    return {"task_id": task_id, "created_at": now.isoformat()}


async def update_task_status(
    task_id: str,
    status: str,
    *,
    planner_id: str | None = None,
    result_summary: str | None = None,
    error: str | None = None,
    fail_kind: FailKind | None = None,
    pool: asyncpg.Pool | None = None,
) -> None:
    """Set the task's status; the fields given (not ``None``) are set too, and kept until a restart."""
    pool = pool or get_pool()
    now = datetime.now(timezone.utc)
    completed_at = now if status in ("completed", "failed", "cancelled") else None

    await pool.execute(
        """
        UPDATE tasks
        SET status = $2,
            planner_id = COALESCE($3, planner_id),
            result_summary = COALESCE($4, result_summary),
            error = COALESCE($5, error),
            updated_at = $6,
            completed_at = COALESCE($7, completed_at),
            fail_kind = COALESCE($8, fail_kind)
        WHERE id = $1
        """,
        task_id, status, planner_id, result_summary, error, now, completed_at,
        FailKind(fail_kind).value if fail_kind else None,
    )


async def update_task_phase(
    task_id: str,
    phase: str,
    *,
    pool: asyncpg.Pool | None = None,
) -> None:
    pool = pool or get_pool()
    await pool.execute(
        "UPDATE tasks SET phase = $2, updated_at = now() WHERE id = $1",
        task_id, phase,
    )


async def get_task(task_id: str, *, pool: asyncpg.Pool | None = None) -> dict[str, Any] | None:
    pool = pool or get_pool()
    row = await pool.fetchrow("SELECT * FROM tasks WHERE id = $1", task_id)
    if row is None:
        return None
    return _task(row)


async def get_task_with_steps(task_id: str, *, pool: asyncpg.Pool | None = None) -> dict[str, Any] | None:
    pool = pool or get_pool()
    task_row = await pool.fetchrow(f"{_WITH_PROJECT} WHERE t.id = $1", task_id)
    if task_row is None:
        return None

    step_rows = await pool.fetch(
        "SELECT * FROM task_steps WHERE task_id = $1 ORDER BY step_index",
        task_id,
    )
    result = _task(task_row)
    result["steps"] = [dict(r) for r in step_rows]
    result["journal"] = await db_journal.list_entries(task_id, pool=pool)
    return result


async def delete_task(
    task_id: str,
    *,
    pool: asyncpg.Pool | None = None,
) -> bool:
    pool = pool or get_pool()
    await pool.execute("DELETE FROM task_steps WHERE task_id = $1", task_id)
    await pool.execute("DELETE FROM activity WHERE task_id = $1", task_id)
    await pool.execute("DELETE FROM task_plans WHERE task_id = $1", task_id)
    await pool.execute("DELETE FROM agent_sessions WHERE agent_id = $1", planner_agent_id(task_id))
    result = await pool.execute("DELETE FROM tasks WHERE id = $1", task_id)
    return result == "DELETE 1"


async def reset_task_for_restart(
    task_id: str,
    *,
    keep_steps: bool = False,
    pool: asyncpg.Pool | None = None,
) -> bool:
    """Clear the task's outcome before it runs again.

    Planned anew, its steps, its plan, its Planner's session, its Planner's
    choices of Agent Profile and the roles its Planner defined go; continuing its
    plan (``keep_steps``), they stay.
    """
    pool = pool or get_pool()
    if not keep_steps:
        await pool.execute("DELETE FROM task_planner_profiles WHERE task_id = $1", task_id)
        await pool.execute("DELETE FROM task_roles WHERE task_id = $1", task_id)
        await pool.execute("DELETE FROM task_steps WHERE task_id = $1", task_id)
        await pool.execute("DELETE FROM task_plans WHERE task_id = $1", task_id)
        await pool.execute("DELETE FROM agent_sessions WHERE agent_id = $1", planner_agent_id(task_id))
    result = await pool.execute(
        """
        UPDATE tasks
        SET status = 'pending', phase = 'pending',
            error = NULL, completed_at = NULL, fail_kind = NULL,
            result_summary = NULL, updated_at = now()
        WHERE id = $1
        """,
        task_id,
    )
    return result == "UPDATE 1"


async def start_ready_task(task_id: str, *, pool: asyncpg.Pool | None = None) -> dict[str, Any] | None:
    """Move a ready task to ``pending``, once: its row, or None when there is no such task or it is not ready."""
    pool = pool or get_pool()
    row = await pool.fetchrow(
        "UPDATE tasks SET status = 'pending', updated_at = now() WHERE id = $1 AND status = $2 RETURNING *",
        task_id, READY,
    )
    return _task(row) if row is not None else None


async def mark_orphaned_tasks_stopped(
    *,
    keep: list[str] | None = None,
    pool: asyncpg.Pool | None = None,
) -> int:
    """Mark the tasks a previous server left running as stopped, except ``keep`` (resumed instead)."""
    pool = pool or get_pool()
    result = await pool.execute(
        """
        UPDATE tasks
        SET status = 'stopped', updated_at = now()
        WHERE status = ANY($1::text[]) AND completed_at IS NULL AND NOT (id = ANY($2::text[]))
        """,
        list(ACTIVE_STATUSES), list(keep or []),
    )
    count = int(result.split()[-1]) if result else 0
    return count


async def update_task_config(
    task_id: str,
    *,
    requirement: str | None = None,
    work_dir: str | None = None,
    config: dict[str, Any] | None = None,
    pool: asyncpg.Pool | None = None,
) -> bool:
    pool = pool or get_pool()
    sets: list[str] = []
    args: list[Any] = [task_id]
    idx = 2
    if requirement is not None:
        sets.append(f"requirement = ${idx}")
        args.append(requirement)
        idx += 1
    if work_dir is not None:
        sets.append(f"work_dir = ${idx}")
        args.append(work_dir)
        idx += 1
    if config is not None:
        sets.append(f"config = ${idx}")
        args.append(_config_json(config))
        idx += 1
    if not sets:
        return False
    sets.append("updated_at = now()")
    sql = f"UPDATE tasks SET {', '.join(sets)} WHERE id = $1 AND status = 'init'"
    result = await pool.execute(sql, *args)
    return result == "UPDATE 1"


async def set_task_budget(task_id: str, budget_usd: float, *, pool: asyncpg.Pool | None = None) -> bool:
    """Set the task's own spend cap (over its project's)."""
    pool = pool or get_pool()
    result = await pool.execute("UPDATE tasks SET budget_usd = $2, updated_at = now() WHERE id = $1",
                                task_id, budget_usd)
    return result == "UPDATE 1"


async def set_task_project(
    task_id: str,
    project_id: str,
    *,
    pool: asyncpg.Pool | None = None,
) -> bool:
    """Move a task, whatever its status, to another project; False when there is no such task."""
    pool = pool or get_pool()
    result = await pool.execute(
        "UPDATE tasks SET project_id = $2, updated_at = now() WHERE id = $1",
        task_id, project_id,
    )
    return result == "UPDATE 1"


# The filters a task list takes ($1 project, $2 status, $3 and $4 a group's statuses, $5 schedule).
_FILTERS = """
    ($1::text IS NULL OR t.project_id = $1) AND ($2::text IS NULL OR t.status = $2)
    AND ($3::text[] IS NULL OR (t.status = ANY($3::text[])) = $4) AND ($5::text IS NULL OR t.schedule_id = $5)
"""


async def list_tasks(
    *,
    limit: int = 50,
    offset: int = 0,
    project_id: str | None = None,
    status: str | None = None,
    group: TaskGroup | None = None,
    schedule_id: str | None = None,
    before: str | None = None,
    pool: asyncpg.Pool | None = None,
) -> list[dict[str, Any]]:
    """The newest tasks (by creation, then id), each with its ``project_name`` and ``schedule_name``; only one
    project's when ``project_id`` is given, one schedule's when ``schedule_id`` is, only those in ``status``, or in
    ``group``, when it is, and only those older than the task ``before`` when it is."""
    pool = pool or get_pool()
    statuses, inside = _group_filter(group)
    rows = await pool.fetch(
        f"""
        {_WITH_PROJECT}
        WHERE {_FILTERS}
          AND ($6::text IS NULL OR (t.created_at, t.id) < (SELECT b.created_at, b.id FROM tasks b WHERE b.id = $6))
        ORDER BY t.created_at DESC, t.id DESC LIMIT $7 OFFSET $8
        """,
        project_id, status, statuses, inside, schedule_id, before, limit, offset,
    )
    return [_task(r) for r in rows]


async def count_by_status(
    *,
    project_id: str | None = None,
    group: TaskGroup | None = None,
    schedule_id: str | None = None,
    pool: asyncpg.Pool | None = None,
) -> dict[str, int]:
    """How many tasks ``list_tasks`` with these filters holds in all, by status."""
    pool = pool or get_pool()
    statuses, inside = _group_filter(group)
    rows = await pool.fetch(
        f"SELECT t.status, count(*) AS n FROM tasks t WHERE {_FILTERS} GROUP BY t.status",
        project_id, None, statuses, inside, schedule_id,
    )
    return {row["status"]: int(row["n"]) for row in rows}


async def task_exists(task_id: str, *, pool: asyncpg.Pool | None = None) -> bool:
    pool = pool or get_pool()
    return bool(await pool.fetchval("SELECT EXISTS (SELECT 1 FROM tasks WHERE id = $1)", task_id))


async def status_counts(*, pool: asyncpg.Pool | None = None) -> list[dict[str, Any]]:
    """How many tasks are in each status, every task counted: one row per project and status
    (``project_id``, ``project_name``, ``status``, ``count``). A project without tasks has one row with
    ``status`` None and ``count`` 0; tasks of no project have ``project_id`` None."""
    pool = pool or get_pool()
    rows = await pool.fetch(
        """
        SELECT p.id AS project_id, p.name AS project_name, t.status, count(t.id) AS count
        FROM projects p FULL JOIN tasks t ON t.project_id = p.id
        GROUP BY p.id, p.name, t.status
        """,
    )
    return [dict(r) for r in rows]
