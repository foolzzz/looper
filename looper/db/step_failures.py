"""Transaction owner for step failure sequences and current holds.

State follows a task's stable step key across replans. History keeps the original
step and run identities even after the corresponding step or run is removed.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any


from .connection import get_pool
from . import journal
from .tasks import ACTIVE_STATUSES
from ..agents.failures import Failure, FailureClass


class StoreError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _row(row) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    for key in ('target', 'result', 'targets', 'payload'):
        if isinstance(out.get(key), str):
            out[key] = json.loads(out[key])
    return out


async def _live_step(conn, task_id, step_id, *, require_live=True):
    task = await conn.fetchrow("SELECT status,completed_at FROM tasks WHERE id=$1 FOR UPDATE", task_id)
    if task is None:
        raise StoreError('not_found', 'Task does not exist')
    step = await conn.fetchrow("SELECT * FROM task_steps WHERE id=$1 FOR UPDATE", step_id)
    if step is None:
        raise StoreError('not_found', 'Step does not exist')
    if step['task_id'] != task_id:
        raise StoreError('invalid', 'Step does not belong to this task')
    if require_live and (task['status'] not in ACTIVE_STATUSES or task['completed_at'] is not None):
        raise StoreError('conflict', 'Task is no longer live')
    return task, step


async def _current_run(conn, task_id, step, run_id, role, target):
    unit_id = target.get('unit_step_id') or step['id']
    unit = step if unit_id == step['id'] else await conn.fetchrow(
        'SELECT * FROM task_steps WHERE id=$1 AND task_id=$2 FOR UPDATE', unit_id, task_id)
    if unit is None:
        raise StoreError('invalid', 'Resume target does not belong to this task')
    kind = target.get('kind', role)
    source_column = 'worker_id' if role == 'worker' or kind == 'panel' else 'reviewer_id'
    if unit[source_column] != run_id:
        raise StoreError('conflict', 'Run is no longer current for this execution unit')
    if unit['status'] in ('verified', 'skipped'):
        raise StoreError('conflict', 'Step already settled')
    return unit


async def _state(conn, task_id, step):
    key = step['step_key'] or step['id']
    await conn.execute('INSERT INTO step_failure_state(task_id,step_key,step_id) VALUES($1,$2,$3) '
                       'ON CONFLICT(task_id,step_key) DO UPDATE SET step_id=EXCLUDED.step_id', task_id, key, step['id'])
    return await conn.fetchrow('SELECT * FROM step_failure_state WHERE task_id=$1 AND step_key=$2 FOR UPDATE', task_id, key)


async def _journal(conn, task_id, step_id, summary):
    await journal.insert(conn, task_id, [{'step_id': step_id, 'kind': 'failure_hold', 'summary': summary}])


async def record_failure(*, task_id: str, step_id: str, run_id: str, role: str, failure: Failure,
                         target: Mapping[str, Any], result: Mapping[str, Any], attempt=None, record_id=None,
                         provider_id=None, hold_until: Callable[[int], datetime | None], pool=None) -> dict[str, Any]:
    if role not in ('worker', 'reviewer') or not run_id or not isinstance(failure, Failure):
        raise StoreError('invalid', 'Failure requires a typed cause, role, and run identity')
    pool = pool or get_pool()
    async with pool.acquire() as conn, conn.transaction():
        _, step = await _live_step(conn, task_id, step_id)
        duplicate = await conn.fetchrow('SELECT * FROM step_failures WHERE task_id=$1 AND run_id=$2', task_id, run_id)
        if duplicate is not None:
            return {'action': 'duplicate', 'failure': _row(duplicate), 'hold': None}
        if await conn.fetchval('SELECT EXISTS(SELECT 1 FROM step_failure_runs WHERE task_id=$1 AND run_id=$2)', task_id, run_id):
            return {'action': 'ignored', 'failure': None, 'hold': None}
        unit = await _current_run(conn, task_id, step, run_id, role, target)
        state = await _state(conn, task_id, step)
        streak = state['streak'] + 1
        action = 'breaker' if streak >= 3 else 'planner' if failure.cls is FailureClass.JUDGMENT else 'hold'
        await conn.execute('UPDATE step_failure_state SET streak=$3,updated_at=now() WHERE task_id=$1 AND step_key=$2',
                           task_id, state['step_key'], streak)
        await conn.execute("INSERT INTO step_failure_runs(task_id,run_id,step_key,outcome) VALUES($1,$2,$3,'failed')",
                           task_id, run_id, state['step_key'])
        row = _row(await conn.fetchrow(
            'INSERT INTO step_failures(task_id,step_id,step_identity,step_key,run_id,attempt,record_id,role,'
            'failure_class,failure_cause,streak,epoch,action,target,result) '
            'VALUES($1,$2,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13::jsonb,$14::jsonb) RETURNING *',
            task_id, step_id, state['step_key'], run_id, str(attempt) if attempt is not None else None, record_id, role,
            failure.cls.value, failure.cause.value, streak, state['epoch'], action, json.dumps(dict(target)),
            json.dumps(dict(result))))
        hold = None
        if action in ('hold', 'breaker'):
            old = _row(await conn.fetchrow('SELECT * FROM step_holds WHERE step_id=$1 FOR UPDATE', step_id))
            targets = list(old['targets']) if old else []
            targets.append({'target': dict(target), 'result': dict(result), 'failure_id': row['id'],
                            'run_id': run_id, 'role': role, 'failure_class': failure.cls.value,
                            'failure_cause': failure.cause.value})
            kind = 'breaker' if action == 'breaker' else failure.cls.value
            until = None if kind in ('environment', 'breaker') else hold_until(streak)
            hold_cause = failure.cause.value
            if old and old['kind'] == 'environment' and kind != 'breaker':
                kind, until, hold_cause = 'environment', None, old['cause']
            if old and old['until'] is not None and until is not None:
                until = max(until, old['until'])
            if kind in ('quota', 'transient') and until is None:
                raise StoreError('invalid', 'Timed failure holds require a finite deadline')
            # A deleted provider cannot keep the target waiting on a nonexistent account.
            provider = provider_id if provider_id and await conn.fetchval(
                'SELECT EXISTS(SELECT 1 FROM llm_providers WHERE id=$1)', provider_id) else None
            hold = _row(await conn.fetchrow(
                'INSERT INTO step_holds(step_id,task_id,kind,cause,provider_id,until,failure_id,targets) '
                'VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb) ON CONFLICT(step_id) DO UPDATE SET '
                'kind=EXCLUDED.kind,cause=EXCLUDED.cause,provider_id=EXCLUDED.provider_id,until=EXCLUDED.until,'
                'failure_id=EXCLUDED.failure_id,targets=EXCLUDED.targets,reminded_at=NULL RETURNING *',
                step_id, task_id, kind, hold_cause, provider, until, row['id'], json.dumps(targets)))
            if action == 'hold':
                await conn.execute("UPDATE task_steps SET status='pending',updated_at=now() WHERE id=$1", unit['id'])
        await _journal(conn, task_id, step_id,
                       f"Failure {row['id']}: {role} {failure.cls.value}/{failure.cause.value}, "
                       f"run {run_id}, streak {streak}, action {action}.")
        return {'action': action, 'failure': row, 'hold': hold}


async def record_success(*, task_id, step_id, run_id, role, target=None, outcome='success', pool=None, **_):
    if outcome == 'not_passed':
        return False
    if role not in ('worker', 'reviewer'):
        raise StoreError('invalid', 'Unknown execution role')
    target = target or {}
    pool = pool or get_pool()
    async with pool.acquire() as conn, conn.transaction():
        _, step = await _live_step(conn, task_id, step_id)
        if await conn.fetchval('SELECT EXISTS(SELECT 1 FROM step_failure_runs WHERE task_id=$1 AND run_id=$2)', task_id, run_id):
            return False
        await _current_run(conn, task_id, step, run_id, role, target)
        state = await _state(conn, task_id, step)
        await conn.execute("INSERT INTO step_failure_runs(task_id,run_id,step_key,outcome) VALUES($1,$2,$3,'success')",
                           task_id, run_id, state['step_key'])
        await conn.execute('UPDATE step_failure_state SET streak=0,epoch=epoch+1,updated_at=now() '
                           'WHERE task_id=$1 AND step_key=$2', task_id, state['step_key'])
        # A sibling seat's successful run resets the sequence, but does not discard a failed seat's hold.
        hold = _row(await conn.fetchrow('SELECT * FROM step_holds WHERE step_id=$1 FOR UPDATE', step_id))
        if hold:
            unit_id = target.get('unit_step_id') or step_id
            kept = [item for item in hold['targets'] if
                    (item['target'].get('unit_step_id') or step_id, item['target'].get('kind'),
                     item['target'].get('review_position')) != (unit_id, target.get('kind'), target.get('review_position'))]
            if not kept:
                await conn.execute('DELETE FROM step_holds WHERE step_id=$1', step_id)
            elif kept != hold['targets']:
                await conn.execute('UPDATE step_holds SET targets=$2::jsonb WHERE step_id=$1', step_id, json.dumps(kept))
        return True


async def list_failures(task_id, *, step_id=None, after_id=0, limit=50, pool=None):
    if not isinstance(after_id, int) or after_id < 0 or not isinstance(limit, int) or not 1 <= limit <= 201:
        raise StoreError('invalid', 'Invalid history cursor or page size')
    pool = pool or get_pool()
    rows = await pool.fetch('SELECT * FROM step_failures WHERE task_id=$1 AND id>$2 '
                            'AND ($3::text IS NULL OR step_identity=$3 OR step_id=$3) ORDER BY id LIMIT $4',
                            task_id, after_id, step_id, limit)
    return [_row(row) for row in rows]


async def continuous_failures(task_id, step_id, *, pool=None):
    pool = pool or get_pool()
    return [_row(row) for row in await pool.fetch(
        'SELECT f.* FROM step_failures f JOIN step_failure_state s ON s.task_id=f.task_id '
        'AND s.step_key=f.step_key AND s.epoch=f.epoch WHERE s.task_id=$1 AND s.step_id=$2 ORDER BY f.id',
        task_id, step_id)]


async def list_holds(task_id=None, *, pool=None):
    pool = pool or get_pool()
    return [_row(row) for row in await pool.fetch(
        'SELECT h.*,f.streak,f.target,f.result,f.run_id,f.role,f.step_key FROM step_holds h '
        'JOIN step_failures f ON f.id=h.failure_id WHERE ($1::text IS NULL OR h.task_id=$1) '
        'ORDER BY h.created_at,h.step_id', task_id)]


async def release_hold(task_id, step_id, failure_id, action, *, actor='operator', pool=None):
    if action not in ('retry', 'planner', 'stop'):
        raise StoreError('invalid', 'Unknown hold action')
    pool = pool or get_pool()
    async with pool.acquire() as conn, conn.transaction():
        task, _ = await _live_step(conn, task_id, step_id)
        row = _row(await conn.fetchrow('SELECT * FROM step_holds WHERE step_id=$1 FOR UPDATE', step_id))
        if row is None or row['failure_id'] != failure_id:
            raise StoreError('conflict', 'Failure hold changed; refresh before releasing it')
        payload = {'event': 'failure_hold_released', 'task_id': task_id, 'step_id': step_id,
                   'failure_id': failure_id, 'action': action, 'actor': actor, 'targets': row['targets'],
                   'target': row['targets'][0]['target'] if row['targets'] else {},
                   'result': row['targets'][0]['result'] if row['targets'] else {},
                   'failure': {'class': row['targets'][0].get('failure_class', row['kind']),
                               'cause': row['targets'][0].get('failure_cause', row['cause'])} if row['targets'] else {}}
        await conn.execute('INSERT INTO step_failure_releases(failure_id,task_id,step_identity,action,payload) '
                           'VALUES($1,$2,$3,$4,$5::jsonb) ON CONFLICT DO NOTHING',
                           failure_id, task_id, step_id, action, json.dumps(payload))
        await conn.execute('DELETE FROM step_holds WHERE step_id=$1 AND failure_id=$2', step_id, failure_id)
        await _journal(conn, task_id, step_id, f'Failure {failure_id}: {actor} released hold with {action}.')
        return {**payload, 'status': task['status']}


async def pending_releases(task_id=None, *, pool=None):
    pool = pool or get_pool()
    return [_row(row) for row in await pool.fetch(
        'SELECT * FROM step_failure_releases WHERE delivered_at IS NULL AND ($1::text IS NULL OR task_id=$1) '
        'ORDER BY failure_id', task_id)]


async def mark_delivered(failure_id, *, pool=None):
    await (pool or get_pool()).execute('UPDATE step_failure_releases SET delivered_at=now() WHERE failure_id=$1', failure_id)


async def clear_task_holds(task_id, *, reason='task ended', step_ids=None, pool=None):
    pool = pool or get_pool()
    async with pool.acquire() as conn, conn.transaction():
        await conn.fetchrow('SELECT id FROM tasks WHERE id=$1 FOR UPDATE', task_id)
        rows = await conn.fetch('DELETE FROM step_holds WHERE task_id=$1 '
                                'AND ($2::text[] IS NULL OR step_id=ANY($2)) RETURNING step_id,failure_id',
                                task_id, list(step_ids) if step_ids is not None else None)
        for row in rows:
            await _journal(conn, task_id, row['step_id'], f"Failure {row['failure_id']}: hold cleared ({reason}).")
        # Do not leave a committed release capable of resurrecting a stopped task.
        if step_ids is None:
            await conn.execute('UPDATE step_failure_releases SET delivered_at=now() WHERE task_id=$1 '
                               'AND delivered_at IS NULL', task_id)
        return [dict(row) for row in rows]


async def operator_reset(task_id, *, operator, actor, pool=None):
    if not operator:
        raise StoreError('forbidden', 'An operator-confirmed restart is required to reset failures')
    pool = pool or get_pool()
    async with pool.acquire() as conn, conn.transaction():
        await conn.fetchrow('SELECT id FROM tasks WHERE id=$1 FOR UPDATE', task_id)
        rows = await conn.fetch('SELECT step_id,step_key,streak FROM step_failure_state WHERE task_id=$1 FOR UPDATE', task_id)
        for row in rows:
            await _journal(conn, task_id, row['step_id'] or '',
                           f"Operator restart by {actor}: step {row['step_key']}, prior streak {row['streak']}; reset to 0.")
        await conn.execute('UPDATE step_failure_state SET streak=0,epoch=epoch+1,updated_at=now() WHERE task_id=$1', task_id)
        return [dict(row) for row in rows]


async def quota_deadline(task_id, max_seconds, *, now=None, pool=None):
    now = now or datetime.now(timezone.utc)
    return await (pool or get_pool()).fetchval(
        'INSERT INTO task_quota_waits(task_id,started_at,deadline) VALUES($1,$2,$3) '
        'ON CONFLICT(task_id) DO UPDATE SET deadline=LEAST(task_quota_waits.deadline,EXCLUDED.deadline) '
        'RETURNING deadline', task_id, now, now + timedelta(seconds=max_seconds))


async def stats(*, pool=None):
    pool = pool or get_pool()
    counts = await pool.fetchrow('SELECT count(*) AS holds, '
        'COALESCE(max(extract(epoch FROM now()-created_at)),0)::float8 AS longest_wait_seconds FROM step_holds')
    distribution = await pool.fetch('SELECT role,failure_class,streak,count(*) AS failures,'
        "count(*) FILTER (WHERE action='breaker') AS breakers FROM step_failures "
        "WHERE created_at>=date_trunc('day',now()) GROUP BY role,failure_class,streak ORDER BY 1,2,3")
    retries = await pool.fetchval("SELECT count(*) FROM step_failure_releases WHERE action='retry' AND payload->>'actor'='timer' AND created_at>=date_trunc('day',now())")
    return {**dict(counts), 'automatic_retries': retries, 'distribution': [dict(row) for row in distribution],
            'breakers_today': sum(row['breakers'] for row in distribution)}
