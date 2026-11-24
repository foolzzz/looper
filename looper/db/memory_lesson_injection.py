"""Short transactions for immutable snapshots, delivery evidence and lesson references."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .connection import get_pool
from .memory import transaction, MemoryRefused
from .memory_lessons import transition_lesson
from ..agents.memory_model import State,Action,Actor,Via,Reason


def _now(value=None):
    return value or datetime.now(timezone.utc)


def _service():
    from ..agents import memory_lesson_injection
    return memory_lesson_injection


def _delivery(row):
    if row is None:
        return None
    result = dict(row)
    result['candidate_ids'] = tuple(result['candidate_ids'])
    result['block'] = _service().LessonBlock(result['block_text'],result['block_sha256'],result['candidate_ids'],
                                            result['omitted'],result['snapshot_key'])
    return result


async def ensure_snapshot(task_id,project_id,settings,now,*,db=None):
    service = _service()
    async with transaction(db) as conn:
        task = await conn.fetchrow('SELECT project_id FROM tasks WHERE id=$1 FOR UPDATE',task_id)
        if task is None or task['project_id'] != project_id:
            raise MemoryRefused('task does not belong to the lesson project',code='not_found')
        if await conn.fetchval('SELECT EXISTS(SELECT 1 FROM task_lesson_snapshot_sets WHERE task_id=$1)',task_id):
            return
        if await conn.fetchval('SELECT EXISTS(SELECT 1 FROM task_lesson_snapshots WHERE task_id=$1)',task_id):
            raise MemoryRefused('lesson snapshot rows have no frozen snapshot set; operator inspection is required',code='invalid')
        rows = await conn.fetch("SELECT c.*,COALESCE(s.cited,0) AS cited,COALESCE(s.reinforced,0) AS reinforced,"
            "(SELECT min(e.created_at) FROM memory_candidate_events e WHERE e.candidate_id=c.id AND e.action='activate') "
            "AS activated_at FROM memory_candidates c LEFT JOIN memory_lesson_stats s ON s.candidate_id=c.id "
            "WHERE c.project_id=$1 AND c.destination='looper' AND c.kind='lesson' AND c.state='in_effect' "
            "AND c.source_class='agent' AND c.valid_until>$2 ORDER BY c.id FOR UPDATE OF c",project_id,now)
        rows = [dict(row) for row in rows]
        if any(row['activated_at'] is None for row in rows):
            raise MemoryRefused('an active lesson has no activation event; repair its provenance before snapshotting',code='invalid')
        rows.sort(key=lambda r:(-r['cited'],-r['reinforced'],-r['activated_at'].timestamp(),r['id']))
        selected,omitted = service.select_lines(rows,settings)
        await conn.execute('INSERT INTO task_lesson_snapshot_sets(task_id,initialized_at,max_lessons,max_chars,'
            'in_effect_total,header,footer_template,format_version,omitted) VALUES ($1,$2,$3,$4,$5,$6,$7,1,$8)',
            task_id,now,settings.max_lessons,settings.max_chars,len(rows),service.LESSON_HEADER,service.LESSON_FOOTER,omitted)
        for position,(row,line) in enumerate(selected,1):
            await conn.execute('INSERT INTO task_lesson_snapshots(task_id,candidate_id,position,line_text,activated_at,fingerprint) '
                'VALUES ($1,$2,$3,$4,$5,$6)',task_id,row['id'],position,line,row['activated_at'],row['fingerprint'])


async def render_snapshot(task_id,project_id,now,*,db=None):
    conn = db or get_pool()
    recipe = await conn.fetchrow('SELECT * FROM task_lesson_snapshot_sets WHERE task_id=$1',task_id)
    if recipe is None:
        return _service().EMPTY_BLOCK
    rows = await conn.fetch("SELECT s.* FROM task_lesson_snapshots s JOIN memory_candidates c ON c.id=s.candidate_id "
        "WHERE s.task_id=$1 AND c.project_id=$2 AND c.destination='looper' AND c.kind='lesson' "
        "AND c.source_class='agent' AND c.state='in_effect' AND c.valid_until>$3 ORDER BY s.position",task_id,project_id,now)
    return _service().render_snapshot(recipe,rows)


async def prepare_injection(task_id,agent_kind,delivery_key,block,*,project_id=None,step_id=None,record_id=None,
                            session_key=None,db=None,now=None):
    if not block.text or not block.candidate_ids:
        return None
    service = _service()
    now = _now(now)
    async with transaction(db) as conn:
        project_id = project_id or await conn.fetchval('SELECT project_id FROM tasks WHERE id=$1',task_id)
        if await service.runtime_settings(project_id,agent_kind,db=conn) is None:
            return None
        # A previously expired snapshot member can be renewed between rendering and preparation.
        # Lock the complete frozen membership before re-rendering, including members absent from block.
        await conn.fetch('SELECT c.id FROM memory_candidates c JOIN task_lesson_snapshots s ON s.candidate_id=c.id '
            'WHERE s.task_id=$1 ORDER BY c.id FOR UPDATE OF c',task_id)
        existing = await conn.fetchrow('SELECT * FROM memory_injections WHERE task_id=$1 AND agent_kind=$2 AND '
            'delivery_key=$3',task_id,agent_kind,delivery_key)
        if existing:
            return None if existing['state'] == 'abandoned' else _delivery(existing)
        if agent_kind == 'planner' and session_key:
            if existing := await planner_injection(task_id,session_key,db=conn):
                return existing
        current = await render_snapshot(task_id,project_id,now,db=conn)
        if not current.text:
            return None
        row = await conn.fetchrow('INSERT INTO memory_injections(task_id,agent_kind,step_id,record_id,delivery_key,'
            'session_key,block_text,block_sha256,block_chars,omitted,candidate_ids,created_at,snapshot_key) '
            'VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13) '
            'ON CONFLICT(task_id,agent_kind,delivery_key) DO UPDATE SET delivery_key=EXCLUDED.delivery_key RETURNING *',
            task_id,agent_kind,step_id,record_id,delivery_key,session_key,current.text,current.sha256,len(current.text),
            current.omitted,list(current.candidate_ids),now,current.snapshot_key)
        result = _delivery(row)
        result['block'] = current
        return result


async def get_injection(injection_id,*,db=None):
    return _delivery(await (db or get_pool()).fetchrow('SELECT * FROM memory_injections WHERE id=$1',injection_id))


async def injection_for_record(record_id,*,db=None):
    return _delivery(await (db or get_pool()).fetchrow('SELECT * FROM memory_injections WHERE record_id=$1 ORDER BY id DESC LIMIT 1',record_id))


async def planner_injection(task_id,session_key,*,db=None):
    return _delivery(await (db or get_pool()).fetchrow("SELECT * FROM memory_injections WHERE task_id=$1 AND "
        "agent_kind='planner' AND session_key IS NOT DISTINCT FROM $2 AND state<>'abandoned' ORDER BY id LIMIT 1",task_id,session_key))


async def mark_delivered(injection_id,*,session_key=None,record_id=None,db=None,now=None):
    now = _now(now)
    async with transaction(db) as conn:
        initial = await conn.fetchrow('SELECT * FROM memory_injections WHERE id=$1',injection_id)
        if initial is None:
            return None
        # All operations that touch multiple candidates take the same ascending candidate locks first.
        candidates = await conn.fetch('SELECT id FROM memory_candidates WHERE id=ANY($1) ORDER BY id FOR UPDATE',initial['candidate_ids'])
        row = await conn.fetchrow('SELECT * FROM memory_injections WHERE id=$1 FOR UPDATE',injection_id)
        if row is None or row['state'] == 'abandoned':
            return _delivery(row)
        if session_key and row['agent_kind'] == 'planner':
            prior = await planner_injection(row['task_id'],session_key,db=conn)
            if prior and prior['id'] != injection_id:
                await conn.execute("UPDATE memory_injections SET state='abandoned' WHERE id=$1",injection_id)
                return prior
        was_delivered = row['state'] == 'delivered'
        row = await conn.fetchrow("UPDATE memory_injections SET state='delivered',delivered_at=COALESCE(delivered_at,$2),"
            'session_key=COALESCE($3,session_key),record_id=COALESCE($4,record_id) WHERE id=$1 RETURNING *',
            injection_id,now,session_key,record_id)
        surviving = {c['id'] for c in candidates}
        for position,candidate_id in enumerate(row['candidate_ids'],1):
            if was_delivered or candidate_id not in surviving:
                continue
            await conn.execute('INSERT INTO memory_injection_items(injection_id,candidate_id,position) VALUES ($1,$2,$3)',
                               injection_id,candidate_id,position)
            await conn.execute('INSERT INTO memory_lesson_stats(candidate_id,injected,last_injected_at) VALUES ($1,1,$2) '
                'ON CONFLICT(candidate_id) DO UPDATE SET injected=memory_lesson_stats.injected+1,last_injected_at=GREATEST(memory_lesson_stats.last_injected_at,EXCLUDED.last_injected_at)',
                candidate_id,now)
        if row['agent_kind'] == 'planner':
            await conn.execute('INSERT INTO task_lesson_planner_state(task_id,session_key,delivered_ids) VALUES ($1,$2,$3) '
                'ON CONFLICT(task_id) DO UPDATE SET session_key=EXCLUDED.session_key,delivered_ids=EXCLUDED.delivered_ids,'
                "notices_acked=CASE WHEN task_lesson_planner_state.session_key IS NOT DISTINCT FROM EXCLUDED.session_key "
                "THEN task_lesson_planner_state.notices_acked ELSE '{}' END,"
                'disabled_acked=CASE WHEN task_lesson_planner_state.session_key IS NOT DISTINCT FROM EXCLUDED.session_key '
                'THEN task_lesson_planner_state.disabled_acked ELSE false END',row['task_id'],row['session_key'],row['candidate_ids'])
        return _delivery(row)


async def abandon_injection(injection_id,*,db=None):
    await (db or get_pool()).execute("UPDATE memory_injections SET state='abandoned' WHERE id=$1 AND state='prepared'",injection_id)


async def mark_uncertain(injection_id,*,db=None):
    await (db or get_pool()).execute("UPDATE memory_injections SET state='uncertain' WHERE id=$1 AND state='prepared'",injection_id)


async def apply_references(injection_id,refs,*,wrong_threshold=None,valid_days=None,db=None,now=None):
    if wrong_threshold is None or valid_days is None:
        from ..agents import memory_lessons, memory_lessons_model
        settings = memory_lessons_model.parse_settings((await memory_lessons.settings_view(db=db))['settings'])
        wrong_threshold = settings.wrong_threshold if wrong_threshold is None else wrong_threshold
        valid_days = settings.valid_days if valid_days is None else valid_days
    now = _now(now)
    result = {'cited':[],'wrong':[],'conflicts':list(refs.conflicts),'invalid':list(refs.invalid)}
    async with transaction(db) as conn:
        injection = await conn.fetchrow('SELECT * FROM memory_injections WHERE id=$1',injection_id)
        if injection is None or injection['state'] != 'delivered':
            return result
        requested = set(refs.used) | set(refs.wrong)
        candidate_rows = await conn.fetch('SELECT id,state FROM memory_candidates WHERE id=ANY($1) ORDER BY id FOR UPDATE',
                                         sorted(requested & set(injection['candidate_ids'])))
        for candidate in candidate_rows:
            number = candidate['id']
            item = await conn.fetchrow('SELECT cited,wrong FROM memory_injection_items WHERE injection_id=$1 AND candidate_id=$2 FOR UPDATE',injection_id,number)
            if item is None:
                continue
            cited = number in refs.used
            if item['cited'] or item['wrong']:
                if item['cited'] != cited:
                    result['conflicts'].append(number)
                continue
            await conn.execute('UPDATE memory_injection_items SET cited=$3,wrong=$4 WHERE injection_id=$1 AND candidate_id=$2',
                               injection_id,number,cited,not cited)
            if cited:
                await conn.execute('INSERT INTO memory_lesson_stats(candidate_id,cited,last_cited_at) VALUES ($1,1,$2) '
                    'ON CONFLICT(candidate_id) DO UPDATE SET cited=memory_lesson_stats.cited+1,last_cited_at=GREATEST(memory_lesson_stats.last_cited_at,EXCLUDED.last_cited_at)',number,now)
                result['cited'].append(number)
            else:
                is_new = await conn.fetchval('INSERT INTO memory_lesson_wrong_tasks(candidate_id,task_id,first_injection_id,created_at) '
                    'VALUES ($1,$2,$3,$4) ON CONFLICT DO NOTHING RETURNING true',number,injection['task_id'],injection_id,now)
                if is_new:
                    count = await conn.fetchval('INSERT INTO memory_lesson_stats(candidate_id,wrong_tasks) VALUES ($1,1) '
                        'ON CONFLICT(candidate_id) DO UPDATE SET wrong_tasks=memory_lesson_stats.wrong_tasks+1 RETURNING wrong_tasks',number)
                    if count >= wrong_threshold and candidate['state'] == State.IN_EFFECT:
                        await transition_lesson(conn,number,State.HELD,action=Action.HOLD,actor=Actor.SYSTEM,via=Via.SYSTEM,
                            reason=Reason.FLAGGED_WRONG,expected_state=State.IN_EFFECT,now=now,valid_days=valid_days)
                result['wrong'].append(number)
    return result


async def planner_state(task_id,session_key,*,db=None):
    row = await (db or get_pool()).fetchrow('SELECT s.*,t.project_id FROM task_lesson_planner_state s '
        'JOIN tasks t ON t.id=s.task_id WHERE s.task_id=$1 AND s.session_key IS NOT DISTINCT FROM $2',task_id,session_key)
    return dict(row) if row else None


async def inactive_delivered(task_id,session_key,now,*,db=None):
    return [dict(r) for r in await (db or get_pool()).fetch("SELECT ids.id,COALESCE(c.state,'deleted') AS state,c.reason,"
        "COALESCE(c.valid_until<=$3,false) AS expired FROM task_lesson_planner_state s "
        "CROSS JOIN LATERAL unnest(s.delivered_ids) ids(id) LEFT JOIN memory_candidates c ON c.id=ids.id "
        "WHERE s.task_id=$1 AND s.session_key IS NOT DISTINCT FROM $2 AND NOT(ids.id=ANY(s.notices_acked)) "
        "AND (c.id IS NULL OR c.state<>'in_effect' OR c.valid_until<=$3) ORDER BY ids.id",task_id,session_key,now)]


async def ack_planner_notices(task_id,session_key,notices,*,db=None):
    await (db or get_pool()).execute('UPDATE task_lesson_planner_state SET notices_acked=ARRAY('
        'SELECT DISTINCT x FROM unnest(notices_acked || $3::bigint[]) x ORDER BY x),'
        'disabled_acked=disabled_acked OR $4 WHERE task_id=$1 AND session_key IS NOT DISTINCT FROM $2',
        task_id,session_key,list(notices.ids),notices.disabled)


async def planner_session_has_lessons(session_key,*,exclude_task_id=None,db=None):
    return await (db or get_pool()).fetchval("SELECT EXISTS(SELECT 1 FROM task_lesson_planner_state "
        "WHERE session_key=$1 AND cardinality(delivered_ids)>0 AND ($2::text IS NULL OR task_id<>$2))",
        session_key,exclude_task_id)


async def expire_due(*,db=None,now=None,limit=200):
    from ..agents import memory_lessons
    from ..agents.memory_lessons_model import parse_settings
    view = await memory_lessons.settings_view(db=db)
    if not view['enabled']:
        return 0
    settings = parse_settings(view['settings'])
    now = _now(now)
    count = 0
    async with transaction(db) as conn:
        rows = await conn.fetch("SELECT id,state FROM memory_candidates WHERE destination='looper' "
            "AND state IN ('in_effect','held') AND valid_until<=$1 ORDER BY id LIMIT $2 FOR UPDATE SKIP LOCKED",now,limit)
        for row in rows:
            await transition_lesson(conn,row['id'],State.EXPIRED,action=Action.EXPIRE,actor=Actor.SYSTEM,via=Via.SYSTEM,
                now=now,valid_days=settings.valid_days,expected_state=row['state'])
            count += 1
    return count


async def purge_logs(*,db=None,now=None,limit=1000):
    # Preserve the only accounting/identity evidence while a task can still use its session.
    cutoff = _now(now)-timedelta(days=90)
    return await (db or get_pool()).fetchval("WITH doomed AS (SELECT i.id FROM memory_injections i "
        "JOIN tasks t ON t.id=i.task_id WHERE i.created_at<$1 AND t.status IN ('completed','failed','cancelled') "
        "ORDER BY i.id LIMIT $2), deleted AS (DELETE FROM memory_injections WHERE id IN (SELECT id FROM doomed) RETURNING id) "
        'SELECT count(*) FROM deleted',cutoff,limit)


def _public(value):
    if isinstance(value,datetime):
        return value.isoformat()
    if isinstance(value,dict):
        return {key:_public(val) for key,val in value.items() if key != 'block'}
    if isinstance(value,(list,tuple)):
        return [_public(val) for val in value]
    return value


async def task_view(task_id,*,limit=50,before=None,db=None):
    conn = db or get_pool()
    recipe = await conn.fetchrow('SELECT * FROM task_lesson_snapshot_sets WHERE task_id=$1',task_id)
    snapshot = [dict(row) for row in await conn.fetch('SELECT s.*,c.state,c.reason,c.valid_until '
        'FROM task_lesson_snapshots s JOIN memory_candidates c ON c.id=s.candidate_id WHERE task_id=$1 ORDER BY position',task_id)]
    rows = await conn.fetch('SELECT * FROM memory_injections WHERE task_id=$1 AND ($2::bigint IS NULL OR id<$2) '
                            'ORDER BY id DESC LIMIT $3',task_id,before,limit+1)
    injections = []
    for row in rows[:limit]:
        item = dict(row)
        item['items'] = [dict(r) for r in await conn.fetch('SELECT * FROM memory_injection_items WHERE injection_id=$1 ORDER BY position',row['id'])]
        injections.append(item)
    from ..agents.memory_model import format_id
    for row in snapshot:
        row['candidate_id'] = format_id(row['candidate_id'])
    for row in injections:
        row['candidate_ids'] = [format_id(i) for i in row['candidate_ids']]
        for item in row['items']:
            item['candidate_id'] = format_id(item['candidate_id'])
    return _public({'initialized':recipe is not None,'snapshot':snapshot,'injections':injections,'more':len(rows)>limit})


async def recover_prepared(*,task_id=None,db=None):
    """At supervisor recovery, quarantine unacknowledged launches without assuming transport success.

    A runtime may subsequently use its durable session/record evidence to call ``mark_delivered``.
    Callers must scope this to the task they own; this function never resends model input.
    """
    if task_id is None:
        raise ValueError('prepared recovery requires an owned task_id')
    rows = await (db or get_pool()).fetch("UPDATE memory_injections SET state='uncertain' "
        "WHERE task_id=$1 AND state='prepared' RETURNING *",task_id)
    return [_delivery(row) for row in rows]


async def bind_injection_record(injection_id,record_id,*,db=None):
    """Bind the actual recorder before launch; binding is not evidence of model input acceptance."""
    await (db or get_pool()).execute('UPDATE memory_injections SET record_id=$2 WHERE id=$1 '
        "AND state IN ('prepared','uncertain') AND (record_id IS NULL OR record_id=$2)",injection_id,record_id)


async def recover_planner_injection(task_id,session_key,*,db=None):
    """Quarantine one interrupted Planner launch and expose durable evidence without resending or counting."""
    async with transaction(db) as conn:
        row = await conn.fetchrow("SELECT * FROM memory_injections WHERE task_id=$1 AND agent_kind='planner' "
            "AND state IN ('prepared','uncertain') AND (session_key IS NULL OR session_key=$2) "
            'ORDER BY id DESC LIMIT 1 FOR UPDATE',task_id,session_key)
        if row is None:
            return None
        record = (await conn.fetchrow('SELECT r.id,r.status,r.session_id,r.last_output_at,r.output_tokens,'
            's.session_id AS saved_session_id FROM llm_records r LEFT JOIN agent_sessions s ON s.agent_id=r.agent_id '
            'WHERE r.id=$1 AND r.task_id=$2',row['record_id'],task_id) if row['record_id'] else None)
        if row['state'] == 'prepared':
            row = await conn.fetchrow("UPDATE memory_injections SET state='uncertain' WHERE id=$1 RETURNING *",row['id'])
        result = _delivery(row)
        result['evidence'] = dict(record) if record else None
        return result


async def reset_planner_disabled_notice(task_id,session_key,*,db=None):
    """An enabled session begins a new close episode; retain per-lesson notice acknowledgements."""
    await (db or get_pool()).execute('UPDATE task_lesson_planner_state SET disabled_acked=false '
        'WHERE task_id=$1 AND session_key IS NOT DISTINCT FROM $2 AND disabled_acked',task_id,session_key)


async def has_prepared_injections(task_id,*,db=None):
    """Read whether an owned task has unacknowledged launches before supervisor recovery."""
    return await (db or get_pool()).fetchval('SELECT EXISTS(SELECT 1 FROM memory_injections '
        "WHERE task_id=$1 AND state='prepared')",task_id)
