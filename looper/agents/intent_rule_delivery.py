"""Recoverable, operator-only rule notices and the confirmation suppression boundary.

The notices contain task/MR references, never source message text. A session lock
covers each committed send claim and its receipt; a leftover sending claim is
uncertain and is never blindly retried.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
import logging
from typing import Any, Mapping

from ..db import connection, intent_rules as rule_store, proactive_deliveries
from . import leads_model, links, proactive_delivery as delivery, turn_metrics
from .channels.base import PostOutcome
from .message import channel_thread_id
from .operator_identity import operator_dm_target, operator_sender_id
from .subscription_runs import push_target

logger = logging.getLogger(__name__)
NOTICE_PREFIXES = ('rule-created:', 'rule-discovery:', 'rule-fault:')
NOTICE_ATTEMPTS = 3
NOTICE_LIMIT = 20


def is_rule_reference(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(NOTICE_PREFIXES)


async def confirmation_allowed(conn: Any, intent_id: int, *, rules_on: bool | None = None) -> bool:
    """Old intents have no work. New work must decide before any confirmation."""
    if rules_on is None:
        rules_on = await leads_model.rules_enabled(db=conn)
    if not rules_on:
        return True
    row = await conn.fetchrow('SELECT state,decision FROM intent_rule_work WHERE intent_id=$1', intent_id)
    return row is None or (row['state'] == 'decided' and row['decision'] == 'push_for_confirmation')


@asynccontextmanager
async def confirmation_locks(conn: Any, intent_ids: list[int]):
    """Use the push/engine gate through the final read and the network call."""
    from .intent_push import _LOCK_NAMESPACE

    held: list[int] = []
    try:
        for iid in sorted(set(intent_ids)):
            await conn.execute('SELECT pg_advisory_lock($1::int,$2::int)', _LOCK_NAMESPACE, iid % 2147483647)
            held.append(iid)
        yield
    finally:
        for iid in reversed(held):
            await conn.execute('SELECT pg_advisory_unlock($1::int,$2::int)', _LOCK_NAMESPACE, iid % 2147483647)


@asynccontextmanager
async def discovery_locks(conn: Any, discovery_ids: list[int]):
    held: list[str] = []
    try:
        for did in sorted(set(discovery_ids)):
            key = f"looper.intent_rules.discovery:{did}"
            await conn.execute('SELECT pg_advisory_lock(hashtextextended($1,0))', key)
            held.append(key)
        yield
    finally:
        for key in reversed(held):
            await conn.execute('SELECT pg_advisory_unlock(hashtextextended($1,0))', key)


def source_ref(notice: Mapping[str, Any]) -> str:
    if notice['kind'] == 'created':
        return f"rule-created:{notice['execution_id']}"
    if notice['kind'] == 'discovery_confirmation':
        return f"rule-discovery:{notice['discovery_id']}"
    return f"rule-fault:{notice['id']}"


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


async def notice_context(conn: Any, ref: str) -> dict[str, Any] | None:
    """Resolve only our explicit namespace; a notice id is never an intent id."""
    if not is_rule_reference(ref):
        return None
    prefix, raw_id = ref.split(':', 1)
    if not raw_id.isdecimal():
        return None
    column = {'rule-created': 'execution_id', 'rule-discovery': 'discovery_id', 'rule-fault': 'id'}[prefix]
    row = await conn.fetchrow(f'SELECT * FROM intent_rule_notices WHERE {column}=$1 ORDER BY id LIMIT 1', int(raw_id))
    if row is None or source_ref(row) != ref:
        return None
    result = dict(row)
    if row['execution_id'] is not None:
        execution = await conn.fetchrow('SELECT task_id,mode,mr_key,revoke_state FROM intent_rule_executions WHERE id=$1',
                                        row['execution_id'])
        if execution is None:
            return None
        result.update(dict(execution))
        result['mr_key'] = _json(result['mr_key'])
        evidence = await conn.fetchrow(
            'SELECT h.intent_id,h.discovery_id FROM intent_rule_hit_executions x '
            'JOIN intent_rule_hits h ON h.id=x.hit_id WHERE x.execution_id=$1 ORDER BY h.id LIMIT 1',
            row['execution_id'])
        result['intent_id'] = evidence['intent_id'] if evidence else None
        if evidence and evidence['discovery_id'] is not None:
            result['discovery_id'] = evidence['discovery_id']
    if result['discovery_id'] is not None:
        discovery = await conn.fetchrow('SELECT * FROM intent_rule_discoveries WHERE id=$1', result['discovery_id'])
        if discovery is None:
            return None
        result['discovery'] = dict(discovery)
        result['fallback_reason'] = await conn.fetchval('SELECT reason FROM intent_rule_work WHERE discovery_id=$1',
                                                        result['discovery_id'])
    return result


def notice_text(context: Mapping[str, Any], *, dashboard_url: str = "") -> str:
    """Code-generated text over typed identifiers; no title/summary/source body."""
    if context['kind'] == 'created':
        task = context['task_id']
        origin = (leads_model.format_intent_id(context['intent_id']) if context.get('intent_id') is not None else
                  f"发现 {context['discovery_id']}" if context.get('discovery_id') is not None else '规则执行记录')
        mr = context.get('mr_key') or {}
        created = '已人工确认创建' if context.get('mode') == 'manual' else '已按规则创建'
        reason = '人工确认' if context.get('mode') == 'manual' else '全部核验条件通过'
        return (f"{origin}：{created}评审任务 [{task}]({links.task_link(dashboard_url, task)})。\n"
                f"MR：{mr.get('canonical_url', '')}\n命中理由：{reason}。\n"
                f"撤销此任务：回复「撤销 {task}」。撤销只停止后续工作，已发出的评论无法撤回。")
    if context['kind'] == 'discovery_confirmation':
        discovery = context['discovery']
        reason = (context.get('fallback_reason') or context.get('reason')
                  or context.get('last_error_code') or '未满足自动创建条件')
        return (f"发现 {discovery['id']} 需要人工确认（{reason}）。\nMR：{discovery['mr_url']}\n"
                f"请到[意图页]({dashboard_url}/#intents)查看发现记录 {discovery['id']}，选择项目建任务或驳回。")
    return 'MR 核验连续不可用，自动创建已退回人工确认。请到意图页检查核验配置和待处理记录。'


async def _valid_target(channel: str | None, chat: str | None) -> tuple[str, str] | None:
    if not channel or not chat or push_target(channel, chat).group:
        return None
    from ..db import channel_configs
    from .channels.allowlist import SENDER_ID_FORMATS, sender_ids
    from .message import parse_channel_id

    row = await channel_configs.get_config(channel)
    parsed = parse_channel_id(channel)
    if not row or not row['enabled'] or parsed is None or chat != await operator_sender_id(channel):
        return None
    try:
        allowed = sender_ids(parsed.channel_type, row['config'].get(SENDER_ID_FORMATS[parsed.channel_type].field))
    except (ValueError, KeyError):
        return None
    return (channel, chat) if chat in allowed else None


async def notice_target(conn: Any, context: Mapping[str, Any]) -> tuple[str, str] | None:
    if context['kind'] == 'discovery_confirmation' or context.get('discovery'):
        config = await rule_store.get_config(conn)
        source = next((item for item in config['discovery_sources']
                       if item['id'] == context['discovery']['discovery_source_id']
                       and (item['enabled'] or context['kind'] == 'created')), None)
        if source is None:
            return (await _valid_target(context.get('channel_id'), context.get('chat'))
                    if context['kind'] == 'created' else None)
        target = source['private_target']
        current = await _valid_target(target['channel_id'], target['chat'])
        if current is not None or context['kind'] != 'created':
            return current
        # An execution is already committed. A changed discovery configuration
        # cannot erase its notice; the stored reference still needs current
        # operator identity and allowlist validation, with no group fallback.
        return await _valid_target(context.get('channel_id'), context.get('chat'))
    if context['kind'] == 'created':
        if context.get('intent_id') is not None:
            from .intent_push import target_for
            schedule_id = await conn.fetchval('SELECT schedule_id FROM intents WHERE id=$1', context['intent_id'])
            if schedule_id is not None:
                return await target_for(schedule_id)
        # The committed reference survives message deletion; it is still checked against current operator config.
        return await _valid_target(context.get('channel_id'), context.get('chat'))
    target = await operator_dm_target()
    return None if target is None else await _valid_target(target.channel_id, target.chat)


async def notice_visible(conn: Any, context: Mapping[str, Any]) -> bool:
    if not await leads_model.rules_enabled(db=conn):
        return False
    if context['kind'] == 'discovery_confirmation':
        return await conn.fetchval('SELECT state FROM intent_rule_discoveries WHERE id=$1',
                                   context['discovery_id']) == 'confirmation'
    return True


class NoticeDispatcher:
    def __init__(self, registry: Any, *, drain: Any = None, clock: Any = None, dashboard_url: str = ""):
        self._registry = registry
        self._dashboard_url = dashboard_url
        self._drain = drain
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def _enabled(self, conn: Any) -> bool:
        return ((self._drain is None or not self._drain.draining)
                and await leads_model.rules_enabled(db=conn)
                and await conn.fetchval("SELECT value='true' FROM system_settings WHERE key='delivery.enabled'") is True)

    async def run_once(self) -> None:
        pool = connection.get_pool()
        if not await self._enabled(pool):
            return
        rows = await pool.fetch("SELECT id FROM intent_rule_notices WHERE state='sending' OR "
                                "(state IN ('pending','failed') AND attempts<$1) ORDER BY id LIMIT $2",
                                NOTICE_ATTEMPTS, NOTICE_LIMIT)
        for row in rows:
            async with pool.acquire() as conn:
                key = f"looper.intent_rules.notice:{row['id']}"
                if not await conn.fetchval('SELECT pg_try_advisory_lock(hashtextextended($1,0))', key):
                    continue
                try:
                    await self._one(conn, row['id'])
                except Exception:
                    logger.warning('intent rule notice %s deferred: notification_failed', row['id'])
                finally:
                    await conn.execute('SELECT pg_advisory_unlock(hashtextextended($1,0))', key)

    async def _one(self, conn: Any, notice_id: int) -> None:
        row = await conn.fetchrow('SELECT * FROM intent_rule_notices WHERE id=$1', notice_id)
        if row is None or not await self._enabled(conn):
            return
        if row['state'] == 'sending':
            await conn.execute("UPDATE intent_rule_notices SET state='uncertain',last_error_code='notification_failed',"
                               "updated_at=$2 WHERE id=$1 AND state='sending'", notice_id, self._clock())
            return
        if row['state'] not in ('pending', 'failed') or row['attempts'] >= NOTICE_ATTEMPTS:
            return
        context = await notice_context(conn, source_ref(row))
        if context is None:
            return
        discovery_lock = (f"looper.intent_rules.discovery:{row['discovery_id']}"
                          if row['kind'] == 'discovery_confirmation' else None)
        if discovery_lock:
            await conn.execute('SELECT pg_advisory_lock(hashtextextended($1,0))', discovery_lock)
        try:
            await self._send(conn, context)
        finally:
            if discovery_lock:
                await conn.execute('SELECT pg_advisory_unlock(hashtextextended($1,0))', discovery_lock)

    async def _send(self, conn: Any, context: dict[str, Any]) -> None:
        if not await notice_visible(conn, context):
            return
        target = await notice_target(conn, context)
        if target is None:
            await conn.execute("UPDATE intent_rule_notices SET state='failed',last_error_code='notification_failed',updated_at=$2 "
                               "WHERE id=$1", context['id'], self._clock())
            return
        channel, chat = target
        ref, key = source_ref(context), context['dedupe_key']
        now = self._clock()
        thread = channel_thread_id(channel, chat, 'manager', 'manager')
        spoke = await conn.fetchval("SELECT max(created_at) FROM chat_messages WHERE session_id=$1 AND role='user'", thread)
        decided = await delivery.decide(
            delivery.Delivery(delivery.DeliveryKind.INTENT, delivery.Urgency.NORMAL, delivery.ChatKind.DIRECT),
            dedupe_key=key, channel_id=channel, chat=chat, last_user_message_at=spoke, source_ref=ref,
            task_id=context.get('task_id'), now=now)
        await conn.execute('UPDATE intent_rule_notices SET channel_id=$2,chat=$3,decision=$4,reason=$5,updated_at=$6 '
                           'WHERE id=$1', context['id'], channel, chat, decided.outcome.value, decided.reason.value, now)
        if decided.outcome is not delivery.Outcome.SEND:
            if decided.outcome is delivery.Outcome.DIGEST:
                await conn.execute("UPDATE intent_rule_notices SET state='digest' WHERE id=$1", context['id'])
            return
        if not await self._enabled(conn) or not await notice_visible(conn, context) or await notice_target(conn, context) != target:
            return
        claimed = await conn.fetchval("UPDATE intent_rule_notices SET state='sending',attempts=attempts+1,updated_at=$2 "
                                      "WHERE id=$1 AND state IN ('pending','failed') AND attempts<$3 RETURNING id",
                                      context['id'], now, NOTICE_ATTEMPTS)
        if claimed is None:
            return
        external = None
        try:
            receipt = await self._registry.post(channel, push_target(channel, chat), notice_text(context, dashboard_url=self._dashboard_url),
                                                idempotency_key=key, **turn_metrics.proactive_kwargs(
                                                    turn_metrics.ProactiveSource.INTENT, ref))
            outcome = receipt.result
            external = receipt.external_ids[0] if receipt.external_ids else None
        except Exception:
            outcome = PostOutcome.UNKNOWN
        state = {PostOutcome.SENT: 'sent', PostOutcome.REJECTED: 'failed', PostOutcome.UNKNOWN: 'uncertain'}[outcome]
        await conn.execute('UPDATE intent_rule_notices SET state=$2,external_id=$3,last_error_code=$4,updated_at=$5 '
                           'WHERE id=$1', context['id'], state, external,
                           None if state == 'sent' else 'notification_failed', self._clock())
        try:
            await proactive_deliveries.record_receipt(key, outcome.value, external)
        except Exception:
            logger.warning('intent rule notice %s receipt deferred: notification_failed', context['id'])
