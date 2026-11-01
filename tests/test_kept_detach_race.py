"""A recorder can finish between the detach snapshot and recorder lookup."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from looper.agents.kept_run import DetachKind, KeptRun, KeptRunState
from looper.agents.runtimes import CLAUDE_CODE
from looper.agents.tiers.worker import WorkerConfig, WorkerPool
from tests.runtime_model_helpers import builtin_launch


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
