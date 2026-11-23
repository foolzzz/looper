"""The per-file unified diff the memory page shows for a candidate's current revision.

Made by code from the vault's files as they are now (replaying the revision's changes without writing), never by the
page or the model. A candidate that is already written, rejected or dropped has nothing left to compare.
"""

from __future__ import annotations

import asyncio
import difflib
from typing import Any

from . import memory_model as model, memory_settings, memory_write, vault

# Where the revision's changes have not reached the vault yet.
_UNWRITTEN = frozenset({model.State.PENDING, model.State.CONFIRMED, model.State.WRITING, model.State.BLOCKED,
                        model.State.CONFLICT, model.State.EXPIRED})


def unified(path: str, before: bytes | None, after: bytes) -> str:
    old = before.decode("utf-8").splitlines(keepends=True) if before is not None else []
    new = after.decode("utf-8").splitlines(keepends=True)
    return "".join(difflib.unified_diff(
        old, new, f"a/{path}" if before is not None else "/dev/null", f"b/{path}"))


async def revision_diffs(state: str, changes: list[dict[str, Any]], db: Any = None) -> tuple[dict[str, str], str | None]:
    """``({path: unified diff}, problem)``: the diffs, or none and a stable reason code: ``not_configured``,
    ``vault_problem``, one of the writer's conflict reasons (``base_changed``, ``anchor_missing``) or ``unreadable``."""
    if state not in _UNWRITTEN:
        return {}, None
    settings, problem = await memory_settings.load(db)
    config, vault_problem = await vault.load()
    if settings is None or config is None:
        return {}, "vault_problem" if problem or vault_problem else "not_configured"
    try:
        after, before = await asyncio.to_thread(memory_write.replay, config.path, changes)
    except memory_write.WriteFailure as error:
        return {}, error.reason.value
    except (ValueError, OSError):
        return {}, "unreadable"
    return {path: unified(path, before[path], data) for path, data in after.items()}, None
