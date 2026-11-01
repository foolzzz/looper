"""Adoption failure cleanup and the replay boundary keep their observable outcomes."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from looper.agents import keeper, kept_dirs
from looper.agents.agent_settings import AgentType
from looper.agents.claude_cli import CapStop, soft_cap_result
from looper.agents.kept_run import AdoptFailure, KeptRun, KeptRunState
from looper.agents.run_usage import Usage
from looper.agents.runtimes import CLAUDE_CODE
from looper.agents.tiers import worker
from looper.db import llm_records
from tests.runtime_model_helpers import builtin_launch


@pytest.mark.parametrize("attached", [False, True])
def test_a_failed_keeper_end_preserves_identity_and_interrupts_the_record(tmp_path, monkeypatch, attached):
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

        await pool._discard_adoption(run_dir, handle, 41, AdoptFailure.KEEPER_UNREACHABLE)

        interrupt.assert_awaited_once_with(
            41, 'the run could not be adopted after a restart: keeper_unreachable')
        end.assert_awaited_once()
        release.assert_not_called()
        assert (run_dir / keeper.MANIFEST).exists()
        assert (run_dir / keeper.STATE).exists()
        assert (run_dir / keeper.STDOUT).exists()
        assert not (run_dir / keeper.DETACH).exists()
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

        interrupt.assert_awaited_once()
        end.assert_not_awaited()
        assert (run_dir / keeper.STATE).exists()
        assert not (run_dir / keeper.DETACH).exists()

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
