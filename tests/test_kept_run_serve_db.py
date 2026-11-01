"""Kept task runs on isolated real serve instances and disposable PostgreSQL."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile

import pytest

from evals.prompt_regression.runner import fresh_database, running_server
from looper.agents import keeper, proc_tree
from tests.db_support import DSN
from tests.runner_support import require_disposable_database
from tests.test_drain_serve_db import _CLI, _completed, _events, _project, _query, _task, _until

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

# The Planner and the run peers use the same real CLI entry points as production.
CLI = _CLI.replace(
    'step = re.search(r"^Your step: (\\S+)$", prompt, re.MULTILINE)',
    'step = re.search(r"^(?:Your step|The step under review): (\\S+)$", prompt, re.MULTILINE)',
).replace(
    'log("worker", step=label)',
    'log("worker", step=label, pid=os.getpid(), args=args)\n'
    '    if variant == "review" and "The step under review:" in prompt:\n'
    '        limit = time.monotonic() + 90\n'
    '        while not (state / "release-review").exists():\n'
    '            if time.monotonic() >= limit:\n'
    '                raise RuntimeError("test did not release its Reviewer")\n'
    '            time.sleep(0.05)\n'
    '        answer(json.dumps({"passed": True, "findings": [], "summary": "verified"}))\n'
    '        sys.exit(0)',
).replace(
    'if variant in ("restart", "deadline") and label == "s1":',
    'if variant in ("held", "pair", "plugin"):',
).replace(
    '(state / ("release-" + variant)).exists()', '(state / ("release-" + label)).exists()',
).replace(
    '"review": False', '"review": variant == "review"',
).replace(
    'if variant == "restart" else []', 'if variant == "pair" else []',
).replace(
    'elif text.startswith("[DECISION]"):\n',
    'elif text.startswith("[DECISION]"):\n'
    '        if "keeper process ended unexpectedly" in text.rsplit("Result of step", 1)[-1]:\n'
    '            answer("Done.\\n```json\\n" + json.dumps({"decision": "retry", "feedback": "try again"}) + "\\n```")\n'
    '            continue\n'
    '        if variant == "review" and "Reviewer" not in text.splitlines()[0]:\n'
    '            answer("Done.\\n```json\\n" + json.dumps({"decision": "review", "summary": "verify it"}) + "\\n```")\n'
    '            continue\n',
)


def _live(pid):
    state = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True,
                           text=True, timeout=5)
    assert state.returncode in (0, 1), state.stderr
    return bool(state.stdout.strip()) and not state.stdout.strip().startswith('Z')


@contextmanager
def fixture():
    require_disposable_database(str(DSN), served=False)
    # Short paths are a real Unix socket constraint, including on macOS.
    with tempfile.TemporaryDirectory(prefix='lk.', dir='/tmp') as temporary:
        root = Path(temporary)
        state, bin_dir, runtime, home = (root / p for p in ('state', 'bin', 'runtime', 'home'))
        for directory in (state, bin_dir, runtime, home):
            directory.mkdir()
        script = root / 'fake.py'
        script.write_text(CLI)
        (bin_dir / 'claude').write_text(
            f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(script))} '
            f'{shlex.quote(str(state))} "$@"\n')
        (bin_dir / 'claude').chmod(0o755)
        with fresh_database(str(DSN)) as (url, _):
            server = dict(database_url=url, runtime_root=runtime, bin_dir=bin_dir,
                          home=home, notes_root=home / 'notes', keeper_mode='on')
            try:
                yield root, url, state, server
            finally:
                # Also covers assertion failures while a run is still held.
                async def cleanup():
                    from looper.agents.kept_dirs import KeeperRecord, end_keeper
                    for directory in (runtime / keeper.RUNS_DIR).glob('*'):
                        record = KeeperRecord.read(directory / keeper.STATE)
                        if record:
                            await end_keeper(record, directory)
                asyncio.run(cleanup())


def _running(runtime, count=1, *, role=None):
    def check():
        runs = {}
        for directory in (runtime / keeper.RUNS_DIR).glob('*'):
            state = keeper.read_json(directory / keeper.STATE) or {}
            manifest = keeper.read_json(directory / keeper.MANIFEST) or {}
            if state.get('state') == 'running' and (role is None or manifest.get('role') == role):
                runs[directory.name] = (directory, state)
        return runs if len(runs) == count else None
    return _until(check, f'{count} running keepers')


def _records(url, task):
    return _query(url, "SELECT * FROM llm_records WHERE task_id=$1 AND agent_id LIKE 'worker-%' ORDER BY id", task)


@pytest.mark.parametrize('variant', ['held', 'review'])
def test_a_worker_runs_through_a_keeper(variant):
    with fixture() as (root, url, state, server):
        with running_server(**server, log_path=root / 'serve.log') as api:
            project = _project(api, root, variant)
            task = _task(api, project, variant)
            if variant == 'held':
                runs = _running(server['runtime_root'])
                run_id, (directory, status) = next(iter(runs.items()))
                manifest = keeper.read_json(directory / keeper.MANIFEST)
                assert manifest['record_id'] is not None
                assert manifest['task_id'] == task and manifest['role'] == 'worker'
                record = _records(url, task)[0]
                metadata = json.loads(record['metadata'])
                assert metadata['command'] == manifest['command']
                assert '-I' not in metadata['command']
                process_record = keeper.read_json(server['runtime_root'] / proc_tree.RECORDS_DIR /
                                                f"{status['child_pid']}.json")
                assert process_record['server_pid'] == status['keeper_pid']
                assert process_record['pid'] == status['child_pid']
                assert api.post('/api/v1/system/drain_status', {})['result']['waiting']['runs'][0]['keepable']
                (state / 'release-s1').touch()
            else:
                directory, status = next(iter(_running(server['runtime_root'], role='reviewer').values()))
                assert directory.is_dir() and status['state'] == 'running'
                manifest = keeper.read_json(directory / keeper.MANIFEST)
                assert manifest['record_id'] is not None and manifest['task_id'] == task
                (state / 'release-review').touch()
            _completed(api, task)
            _until(lambda: not list((server['runtime_root'] / keeper.RUNS_DIR).glob('*')), 'released runs')
            if variant == 'review':
                rows = _query(url, "SELECT status FROM llm_records WHERE task_id=$1 AND agent_id LIKE 'reviewer-%'", task)
                assert rows and all(row['status'] == 'completed' for row in rows)
            assert all(row['status'] == 'completed' for row in _records(url, task))


def test_killing_one_keeper_affects_only_its_run():
    with fixture() as (root, url, state, server):
        with running_server(**server, log_path=root / 'serve.log') as api:
            task = _task(api, _project(api, root, 'pair'), 'pair')
            runs = _running(server['runtime_root'], 2)
            _until(lambda: len(_events(state, 'worker', 'pair')) == 2, 'both peers reading their prompts')
            rows = {keeper.read_json(directory / keeper.MANIFEST)['step_key']: (directory, status)
                    for directory, status in runs.values()}
            children = [status['child_pid'] for _, status in rows.values()]
            pids = [pid for _, status in rows.values() for pid in (status['keeper_pid'], status['child_pid'])]
            evidence = subprocess.run(['ps', '-o', 'pid=,pgid=,sess=,command=', '-p', ','.join(map(str, pids))],
                                      capture_output=True, text=True, timeout=5, check=True).stdout
            print('pid pgid sess sid command')
            for line in evidence.splitlines():
                pid, pgid, sess, command_line = line.split(maxsplit=3)
                role = 'keeper' if keeper.BOOT in command_line else 'claude'
                print(pid, pgid, sess, os.getsid(int(pid)), role, command_line.split()[0])
            assert {int(line.split()[0]) for line in evidence.splitlines()} == set(pids)
            # On macOS sess is an opaque session id; pgid==pid proves each started its own group.
            assert all(line.split()[0] == line.split()[1] for line in evidence.splitlines())
            assert all(os.getsid(pid) == pid for pid in pids)
            os.kill(rows['s1'][1]['keeper_pid'], signal.SIGKILL)
            (state / 'release-s2').touch()
            _until(lambda: len([row for row in _events(state, 'worker', 'pair') if row['step'] == 's1']) == 2,
                   'the Planner retrying the failed run')
            (state / 'release-s1').touch()
            detail = _completed(api, task)
            assert all(step['status'] == 'verified' for step in detail['steps'])
            records = _records(url, task)
            failed = [row for row in records if row['status'] == 'failed']
            assert len(failed) == 1
            assert 'keeper process ended unexpectedly' in failed[0]['error']
            assert sum(row['status'] == 'completed' for row in records) == 2
            assert all(not _live(pid) for pid in children)


def test_a_shutdown_ends_kept_runs():
    with fixture() as (root, url, state, server):
        with running_server(**server, log_path=root / 'serve.log') as api:
            task = _task(api, _project(api, root, 'held'), 'held')
            _, status = next(iter(_running(server['runtime_root']).values()))
            holder = _query(url, "SELECT application_name FROM pg_stat_activity WHERE datname=current_database() AND application_name LIKE 'looper serve %'")[0]
            serve_pid = int(holder['application_name'].split()[2].split('@')[0])
            os.kill(serve_pid, signal.SIGTERM)
            _until(lambda: not _live(serve_pid), 'serve stopping')
        assert not _live(status['keeper_pid']) and not _live(status['child_pid'])
        assert _records(url, task)[0]['status'] == 'interrupted'


def test_stopping_a_task_releases_its_kept_run_directory():
    with fixture() as (root, url, state, server):
        with running_server(**server, log_path=root / 'serve.log') as api:
            task = _task(api, _project(api, root, 'held'), 'held')
            directory, status = next(iter(_running(server['runtime_root']).values()))
            _until(lambda: _events(state, 'worker', 'held'), 'the Worker reading its prompt')
            assert (directory / keeper.MANIFEST).is_file()
            assert api.post('/api/v1/ops/task_control/stop', {'task_id': task})['result']['stopped']
            _until(lambda: not _live(status['keeper_pid']) and not _live(status['child_pid']), 'the run ending')
            _until(lambda: not directory.exists(), 'the cancelled run directory being released', seconds=5)
            assert _records(url, task)[0]['status'] == 'interrupted'


@pytest.mark.parametrize('scope', ['turns', 'runs'])
def test_only_a_runs_drain_waits_for_a_live_kept_run(scope):
    with fixture() as (root, url, state, server):
        with running_server(**server, log_path=root / 'serve.log') as api:
            task = _task(api, _project(api, root, 'held'), 'held')
            _running(server['runtime_root'])
            _until(lambda: _events(state, 'worker', 'held'), 'the Worker reading its prompt')
            waiting = _until(
                lambda: (status := api.post('/api/v1/system/drain_status', {})['result'])
                and not status['waiting']['planner_turns'] and status, 'the Planner finishing its turn')
            assert waiting['waiting']['runs'][0]['keepable']
            assert not any(value for key, value in waiting['waiting'].items() if key != 'runs')
            draining = api.post('/api/v1/system/drain', {'scope': scope})['result']
            phase = 'drained' if scope == 'turns' else 'draining'
            assert draining['phase'] == phase
            assert draining['waiting']['runs'] == waiting['waiting']['runs']
            assert api.post('/api/v1/system/drain_status', {})['result']['phase'] == phase
            (state / 'release-s1').touch()
            _until(lambda: api.post('/api/v1/system/drain_status', {})['result']['phase'] == 'drained',
                   'the drain finishing after the run')
            _completed(api, task)
            assert _records(url, task)[0]['status'] == 'completed'


def test_keeper_off_starts_no_keeper():
    snapshots = []
    with fixture() as (root, url, state, server):
        for mode in ('on', 'off'):
            server['keeper_mode'] = mode
            server['runtime_root'] = root / mode
            with running_server(**server, log_path=root / f'{mode}.log') as api:
                task = _task(api, _project(api, root, mode), 'quick')
                _completed(api, task)
                record = _records(url, task)[0]
                activities = _query(url, 'SELECT kind FROM activity WHERE task_id=$1 ORDER BY seq', task)
                snapshots.append(([record[key] for key in ('status', 'cost_usd', 'cost_source', 'input_tokens', 'output_tokens',
                                                                          'cache_read_tokens', 'cache_creation_tokens', 'context_tokens')],
                                  [row['kind'] for row in activities]))
                if mode == 'off':
                    assert not (server['runtime_root'] / keeper.RUNS_DIR).exists()
        assert snapshots[0] == snapshots[1]


def test_a_live_kept_runs_copies_survive_reclaim():
    with fixture() as (root, url, state, server):
        with running_server(**server, log_path=root / 'serve.log') as api:
            project = _project(api, root, 'plugin')
            api.post('/api/v1/project_skills/add', {'project_id': project, 'skill': {
                'name': 'keep-copy', 'description': 'Fixture skill.', 'content': 'Read the fixture.'}})
            task = _task(api, project, 'plugin')
            directory, status = next(iter(_running(server['runtime_root']).values()))
            manifest = keeper.read_json(directory / keeper.MANIFEST)
            command = manifest['command']
            plugin = Path(command[command.index('--plugin-dir') + 1])
            assert plugin.is_dir()
            # Remove its current install so only the live argv can retain this copy.
            _query(url, 'DELETE FROM library_installs WHERE project_id=$1', project, execute=True)
            async def reclaim():
                from looper.db import connection
                from looper.agents.read_only_copies import reclaim_unused
                await connection.init_pool(url)
                try:
                    return await reclaim_unused(server['runtime_root'])
                finally:
                    await connection.close_pool()
            assert plugin not in asyncio.run(reclaim())
            assert plugin.is_dir() and _live(status['child_pid'])
            (state / 'release-s1').touch()
            _completed(api, task)
