"""Cached directory keys; symlinks and unresolved paths are rechecked on selection."""

from __future__ import annotations

import os

import asyncpg


def canonical_directory(path: object) -> str:
    """The real absolute directory, or '' when no existing absolute path can be cached.

    An empty key for a missing path keeps it in the indexed recheck set: it may be
    created as a symlink later. A raw path differing from its key also stays in that
    set, so retargeting a symlink never relies on its old destination.
    """
    if not isinstance(path, str) or not os.path.isabs(path):
        return ""
    try:
        if not os.path.exists(path):
            return ""
        return os.path.normpath(os.path.realpath(path))
    except OSError:
        return ""


async def refresh_project_directories(conn: asyncpg.Connection, project_id: str) -> None:
    """Recompute stored real paths for one project's tasks and worktrees.

    A move can leave a task path on the old location when that location is now a
    symlink, and it rewrites worktree paths. Both are resolved after the rename.
    """
    rows = await conn.fetch(
        "SELECT id, work_dir, workspace_dir FROM tasks WHERE project_id = $1 "
        "AND (work_dir <> '' OR workspace_dir <> '')", project_id)
    for row in rows:
        await conn.execute(
            "UPDATE tasks SET work_dir_real = $2, workspace_dir_real = $3 WHERE id = $1",
            row["id"], canonical_directory(row["work_dir"]), canonical_directory(row["workspace_dir"]))
    trees = await conn.fetch(
        "SELECT w.task_id, w.path FROM task_worktrees w JOIN tasks t ON t.id = w.task_id "
        "WHERE t.project_id = $1 AND w.path <> ''", project_id)
    for row in trees:
        await conn.execute(
            "UPDATE task_worktrees SET path_real = $2 WHERE task_id = $1",
            row["task_id"], canonical_directory(row["path"]))
