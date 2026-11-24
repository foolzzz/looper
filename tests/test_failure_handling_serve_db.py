"""Zero-model failure recovery through an isolated, real serve process and CLI."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import time
import tomllib
from urllib.parse import urlsplit

import pytest

from tests.db_support import DSN, fresh_database_without_recording

pytestmark = pytest.mark.skipif(not DSN, reason='requires disposable PostgreSQL')
ROOT = Path(__file__).resolve().parents[1]

_CLI = r'''
import json,re,sys,time
from pathlib import Path
state=Path(sys.argv[1]); args=sys.argv[2:]
if '--version' in args:
    print('2.1.285 (fixture)');sys.exit(0)
if args[:1]==['auth']:
    print('{"loggedIn":true}');sys.exit(0)
flag=next((x for x in ('--append-system-prompt-file','--system-prompt-file') if x in args),None)
prompt=Path(args[args.index(flag)+1]).read_text() if flag else ''
variant=re.search(r'failure_variant=(\w+)',prompt)
variant=variant.group(1) if variant else 'manager'
worker='Your step:' in prompt

def emit(value):
    print(json.dumps(value),flush=True)
def answer(value):
    text='Fixture result.\n```json\n'+json.dumps(value)+'\n```'
    emit(dict(type='system',subtype='init',session_id='failure-fixture-session',model='synthetic'))
    emit(dict(type='assistant',message=dict(role='assistant',content=[dict(type='text',text=text)])))
    emit(dict(type='result',subtype='success',is_error=False,result=text,session_id='failure-fixture-session',
              total_cost_usd=.001,usage=dict(input_tokens=10,output_tokens=10)))
for line in sys.stdin:
    if not line.strip():continue
    content=json.loads(line)['message']['content']
    text=content if isinstance(content,str) else ''.join(x.get('text','') for x in content)
    if worker:
        count=state/(variant+'.count')
        previous=int(count.read_text()) if count.exists() else 0
        count.write_text(str(previous+1))
        with (state/'starts.jsonl').open('a') as log:
            log.write(json.dumps(dict(variant=variant,attempt=previous+1,at=time.time()))+'\n')
        if previous==0 and variant!='off':
            emit(dict(type='system',subtype='init',session_id='worker-fixture-session',model='synthetic'))
            if variant=='transient':
                fields=dict(api_error='connection_failed',api_error_type='api_error')
            elif variant=='quota':
                emit(dict(type='rate_limit_event',rate_limit_info=dict(status='rejected',
                    rateLimitType='seven_day',resetsAt=time.time()+900,utilization=1)))
                fields=dict(api_error='usage_limit_reached',api_error_status=429,api_error_type='rate_limit_error')
            else:fields=dict(api_error_status=401,api_error_type='authentication_error')
            emit(dict(type='result',subtype='error_during_execution',is_error=True,num_turns=1,
                      result='Synthetic controlled failure',total_cost_usd=.001,**fields))
            sys.exit(1)
        answer(dict(status='completed',changed_files={},deleted_files=[],summary='Synthetic work is ready.'))
        sys.exit(0)
    if '[PLAN]' in text or 'Your last reply broke the reply contract' in text:
        answer(dict(steps=[dict(id='work',instruction='Complete the synthetic work.',
            acceptance_criteria=['A result is available.'],depends_on=[],review=False)]))
    elif text.startswith('[DECISION]'):
        if variant in ('env_planner','env_drain'):
            answer(dict(decision='fail',reason='Operator requested Planner handling of the synthetic environment failure.'))
        else:
            answer(dict(decision='accept',reason='Synthetic work succeeded.',summary='Completed.',
                final=dict(delivered='Synthetic result.',how_to_use='Read the result.',
                    verification='Fixture checks passed.',open_issues='None.')))
    else:answer(dict(action='none',reply='Fixture reply.'))
'''

_NOTICE = r'''
import json,os
from pathlib import Path
from looper.agents.channels.registry import ChannelRegistry
from looper.agents.channels.base import PostReceipt
_original_post=ChannelRegistry.post
async def _fixture_post(self,channel_id,target,text,**kwargs):
    if channel_id=='channel-telegram':
        assert target.id=='synthetic-failure-chat'
        with Path(os.environ['FAILURE_SMOKE_NOTICE']).open('a') as output:
            output.write(json.dumps(dict(chat=target.id,text=text,key=kwargs.get('idempotency_key'),reply_to=kwargs.get('reply_to')))+'\n')
        return PostReceipt(True)
    return await _original_post(self,channel_id,target,text,**kwargs)
ChannelRegistry.post=_fixture_post
'''


def query(url, statement, *args):
    import asyncpg
    async def run():
        conn = await asyncpg.connect(url, timeout=10)
        try:
            return [dict(row) for row in await conn.fetch(statement, *args)]
        finally:
            await conn.close()
    return asyncio.run(run())


def until(check, what, timeout=30):
    deadline = time.monotonic() + timeout
    while True:
        value = check()
        if value:
            return value
        assert time.monotonic() < deadline, f'timed out waiting for {what}'
        time.sleep(.1)


@contextmanager
def serve(root, url):
    from evals.prompt_regression.runner import Api, free_port
    from looper import ops_config
    from looper.agents import operator_auth

    runtime, home, bin_dir, state = (root / name for name in ('runtime', 'home', 'bin', 'state'))
    for directory in (runtime, home, bin_dir, state):
        directory.mkdir(exist_ok=True)
    script = root / 'fixture_cli.py'
    script.write_text(_CLI)
    for name in ('claude', 'codex', 'grok', 'omp', 'pi', 'dsh', 'coco', 'cursor-agent', 'agent', 'ttadk', 'lark-cli'):
        executable = bin_dir / name
        executable.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} ' +
            (f'{shlex.quote(str(script))} {shlex.quote(str(state))} "$@"\n' if name == 'claude' else '-c "import sys; sys.exit(76)"\n'))
        executable.chmod(0o755)
    port = free_port()
    assert port != 8767 and urlsplit(url).port == urlsplit(DSN).port
    assert urlsplit(url).path != urlsplit(DSN).path  # the shared helper created a new database
    ops = ops_config.OpsConfig(runtime_root=runtime, path=runtime / ops_config.FILE, database_url=url,
        host='127.0.0.1', port=port, operator_notes_root=str(home / 'notes'),
        pi_runtime='cli', dsh_runtime='cli')
    ops_config.write_private(ops.path, ops_config.render(ops))
    saved = tomllib.loads(ops.path.read_text())
    assert saved['server']['port'] == port != 8767 and saved['database']['url'] == url
    assert ops.path.stat().st_mode & 0o777 == 0o600 and ops.path.parent.stat().st_mode & 0o777 == 0o700
    env = {key: value for key, value in os.environ.items() if key not in ops_config.VARIABLES and not key.endswith('_API_KEY')
           and not key.startswith(('LOOPER_', 'LOOPX_', 'TELEGRAM_', 'LARK_', 'FEISHU_', 'CLAUDE_',
                                   'ANTHROPIC_', 'OPENAI_', 'GROK_', 'CODEX_'))}
    env.update(HOME=str(home), PATH=str(bin_dir) + os.pathsep + os.environ['PATH'], PYTHONPATH=str(ROOT),
        CLAUDE_CONFIG_DIR=str(home / '.claude'), CODEX_HOME=str(home / '.codex'), GROK_HOME=str(home / '.grok'),
        FAILURE_SMOKE_NOTICE=str(root / 'notices.jsonl'))
    dashboard = ROOT / 'web' / 'dist'
    if (dashboard / 'index.html').is_file():
        env['PYTEST_LOOPER_STATIC_DIR'] = str(dashboard)
    command = _NOTICE + '\nfrom looper.cli import main\nimport sys\nsys.exit(main(sys.argv[1:]))\n'
    log = (root / 'serve.log').open('a')
    process = subprocess.Popen([sys.executable, '-c', command, 'serve', '--config', str(ops.path),
        '--runtime-root', str(runtime)], cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT)
    class IsolatedApi(Api):
        def request(self, method, path, body=None, **query):
            assert urlsplit(self.base_url).port == port != 8767
            return super().request(method, path, body, **query)
    api = IsolatedApi(f'http://127.0.0.1:{port}', timeout=10)
    try:
        def ready():
            if process.poll() is not None:
                raise AssertionError('isolated serve exited before becoming ready')
            api.token = operator_auth.read_token(runtime)
            if api.token is None:
                return False
            try:
                return api.post('/api/v1/system/status', {})
            except Exception:
                return False
        until(ready, 'isolated server startup', timeout=45)
        import urllib.request
        assert urlsplit(api.base_url).port != 8767
        if (dashboard / 'index.html').is_file():
            with urllib.request.urlopen(api.base_url + '/', timeout=10) as homepage:
                assert b'<html' in homepage.read().lower()
        yield api, runtime, state, env
    except BaseException:
        print((root / 'serve.log').read_text(errors='replace')[-12000:])
        raise
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        log.close()
        with socket.socket() as probe:
            assert probe.connect_ex(('127.0.0.1', port)) != 0


def test_isolated_serve_holds_retries_releases_and_reads_full_history():
    import looper
    from looper.agents import operator_auth
    assert Path(looper.__file__).resolve().parents[1] == ROOT
    assert sys.version_info[:2] == (3, 11)
    local = ROOT / '.local'
    local.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='failure-serve-', dir=local) as directory:
        root = Path(directory)
        work = root / 'work'
        work.mkdir()
        subprocess.run(['git', 'init', '-q', '-b', 'main'], cwd=work, check=True, capture_output=True)
        subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgSign=false',
            '-c', 'user.name=Example', '-c', 'user.email=dev@example.org', 'commit', '--allow-empty', '-qm', 'Initial'],
            cwd=work, check=True, capture_output=True)
        with fresh_database_without_recording(str(DSN)) as (url, _):
            assert query(url, 'SELECT current_database() AS name')[0]['name'] == urlsplit(url).path.lstrip('/')
            query(url, "INSERT INTO system_settings(key,value) VALUES('attention.enabled','true') ON CONFLICT(key) DO UPDATE SET value='true' RETURNING key")
            with serve(root, url) as (api, runtime, state, env):
                def call(operation, body=None):
                    return api.post('/api/v1/' + operation.replace('.', '/'), body or {})
                def confirmed(operation, body):
                    result = call(operation, body)
                    if result['status'] == 'needs_confirmation':
                        return call('confirm', {'confirmation_id': result['confirmation_id']})
                    return result
                def detail(task_id):
                    return call('tasks.detail', {'task_id': task_id})['result']
                def holds(task_id):
                    return call('tasks.holds', {'task_id': task_id})['result']['holds']
                project = call('project_admin.create', {'name': 'Failure recovery example', 'work_dir': str(work),
                    'task_workspaces': 'in_place', 'step_workspaces': 'shared', 'operator_notes': 'off',
                    'vault': 'off', 'distill_lessons': 'off'})['result']['project']['project_id']
                def task(variant):
                    task_id = call('tasks.create', {'project_id': project,
                        'requirement': 'Synthetic failure_variant=' + variant})['result']['task_id']
                    query(url, "UPDATE tasks SET origin_channel_id='channel-telegram',chat_id='synthetic-failure-chat',"
                        "chat_kind='chat',origin_thread='synthetic-thread',origin_reply_route=jsonb_build_object('reply_to','synthetic-parent') WHERE id=$1 RETURNING id", task_id)
                    confirmed('ops.task_control.start', {'task_id': task_id})
                    return task_id
                off = task('off')
                until(lambda: detail(off)['status'] == 'completed', 'default-off task')
                assert not query(url, 'SELECT id FROM step_failures')
                confirmed('engine_settings.update', {'failure_typing': 'act'})
                transient = task('transient')
                transient_hold = until(lambda: holds(transient), 'transient hold')[0]
                assert transient_hold['kind'] == 'transient'
                assert detail(transient)['steps'][0]['status'] == 'pending'
                retry_task = task('env_retry')
                environment_hold = until(lambda: holds(retry_task), 'environment hold')[0]
                until(lambda: detail(retry_task)['status'] == 'waiting_user', 'Needs you state')
                until(lambda: (root / 'notices.jsonl').exists(), 'synthetic source notification')
                assert all(row['chat'] == 'synthetic-failure-chat' and row['reply_to'] == 'synthetic-parent' for row in
                           map(json.loads, (root / 'notices.jsonl').read_text().splitlines()))
                pointer = local / 'failure-smoke-current.json'
                pointer.write_text(json.dumps({'base_url': api.base_url, 'runtime_root': str(runtime),
                    'task_id': retry_task, 'transient_task_id': transient}))
                pointer.chmod(0o600)
                # Requesting and then cancelling the confirmation locally must not release the hold.
                fields = {'task_id': retry_task, 'step_id': environment_hold['step_id'],
                          'failure_id': environment_hold['failure_id'], 'action': 'retry'}
                assert call('tasks.hold_release', fields)['status'] == 'needs_confirmation'
                assert holds(retry_task)[0]['failure_id'] == environment_hold['failure_id']
                token_path = operator_auth.token_path(runtime)
                assert urlsplit(api.base_url).port != 8767
                cli = subprocess.run([sys.executable, '-m', 'looper.loopercli', '--json', 'tasks', 'failure-history',
                    '--task-id', retry_task, '--url', api.base_url, '--operator-token-file', str(token_path)],
                    env=env, cwd=root, text=True, capture_output=True, timeout=15)
                assert cli.returncode == 0, cli.stderr
                assert 'login' in cli.stdout
                # Keep the live environment hold available while the independent transient timer really elapses.
                until(lambda: detail(transient)['status'] == 'completed', 'real transient backoff recovery', timeout=170)
                starts = [json.loads(line) for line in (state / 'starts.jsonl').read_text().splitlines()
                          if json.loads(line)['variant'] == 'transient']
                assert len(starts) == 2 and 95 <= starts[1]['at'] - starts[0]['at'] <= 150
                confirmed('tasks.hold_release', fields)
                until(lambda: detail(retry_task)['status'] == 'completed', 'operator retry recovery')
                for variant, action, expected in (('env_planner', 'planner', 'failed'), ('env_stop', 'stop', 'stopped')):
                    task_id = task(variant)
                    hold = until(lambda: holds(task_id), variant + ' hold')[0]
                    confirmed('tasks.hold_release', {'task_id': task_id, 'step_id': hold['step_id'],
                        'failure_id': hold['failure_id'], 'action': action})
                    until(lambda: detail(task_id)['status'] == expected, action + ' terminal state')
                    assert not holds(task_id)
                confirmed('admission_settings.update', {'enabled': True})
                quota = task('quota')
                quota_hold = until(lambda: holds(quota), 'quota hold')[0]
                assert quota_hold['kind'] == 'quota' and quota_hold['until'] is not None
                admission = call('providers.limits', {'provider_id': 'prov-claude-subscription'})['result']['providers'][0]['admission']
                assert admission['state'] == 'frozen' and admission['running'] == 0
                confirmed('providers.limits_clear', {'provider_id': 'prov-claude-subscription'})
                until(lambda: detail(quota)['status'] == 'completed', 'quota recovery after account clear')
                drain = task('env_drain')
                until(lambda: holds(drain), 'hold before disabling recovery')
                confirmed('engine_settings.update', {'failure_typing': 'record'})
                until(lambda: detail(drain)['status'] == 'failed', 'old decision path after disabling recovery')
                assert not holds(drain)
                history = call('tasks.failure_history', {'task_id': drain})['result']['items']
                assert len(history) == 1 and history[0]['failure_class'] == 'environment'
                confirmed('engine_settings.update', {'failure_typing': 'act'})
                restart_task = task('env_restart')
                restart_hold = until(lambda: holds(restart_task), 'durable environment hold before restart')[0]
                assert restart_hold['streak'] == 1
                pointer.unlink(missing_ok=True)
            # Stop a real process while a hold is live; restoring cannot reset its sequence or rerun its Worker.
            with serve(root, url) as (api, runtime, state, env):
                restored = until(lambda: holds(restart_task), 'hold restored by a new process')[0]
                assert restored['failure_id'] == restart_hold['failure_id'] and restored['streak'] == 1
                assert int((state / 'env_restart.count').read_text()) == 1
                confirmed('tasks.hold_release', {'task_id': restart_task, 'step_id': restored['step_id'],
                    'failure_id': restored['failure_id'], 'action': 'retry'})
                until(lambda: detail(restart_task)['status'] == 'completed', 'resumed operator retry')
                assert int((state / 'env_restart.count').read_text()) == 2
                assert len(call('tasks.failure_history', {'task_id': restart_task})['result']['items']) == 1
