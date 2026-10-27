"""Operator history cleanup, after both database eligibility and fleet retirement are checked."""

from __future__ import annotations

from typing import Any

from ..db import manager_cleanup as db_cleanup
from .ops import ErrorCode, OpError


def _runtime_blockers(fleet: Any, manager_id: str) -> list[str]:
    if fleet is None:
        return ["The Manager fleet is unavailable; retirement cannot be verified."]
    return fleet.cleanup_blockers(manager_id)


async def preview(manager_id: str, retention_days: int = 30, *, fleet: Any) -> dict[str, Any]:
    try:
        result = await db_cleanup.preview(manager_id, retention_days)
    except db_cleanup.CleanupIdentityError as exc:
        raise OpError(ErrorCode.CONFLICT, str(exc)) from None
    result["blockers"].extend(_runtime_blockers(fleet, manager_id))
    result["eligible"] = not result["blockers"]
    return result


async def purge(manager_id: str, retention_days: int = 30, *, fleet: Any) -> dict[str, Any]:
    def check_retired() -> None:
        blockers = _runtime_blockers(fleet, manager_id)
        if blockers:
            raise OpError(ErrorCode.CONFLICT, "; ".join(blockers))

    async def recheck() -> None:
        check_retired()

    check_retired()
    async with fleet.cleanup_guard(manager_id):
        check_retired()
        try:
            return await db_cleanup.purge(manager_id, retention_days, before_purge=recheck)
        except (db_cleanup.CleanupIdentityError, db_cleanup.CleanupConflict) as exc:
            raise OpError(ErrorCode.CONFLICT, str(exc)) from None
