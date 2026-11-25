"""Task residents on a disposable database and a scripted stream-json process."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from looper.agents import outbound_hook, task_conversations_settings as settings, task_manager_safety as safety
from looper.agents import task_manager_settings
from looper.agents.agent_settings import AgentSettings
from looper.agents.profile_registry import builtin_resolved_profile
from looper.agents.tiers import task_manager as tm
from looper.db import projects, tasks
from tests.db_support import run_db
from tests.fake_claude import fake_claude


@pytest.fixture
def setup_manager(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "_enabled", True)
    monkeypatch.setattr(outbound_hook, "known_enabled", lambda: False)
    monkeypatch.setattr(task_manager_settings, "load_task_manager_settings", AsyncMock(return_value=AgentSettings(profile=builtin_resolved_profile())))
    monkeypatch.setattr(safety, "gate", AsyncMock(return_value=None))
    executable = fake_claude(tmp_path / "claude", '''
import sys, json, os, time
if '--version' in sys.argv:
    print('2.1.332 (Claude Code)'); sys.exit()
session = sys.argv[sys.argv.index('--resume')+1] if '--resume' in sys.argv else 'task-session'
for line in sys.stdin:
    data = json.loads(line)
    if data.get('type') == 'control_request':
        print(json.dumps({'type':'control_response','response':{'subtype':'success','request_id':data['request_id']}}), flush=True)
        print(json.dumps({'type':'result','subtype':'success','result':'stopped','session_id':session,'total_cost_usd':0.1}), flush=True)
        continue
    text = data.get('message',{}).get('content','')
    if text == 'fail-resume' and '--resume' in sys.argv:
        sys.exit(2)
    print(json.dumps({'type':'system','subtype':'init','session_id':session}), flush=True)
    if text == 'wait':
        continue
    if text == 'write':
        open('changed.txt','w').write('changed')
    print(json.dumps({'type':'stream_event','event':{'delta':{'type':'text_delta','text':'one'}}}), flush=True)
    print(json.dumps({'type':'stream_event','event':{'delta':{'type':'text_delta','text':'two'}}}), flush=True)
    print(json.dumps({'type':'assistant','message':{'content':[{'type':'tool_use','id':'call','name':'Read','input':{'file_path':'sample'}}]}}), flush=True)
    print(json.dumps({'type':'user','message':{'content':[{'type':'tool_result','tool_use_id':'call','content':'contents'}]}}), flush=True)
    print(json.dumps({'type':'result','subtype':'success','result':'onetwo','session_id':session,'total_cost_usd':0.1}), flush=True)
''')

    async def create(pool, task_id="task-one", status="draft", work_dir=None, workspace_mode="in_place"):
        project = await projects.create_project(name=f"Project {task_id}", work_dir=str(work_dir or tmp_path), pool=pool)
        await tasks.create_task(task_id=task_id, requirement="Implement a feature", project_id=project["id"],
                                status=status, work_dir=str(work_dir or tmp_path), workspace_mode=workspace_mode,
                                workspace_dir=str(work_dir or tmp_path), pool=pool)
        return task_id

    return executable, create


def test_pool_constructs_without_a_process(monkeypatch, tmp_path, setup_manager):
    async def check(db):
        executable, create = setup_manager
        task_id = await create(db)
        pool = tm.TaskManagerPool(tmp_path / 'runtime', executable, max_resident=lambda: 2)
        try:
            settings.set_enabled(False)
            agent = pool.agent(task_id)
            with pytest.raises(tm.Refused) as refused:
                await agent.prepare()
            assert refused.value.reason is tm.RefuseReason.WORK_MODE_OFF
            assert not agent.resident.started
            assert tm.current_pool() is pool
        finally:
            await pool.shutdown()
        assert tm.current_pool() is None
    run_db(check, monkeypatch)


def test_prepare_operator_and_read_only_commands(monkeypatch, tmp_path, setup_manager):
    async def check(db):
        executable, create = setup_manager
        task_id = await create(db)
        pool = tm.TaskManagerPool(tmp_path / 'runtime', executable, max_resident=lambda: 2)
        try:
            agent = pool.agent(task_id)
            _, _, launch, _ = await agent.prepare()
            direct = agent.resident.command
            assert '--setting-sources' not in direct
            assert '--append-system-prompt-file' in direct
            await tasks.update_task_status(task_id, 'pending', pool=db)
            agent.session_id = 'task-session'
            await agent.prepare()
            readonly = agent.resident.command
            assert '--setting-sources' in readonly
            assert 'Bash,Edit,Write,NotebookEdit,EnterWorktree' in readonly
            assert readonly[-2:] == ['--resume', 'task-session']
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch)
