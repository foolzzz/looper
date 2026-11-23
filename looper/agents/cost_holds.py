"""Operator-owned spend decisions, notifications and task wakeups; never model context."""

from __future__ import annotations

import logging
import math

from ..db import cost_holds as store, tasks as db_tasks
from ..db.connection import get_pool
from .message import AgentAddress, MessageEnvelope, MessageType, TASK_ROUTER_ADDRESS, planner_address

logger = logging.getLogger(__name__)
_platform = None
_sender = None
_dashboard_url = ''


def needs_service():
    """Keep existing decisions reachable after enforcement is switched off."""
    from . import cost_guard, cost_guard_settings

    return (cost_guard.current().holds_need_recovery
            or cost_guard_settings.cached().mode is cost_guard_settings.Mode.ENFORCE)


async def initialize(*, pool=None):
    from . import cost_guard

    cost_guard.current().holds_need_recovery = await (pool or get_pool()).fetchval(
        "SELECT EXISTS (SELECT 1 FROM cost_holds WHERE state='pending' OR applied_at IS NULL)")


def configure(platform=None, *, sender=None, dashboard_url=''):
    global _platform, _sender, _dashboard_url
    _platform, _sender, _dashboard_url = platform, sender, dashboard_url


def _value(value):
    return value.value if hasattr(value, 'value') else value


async def create_hold(scope, ref, currency, limit, spent, reason='budget', *, task_id=None, estimate_usd=0,
                      platform=None, pool=None):
    from . import cost_guard

    task_id = task_id or (ref if _value(scope) == 'task' else None)
    row, created = await store.create_hold(_value(scope), ref, _value(currency), limit, spent, reason,
                                            task_id=task_id, estimate_usd=estimate_usd, pool=pool)
    # Keep recovery enabled until restart: clearing a cached flag on a tick can race a new hold commit.
    cost_guard.current().holds_need_recovery = True
    runtime = platform or _platform
    if runtime is not None and task_id:
        task = getattr(getattr(runtime, 'task_router', None), '_tasks', {}).get(task_id)
        if task is not None:
            task.status = 'awaiting_budget'
    if created:
        try:
            await _question(row)
        except Exception:
            # A notification failure never releases the waiting task; the dashboard still holds its decision.
            logger.exception('cannot notify spend hold %s', row['id'])
    return row, created


async def _question(row):
    from . import chat_settings, proactive_delivery as delivery
    from .gate_service import dashboard_target
    from .turn_metrics import ProactiveSource, proactive_kwargs

    label = f"C-{row['id']}"
    scope = '任务' if row['scope'] == 'task' else '今天的全局花费'
    currency = '等价花费' if row['currency'] == 'equivalent_usd' else '真钱'
    link = _dashboard_url.rstrip('/') + '/#todo'
    text = (f"{label}：{scope}的{currency}到了上限，已花及预留 ${row['spent_usd']:g} / 上限 ${row['limit_usd']:g}。\n"
            f"回复「继续 {label} <新上限美元>」或「停 {label}」；只有一条等待时也可回复「继续」或「停」。\n"
            f"24 小时未答复将停下。控制台：{link}")
    logger.warning('spend hold %s waits for the operator', label)
    target = await dashboard_target()
    if target is None or _sender is None:
        return
    key = f"cost-hold:{row['id']}"
    if chat_settings.delivery_enabled():
        await delivery.decide(delivery.Delivery(delivery.DeliveryKind.ADVISOR_ALERT, delivery.Urgency.BLOCKING,
            delivery.ChatKind.GROUP if target.target.group else delivery.ChatKind.DIRECT),
            dedupe_key=key, channel_id=target.channel_id, chat=target.target.id,
            last_user_message_at=None, source_ref=label,
            task_id=row['scope_ref'] if row['scope'] == 'task' else None)
    proactive = proactive_kwargs(ProactiveSource.ADVISOR_ALERT, row['scope_ref'] if row['scope'] == 'task' else None)
    sent, error = await _sender(target.channel_id, target.target.id, text, idempotency_key=key,
                                  group=target.target.group, **proactive)
    if not sent:
        logger.warning('cannot send spend hold %s: %s', label, error)


async def _continue_limit(conn, row, new_limit):
    """Validate and change the cap under the same transaction that decides the hold."""
    from . import cost_guard

    if new_limit is not None and (isinstance(new_limit, bool) or not isinstance(new_limit, (float, int))
                                 or not math.isfinite(new_limit) or new_limit < 0):
        raise ValueError('新的上限必须是非负有限美元金额。')
    if row['scope'] == 'task':
        current = await conn.fetchval("SELECT COALESCE(t.budget_usd,p.task_budget_usd) FROM tasks t "
            "JOIN projects p ON p.id=t.project_id WHERE t.id=$1 FOR UPDATE OF t", row['scope_ref'])
    elif row['scope'] == 'global_day':
        key = 'cost_guard.daily_equivalent_usd' if row['currency'] == 'equivalent_usd' else 'cost_guard.daily_usd'
        raw = await conn.fetchval("SELECT value FROM system_settings WHERE key=$1", key)
        current = float(raw) if raw is not None else None
    else:
        raise ValueError('此类花费等待暂不能继续。')
    spent = row['spent_usd']
    guard = cost_guard.current()
    # Midnight changes a global hold's applicable spend. Refresh uses the scheduler's own day boundary.
    if guard.config.mode.value == 'enforce' and guard.day is not None:
        for counter in guard.snapshot()['counters']:
            if (counter['scope'], counter['scope_ref'], counter['currency']) == (
                    row['scope'], row['scope_ref'], row['currency']):
                spent = counter['spent_usd'] + counter['reserved_usd']
                break
    projected = spent + row['launch_estimate_usd']
    if new_limit is None:
        if current is not None and (current <= spent or current < projected):
            raise ValueError(f"请回复「继续 C-{row['id']} <新上限美元>」，给出能覆盖 ${projected:g} 的上限金额。")
        return
    if new_limit <= spent or new_limit < projected or (current is not None and new_limit <= current):
        raise ValueError(f'新的上限必须高于当前上限，并覆盖已花、预留和本次预估金额（${projected:g}）。')
    if row['scope'] == 'task':
        await conn.execute('UPDATE tasks SET budget_usd=$2 WHERE id=$1', row['scope_ref'], new_limit)
    else:
        await conn.execute("INSERT INTO system_settings(key,value) VALUES ($1,$2) ON CONFLICT(key) "
                           "DO UPDATE SET value=EXCLUDED.value,updated_at=now()", key, repr(float(new_limit)))


async def decide(hold_id, decision, new_limit_usd=None, *, via, platform=None, pool=None):
    if decision not in ('continue', 'stop') or via not in ('dashboard', 'api', 'lark', 'telegram'):
        raise ValueError('无效的花费决定或来源。')
    if decision == 'stop' and new_limit_usd is not None:
        raise ValueError('停下时不应指定新上限。')
    if decision == 'continue':
        from . import cost_guard
        await cost_guard.refresh_day()
    pool = pool or get_pool()
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow('SELECT * FROM cost_holds WHERE id=$1 FOR UPDATE', hold_id)
        if row is None:
            raise ValueError(f'没有 C-{hold_id} 这条花费等待。')
        if row['state'] != 'pending':
            raise ValueError(f'C-{hold_id} 已处理。')
        if decision == 'continue':
            await _continue_limit(conn, row, new_limit_usd)
        row = dict(await conn.fetchrow("UPDATE cost_holds SET state=$2,decided_at=now(),decided_via=$3 "
            "WHERE id=$1 RETURNING *", hold_id, 'continued' if decision == 'continue' else 'stopped', via))
    if decision == 'continue':
        from . import cost_guard
        if cost_guard.current().config.mode.value == 'enforce':
            await cost_guard.refresh()
    await _apply(row, decision, platform or _platform, pool)
    return row


async def _wake(row, decision, platform, db):
    if platform is None or platform.bus is None:
        return False
    for task_id in row['task_ids']:
        persisted = await db_tasks.get_task(task_id, pool=db)
        if persisted is None or persisted['status'] not in db_tasks.ACTIVE_STATUSES:
            continue
        if decision == 'continue' and persisted['status'] == 'awaiting_budget':
            continue
        task = getattr(getattr(platform, 'task_router', None), '_tasks', {}).get(task_id)
        if task is not None and decision == 'continue':
            task.status = 'running'
        await platform.bus.publish(MessageEnvelope.create(source=AgentAddress('system', 'cost-holds'),
            target=planner_address(task_id) if decision == 'continue' else TASK_ROUTER_ADDRESS,
            message_type=MessageType.SYSTEM_EVENT if decision == 'continue' else MessageType.TASK_STOP,
            payload={'type': 'cost_hold_continue', 'task_id': task_id} if decision == 'continue' else
                    {'task_id': task_id, 'stop_note': 'stopped at a spend hold'}))
    return True


async def _apply(row, decision, platform, pool):
    try:
        if decision == 'continue':
            # Commit the status before a fast consumer can read the wakeup.
            await pool.execute("UPDATE tasks SET status='running' WHERE id=ANY($1::text[]) "
                "AND status='awaiting_budget' AND NOT EXISTS "
                "(SELECT 1 FROM cost_holds h WHERE h.state='pending' AND tasks.id=ANY(h.task_ids)) AND EXISTS "
                "(SELECT 1 FROM cost_holds WHERE id=$2 AND applied_at IS NULL)", row['task_ids'], row['id'])
        async with pool.acquire() as conn, conn.transaction():
            pending = await conn.fetchrow('SELECT * FROM cost_holds WHERE id=$1 AND applied_at IS NULL FOR UPDATE',
                                          row['id'])
            if pending is not None:
                if await _wake(dict(pending), decision, platform, conn):
                    await conn.execute('UPDATE cost_holds SET applied_at=now() WHERE id=$1', row['id'])
    except Exception:
        # The state and the work to wake survive a disconnected bus or a crash before acknowledgement.
        logger.exception('spend hold %s has a pending task wakeup', row['id'])


async def expire(*, platform=None, pool=None):
    pool = pool or get_pool()
    rows = await pool.fetch("UPDATE cost_holds SET state='expired',decided_at=now() "
                            "WHERE state='pending' AND created_at <= now()-interval '24 hours' RETURNING *")
    pending = await pool.fetch("SELECT * FROM cost_holds WHERE state <> 'pending' AND applied_at IS NULL "
                               "ORDER BY decided_at,id")
    for row in pending:
        await _apply(dict(row), 'continue' if row['state'] == 'continued' else 'stop', platform or _platform, pool)
    return [dict(row) for row in rows]
