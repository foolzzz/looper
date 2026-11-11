"""Delivery records survive the server/router/database path with isolated CLI stand-ins."""
from __future__ import annotations

import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from evals.prompt_regression.runner import running_server
from tests.db_support import DSN, fresh_database_without_recording
from tests.test_review_panel_serve_db import _install

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.installed
def test_workflow_record_is_persisted_by_serve(monkeypatch):
    from evals.prompt_regression import runner

    monkeypatch.setattr(runner, '_SERVE', 'import sys\nfrom looper.cli import main\n'
        'sys.exit(main(["serve", "--host", "127.0.0.1", "--port", sys.argv[1], "--runtime-root", sys.argv[2]]))\n')
    local = ROOT / '.local'
    local.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='workflow-reporting-', dir=local) as directory:
        root = Path(directory)
        home, work, bin_dir = (root / name for name in ('home', 'work', 'bin'))
        home.mkdir()
        (home / '.codex').mkdir()
        (home / '.codex' / 'auth.json').write_text('{}')
        work.mkdir()
        subprocess.run(['git', 'init', '-q', '-b', 'main'], cwd=work, check=True, capture_output=True, timeout=10)
        subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgSign=false',
                        '-c', 'user.name=Example', '-c', 'user.email=dev@example.org',
                        'commit', '--allow-empty', '-qm', 'Initial'], cwd=work,
                       check=True, capture_output=True, timeout=10)
        _install(bin_dir)
        env = {'CLAUDE_CONFIG_DIR': str(home / '.claude'), 'CODEX_HOME': str(home / '.codex'),
               'GROK_HOME': str(home / '.grok'), 'CURSOR_CONFIG_DIR': str(home / '.cursor-config'),
               'CURSOR_DATA_DIR': str(home / '.cursor-data'), 'LOOPER_TELEGRAM_BOT_TOKEN': ''}
        with fresh_database_without_recording(str(DSN)) as (url, _):
            with running_server(database_url=url, runtime_root=root / 'runtime', bin_dir=bin_dir,
                                home=home, notes_root=home / 'notes', env=env,
                                log_path=root / 'serve.log') as api:
                assert not api.base_url.endswith(':8767')
                models = api.post('/api/v1/ops/models/list', {})['result']['models']
                profiles = {}
                for role, runtime, provider in [('dev', 'rt-claude-code', 'prov-claude-subscription'),
                                                 ('review_a', 'rt-codex', 'prov-codex-login')]:
                    model = next(m for m in models if m['provider_id'] == provider)
                    profiles[role] = api.post('/api/v1/profiles/create', {
                        'name': role, 'runtime_id': runtime, 'model_id': model['model_id'],
                    })['result']['profile']['profile_id']
                api.post('/api/v1/workflows/put', {
                    'key': 'checked', 'name': 'Checked work', 'work_role': 'dev', 'phase_notices': True,
                    'review_all': True, 'distinct_reviewer_runtime': True,
                    'roles': [{'key': 'dev', 'kind': 'work', 'role_prompt': 'Implement the change.',
                               'profile_id': profiles['dev']},
                              {'key': 'review_a', 'kind': 'review', 'role_prompt': 'Review the change.',
                               'profile_id': profiles['review_a']}],
                    'review_panel': {'members': ['review_a'], 'accept_when': {'all_of': ['review_a']}},
                })
                project = api.post('/api/v1/project_admin/create', {
                    'name': 'Reporting example', 'work_dir': str(work), 'task_workspaces': 'in_place',
                    'step_workspaces': 'shared', 'operator_notes': 'off', 'vault': 'off',
                    'distill_lessons': 'off',
                })['result']['project']
                task = api.post('/api/v1/tasks/create', {'project_id': project['project_id'],
                    'requirement': 'Check the prepared change.', 'workflow': 'checked'})['result']
                api.post('/api/v1/ops/task_control/start', {'task_id': task['task_id']})
                deadline = time.monotonic() + 30
                while True:
                    detail = api.post('/api/v1/tasks/detail', {'task_id': task['task_id']})['result']
                    if detail['status'] in ('completed', 'failed'):
                        break
                    assert time.monotonic() < deadline, detail['status']
                    time.sleep(.2)
                assert detail['status'] == 'completed', detail.get('error')
                assert detail['result_summary'].endswith('Full record: task page / looper_tasks get')
                appendix = detail['result_summary'].split('## Process record', 1)[1]
                assert len('## Process record' + appendix) <= 1200
                assert all(text in appendix for text in ('Roles:', 'stand-in', 'Reviews:', '第 1 轮',
                                                        'Verification:', 'Escalations:'))
                from looper.agents.workflow_reporting import process_record_text
                result = process_record_text(detail['process_record'])
                manager_view = api.post('/api/v1/ops/tasks/get', {'task_id': task['task_id']})['result']
                assert manager_view['process_record'] == result
                assert '报告模型 stand-in' in result
                for text in ('dev', r'review\_a', 'codex', '请求模型', '报告模型', '第 1 轮', '0 blocking',
                             'Remote-environment safety (read-only / dry run) is not enforced by Looper'):
                    assert text in result
                assert not any(str(path) in result for path in (root, home, work))
