"""Rule notices and suggestion suppression on disposable PostgreSQL, with scripted channels."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from looper.agents import attention, chat_settings, intent_push, morning_brief, subscriptions
from looper.agents.channels.base import PostOutcome, PostReceipt
from looper.db import attention as attention_store, morning_briefs
from tests.db_support import DSN, run_db
from tests.leads_support import intent
from tests.subscription_support import DM, add, lark_targets, source

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
NOW = datetime(2026, 11, 23, 14, tzinfo=timezone.utc)
DASHBOARD = 'https://looper.example'


class Channels:
    def __init__(self):
        self.posts = []
        self.outcome = PostOutcome.SENT

    async def post(self, channel, target, text, **kwargs):
        self.posts.append((channel, target, text, kwargs))
        return PostReceipt(self.outcome is PostOutcome.SENT, outcome=self.outcome,
                           external_ids=(f"synthetic-{len(self.posts)}",) if self.outcome is PostOutcome.SENT else ())


async def setup(pool, monkeypatch):
    await source(pool)
    await lark_targets(pool)
    await pool.execute("UPDATE channel_configs SET enabled=true,config=config || jsonb_build_object('operator_sender_id',$1::text) "
                       "WHERE channel_id='channel-lark'", DM)
    await pool.execute("INSERT INTO system_settings(key,value) VALUES('intents.enabled','true'),"
                       "('intents.rules.enabled','true'),('delivery.enabled','true')")
    await pool.execute("INSERT INTO projects(id,name,work_dir) VALUES('rules-project','Review project','/tmp/synthetic')")
    await pool.execute("INSERT INTO schedules(id,project_id,name,cron,timezone,requirement,kind,notify_channel,notify_chat) "
                       "VALUES('rules-schedule','rules-project','Review requests','*/10 * * * *','UTC','Read',"
                       "'subscription','lark',$1)", DM)
    await pool.execute("INSERT INTO subscriptions(schedule_id,source_id,output_kind,target_confirmed_at,"
                       "target_confirmed_version,chat_kinds,sender_kinds,max_age_minutes,max_events,text_max_chars) "
                       "VALUES('rules-schedule','stream','intents',$1,'synthetic-version',ARRAY['p2p'],"
                       "ARRAY['user'],180,500,200)", NOW)
    async def version(schedule):
        return 'synthetic-version'
    monkeypatch.setattr(subscriptions, 'config_version', version)
    monkeypatch.setattr(chat_settings, '_delivery_enabled', True)
    monkeypatch.setattr(chat_settings, '_timezone', 'UTC')
    seq = await add(pool, 'Synthetic request', sent_at=NOW, kind='p2p', chat_id='synthetic-dm')
    iid = await intent(pool, [seq], schedule_id='rules-schedule', project_id='rules-project', created_at=NOW)
    channels = Channels()
    return SimpleNamespace(iid=iid, channels=channels, service=intent_push.IntentPushService(channels, clock=lambda: NOW))


async def work(pool, iid, *, decision=None):
    return await pool.fetchval("INSERT INTO intent_rule_work(intent_id,generation,source_fingerprint,"
                              "first_verification_status,state,decision) VALUES($1,1,'synthetic',"
                              "'unavailable',$2,$3) RETURNING id", iid, 'decided' if decision else 'pending', decision)


async def created(pool, iid):
    await pool.execute("INSERT INTO tasks(id,project_id,title,requirement,status) "
                       "VALUES('task-synthetic-review','rules-project','MR review 17','Review verified MR','init')")
    eid = await pool.fetchval("INSERT INTO intent_rule_executions(task_id,mode,mr_key,quota_units,quota_day,quota_operator,quota_project) "
                             "VALUES('task-synthetic-review','auto',$1::jsonb,1,'2026-11-23','operator','rules-project') RETURNING id", json.dumps({
                                 'system_id': 'review-system', 'repository_id': 'repo', 'mr_id': '17',
                                 'revision': 'head-a', 'canonical_url': 'https://review.example/repo/mr/17'}))
    await pool.execute("UPDATE intents SET state='tasked',task_id='task-synthetic-review',handled_at=$2,"
                       "handled_via='system' WHERE id=$1", iid, NOW)
    hit = await pool.fetchval("INSERT INTO intent_rule_hits(evaluation_key,intent_id,generation,selection,action,reason) "
                              "VALUES($1,$2,1,'matched','create_task','matched') RETURNING id", f'create:{iid}', iid)
    await pool.execute('INSERT INTO intent_rule_hit_executions(hit_id,execution_id) VALUES($1,$2)', hit, eid)
    await pool.execute('INSERT INTO intent_rule_hit_sources(hit_id,event_seq) '
                       'SELECT $1,event_seq FROM intent_sources WHERE intent_id=$2', hit, iid)
    # Notices outlive deleted message evidence; their stored, operator-only target remains revalidated.
    nid = await pool.fetchval("INSERT INTO intent_rule_notices(execution_id,kind,dedupe_key,channel_id,chat) "
                             "VALUES($1,'created',$2,'channel-lark',$3) RETURNING id", eid, f'rule-created:{eid}', DM)
    return eid, nid


@pytest.mark.parametrize('decision', [None, 'record_only', 'create_task', 'push_for_confirmation'])
def test_create_suppresses_confirmation_push_and_digest(monkeypatch, decision):
    async def check(pool):
        env = await setup(pool, monkeypatch)
        await work(pool, env.iid, decision=decision)
        await pool.execute("INSERT INTO proactive_deliveries(dedupe_key,source,source_ref,channel_id,chat,"
                           "chat_kind,decision,reason,digest_date,decided_at) VALUES('old-suggestion','intent',"
                           "$1,'channel-lark',$2,'direct','digest','quiet_hours',$3,$4)", str(env.iid), DM, NOW.date(), NOW)
        await env.service.run_once()
        allowed = decision == 'push_for_confirmation'
        assert len(env.channels.posts) == int(allowed)
        rows, count = await attention_store.pending_intents(10, rules_on=True)
        assert [r['id'] for r in rows] == ([env.iid] if allowed else [])
        assert count == int(allowed)
        digests = await morning_briefs.digests(pool, channel_id='channel-lark', chat=DM, until=NOW,
                                               dates=[NOW.date()])
        assert len(digests) == int(allowed)
        assert await pool.fetchval('SELECT state FROM intents WHERE id=$1', env.iid) == 'pending'
        await pool.execute("UPDATE system_settings SET value='false' WHERE key='intents.rules.enabled'")
        await env.service.run_once()
        assert len(env.channels.posts) == 1
        assert (await attention_store.pending_intents(10))[1] == 1
    run_db(check, monkeypatch)


def test_creation_notice_uses_normal_delivery_and_operator_dm(monkeypatch):
    from looper.agents.intent_rule_delivery import NoticeDispatcher
    async def check(pool):
        env = await setup(pool, monkeypatch)
        eid, nid = await created(pool, env.iid)
        service = NoticeDispatcher(env.channels, clock=lambda: NOW, dashboard_url=DASHBOARD)
        await service.run_once()
        await service.run_once()
        assert len(env.channels.posts) == 1
        channel, target, text, kwargs = env.channels.posts[0]
        assert channel == 'channel-lark' and target.id == DM and not target.group
        assert 'task-synthetic-review' in text and '撤销 task-synthetic-review' in text
        assert 'https://review.example/repo/mr/17' in text and '评论' in text
        assert f']({DASHBOARD}/#tasks/task-synthetic-review)' in text
        assert 'Synthetic request' not in text
        decision = await pool.fetchrow('SELECT source,source_ref,urgency,chat_kind,decision,outcome FROM proactive_deliveries')
        assert tuple(decision) == ('intent', f'rule-created:{eid}', 'normal', 'direct', 'send', 'sent')
        assert await pool.fetchval('SELECT state FROM intent_rule_notices WHERE id=$1', nid) == 'sent'
    run_db(check, monkeypatch)


@pytest.mark.parametrize('outcome', [PostOutcome.REJECTED, PostOutcome.UNKNOWN])
def test_start_and_notice_recovery_never_creates_again(monkeypatch, outcome):
    from looper.agents.intent_rule_delivery import NoticeDispatcher
    async def check(pool):
        env = await setup(pool, monkeypatch)
        _, nid = await created(pool, env.iid)
        env.channels.outcome = outcome
        await NoticeDispatcher(env.channels, clock=lambda: NOW).run_once()
        env.channels.outcome = PostOutcome.SENT
        await NoticeDispatcher(env.channels, clock=lambda: NOW+timedelta(minutes=10)).run_once()
        expected = 'sent' if outcome is PostOutcome.REJECTED else 'uncertain'
        assert await pool.fetchval('SELECT state FROM intent_rule_notices WHERE id=$1', nid) == expected
        assert len(env.channels.posts) == (2 if outcome is PostOutcome.REJECTED else 1)
        assert await pool.fetchval('SELECT count(*) FROM tasks') == 1
        rows, count = await attention_store.uncertain_deliveries(NOW-timedelta(days=1), 10, rules_on=True)
        assert count == int(outcome is PostOutcome.UNKNOWN)
        if rows:
            item = attention._delivery_item(rows[0])
            assert item['facts']['source'] == 'intent_rule'
    run_db(check, monkeypatch)


def test_rule_switch_holds_and_resumes_notices_without_backfill(monkeypatch):
    from looper.agents.intent_rule_delivery import NoticeDispatcher
    async def check(pool):
        env = await setup(pool, monkeypatch)
        _, nid = await created(pool, env.iid)
        await pool.execute("UPDATE system_settings SET value='false' WHERE key='intents.rules.enabled'")
        await NoticeDispatcher(env.channels, clock=lambda: NOW).run_once()
        assert env.channels.posts == []
        assert await pool.fetchval('SELECT state FROM intent_rule_notices WHERE id=$1', nid) == 'pending'
        await pool.execute("UPDATE system_settings SET value='true' WHERE key='intents.rules.enabled'")
        await NoticeDispatcher(env.channels, clock=lambda: NOW).run_once()
        assert len(env.channels.posts) == 1
        assert await pool.fetchval('SELECT count(*) FROM tasks') == 1
    run_db(check, monkeypatch)


def test_creation_digest_keeps_task_and_revoke_without_old_confirmation(monkeypatch):
    from looper.agents.intent_rule_delivery import NoticeDispatcher
    async def check(pool):
        env = await setup(pool, monkeypatch)
        eid, nid = await created(pool, env.iid)
        night = NOW.replace(hour=23)
        await NoticeDispatcher(env.channels, clock=lambda: night).run_once()
        assert env.channels.posts == []
        assert await pool.fetchval('SELECT state FROM intent_rule_notices WHERE id=$1', nid) == 'digest'
        digests = await morning_briefs.digests(pool, channel_id='channel-lark', chat=DM, until=night,
                                               dates=[night.date()+timedelta(days=1)])
        assert len(digests) == 1
        line = morning_brief._digest_line(digests[0], ZoneInfo('UTC'), dashboard_url=DASHBOARD)
        assert 'task-synthetic-review' in line and '撤销 task-synthetic-review' in line
        assert f']({DASHBOARD}/#tasks/task-synthetic-review)' in line
        await pool.execute("UPDATE system_settings SET value='false' WHERE key='intents.rules.enabled'")
        assert await morning_briefs.digests(pool, channel_id='channel-lark', chat=DM, until=night,
                                            dates=[night.date()+timedelta(days=1)]) == []
    run_db(check, monkeypatch)


@pytest.mark.parametrize('handled', [None, 'rejected', 'tasked'])
def test_discovery_fallback_posts_one_operator_confirmation_and_suppresses_it_after_build(monkeypatch, handled):
    from looper.agents.intent_rule_delivery import NoticeDispatcher
    async def check(pool):
        env = await setup(pool, monkeypatch)
        sources = [{'id': 'project-source', 'system_id': 'review-system', 'operator_key': 'operator',
                    'enabled': True, 'private_target': {'channel_id': 'channel-lark', 'chat': DM},
                    'migration_state': 'shared_entry'}]
        await pool.execute('INSERT INTO intent_rule_config(id,discovery_sources) VALUES(true,$1::jsonb)', json.dumps(sources))
        did = await pool.fetchval("INSERT INTO intent_rule_discoveries(discovery_source_id,source_ref,system_id,mr_url,state) "
                                 "VALUES('project-source','origin-17','review-system','https://review.example/repo/mr/17',"
                                 "'confirmation') RETURNING id")
        nid = await pool.fetchval("INSERT INTO intent_rule_notices(discovery_id,kind,dedupe_key,reason) "
                                 "VALUES($1,'discovery_confirmation',$2,'not_reviewer') RETURNING id", did,
                                 f'rule-discovery:{did}')
        if handled:
            await pool.execute('UPDATE intent_rule_discoveries SET state=$2 WHERE id=$1', did, handled)
        await NoticeDispatcher(env.channels, clock=lambda: NOW, dashboard_url=DASHBOARD).run_once()
        await NoticeDispatcher(env.channels, clock=lambda: NOW, dashboard_url=DASHBOARD).run_once()
        assert len(env.channels.posts) == int(handled is None)
        assert await pool.fetchval('SELECT count(*) FROM intents') == 1
        if handled is None:
            text = env.channels.posts[0][2]
            assert f'发现 {did}' in text and f']({DASHBOARD}/#intents)' in text
            assert 'I-' not in text and '建 I-' not in text
            assert await pool.fetchval('SELECT state FROM intent_rule_notices WHERE id=$1', nid) == 'sent'
    run_db(check, monkeypatch)


@pytest.mark.parametrize('blocked', ['rules', 'delivery', 'target'])
def test_notice_dependency_and_invalid_target_cannot_send(monkeypatch, blocked):
    from looper.agents.intent_rule_delivery import NoticeDispatcher
    async def check(pool):
        env = await setup(pool, monkeypatch)
        _, nid = await created(pool, env.iid)
        if blocked == 'target':
            await pool.execute("UPDATE channel_configs SET config=config || '{\"operator_sender_id\":\"ou_other_synthetic\"}'::jsonb "
                               "WHERE channel_id='channel-lark'")
        else:
            await pool.execute('UPDATE system_settings SET value=\'false\' WHERE key=$1',
                               'intents.rules.enabled' if blocked == 'rules' else 'delivery.enabled')
        await NoticeDispatcher(env.channels, clock=lambda: NOW).run_once()
        assert env.channels.posts == []
        assert await pool.fetchval('SELECT attempts FROM intent_rule_notices WHERE id=$1', nid) == 0
        if blocked == 'target':
            rows, count = await attention_store.uncertain_deliveries(NOW-timedelta(days=1), 10, rules_on=True)
            assert count == 1 and rows[0]['reason'] == 'notification_failed'
            assert attention._delivery_item(rows[0])['facts']['source'] == 'intent_rule'
    run_db(check, monkeypatch)


def test_orphan_notice_send_is_uncertain_and_never_retried(monkeypatch):
    from looper.agents.intent_rule_delivery import NoticeDispatcher
    async def check(pool):
        env = await setup(pool, monkeypatch)
        _, nid = await created(pool, env.iid)
        await pool.execute("UPDATE intent_rule_notices SET state='sending',attempts=1 WHERE id=$1", nid)
        await NoticeDispatcher(env.channels, clock=lambda: NOW).run_once()
        await NoticeDispatcher(env.channels, clock=lambda: NOW).run_once()
        assert env.channels.posts == []
        assert await pool.fetchval('SELECT state FROM intent_rule_notices WHERE id=$1', nid) == 'uncertain'
    run_db(check, monkeypatch)


@pytest.mark.parametrize('handled', [False, True])
def test_brief_rechecks_a_confirmation_after_its_summary_was_built(monkeypatch, handled):
    # Reach summary collection inside the brief window so the confirmation race is exercised.
    monkeypatch.setattr(f'{__name__}.NOW', NOW.replace(hour=11))
    async def check(pool):
        env = await setup(pool, monkeypatch)
        wid = await work(pool, env.iid, decision='push_for_confirmation')
        await pool.execute("INSERT INTO proactive_deliveries(dedupe_key,source,source_ref,channel_id,chat,"
                           "chat_kind,decision,reason,digest_date,decided_at) VALUES('old-suggestion','intent',"
                           "$1,'channel-lark',$2,'direct','digest','quiet_hours',$3,$4)", str(env.iid), DM, NOW.date(), NOW)
        monkeypatch.setattr(chat_settings, '_morning_brief_enabled', True)
        calls = 0
        async def collect(*, managers=None):
            nonlocal calls
            calls += 1
            rows, count = await attention_store.pending_intents(10, rules_on=True)
            summary = {'read_at': NOW, 'total': count, 'sections': [
                {'kind': 'intents', 'state': 'ok', 'count': count,
                 'items': [attention._intent_item(row) for row in rows], 'error': None}]}
            if calls == 1 and handled:
                await pool.execute("UPDATE intent_rule_work SET decision='record_only' WHERE id=$1", wid)
            return summary
        monkeypatch.setattr(attention, 'collect', collect)
        await morning_brief.MorningBriefService(env.channels, clock=lambda: NOW).run_once()
        assert calls == 2
        assert len(env.channels.posts) == int(not handled)
        assert await pool.fetchval('SELECT status FROM morning_briefs') == ('skipped' if handled else 'sent')
        assert await pool.fetchval('SELECT count(*) FROM morning_brief_consumed') == int(not handled)
    run_db(check, monkeypatch)


def test_disabled_delivery_never_reads_execution_tables(monkeypatch):
    from looper.agents.intent_rule_delivery import NoticeDispatcher
    async def check(pool):
        env = await setup(pool, monkeypatch)
        await work(pool, env.iid)
        await pool.execute("UPDATE system_settings SET value='false' WHERE key='intents.rules.enabled'")
        await pool.execute('ALTER TABLE intent_rule_work RENAME TO hidden_rule_work; '
                           'ALTER TABLE intent_rule_notices RENAME TO hidden_rule_notices')
        await NoticeDispatcher(env.channels, clock=lambda: NOW).run_once()
        await env.service.run_once()
        assert len(env.channels.posts) == 1
        assert (await attention._intents(None))[1] == 1
        assert (await attention._deliveries(None))[1] == 0
        assert await morning_briefs.digests(pool, channel_id='channel-lark', chat=DM, until=NOW,
                                            dates=[NOW.date()]) == []
    run_db(check, monkeypatch)


@pytest.mark.parametrize('channel_id', ['channel-lark', 'channel-lark-2'])
def test_created_notice_survives_deleted_message_evidence(monkeypatch, channel_id):
    from looper.agents.intent_rule_delivery import NoticeDispatcher
    async def check(pool):
        env = await setup(pool, monkeypatch)
        _, nid = await created(pool, env.iid)
        if channel_id != 'channel-lark':
            await pool.execute('INSERT INTO channel_configs(channel_id,channel_type,enabled,config) '
                               "VALUES($1,'lark',true,$2::jsonb)", channel_id,
                               json.dumps({'operator_sender_id': DM, 'allowed_open_ids': [DM]}))
            await pool.execute('UPDATE intent_rule_notices SET channel_id=$2 WHERE id=$1', nid, channel_id)
        await pool.execute('DELETE FROM source_events')
        await pool.execute('DELETE FROM intents WHERE id=$1', env.iid)
        assert await pool.fetchval('SELECT count(*) FROM intent_rule_hits') == 0
        await NoticeDispatcher(env.channels, clock=lambda: NOW).run_once()
        assert len(env.channels.posts) == 1
        assert 'task-synthetic-review' in env.channels.posts[0][2]
        assert await pool.fetchval('SELECT state FROM intent_rule_notices WHERE id=$1', nid) == 'sent'
    run_db(check, monkeypatch)


def test_three_unavailable_checks_emit_one_normal_operator_alert(monkeypatch):
    from looper.agents.intent_rule_delivery import NoticeDispatcher
    from looper.db import intent_rule_execution as ledger
    async def check(pool):
        env = await setup(pool, monkeypatch)
        for _ in range(4):
            async with pool.acquire() as conn, conn.transaction():
                await ledger.record_verification(conn, 'review-system', available=False, now=NOW)
        await NoticeDispatcher(env.channels, clock=lambda: NOW).run_once()
        await NoticeDispatcher(env.channels, clock=lambda: NOW).run_once()
        assert len(env.channels.posts) == 1
        assert env.channels.posts[0][1].id == DM and not env.channels.posts[0][1].group
        assert '核验连续不可用' in env.channels.posts[0][2]
        assert await pool.fetchval("SELECT urgency FROM proactive_deliveries WHERE source='intent'") == 'normal'
        async with pool.acquire() as conn, conn.transaction():
            await ledger.record_verification(conn, 'review-system', available=True, now=NOW)
        for _ in range(3):
            async with pool.acquire() as conn, conn.transaction():
                await ledger.record_verification(conn, 'review-system', available=False, now=NOW)
        await NoticeDispatcher(env.channels, clock=lambda: NOW).run_once()
        assert len(env.channels.posts) == 2
        assert await pool.fetchval("SELECT count(*) FROM intent_rule_notices WHERE kind='verification_fault'") == 2
    run_db(check, monkeypatch)


def test_manual_creation_notice_never_claims_rule_authorization(monkeypatch):
    from looper.agents.intent_rule_delivery import NoticeDispatcher
    async def check(pool):
        env = await setup(pool, monkeypatch)
        eid, _ = await created(pool, env.iid)
        await pool.execute("UPDATE intent_rule_executions SET mode='manual',quota_units=0,quota_day=NULL,"
                           "quota_operator=NULL,quota_project=NULL WHERE id=$1", eid)
        await NoticeDispatcher(env.channels, clock=lambda: NOW).run_once()
        assert len(env.channels.posts) == 1
        text = env.channels.posts[0][2]
        assert '人工确认' in text
        assert '全部核验条件通过' not in text
        assert '按规则创建' not in text
        assert '撤销 task-synthetic-review' in text
    run_db(check, monkeypatch)


@pytest.mark.parametrize('kind', ['created', 'discovery_confirmation'])
@pytest.mark.parametrize('change', ['disabled', 'removed', 'invalid_target'])
def test_committed_creation_notice_survives_discovery_source_config_changes(monkeypatch, kind, change):
    from looper.agents.intent_rule_delivery import NoticeDispatcher
    async def check(pool):
        env = await setup(pool, monkeypatch)
        eid, nid = await created(pool, env.iid)
        did = await pool.fetchval("INSERT INTO intent_rule_discoveries(discovery_source_id,source_ref,system_id,mr_url,state) "
                                 "VALUES('project-source','origin-17','review-system','https://review.example/repo/mr/17',$1) "
                                 "RETURNING id", 'tasked' if kind == 'created' else 'confirmation')
        if kind == 'created':
            await pool.execute('DELETE FROM intent_rule_hits WHERE id IN '
                               '(SELECT hit_id FROM intent_rule_hit_executions WHERE execution_id=$1)', eid)
            hit = await pool.fetchval("INSERT INTO intent_rule_hits(evaluation_key,discovery_id,generation,selection,action,reason) "
                                     "VALUES($1,$2,1,'matched','create_task','matched') RETURNING id", f'discovery:{did}', did)
            await pool.execute('INSERT INTO intent_rule_hit_executions(hit_id,execution_id) VALUES($1,$2)', hit, eid)
        else:
            await pool.execute('DELETE FROM intent_rule_notices WHERE id=$1', nid)
            nid = await pool.fetchval("INSERT INTO intent_rule_notices(discovery_id,kind,dedupe_key,channel_id,chat) "
                                     "VALUES($1,'discovery_confirmation',$2,'channel-lark',$3) RETURNING id", did,
                                     f'rule-discovery:{did}', DM)
        sources = [{'id': 'project-source', 'system_id': 'review-system', 'operator_key': 'operator',
                    'enabled': change != 'disabled', 'private_target': {
                        'channel_id': 'channel-lark', 'chat': 'ou_other_synthetic' if change == 'invalid_target' else DM},
                    'migration_state': 'shared_entry'}] if change != 'removed' else []
        await pool.execute('INSERT INTO intent_rule_config(id,discovery_sources) VALUES(true,$1::jsonb)', json.dumps(sources))
        service = NoticeDispatcher(env.channels, clock=lambda: NOW)
        await service.run_once()
        await service.run_once()
        assert len(env.channels.posts) == int(kind == 'created')
        if kind == 'created':
            channel, target, text, _ = env.channels.posts[0]
            assert channel == 'channel-lark' and target.id == DM and not target.group
            assert 'task-synthetic-review' in text
            assert await pool.fetchval('SELECT state FROM intent_rule_notices WHERE id=$1', nid) == 'sent'
        else:
            assert await pool.fetchval('SELECT state FROM intent_rule_notices WHERE id=$1', nid) != 'sent'
    run_db(check, monkeypatch)


def test_morning_brief_posts_an_absolute_link_for_a_rule_creation_digest(monkeypatch):
    from looper.agents.intent_rule_delivery import NoticeDispatcher
    async def check(pool):
        env = await setup(pool, monkeypatch)
        await created(pool, env.iid)
        night = NOW.replace(hour=23)
        await NoticeDispatcher(env.channels, clock=lambda: night, dashboard_url=DASHBOARD).run_once()
        assert env.channels.posts == []
        monkeypatch.setattr(chat_settings, '_morning_brief_enabled', True)
        async def collect(*, managers=None):
            return {'read_at': NOW, 'total': 0, 'sections': []}
        monkeypatch.setattr(attention, 'collect', collect)
        next_morning = (NOW + timedelta(days=1)).replace(hour=9, minute=15)
        await morning_brief.MorningBriefService(env.channels, clock=lambda: next_morning,
                                                dashboard_url=DASHBOARD).run_once()
        assert len(env.channels.posts) == 1
        text = env.channels.posts[0][2]
        assert f']({DASHBOARD}/#tasks/task-synthetic-review)' in text
        assert '撤销 task-synthetic-review' in text
        assert await pool.fetchval('SELECT status FROM morning_briefs') == 'sent'
        assert await pool.fetchval('SELECT count(*) FROM morning_brief_consumed') == 1
    run_db(check, monkeypatch)


@pytest.mark.parametrize('deferred', [False, True])
def test_one_auto_created_event_has_at_most_one_notice_needs_you_and_brief_line(monkeypatch, tmp_path, deferred):
    from looper.agents import extraction, intent_rule_engine as engine, intent_rule_evaluation as evaluation
    from looper.agents.intent_rule_delivery import NoticeDispatcher
    from tests.intent_rule_execution_support import URL, configure, fact

    async def check(pool):
        env = await setup(pool, monkeypatch)
        # Keep the channel/schedule fixture, but let the engine own task creation.
        await pool.execute('DELETE FROM intents')
        await pool.execute('DELETE FROM system_settings')
        await configure(pool, tmp_path)
        await pool.execute("UPDATE schedules SET enabled=true WHERE id='rules-schedule'")
        async def verifier(*_args, **_kwargs):
            return fact()
        monkeypatch.setattr(evaluation, 'verify', verifier)
        seq = await add(pool, 'Review '+URL, kind='p2p', sent_at=NOW, chat_id='synthetic-review-dm')
        parsed = extraction.Parsed('items', items=[{'type': 'mr_review', 'title': 'Synthetic MR review',
            'summary': 'Synthetic request', 'confidence': 'high', 'sources': [seq], 'window_sources': [seq], 'same_as': None}])
        prepared = await engine.prepare_for_extraction(parsed, source_id='stream', db=pool)
        async with pool.acquire() as conn, conn.transaction():
            await extraction.store_intents(conn, parsed, source_id='stream', schedule_id='rules-schedule',
                run_id=None, record_id=None, now=NOW, more=False, rules_on=True, prepared_rules=prepared)
        service = engine.IntentRuleService(db=pool, verifier=verifier)
        await service.run_once()
        await service.run_once()
        task_id = await pool.fetchval('SELECT id FROM tasks')
        assert task_id is not None, [dict(row) for row in await pool.fetch('SELECT state,decision,reason FROM intent_rule_work')]
        assert await pool.fetchval('SELECT count(*) FROM tasks') == 1
        assert await pool.fetchval("SELECT count(*) FROM intent_rule_notices WHERE kind='created'") == 1
        assert await pool.fetchval("SELECT count(*) FROM intents WHERE state='tasked'") == 1

        when = NOW.replace(hour=23) if deferred else NOW
        notice = NoticeDispatcher(env.channels, clock=lambda: when, dashboard_url=DASHBOARD)
        for _ in range(2):
            await notice.run_once()
            await env.service.run_once()
        creation_notices = len(env.channels.posts)
        rows, count = await attention_store.pending_intents(10, rules_on=True)
        assert rows == [] and count == 0
        summary = await attention.collect()
        needs_you = sum(item['ref'] == task_id or item['title'] == 'Synthetic MR review'
                        for section in summary['sections'] for item in section['items'])
        assert all(section['state'] != 'error' for section in summary['sections'])
        monkeypatch.setattr(chat_settings, '_morning_brief_enabled', True)
        next_morning = (NOW + timedelta(days=1)).replace(hour=9, minute=15)
        brief = morning_brief.MorningBriefService(env.channels, clock=lambda: next_morning, dashboard_url=DASHBOARD)
        await brief.run_once()
        await brief.run_once()
        brief_posts = env.channels.posts[creation_notices:]
        brief_lines = sum(task_id in line for post in brief_posts for line in post[2].splitlines())
        counts = (creation_notices, needs_you, brief_lines)
        assert counts == ((0, 0, 1) if deferred else (1, 0, 0))
        assert all(value <= 1 for value in counts)
        assert await pool.fetchval("SELECT count(*) FROM intent_posts WHERE kind IN ('push','progress')") == 0
        assert await pool.fetchval('SELECT count(*) FROM morning_brief_consumed') == int(deferred)
    run_db(check, monkeypatch)
