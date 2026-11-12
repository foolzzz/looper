"""A recorder can finish between the detach snapshot and recorder lookup."""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from looper import components
from looper.agents import keeper
from looper.agents.tiers import worker
from looper.agents.kept_run import DetachKind, KeptRun, KeptRunState
from looper.agents.runtimes import CLAUDE_CODE
from looper.agents.tiers.worker import WorkerConfig, WorkerPool
from tests.runtime_model_helpers import builtin_launch


@pytest.fixture(scope='module', autouse=True)
def loaded_components():
    # The first snapshot probes contracts in a subprocess; it is setup, not detach time.
    components.loaded_manifest()


def test_a_recorder_removed_during_the_detach_snapshot_is_finishing(tmp_path):
    async def run():
        pool = WorkerPool(config=WorkerConfig(), runtime_root=tmp_path)
        handle = KeptRun(CLAUDE_CODE, ['claude'], launch=builtin_launch(), cwd=tmp_path,
                         session_dir=None, label='finished-run', run_dir=tmp_path / 'run',
                         runtime_root=tmp_path, epoch=1, on_stale=lambda reason: None)
        handle.state = KeptRunState.ATTACHED
        handle.detach = AsyncMock(side_effect=AssertionError('a finished recorder must not detach'))
        release = asyncio.Event()
        finishing = asyncio.create_task(release.wait())
        pool._tasks['finished-run'] = finishing
        pool._active['finished-run'] = SimpleNamespace(handle=handle)

        class FinishingRecorders(dict):
            def get(self, key, default=None):
                self.pop(key, None)
                release.set()
                return super().get(key, default)

        pool._recorders = FinishingRecorders({'finished-run': object()})
        try:
            assert await pool.detach_kept(epoch=1, timeout=1) == {DetachKind.FINISHING: ['finished-run']}
            assert finishing.done()
            handle.detach.assert_not_called()
            assert not (handle.run_dir / 'detach.json').exists()
        finally:
            release.set()
            await asyncio.gather(finishing, return_exceptions=True)

    asyncio.run(asyncio.wait_for(run(), 3))


@pytest.mark.parametrize('blocked', ['transport', 'consumer', 'finishing'])
def test_detach_has_one_budget_including_transport_and_consumer_cleanup(tmp_path, monkeypatch, caplog, blocked):
    monkeypatch.setattr(worker, 'KEPT_DETACH_BUDGET_SEC', 0.05, raising=False)
    caplog.set_level(logging.INFO, logger=worker.__name__)

    async def run():
        pool = WorkerPool(config=WorkerConfig(), runtime_root=tmp_path)
        handle = KeptRun(CLAUDE_CODE, ['claude'], launch=builtin_launch(), cwd=tmp_path,
                         session_dir=None, label='slow-run', run_dir=tmp_path / 'run',
                         runtime_root=tmp_path, epoch=1, on_stale=lambda reason: None)
        handle.run_dir.mkdir()
        handle.state = KeptRunState.ATTACHED
        handle._process.stdout.idle.set()
        handle._process.stdout.delivered = 42
        release = asyncio.Event()
        transport_tasks = []

        async def stuck():
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()

        async def wait_closed():
            transport_tasks.append(asyncio.current_task())
            await stuck()

        handle._client.close = AsyncMock()
        handle._client.wait_closed = wait_closed if blocked == 'transport' else AsyncMock()
        consumer = asyncio.create_task(stuck() if blocked == 'consumer' else release.wait())
        await asyncio.sleep(0)
        pool._tasks['slow-run'] = consumer
        pool._active['slow-run'] = SimpleNamespace(handle=handle)
        recorder = Mock()
        if blocked != 'finishing':
            pool._recorders['slow-run'] = recorder
        detaching = asyncio.create_task(pool.detach_kept(epoch=1))
        try:
            done, _ = await asyncio.wait([detaching], timeout=0.5)
            assert detaching in done, 'detach exceeded its total budget'
            expected = DetachKind.FINISHING if blocked == 'finishing' else DetachKind.DETACHED
            assert detaching.result() == {expected: ['slow-run']}
            assert 'detach outcomes=' in caplog.text
            if blocked != 'finishing':
                assert keeper.read_json(handle.run_dir / keeper.DETACH)['consumed']['stdout'] == 42
                recorder.detach.assert_called_once()
        finally:
            release.set()
            await asyncio.gather(detaching, consumer, *transport_tasks, return_exceptions=True)

    asyncio.run(asyncio.wait_for(run(), 3))
