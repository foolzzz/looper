"""In-process counters for scoped Manager refusals and session changes.

Counters live in this process from import. They are not model-visible and they
do not change an unscoped turn. ``resolution_recovered`` leaves the retry count
in place and clears the alert streak.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any, Literal

from .chat_scopes import ScopeInvariantViolation

started_at = datetime.now(timezone.utc)

Where = Literal["resolve", "launch", "session", "prepare"]
OpenReason = Literal["first", "scope_changed", "context_full"]
_WHERE = ("resolve", "launch", "session", "prepare")
_REASONS = ("first", "scope_changed", "context_full")

_start_refusals: Counter[tuple[str, str, str]] = Counter()
_invariants: Counter[tuple[str, str]] = Counter()
_retries: Counter[tuple[str]] = Counter()
_opened: Counter[tuple[str, str]] = Counter()


class MarkedInvariant(ScopeInvariantViolation):
    """A scope invariant raised at a known site. Handlers of the base class still catch it."""

    def __init__(self, where: Where, message: str) -> None:
        self.where = where
        super().__init__(message)


def start_refused(manager_id: str, runtime: str, mode: str) -> None:
    _start_refusals[(manager_id, runtime, mode)] += 1


def invariant(manager_id: str, where: Where) -> None:
    if where not in _WHERE:
        raise ValueError(f"unknown invariant site: {where}")
    _invariants[(manager_id, where)] += 1


def resolution_retry(manager_id: str) -> None:
    _retries[(manager_id,)] += 1


def resolution_recovered(manager_id: str, episode: str | None = None) -> None:
    """The retry count stays. This attempt's alert streak ends; other attempts stay."""
    from . import chat_scope_alerts

    chat_scope_alerts.clear_retry(manager_id, episode)


def session_opened(manager_id: str, reason: OpenReason) -> None:
    if reason not in _REASONS:
        raise ValueError(f"unknown session open reason: {reason}")
    _opened[(manager_id, reason)] += 1


def _listed(counter: Counter[tuple[str, ...]], fields: tuple[str, ...]) -> list[dict[str, Any]]:
    rows = [{field: key[index] for index, field in enumerate(fields)} | {"count": count}
            for key, count in counter.items()]
    return sorted(rows, key=lambda row: tuple(str(row[field]) for field in fields))


def view() -> dict[str, Any]:
    return {
        "started_at": started_at.isoformat(),
        "start_refusals": _listed(_start_refusals, ("manager_id", "runtime", "mode")),
        "invariants": _listed(_invariants, ("manager_id", "where")),
        "resolution_retries": _listed(_retries, ("manager_id",)),
        "sessions_opened": _listed(_opened, ("manager_id", "reason")),
    }


def residents() -> dict[str, Any]:
    """Started or reserved sessions, the machine cap, and the longest in-process queue wait.

    ``pool.waits`` keeps the latest 256 waits in this process. It has no timestamps, so
    ``max_wait_s_24h`` is the longest of that sample, not a clock-bounded 24 hours.
    """
    from .session_routing import max_resident_total
    from .tiers.manager_routing import pool

    by_manager: dict[str, int] = {}
    total = 0
    for router in list(pool.routers):
        count = sum(1 for session in router._agent._sessions.values()
                    if session.claude.started or session.key in router._reservations)
        if not count:
            continue
        manager_id = router._agent.agent_id
        by_manager[manager_id] = by_manager.get(manager_id, 0) + count
        total += count
    waits = [seconds for _, seconds in pool.waits]
    return {"total": total, "cap": max_resident_total(), "by_manager": dict(sorted(by_manager.items())),
            "max_wait_s_24h": max(waits) if waits else 0}


def reset() -> None:
    """Drop this process's counters. Tests use it; the start time stays."""
    _start_refusals.clear()
    _invariants.clear()
    _retries.clear()
    _opened.clear()
