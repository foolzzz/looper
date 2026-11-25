"""Network recovery through a real isolated server and a native CLI stand-in, without model calls."""
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
import time
import tomllib
from urllib.parse import urlsplit

import pytest

from tests.db_support import DSN, fresh_database_without_recording

pytestmark = pytest.mark.skipif(not DSN, reason="requires disposable PostgreSQL")
ROOT = Path(__file__).resolve().parents[1]

_CLI = r'''
import json,re,sys
from pathlib import Path
state=Path(sys.argv[1]); args=sys.argv[2:]
if '--version' in args:
    print('2.1.285 (fixture)');sys.exit(0)
if args[:1]==['auth']:
    print('{"loggedIn":true}');sys.exit(0)
flag=next((x for x in ('--append-system-prompt-file','--system-prompt-file') if x in args),None)
prompt=Path(args[args.index(flag)+1]).read_text() if flag else ''
variant=re.search(r'network_variant=(\w+)',prompt)
variant=variant.group(1) if variant else 'manager'
worker='Your step:' in prompt
def emit(value): print(json.dumps(value),flush=True)
def answer(value):
    text='Fixture result.\n```json\n'+json.dumps(value)+'\n```'
    emit(dict(type='system',subtype='init',session_id='network-fixture',model='synthetic'))
    emit(dict(type='assistant',message=dict(role='assistant',content=[dict(type='text',text=text)])))
    emit(dict(type='result',subtype='success',is_error=False,result=text,session_id='network-fixture',
        total_cost_usd=.001,usage=dict(input_tokens=10,output_tokens=10)))
for line in sys.stdin:
    if not line.strip():continue
    content=json.loads(line)['message']['content']
    text=content if isinstance(content,str) else ''.join(x.get('text','') for x in content)
    role='worker' if worker else 'planner'
    count=state/(role+'.count')
    previous=int(count.read_text()) if count.exists() else 0
    count.write_text(str(previous+1))
    should_fail=(worker and variant in ('worker','expiry','off')) or (not worker and variant=='planner' and '[PLAN]' in text)
    if should_fail and previous==0:
        emit(dict(type='system',subtype='init',session_id='network-fixture',model='synthetic'))
        emit(dict(type='result',subtype='error_during_execution',is_error=True,
            api_error_code='ENOTFOUND',result='Controlled model API network failure',total_cost_usd=.001))
        sys.exit(1)
    if worker:
        answer(dict(status='completed',changed_files={},deleted_files=[],summary='Synthetic work completed.'))
        sys.exit(0)
    if '[PLAN]' in text or 'Your last reply broke the reply contract' in text:
        answer(dict(steps=[dict(id='work',instruction='Complete the synthetic work.',
            acceptance_criteria=['A result is available.'],depends_on=[],review=False)]))
    elif text.startswith('[DECISION]'):
        if variant=='off':
            answer(dict(decision='fail',reason='The failed execution followed the original decision path.'))
            continue
        answer(dict(decision='accept',reason='Synthetic work succeeded.',summary='Completed.',
            final=dict(delivered='Synthetic result.',how_to_use='Read the result.',
                verification='Fixture checks passed.',open_issues='None.')))
    else:answer(dict(action='none',reply='Fixture reply.'))
'''


def _query(url, sql, *args):
    import asyncpg

    async def run():
        conn = await asyncpg.connect(url, timeout=10)
        try:
            return [dict(row) for row in await conn.fetch(sql, *args)]
        finally:
            await conn.close()
    return asyncio.run(run())


def _until(check, label, timeout=30):
    deadline = time.monotonic() + timeout
    while True:
        value = check()
        if value:
            return value
        assert time.monotonic() < deadline, f"timed out waiting for {label}"
        time.sleep(.1)


@contextmanager
def _serve(root, url):
    from evals.prompt_regression.runner import Api, free_port
    from looper import ops_config
    from looper.agents import operator_auth

    runtime, home, binary, state = (root / name for name in ("runtime", "home", "bin", "state"))
    for directory in (runtime, home, binary, state):
        directory.mkdir()
    script = root / "fixture_cli.py"
    script.write_text(_CLI)
    for name in ("claude", "codex", "grok", "omp", "pi", "dsh", "coco", "cursor-agent", "agent", "ttadk", "lark-cli"):
        executable = binary / name
        executable.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} " +
            (f'{shlex.quote(str(script))} {shlex.quote(str(state))} "$@"\n' if name == "claude"
             else '-c "import sys; sys.exit(76)"\n'))
        executable.chmod(0o755)
    port = free_port()
    assert port != 8767 and urlsplit(url).port == 54331
    assert urlsplit(url).path != urlsplit(DSN).path
    config = ops_config.OpsConfig(runtime_root=runtime, path=runtime / ops_config.FILE, database_url=url,
        host="127.0.0.1", port=port, operator_notes_root=str(home / "notes"), pi_runtime="cli", dsh_runtime="cli")
    ops_config.write_private(config.path, ops_config.render(config))
    saved = tomllib.loads(config.path.read_text())
    assert saved["server"]["port"] == port != 8767 and saved["database"]["url"] == url
    env = {key: value for key, value in os.environ.items() if key not in ops_config.VARIABLES
           and not key.endswith("_API_KEY") and not key.startswith(("LOOPER_", "LOOPX_", "TELEGRAM_", "LARK_",
               "FEISHU_", "CLAUDE_", "ANTHROPIC_", "OPENAI_", "GROK_", "CODEX_"))}
    env.update(HOME=str(home), PATH=str(binary) + os.pathsep + os.environ["PATH"], PYTHONPATH=str(ROOT),
        CLAUDE_CONFIG_DIR=str(home / ".claude"), CODEX_HOME=str(home / ".codex"), GROK_HOME=str(home / ".grok"))
    log = (root / "serve.log").open("w")
    bootstrap = f"""
from pathlib import Path
from looper.agents import network_pause
network_pause.PROBE_INITIAL_SECONDS = .1
network_pause.PROBE_MAX_SECONDS = .2
original_probe = network_pause.probe
async def observed_probe(target):
    connected = await original_probe(target)
    with Path({str(state / 'probes.jsonl')!r}).open('a') as output:
        output.write(str(connected) + '\\n')
    return connected
network_pause.probe = observed_probe
from looper.cli import main
import sys
sys.exit(main(sys.argv[1:]))
"""
    process = subprocess.Popen([sys.executable, "-c", bootstrap,
        "serve", "--config", str(config.path),
        "--runtime-root", str(runtime)], cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT)

    class IsolatedApi(Api):
        def request(self, method, path, body=None, **query):
            assert urlsplit(self.base_url).port == port != 8767
            return super().request(method, path, body, **query)

    api = IsolatedApi(f"http://127.0.0.1:{port}", timeout=10)
    try:
        def ready():
            assert process.poll() is None, "isolated server exited before readiness"
            api.token = operator_auth.read_token(runtime)
            if api.token is None:
                return False
            try:
                return api.post("/api/v1/system/status", {})
            except Exception:
                return False
        _until(ready, "isolated server startup", timeout=45)
        yield api, state
    except BaseException:
        print((root / "serve.log").read_text(errors="replace")[-14000:])
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


@pytest.mark.parametrize("variant", ["planner", "worker", "expiry", "off"])
def test_isolated_serve_network_pause_resume_and_expiry(tmp_path, variant):
    work = tmp_path / "work"
    work.mkdir()
    (work / "retained.txt").write_text("Workspace content survives a network pause.\n")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=work, check=True, capture_output=True)
    subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgSign=false",
        "-c", "user.name=Example", "-c", "user.email=dev@example.org", "commit", "--allow-empty", "-qm", "Initial"],
        cwd=work, check=True, capture_output=True)
    with socket.socket() as endpoint, fresh_database_without_recording(str(DSN)) as (url, _):
        endpoint.bind(("127.0.0.1", 0))  # Bound but not listening: the model endpoint is unavailable.
        endpoint_url = f"http://127.0.0.1:{endpoint.getsockname()[1]}"
        _query(url, "INSERT INTO agent_setting_layers(scope,env) VALUES('system',$1::jsonb) RETURNING scope",
               json.dumps([{"key": "ANTHROPIC_BASE_URL", "value": endpoint_url}]))
        with _serve(tmp_path, url) as (api, state):
            def call(operation, body=None):
                return api.post("/api/v1/" + operation.replace(".", "/"), body or {})
            def confirmed(operation, body):
                answer = call(operation, body)
                if answer["status"] == "needs_confirmation":
                    answer = call("confirm", {"confirmation_id": answer["confirmation_id"]})
                return answer
            def detail():
                return call("tasks.detail", {"task_id": task_id})["result"]

            assert call("network_pause.get")["result"]["enabled"] is False
            if variant != 'off':
                policy = confirmed("network_pause.update", {"enabled": True, "max_pause_seconds": 1 if variant == "expiry" else 60})
                assert policy["result"]["enabled"] is True
            confirmed("attention.settings_update", {"enabled": True})
            project = call("project_admin.create", {"name": "Network recovery example", "work_dir": str(work),
                "task_workspaces": "in_place", "step_workspaces": "shared", "operator_notes": "off",
                "vault": "off", "distill_lessons": "off"})["result"]["project"]["project_id"]
            task_id = call("tasks.create", {"project_id": project,
                "requirement": "Synthetic network_variant=" + variant})["result"]["task_id"]
            confirmed("ops.task_control.start", {"task_id": task_id})
            if variant == 'off':
                failed = _until(lambda: (row if (row := detail())['status'] == 'failed' else None), 'original failure path')
                assert failed.get('network_pause') is None
                assert int((state / 'worker.count').read_text()) == 1
                assert _query(url, 'SELECT count(*) AS total FROM task_network_pauses')[0]['total'] == 0
                assert 'network_pause' not in call('metrics.task_outcomes')['result']
                return
            paused = _until(lambda: (row if (row := detail()).get("network_pause") else None), "network pause")
            assert paused["network_pause"]["state"] == "paused"
            assert paused["network_pause"]["reason"] and paused["status"] == "waiting"
            assert all(step["retries"] == 0 for step in paused["steps"])
            plan = _query(url, "SELECT plan_id FROM task_plans WHERE task_id=$1", task_id)
            counts = {path.name: path.read_text() for path in state.glob("*.count")}
            if variant == "expiry":
                expired = _until(lambda: (row if (row := detail())["status"] == "waiting_user" else None), "bounded expiry")
                assert expired["network_pause"]["state"] == "waiting_user"
                assert all(step["retries"] == 0 for step in expired["steps"])
                for _ in range(2):
                    summary = call("attention.summary")["result"]
                    matching = [item for section in summary["sections"] for item in section["items"]
                                if item.get("context") == task_id or item.get("href") == f"#tasks/{task_id}"]
                    assert len(matching) == 1 and matching[0]["title"]
                assert counts == {path.name: path.read_text() for path in state.glob("*.count")}
            else:
                _until(lambda: (state / "probes.jsonl").exists(), "failed real TCP probe")
                assert "False" in (state / "probes.jsonl").read_text().splitlines()
                # A failed real probe cannot dispatch another model run.
                assert detail()["network_pause"]["state"] == "paused"
                assert counts == {path.name: path.read_text() for path in state.glob("*.count")}
                endpoint.listen()
                completed = _until(lambda: (row if (row := detail())["status"] == "completed" else None), "probe recovery")
                assert completed.get("network_pause") is None
                assert completed["work_dir"] == paused["work_dir"]
                assert all(step["retries"] == 0 for step in completed["steps"])
                assert _query(url, "SELECT plan_id FROM task_plans WHERE task_id=$1", task_id) == plan
                assert int((state / f"{'planner' if variant == 'planner' else 'worker'}.count").read_text()) >= 2
                assert "True" in (state / "probes.jsonl").read_text().splitlines()
            stats = call("metrics.task_outcomes")["result"]["network_pause"]
            assert stats["pause_count"] == 1
            assert stats["expired_count" if variant == "expiry" else "resume_count"] == 1
            assert stats["duration_seconds"] > 0
            assert (work / "retained.txt").read_text() == "Workspace content survives a network pause.\n"
            if variant == 'expiry':
                confirmed('ops.task_control.stop', {'task_id': task_id})
                stopped = _until(lambda: (row if (row := detail())['status'] == 'stopped' else None), 'operator stop')
                assert stopped.get('network_pause') is None
                assert counts == {path.name: path.read_text() for path in state.glob('*.count')}
                endpoint.listen()
                confirmed('ops.task_control.restart', {'task_id': task_id})
                restarted = _until(lambda: (row if (row := detail())['status'] == 'completed' else None), 'operator restart')
                assert restarted.get('network_pause') is None
                assert _query(url, 'SELECT plan_id FROM task_plans WHERE task_id=$1', task_id) == plan
