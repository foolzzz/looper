"""Durable operator notices for vault commits whose owned branch could not be merged.

Each branch has its own setting row so concurrent writers cannot overwrite another
notice. Reads suppress a notice only once its commit is merged or its branch is
removed by the operator; they never clean up git state.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..db import connection
from . import vault

if TYPE_CHECKING:
    from .vault_commit import CommitResult

_PREFIX = "vault_commit.blocked."


async def record(result: CommitResult, *, repo: Path, db: Any = None) -> None:
    """Persist a blocked result before the writer finishes its durable bookkeeping.

    A caller already holding a database connection passes it here. Notification
    failures propagate so a blocked transaction cannot silently lose its notice.
    """
    if result.state != "blocked" or not result.branch:
        return
    key = _PREFIX + hashlib.sha256(f"{repo}\0{result.branch}".encode()).hexdigest()
    value = json.dumps({"repo": str(repo), "branch": result.branch, "worktree": str(result.worktree),
                        "commit": result.commit, "reason": str(result.reason)})
    await (db or connection.get_pool()).execute(
        "INSERT INTO system_settings (key, value) VALUES ($1, $2) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", key, value)


def _pending(value: dict[str, Any]) -> bool:
    repo = Path(value["repo"])
    # Distinguish an intentionally removed branch (handled) from an unreadable
    # repository (an attention source error, never an empty success).
    vault._git(repo, "rev-parse", "--git-dir")
    try:
        vault._git(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{value['branch']}")
    except subprocess.CalledProcessError as exc:
        if exc.returncode == 1:
            return False
        raise
    if not value.get("commit"):
        return True
    try:
        vault._git(repo, "merge-base", "--is-ancestor", value["commit"], "HEAD")
    except subprocess.CalledProcessError as exc:
        if exc.returncode == 1:
            return True
        raise
    return False


async def pending(*, db: Any = None) -> list[dict[str, Any]]:
    """Unmerged, preserved branches, oldest first; an unreadable source raises."""
    rows = await (db or connection.get_pool()).fetch(
        "SELECT key, value, updated_at FROM system_settings WHERE key LIKE $1 ORDER BY updated_at, key",
        _PREFIX + "%")
    found = []
    for row in rows:
        value = json.loads(row["value"])
        if await asyncio.to_thread(_pending, value):
            value["key"] = row["key"]
            value["since"] = row["updated_at"]
            found.append(value)
    return found
