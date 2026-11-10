"""A review-panel task completes through a real server with isolated CLI stand-ins."""
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import time

import pytest

from evals.prompt_regression.runner import running_server
from tests.db_support import DSN, fresh_database_without_recording

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
ROOT = Path(__file__).resolve().parents[1]

_CLAUDE = r'''
import json, sys
args = sys.argv[1:]
if '--version' in args:
    print('2.1.285 (stand-in)'); sys.exit(0)
if args[:1] == ['auth']:
    print('{"loggedIn":true}'); sys.exit(0)
flag = next(flag for flag in ('--append-system-prompt-file', '--system-prompt-file') if flag in args)
prompt = open(args[args.index(flag) + 1]).read()

def answer(value):
    text = 'Done.\n```json\n' + json.dumps(value) + '\n```'
    for event in (
        {'type': 'system', 'subtype': 'init', 'session_id': 'stand-in-session', 'model': 'stand-in'},
        {'type': 'assistant', 'session_id': 'stand-in-session',
         'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': text}]}},
        {'type': 'result', 'subtype': 'success', 'is_error': False, 'result': text,
         'session_id': 'stand-in-session', 'total_cost_usd': 0.001,
         'usage': {'input_tokens': 10, 'output_tokens': 10}},
    ):
        print(json.dumps(event), flush=True)

for line in sys.stdin:
    item = json.loads(line)['message']['content']
    text = item if isinstance(item, str) else ''.join(part.get('text', '') for part in item)
    if 'Your step:' in prompt:
        answer({'status': 'completed', 'changed_files': {}, 'deleted_files': [], 'summary': 'Change ready.'})
        break
    if text.startswith('[PLAN]'):
        answer({'steps': [{'id': 's1', 'instruction': 'Check the prepared change.', 'review': True,
                            'acceptance_criteria': ['The change is ready.'], 'depends_on': []}]})
    elif text.startswith('[DECISION]') and "Worker\'s result" in text:
        answer({'decision': 'review'})
    elif text.startswith('[DECISION]'):
        answer({'decision': 'accept', 'reason': 'Both seats passed.', 'summary': 'Reviewed change.',
                'final': {'delivered': 'Reviewed change.', 'how_to_use': 'Use the change.',
                          'verification': 'Both review seats passed.', 'open_issues': 'None.'}})
    elif text.startswith('[CHAT]'):
        answer({'action': 'none', 'reason': 'No additional work.'})
    else:
        answer({'reviews': []})
'''

_CODEX = r'''
import json, sys
args = sys.argv[1:]
if '--version' in args:
    print('codex-cli 0.160.0'); sys.exit(0)
if 'login' in args:
    print('Logged in using ChatGPT'); sys.exit(0)
assert args[0] == 'exec', args
request = sys.stdin.read()
assert "Worker's report" in request, request
text = json.dumps({'passed': True, 'findings': [], 'summary': 'The acceptance criteria hold.'})
for event in (
    {'type': 'thread.started', 'thread_id': 'review-thread'},
    {'type': 'item.completed', 'item': {'id': 'answer', 'type': 'agent_message', 'text': text}},
    {'type': 'turn.completed', 'usage': {'input_tokens': 10, 'output_tokens': 10}},
):
    print(json.dumps(event), flush=True)
'''


def _install(bin_dir: Path) -> None:
    bin_dir.mkdir()
    for name in ('claude', 'codex', 'omp', 'pi', 'dsh', 'grok', 'cursor-agent', 'agent', 'ttadk', 'lark-cli'):
        script = bin_dir / name
        source = _CLAUDE if name == 'claude' else _CODEX if name == 'codex' else 'import sys; sys.exit(76)\n'
        script.write_text(f'#!{sys.executable}\n' + source)
        script.chmod(0o755)


def _pipeline_screenshot(api, task_id):
    from playwright.sync_api import expect, sync_playwright

    with sync_playwright() as driver:
        browser = driver.chromium.launch(headless=True, timeout=15000)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 1000})
            page.set_default_timeout(10000)
            page.goto(f"{api.base_url}/#tasks/{task_id}?tab=pipeline", timeout=15000)
            graph = page.locator('.react-flow')
            expect(graph).to_be_visible()
            expect(page.locator('.react-flow__node-condition')).to_have_count(1)
            expect(page.locator('.react-flow__node-condition')).to_contain_text('必选全部通过')
            page.mouse.move(0, 0)
            page.evaluate("async () => { await document.fonts.ready; await new Promise(requestAnimationFrame); "
                          "await new Promise(requestAnimationFrame); }")
            output = ROOT / 'output' / 'playwright' / 'review-panel-serve.png'
            output.parent.mkdir(parents=True, exist_ok=True)
            graph.screenshot(path=str(output))
        finally:
            browser.close()


@pytest.mark.installed
def test_panel_task_completes_on_serve_with_distinct_runtime_profiles(monkeypatch):
    from evals.prompt_regression import runner

    monkeypatch.setattr(runner, '_SERVE',
        'import sys\nfrom looper.cli import main\n'
        'sys.exit(main(["serve", "--host", "127.0.0.1", "--port", sys.argv[1], '
        '"--runtime-root", sys.argv[2]]))\n')
    # All runtime state stays inside the checkout. The stand-ins cannot contact a model.
    local = ROOT / '.local'
    local.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='review-panel-serve-', dir=local) as directory:
        root = Path(directory)
        runtime, home, bin_dir, work = (root / name for name in ('runtime', 'home', 'bin', 'work'))
        home.mkdir()
        (home / '.codex').mkdir()
        (home / '.codex' / 'auth.json').write_text('{}')
        work.mkdir()
        _install(bin_dir)
        env = {'CLAUDE_CONFIG_DIR': str(home / '.claude'), 'CODEX_HOME': str(home / '.codex'),
               'GROK_HOME': str(home / '.grok'), 'CURSOR_CONFIG_DIR': str(home / '.cursor-config'),
               'CURSOR_DATA_DIR': str(home / '.cursor-data'), 'LOOPER_TELEGRAM_BOT_TOKEN': ''}
        with fresh_database_without_recording(str(DSN)) as (url, _):
            try:
                with running_server(database_url=url, runtime_root=runtime, bin_dir=bin_dir, home=home,
                                    notes_root=home / 'notes', env=env, startup_timeout=30,
                                    log_path=root / 'serve.log') as api:
                    assert not api.base_url.endswith(':8767')
                    models = api.post('/api/v1/ops/models/list', {})['result']['models']
                    profiles = {}
                    for role, runtime_id, provider in (
                        ('dev', 'rt-claude-code', 'prov-claude-subscription'),
                        ('review_a', 'rt-codex', 'prov-codex-login'),
                    ):
                        model = next(model for model in models if model['provider_id'] == provider)
                        profile = api.post('/api/v1/profiles/create', {
                            'name': role, 'runtime_id': runtime_id, 'model_id': model['model_id'],
                        })['result']['profile']
                        profiles[role] = profile['profile_id']
                    api.post('/api/v1/workflows/put', {
                        'key': 'panel', 'name': 'Review panel', 'work_role': 'dev', 'review_all': True,
                        'distinct_reviewer_runtime': True,
                        'roles': [{'key': 'dev', 'kind': 'work', 'role_prompt': 'Implement the change.',
                                   'profile_id': profiles['dev']},
                                  *[{'key': member, 'kind': 'review', 'role_prompt': 'Review the change.',
                                     'profile_id': profiles['review_a']} for member in ('review_a', 'review_b')]],
                        'review_panel': {'members': ['review_a', 'review_b'],
                                         'accept_when': {'all_of': ['review_a', 'review_b']}},
                    })
                    project = api.post('/api/v1/project_admin/create', {
                        'name': 'Review example', 'work_dir': str(work), 'task_workspaces': 'in_place',
                        'step_workspaces': 'shared', 'operator_notes': 'off', 'vault': 'off',
                        'distill_lessons': 'off',
                    })['result']['project']
                    task = api.post('/api/v1/tasks/create', {'project_id': project['project_id'],
                        'requirement': 'Check the prepared change with both review seats.', 'workflow': 'panel'})['result']
                    api.post('/api/v1/ops/task_control/start', {'task_id': task['task_id']})
                    deadline = time.monotonic() + 30
                    while True:
                        detail = api.post('/api/v1/tasks/detail', {'task_id': task['task_id']})['result']
                        if detail['status'] in ('completed', 'failed', 'stopped', 'cancelled'):
                            break
                        assert time.monotonic() < deadline, detail['status']
                        time.sleep(.2)
                    assert detail['status'] == 'completed', detail.get('error')
                    target, *reviews = detail['steps']
                    assert target['accept_when'] and target['accept_state'] == 'satisfied'
                    assert len(reviews) == 2
                    assert all(step['kind'] == 'review' and step['reviews'] == target['id']
                               and step['status'] == 'verified' and step['result']['passed'] for step in reviews)
                    assert target['result']['runtime_id'] == 'rt-claude-code'
                    runs = api.post('/api/v1/runs/for_task', {'task_id': task['task_id']})['result']['runs']
                    assert any(run.get('runtime', {}).get('type') == 'codex' for run in runs)
                    assert [entry['kind'] for entry in detail['journal']].count('review_round') == 1
                    _pipeline_screenshot(api, task['task_id'])
            except BaseException:
                print((root / 'serve.log').read_text(errors='replace')[-12000:])
                raise
