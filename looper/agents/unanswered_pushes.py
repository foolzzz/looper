"""Ask once when a schedule's direct pushes go unanswered, then pause it.

The switch is independent of cost-guard mode. While it is off, ``check`` returns before any query.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from enum import Enum

from . import cost_guard_settings, cost_holds

logger = logging.getLogger(__name__)
_last_checked: datetime | None = None
_warned_day = None


class PauseReason(str, Enum):
    """Why ``schedules.enabled`` is false. This feature writes only ``unanswered`` and ``operator``."""

    OPERATOR = "operator"
    UNANSWERED = "unanswered"
    BUDGET = "budget"
    QUOTA = "quota"


async def check(now: datetime) -> None:
    """One pass at ``now``. Off: no query and the hourly clock stays where it was."""
    rule = cost_guard_settings.cached().unanswered
    if not rule.enabled:
        return
    global _last_checked, _warned_day
    if _last_checked is not None and now - _last_checked < timedelta(hours=1):
        return
    _last_checked = now
    from . import chat_settings

    if not chat_settings.metrics_enabled():
        if _warned_day != now.date():
            _warned_day = now.date()
            logger.warning("没人回的推送规则已打开，但 manager.metrics.enabled 是关的："
                           "没有推送和回复记录，本次不提问也不暂停")
        return
    # A restart can try before the channel is up, or the send can time out. ``asked_at`` stays null, so this
    # pass retries until the question is delivered. An unsent hold is not paused.
    await cost_holds.resume_unanswered_questions()
    await _ask(now, rule)
    await _pause(now, rule)


async def _ask(now: datetime, rule: cost_guard_settings.UnansweredRule) -> None:
    from ..db import unanswered_pushes as store

    rows = await store.schedules_to_ask(
        now - timedelta(days=rule.window_days), now - timedelta(minutes=30), now, rule.min_pushes)
    for row in rows:
        await cost_holds.create_hold(
            cost_holds.HoldScope.SCHEDULE, row["schedule_id"], reason="unanswered", pushes=row["pushes"])


async def _pause(now: datetime, rule: cost_guard_settings.UnansweredRule) -> None:
    from ..db.connection import get_pool
    from ..db import unanswered_pushes as store

    pool = get_pool()
    for hold_id in await store.due_hold_ids(now - timedelta(days=rule.pause_after_days), pool=pool):
        async with pool.acquire() as conn, conn.transaction():
            outcome = await store.claim_pause(conn, hold_id, now)
        if outcome and outcome.get("paused"):
            try:
                await cost_holds._paused_notice(outcome["id"], outcome.get("name"), rule.pause_after_days)
            except Exception:
                logger.exception("cannot send the unanswered pause notice for hold %s", outcome["id"])
