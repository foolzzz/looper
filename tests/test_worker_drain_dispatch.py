"""Drain admission and startup handover use deterministic scheduling barriers."""

import asyncio
from pathlib import Path
import tempfile
from unittest.mock import AsyncMock

import pytest

from looper import components
from looper.agents import keeper
from looper.agents.bus import MessageBus
from looper.agents.drain import DrainScope, DrainState
from looper.agents.kept_run import DetachKind, KeeperSettings, KeptRun, KeptRunState
from looper.agents.message import MessageEnvelope, MessageType, planner_address, worker_address
from looper.agents.tiers import worker
from tests.db_support import until
from tests.fake_claude import fake_claude
from tests.runtime_model_helpers import builtin_launch
from tests.test_kept_run import live


@pytest.fixture(scope="module", autouse=True)
def loaded_components():
    components.loaded_manifest()


def item(root, review=False):
    return MessageEnvelope.create(
        source=planner_address("task-drain"), target=worker_address("worker-drain"),
        message_type=MessageType.VERIFY_REQUEST if review else MessageType.WORK_ITEM,
        payload={"task_id": "step-drain", "parent_task_id": "task-drain", "instruction": "inspect",
                 "context": {"work_dir": str(root)}},
    )


@pytest.mark.parametrize("review", [False, True])
@pytest.mark.parametrize("resume", [False, True])
def test_dispatch_during_drain_stays_queued_until_serving(tmp_path, monkeypatch, review, resume):
    async def scenario():
        drain = DrainState()
        pool = worker.WorkerPool(config=worker.WorkerConfig(), runtime_root=tmp_path, drain=drain)
        started = asyncio.Event()

        async def run(envelope):
            started.set()
            await asyncio.Event().wait()

        execute = AsyncMock(side_effect=run)
        monkeypatch.setattr(pool, "_run_worker", execute)
        bus = MessageBus(tmp_path)
        await pool.start(bus)
        try:
            drain.begin(DrainScope.TURNS)
            await pool._dispatch(item(tmp_path, review))
            await asyncio.sleep(0)
            assert "worker-drain" in pool._tasks  # retained for cancel or restart; no result was emitted
            assert not pool._active and not pool._slots
            execute.assert_not_awaited()
            assert drain.snapshot()["phase"] == "drained"
            if resume:
                drain.cancel()
                await asyncio.wait_for(started.wait(), 1)
                execute.assert_awaited_once()
            else:
                await pool.stop()
                drain.cancel()
                await asyncio.sleep(0)
                execute.assert_not_awaited()
        finally:
            await pool.stop()
            await bus.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("blocked_at", ["preparation", "record", "launch", "retry_record", "initial_input",
                                       "retry_initial_input"])
def test_detach_during_startup_aborts_before_launch_or_preserves_the_child(monkeypatch, agent_db, blocked_at):
    async def scenario(root):
        reached, release = asyncio.Event(), asyncio.Event()
        handles, results = [], []

        class HeldRun(KeptRun):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                handles.append(self)

            async def launch(self):
                if blocked_at == "launch":
                    reached.set()
                    await release.wait()
                await super().launch()
                if blocked_at in ("initial_input", "retry_initial_input"):
                    start = self.channel.start

                    async def held_input(prompt):
                        reached.set()
                        await release.wait()
                        await start(prompt)

                    self.channel.start = held_input

        monkeypatch.setattr(worker, "KeptRun", HeldRun)
        monkeypatch.setattr(worker, "load_agent_settings", AsyncMock(return_value=builtin_launch().settings))
        monkeypatch.setattr(worker, "_load_settings", AsyncMock(return_value=worker.WorkerSettings(0, 0, "work")))
        monkeypatch.setattr(worker, "load_task_limit", AsyncMock(return_value=1))
        monkeypatch.setattr(worker.WorkerPool, "_task_project", AsyncMock(return_value=None))
        executable = fake_claude(root / "claude", "import sys, time\nsys.stdin.readline()\ntime.sleep(60)\n")
        drain = DrainState()
        pool = worker.WorkerPool(config=worker.WorkerConfig(claude_bin=executable), runtime_root=root,
                                 drain=drain, keeper=KeeperSettings(1))
        prepare = pool._prepare
        enter_record = worker.ClaudeRunRecorder.__aenter__

        async def held_prepare(*args, **kwargs):
            if blocked_at.startswith("retry_"):
                # A previous attempt delivered input before fallback prepared this replacement handle.
                pool._input_ready["worker-drain"].set()
            if blocked_at == "preparation":
                reached.set()
                await release.wait()
            return await prepare(*args, **kwargs)

        async def held_record(recorder):
            if blocked_at in ("record", "retry_record"):
                reached.set()
                await release.wait()
            return await enter_record(recorder)

        monkeypatch.setattr(worker.ClaudeRunRecorder, "__aenter__", held_record)
        monkeypatch.setattr(pool, "_prepare", held_prepare)
        bus = MessageBus(root)
        bus.subscribe_topic(MessageType.WORK_RESULT, results.append)
        await pool.start(bus)
        detaching = None
        attached = None
        try:
            await pool._dispatch(item(root))
            await asyncio.wait_for(reached.wait(), 15)
            if blocked_at.startswith("retry_"):
                assert pool._input_ready["worker-drain"].is_set()  # ordinary input delivery stays unchanged
            status = drain.begin(DrainScope.TURNS)
            assert status["phase"] == "draining"
            assert status["waiting"]["runs"][0]["keepable"] is False
            detaching = asyncio.create_task(pool.detach_kept(epoch=1))
            await asyncio.sleep(0)
            if blocked_at in ("preparation", "record", "retry_record"):
                assert await asyncio.wait_for(detaching, 2) == {}
                release.set()
                # Let preparation finish after detach; the shutdown admission gate must prevent its spawn.
                await until(lambda: not pool._tasks, timeout=3)
                await pool.stop()
                handle = handles[0]
                assert not (handle.run_dir / keeper.MANIFEST).exists()
                assert handle.pid is None
            else:
                assert not detaching.done(), "detach must wait for startup and initial input"
                release.set()
                assert await asyncio.wait_for(detaching, 15) == {DetachKind.DETACHED: ["worker-drain"]}
                handle = handles[0]
                pid = handle.pid
                assert pid and live(pid)
                await pool.stop()
                assert live(pid), "pool shutdown killed a child that started during detach"
                assert keeper.read_json(handle.run_dir / keeper.DETACH)["stdin_offset"] > 0
                attached = await KeptRun.attach(handle.run_dir, runtime=handle._runtime,
                    launch=builtin_launch(), epoch=2, on_stale=lambda reason: None, label="startup readback")
                assert attached.pid == pid and attached.state is KeptRunState.ATTACHED
            assert not results  # the saved step can be adopted or re-dispatched without a failure reply
        finally:
            release.set()
            if detaching is not None:
                await asyncio.gather(detaching, return_exceptions=True)
            await pool.stop()
            if attached is not None:
                await attached.end()
                attached.release()
            for handle in handles:
                if handle.state is KeptRunState.DETACHED and attached is None:
                    from looper.agents.kept_dirs import KeeperRecord, end_keeper

                    record = KeeperRecord.read(handle.run_dir / keeper.STATE)
                    if record is not None:
                        await end_keeper(record, handle.run_dir)
                else:
                    await handle.end()
                if handle._keeper_process is not None:
                    await handle._keeper_process.wait()
            await bus.shutdown()

    # Keep the actual keeper socket below the Unix socket path limit.
    with tempfile.TemporaryDirectory(prefix="ldr.", dir="/tmp") as directory:
        asyncio.run(asyncio.wait_for(scenario(Path(directory)), 35))
