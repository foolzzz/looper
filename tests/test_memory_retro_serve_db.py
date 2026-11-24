"""Real isolated CLI server, with a protocol executable standing in for every model."""
from __future__ import annotations

import asyncio
import json
import socket
import sys
import time
from pathlib import Path

import asyncpg
import pytest

from evals.prompt_regression import runner
from tests.db_support import DSN, fresh_database_without_recording

pytestmark = pytest.mark.skipif(not DSN, reason="requires disposable LOOPER_TEST_DATABASE_URL")

_CODEX = r'''
import json, os, sys
from pathlib import Path
args=sys.argv[1:]
if '--version' in args:
    print('codex-cli 0.160.0');sys.exit(0)
if 'login' in args:
    print('Logged in using ChatGPT');sys.exit(0)
assert args[0]=='exec',args
text=sys.stdin.read()
with open(CAPTURE_PATH,'a') as stream:
    stream.write(json.dumps({'argv':args,'stdin':text})+'\n')
retro=text.startswith('[MEMORY RETRO]')
if retro:
    marker=Path(CAPTURE_PATH+'.retry')
    if not marker.exists():
        marker.write_text('first malformed reply')
        reply='{"lessons": "invalid"}'
    else:
        reply=json.dumps({'lessons':[{'statement':'Check the synthetic status before changing data.',
            'why':'The status check identified the cause.', 'keywords':['status'], 'evidence':['J1']}],
            'note':'One supported check.'})
elif text.startswith('[PLAN]'):
    answer={'steps':[{'id':'s'+str(i),'instruction':'Check synthetic item '+str(i), 'review':False,
        'acceptance_criteria':['Checked.'],'depends_on':[]} for i in range(1,4)]}
elif text.startswith('[DECISION]'):
    answer={'decision':'accept','reason':'Checked.','summary':'Done.',
        'final':{'delivered':'Checked.','how_to_use':'Read it.','verification':'Checked.','open_issues':'None.'}}
else:
    answer={'status':'completed','changed_files':{},'deleted_files':[],'summary':'Checked.',
        'learnings':[{'text':'Check the synthetic status before changing data.','scope':'project'}]}
if not retro:
    reply='Done.\n```json\n'+json.dumps(answer)+'\n```'
for event in ({'type':'thread.started','thread_id':'synthetic-thread'},
    {'type':'item.completed','item':{'id':'answer','type':'agent_message','text':reply}},
    {'type':'turn.completed','usage':{'input_tokens':10,'output_tokens':10}}):
    print(json.dumps(event),flush=True)
'''

_CLAUDE = r'''
import json, sys
from pathlib import Path
args=sys.argv[1:]
if '--version' in args:
    print('2.1.300 (Claude Code)');sys.exit(0)
if 'auth' in args or '--help' in args:
    print(json.dumps({'loggedIn':True,'authMethod':'claude.ai','subscriptionType':'max'}));sys.exit(0)
packet=sys.stdin.readline()
with open(CAPTURE_PATH,'a') as stream:
    stream.write(json.dumps({'argv':args,'stdin':packet})+'\n')
marker=Path(CAPTURE_PATH+'.retry')
if not marker.exists():
    marker.write_text('malformed initial attempt')
    reply='{"lessons":"invalid"}'
else:
    statement='Check the synthetic status before changing data.'
    if 'H1 M-' in packet:
        statement='Compare the synthetic response after checking its status.'
    reply=json.dumps({'lessons':[{'statement':statement,'why':'The status check identified the cause.',
        'keywords':['status'],'evidence':['J1']}],'note':'One supported check.'})
if 'Synthetic restart range' in packet:
    reply=json.dumps({'lessons':[],'note':'No additional lesson.'})
model=args[args.index('--model')+1]
for event in ({'type':'system','subtype':'init','session_id':'synthetic-retro','model':model,'tools':[],'mcp_servers':[]},
    {'type':'assistant','message':{'role':'assistant','content':[{'type':'text','text':reply}]}},
    {'type':'result','subtype':'success','is_error':False,'result':reply,'session_id':'synthetic-retro',
     'total_cost_usd':.004,'modelUsage':{model:{'costUSD':.004}}}):
    print(json.dumps(event),flush=True)
'''

_SERVE = '''
import sys
from dataclasses import replace
from pathlib import Path
from looper import ops_config
from looper.cli import main
root=Path(sys.argv[2])
path=root/ops_config.FILE
ops=ops_config.load(ops_config.Flags(runtime_root=str(root)))
text=ops_config.render(replace(ops,host='127.0.0.1',port=int(sys.argv[1])))
ops_config.write_private(path,text.replace('# host = \"127.0.0.1\"','host = \"127.0.0.1\"'))
assert int(sys.argv[1]) != 8767
sys.exit(main(['serve','--config',str(path),'--host','127.0.0.1','--port',sys.argv[1],'--runtime-root',str(root)]))
'''


def _port():
    for port in range(8900, 9000):
        with socket.socket() as sock:
            try:
                sock.bind(('127.0.0.1', port))
                return port
            except OSError:
                pass
    raise AssertionError('no disposable test port')


def test_real_serve_retro_survives_restart(monkeypatch, tmp_path):
    import looper
    assert Path(looper.__file__).resolve().parent.parent == Path(__file__).resolve().parent.parent
    assert sys.version_info[:2] >= (3, 11)
    monkeypatch.setattr(runner, '_SERVE', _SERVE)
    runtime, home, binaries, work = (tmp_path/name for name in ('runtime', 'home', 'bin', 'work'))
    for path in (home, binaries, work):
        path.mkdir()
    (home/'.codex').mkdir()
    (home/'.codex'/'auth.json').write_text('{}')
    capture = tmp_path/'capture.jsonl'
    for name in ('claude', 'codex', 'omp', 'pi', 'dsh', 'grok', 'cursor-agent', 'agent', 'lark-cli'):
        script = binaries/name
        script.write_text(f'#!{sys.executable}\n' + (_CODEX.replace('CAPTURE_PATH', repr(str(capture)))
            if name == 'codex' else _CLAUDE.replace('CAPTURE_PATH',repr(str(capture)))
            if name == 'claude' else 'import sys;sys.exit(76)\n'))
        script.chmod(0o755)
    env = {'CODEX_HOME': str(home/'.codex'), 'CLAUDE_CONFIG_DIR': str(home/'.claude'),
           'LOOPER_TELEGRAM_BOT_TOKEN': ''}
    with fresh_database_without_recording(str(DSN)) as (url, _):
        async def query(sql, *args):
            conn = await asyncpg.connect(url)
            try:
                return [dict(r) for r in await conn.fetch(sql, *args)]
            finally:
                await conn.close()

        def rows(sql, *args):
            return asyncio.run(query(sql, *args))

        common = dict(database_url=url, runtime_root=runtime, bin_dir=binaries, home=home,
                      notes_root=home/'notes', env=env, port=_port(), log_path=tmp_path/'serve.log')
        with runner.running_server(**common) as api:
            assert api.base_url.endswith(':'+str(common['port'])) and common['port'] != 8767
            import tomllib
            ops = tomllib.loads((runtime/'ops'/'server.toml').read_text())
            assert ops['server']['port'] == common['port'] and ops['database']['url'] == url
            registry = api.post('/api/v1/registry/view', {})['result']
            runtime_spec = next(r for r in registry['runtimes'] if r['type'] == 'codex')
            models = api.post('/api/v1/ops/models/list', {})['result']['models']
            model = next(m for m in models if m['provider_id'] == 'prov-codex-login')
            profile = api.post('/api/v1/profiles/create', {'name': 'Synthetic protocol profile',
                'runtime_id': runtime_spec['runtime_id'], 'model_id': model['model_id']})['result']['profile']
            api.post('/api/v1/agent_settings/update_system', {'values': {'profile_id': profile['profile_id']}})
            api.post('/api/v1/agent_settings/update_role', {'role': 'memory_retro',
                'values': {'profile_id': 'agent-claude-code-sonnet-5-5'}})
            api.post('/api/v1/memory_lessons/settings_put', {'enabled': True, 'daily_budget_usd': 2})
            project = api.post('/api/v1/project_admin/create', {'name': 'Synthetic retro',
                'work_dir': str(work), 'memory_lessons': 'on', 'task_workspaces': 'in_place',
                'step_workspaces': 'shared', 'operator_notes': 'off', 'vault': 'off', 'agent_room': 'off',
                'distill_lessons': 'draft'})['result']['project']

            def finish():
                task = api.post('/api/v1/tasks/create', {'project_id': project['project_id'],
                    'requirement': 'Check three synthetic items.', 'work_dir': str(work)})['result']
                task_id = task['task_id']
                api.post('/api/v1/ops/task_control/start', {'task_id': task_id})
                deadline = time.monotonic()+50
                while True:
                    status = api.post('/api/v1/tasks/detail', {'task_id': task_id})['result']['status']
                    if status in ('completed', 'failed'):
                        break
                    assert time.monotonic() < deadline, status
                    time.sleep(.1)
                assert status == 'completed'
                while True:
                    runs = rows('SELECT * FROM memory_retro_runs WHERE task_id=$1', task_id)
                    if runs and runs[0]['state'] in ('done', 'failed', 'skipped'):
                        break
                    assert time.monotonic() < deadline, runs
                    time.sleep(.1)
                assert runs[0]['state'] == 'done', runs
                return task_id

            first = finish()
            held = api.post('/api/v1/memory/lessons_list', {'project': project['project_id']})['result']['lessons']
            assert len(held) == 1 and held[0]['candidate']['state'] == 'held'
            assert rows('SELECT attempts FROM memory_retro_runs WHERE task_id=$1', first)[0]['attempts'] == 2
            asked = api.post('/api/v1/memory_lessons/settings_put', {
                'enabled': True, 'auto_activate': True, 'daily_budget_usd': 2})
            assert asked['status'] == 'needs_confirmation'
            api.post('/api/v1/confirm', {'confirmation_id': asked['confirmation_id']})
            second = finish()
            active = api.post('/api/v1/memory/lessons_list', {'project': project['project_id']})['result']['lessons']
            assert any(x['candidate']['state'] == 'in_effect' for x in active)
            assert rows("SELECT count(*) AS n FROM project_document_drafts WHERE task_id=ANY($1::text[])",
                        [first, second])[0]['n'] == 0
            assert rows("SELECT count(*) AS n FROM activity WHERE worker='memory_retro' AND task_id=ANY($1::text[])",
                        [first, second])[0]['n'] == 2
            spend_before = rows('SELECT * FROM memory_retro_spend ORDER BY attempt_token')
            assert len(spend_before) == 3
        async def interrupted():
            from datetime import datetime, timedelta, timezone
            from looper.agents.memory_lessons_model import parse_settings
            from looper.db import memory_retro

            conn = await asyncpg.connect(url)
            try:
                now = datetime.now(timezone.utc)
                await conn.execute("INSERT INTO tasks(id,requirement,project_id,status,completed_at) "
                    "VALUES('task-restart-synthetic','Synthetic restart range',$1,'completed',$2)",
                    project['project_id'], now)
                for index in range(3):
                    await conn.execute("INSERT INTO task_journal(task_id,step_id,kind,summary) "
                        "VALUES('task-restart-synthetic',$1,'step_failed','Synthetic interrupted evidence')", str(index))
                settings = parse_settings({'enabled': True, 'auto_activate': True, 'daily_budget_usd': 2})
                await memory_retro.enqueue_finished(conn, 'task-restart-synthetic', 'completed', settings=settings, now=now)
                claimed = await memory_retro.claim_next(conn, settings=settings, now=now)
                assert claimed['task_id'] == 'task-restart-synthetic' and claimed['attempts'] == 1
                await conn.execute('UPDATE memory_retro_runs SET lease_until=$1 WHERE id=$2',
                                   now-timedelta(seconds=1), claimed['id'])
                return claimed['lease_token']
            finally:
                await conn.close()
        abandoned = asyncio.run(interrupted())
        common['log_path'] = tmp_path/'restart.log'
        with runner.running_server(**common) as api:
            assert api.base_url.endswith(':'+str(common['port']))
            deadline = time.monotonic()+25
            while True:
                recovered = rows("SELECT state,attempts FROM memory_retro_runs WHERE task_id='task-restart-synthetic'")
                if recovered[0]['state'] == 'done':
                    break
                assert time.monotonic() < deadline, recovered
                time.sleep(.1)
            assert recovered[0]['attempts'] == 2
            assert len(api.post('/api/v1/memory/lessons_list', {'project': project['project_id']})['result']['lessons']) == 2
            assert rows('SELECT * FROM memory_retro_spend WHERE run_id IN '
                        '(SELECT id FROM memory_retro_runs WHERE task_id=ANY($1::text[])) ORDER BY attempt_token',
                        [first, second]) == spend_before
            assert rows('SELECT state,reserved_usd FROM memory_retro_spend WHERE attempt_token=$1',
                        abandoned)[0]['state'] == 'unknown'
            assert len(rows('SELECT * FROM memory_retro_spend')) == 5
            assert len(rows('SELECT * FROM memory_retro_runs')) == 3
            for table in ('memory_injections', 'memory_injection_items', 'memory_weekly_cards', 'task_lesson_snapshots'):
                assert rows(f'SELECT count(*) AS n FROM {table}')[0]['n'] == 0
        captured = [json.loads(line) for line in capture.read_text().splitlines()]
        assert not any(x['stdin'].startswith('[LESSONS]') for x in captured)
