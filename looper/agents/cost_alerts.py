"""Code-written spend alerts, daily persistent claims and process-local repeat counts.

Admission creates immutable alert values. Callers publish them after releasing the guard lock (and, for
schedule firings, after committing). Repeat counters intentionally do not survive a process restart.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import TYPE_CHECKING

from ..db import proactive_deliveries, schedules as db_schedules, system_settings
from ..db.connection import get_pool
from . import chat_settings, proactive_delivery as delivery, turn_metrics

if TYPE_CHECKING:
    from .cost_guard import Currency, RunKind, Scope

logger = logging.getLogger(__name__)
Sender = Callable[..., Awaitable[tuple[bool, str | None]]]
_sender: Sender | None = None
_dashboard_url = ''


class AlertKind(str, Enum):
    NEAR_LIMIT = 'near_limit'
    LIMIT = 'limit'
    GUARD_ERROR = 'guard_error'


@dataclass(frozen=True)
class Alert:
    kind: AlertKind
    scope: Scope
    currency: Currency | None
    day: datetime
    spent_usd: float = 0.
    run_kind: RunKind | None = None
    schedule_id: str | None = None


@dataclass(frozen=True)
class BudgetSkipped:
    schedule_id: str
    scope: Scope
    limit: float
    spent: float
    currency: Currency | None = None
    day: datetime | None = None
    alerts: tuple[Alert, ...] = ()


# Only failed claims enter here. A process restart deliberately loses these counts.
_duplicates: dict[tuple[AlertKind, str, str, str, datetime], int] = {}
_cleaned_day: datetime | None = None
_summary_day: datetime | None = None


def configure(sender: Sender | None, *, dashboard_url: str = '') -> None:
    global _sender, _dashboard_url
    _sender, _dashboard_url = sender, dashboard_url


def _enforcing() -> bool:
    from . import cost_guard, cost_guard_settings

    return cost_guard.current().config.mode is cost_guard_settings.Mode.ENFORCE


def _key(item: Alert) -> tuple[AlertKind, str, str, str, datetime]:
    return (item.kind, item.scope.scope.value, item.scope.scope_ref,
            item.currency.value if item.currency is not None else 'none', item.day)


def _claim_key(key) -> str:
    kind, scope, ref, currency, _day = key
    return f'cost_guard_alert:{kind.value}:{scope}:{ref}:{currency}'


def _link() -> str:
    return 'Settings 花费：' + _dashboard_url.rstrip('/') + '/#settings/cost'


async def _scope_name(item: Alert) -> str:
    from .attention import _plain
    from .cost_guard import BudgetScope

    if item.scope.scope is BudgetScope.ROLE_DAY:
        return '后台角色 ' + _plain(item.scope.scope_ref)
    if item.scope.scope is BudgetScope.SCHEDULE_DAY:
        return '定时任务『' + await _schedule_name(item.scope.scope_ref) + '』'
    if item.scope.scope is BudgetScope.TASK:
        return '任务 ' + _plain(item.scope.scope_ref)
    return '整个 Looper'


async def _schedule_name(schedule_id: str) -> str:
    from .attention import _plain

    row = await db_schedules.get_schedule(schedule_id)
    return _plain(row['name'] if row else schedule_id)


async def _text(item: Alert) -> str:
    from .attention import _plain
    from .cost_guard import BACKGROUND, Currency, RunKind

    if item.kind is AlertKind.GUARD_ERROR:
        return '花费守卫：今天的花费计数或准入检查出错；定时触发检查失败时跳过本次触发，对外动作仍须通过审查。\n' + _link()
    scope = await _scope_name(item)
    currency = '等价花费' if item.currency is Currency.EQUIVALENT else '真钱'
    if item.kind is AlertKind.NEAR_LIMIT:
        return (f'花费提醒：{scope} 今天的{currency}已花及预留 ${item.spent_usd:g}，'
                f'到了上限 ${item.scope.limit_usd:g} 的 80%。\n' + _link())
    if item.run_kind is RunKind.SCHEDULE:
        consequence = '定时任务『' + await _schedule_name(item.schedule_id or item.scope.scope_ref) + '』这次触发已跳过'
    elif item.run_kind in BACKGROUND:
        consequence = '后台角色 ' + _plain(item.run_kind.value) + ' 今天不再运行'
    else:
        consequence = '本次运行已等待操作者决定'
    return f'花费上限：{scope} 今天的{currency}到了上限 ${item.scope.limit_usd:g}；{consequence}。\n' + _link()


async def flush(alerts: Sequence[Alert]) -> None:
    """Claim and deliver best effort; no alert failure changes the caller's admission."""
    if not alerts or not _enforcing():
        return
    for item in alerts:
        try:
            key = _key(item)
            if not await system_settings.claim(_claim_key(key), item.day.isoformat()):
                _duplicates[key] = _duplicates.get(key, 0) + 1
                continue
            from . import cost_guard

            await _send(await _text(item), _claim_key(key) + ':' + item.day.isoformat(), cost_guard.current().clock())
        except Exception:
            logger.warning('cannot publish spend alert %s', item.kind.value, exc_info=True)


async def publish_skip(notice: BudgetSkipped) -> None:
    """Publish a skipped firing only after its transaction committed."""
    await flush(notice.alerts)


async def rollover(day: datetime) -> None:
    """Summarize expired repeat counters even if another admission already rebuilt the guard's day."""
    global _cleaned_day, _summary_day
    previous = {key: count for key, count in _duplicates.items() if key[4] < day and count > 0}
    for key in previous:
        del _duplicates[key]
    if not _enforcing():
        return
    try:
        if previous and _summary_day != day:
            # Claim synchronously: an old-day flush may finish while this send awaits I/O.
            _summary_day = day
            from . import cost_guard
            from .attention import _plain

            scopes = {'role_day': '后台角色', 'schedule_day': '定时任务', 'task': '任务', 'global_day': '整个 Looper'}
            lines = [f"{scopes[scope]} {_plain(ref)}（{'等价花费' if currency == 'equivalent_usd' else '真钱'}）：{count} 次"
                     for (_kind, scope, ref, currency, _date), count in sorted(previous.items())]
            previous_day = max(key[4] for key in previous)
            await _send('昨天到上限之后又拦下：' + '；'.join(lines) + '。\n' + _link(),
                        'cost-guard-summary:' + previous_day.isoformat(), cost_guard.current().clock())
        if _cleaned_day != day:
            await get_pool().execute("DELETE FROM system_settings WHERE key LIKE 'cost_guard_alert:%' AND value < $1",
                                     (day - timedelta(days=2)).isoformat())
            _cleaned_day = day
    except Exception:
        logger.warning('cannot publish or clean up spend alert summary', exc_info=True)


async def _send(text: str, key: str, now: datetime) -> None:
    from .gate_service import dashboard_target

    logger.warning('spend alert: %s', text)
    target = await dashboard_target()
    if target is None or _sender is None or not _enforcing():
        return
    decided = chat_settings.delivery_enabled()
    if decided:
        outcome = await delivery.decide(
            delivery.Delivery(delivery.DeliveryKind.ADVISOR_ALERT, delivery.Urgency.NORMAL,
                              delivery.ChatKind.GROUP if target.target.group else delivery.ChatKind.DIRECT),
            dedupe_key=key, channel_id=target.channel_id, chat=target.target.id, last_user_message_at=None, now=now)
        if outcome.outcome is not delivery.Outcome.SEND:
            return
    proactive = turn_metrics.proactive_kwargs(turn_metrics.ProactiveSource.ADVISOR_ALERT, dedupe_key=key)
    sent, error = await _sender(target.channel_id, target.target.id, text, group=target.target.group,
                               idempotency_key=key, **proactive)
    if decided:
        await proactive_deliveries.record_receipt(key, 'sent' if sent else 'rejected')
    if not sent:
        logger.warning('cannot send spend alert to %s: %s', target.channel_id, error)
