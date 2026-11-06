"""The detached recording proxy survives serve and recovers through its real five-second tick."""

import asyncio
import http.client
import json
import os
from pathlib import Path
import signal
import shutil
import sys
import tempfile
import time

import pytest

from evals.prompt_regression.runner import fresh_database, running_server
from looper.agents import proc_tree
from looper.agents.llmrec import STATE_FILE
from looper.agents.llmrec.control import ProxyControl
from tests.db_support import DSN
from tests.runner_support import require_disposable_database

pytestmark = pytest.mark.skipif(not DSN, reason="requires disposable LOOPER_TEST_DATABASE_URL")


def _until(check, *, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(.1)
    raise AssertionError("recording proxy did not reach its expected lifecycle state")


def _state(path):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return None


def _health(state):
    if not state:
        return None
    connection = http.client.HTTPConnection('127.0.0.1', state['port'], timeout=.5)
    try:
        connection.request('GET', '/llmrec/health')
        response = connection.getresponse()
        value = json.loads(response.read())
        return value if response.status == 200 and value.get('instance') == state['instance'] else None
    except (OSError, ValueError, http.client.HTTPException):
        return None
    finally:
        connection.close()


def _install_stubs(bin_dir: Path):
    bin_dir.mkdir(exist_ok=True)
    for name in ('claude', 'codex', 'omp', 'pi', 'dsh', 'grok', 'cursor-agent', 'agent', 'ttadk', 'lark-cli'):
        script = bin_dir / name
        script.write_text(f'#!{sys.executable}\n'
            'import sys\n'
            "if '--version' in sys.argv:\n    print('2.1.285 (test runtime)'); sys.exit(0)\n"
            "if sys.argv[1:] == ['auth', '--help']:\n    print('Usage: claude auth status'); sys.exit(0)\n"
            "if sys.argv[1:3] == ['auth', 'status']:\n    print('{\"loggedIn\": false}'); sys.exit(1)\n"
            # Any accidental agent launch stops here; none of the stand-ins can call a model.
            'sys.exit(76)\n')
        script.chmod(0o755)


@pytest.fixture
def short_root(tmp_path):
    # Keeper sockets must fit the platform's short Unix socket path limit.
    with tempfile.TemporaryDirectory(prefix='lr.', dir='/tmp') as directory:
        try:
            yield Path(directory)
        except BaseException:
            for log in Path(directory).rglob('*.log'):
                print(log.name + ':\n' + log.read_text()[-10000:])
            raise
        finally:
            for log in Path(directory).rglob('*.log'):
                print(log.name + ':\n' + log.read_text()[-12000:])
                shutil.copyfile(log, tmp_path / log.name)


@pytest.fixture
def cli_entry(monkeypatch):
    from evals.prompt_regression import runner

    # Use the public CLI entry point in the subprocess, with the fixture's isolated server flags.
    monkeypatch.setattr(runner, '_SERVE',
        'import sys\nfrom looper.cli import main\n'
        'sys.exit(main(["serve", "--host", "127.0.0.1", "--port", sys.argv[1], '
        '"--runtime-root", sys.argv[2]]))\n')


def test_serve_proxy_switch_restart_recovery_and_shutdown(short_root, cli_entry):
    tmp_path = short_root
    require_disposable_database(str(DSN), served=False)
    runtime, home, bin_dir = (tmp_path / name for name in ('runtime', 'home', 'bin'))
    home.mkdir()
    _install_stubs(bin_dir)
    state_path = runtime / STATE_FILE
    environment = {'CLAUDE_CONFIG_DIR': str(home / '.claude'), 'CODEX_HOME': str(home / '.codex'),
        'GROK_HOME': str(home / '.grok'), 'CURSOR_CONFIG_DIR': str(home / '.cursor-config'),
        'CURSOR_DATA_DIR': str(home / '.cursor-data'), 'LOOPER_TELEGRAM_BOT_TOKEN': ''}
    with fresh_database(str(DSN)) as (url, _):
        require_disposable_database(url, served=True)
        server = {'database_url': url, 'runtime_root': runtime, 'bin_dir': bin_dir, 'home': home,
            'notes_root': home / 'notes', 'keeper_mode': 'on', 'startup_timeout': 60, 'env': environment}
        identities = []
        try:
            with running_server(**server, log_path=tmp_path / 'first.log') as api:
                settings = api.post('/api/v1/llmrec_settings/get', {})['result']
                assert not settings['enabled'] and not settings['include_oauth']
                assert settings['proxy']['state'] == 'stopped'
                assert not state_path.exists()
                settings = api.post('/api/v1/llmrec_settings/update', {'enabled': True})['result']
                assert settings['enabled'] and not settings['include_oauth']
                assert settings['proxy']['state'] == 'running'
                first = _until(lambda: (value := _state(state_path)) and _health(value) and value, timeout=20)
                identities.append(first)
                assert first['pid'] != os.getpid()
            assert _health(first), 'serve shutdown stopped the independently running proxy'

            with running_server(**server, log_path=tmp_path / 'second.log') as api:
                reused = _until(lambda: (value := _state(state_path)) and _health(value) and value)
                assert reused['pid'] == first['pid'] and reused['instance'] == first['instance']
                assert proc_tree._start_time(reused['pid']) == reused['start_time']
                os.kill(reused['pid'], signal.SIGKILL)
                # The real serve loop checks every five seconds; allow startup and shared-host scheduling.
                recovered = _until(lambda: (value := _state(state_path)) and value['pid'] != reused['pid']
                                   and _health(value) and value)
                identities.append(recovered)
                assert recovered['port'] == first['port']
                assert recovered['instance'] != first['instance']
                settings = api.post('/api/v1/llmrec_settings/update', {'enabled': False})['result']
                assert not settings['enabled']
                assert _health(recovered), 'disabling recording cut the proxy serving existing routes'
            assert _health(recovered)

            with running_server(**server, log_path=tmp_path / 'third.log') as api:
                settings = api.post('/api/v1/llmrec_settings/get', {})['result']
                assert not settings['enabled'] and settings['proxy']['state'] == 'stopped'
                assert not state_path.exists()
                assert proc_tree._start_time(recovered['pid']) != recovered['start_time']
        finally:
            async def cleanup():
                owner = ProxyControl(runtime, url, enabled=False)
                current = owner._state()
                if current:
                    identities.append(current)
                for state in {value['instance']: value for value in identities}.values():
                    if await owner._running(state):
                        await owner._stop(state)
            asyncio.run(cleanup())
