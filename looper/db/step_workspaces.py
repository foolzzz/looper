"""The step workspaces (``step_workspaces``): a step's git worktree in a project with step workspaces.

The Planner writes a workspace's row before each git step that changes it and
reads its task's rows back with its plan; removing a task's workspaces and the
sweep at startup read them too. The states and their transitions are
``agents.step_workspaces``'.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import asyncpg

from .connection import get_pool

_COLUMNS = ("id", "task_id", "step_id", "repo_dir", "subdir", "target_branch", "path", "branch", "base_commit",
            "checkpoint", "result_commit", "merge_commit", "state", "merge_error", "detail", "removed_at", "links",
            "merge_files")

_SAVE = f"""
INSERT INTO step_workspaces ({", ".join(_COLUMNS)})
VALUES ({", ".join(f"${i}" for i in range(1, len(_COLUMNS) + 1))})
ON CONFLICT (id) DO UPDATE
SET result_commit = EXCLUDED.result_commit, merge_commit = EXCLUDED.merge_commit, state = EXCLUDED.state,
    merge_error = EXCLUDED.merge_error, detail = EXCLUDED.detail, removed_at = EXCLUDED.removed_at,
    links = EXCLUDED.links, merge_files = EXCLUDED.merge_files, updated_at = now()
"""


async def next_id(*, pool: asyncpg.Pool | None = None) -> int:
    """A new workspace's id, before its row: its directory and branch are named after it."""
    pool = pool or get_pool()
    return int(await pool.fetchval("SELECT nextval(pg_get_serial_sequence('step_workspaces', 'id'))"))


async def save(row: Mapping[str, Any], *, pool: asyncpg.Pool | None = None) -> None:
    """Write a workspace (``StepWorkspace.row``): its row the first time, then what changes."""
    pool = pool or get_pool()
    await pool.execute(_SAVE, *(row.get(column) for column in _COLUMNS))


async def get(workspace_id: int, *, pool: asyncpg.Pool | None = None) -> dict[str, Any] | None:
    pool = pool or get_pool()
    row = await pool.fetchrow("SELECT * FROM step_workspaces WHERE id = $1", workspace_id)
    return dict(row) if row else None


async def list_for_task(task_id: str, *, pool: asyncpg.Pool | None = None) -> list[dict[str, Any]]:
    """The task's workspaces, oldest first."""
    pool = pool or get_pool()
    rows = await pool.fetch("SELECT * FROM step_workspaces WHERE task_id = $1 ORDER BY id", task_id)
    return [dict(row) for row in rows]


async def task_start(task_id: str, *, pool: asyncpg.Pool | None = None) -> tuple[str, str]:
    """Where the task's branch was when it dispatched its first step (``task_plans``): ('', '') when not recorded."""
    pool = pool or get_pool()
    row = await pool.fetchrow("SELECT start_branch, start_commit FROM task_plans WHERE task_id = $1", task_id)
    return (str(row["start_branch"]), str(row["start_commit"])) if row else ("", "")


async def list_kept(*, pool: asyncpg.Pool | None = None) -> list[dict[str, Any]]:
    """Every workspace whose directory or branch is still there, with its task's status (``task_status``)."""
    pool = pool or get_pool()
    rows = await pool.fetch(
        """
        SELECT w.*, t.status AS task_status FROM step_workspaces w JOIN tasks t ON t.id = w.task_id
        WHERE w.removed_at IS NULL ORDER BY w.id
        """
    )
    return [dict(row) for row in rows]
