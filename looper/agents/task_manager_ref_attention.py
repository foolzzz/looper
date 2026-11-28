"""Ref reports that wait for the operator: a direct session's turn changed a branch or tag other than the task branch.

``attention.py`` turns each unresolved report that needs the operator into one "needs you" item. A change to the
task branch or to another Looper task's branch is not listed (RFC 25.9): it is in the task conversation only.
"""
from __future__ import annotations

from typing import Any

from ..db import task_manager_refs as store


async def pending() -> list[dict[str, Any]]:
    """One row per unresolved report that needs the operator, oldest first: ``id``, ``task_id``, the task's
    ``title``, ``goal_id`` and ``requirement`` (for its name), ``since`` and the changed ``refs`` (names only)."""
    return [{"id": row["id"], "task_id": row["task_id"], "title": row["title"], "goal_id": row["goal_id"],
             "requirement": row["requirement"], "since": row["created_at"],
             "refs": [change["ref"] for change in row["changes"].get("changes", [])]}
            for row in await store.pending_reports()]
