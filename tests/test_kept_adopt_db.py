"""A second pool resumes the same kept process, record, stream and reply."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import random
import shutil
import signal
import string
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import keeper, kept_dirs, proc_tree
from looper.agents.agent_settings import AgentSettings, AgentType
from looper.agents.bus import MessageBus
from looper.agents.drain import DrainScope, DrainState
from looper.agents.kept_run import AdoptFailure, AdoptOutcome, DetachKind, KeeperSettings, KeptRun, RunHold
from looper.agents.message import MessageEnvelope, MessageType, planner_address, worker_address
from looper.agents.ops.room import RoomOps
from looper.agents.profile_registry import BUILTIN_RUNTIME, LegacyProviderSpec
from looper.agents.room import MemberKind, RoomService, Seat
from looper.agents.runtimes import ApiProtocol, AuthKind
from looper.agents.runtimes.channel import DeliveryResult
from looper.agents.tiers import worker
from looper.agents.tiers.planner import PlannerAgent
from looper.db import llm_records
from tests.db_support import DSN, run_db, until
from tests.fake_claude import fake_claude
from tests.runtime_model_helpers import builtin_launch, resolved_profile, with_model_name

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
ROOT = Path(__file__).resolve().parents[1]

# The initial input is echoed just as --replay-user-messages does. A tool call
# remains in flight across detach; every later stdin line is echoed as well.
BODY = """\
import json, select, sys, time
from pathlib import Path

def emit(event):
    print(json.dumps(event), flush=True)

first = json.loads(sys.stdin.readline())
reminders = []
emit({'type': 'system', 'subtype': 'init', 'session_id': 'adopt-test'})
emit(first)
emit({'type': 'assistant', 'message': {'content': [
    {'type': 'tool_use', 'id': 'tool-one', 'name': 'Read', 'input': {'file_path': 'sample.txt'}}]}})
while not Path('release').exists():
    if Path('gap-usage').exists():
        Path('gap-usage').unlink()
        emit({'type': 'usage', 'model': 'claude-opus-5-5', 'usage': {'input_tokens': 1000000}})
        Path('gap-usage-written').touch()
    readable, _, _ = select.select([sys.stdin], [], [], .02)
    if readable:
        line = sys.stdin.readline()
        if not line:
            break
        message = json.loads(line)
        if str(message.get('uuid', '')).startswith('looper-reminder-'):
            reminders.append(message['uuid'])
        emit(message)
emit({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'Finished the held work.'}]}})
emit({'type': 'result', 'subtype': 'success', 'is_error': False, 'total_cost_usd': .375,
      'result': json.dumps({'status': 'completed', 'summary': 'done', 'changed_files': {}})})
Path('result-written').touch()
if Path('reminder-turn').exists():
    while not Path('reminder-result').exists():
        time.sleep(.02)
    emit({'type': 'result', 'subtype': 'success', 'is_error': False, 'total_cost_usd': .625,
          'user_message_uuids': reminders,
          'result': json.dumps({'status': 'completed', 'summary': 'only a reminder', 'changed_files': {}})})
if Path('delay-exit').exists():
    time.sleep(3)
    emit({'type': 'result', 'subtype': 'success', 'is_error': False, 'total_cost_usd': .625,
          'result': json.dumps({'status': 'completed', 'summary': 'teammate finished', 'changed_files': {}})})
"""


def _short_directory() -> Path:
    for suffix in random.sample(string.ascii_letters + string.digits, 62):
        directory = ROOT / f'.k{suffix}'
        if not directory.exists():
            assert len(os.fsencode(directory / keeper.SOCKET)) <= keeper.SOCKET_PATH_MAX
            return directory
    raise AssertionError('no short keeper directory is available')


def _live(pid: int) -> bool:
    result = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True,
                            text=True, check=False, timeout=5)
    assert result.returncode in (0, 1), result.stderr
    return bool(result.stdout.strip()) and not result.stdout.strip().startswith('Z')


async def _stored(db, count: int) -> None:
    async with asyncio.timeout(10):
        while await db.fetchval('SELECT count(*) FROM llm_record_events') < count:
            await asyncio.sleep(.02)


@asynccontextmanager
async def _pools(db, tmp_path, monkeypatch, *, timeout=0, cap=None, rooms=False):
    run_dir = _short_directory()
    run_id = run_dir.name
    saved = {}
    handles = []

    class ShortKeptRun(KeptRun):
        def __init__(self, *args, **kwargs):
            kwargs['run_dir'] = run_dir
            kwargs['runtime_root'] = tmp_path / 'runtime'
            super().__init__(*args, **kwargs)
            handles.append(self)

        def release(self):
            if (self.run_dir / keeper.STDOUT).exists():
                saved['stdout'] = (self.run_dir / keeper.STDOUT).read_bytes()
            super().release()

    launch = builtin_launch()
    settings = replace(launch.settings, max_budget_usd=cap)
    if cap is not None:
        profile = resolved_profile(
            "profile-priced", "priced Claude", BUILTIN_RUNTIME,
            LegacyProviderSpec("provider-priced", "test account", AuthKind.API_KEY,
                               frozenset({ApiProtocol.ANTHROPIC_MESSAGES}),
                               base_url="http://127.0.0.1:1", api_key="test-key"),
            "claude-opus-5-5",
        )
        settings = AgentSettings(profile=profile, max_budget_usd=cap)
    load = AsyncMock(side_effect=lambda *args, **kwargs: settings)
    monkeypatch.setattr(worker, 'KeptRun', ShortKeptRun)
    monkeypatch.setattr(worker, 'load_agent_settings', load)
    monkeypatch.setattr(worker, 'load_task_limit', AsyncMock(return_value=1))
    monkeypatch.setattr(worker, '_load_settings', AsyncMock(return_value=worker.WorkerSettings(timeout, 0, 'work')))
    monkeypatch.setattr(worker.WorkerPool, '_task_project', AsyncMock(return_value=None))
    await db.execute("INSERT INTO projects (id, name) VALUES ('project-adopt', 'Adoption test')")
    await db.execute("INSERT INTO tasks (id, requirement, work_dir, project_id) "
                     "VALUES ('task-adopt', 'hold a run', $1, 'project-adopt')", str(tmp_path))
    executable = fake_claude(tmp_path / 'claude', BODY)
    bus = MessageBus(runtime_root=tmp_path / 'runtime')
    results = []
    bus.subscribe_topic(MessageType.WORK_RESULT, results.append)
    drains = [DrainState(), DrainState()]
    pools = [worker.WorkerPool(config=worker.WorkerConfig(claude_bin=executable),
                               runtime_root=tmp_path / 'runtime', drain=drains[i], keeper=KeeperSettings(i + 1))
             for i in range(2)]
    if rooms:
        for pool in pools:
            pool.room_ops = RoomOps(RoomService(worker_pool=pool, batch_seconds=0), url='http://127.0.0.1:9')
    envelope = MessageEnvelope.create(
        source=planner_address('original-planner'), target=worker_address(run_id),
        message_type=MessageType.WORK_ITEM, correlation_id='dispatch-correlation', metadata={'dispatch': 'original'},
        payload={'task_id': 'step-adopt', 'parent_task_id': 'task-adopt', 'plan_id': 'plan-adopt',
                 'step_key': 'implement', 'instruction': 'finish the held work',
                 'context': {'work_dir': str(tmp_path)}},
    )
    case = SimpleNamespace(a=pools[0], b=pools[1], run_dir=run_dir, run_id=run_id, results=results,
                           settings=settings, load=load, saved=saved, envelope=envelope,
                           expect=RunHold('task-adopt', 'step-adopt', AgentType.WORKER), db=db, root=tmp_path)
    try:
        for pool in pools:
            await pool.start(bus)
        await pools[0]._dispatch(envelope)
        await _stored(db, 3)
        case.original = pools[0]._active[run_id].handle
        case.identity = keeper.read_json(run_dir / keeper.STATE)
        case.started_at = pools[0]._active[run_id].since
        yield case
    finally:
        (tmp_path / 'release').touch()
        for pool in pools:
            await pool.stop()
        await bus.shutdown()
        # A detached old handle deliberately does not own shutdown; clean the
        # actual process identity even if adoption raised before taking ownership.
        identity = getattr(case, 'identity', keeper.read_json(run_dir / keeper.STATE) or {})
        child, child_started = identity.get('child_pid'), identity.get('child_started')
        if child and child_started and _live(child):
            await proc_tree.end_orphan(child, child_started, label='adoption test cleanup')
        pid, started = identity.get('keeper_pid'), identity.get('keeper_started')
        if pid and started and _live(pid) and proc_tree._start_time(pid) == started:
            os.kill(pid, signal.SIGTERM)
            await until(lambda: not _live(pid), 'keeper to exit', timeout=10)
        for handle in handles:
            if handle._keeper_process is not None:
                await asyncio.wait_for(handle._keeper_process.wait(), 5)
        shutil.rmtree(run_dir, ignore_errors=True)
        assert not child or not _live(child), 'test left the Claude stand-in running'
        assert not pid or not _live(pid), 'test left its keeper running'


async def _detach(case):
    assert case.a._drain.begin(DrainScope.TURNS)['phase'] == 'drained'
    assert await case.a.detach_kept(epoch=1) == {DetachKind.DETACHED: [case.run_id]}
    assert not case.results
    return keeper.read_json(case.run_dir / keeper.DETACH)


async def _adopt(case):
    answer = await case.b.adopt_dir(case.run_dir, expect=case.expect)
    assert answer.outcome is AdoptOutcome.ADOPTED, answer
    assert not (case.run_dir / keeper.DETACH).exists()
    return case.b._active[case.run_id].handle


async def _finished(case):
    await until(lambda: len(case.results) == 1, 'one adopted result')
    await until(lambda: case.run_id not in case.b._tasks, 'adopted run cleanup')
    result = case.results[0]
    assert result.target == case.envelope.source
    assert result.correlation_id == case.envelope.correlation_id
    assert result.metadata == case.envelope.metadata
    assert result.payload['task_id'] == 'step-adopt'
    assert result.payload['plan_id'] == 'plan-adopt'
    assert not case.b._inputs and not case.b._slots
    return result


def test_an_adopted_run_continues_its_record(tmp_path, monkeypatch):
    async def run(db):
        async with _pools(db, tmp_path, monkeypatch) as case:
            await _detach(case)
            await _adopt(case)
            (tmp_path / 'release').touch()
            result = await _finished(case)
            assert result.payload['status'] == 'completed'
            records = await db.fetch('SELECT * FROM llm_records')
            assert len(records) == 1
            assert records[0]['status'] == 'completed'
            assert float(records[0]['cost_usd']) == .375
            events = await db.fetch('SELECT seq, event FROM llm_record_events ORDER BY seq')
            assert [row['seq'] for row in events] == list(range(1, len(events) + 1))
            assert [json.loads(row['event']) for row in events] == [
                json.loads(line) for line in case.saved['stdout'].splitlines()]
            activity = await db.fetch('SELECT kind, summary FROM activity ORDER BY seq')
            assert sum(row['summary'] == 'Worker started execution' for row in activity) == 1
            assert sum(row['kind'] == 'tool_use' and not row['summary'].startswith('$ ')
                       for row in activity) == 1
            assert sum(row['summary'].startswith('Worker finished') for row in activity) == 1
            assert sum(row['kind'] == 'text' for row in activity) == 1
            assert len(case.results) == 1

    run_db(run, monkeypatch, timeout=35)


def _candidate(case):
    manifest = keeper.read_json(case.run_dir / keeper.MANIFEST)
    grant = keeper.read_json(case.run_dir / keeper.GRANT)
    assert grant is not None, 'the real room grant must have been persisted at launch'
    candidate = kept_dirs.Candidate(case.run_dir, case.run_id, case.expect, manifest['record_id'], grant)
    case.b.room_ops.import_pending(grant)
    case.b.set_candidates([candidate])
    return candidate


def test_killing_a_candidate_ends_it(tmp_path, monkeypatch):
    async def run(db):
        async with _pools(db, tmp_path, monkeypatch, rooms=True) as case:
            await _detach(case)
            candidate = _candidate(case)
            assert case.b.room_ops.export_run(case.run_id) is not None
            assert case.b.keeper_status()['pending'] == 1

            assert await case.b.kill(case.run_id)

            await asyncio.wait_for(case.original._keeper_process.wait(), 5)
            assert not _live(case.identity['child_pid'])
            assert not _live(case.identity['keeper_pid'])
            assert await db.fetchval('SELECT status FROM llm_records') == 'interrupted'
            assert await db.fetchval('SELECT error FROM llm_records') == (
                'its task stopped it before the run was adopted')
            assert case.b.room_ops.export_run(case.run_id) is None
            assert not any(Path(path).exists() for path in candidate.grant['mcp_files'])
            assert not case.run_dir.exists()
            assert case.b.keeper_status()['pending'] == 0
            assert not case.results
            assert not await case.b.kill(case.run_id)

    run_db(run, monkeypatch, timeout=35)


def test_unadopted_candidates_expire(tmp_path, monkeypatch):
    async def run(db):
        async with _pools(db, tmp_path, monkeypatch, rooms=True) as case:
            await _detach(case)
            candidate = _candidate(case)

            await db.execute("UPDATE tasks SET status = 'running' WHERE id = 'task-adopt'")
            await db.execute("INSERT INTO task_steps (id, task_id, step_index, step_key, instruction, status, "
                             "worker_id) VALUES ('step-adopt', 'task-adopt', 0, 'implement', 'held work', "
                             "'dispatched', $1), ('step-observer', 'task-adopt', 1, 'observer', 'watch', "
                             "'dispatched', 'worker-observer')", case.run_id)
            room = case.b.room_ops.room
            seat_data = candidate.grant['seat']
            seat = Seat(**{**seat_data, 'kind': MemberKind(seat_data['kind'])})
            observer = Seat('worker@observer', 'worker', MemberKind.WORK, run='worker-observer')
            own_watch = await room.watch('task-adopt', seat, kind='announce', until='run_end')
            offline_watch = await room.watch('task-adopt', observer, presence=seat.key, state='offline')

            await case.b.expire_candidates(window=.01)

            for watch in (own_watch, offline_watch):
                assert await db.fetchval('SELECT ended_at IS NOT NULL FROM room_watches WHERE id = $1',
                                         watch['watch'])
            notices = await db.fetch("SELECT body, audience FROM room_messages WHERE reason = 'watch'")
            assert len(notices) == 1
            assert notices[0]['audience'] == [observer.key]
            assert f'{seat.key} is offline' in notices[0]['body']

            await asyncio.wait_for(case.original._keeper_process.wait(), 5)
            assert not _live(case.identity['child_pid'])
            assert not _live(case.identity['keeper_pid'])
            assert await db.fetchval('SELECT status FROM llm_records') == 'interrupted'
            assert await db.fetchval('SELECT error FROM llm_records') == 'not_adopted'
            assert case.b.room_ops.export_run(case.run_id) is None
            assert not any(Path(path).exists() for path in candidate.grant['mcp_files'])
            assert not case.run_dir.exists()
            assert case.b.keeper_status() == {
                'mode': 'on', 'attached': 0, 'candidates_at_start': 1, 'adopted_at_start': 0,
                'pending': 0, 'ended_at_start': {'not_adopted': 1},
            }
            assert not case.results

    run_db(run, monkeypatch, timeout=35)


def test_failed_adoption_waits_for_keeper_end_before_repeating_the_step(tmp_path, monkeypatch):
    async def run(db):
        async with _pools(db, tmp_path, monkeypatch, rooms=True) as case:
            await _detach(case)
            _candidate(case)
            end_keeper = kept_dirs.end_keeper
            attempts = 0

            async def end(*args):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise OSError('transient ps failure')
                await end_keeper(*args)

            monkeypatch.setattr(kept_dirs, 'end_keeper', end)
            monkeypatch.setattr(worker.KeptRun, 'attach', AsyncMock(side_effect=OSError('attach failed')))
            outcomes = []

            async def adopt(*args):
                result = await case.b.adopt(*args)
                outcomes.append(result.outcome.value)
                return result

            planner = PlannerAgent(task_id='task-adopt', runtime_root=tmp_path / 'runtime', adopter=adopt)
            planner._bus = case.b._bus
            step = SimpleNamespace(step_id='step-adopt', status='dispatched', worker_id=case.run_id)
            planner._plan = SimpleNamespace(status='executing', steps=[step], has_failed=False,
                                            is_complete=False, progress_summary=lambda: 'running')
            for name in ('_activity', '_dispatch_step', '_dispatch_ready', '_report_progress'):
                setattr(planner, name, AsyncMock())
            planner._within_budget = AsyncMock(return_value=True)
            resumes = []
            case.b._bus.subscribe_topic(MessageType.TASK_RESUME, resumes.append)
            resume = MessageEnvelope.create(source=planner_address('old'), target=planner.agent_address,
                                             message_type=MessageType.TASK_RESUME, payload={})

            await planner._resume(resume)

            assert outcomes == ['end_pending']
            planner._dispatch_step.assert_not_awaited()
            assert _live(case.identity['child_pid'])
            assert _live(case.identity['keeper_pid'])
            assert (case.run_dir / keeper.DETACH).exists()
            assert await db.fetchval('SELECT status FROM llm_records') == 'running'
            assert case.b.room_ops.export_run(case.run_id) is not None
            # A second resume while termination is pending cannot attach or repeat it either.
            await planner._resume(resume)
            assert outcomes == ['end_pending', 'end_pending']
            assert attempts == 1
            planner._dispatch_step.assert_not_awaited()

            await case.b.expire_candidates(window=.01)

            await asyncio.wait_for(case.original._keeper_process.wait(), 5)
            assert not _live(case.identity['child_pid'])
            assert not _live(case.identity['keeper_pid'])
            assert await db.fetchval('SELECT status FROM llm_records') == 'interrupted'
            assert case.b.room_ops.export_run(case.run_id) is None
            assert not case.run_dir.exists()
            assert len(resumes) == 1
            assert resumes[0].target == planner.agent_address
            await planner._resume(resumes[0])
            planner._dispatch_step.assert_awaited_once_with(planner._plan, step, same_attempt=True)

    run_db(run, monkeypatch, timeout=35)


@pytest.mark.parametrize('failure', ['keeper', 'record'])
def test_expiry_retries_transient_failures_until_cleanup_finishes(tmp_path, monkeypatch, failure):
    async def run(db):
        async with _pools(db, tmp_path, monkeypatch, rooms=True) as case:
            await _detach(case)
            candidate = _candidate(case)
            target, name = (kept_dirs, 'end_keeper') if failure == 'keeper' else (llm_records, 'end_record_interrupted')
            original = getattr(target, name)
            calls = 0

            async def transient(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    assert case.b.keeper_status()['pending'] == 1
                    assert case.b.room_ops.export_run(case.run_id) is not None
                    assert await db.fetchval('SELECT status FROM llm_records') == 'running'
                    raise OSError('transient cleanup failure')
                return await original(*args, **kwargs)

            monkeypatch.setattr(target, name, transient)
            await case.b.expire_candidates(window=.01)

            assert calls == 2
            await asyncio.wait_for(case.original._keeper_process.wait(), 5)
            assert not _live(case.identity['child_pid'])
            assert not _live(case.identity['keeper_pid'])
            assert await db.fetchval('SELECT status FROM llm_records') == 'interrupted'
            assert case.b.room_ops.export_run(case.run_id) is None
            assert not any(Path(path).exists() for path in candidate.grant['mcp_files'])
            assert not case.run_dir.exists()
            assert case.b.keeper_status()['pending'] == 0
            assert case.b.keeper_status()['ended_at_start'] == {'not_adopted': 1}

    run_db(run, monkeypatch, timeout=35)


def test_expiry_releases_a_closed_run_even_if_directory_removal_fails(tmp_path, monkeypatch):
    async def run(db):
        async with _pools(db, tmp_path, monkeypatch, rooms=True) as case:
            await _detach(case)
            _candidate(case)
            resumes = []
            case.b._bus.subscribe_topic(MessageType.TASK_RESUME, resumes.append)
            remove = shutil.rmtree

            def partial_remove(path, *args, **kwargs):
                if Path(path) == case.run_dir:
                    (case.run_dir / keeper.STATE).unlink()
                    raise OSError('partial directory removal')
                return remove(path, *args, **kwargs)

            with monkeypatch.context() as patch:
                patch.setattr(worker.shutil, 'rmtree', partial_remove)
                await case.b.expire_candidates(window=.01)

            await asyncio.wait_for(case.original._keeper_process.wait(), 5)
            assert not _live(case.identity['child_pid'])
            assert not _live(case.identity['keeper_pid'])
            assert await db.fetchval('SELECT status FROM llm_records') == 'interrupted'
            assert case.b.room_ops.export_run(case.run_id) is None
            assert case.b.keeper_status()['pending'] == 0
            assert len(resumes) == 1 and resumes[0].payload == {'ended_run_id': case.run_id}
            assert (await case.b.adopt(case.run_id, case.expect)).outcome is AdoptOutcome.NOT_CANDIDATE

    run_db(run, monkeypatch, timeout=35)


def test_room_reminders_survive_adoption(tmp_path, monkeypatch):
    async def run(db):
        (tmp_path / 'reminder-turn').touch()
        async with _pools(db, tmp_path, monkeypatch, rooms=True) as case:
            assert await case.a.remind(case.run_id, 'The room has a message for you')
            await _stored(db, 4)
            stdin = [json.loads(line) for line in (case.run_dir / keeper.STDIN).read_text().splitlines()]
            reminders = {message['uuid'] for message in stdin if str(message.get('uuid', '')).startswith(
                'looper-reminder-')}
            assert len(reminders) == 1
            (tmp_path / 'release').touch()
            await _stored(db, 6)
            await _detach(case)
            _candidate(case)

            adopted = await case.b.adopt(case.run_id, case.expect)
            assert adopted.outcome is AdoptOutcome.ADOPTED
            assert case.b._reminders[case.run_id].uuids == reminders
            (tmp_path / 'reminder-result').touch()
            result = (await _finished(case)).payload

            assert result['status'] == 'completed'
            assert result['summary'] == 'done'
            assert await db.fetchval('SELECT count(*) FROM llm_records') == 1
            assert await db.fetchval('SELECT status FROM llm_records') == 'completed'
            assert float(await db.fetchval('SELECT cost_usd FROM llm_records')) == .625
            events = [json.loads(row['event']) for row in await db.fetch(
                'SELECT event FROM llm_record_events ORDER BY seq')]
            assert events[-1]['user_message_uuids'] == list(reminders)
            assert json.loads(events[-1]['result'])['summary'] == 'only a reminder'
            assert case.b.room_ops.export_run(case.run_id) is None

    run_db(run, monkeypatch, timeout=35)


def test_steering_reaches_an_adopted_run(tmp_path, monkeypatch):
    async def run(db):
        async with _pools(db, tmp_path, monkeypatch) as case:
            detached = await _detach(case)
            previous = (case.run_dir / keeper.STDIN).read_bytes()
            assert len(previous) == detached['stdin_offset']
            await _adopt(case)
            assert await case.b.send_input(case.run_id, 'continue with this correction') is DeliveryResult.ACCEPTED
            await _stored(db, 4)
            written = (case.run_dir / keeper.STDIN).read_bytes()
            assert written.startswith(previous)
            turn = json.loads(written[len(previous):])
            assert turn['message']['content'] == 'continue with this correction'
            (tmp_path / 'release').touch()
            assert (await _finished(case)).payload['status'] == 'completed'
            echoes = [json.loads(row['event']) for row in await db.fetch('SELECT event FROM llm_record_events')]
            assert turn in echoes

    run_db(run, monkeypatch, timeout=35)


def test_the_timeout_counts_from_the_original_start(tmp_path, monkeypatch):
    async def run(db):
        async with _pools(db, tmp_path, monkeypatch, timeout=3) as case:
            async def at_age(age):
                remaining = age - (datetime.now(timezone.utc) - case.started_at).total_seconds()
                if remaining > 0:
                    await asyncio.sleep(remaining)
            await at_age(1)
            await _detach(case)
            await at_age(2.5)
            started = asyncio.get_running_loop().time()
            await _adopt(case)
            result = await _finished(case)
            elapsed = asyncio.get_running_loop().time() - started
            assert elapsed < 1.5, f'adoption restarted the original timeout: {elapsed:.2f} s'
            assert '3 s limit (timeout_seconds)' in result.payload['error']
            assert await db.fetchval('SELECT status FROM llm_records') == 'failed'

    run_db(run, monkeypatch, timeout=35)


def test_the_spend_cap_stops_at_the_first_live_event(tmp_path, monkeypatch):
    async def run(db):
        async with _pools(db, tmp_path, monkeypatch, cap=.01) as case:
            await _detach(case)
            (tmp_path / 'gap-usage').touch()
            await until(lambda: (tmp_path / 'gap-usage-written').exists(), 'gap usage output')
            # Changing the configured cap applies to a future run, not this one.
            case.load.side_effect = lambda *args, **kwargs: replace(case.settings, max_budget_usd=100)
            await _adopt(case)
            result = await _finished(case)
            assert result.payload['status'] == 'failed'
            assert 'per-run spend cap' in result.payload['error']
            assert '$0.01 cap' in result.payload['error']
            activity = await db.fetch('SELECT kind, summary FROM activity')
            assert sum(row['kind'] == 'tool_use' and not row['summary'].startswith('$ ')
                       for row in activity) == 1
            assert not any(row['kind'] == 'text' for row in activity)

    run_db(run, monkeypatch, timeout=35)


def test_a_run_that_finished_during_the_gap_is_reported_once(tmp_path, monkeypatch):
    async def run(db):
        async with _pools(db, tmp_path, monkeypatch) as case:
            await _detach(case)
            (tmp_path / 'release').touch()
            await asyncio.wait_for(case.original._keeper_process.wait(), 10)
            assert keeper.read_json(case.run_dir / keeper.STATE)['state'] == keeper.KeeperState.EXITED
            handle = await _adopt(case)
            assert handle._process.stdin.is_closing()
            assert (await _finished(case)).payload['status'] == 'completed'
            assert await db.fetchval('SELECT count(*) FROM llm_records') == 1
            assert await db.fetchval('SELECT status FROM llm_records') == 'completed'
            assert len(case.results) == 1

    run_db(run, monkeypatch, timeout=35)


def test_a_result_consumed_before_detach_keeps_later_output(tmp_path, monkeypatch):
    async def run(db):
        async with _pools(db, tmp_path, monkeypatch) as case:
            (tmp_path / 'delay-exit').touch()
            (tmp_path / 'release').touch()
            await _stored(db, 5)
            assert (tmp_path / 'result-written').exists()
            assert _live(case.identity['child_pid'])
            detached = await _detach(case)
            output = (case.run_dir / keeper.STDOUT).read_bytes()
            assert detached['consumed']['stdout'] == len(output)
            assert json.loads(output.splitlines()[-1])['type'] == 'result'
            await _adopt(case)
            result = (await _finished(case)).payload
            assert result['status'] == 'completed'
            assert result['summary'] == 'teammate finished'
            assert await db.fetchval('SELECT status FROM llm_records') == 'completed'
            assert float(await db.fetchval('SELECT cost_usd FROM llm_records')) == .625
            events = await db.fetch('SELECT event FROM llm_record_events ORDER BY seq')
            assert [json.loads(row['event']) for row in events] == [
                json.loads(line) for line in case.saved['stdout'].splitlines()]
            assert len(case.results) == 1
            assert not _live(case.identity['child_pid'])

    run_db(run, monkeypatch, timeout=35)


def test_a_stale_adoption_preserves_the_newer_controllers_keeper_and_record(tmp_path, monkeypatch):
    from looper.agents.keeper.client import KeeperClient

    async def run(db):
        async with _pools(db, tmp_path, monkeypatch) as case:
            await _detach(case)
            newer = KeeperClient()
            try:
                await newer.connect(case.run_dir / keeper.SOCKET)
                await newer.initialize(epoch=3, looper_version='test')
                answer = await case.b.adopt_dir(case.run_dir, expect=case.expect)
                assert answer.outcome is AdoptOutcome.END_PENDING
                assert answer.failure is AdoptFailure.PROTOCOL
                assert _live(case.identity['child_pid'])
                assert _live(case.identity['keeper_pid'])
                assert (case.run_dir / keeper.DETACH).exists()
                assert await db.fetchval('SELECT status FROM llm_records') == 'running'
                assert not case.results
            finally:
                await newer.close()
                await newer.wait_closed()

    run_db(run, monkeypatch, timeout=35)


def _adoption_failure(tmp_path, monkeypatch, *, closed):
    async def run(db):
        async with _pools(db, tmp_path, monkeypatch) as case:
            await _detach(case)
            if closed:
                await db.execute("UPDATE llm_records SET status = 'failed'")
                expected, status = AdoptFailure.RECORD_NOT_RUNNING, 'failed'
            else:
                changed = replace(case.settings, profile=with_model_name(case.settings.profile, 'sonnet'))
                case.load.side_effect = lambda *args, **kwargs: changed
                expected, status = AdoptFailure.LAUNCH_CHANGED, 'interrupted'
            answer = await case.b.adopt_dir(case.run_dir, expect=case.expect)
            assert answer.outcome is AdoptOutcome.FAILED
            assert answer.failure is expected
            assert not case.run_dir.exists()
            assert not case.b._active and not case.b._slots and not case.b._tasks
            assert not _live(case.identity['child_pid'])
            await asyncio.wait_for(case.original._keeper_process.wait(), 5)
            assert not _live(case.identity['keeper_pid'])
            assert await db.fetchval('SELECT status FROM llm_records') == status
            if not closed:
                error = await db.fetchval('SELECT error FROM llm_records')
                assert 'could not be adopted after a restart' in error
                assert expected.value in error
            assert not case.results

    run_db(run, monkeypatch, timeout=35)


def test_a_changed_launch_fails_adoption_cleanly(tmp_path, monkeypatch):
    _adoption_failure(tmp_path, monkeypatch, closed=False)


def test_a_closed_record_fails_adoption_cleanly(tmp_path, monkeypatch):
    _adoption_failure(tmp_path, monkeypatch, closed=True)
