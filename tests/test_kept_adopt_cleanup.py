"""Adoption failure cleanup and the replay boundary keep their observable outcomes."""

from __future__ import annotations

import asyncio
import signal
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from looper.agents import keeper, kept_dirs, proc_tree
from looper.agents.agent_settings import AgentType
from looper.agents.claude_cli import CapStop, soft_cap_result
from looper.agents.kept_run import AdoptFailure, AdoptOutcome, AdoptResult, KeeperSettings, KeptRun, KeptRunState, RunHold
from looper.agents.keeper.protocol import ErrorCode, KeeperError
from looper.agents.message import planner_address
from looper.agents.run_usage import Usage
from looper.agents.runtimes import CLAUDE_CODE
from looper.agents.tiers import worker
from looper.db import llm_records
from tests.runtime_model_helpers import builtin_launch


@pytest.mark.parametrize('outcome', [AdoptOutcome.FAILED, AdoptOutcome.END_PENDING])
def test_status_counts_failed_adoptions_after_start_without_counting_pending(tmp_path, monkeypatch, outcome):
    async def run():
        pool = worker.WorkerPool(config=worker.WorkerConfig(), runtime_root=tmp_path,
                                 keeper=KeeperSettings(2))
        hold = RunHold('task-kept', 'step-kept', AgentType.WORKER)
        candidate = kept_dirs.Candidate(tmp_path / 'worker-kept', 'worker-kept', hold, 41, None)
        pool.set_candidates([candidate])
        monkeypatch.setattr(pool, 'adopt_dir', AsyncMock(return_value=AdoptResult(
            outcome, AdoptFailure.KEEPER_UNREACHABLE)))

        assert (await pool.adopt(candidate.run_id, hold)).outcome is outcome
        status = pool.keeper_status()
        assert status['failed_after_start'] == (
            {'keeper_unreachable': 1} if outcome is AdoptOutcome.FAILED else {})
        assert status['pending'] == (1 if outcome is AdoptOutcome.END_PENDING else 0)
        assert status['adopted_at_start'] == 0
        assert (await pool.adopt(candidate.run_id, hold)).outcome is (
            AdoptOutcome.NOT_CANDIDATE if outcome is AdoptOutcome.FAILED else AdoptOutcome.END_PENDING)
        assert pool.keeper_status()['failed_after_start'] == status['failed_after_start']

    asyncio.run(asyncio.wait_for(run(), 3))


@pytest.mark.parametrize("attached", [False, True])
def test_a_failed_keeper_end_preserves_identity_and_the_running_record(tmp_path, monkeypatch, attached):
    async def run():
        pool = worker.WorkerPool(config=worker.WorkerConfig(), runtime_root=tmp_path)
        run_dir = tmp_path / 'worker-kept'
        run_dir.mkdir()
        for name in (keeper.MANIFEST, keeper.STATE, keeper.STDOUT, keeper.DETACH):
            (run_dir / name).write_text('{}')
        end = AsyncMock(side_effect=OSError('cannot determine keeper liveness'))
        release = Mock()
        handle = SimpleNamespace(end=end, release=release) if attached else None
        monkeypatch.setattr(kept_dirs.KeeperRecord, 'read', Mock(return_value=object()))
        monkeypatch.setattr(kept_dirs, 'end_keeper', end)
        interrupt = AsyncMock()
        monkeypatch.setattr(llm_records, 'end_record_interrupted', interrupt)
        pool._active[run_dir.name] = SimpleNamespace(handle=handle)
        pool._slots[run_dir.name] = (AgentType.WORKER, 'task-kept')
        ready = pool._input_ready[run_dir.name] = asyncio.Event()

        assert not await pool._discard_adoption(run_dir, handle, 41, AdoptFailure.KEEPER_UNREACHABLE)

        interrupt.assert_not_awaited()
        end.assert_awaited_once()
        release.assert_not_called()
        assert (run_dir / keeper.MANIFEST).exists()
        assert (run_dir / keeper.STATE).exists()
        assert (run_dir / keeper.STDOUT).exists()
        assert (run_dir / keeper.DETACH).exists()
        assert run_dir.name not in pool._active
        assert run_dir.name not in pool._slots
        assert ready.is_set()

    asyncio.run(asyncio.wait_for(run(), 3))


def test_an_unidentifiable_keeper_keeps_its_directory(tmp_path, monkeypatch):
    async def run():
        pool = worker.WorkerPool(config=worker.WorkerConfig(), runtime_root=tmp_path)
        run_dir = tmp_path / 'worker-kept'
        run_dir.mkdir()
        (run_dir / keeper.STATE).write_text('invalid state')
        (run_dir / keeper.DETACH).write_text('{}')
        interrupt = AsyncMock()
        monkeypatch.setattr(llm_records, 'end_record_interrupted', interrupt)
        end = AsyncMock()
        monkeypatch.setattr(kept_dirs, 'end_keeper', end)

        await pool._discard_adoption(run_dir, None, 41, AdoptFailure.FILES)

        interrupt.assert_not_awaited()
        end.assert_not_awaited()
        assert (run_dir / keeper.STATE).exists()
        assert (run_dir / keeper.DETACH).exists()

    asyncio.run(asyncio.wait_for(run(), 3))


def test_a_live_cap_result_is_persisted_when_the_replay_boundary_completes(tmp_path):
    async def run():
        pool = worker.WorkerPool(config=worker.WorkerConfig(), runtime_root=tmp_path)
        launch = builtin_launch()
        handle = KeptRun(CLAUDE_CODE, ['claude'], launch=launch, cwd=tmp_path,
                         session_dir=None, label='kept-run', run_dir=tmp_path / 'worker-kept',
                         runtime_root=tmp_path, epoch=2, on_stale=lambda reason: None)
        handle.state = KeptRunState.ATTACHED
        handle._process.stdout.replay_until = 1
        handle.end = AsyncMock()
        handle.wait = AsyncMock(return_value=0)
        cap = soft_cap_result(.2, .1)
        persisted = []
        write_allowed = asyncio.Event()
        timer = None

        class Recorder:
            cap_stop = None
            finish = AsyncMock()

            def account(self, event):
                # A new price table can make usage from the detached portion exceed its frozen cap.
                assert event['type'] == 'usage'
                self.cap_stop = CapStop(cap, Usage(cost_usd=.2))

            async def record(self, event):
                nonlocal timer
                # The boundary waiter runs while this result's database write is pending.
                timer = asyncio.get_running_loop().call_later(.01, write_allowed.set)
                await write_allowed.wait()
                persisted.append(event)

        async def events():
            yield {'type': 'usage'}
            handle.replay_finished.set()
            yield {'type': 'assistant', 'message': {'content': []}}

        handle._channel = SimpleNamespace(events=events, end_input=Mock(), start=AsyncMock())
        ctx = worker.RunContext('worker-kept', '', None, str(tmp_path), '', False, launch, CLAUDE_CODE,
                                None, 0, datetime.now(timezone.utc), False, ['claude'])
        recorder = Recorder()
        try:
            result = await pool._consume(ctx, handle, recorder, start=worker.ConsumeStart.ADOPTED)
            assert result['status'] == 'failed'
            assert persisted == [cap]
            assert recorder.finish.await_args.kwargs['result_event'] == cap
            handle._channel.start.assert_not_awaited()
            handle.end.assert_awaited_once()
        finally:
            if timer is not None:
                timer.cancel()

    asyncio.run(asyncio.wait_for(run(), 3))


def test_output_after_a_replayed_result_is_recorded_until_eof(tmp_path):
    async def run():
        pool = worker.WorkerPool(config=worker.WorkerConfig(), runtime_root=tmp_path)
        launch = builtin_launch()
        handle = KeptRun(CLAUDE_CODE, ['claude'], launch=launch, cwd=tmp_path,
                         session_dir=None, label='kept-run', run_dir=tmp_path / 'worker-kept',
                         runtime_root=tmp_path, epoch=2, on_stale=lambda reason: None)
        handle.state = KeptRunState.ATTACHED
        handle._process.stdout.replay_until = 1
        handle.end = AsyncMock()
        handle.wait = AsyncMock(return_value=0)
        first = {'type': 'result', 'subtype': 'success', 'result': 'initial', 'total_cost_usd': .1}
        last = {**first, 'result': 'teammate finished', 'total_cost_usd': .2}
        accounted, persisted = [], []

        class Recorder:
            cap_stop = None
            finish = AsyncMock()

            def account(self, event):
                accounted.append(event)

            async def record(self, event):
                persisted.append(event)

        async def events():
            yield first
            handle.replay_finished.set()
            # Let the replay-boundary waiter run before more stdout arrives.
            await asyncio.sleep(.02)
            yield {'type': 'assistant', 'message': {'content': []}}
            yield last

        handle._channel = SimpleNamespace(events=events, end_input=Mock(), start=AsyncMock())
        ctx = worker.RunContext('worker-kept', '', None, str(tmp_path), '', False, launch, CLAUDE_CODE,
                                None, 0, datetime.now(timezone.utc), False, ['claude'])
        recorder = Recorder()
        await pool._consume(ctx, handle, recorder, start=worker.ConsumeStart.ADOPTED)
        assert accounted == [first]
        assert persisted == [{'type': 'assistant', 'message': {'content': []}}, last]
        assert recorder.finish.await_args.kwargs['result_event'] == last
        handle.end.assert_not_awaited()
        handle._channel.end_input.assert_called_once()

    asyncio.run(asyncio.wait_for(run(), 3))


@pytest.mark.parametrize('failure', ['newer_controller', 'keeper_unreachable'])
def test_adoption_reports_unreachable_keepers_and_preserves_newer_controllers(tmp_path, monkeypatch, failure):
    async def run():
        pool = worker.WorkerPool(config=worker.WorkerConfig(), runtime_root=tmp_path,
                                 keeper=KeeperSettings(2))
        run_dir = tmp_path / 'worker-kept'
        run_dir.mkdir()
        launch = builtin_launch()
        keeper.write_json(run_dir / keeper.MANIFEST, {
            'run_id': run_dir.name, 'task_id': 'task-kept', 'step_id': 'step-kept', 'role': 'worker',
            'record_id': 41, 'started_at': datetime.now(timezone.utc).isoformat(), 'timeout_s': 0,
            'command': ['claude'], 'work_dir': str(tmp_path), 'runtime_type': launch.runtime.type,
            'consume': {'provider_id': launch.profile.provider.id, 'model': launch.model,
                        'launcher': launch.profile.runtime.launcher.value, 'max_budget_usd': None},
            'reply': {'target': planner_address('planner-kept').to_dict(),
                      'correlation_id': 'dispatch-kept', 'metadata': {}},
        })
        monkeypatch.setattr(pool, '_task_project', AsyncMock(return_value=None))
        monkeypatch.setattr(worker, 'load_agent_settings', AsyncMock(return_value=launch.settings))
        monkeypatch.setattr(worker.ClaudeRunRecorder, 'resume', AsyncMock())
        error = (KeeperError(ErrorCode.STALE_CONTROLLER, 'a newer control plane holds this run')
                 if failure == 'newer_controller' else OSError('keeper is unreachable'))
        monkeypatch.setattr(KeptRun, 'attach', AsyncMock(side_effect=error))
        discard = AsyncMock(return_value=True)
        monkeypatch.setattr(pool, '_discard_adoption', discard)
        interrupt = AsyncMock()
        monkeypatch.setattr(llm_records, 'end_record_interrupted', interrupt)

        hold = RunHold('task-kept', 'step-kept', AgentType.WORKER)
        pool.set_candidates([kept_dirs.Candidate(run_dir, run_dir.name, hold, 41, None)])
        result = await pool.adopt(run_dir.name, hold)

        if failure == 'newer_controller':
            assert result.outcome is AdoptOutcome.END_PENDING
            assert result.failure is AdoptFailure.PROTOCOL
            discard.assert_not_awaited()
            assert pool.keeper_status()['failed_after_start'] == {}
            assert pool.keeper_status()['pending'] == 1
        else:
            assert result.outcome is AdoptOutcome.FAILED
            assert result.failure is AdoptFailure.KEEPER_UNREACHABLE
            discard.assert_awaited_once()
            assert pool.keeper_status()['failed_after_start'] == {'keeper_unreachable': 1}
            assert pool.keeper_status()['pending'] == 0
            assert pool.keeper_status()['adopted_at_start'] == 0
        interrupt.assert_not_awaited()
        assert (run_dir / keeper.MANIFEST).exists()

    asyncio.run(asyncio.wait_for(run(), 3))


def test_adoption_waits_for_candidate_expiry_to_finish(tmp_path, monkeypatch):
    async def run():
        pool = worker.WorkerPool(config=worker.WorkerConfig(), runtime_root=tmp_path)
        hold = RunHold('task-kept', 'step-kept', AgentType.WORKER)
        candidate = kept_dirs.Candidate(tmp_path / 'worker-kept', 'worker-kept', hold, 41, None)
        pool.set_candidates([candidate])
        ending, release = asyncio.Event(), asyncio.Event()

        async def end_candidate(*args, **kwargs):
            ending.set()
            await release.wait()

        monkeypatch.setattr(pool, '_end_candidate', end_candidate)
        expiry = asyncio.create_task(pool.expire_candidates(window=0))
        await ending.wait()
        adoption = asyncio.create_task(pool.adopt(candidate.run_id, hold))
        try:
            await asyncio.sleep(.02)
            assert not adoption.done(), 'a replacement must wait until the old keeper has ended'
            release.set()
            await expiry
            assert (await adoption).outcome is AdoptOutcome.NOT_CANDIDATE
        finally:
            release.set()
            await asyncio.gather(expiry, adoption, return_exceptions=True)

    asyncio.run(asyncio.wait_for(run(), 3))


def test_killing_a_run_waits_for_its_in_progress_adoption(tmp_path, monkeypatch):
    async def run():
        pool = worker.WorkerPool(config=worker.WorkerConfig(), runtime_root=tmp_path)
        hold = RunHold('task-kept', 'step-kept', AgentType.WORKER)
        candidate = kept_dirs.Candidate(tmp_path / 'worker-kept', 'worker-kept', hold, 41, None)
        pool.set_candidates([candidate])
        attaching, release, running, stopped = (asyncio.Event() for _ in range(4))

        async def consume():
            running.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        async def adopt_dir(*args, **kwargs):
            attaching.set()
            await release.wait()
            pool._tasks[candidate.run_id] = asyncio.create_task(consume())
            await running.wait()
            return AdoptResult(AdoptOutcome.ADOPTED)

        monkeypatch.setattr(pool, 'adopt_dir', adopt_dir)
        adoption = asyncio.create_task(pool.adopt(candidate.run_id, hold))
        await attaching.wait()
        killing = asyncio.create_task(pool.kill(candidate.run_id))
        try:
            await asyncio.sleep(.02)
            assert not killing.done(), 'a stop must wait for adoption to register the running task'
            release.set()
            assert (await adoption).outcome is AdoptOutcome.ADOPTED
            assert await killing
            assert stopped.is_set()
            assert pool._tasks[candidate.run_id].cancelled()
        finally:
            release.set()
            await asyncio.gather(adoption, killing, return_exceptions=True)
            for task in pool._tasks.values():
                task.cancel()
            await asyncio.gather(*pool._tasks.values(), return_exceptions=True)

    asyncio.run(asyncio.wait_for(run(), 3))


@pytest.mark.parametrize('protection', ['fenced', 'newer_controller'])
def test_failed_adoption_preserves_a_run_this_controller_cannot_end(tmp_path, monkeypatch, protection):
    async def run():
        pool = worker.WorkerPool(config=worker.WorkerConfig(), runtime_root=tmp_path,
                                 keeper=KeeperSettings(2))
        run_dir = tmp_path / 'worker-kept'
        run_dir.mkdir()
        before = {}
        for name in (keeper.MANIFEST, keeper.STATE, keeper.DETACH, keeper.STDOUT):
            before[name] = f'preserve {name}'.encode()
            (run_dir / name).write_bytes(before[name])
        monkeypatch.setattr(proc_tree, '_spawns_fenced', protection == 'fenced')
        record = SimpleNamespace(controller_epoch=3 if protection == 'newer_controller' else 1)
        monkeypatch.setattr(kept_dirs.KeeperRecord, 'read', Mock(return_value=record))
        end = AsyncMock()
        interrupt = AsyncMock()
        monkeypatch.setattr(kept_dirs, 'end_keeper', end)
        monkeypatch.setattr(llm_records, 'end_record_interrupted', interrupt)

        await pool._discard_adoption(run_dir, None, 41, AdoptFailure.PROTOCOL)

        end.assert_not_awaited()
        interrupt.assert_not_awaited()
        assert {name: (run_dir / name).read_bytes() for name in before} == before

    asyncio.run(asyncio.wait_for(run(), 3))


@pytest.mark.parametrize('protection', ['fenced', 'newer_controller'])
def test_candidate_cleanup_preserves_a_run_this_controller_cannot_end(tmp_path, monkeypatch, protection):
    async def run():
        pool = worker.WorkerPool(config=worker.WorkerConfig(), runtime_root=tmp_path,
                                 keeper=KeeperSettings(2))
        run_dir = tmp_path / 'worker-kept'
        run_dir.mkdir()
        (run_dir / keeper.STATE).write_text('preserve keeper identity')
        hold = RunHold('task-kept', 'step-kept', AgentType.WORKER)
        candidate = kept_dirs.Candidate(run_dir, run_dir.name, hold, 41, None)
        monkeypatch.setattr(proc_tree, '_spawns_fenced', protection == 'fenced')
        record = SimpleNamespace(controller_epoch=3 if protection == 'newer_controller' else 1)
        monkeypatch.setattr(kept_dirs.KeeperRecord, 'read', Mock(return_value=record))
        end = AsyncMock()
        interrupt = AsyncMock()
        monkeypatch.setattr(kept_dirs, 'end_keeper', end)
        monkeypatch.setattr(llm_records, 'end_record_interrupted', interrupt)
        pool.room_ops = SimpleNamespace(revoke_run=Mock())

        with pytest.raises(RuntimeError):
            await pool._end_candidate(candidate, kept_dirs.Reason.NOT_ADOPTED.value)

        end.assert_not_awaited()
        interrupt.assert_not_awaited()
        pool.room_ops.revoke_run.assert_not_called()
        assert (run_dir / keeper.STATE).read_text() == 'preserve keeper identity'

    asyncio.run(asyncio.wait_for(run(), 3))


@pytest.mark.parametrize('protection', ['fenced', 'newer_controller'])
@pytest.mark.parametrize('target', ['keeper', 'child'])
def test_keeper_cleanup_rechecks_ownership_after_reading_liveness(tmp_path, monkeypatch, protection, target):
    async def run():
        monkeypatch.setattr(proc_tree, '_spawns_fenced', False)
        record = SimpleNamespace(keeper_pid=111, keeper_started='keeper-start', child_pid=112,
                                 child_started='child-start', controller_epoch=1)
        latest = SimpleNamespace(controller_epoch=1)
        monkeypatch.setattr(kept_dirs.KeeperRecord, 'read', Mock(side_effect=lambda path: latest))

        async def running(*args):
            # Ownership changes while the process identity lookup is in flight.
            await asyncio.sleep(0)
            if protection == 'fenced':
                monkeypatch.setattr(proc_tree, '_spawns_fenced', True)
            else:
                latest.controller_epoch = 3
            return True

        monkeypatch.setattr(kept_dirs, '_running', running)
        kill = Mock()
        end_child = AsyncMock()
        monkeypatch.setattr(kept_dirs.os, 'kill', kill)
        monkeypatch.setattr(proc_tree, 'end_orphan', end_child)

        with pytest.raises(OSError):
            if target == 'keeper':
                await kept_dirs._signal_keeper(record, signal.SIGTERM, tmp_path)
            else:
                await kept_dirs._end_child(record, tmp_path)

        kill.assert_not_called()
        end_child.assert_not_awaited()

    asyncio.run(asyncio.wait_for(run(), 3))


@pytest.mark.parametrize('protection', ['fenced', 'newer_controller'])
def test_failed_adoption_preserves_files_when_ownership_changes_during_cleanup(tmp_path, monkeypatch, protection):
    async def run():
        pool = worker.WorkerPool(config=worker.WorkerConfig(), runtime_root=tmp_path,
                                 keeper=KeeperSettings(2))
        run_dir = tmp_path / 'worker-kept'
        run_dir.mkdir()
        before = {name: f'preserve {name}'.encode() for name in (
            keeper.MANIFEST, keeper.STATE, keeper.DETACH, keeper.STDOUT)}
        for name, data in before.items():
            (run_dir / name).write_bytes(data)
        monkeypatch.setattr(proc_tree, '_spawns_fenced', False)
        latest = SimpleNamespace(controller_epoch=1)
        monkeypatch.setattr(kept_dirs.KeeperRecord, 'read', Mock(side_effect=lambda path: latest))

        async def end_keeper(*args):
            if protection == 'fenced':
                monkeypatch.setattr(proc_tree, '_spawns_fenced', True)
            else:
                latest.controller_epoch = 3
            raise OSError('ownership changed during cleanup')

        monkeypatch.setattr(kept_dirs, 'end_keeper', end_keeper)
        interrupt = AsyncMock()
        monkeypatch.setattr(llm_records, 'end_record_interrupted', interrupt)

        await pool._discard_adoption(run_dir, None, 41, AdoptFailure.PROTOCOL)

        interrupt.assert_not_awaited()
        assert {name: (run_dir / name).read_bytes() for name in before} == before

    asyncio.run(asyncio.wait_for(run(), 3))
