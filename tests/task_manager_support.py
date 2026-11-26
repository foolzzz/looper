"""Scripted native CLI and collecting sink for task Manager lifecycle checks."""
from __future__ import annotations

import json
from dataclasses import replace
from unittest.mock import AsyncMock

from looper.agents import outbound_hook, task_conversations_settings, task_manager_safety, task_manager_settings
from looper.agents.agent_settings import AgentSettings
from looper.agents.profile_registry import builtin_resolved_profile
from looper.db import projects, tasks
from tests.db_support import until
from tests.fake_claude import fake_claude


class Sink:
    def __init__(self):
        self.deltas, self.tools, self.events, self.finals = [], [], [], []

    async def delta(self, text):
        self.deltas.append(text)

    async def tool(self, record):
        self.tools.append(record)

    async def event(self, kind, payload):
        self.events.append((kind.value, dict(payload)))

    async def final(self, text, outcome):
        self.finals.append((text, outcome))

    async def done(self):
        await until(lambda: bool(self.finals), "the task Manager's final reply")


class NativeTaskManager:
    def __init__(self, monkeypatch, tmp_path):
        self.root, self.transcript = tmp_path, tmp_path / "native-transcript.jsonl"
        self.settings = AgentSettings(profile=builtin_resolved_profile())
        monkeypatch.setattr(task_conversations_settings, "_enabled", True)
        monkeypatch.setattr(task_conversations_settings, "_manager_answers", True)
        monkeypatch.setattr(outbound_hook, "known_enabled", lambda: False)
        monkeypatch.setattr(task_manager_safety, "gate", AsyncMock(return_value=None))

        async def read_settings(task):
            return self.settings

        monkeypatch.setattr(task_manager_settings, "load_task_manager_settings", read_settings)
        self.executable = fake_claude(tmp_path / "scripted-claude", f'''
import sys, json, os, uuid, subprocess
from pathlib import Path
log = Path({str(self.transcript)!r})
def record(value):
    with log.open('a') as stream:
        stream.write(json.dumps(value) + '\\n')
def emit(value):
    print(json.dumps(value), flush=True)
if '--version' in sys.argv:
    print('2.1.332 (Claude Code)'); sys.exit()
resuming = '--resume' in sys.argv
record({{'kind':'launch', 'command':sys.argv[1:], 'cwd':os.getcwd(),
         'environment':{{key:os.environ.get(key) for key in ('LOOPER_TASK_MANAGER','LOOPER_AGENT_RUN','SERVER_PRIVATE')}}}})
session = sys.argv[sys.argv.index('--resume')+1] if resuming else uuid.uuid4().hex
if resuming and Path('reject-resume').exists():
    print('Synthetic missing session', file=sys.stderr); sys.exit(2)
if resuming and Path('wrong-resume').exists():
    session = 'wrong-session'
total = .1 if resuming else 0
measurement_config = {{}}
def finish(text):
    global total
    total += .1
    emit({{'type':'result','subtype':'success','result':text,'session_id':session,
           'total_cost_usd':total,'usage':{{'input_tokens':10,'output_tokens':5}},
           **({{'usage_complete':False}} if measurement_config.get('unknown_cost') else {{}})}})
for line in sys.stdin:
    data = json.loads(line)
    if data.get('type') == 'control_request':
        emit({{'type':'control_response','response':{{'subtype':'success','request_id':data['request_id']}}}})
        finish('stopped')
        continue
    text = data.get('message',{{}}).get('content','')
    config_path = Path('context-probe.json')
    measurement_config = json.loads(config_path.read_text()) if config_path.exists() else {{}}
    record({{'kind':'input','text':text,'session':session}})
    emit({{'type':'system','subtype':'init','session_id':session}})
    if text == 'wait':
        continue
    if text == '/compact':
        behavior = measurement_config.get('compaction', 'success')
        if behavior == 'wait':
            continue
        if behavior == 'failure':
            emit({{'type':'result','subtype':'error','is_error':True,'session_id':session}})
            continue
        if behavior != 'missing':
            post = measurement_config.get('post_tokens', 20)
            metadata = {{'trigger':'manual', 'pre_tokens':measurement_config.get('context_tokens', 120)}}
            if post is not None:
                metadata['post_tokens'] = post
            emit({{'type':'system','subtype':'compact_boundary','compact_metadata':metadata}})
            if post is not None:
                emit({{'type':'assistant','message':{{'content':[], 'usage':{{'input_tokens':post}}}}}})
        finish('compacted')
        continue
    if measurement_config.get('context_tokens') is not None:
        emit({{'type':'assistant','message':{{'content':[], 'usage':{{'input_tokens':measurement_config['context_tokens']}}}}}})
    if text == 'write':
        Path('changed.txt').write_text('changed')
    if text.startswith(('mv:', 'gitmv:')):
        old, new = text.split(':', 1)[1].split('>')
        if text.startswith('gitmv:'):
            subprocess.run(['git', 'mv', old, new], check=True)
        else:
            os.rename(old, new)
    if text.startswith('revert:'):
        subprocess.run(['git', 'checkout', '--', text[7:]], check=True)
    if text.startswith('edit:'):
        for name in text[5:].split(','):
            Path(name).write_text('changed by the turn\\n')
    emit({{'type':'stream_event','event':{{'delta':{{'type':'text_delta','text':'one'}}}}}})
    emit({{'type':'stream_event','event':{{'delta':{{'type':'text_delta','text':'two'}}}}}})
    emit({{'type':'assistant','message':{{'content':[{{'type':'tool_use','id':'tool-call','name':'Read','input':{{'file_path':'sample'}}}}]}}}})
    emit({{'type':'user','message':{{'content':[{{'type':'tool_result','tool_use_id':'tool-call','content':'contents'}}]}}}})
    finish('onetwo')
''')

    def records(self, kind):
        if not self.transcript.exists():
            return []
        return [row for line in self.transcript.read_text().splitlines() if (row := json.loads(line))["kind"] == kind]

    async def create(self, db, task_id="task-one", *, status="draft", workspace_mode="in_place", path=None):
        directory = path or self.root / task_id
        directory.mkdir(parents=True, exist_ok=True)
        project = await projects.create_project(name=f"Synthetic project {task_id}", work_dir=str(directory), pool=db)
        await tasks.create_task(task_id=task_id, requirement="Implement the synthetic feature", project_id=project["id"],
            status=status, work_dir=str(directory), workspace_mode=workspace_mode, workspace_dir=str(directory), pool=db)
        return task_id, project["id"], directory

    def switch_model(self, name):
        profile = self.settings.profile
        resolved = replace(profile.resolved_model, model=replace(profile.resolved_model.model, name=name))
        self.settings = replace(self.settings, profile=replace(profile, resolved_model=resolved))


async def submit_and_wait(pool, task_id, text="reply", *, message_id="message-one", history=()):
    sink = Sink()
    result = await pool.submit(task_id, text, message_id=message_id, sink=sink, history=history)
    assert result.state == "started"
    await sink.done()
    await until(lambda: not pool.in_turn(task_id), "the task Manager's turn cleanup")
    return sink
