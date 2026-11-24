"""Lesson settings persist across real CLI-server restarts without starting processing."""
from __future__ import annotations

import asyncio
import json
import socket
import sys
import time

import asyncpg
import httpx
import pytest

from evals.prompt_regression import runner
from tests.db_support import DSN, fresh_database_without_recording
from tests.memory_lessons_support import lesson
from tests import memory_support

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

# Protocol-speaking executable, never a real model. It records what the normal task receives.
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
    stream.write(json.dumps({'argv':args,'stdin':text,'system':(Path(os.environ['CODEX_HOME'])/'AGENTS.md').read_text()})+'\n')
if text.startswith('[PLAN]'):
    answer={'steps':[{'id':'s1','instruction':'Check synthetic data.','review':False,
        'acceptance_criteria':['Synthetic data checked.'],'depends_on':[]}]}
elif text.startswith('[LESSONS]'):
    answer={'lessons':'# Lessons\n\nKeep synthetic evidence.'}
elif text.startswith('[DECISION]'):
    answer={'decision':'accept','reason':'Checked.','summary':'Done.',
        'final':{'delivered':'Checked.','how_to_use':'Read it.','verification':'Checked.','open_issues':'None.'}}
else:
    answer={'status':'completed','changed_files':{},'deleted_files':[],'summary':'Checked.','learnings':[{'text':'Keep synthetic evidence.','scope':'project'}]}
for event in ({'type':'thread.started','thread_id':'synthetic-thread'},
    {'type':'item.completed','item':{'id':'answer','type':'agent_message','text':'Done.\n```json\n'+json.dumps(answer)+'\n```'}},
    {'type':'turn.completed','usage':{'input_tokens':10,'output_tokens':10}}):
    print(json.dumps(event),flush=True)
'''


def _port():
    for port in range(8900,9000):
        with socket.socket() as sock:
            try:
                sock.bind(('127.0.0.1',port))
                return port
            except OSError:
                pass
    raise AssertionError('no disposable test port')


def test_lesson_foundation_through_cli_serve_and_restart(monkeypatch,tmp_path):
    monkeypatch.setattr(runner,'_SERVE','import sys\nfrom looper.cli import main\n'
        'sys.exit(main(["serve","--host","127.0.0.1","--port",sys.argv[1],"--runtime-root",sys.argv[2]]))\n')
    runtime,home,binaries,work=(tmp_path/name for name in ('runtime','home','bin','work'))
    for path in (home,binaries,work):
        path.mkdir()
    (home/'.codex').mkdir()
    (home/'.codex'/'auth.json').write_text('{}')
    capture=tmp_path/'capture.jsonl'
    for name in ('claude','codex','omp','pi','dsh','grok','cursor-agent','agent','lark-cli','ttadk','coco'):
        script=binaries/name
        script.write_text(f'#!{sys.executable}\n'+(_CODEX.replace('CAPTURE_PATH',repr(str(capture))) if name=='codex' else 'import sys;sys.exit(76)\n'))
        script.chmod(0o755)
    env={'CODEX_HOME':str(home/'.codex'),'CLAUDE_CONFIG_DIR':str(home/'.claude'),
         'LOOPER_TELEGRAM_BOT_TOKEN':''}
    with fresh_database_without_recording(str(DSN)) as (url,_):
        async def seed():
            conn=await asyncpg.connect(url)
            try:
                first=await lesson(conn,'project-smoke','First lesson')
                second=await lesson(conn,'project-smoke','Second lesson')
                await memory_support.candidate(conn)
                return first,second
            finally:
                await conn.close()
        first,second=asyncio.run(seed())
        common=dict(database_url=url,runtime_root=runtime,bin_dir=binaries,home=home,notes_root=home/'notes',env=env,
                    port=_port(),log_path=tmp_path/'serve.log')
        with runner.running_server(**common) as api:
            registry=api.post('/api/v1/registry/view',{})['result']
            runtime_spec=next(r for r in registry['runtimes'] if r['type']=='codex')
            models=api.post('/api/v1/ops/models/list',{})['result']['models']
            model=next(m for m in models if m['provider_id']=='prov-codex-login')
            profile=api.post('/api/v1/profiles/create',{'name':'Synthetic Codex','runtime_id':runtime_spec['runtime_id'],
                'model_id':model['model_id']})['result']['profile']
            api.post('/api/v1/agent_settings/update_system',{'values':{'profile_id':profile['profile_id']}})
            assert api.post('/api/v1/memory_lessons/settings_put',{'enabled':True})['result']['enabled']
            asked=api.post('/api/v1/memory_lessons/settings_put',{'enabled':True,'auto_activate':True})
            assert asked['status']=='needs_confirmation'
            assert not api.post('/api/v1/memory_lessons/settings_get',{})['result']['settings']['auto_activate']
            assert api.post('/api/v1/confirm',{'confirmation_id':asked['confirmation_id']})['result']['settings']['auto_activate']
            for mode in ('on','off',None):
                result=api.post('/api/v1/project_admin/update',{'project_id':'project-smoke','fields':{'memory_lessons':mode}})['result']['project']
                assert result.get('memory_lessons')==mode
            api.post('/api/v1/project_admin/update',{'project_id':'project-smoke','fields':{'memory_lessons':'on','work_dir':str(work),'task_workspaces':'in_place','step_workspaces':'shared','operator_notes':'off','vault':'off','agent_room':'off','distill_lessons':'draft'}})
            page=api.post('/api/v1/memory/lessons_list',{'project':'project-smoke','limit':1})['result']
            assert page['more'] and page['lessons'][0]['candidate']['id']==second
            assert page['lessons'][0]['stats']['injected']==0
            page=api.post('/api/v1/memory/lessons_list',{'project':'project-smoke','before':second})['result']
            assert not page['more'] and [x['candidate']['id'] for x in page['lessons']]==[first]
            token=api.post('/api/v1/clients/create',{'name':'Synthetic client'})['result']['token']
            for path in ('memory/lessons_list','memory_lessons/settings_get','memory_lessons/settings_put'):
                response=httpx.post(api.base_url+'/api/v1/'+path,json={},headers={'Authorization':'Bearer '+token})
                assert response.status_code==403
            # A normal task still follows the existing Planner/Worker path while the settings are enabled.
            task=api.post('/api/v1/tasks/create',{'project_id':'project-smoke','requirement':'Check synthetic data.',
                'work_dir':str(work)})['result']
            task_id=task['task_id']
            api.post('/api/v1/ops/task_control/start',{'task_id':task_id})
            deadline=time.monotonic()+35
            while True:
                state=api.post('/api/v1/tasks/detail',{'task_id':task_id})['result']['status']
                if state in ('completed','failed'):
                    break
                assert time.monotonic()<deadline,state
                time.sleep(.1)
            assert state=='completed',api.post('/api/v1/tasks/detail',{'task_id':task_id})['result']
        common['log_path']=tmp_path/'restart.log'
        with runner.running_server(**common) as api:
            assert api.post('/api/v1/memory_lessons/settings_get',{})['result']['settings']['auto_activate']
            assert len(api.post('/api/v1/memory/lessons_list',{'project':'project-smoke'})['result']['lessons'])==2
        async def verify():
            conn=await asyncpg.connect(url)
            try:
                assert await conn.fetchval("SELECT count(*) FROM project_document_drafts WHERE task_id=$1",task_id)==2
                for table in ('memory_retro_runs','memory_injections','memory_weekly_cards','task_lesson_snapshots'):
                    assert await conn.fetchval(f'SELECT count(*) FROM {table}')==0
            finally:
                await conn.close()
        asyncio.run(verify())
        records=[json.loads(line) for line in capture.read_text().splitlines()]
        assert any(row['stdin'].startswith('[PLAN]') for row in records)
        assert any(row['stdin'].startswith('[LESSONS]') for row in records)
        assert all('Lessons from earlier tasks' not in row['system'] for row in records)
        assert all('memory_retro' not in row['stdin'] for row in records)
