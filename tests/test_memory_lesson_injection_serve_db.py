"""Isolated CLI-server acceptance with a protocol executable and journal-backed lessons."""
from __future__ import annotations

import asyncio
import hashlib
import json
import socket
import stat
import sys
import time
import tomllib
from urllib.parse import urlparse

import asyncpg
import pytest

from evals.prompt_regression import runner
from looper import ops_config
from looper.agents.memory_model import fingerprint, format_id
from tests.db_support import DSN, fresh_database_without_recording

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


_CODEX = r'''
import json, os, sys, time, tomllib, uuid
from pathlib import Path
args=sys.argv[1:]
if '--version' in args:
    print('codex-cli 0.160.0');sys.exit(0)
if 'login' in args:
    print('Logged in using ChatGPT');sys.exit(0)
assert args[0]=='exec',args
text=sys.stdin.read()
codex_home=Path(os.environ['CODEX_HOME'])
system=(codex_home/'AGENTS.md').read_text()
config=tomllib.loads((codex_home/'config.toml').read_text())
if config.get('model_instructions_file'):
    system=Path(config['model_instructions_file']).read_text()+system
options=json.loads(Path(OPTIONS_PATH).read_text())
with open(CAPTURE_PATH,'a') as stream:
    stream.write(json.dumps({'stdin':text,'system':system})+'\n')
if '[PLAN]' in text or '[RESUME]' in text:
    steps=[{'id':'s1','instruction':'SYNTHETIC_HOLD_POINT' if 'CASE_REVOKE' in system+text else 'Check synthetic evidence.',
            'review':False,'acceptance_criteria':['Synthetic evidence checked.'],'depends_on':[]}]
    if 'CASE_REVOKE' in system+text:
        steps.append({'id':'s2','instruction':'SYNTHETIC_SECOND_CHECK','review':False,
                      'acceptance_criteria':['Synthetic evidence checked again.'],'depends_on':['s1']})
    answer={'steps':steps}
elif '[LESSONS]' in text:
    answer={'lessons':'# Lessons\n\nKeep synthetic evidence.'}
elif '[DECISION]' in text:
    answer={'decision':'accept','reason':'Checked.','summary':'Done.',
            'final':{'delivered':'Checked.','how_to_use':'Read the evidence.',
                     'verification':'Protocol stand-in checked.','open_issues':'None.'}}
else:
    if 'SYNTHETIC_HOLD_POINT' in system:
        Path(GATE_PATH+'.ready').write_text('waiting')
        until=time.monotonic()+30
        while not Path(GATE_PATH+'.release').exists():
            assert time.monotonic()<until,'synthetic gate timed out'
            time.sleep(.05)
    answer={'status':'completed','changed_files':{},'deleted_files':[],'summary':'Synthetic evidence checked.'}
    if 'lessons_used (optional)' in system:
        if 'CASE_WRONG' in system:
            answer['lessons_wrong']=[options['wrong']]
        else:
            answer['lessons_used']=[options['good']]
session=args[args.index('resume')+1] if 'resume' in args else 'synthetic-'+uuid.uuid4().hex
for event in ({'type':'thread.started','thread_id':session},
    {'type':'item.completed','item':{'id':'answer','type':'agent_message','text':'```json\n'+json.dumps(answer)+'\n```'}},
    {'type':'turn.completed','usage':{'input_tokens':10,'output_tokens':10}}):
    print(json.dumps(event),flush=True)
'''


def _available_port():
    for port in range(8900, 9000):
        with socket.socket() as sock:
            try:
                sock.bind(('127.0.0.1', port))
                return port
            except OSError:
                continue
    raise AssertionError('no isolated test port is available')


def _sql(url, statement, *args, rows=False):
    async def read():
        conn = await asyncpg.connect(url)
        try:
            return await (conn.fetch(statement, *args) if rows else conn.fetchval(statement, *args))
        finally:
            await conn.close()
    return asyncio.run(read())


def _seed(url, project, statement, *, state='in_effect', source='agent', expired=False):
    async def write():
        conn = await asyncpg.connect(url)
        try:
            async with conn.transaction():
                await conn.execute('INSERT INTO projects(id,name) VALUES ($1,$1) ON CONFLICT DO NOTHING', project)
                source_task = 'source-' + fingerprint('lesson', project + statement)[:16]
                await conn.execute("INSERT INTO tasks(id,requirement,project_id,status) VALUES ($1,'Synthetic evidence',$2,'completed')",
                                   source_task, project)
                journal = await conn.fetchval("INSERT INTO task_journal(task_id,step_id,kind,summary) "
                    "VALUES ($1,'evidence','step_accepted','Synthetic verified evidence') RETURNING id", source_task)
                number = await conn.fetchval("INSERT INTO memory_candidates(kind,source_kind,source_class,destination,"
                    "state,reason,statement,why,fingerprint,project_id,valid_until) "
                    "VALUES ('lesson','task',$1,'looper',$2,$3,$4,'Synthetic journal evidence',$5,$6,"
                    "now()+CASE WHEN $7 THEN interval '-1 day' ELSE interval '90 days' END) RETURNING id",
                    source, state, 'shadow' if state == 'held' else None, statement,
                    fingerprint('lesson', statement), project, expired)
                await conn.execute("INSERT INTO memory_candidate_sources(candidate_id,kind,journal_id) VALUES ($1,'journal',$2)",
                                   number, journal)
                if state == 'in_effect':
                    await conn.execute("INSERT INTO memory_candidate_events(candidate_id,action,actor_kind,via) "
                                       "VALUES ($1,'activate','system','system')", number)
                return format_id(number)
        finally:
            await conn.close()
    return asyncio.run(write())


def _wait(predicate, *, timeout=40):
    deadline = time.monotonic() + timeout
    while True:
        if result := predicate():
            return result
        assert time.monotonic() < deadline, 'isolated task did not reach its expected state'
        time.sleep(.1)


def _task(api, project, work, requirement):
    task_id = api.post('/api/v1/tasks/create', {'project_id': project, 'requirement': requirement,
                                              'work_dir': str(work)})['result']['task_id']
    api.post('/api/v1/ops/task_control/start', {'task_id': task_id})
    return task_id


def _completed(api, task_id):
    def terminal():
        detail = api.post('/api/v1/tasks/detail', {'task_id': task_id})['result']
        if detail['status'] in ('completed', 'failed', 'stopped', 'cancelled'):
            assert detail['status'] == 'completed', {key: detail[key] for key in (
                'status', 'error', 'fail_reason', 'final_summary', 'summary') if key in detail}
            return detail
    return _wait(terminal)


def _view(api, task_id, **page):
    return api.post('/api/v1/memory/task_lessons', {'task_id': task_id, **page})['result']


def test_serve_delivers_cites_holds_and_revokes(monkeypatch, tmp_path):
    # Invoke the public CLI, loading the ops file the harness writes for this port.
    monkeypatch.setattr(runner, '_SERVE', 'import sys\nfrom looper.cli import main\n'
        'sys.exit(main(["serve","--host","127.0.0.1","--port",sys.argv[1],"--runtime-root",sys.argv[2]]))\n')
    runtime, home, binaries, work = (tmp_path / name for name in ('runtime', 'home', 'bin', 'work'))
    for path in (home, binaries, work):
        path.mkdir()
    (home / '.codex').mkdir()
    (home / '.codex' / 'auth.json').write_text('{}')
    capture, options, gate = (tmp_path / name for name in ('capture.jsonl', 'options.json', 'gate'))
    from pathlib import Path
    from looper.agents.runtimes import ADAPTERS

    for name in sorted({'codex', *(Path(adapter.default_executable).name for adapter in ADAPTERS.values()
                                  if adapter.default_executable and Path(adapter.default_executable).name)}):
        executable = binaries / name
        source = (_CODEX.replace('CAPTURE_PATH', repr(str(capture))).replace('OPTIONS_PATH', repr(str(options)))
                  .replace('GATE_PATH', repr(str(gate)))) if name == 'codex' else 'import sys;sys.exit(76)\n'
        executable.write_text(f'#!{sys.executable}\n' + source)
        executable.chmod(0o755)
    env = {'CODEX_HOME': str(home / '.codex'), 'CLAUDE_CONFIG_DIR': str(home / '.claude'),
           'LOOPER_TELEGRAM_BOT_TOKEN': ''}
    with fresh_database_without_recording(str(DSN)) as (url, _):
        good = _seed(url, 'project-lessons', 'Keep synthetic evidence')
        wrong = _seed(url, 'project-lessons', 'Use the obsolete build command')
        revoked = _seed(url, 'project-lessons', 'Run the old verification command')
        excluded = [_seed(url, 'project-lessons', 'Held evidence', state='held'),
                    _seed(url, 'project-lessons', 'External evidence', state='held', source='external'),
                    _seed(url, 'project-lessons', 'Expired evidence', expired=True),
                    _seed(url, 'project-other', 'Other project evidence')]
        _sql(url, "INSERT INTO projects(id,name) VALUES ('project-empty','Empty project') RETURNING id")
        options.write_text(json.dumps({'good': good, 'wrong': wrong}))
        port = _available_port()
        assert port != 8767
        common = dict(database_url=url, runtime_root=runtime, bin_dir=binaries, home=home,
                      notes_root=home / 'notes', env=env, port=port, log_path=tmp_path / 'serve.log')
        with runner.running_server(**common) as api:
            assert urlparse(api.base_url).port == port != 8767
            ops_path = runtime / ops_config.FILE
            assert tomllib.loads(ops_path.read_text())['server']['port'] == port
            assert stat.S_IMODE(ops_path.stat().st_mode) == 0o600
            assert stat.S_IMODE(ops_path.parent.stat().st_mode) == 0o700
            registry = api.post('/api/v1/registry/view', {})['result']
            codex = next(row for row in registry['runtimes'] if row['type'] == 'codex')
            models = api.post('/api/v1/ops/models/list', {})['result']['models']
            model = next(row for row in models if row['provider_id'] == 'prov-codex-login')
            profile = api.post('/api/v1/profiles/create', {'name': 'Synthetic protocol profile',
                'runtime_id': codex['runtime_id'], 'model_id': model['model_id']})['result']['profile']
            api.post('/api/v1/agent_settings/update_system', {'values': {'profile_id': profile['profile_id']}})
            api.post('/api/v1/memory_lessons/settings_put', {'enabled': True})
            for project in ('project-lessons', 'project-empty'):
                api.post('/api/v1/project_admin/update', {'project_id': project, 'fields': {
                    'memory_lessons': 'on', 'work_dir': str(work), 'task_workspaces': 'in_place',
                    'step_workspaces': 'shared', 'operator_notes': 'off', 'vault': 'off', 'agent_room': 'off',
                    'distill_lessons': 'off'}})
            first = _task(api, 'project-lessons', work, 'CASE_USE: check synthetic evidence.')
            _completed(api, first)
            first_view = _view(api, first)
            delivered = [row for row in first_view['injections'] if row['state'] == 'delivered']
            assert {row['agent_kind'] for row in delivered} == {'planner', 'worker'}
            assert len(delivered) == 2
            assert len({row['block_sha256'] for row in delivered}) == 1
            for row in delivered:
                assert row['candidate_ids'] == [revoked, wrong, good]
                assert row['block_sha256'] == hashlib.sha256(row['block_text'].encode()).hexdigest()
                assert row['block_chars'] == len(row['block_text'])
                assert all(value not in row['candidate_ids'] for value in excluded)
            worker_delivery = next(row for row in delivered if row['agent_kind'] == 'worker')
            assert next(item for item in worker_delivery['items'] if item['candidate_id'] == good)['cited']
            captures = [json.loads(line) for line in capture.read_text().splitlines()]
            block = delivered[0]['block_text']
            assert any('[PLAN]' in row['stdin'] and block in row['stdin'] for row in captures)
            assert any(row['stdin'].startswith('Start your step') and block in row['system'] for row in captures)
            assert all('Lessons from earlier tasks' not in row['system'] for row in captures if '[PLAN]' in row['stdin'])

            changing = _task(api, 'project-lessons', work, 'CASE_REVOKE: check evidence twice.')
            _wait(lambda: (tmp_path / 'gate.ready').exists())
            before = _view(api, changing)
            frozen = [row['line_text'] for row in before['snapshot']]
            later = _seed(url, 'project-lessons', 'A later lesson must not join an existing task')
            api.post('/api/v1/memory/renew', {'candidate': good})
            api.post('/api/v1/memory/revoke', {'candidate': revoked, 'reason': 'Synthetic evidence contradicted it.'})
            (tmp_path / 'gate.release').write_text('continue')
            _completed(api, changing)
            after = _view(api, changing)
            assert [row['line_text'] for row in after['snapshot']] == frozen
            workers = sorted((row for row in after['injections'] if row['agent_kind'] == 'worker'), key=lambda row: row['id'])
            assert len(workers) == 2
            assert revoked in workers[0]['candidate_ids']
            assert revoked not in workers[1]['candidate_ids']
            assert all(later not in row['candidate_ids'] for row in after['injections'])
            notice = f'Lesson {revoked} was revoked by the operator: do not rely on it.'
            captures = [json.loads(line) for line in capture.read_text().splitlines()]
            assert sum(row['stdin'].startswith(notice) for row in captures) == 1

            for label in ('one', 'two'):
                task_id = _task(api, 'project-lessons', work, f'CASE_WRONG {label}: verify obsolete evidence.')
                _completed(api, task_id)
            page = api.post('/api/v1/memory/lessons_list', {'project': 'project-lessons'})['result']
            wrong_view = next(row for row in page['lessons'] if row['candidate']['id'] == wrong)
            assert wrong_view['candidate']['state'] == 'held'
            assert wrong_view['candidate']['reason'] == 'flagged_wrong'
            assert wrong_view['stats']['wrong_tasks'] == 2
            assert _sql(url, 'SELECT count(*) FROM memory_tombstones WHERE fingerprint=$1',
                        fingerprint('lesson', 'Use the obsolete build command')) == 0
            assert _sql(url, "SELECT count(*) FROM memory_candidate_events WHERE candidate_id=$1 AND action='hold'",
                        int(wrong[2:])) == 1
            hold = _sql(url, "SELECT actor_kind,via FROM memory_candidate_events WHERE candidate_id=$1 AND action='hold'",
                        int(wrong[2:]), rows=True)
            assert [(row['actor_kind'], row['via']) for row in hold] == [('system', 'system')]
            for row in page['lessons']:
                candidate = int(row['candidate']['id'][2:])
                assert row['stats']['injected'] == _sql(url, 'SELECT count(*) FROM memory_injection_items WHERE candidate_id=$1', candidate)
                assert row['stats']['cited'] == _sql(url, 'SELECT count(*) FROM memory_injection_items WHERE candidate_id=$1 AND cited', candidate)
            empty = _task(api, 'project-empty', work, 'CASE_EMPTY: check synthetic evidence.')
            _completed(api, empty)
            assert _view(api, empty) == {'initialized': True, 'snapshot': [], 'injections': [], 'more': False}
            _seed(url, 'project-empty', 'This later lesson cannot fill an empty snapshot')
            frozen_ids = [row['candidate_id'] for row in after['snapshot']]
        common['log_path'] = tmp_path / 'restart.log'
        with runner.running_server(**common) as api:
            assert urlparse(api.base_url).port == port != 8767
            assert [row['candidate_id'] for row in _view(api, changing)['snapshot']] == frozen_ids
            assert _view(api, empty) == {'initialized': True, 'snapshot': [], 'injections': [], 'more': False}
            # The original body remains auditable after its record is erased.
            log_id, record_id = worker_delivery['id'], worker_delivery['record_id']
            _sql(url, 'DELETE FROM llm_records WHERE id=$1 RETURNING id', record_id)
            assert _sql(url, 'SELECT record_id IS NULL FROM memory_injections WHERE id=$1', log_id)
            assert _sql(url, 'SELECT block_text FROM memory_injections WHERE id=$1', log_id) == block
