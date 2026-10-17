"""The task worktrees (``task_worktrees``): the git worktree a task runs in, in a worktree workspace.

The TaskRouter writes a task's row when it makes the worktree, as the task
first starts, and when it removes it; the sweep at startup reads the rows of
tasks that have ended. The states are ``agents.task_workspaces``'.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import asyncpg

from .connection import get_pool

_COLUMNS = ("task_id", "repo_dir", "subdir", "path", "branch", "base_commit", "links", "state", "result_commit",
            "removed_at")

_SAVE = f"""
INSERT INTO task_worktrees ({", ".join(_COLUMNS)})
VALUES ({", ".join(f"${i}" for i in range(1, len(_COLUMNS) + 1))})
ON CONFLICT (task_id) DO UPDATE
SET links = EXCLUDED.links, state = EXCLUDED.state, result_commit = EXCLUDED.result_commit,
    removed_at = EXCLUDED.removed_at
"""


async def save(row: Mapping[str, Any], *, pool: asyncpg.Pool | asyncpg.Connection | None = None) -> None:
    """Write a task's worktree (``TaskWorktree.row``): its row the first time, then what changes."""
    pool = pool or get_pool()
    await pool.execute(_SAVE, *(row.get(column) for column in _COLUMNS))


async def get(task_id: str, *, pool: asyncpg.Pool | asyncpg.Connection | None = None) -> dict[str, Any] | None:
    pool = pool or get_pool()
    row = await pool.fetchrow("SELECT * FROM task_worktrees WHERE task_id = $1", task_id)
    return dict(row) if row else None


async def list_active(*, pool: asyncpg.Pool | None = None) -> list[dict[str, Any]]:
    """Every worktree still there, with its task's status (``task_status``)."""
    pool = pool or get_pool()
    rows = await pool.fetch(
        "SELECT w.*, t.status AS task_status FROM task_worktrees w JOIN tasks t ON t.id = w.task_id "
        "WHERE w.state = 'active' ORDER BY w.created_at"
    )
    return [dict(row) for row in rows]
