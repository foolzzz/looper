"""Memory notices to the operator, claimed durably before asking a channel.

The session advisory lock covers committed claims and network I/O across processes. A leftover sending claim has
an unknowable outcome and is never retried. Polling also repairs missed result edits after a writer restart.
"""
from __future__ import annotations

import asyncio
import difflib
import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from ..db import channel_configs, connection, memory as db_memory, system_settings
from . import memory_model as model, memory_settings, memory_write, vault
from .channels.base import PostOutcome
from .message import LARK_CHANNEL_TYPES
from .operator_identity import OPERATOR_KEY
from .supervisor import cancel_tasks
from .subscription_runs import push_target
from .subscriptions import strip_mentions

logger = logging.getLogger(__name__)
TICK_SECONDS = 10.0
RESULT_WAIT_SECONDS = 20.0
_AWAITING_REPLY = 'awaiting_reply'
_LOCK_NAMESPACE = 741924
FINAL_STATES = frozenset({'written', 'blocked', 'conflict', 'rejected', 'expired', 'dropped'})
_KIND_NAMES = {'preference': '偏好', 'feedback': '反馈', 'decision': '决定', 'lesson': '经验',
               'fact': '事实', 'commitment': '承诺', 'person': '人物', 'project': '项目'}
_REASONS = {
    'vault_problem': '记忆区设置不再有效', 'not_git': '记忆区不在 Git 仓库中',
    'detached_head': '记忆区处于分离 HEAD', 'branch_mismatch': '记忆区分支已改变',
    'operation_in_progress': '记忆区有进行中的 Git 操作', 'dirty_target': '目标笔记有未提交的修改',
    'hook_rejected': 'Git hook 拒绝了提交', 'git_failed': 'Git 操作失败', 'git_timeout': 'Git 操作超时',
    'commit_mismatch': '提交内容与确认的改动不一致', 'worktree_failed': '无法创建写入工作目录',
    'interrupted': '写入被中断', 'base_changed': '目标笔记在确认后改变了',
    'anchor_missing': '目标笔记中的原文或标题已改变', 'target_changed': '目标笔记在写入期间改变了',
}
_active: MemoryPushService | None = None


def dashboard_link(candidate_id: int, *, dashboard_url: str | None = None) -> str:
    base = dashboard_url or (_active._dashboard_url if _active is not None else 'http://localhost:8767')
    return f'{base.rstrip("/")}/#/memory?candidate={model.format_id(candidate_id)}'


def final_text(row: Mapping[str, Any], *, dashboard_url: str | None = None) -> str:
    label = model.format_id(row['id'])
    match row['state']:
        case 'written':
            text = f"✅ {label} 已写入 {row.get('note_path') or '记忆区'}（commit {str(row.get('commit_sha') or '')[:8]}）"
        case 'blocked':
            text = f"⚠️ {label} 写入受阻：{_REASONS.get(row.get('reason'), '记忆写入未完成')}。处理后回「重试 {label}」"
        case 'pending':
            text = f'⚠️ {label} 已重新起草，请看最新推送并重新确认'
        case 'conflict' | 'drafting':
            text = f"⚠️ {label} {_REASONS.get(row.get('reason'), '内容需要重新起草')}，请查看新版本并重新确认"
        case 'rejected':
            text = f'❌ {label} 已驳回'
        case 'expired':
            text = f'⌛ {label} 已过期'
        case 'dropped':
            text = f'❌ {label} 未写入，请到记忆页查看：{dashboard_link(row["id"], dashboard_url=dashboard_url)}'
        case _:
            text = f'{label} 已确认，写入后通知你'
    return strip_mentions(text)


def push_text(row: Mapping[str, Any], revision: Mapping[str, Any], lines: list[str] | None, *,
              sources: list[Mapping[str, Any]] = (), pending_days: int = 7, dashboard_url: str | None = None,
              markdown: bool = False) -> tuple[str, bool]:
    """The 1,500-character limit counts changed and context lines, including markers and newlines."""
    label = model.format_id(row['id'])
    source = {'manager_turn': '私聊', 'task': '任务', 'lead': '消息线索'}[row['source_kind']]
    when = row['created_at'].strftime('%m-%d %H:%M')
    result = [f"📝 {label} · {_KIND_NAMES[row['kind']]}（来自：{source} · {when}）", row['statement']]
    complete = lines is not None and len('\n'.join(line for line in lines if line.startswith(('+', '-', ' ')))) <= 1500
    if row.get('untrusted'):
        leads = '、'.join(f"lead-{s['lead_id']}" for s in sources if s.get('lead_id') is not None)
        result.append(f'⚠️ 来自飞书消息，未经确认{f"（{leads}）" if leads else ""}')
    if row.get('flagged'):
        result.append('⚠️ 来源里有要求助手做事的话')
    if revision.get('large_removal'):
        removed = sum(item.get('removed', 0) for item in revision['diff_stats'].values())
        result.append(f'⚠️ 会删掉这篇笔记 {removed} 行')
    if row.get('hard_rule'):
        result.append('⚠️ 这条像是每次都要遵守的硬规则：可能还要写进全局指令（Looper 不写）')
    display = '\n'.join(lines or [])
    if not complete:
        display = display[:400]
    if display:
        # Render literal changes: Markdown links and images must not conceal the URLs being confirmed.
        result.append(re.sub(r'([\\`*_{}\[\]<>()#+\-.!|~])', r'\\\1', display) if markdown else display)
    if complete:
        result.append(f'回复「记」或「记 {label}」写入；「驳回 {label} [理由]」驳回。{pending_days} 天没处理会过期。')
    else:
        result.append(f'内容较长，请到记忆页确认：{dashboard_link(row["id"], dashboard_url=dashboard_url)}')
        result.append(f'「驳回 {label} [理由]」驳回。{pending_days} 天没处理会过期。')
    return strip_mentions('\n'.join(result)), complete


def changed_lines(root, changes) -> list[str]:
    """Replay validates bases and anchors, so a displayed removal always belongs to the confirmed operation."""
    after, before = memory_write.replay(root, changes)
    result = []
    for path, data in after.items():
        result.append(f'写入 {path}：')
        old, new = (before[path] or b'').decode('utf-8').splitlines(), data.decode('utf-8').splitlines()
        # Repeated Markdown rows are meaningful: autojunk would report an unchanged repeated suffix as replaced.
        matcher = difflib.SequenceMatcher(a=old, b=new, autojunk=False)
        for group in matcher.get_grouped_opcodes(n=1):
            for tag, left, right, start, end in group:
                if tag == 'equal':
                    result.extend(' ' + line for line in old[left:right])
                else:
                    if tag in ('replace', 'delete'):
                        result.extend('-' + line for line in old[left:right])
                    if tag in ('replace', 'insert'):
                        result.extend('+' + line for line in new[start:end])
    return result


async def target_for(db: Any = None) -> tuple[str, str] | None:
    # No vault reads, candidate queries or posts when disabled or not configured for DM pushes.
    raw = await memory_settings.stored(db)
    if raw is None or raw.get('enabled') is not True or not raw.get('push_channel'):
        return None
    channel = raw['push_channel']
    configured = await channel_configs.get_config(channel, pool=db)
    if not configured or not configured['enabled'] or configured['channel_type'] not in (*LARK_CHANNEL_TYPES, 'telegram'):
        return None
    sender = (configured.get('config') or {}).get(OPERATOR_KEY)
    if not isinstance(sender, str) or not sender.strip() or push_target(channel, sender.strip()).group:
        return None
    return channel, sender.strip()


@asynccontextmanager
async def _candidate_lock(candidate_id: int):
    async with connection.get_pool().acquire() as conn:
        key = candidate_id % 2147483647
        locked = await conn.fetchval('SELECT pg_try_advisory_lock($1::int,$2::int)', _LOCK_NAMESPACE, key)
        try:
            yield conn if locked else None
        finally:
            if locked:
                await conn.execute('SELECT pg_advisory_unlock($1::int,$2::int)', _LOCK_NAMESPACE, key)


async def reserve_result(conn: Any, row: Mapping[str, Any], *, channel_id: str, chat: str) -> int | None:
    """Reserve a result before confirmation commits, while the caller still owns the candidate row lock.

    The writer can hold that lock throughout git. Inserting beforehand avoids taking an FK key-share lock after
    the reply's deadline. The poller waits 20 seconds unless the reply explicitly releases this reservation.
    """
    if await target_for(conn) != (channel_id, chat):
        return None
    return await conn.fetchval(
        "INSERT INTO memory_posts(candidate_id,kind,revision,content_hash,complete,channel_id,chat,text,error,created_at) "
        "VALUES($1,'final',$2,$3,false,$4,$5,'',$6,clock_timestamp()) RETURNING id", row['id'], row['revision'],
        row['content_hash'], channel_id, chat, _AWAITING_REPLY)


async def settle_result(post_id: int | None) -> None:
    """An inline result consumes only its still-unsent reservation, without touching the locked candidate."""
    if post_id is not None:
        await connection.get_pool().execute(
            "DELETE FROM memory_posts WHERE id=$1 AND kind='final' AND state='pending' AND error=$2",
            post_id, _AWAITING_REPLY)


async def defer_result(candidate_id: int, *, channel_id: str, chat: str) -> None:
    """Release a reserved result immediately after the inline deadline, without waiting for the writer's lock."""
    if await target_for() != (channel_id, chat):
        return
    pool = connection.get_pool()
    released = await pool.fetchval(
        "UPDATE memory_posts SET error=NULL WHERE id=(SELECT id FROM memory_posts "
        "WHERE candidate_id=$1 AND kind='final' AND channel_id=$2 AND chat=$3 "
        "AND state='pending' AND error=$4 ORDER BY id DESC LIMIT 1) RETURNING id",
        candidate_id, channel_id, chat, _AWAITING_REPLY)
    if released is not None:
        return
    # A reservation may already have been delivered (for example after the replying process restarted).
    exists = await pool.fetchval(
        "SELECT EXISTS(SELECT 1 FROM memory_posts WHERE candidate_id=$1 AND kind='final' "
        "AND channel_id=$2 AND chat=$3 AND created_at >= COALESCE((SELECT created_at "
        "FROM memory_candidate_events WHERE candidate_id=$1 AND action IN ('confirm','retry') "
        "ORDER BY id DESC LIMIT 1), '-infinity'::timestamptz))", candidate_id, channel_id, chat)
    if exists:
        return
    # Compatibility for callers that register without a confirming transaction (the DM router reserves first).
    async with pool.acquire() as conn, conn.transaction():
        if await target_for(conn) != (channel_id, chat):
            return
        row = await conn.fetchrow('SELECT * FROM memory_candidates WHERE id=$1 FOR UPDATE', candidate_id)
        if row is not None:
            await _insert_final(conn, row, channel_id, chat, '')


async def _insert_final(conn, row, channel, chat, text):
    # Both deferred-result registration and an edit fallback hold this row lock, without waiting on a send.
    if await conn.fetchval('SELECT id FROM memory_candidates WHERE id=$1 FOR UPDATE', row['id']) is None:
        return
    # A later retry needs a fresh subscription; the append-only event provides the confirmation's durable boundary.
    since = await conn.fetchval("SELECT created_at FROM memory_candidate_events WHERE candidate_id=$1 "
                               "AND action IN ('confirm','retry','revive') ORDER BY id DESC LIMIT 1", row['id'])
    existing = await conn.fetchval("SELECT id FROM memory_posts WHERE candidate_id=$1 AND kind='final' "
        "AND revision=$2 AND channel_id=$3 AND chat=$4 AND ($5::timestamptz IS NULL OR created_at >= $5) "
        "ORDER BY id DESC LIMIT 1 FOR UPDATE", row['id'], row['revision'], channel, chat, since)
    if existing is not None:
        if text:
            await conn.execute("UPDATE memory_posts SET text=$2,error=NULL WHERE id=$1 AND state='pending'", existing, text)
        return
    await conn.execute("INSERT INTO memory_posts(candidate_id,kind,revision,content_hash,complete,channel_id,chat,text) "
        "VALUES($1,'final',$2,$3,false,$4,$5,$6)", row['id'], row['revision'], row['content_hash'], channel, chat, text)


class MemoryPushService:
    def __init__(self, registry: Any, *, drain: Any = None, clock: Callable[[], datetime] | None = None,
                 interval: float = TICK_SECONDS, dashboard_url: str | None = None):
        self._dashboard_url = dashboard_url or 'http://localhost:8767'
        self._registry, self._drain = registry, drain
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._interval, self._task = interval, None

    async def start(self):
        global _active
        _active = self
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name='memory-push')

    async def stop(self):
        global _active
        if _active is self:
            _active = None
        await cancel_tasks([self._task])
        self._task = None

    async def _loop(self):
        while True:
            try:
                await self.run_once()
            except Exception:
                logger.warning('memory push pass failed; deferred', exc_info=True)
            await asyncio.sleep(self._interval)

    async def run_once(self):
        if (self._drain is not None and self._drain.draining) or await target_for() is None:
            return
        pool = connection.get_pool()
        rows = await pool.fetch("SELECT id FROM memory_candidates c WHERE destination='vault' AND purged_at IS NULL "
            "AND (state='pending' OR EXISTS (SELECT 1 FROM memory_posts p WHERE p.candidate_id=c.id "
            "AND (p.state='sending' OR (p.kind='push' AND p.state='sent' AND (p.edited_at IS NULL OR p.edited_at<c.updated_at)) "
            "OR (p.kind='final' AND p.state='pending')))) ORDER BY id")
        for row in rows:
            try:
                await self.final_edit(row['id'])
            except Exception:
                logger.warning('memory candidate=%s push processing deferred', row['id'], exc_info=True)
        await self._alerts()

    async def final_edit(self, candidate_id):
        if await target_for() is None:
            return
        async with _candidate_lock(candidate_id) as conn:
            if conn is not None:
                await self._process(conn, candidate_id)

    async def _process(self, conn, candidate_id):
        target = await target_for(conn)
        if target is None:
            return
        row = await conn.fetchrow('SELECT * FROM memory_candidates WHERE id=$1', candidate_id)
        if row is None or row['purged_at'] is not None:
            return
        row = dict(row)
        now = self._clock()
        await conn.execute("UPDATE memory_posts SET state='uncertain',error='sender stopped before recording result', "
                           "updated_at=$2 WHERE candidate_id=$1 AND state='sending'", candidate_id, now)
        if row['state'] == 'pending' and row['expires_at'] <= now:
            async with conn.transaction():
                current = await conn.fetchrow('SELECT * FROM memory_candidates WHERE id=$1 FOR UPDATE', candidate_id)
                if current and current['state'] == 'pending' and current['expires_at'] <= now:
                    row = await db_memory.transition(conn, candidate_id, 'expired', action='expire', actor='system',
                                                     via='system', now=now)
        posts = await conn.fetch('SELECT * FROM memory_posts WHERE candidate_id=$1 ORDER BY id', candidate_id)
        pushes = [p for p in posts if p['kind'] == 'push']
        # A revive reopens the same revision; only attempts after its durable event suppress a new push.
        since = await conn.fetchval("SELECT created_at FROM memory_candidate_events WHERE candidate_id=$1 "
                                    "AND action='revive' ORDER BY id DESC LIMIT 1", candidate_id)
        latest = next((p for p in reversed(pushes) if p['state'] == 'sent' and p['external_id']
                       and (p['channel_id'], p['chat']) == target), None)
        if row['state'] == 'pending' and not any(p['revision'] == row['revision'] and (
                since is None or p['created_at'] >= since) for p in pushes):
            detail = await db_memory.get(candidate_id, db=conn)
            config, problem = await vault.load()
            lines = None
            if config is not None and not problem:
                try:
                    lines = await asyncio.to_thread(changed_lines, config.path, detail['revision']['changes'])
                except (ValueError, OSError, memory_write.WriteFailure):
                    pass
            raw = await memory_settings.stored(conn)
            channel = await channel_configs.get_config(target[0], pool=conn)
            text, complete = push_text(row, detail['revision'], lines, sources=detail['sources'],
                                       pending_days=int(raw.get('pending_days', 7)), dashboard_url=self._dashboard_url,
                                       markdown=bool(channel and channel['channel_type'] in LARK_CHANNEL_TYPES))
            post = await conn.fetchrow("INSERT INTO memory_posts(candidate_id,kind,revision,content_hash,complete, "
                "channel_id,chat,text,state,attempts) VALUES($1,'push',$2,$3,$4,$5,$6,$7,'sending',1) RETURNING *",
                candidate_id, row['revision'], row['content_hash'], complete, *target, text)
            await self._deliver(conn, post, reply=latest['external_id'] if latest else None)
            async with conn.transaction():
                await db_memory._event(conn, candidate_id, model.Action.PUSH, model.Actor.SYSTEM, model.Via.SYSTEM,
                    revision=row['revision'], content_hash=row['content_hash'], post_id=post['id'],
                    channel_id=target[0], chat=target[1])
            posts = await conn.fetch('SELECT * FROM memory_posts WHERE candidate_id=$1 ORDER BY id', candidate_id)
            pushes = [p for p in posts if p['kind'] == 'push']
        current_push = next((p for p in reversed(pushes) if p['revision'] == row['revision']
                            and (since is None or p['created_at'] >= since)
                            and p['state'] == 'sent' and p['external_id'] and (p['channel_id'], p['chat']) == target), None)
        if current_push:
            for old in pushes:
                if (old['revision'] < row['revision'] or (since is not None and old['created_at'] < since)) and (
                        old['state'] == 'sent' and old['external_id']) and (
                        old['channel_id'], old['chat']) == target:
                    text = f'🔄 {model.format_id(candidate_id)} 已更新，请看新消息'
                    if old['text'] != text:
                        await self._edit(conn, row, old, text, now)
        # The writer may immediately schedule a redraft after a conflict; its event still records the result.
        ended = row['state'] in FINAL_STATES or (row['state'] == 'drafting' and await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM memory_candidate_events WHERE candidate_id=$1 AND action='conflict')", candidate_id))
        if row['state'] == 'pending':
            # The old confirmation ended in conflict and was redrafted before this tick observed its result.
            await conn.execute("UPDATE memory_posts SET text=$2 WHERE candidate_id=$1 AND kind='final' "
                               "AND state='pending' AND text='' AND revision<>$3", candidate_id,
                               final_text(row, dashboard_url=self._dashboard_url), row['revision'])
        if ended:
            if row['state'] == 'written' and not row['note_path']:
                detail = await db_memory.get(candidate_id, db=conn)
                row['note_path'] = '、'.join(dict.fromkeys(c['path'] for c in detail['revision']['changes']))
            text = final_text(row, dashboard_url=self._dashboard_url)
            if current_push and (current_push['text'] != text or current_push['edited_at'] is None
                                 or current_push['edited_at'] < row['updated_at']):
                await self._edit(conn, row, current_push, text, max(now, row['updated_at']))
            await conn.execute("UPDATE memory_posts SET text=$2 WHERE candidate_id=$1 AND kind='final' "
                               "AND state='pending' AND text=''", candidate_id, text)
        for final in await conn.fetch("SELECT * FROM memory_posts WHERE candidate_id=$1 AND kind='final' "
                                     "AND state='pending' AND text<>'' AND (error IS DISTINCT FROM $2 OR created_at<=$3) ORDER BY id",
                                     candidate_id, _AWAITING_REPLY, now-timedelta(seconds=RESULT_WAIT_SECONDS)):
            if (final['channel_id'], final['chat']) != target:
                continue
            if await target_for(conn) != target:
                return
            claimed = await conn.fetchval(
                "UPDATE memory_posts SET state='sending',attempts=attempts+1 WHERE id=$1 AND state='pending' "
                "AND (error IS DISTINCT FROM $2 OR created_at<=$3) RETURNING id", final['id'],
                _AWAITING_REPLY, now-timedelta(seconds=RESULT_WAIT_SECONDS))
            if claimed is None:
                continue
            parent = next((p for p in reversed(pushes) if p['revision'] == final['revision']
                           and p['external_id'] and (p['channel_id'], p['chat']) == target), None)
            await self._deliver(conn, final, reply=parent['external_id'] if parent else None)

    async def _deliver(self, conn, post, *, reply):
        if await target_for(conn) != (post['channel_id'], post['chat']):
            # No network call occurred: unlike an interrupted call, this is a known failed delivery.
            await conn.execute("UPDATE memory_posts SET state='failed',error='push target changed' WHERE id=$1", post['id'])
            return
        external = error = None
        try:
            receipt = await self._registry.post(post['channel_id'], push_target(post['channel_id'], post['chat']),
                post['text'], reply_to=reply, idempotency_key=f'memory-{post["id"]}')
            state = {PostOutcome.SENT: 'sent', PostOutcome.REJECTED: 'failed', PostOutcome.UNKNOWN: 'uncertain'}[receipt.result]
            external = receipt.external_ids[0] if receipt.external_ids else None
            if state == 'sent' and external is None:
                state = 'uncertain'
            error = receipt.error
        except Exception as exc:
            state, error = 'uncertain', type(exc).__name__
        await conn.execute('UPDATE memory_posts SET state=$2,external_id=$3,error=$4,updated_at=$5 WHERE id=$1',
                           post['id'], state, external, error, self._clock())
        logger.info('memory candidate=%s post=%s kind=%s state=%s', post['candidate_id'], post['id'], post['kind'], state)

    async def _edit(self, conn, row, post, text, now):
        if await target_for(conn) != (post['channel_id'], post['chat']):
            return
        try:
            receipt = await self._registry.edit(post['channel_id'], push_target(post['channel_id'], post['chat']),
                                                post['external_id'], text)
            success = receipt.success
        except Exception:
            success = False
        async with conn.transaction():
            if not success:
                await _insert_final(conn, {**row, 'revision': post['revision'], 'content_hash': post['content_hash']},
                                    post['channel_id'], post['chat'], text)
            await conn.execute('UPDATE memory_posts SET text=$2,edited_at=$3,updated_at=$3 WHERE id=$1',
                               post['id'], text, now)

    async def _alerts(self):
        target = await target_for()
        if target is None:
            return
        pool, now = connection.get_pool(), self._clock()
        ends = await pool.fetch("SELECT action,note FROM memory_candidate_events WHERE action IN ('write','block','conflict','drop') "
                                "ORDER BY id DESC LIMIT 3")
        streak = len(ends) == 3 and all(e['action'] == 'block' for e in ends)
        repeated = await pool.fetchval("SELECT note FROM memory_candidate_events WHERE action='block' AND created_at >= $1 "
                                       "GROUP BY note HAVING count(*)>=5 ORDER BY count(*) DESC LIMIT 1", now-timedelta(hours=24))
        if not streak and repeated is None:
            return
        if await system_settings.claim('memory_alert_sent:blocked', now.date().isoformat()):
            text = ('告警：记忆写入已连续 3 次受阻，请检查记忆区。' if streak else
                    f'告警：记忆写入在 24 小时内因「{_REASONS.get(repeated, "写入受阻") }」受阻至少 5 次，请检查记忆区。')
            if await target_for() != target:
                return
            try:
                await self._registry.post(target[0], push_target(*target), text,
                                         idempotency_key=f'memory-blocked-alert-{now.date().isoformat()}')
            except Exception:
                logger.warning('memory write alert delivery uncertain')
