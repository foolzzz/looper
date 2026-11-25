"""Code-owned system hold notices sent to the task's recorded origin."""
import logging

from ..db import system_settings, tasks
from .task_notice import NotifyTarget

logger = logging.getLogger(__name__)


async def notify(task_id, payload, *, post):
    if payload.get('event') not in ('failure_hold_opened', 'failure_hold_reminder'):
        return
    if payload.get('kind') not in ('environment', 'breaker'):
        return
    row = await tasks.get_task(task_id)
    if row is None or row['status'] not in tasks.ACTIVE_STATUSES:
        return
    target = NotifyTarget.from_columns(row)
    channel = row.get('origin_channel_id')
    if target is None or not channel:
        return
    key = f"failure_notice:{payload['failure_id']}:{payload['event']}"
    if not await system_settings.claim(key, 'sent'):
        return
    sent = False
    try:
        text = (f"任务 {task_id} 的步骤 {payload['step_id']} 需要处理：{payload['kind']}（{payload['cause']}），"
                f"failure_id={payload['failure_id']}。可选择已修好后重试、交给 Planner 或停止任务；"
                "熔断后 Planner DECISION 或操作者 release/restart 可解除。")
        route = row.get('origin_reply_route') or {}
        receipt = await post(channel, target, text, reply_to=route.get('reply_to'), idempotency_key=key)
        sent, error = receipt.success, receipt.error
        if not sent:
            logger.error('Failure notice unavailable task=%s failure_id=%s: %s', task_id, payload['failure_id'], error)
    finally:
        if not sent:
            await system_settings.put_settings({key: None})
