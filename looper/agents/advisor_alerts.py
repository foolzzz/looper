"""Alerts about the advisor (the advisor RFC, section 15): they tell the operator, and never stop a consult.

- **The day's spend:** the advisor's consults since the server's local midnight spent more than
  ``advisor_daily_alert_usd`` (a system setting; ``DAILY_ALERT_USD`` when unset).
- **The consult rate:** a task consulted the advisor about more than half of its steps (of two or more).
- **Failures in a row:** the last ``FAILURES_IN_A_ROW`` consults to end, across tasks, all failed (failed, blocked
  or over budget; one the Planner withdrew, or a restart stopped, is not a failure).

Each kind is sent at most once a local day: the day it was last sent is stored (``system_settings``, under
``advisor_alert_sent:<kind>``), so a restart or a second process does not send it again.

An alert goes to the chat the dashboard's user gates use (``gate_service.dashboard_target``), through the sender
``configure`` gave (``looper serve``: its channels); with neither, only the server log has it.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime

from ..db import system_settings
from . import turn_metrics
from ..db.connection import get_pool

logger = logging.getLogger(__name__)

DAILY_ALERT_KEY = "advisor_daily_alert_usd"
DAILY_ALERT_USD = 30.0
FAILURES_IN_A_ROW = 3
# Ends that are not the advisor failing.
_NOT_FAILURES = ("withdrawn", "no_request", "interrupted")

Sender = Callable[..., Awaitable[tuple[bool, str | None]]]
_sender: Sender | None = None
SENT_KEY = "advisor_alert_sent:"  # + the kind: the local day it was last sent


def configure(sender: Sender | None) -> None:
    """Send alerts through ``sender`` (channel id, chat, text); None: only log them."""
    global _sender
    _sender = sender


async def daily_threshold() -> float:
    stored = (await system_settings.get_settings([DAILY_ALERT_KEY])).get(DAILY_ALERT_KEY)
    try:
        value = float(stored) if stored is not None else DAILY_ALERT_USD
    except ValueError:
        return DAILY_ALERT_USD
    return value if value > 0 else DAILY_ALERT_USD


async def consult_ended(task_id: str) -> None:
    """Check the alerts once a consult of ``task_id`` ended; an error is logged, never raised."""
    try:
        today = datetime.now().astimezone().date().isoformat()
        for kind, text in await _due(task_id):
            if await system_settings.claim(SENT_KEY + kind, today):
                await _send(text, task_id)
    except Exception:
        logger.warning("cannot check the advisor's alerts after a consult of task %s", task_id, exc_info=True)


async def _due(task_id: str) -> list[tuple[str, str]]:
    pool = get_pool()
    due: list[tuple[str, str]] = []
    midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    spend = await pool.fetchrow("SELECT COALESCE(sum(spent_usd), 0) AS usd, "
                                "bool_or(jsonb_array_length(COALESCE(result->'unpriced_runs', '[]')) > 0) AS unknown "
                                "FROM agent_spawns WHERE kind = 'advisor' AND created_at >= $1", midnight)
    spent = float(spend["usd"])
    threshold = await daily_threshold()
    if spend["unknown"]:
        due.append(("spend", f"告警：今天 advisor 的花费包含未知费用；已知小计 {spent:.2f} 美元，"
                             f"无法核实 {threshold:.2f} 美元的告警阈值。"))
    elif spent > threshold:
        due.append(("spend",
                    f"告警：今天 advisor 的花费 {spent:.2f} 美元，超过了 {threshold:.2f} 美元的告警阈值。"))
    row = await pool.fetchrow(
        "SELECT (SELECT count(DISTINCT step_id) FROM agent_spawns WHERE task_id = $1 AND kind = 'advisor' "
        "AND step_id IS NOT NULL) AS consulted, (SELECT count(*) FROM task_steps WHERE task_id = $1) AS steps",
        task_id)
    if row["steps"] >= 2 and row["consulted"] * 2 > row["steps"]:
        due.append(("rate", f"告警：任务 {task_id} 的 {row['steps']} 个步骤里有 {row['consulted']} 个请教过 "
                                       "advisor，超过一半。"))
    ends = await pool.fetch(
        "SELECT id, state IN ('failed', 'blocked', 'over_budget') AND NOT COALESCE(result->>'failure', '') = "
        "ANY($1::text[]) AS failed FROM agent_spawns WHERE kind = 'advisor' AND finished_at IS NOT NULL "
        "ORDER BY finished_at DESC, id DESC LIMIT $2", list(_NOT_FAILURES), FAILURES_IN_A_ROW + 1)
    streak = [r for r in ends[:FAILURES_IN_A_ROW] if r["failed"]]
    if len(streak) == FAILURES_IN_A_ROW and not (len(ends) > FAILURES_IN_A_ROW and ends[FAILURES_IN_A_ROW]["failed"]):
        due.append(("failures",
                    f"告警：advisor 的请教已连续 {FAILURES_IN_A_ROW} 次失败（跨任务），最近一次在任务 {task_id}。"))
    return due


async def _send(text: str, task_id: str | None = None) -> None:
    from .gate_service import dashboard_target

    logger.warning("advisor alert: %s", text)
    chat = await dashboard_target()
    if chat is None or _sender is None:
        return
    proactive = turn_metrics.proactive_kwargs(turn_metrics.ProactiveSource.ADVISOR_ALERT, task_id)
    sent, error = await _sender(
        chat.channel_id, chat.target.id, text,
        **({**proactive, "group": chat.target.group} if proactive else {}))
    if not sent:
        logger.warning("cannot send the advisor alert to %s: %s", chat.channel_id, error)
