"""Usage-window holds: wait until a reset, or for a cooldown taken only from stored timestamps.

A repeated admission or a restart does not extend a cooldown. Window rows are read only while
``engine.limit_signals.enabled`` is on; quota failures are always eligible. ``cost_guard.mode = off``
never calls this module.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Awaitable, Callable

from ..db import provider_limits, system_settings
from ..db.connection import get_pool
from . import limit_signals

logger = logging.getLogger(__name__)

HIGH_UTILIZATION = 0.95
COOLDOWN = timedelta(minutes=5)
CACHE_SECONDS = 5.0
# A weekly window's reset is visible for eight days; the bound also keeps the provider-day index seekable.
FAILURE_LOOKBACK = timedelta(days=8)
_CLAIM = "cost_guard_hold:"

Sender = Callable[..., Awaitable[tuple[bool, str | None]]]
_sender: Sender | None = None


class HoldReason(str, Enum):
    REJECTED = "rejected"
    HIGH_UTILIZATION = "high_utilization"
    QUOTA_FAILURE = "quota_failure"
    QUOTA_COOLDOWN = "quota_cooldown"


class ScheduleDeferred(Exception):
    """A firing the operator did not start waited. Nothing was started."""

    def __init__(self, until: datetime, reason: str):
        self.until = until
        self.reason = reason
        super().__init__(f"usage window on hold until {until.astimezone().strftime('%H:%M')}")


@dataclass(frozen=True)
class Hold:
    until: datetime
    reason: HoldReason
    reset_known: bool
    window: str | None


@dataclass
class _Snapshot:
    at: float
    windows: list[dict[str, Any]] | None  # None: limit signals are off, so the table was not read
    failure: dict[str, Any] | None


_cache: dict[str, _Snapshot] = {}
_deferred: dict[str, dict[str, Any]] = {}


def configure(sender: Sender | None) -> None:
    """Send hold notices through ``sender`` (channel id, chat, text). None: the server log only."""
    global _sender
    _sender = sender


def clear_cache() -> None:
    """Drop the short-lived reads and the in-memory schedule notes, as a restart does."""
    _cache.clear()
    _deferred.clear()


def note_schedule(schedule_id: str, *, until: datetime, reason: str, reset_known: bool,
                  window: str | None) -> None:
    _deferred[schedule_id] = {"until": until.isoformat(), "reason": reason, "reset_known": reset_known,
                              "window": window}


def clear_schedule(schedule_id: str) -> None:
    _deferred.pop(schedule_id, None)


def noted_schedule(schedule_id: str) -> dict[str, Any] | None:
    """The deferral note, including after ``until`` has passed. The schedule view hides an expired one."""
    return _deferred.get(schedule_id)


def schedule_hold(schedule_id: str, *, now: datetime | None = None) -> dict[str, Any] | None:
    """The firing's typed deferral, absent once ``until`` has passed."""
    noted = _deferred.get(schedule_id)
    if noted is None:
        return None
    until = _aware(noted["until"])
    if until is None or until <= (now or datetime.now(timezone.utc)):
        return None
    return noted


def _aware(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        at = value
    elif isinstance(value, str):
        try:
            at = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return at


def _failure_reset(failure: Mapping[str, Any]) -> Any:
    """The reset instant from ``metadata.failure.resets_at`` only. Error text is not a clock."""
    metadata = failure.get("metadata")
    if isinstance(metadata, str):
        import json
        try:
            metadata = json.loads(metadata)
        except ValueError:
            return None
    inner = metadata.get("failure") if isinstance(metadata, Mapping) else None
    return inner.get("resets_at") if isinstance(inner, Mapping) else None


def hold_from(windows: Sequence[Mapping[str, Any]] | None, failure: Mapping[str, Any] | None, *,
              background: bool, now: datetime) -> Hold | None:
    """The first matching rule, and within it the latest ``until``. An instant that is not still ahead holds nothing."""
    rejected: list[tuple[datetime, str | None]] = []
    high: list[tuple[datetime, str | None]] = []
    open_rejected: list[tuple[datetime, str | None]] = []
    open_high: list[tuple[datetime, str | None]] = []
    for row in windows or ():
        resets = _aware(row.get("resets_at"))
        observed = _aware(row.get("observed_at"))
        name = row.get("window_name") if isinstance(row.get("window_name"), str) else None
        future = resets is not None and resets > now
        if row.get("status") == "rejected" and future:
            rejected.append((resets, name))
            continue
        try:
            utilization = float(row["utilization"]) if row.get("utilization") is not None else None
        except (TypeError, ValueError):
            utilization = None
        if utilization is not None and utilization >= HIGH_UTILIZATION and future:
            high.append((resets, name))
        elif resets is None and observed is not None and row.get("status") == "rejected":
            open_rejected.append((observed + COOLDOWN, name))
        elif resets is None and observed is not None and utilization is not None and utilization >= HIGH_UTILIZATION:
            open_high.append((observed + COOLDOWN, name))
    if rejected:
        until, name = max(rejected, key=lambda item: item[0])
        return Hold(until, HoldReason.REJECTED, True, name)
    if high and background:
        until, name = max(high, key=lambda item: item[0])
        return Hold(until, HoldReason.HIGH_UTILIZATION, True, name)
    if failure is not None:
        resets = _aware(_failure_reset(failure))
        if resets is not None and resets > now:
            return Hold(resets, HoldReason.QUOTA_FAILURE, True, None)
        end = _aware(failure.get("finished_at")) or _aware(failure.get("created_at"))
        if end is not None and end + COOLDOWN > now:
            return Hold(end + COOLDOWN, HoldReason.QUOTA_COOLDOWN, False, None)
    candidates = list(open_rejected)
    if background:
        candidates.extend(open_high)
    active = [(until, name) for until, name in candidates if until > now]
    if not active:
        return None
    until, name = max(active, key=lambda item: item[0])
    reason = HoldReason.REJECTED if (until, name) in open_rejected else HoldReason.HIGH_UTILIZATION
    return Hold(until, reason, False, name)


async def read_windows(provider_id: str, *, pool=None) -> list[dict[str, Any]] | None:
    """Current window rows, or None when limit signals are off (the table is not read)."""
    if not await limit_signals.enabled():
        return None
    return [dict(row) for row in await provider_limits.windows(provider_id, pool=pool)]


async def latest_quota_failure(provider_id: str, *, now: datetime, pool=None) -> dict[str, Any] | None:
    """The provider's newest quota failure in the lookback. One index seek on ``idx_llm_records_provider_day``."""
    pool = pool or get_pool()
    row = await pool.fetchrow(
        "SELECT finished_at, created_at, metadata FROM llm_records "
        "WHERE (metadata->'profile'->'provider'->>'id') = $1 "
        "AND failure_class = 'quota' AND created_at >= $2 "
        "ORDER BY created_at DESC LIMIT 1",
        provider_id, now - FAILURE_LOOKBACK)
    return dict(row) if row is not None else None


async def _snapshot(provider_id: str, *, now: datetime, pool=None) -> _Snapshot:
    cached = _cache.get(provider_id)
    if cached is not None and time.monotonic() - cached.at < CACHE_SECONDS:
        return cached
    windows = await read_windows(provider_id, pool=pool)
    failure = await latest_quota_failure(provider_id, now=now, pool=pool)
    cached = _Snapshot(time.monotonic(), windows, failure)
    _cache[provider_id] = cached
    return cached


async def hold_for(provider_id: str, *, background: bool, now: datetime, pool=None) -> Hold | None:
    """The hold in force for ``provider_id`` at ``now``. ``background`` is false for a run the operator started."""
    if not provider_id:
        return None
    snapshot = await _snapshot(provider_id, now=now, pool=pool)
    return hold_from(snapshot.windows, snapshot.failure, background=background, now=now)


def notifies(hold: Hold) -> bool:
    """A known reset notifies only when the account is rejected. Every unknown reset notifies once."""
    return (not hold.reset_known) or hold.reason is HoldReason.REJECTED


def notice_text(provider_id: str, hold: Hold) -> str:
    label = hold.window or "失败记录"
    when = hold.until.astimezone().strftime("%Y-%m-%d %H:%M")
    if hold.reset_known:
        return f"用量窗口暂停：账号 {provider_id} 的 {label} 将等待到 {when}。后台工作会等待。"
    return f"用量窗口暂停：账号 {provider_id} 的 {label} 重置时间未知，最多等待到 {when}。后台工作会等待。"


def _notice_claim(provider_id: str, hold: Hold) -> tuple[str, str]:
    """One ``system_settings`` row per account and window (or the failure signal).

    The value is ``until`` when the reset is known, and the local date when it is not, so a new
    ``observed_at`` on that day does not send another notice or insert another row.
    """
    key = f"{_CLAIM}{provider_id}:{hold.window or 'failure'}"
    value = hold.until.isoformat() if hold.reset_known else hold.until.astimezone().date().isoformat()
    return key, value


async def notify_hold(provider_id: str | None, hold: Hold | None) -> None:
    """One direct message per account and window (or failure signal). A missing target is logged once."""
    if not provider_id or hold is None or not notifies(hold):
        return
    key, value = _notice_claim(provider_id, hold)
    try:
        claimed = await system_settings.claim(key, value)
    except Exception:
        logger.exception("could not claim the usage-window notice for %s", provider_id)
        return
    if not claimed:
        return
    text = notice_text(provider_id, hold)
    logger.warning("usage window hold: %s", text)
    try:
        from .gate_service import dashboard_target

        chat = await dashboard_target()
        if chat is None or _sender is None:
            return
        sent, error = await _sender(chat.channel_id, chat.target.id, text)
        if not sent:
            logger.warning("cannot send the usage-window notice to %s: %s", chat.channel_id, error)
    except Exception:
        logger.exception("cannot send the usage-window notice")


async def list_holds(now: datetime, *, pool=None) -> list[dict[str, Any]]:
    """Active holds for the settings view. Empty when the reads fail; the view itself still loads."""
    pool = pool or get_pool()
    providers: set[str] = set()
    if await limit_signals.enabled():
        for row in await provider_limits.windows(pool=pool):
            providers.add(row["provider_id"])
    rows = await pool.fetch(
        "SELECT DISTINCT metadata->'profile'->'provider'->>'id' AS provider_id FROM llm_records "
        "WHERE failure_class IS NOT NULL AND failure_class = 'quota' AND created_at >= $1 "
        "AND metadata->'profile'->'provider'->>'id' IS NOT NULL",
        now - FAILURE_LOOKBACK)
    providers.update(row["provider_id"] for row in rows if row["provider_id"])
    found = []
    for provider_id in sorted(providers):
        hold = await hold_for(provider_id, background=True, now=now, pool=pool)
        if hold is None:
            continue
        found.append({"provider": provider_id, "reason": hold.reason.value, "until": hold.until.isoformat(),
                      "window": hold.window, "reset_known": hold.reset_known})
    return found
