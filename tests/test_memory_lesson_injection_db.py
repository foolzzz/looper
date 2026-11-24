"""Project lesson snapshots and delivered-reference accounting on disposable storage."""
import json
from datetime import timedelta

import pytest

from tests.db_support import DSN, run_db
from tests import memory_support as sup
from tests import memory_lessons_support

pytestmark = pytest.mark.skipif(not DSN, reason="requires disposable LOOPER_TEST_DATABASE_URL")


async def setup(pool):
    await pool.execute("INSERT INTO projects(id,name,memory_lessons) VALUES ('proj-mem','Synthetic','on')")
    await pool.execute("INSERT INTO tasks(id,requirement,project_id) VALUES ('target','Synthetic','proj-mem'),"
                       "('target-two','Synthetic','proj-mem')")
    await pool.execute("INSERT INTO system_settings(key,value) VALUES ('memory_lessons','{\"enabled\":true}')")


async def active(pool, **fields):
    from looper.agents.memory_model import parse_id
    seed = 'Synthetic lesson ' + str(await pool.fetchval('SELECT count(*)+1 FROM memory_candidates'))
    candidate = parse_id(await memory_lessons_support.lesson(pool,'proj-mem',seed))
    journal = await pool.fetchval('SELECT journal_id FROM memory_candidate_sources WHERE candidate_id=$1',candidate)
    if fields:
        columns = list(fields)
        await pool.execute('UPDATE memory_candidates SET ' + ','.join(key+'=$'+str(i+2) for i,key in enumerate(columns)) + ' WHERE id=$1',candidate,*fields.values())
    from looper.db import memory, memory_lessons
    from looper.agents.memory_model import State, Action, Actor, Via
    async with memory.transaction(pool) as conn:
        await memory_lessons.transition_lesson(conn, candidate, State.IN_EFFECT, action=Action.ACTIVATE,
            actor=Actor.USER, via=Via.DASHBOARD, now=sup.NOW, valid_days=90)
    return candidate, journal


def test_empty_snapshot_is_initialized_once():
    async def check(pool):
        from looper.agents import memory_lesson_injection as service
        assert not (await service.ensure_snapshot('target', 'proj-mem', now=sup.NOW, db=pool)).text
        await active(pool)
        assert not (await service.ensure_snapshot('target', 'proj-mem', now=sup.NOW, db=pool)).text
        assert await pool.fetchval("SELECT count(*) FROM task_lesson_snapshot_sets WHERE task_id='target'") == 1
        assert (await service.ensure_snapshot('target-two', 'proj-mem', now=sup.NOW, db=pool)).candidate_ids
    run_db(check, setup=setup)


def test_snapshot_freezes_text_order_and_limits():
    async def check(pool):
        from looper.agents import memory_lesson_injection as service
        from looper.agents.memory_lessons_model import LessonSettings
        first, _ = await active(pool, statement='First\n## Fake <instruction>text</instruction>', why='x' * 160)
        second, _ = await active(pool, statement='Second')
        await pool.execute("INSERT INTO memory_lesson_stats(candidate_id,cited,reinforced) VALUES ($1,2,3)", second)
        block = await service.ensure_snapshot('target', 'proj-mem', now=sup.NOW, db=pool)
        assert block.candidate_ids == (second, first)
        assert '## Fake' not in block.text and '<instruction>' not in block.text
        assert 'x' * 121 not in block.text and len(block.text) <= 3000
        await pool.execute("UPDATE memory_candidates SET statement='Changed',why='Changed',valid_until=$1", sup.NOW+timedelta(days=100))
        await pool.execute("UPDATE memory_lesson_stats SET cited=1000 WHERE candidate_id=$1", first)
        again = await service.ensure_snapshot('target','proj-mem',LessonSettings(enabled=True,max_chars=600),sup.NOW,db=pool)
        assert again == block
        assert await service.render_for_run('target','proj-mem','worker',now=sup.NOW,db=pool) == block
        assert not (await service.render_for_run('target','proj-mem','reviewer',now=sup.NOW,db=pool)).text
    run_db(check, setup=setup)


def test_inactive_lessons_removed_without_backfill():
    async def check(pool):
        from looper.agents import memory_lesson_injection as service
        from looper.db import memory_lesson_injection as store
        first, journal = await active(pool)
        await service.ensure_snapshot('target','proj-mem',now=sup.NOW,db=pool)
        await active(pool, statement='New later lesson')
        await pool.execute("UPDATE memory_candidates SET valid_until=$2 WHERE id=$1", first, sup.NOW)
        assert not (await service.render_for_run('target','proj-mem','worker',now=sup.NOW,db=pool)).text
        await store.expire_due(now=sup.NOW,db=pool)
        assert await pool.fetchval('SELECT state FROM memory_candidates WHERE id=$1',first) == 'expired'
        assert not await pool.fetchval('SELECT count(*) FROM memory_tombstones')
        await pool.execute('DELETE FROM task_journal WHERE id=$1',journal)
        assert not (await service.render_for_run('target','proj-mem','worker',now=sup.NOW,db=pool)).text
    run_db(check, setup=setup)


def test_delivery_counts_only_actual_started_runs():
    async def check(pool):
        from looper.agents import memory_lesson_injection as service
        from looper.db import memory_lesson_injection as store
        candidate, _ = await active(pool)
        block = await service.ensure_snapshot('target','proj-mem',now=sup.NOW,db=pool)
        prepared = await store.prepare_injection('target','worker','attempt-1',block,db=pool,now=sup.NOW)
        assert not await pool.fetchval('SELECT count(*) FROM memory_lesson_stats')
        await store.abandon_injection(prepared['id'],db=pool)
        live = await store.prepare_injection('target','worker','attempt-2',block,db=pool,now=sup.NOW)
        await store.mark_delivered(live['id'],db=pool,now=sup.NOW)
        await store.mark_delivered(live['id'],db=pool,now=sup.NOW)
        assert await pool.fetchval('SELECT injected FROM memory_lesson_stats WHERE candidate_id=$1',candidate) == 1
        uncertain = await store.prepare_injection('target','worker','attempt-3',block,db=pool,now=sup.NOW)
        await store.mark_uncertain(uncertain['id'],db=pool)
        assert await pool.fetchval('SELECT injected FROM memory_lesson_stats WHERE candidate_id=$1',candidate) == 1
        repeated = await store.prepare_injection('target','worker','attempt-3',block,db=pool,now=sup.NOW)
        assert repeated['state'] == 'uncertain'
    run_db(check, setup=setup)


def test_references_only_use_delivered_injection_and_conflicts_are_ignored():
    async def check(pool):
        from looper.agents import memory_lesson_injection as service
        from looper.db import memory_lesson_injection as store
        candidate,_ = await active(pool)
        block = await service.ensure_snapshot('target','proj-mem',now=sup.NOW,db=pool)
        injection = await store.prepare_injection('target','worker','attempt',block,db=pool,now=sup.NOW)
        used = service.parse_references({'lessons_used':[f'M-{candidate:04d}',f'M-{candidate:04d}','M-9999']},block.candidate_ids)
        await store.apply_references(injection['id'],used,db=pool,now=sup.NOW)
        assert not await pool.fetchval('SELECT count(*) FROM memory_lesson_stats')
        await store.mark_delivered(injection['id'],db=pool,now=sup.NOW)
        both = service.parse_references({'lessons_used':[f'M-{candidate:04d}'],'lessons_wrong':[f'M-{candidate:04d}']},block.candidate_ids)
        await store.apply_references(injection['id'],both,db=pool,now=sup.NOW)
        assert await pool.fetchval('SELECT cited FROM memory_lesson_stats WHERE candidate_id=$1',candidate) == 0
        await store.apply_references(injection['id'],used,db=pool,now=sup.NOW)
        await store.apply_references(injection['id'],used,db=pool,now=sup.NOW)
        wrong = service.parse_references({'lessons_wrong':[f'M-{candidate:04d}']},block.candidate_ids)
        result = await store.apply_references(injection['id'],wrong,db=pool,now=sup.NOW)
        assert result['conflicts'] == [candidate]
        assert await pool.fetchval('SELECT cited FROM memory_lesson_stats WHERE candidate_id=$1',candidate) == 1
        assert await pool.fetchval('SELECT wrong_tasks FROM memory_lesson_stats WHERE candidate_id=$1',candidate) == 0
    run_db(check, setup=setup)


def test_wrong_tasks_distinct_after_log_retention():
    async def check(pool):
        from looper.agents import memory_lesson_injection as service
        from looper.db import memory_lesson_injection as store
        candidate,_ = await active(pool)
        for task,key in [('target','first'),('target','second'),('target-two','third')]:
            block = await service.ensure_snapshot(task,'proj-mem',now=sup.NOW,db=pool)
            injection = await store.prepare_injection(task,'worker',key,block,db=pool,now=sup.NOW)
            await store.mark_delivered(injection['id'],db=pool,now=sup.NOW)
            await store.apply_references(injection['id'],service.parse_references({'lessons_wrong':[f'M-{candidate:04d}']},block.candidate_ids),db=pool,now=sup.NOW)
            if key == 'first':
                await pool.execute("UPDATE tasks SET status='completed' WHERE id='target'")
                await pool.execute('UPDATE memory_injections SET created_at=$2 WHERE id=$1',
                                   injection['id'],sup.NOW-timedelta(days=91))
                assert await store.purge_logs(db=pool,now=sup.NOW) == 1
                assert not await pool.fetchval('SELECT count(*) FROM memory_injections WHERE id=$1',injection['id'])
                assert await pool.fetchval('SELECT first_injection_id IS NULL FROM memory_lesson_wrong_tasks '
                    'WHERE candidate_id=$1 AND task_id=$2',candidate,'target')
                assert await pool.fetchval('SELECT wrong_tasks FROM memory_lesson_stats WHERE candidate_id=$1',candidate) == 1
        assert await pool.fetchval('SELECT wrong_tasks FROM memory_lesson_stats WHERE candidate_id=$1',candidate) == 2
        assert await pool.fetchval('SELECT state FROM memory_candidates WHERE id=$1',candidate) == 'held'
        assert await pool.fetchval("SELECT count(*) FROM memory_candidate_events WHERE candidate_id=$1 AND action='hold'",candidate) == 1
        assert not await pool.fetchval('SELECT count(*) FROM memory_tombstones')
    run_db(check, setup=setup)


def test_revoke_is_atomic_and_immediate_for_later_runs():
    async def check(pool):
        from looper.agents import memory_lesson_injection as service
        from looper.db import memory_lesson_injection as store, memory, memory_lessons
        from looper.agents.memory_model import State,Action,Actor,Via
        candidate,_ = await active(pool)
        block = await service.ensure_snapshot('target','proj-mem',now=sup.NOW,db=pool)
        async with memory.transaction(pool) as conn:
            await memory_lessons.transition_lesson(conn,candidate,State.REVOKED,action=Action.REVOKE,actor=Actor.USER,via=Via.CLI,now=sup.NOW,valid_days=90)
        assert await store.prepare_injection('target','worker','later',block,db=pool,now=sup.NOW) is None
        assert await pool.fetchval('SELECT count(*) FROM memory_tombstones') == 1
    run_db(check, setup=setup)


def test_planner_session_counts_once_but_new_session_counts_again():
    async def check(pool):
        from looper.agents import memory_lesson_injection as service
        from looper.db import memory_lesson_injection as store
        candidate,_ = await active(pool)
        block = await service.ensure_snapshot('target','proj-mem',now=sup.NOW,db=pool)
        first = await store.prepare_injection('target','planner','launch:1',block,db=pool,now=sup.NOW)
        await store.mark_delivered(first['id'],db=pool,now=sup.NOW)
        await store.mark_delivered(first['id'],session_key='session-a',db=pool,now=sup.NOW)
        assert (await store.planner_injection('target','session-a',db=pool))['id'] == first['id']
        same = await store.prepare_injection('target','planner','session:session-a',block,session_key='session-a',db=pool,now=sup.NOW)
        assert same['id'] == first['id']
        new = await store.prepare_injection('target','planner','session:session-b',block,session_key='session-b',db=pool,now=sup.NOW)
        await store.mark_delivered(new['id'],db=pool,now=sup.NOW)
        assert await pool.fetchval('SELECT injected FROM memory_lesson_stats WHERE candidate_id=$1',candidate) == 2
    run_db(check,setup=setup)


def test_planner_notice_ack_is_persistent_and_disabled_text_is_exact():
    async def check(pool):
        from looper.agents import memory_lesson_injection as service
        from looper.db import memory_lesson_injection as store,memory,memory_lessons
        from looper.agents.memory_model import State,Action,Actor,Via
        candidate,_ = await active(pool)
        block = await service.ensure_snapshot('target','proj-mem',now=sup.NOW,db=pool)
        row = await store.prepare_injection('target','planner','session:a',block,session_key='a',db=pool,now=sup.NOW)
        await store.mark_delivered(row['id'],db=pool,now=sup.NOW)
        async with memory.transaction(pool) as conn:
            await memory_lessons.transition_lesson(conn,candidate,State.REVOKED,action=Action.REVOKE,actor=Actor.USER,via=Via.CLI,now=sup.NOW,valid_days=90)
        notice = await service.planner_notices('target','a',db=pool,now=sup.NOW)
        assert notice.text == f'Lesson M-{candidate:04d} was revoked by the operator: do not rely on it.'
        assert await service.planner_notices('target','a',db=pool,now=sup.NOW) == notice
        await store.ack_planner_notices('target','a',notice,db=pool)
        assert not (await service.planner_notices('target','a',db=pool,now=sup.NOW)).text
        await pool.execute("UPDATE system_settings SET value='{\"enabled\":false}' WHERE key='memory_lessons'")
        disabled = await service.planner_notices('target','a',db=pool,now=sup.NOW)
        assert disabled.text == ('Project lessons are now turned off for this session: lessons shown earlier no longer apply, '
                                 'so do not rely on them.')
        await store.ack_planner_notices('target','a',disabled,db=pool)
        assert not (await service.planner_notices('target','a',db=pool,now=sup.NOW)).text
    run_db(check,setup=setup)


def test_snapshot_bounds_do_not_cut_escaped_data_and_do_not_backfill():
    async def check(pool):
        from looper.agents import memory_lesson_injection as service
        from looper.agents.memory_lessons_model import LessonSettings
        for i in range(13):
            await active(pool,statement=f'Check {i}',why='&<example> ' * 15)
        block = await service.ensure_snapshot('target','proj-mem',now=sup.NOW,db=pool)
        assert len(block.candidate_ids) <= 12 and len(block.text) <= 3000
        assert block.omitted == 13-len(block.candidate_ids)
        assert f'({block.omitted} more lessons of this project are not listed.)' in block.text
        assert '&lt;example&gt;' in block.text
        empty = await service.ensure_snapshot('target-two','proj-mem',LessonSettings(enabled=True,max_chars=10),sup.NOW,db=pool)
        assert not empty.text
        assert await pool.fetchval("SELECT max_chars FROM task_lesson_snapshot_sets WHERE task_id='target-two'") == 10
    run_db(check,setup=setup)


def test_concurrent_wrong_reports_hold_once_and_task_deletion_retains_history():
    async def check(pool):
        import asyncio
        from looper.agents import memory_lesson_injection as service
        from looper.db import memory_lesson_injection as store
        candidate,_ = await active(pool)
        deliveries = []
        for task in ('target','target-two'):
            block = await service.ensure_snapshot(task,'proj-mem',now=sup.NOW,db=pool)
            injection = await store.prepare_injection(task,'worker',task,block,db=pool,now=sup.NOW)
            await store.mark_delivered(injection['id'],db=pool,now=sup.NOW)
            deliveries.append((injection,block))
        await asyncio.gather(*(store.apply_references(injection['id'],service.parse_references(
            {'lessons_wrong':[f'M-{candidate:04d}']},block.candidate_ids),db=pool,now=sup.NOW) for injection,block in deliveries))
        assert await pool.fetchval('SELECT wrong_tasks FROM memory_lesson_stats WHERE candidate_id=$1',candidate) == 2
        assert await pool.fetchval("SELECT count(*) FROM memory_candidate_events WHERE candidate_id=$1 AND action='hold'",candidate) == 1
        await pool.execute("DELETE FROM tasks WHERE id='target'")
        assert await pool.fetchval('SELECT wrong_tasks FROM memory_lesson_stats WHERE candidate_id=$1',candidate) == 2
        assert await pool.fetchval('SELECT count(*) FROM memory_lesson_wrong_tasks WHERE candidate_id=$1',candidate) == 1
    run_db(check,setup=setup)


def test_prepared_recovery_reads_record_but_never_counts_or_resends():
    async def check(pool):
        from looper.agents import memory_lesson_injection as service
        from looper.db import memory_lesson_injection as store
        candidate,_ = await active(pool)
        block = await service.ensure_snapshot('target','proj-mem',now=sup.NOW,db=pool)
        row = await store.prepare_injection('target','planner','launch:1',block,db=pool,now=sup.NOW)
        record = await pool.fetchval("INSERT INTO llm_records(agent_id,task_id,status,session_id) "
            "VALUES ('planner-target','target','running','recovered-session') RETURNING id")
        await store.bind_injection_record(row['id'],record,db=pool)
        recovered = await store.recover_planner_injection('target','recovered-session',db=pool)
        assert recovered['state'] == 'uncertain' and recovered['record_id'] == record
        assert recovered['evidence']['status'] == 'running'
        assert recovered['block'] == block
        assert not await pool.fetchval('SELECT count(*) FROM memory_lesson_stats')
    run_db(check,setup=setup)


def test_snapshot_selection_checks_project_source_destination_and_all_sort_keys():
    async def check(pool):
        from looper.agents import memory_lesson_injection as service
        selected = []
        for i in range(5):
            candidate,_ = await active(pool,statement=f'Sort candidate {i}')
            selected.append(candidate)
        for candidate,cited,reinforced,days in [(selected[0],2,1,0),(selected[1],2,2,0),
                (selected[2],2,2,1),(selected[3],2,2,1),(selected[4],3,0,0)]:
            await pool.execute('INSERT INTO memory_lesson_stats(candidate_id,cited,reinforced) VALUES ($1,$2,$3)',candidate,cited,reinforced)
            await pool.execute("UPDATE memory_candidate_events SET created_at=$2 WHERE candidate_id=$1 AND action='activate'",candidate,sup.NOW+timedelta(days=days))
        await pool.execute("INSERT INTO projects(id,name,memory_lessons) VALUES ('other','Other','on')")
        other,_ = await active(pool,statement='Other project',project_id='other')
        held = await memory_lessons_support.lesson(pool,'proj-mem','External candidate')
        from looper.agents.memory_model import parse_id
        await pool.execute("UPDATE memory_candidates SET source_class='external' WHERE id=$1",parse_id(held))
        block = await service.ensure_snapshot('target','proj-mem',now=sup.NOW,db=pool)
        assert block.candidate_ids == (selected[4],selected[2],selected[3],selected[1],selected[0])
        assert other not in block.candidate_ids and parse_id(held) not in block.candidate_ids
        await pool.execute("UPDATE projects SET memory_lessons='off' WHERE id='proj-mem'")
        assert not (await service.render_for_run('target','proj-mem','worker',db=pool,now=sup.NOW)).text
        assert not await pool.fetchval('SELECT count(*) FROM memory_injections')
    run_db(check,setup=setup)


@pytest.mark.parametrize('change',['held','revoked','superseded','expired','source_task_deleted'])
def test_inactive_lesson_variants_are_filtered_without_backfill(change):
    async def check(pool):
        from looper.agents import memory_lesson_injection as service
        from looper.agents.memory_lessons_model import LessonSettings
        from looper.agents.memory_model import State,Action,Actor,Via,Reason
        from looper.db import memory,memory_lessons
        candidate,journal = await active(pool,statement='Chosen lesson')
        backup,_ = await active(pool,statement='Lesson omitted by the cap')
        await pool.execute('INSERT INTO memory_lesson_stats(candidate_id,cited) VALUES ($1,1)',candidate)
        block = await service.ensure_snapshot('target','proj-mem',LessonSettings(enabled=True,max_lessons=1),sup.NOW,db=pool)
        assert block.candidate_ids == (candidate,) and block.omitted == 1
        newcomer,_ = await active(pool,statement='Lesson activated after the snapshot')
        if change == 'source_task_deleted':
            source_task = await pool.fetchval('SELECT task_id FROM task_journal WHERE id=$1',journal)
            await pool.execute('DELETE FROM tasks WHERE id=$1',source_task)
            assert not await pool.fetchval('SELECT count(*) FROM memory_candidates WHERE id=$1',candidate)
        else:
            kwargs = {'held':dict(to=State.HELD,action=Action.HOLD,actor=Actor.SYSTEM,via=Via.SYSTEM,reason=Reason.FLAGGED_WRONG),
                      'revoked':dict(to=State.REVOKED,action=Action.REVOKE,actor=Actor.USER,via=Via.CLI),
                      'superseded':dict(to=State.SUPERSEDED,action=Action.SUPERSEDE,actor=Actor.SYSTEM,via=Via.SYSTEM),
                      'expired':dict(to=State.EXPIRED,action=Action.EXPIRE,actor=Actor.SYSTEM,via=Via.SYSTEM)}[change]
            async with memory.transaction(pool) as conn:
                await memory_lessons.transition_lesson(conn,candidate,now=sup.NOW,valid_days=90,
                    expected_state=State.IN_EFFECT,**kwargs)
        filtered = await service.render_for_run('target','proj-mem','worker',db=pool,now=sup.NOW)
        assert not filtered.text and not filtered.candidate_ids
        assert await pool.fetchval("SELECT count(*) FROM memory_candidates WHERE id=ANY($1) AND state='in_effect'",
                                   [backup,newcomer]) == 2
        assert not await pool.fetchval("SELECT count(*) FROM task_lesson_snapshots WHERE task_id='target' "
                                     'AND candidate_id=ANY($1)',[backup,newcomer])
    run_db(check,setup=setup)


def test_revoke_rolls_back_candidate_event_and_tombstone_together():
    async def check(pool):
        from looper.agents import memory_lesson_injection as service
        from looper.agents.memory_model import State,Action,Actor,Via
        from looper.db import memory,memory_lessons,memory_lesson_injection as store
        candidate,_ = await active(pool)
        original = await service.ensure_snapshot('target','proj-mem',now=sup.NOW,db=pool)
        event_count = await pool.fetchval('SELECT count(*) FROM memory_candidate_events WHERE candidate_id=$1',candidate)
        with pytest.raises(RuntimeError,match='synthetic rollback'):
            async with memory.transaction(pool) as conn:
                await memory_lessons.transition_lesson(conn,candidate,State.REVOKED,action=Action.REVOKE,
                    actor=Actor.USER,via=Via.CLI,now=sup.NOW,valid_days=90,expected_state=State.IN_EFFECT)
                assert await conn.fetchval('SELECT count(*) FROM memory_tombstones') == 1
                raise RuntimeError('synthetic rollback')
        assert await pool.fetchval('SELECT state FROM memory_candidates WHERE id=$1',candidate) == 'in_effect'
        assert await pool.fetchval('SELECT count(*) FROM memory_candidate_events WHERE candidate_id=$1',candidate) == event_count
        assert not await pool.fetchval('SELECT count(*) FROM memory_tombstones')
        assert await service.render_for_run('target','proj-mem','worker',now=sup.NOW,db=pool) == original
        prepared = await store.prepare_injection('target','worker','after-rollback',original,now=sup.NOW,db=pool)
        assert prepared['block'] == original
    run_db(check,setup=setup)


def test_reopened_planner_resets_only_disabled_episode_ack():
    async def check(pool):
        from looper.agents import memory_lesson_injection as service
        from looper.db import memory_lesson_injection as store
        candidate,_ = await active(pool)
        block = await service.ensure_snapshot('target','proj-mem',now=sup.NOW,db=pool)
        row = await store.prepare_injection('target','planner','session:a',block,session_key='a',db=pool,now=sup.NOW)
        await store.mark_delivered(row['id'],db=pool,now=sup.NOW)
        stopped = service.PlannerNotices('Synthetic per-lesson notice',(candidate,))
        await store.ack_planner_notices('target','a',stopped,db=pool)
        await pool.execute("UPDATE system_settings SET value='{\"enabled\":false}' WHERE key='memory_lessons'")
        first = await service.planner_notices('target','a',db=pool,now=sup.NOW)
        await store.ack_planner_notices('target','a',first,db=pool)
        assert not (await service.planner_notices('target','a',db=pool,now=sup.NOW)).text
        await store.reset_planner_disabled_notice('target','different-session',db=pool)
        assert not (await service.planner_notices('target','a',db=pool,now=sup.NOW)).text
        await pool.execute("UPDATE system_settings SET value='{\"enabled\":true}' WHERE key='memory_lessons'")
        await store.reset_planner_disabled_notice('target','a',db=pool)
        state = await store.planner_state('target','a',db=pool)
        assert state['notices_acked'] == [candidate] and not state['disabled_acked']
        assert await pool.fetchval('SELECT injected FROM memory_lesson_stats WHERE candidate_id=$1',candidate) == 1
        await pool.execute("UPDATE system_settings SET value='{\"enabled\":false}' WHERE key='memory_lessons'")
        second = await service.planner_notices('target','a',db=pool,now=sup.NOW)
        assert second == first
        await store.ack_planner_notices('target','a',second,db=pool)
        assert not (await service.planner_notices('target','a',db=pool,now=sup.NOW)).text
    run_db(check,setup=setup)


@pytest.mark.parametrize('inject,expected', [
    ({'planner': False, 'worker': True}, {'worker'}),
    ({'planner': True, 'worker': False}, {'planner'}),
    ({'reviewer': True}, {'planner', 'worker', 'reviewer'}),
])
def test_role_gates_are_independent_and_manager_never_receives_lessons(inject, expected):
    async def check(pool):
        from looper.agents import memory_lesson_injection as service
        from looper.db import memory_lesson_injection as store
        candidate, _ = await active(pool)
        await pool.execute('UPDATE system_settings SET value=$1 WHERE key=\'memory_lessons\'',
                           json.dumps({'enabled': True, 'inject': inject}))
        original = await service.ensure_snapshot('target', 'proj-mem', now=sup.NOW, db=pool)
        for role in ('planner', 'worker', 'reviewer', 'manager'):
            block = await service.render_for_run('target', 'proj-mem', role, now=sup.NOW, db=pool)
            if role in expected:
                assert block == original
                row = await store.prepare_injection('target', role, 'role:' + role, block, now=sup.NOW, db=pool)
                await store.mark_delivered(row['id'], now=sup.NOW, db=pool)
            else:
                assert not block.text
                assert await store.prepare_injection('target', role, 'closed:' + role, original,
                                                      now=sup.NOW, db=pool) is None
        assert await pool.fetchval('SELECT injected FROM memory_lesson_stats WHERE candidate_id=$1', candidate) == len(expected)
        assert {row['agent_kind'] for row in await pool.fetch('SELECT agent_kind FROM memory_injections')} == expected
    run_db(check, setup=setup)


def test_prepare_locks_every_snapshot_candidate_before_restoring_renewed_lesson(monkeypatch):
    async def check(pool):
        import asyncpg
        from looper.agents import memory_lesson_injection as service
        from looper.agents.memory_model import State,Action,Actor,Via
        from looper.db import memory,memory_lessons,memory_lesson_injection as store
        first,_ = await active(pool,statement='Originally selected first lesson')
        second,_ = await active(pool,statement='Originally selected second lesson')
        original = await service.ensure_snapshot('target','proj-mem',now=sup.NOW,db=pool)
        assert set(original.candidate_ids) == {first,second}
        await pool.execute('UPDATE memory_candidates SET valid_until=$2 WHERE id=$1',first,sup.NOW)
        stale = await service.render_for_run('target','proj-mem','worker',now=sup.NOW,db=pool)
        assert stale.candidate_ids == (second,)
        async with memory.transaction(pool) as conn:
            await memory_lessons.transition_lesson(conn,first,State.IN_EFFECT,action=Action.RENEW,
                actor=Actor.USER,via=Via.CLI,now=sup.NOW,valid_days=90,expected_state=State.IN_EFFECT)
        newcomer,_ = await active(pool,statement='New lesson outside the frozen snapshot')
        real_render = store.render_snapshot
        lock_observed = False

        async def render_with_lock_probe(task_id,project_id,now,*,db=None):
            nonlocal lock_observed
            # Probe from an independent transaction while prepare holds its candidate locks.
            async with pool.acquire() as other:
                with pytest.raises(asyncpg.LockNotAvailableError):
                    async with other.transaction():
                        await other.fetchrow('SELECT id FROM memory_candidates WHERE id=$1 FOR UPDATE NOWAIT',first)
            lock_observed = True
            return await real_render(task_id,project_id,now,db=db)

        monkeypatch.setattr(store,'render_snapshot',render_with_lock_probe)
        prepared = await store.prepare_injection('target','worker','renewed-snapshot',stale,now=sup.NOW,db=pool)
        assert lock_observed
        assert prepared['block'] == original
        assert newcomer not in prepared['block'].candidate_ids
    run_db(check,setup=setup)
