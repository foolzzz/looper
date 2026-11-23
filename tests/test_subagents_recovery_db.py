"""Explorer restart recovery keeps process identity, transcript and cost ownership."""
from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path
import sys
from unittest.mock import AsyncMock

import pytest

from looper.agents import keeper, kept_run, proc_tree, subagents
from looper.agents.kept_run import KeeperSettings
from looper.db import agent_spawns, llm_records
from tests.db_support import run_db, until
from tests.fake_claude import fake_claude
from tests.test_keeper_protocol import short_directory
from tests.test_subagents_service_db import brief, setup


HELD_EXPLORER = """\
import json, sys, time
from pathlib import Path

def emit(event):
    print(json.dumps(event), flush=True)

Path('starts').open('a').write('started\\n')
first = json.loads(sys.stdin.readline())
emit({'type': 'system', 'subtype': 'init', 'session_id': 'held-explorer'})
emit(first)
emit({'type': 'assistant', 'message': {'content': [
    {'type': 'tool_use', 'id': 'held-read', 'name': 'Read', 'input': {'file_path': 'source.txt'}}]}})
Path('ready').touch()
while not Path('release').exists():
    time.sleep(.01)
answer = json.dumps({'summary': 'Explorer finished after adoption', 'findings': [],
                     'open_questions': [], 'confidence': 'high'})
emit({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': answer}]}})
emit({'type': 'result', 'subtype': 'success', 'is_error': False,
      'total_cost_usd': .04, 'result': answer})
"""


async def row_until(pool, spawn_id, state):
    async with asyncio.timeout(20):
        while True:
            row = await agent_spawns.get(spawn_id, pool=pool)
            if row['state'] == state:
                return row
            if row['finished_at'] is not None:
                raise AssertionError(f"explorer ended as {row['state']}: {row['detail']}")
            await asyncio.sleep(.01)


def test_kept_explorer_detaches_and_recovers_same_process_record_and_cost(tmp_path, monkeypatch):
    # Probe/hash outside the deadlines which measure starting and reattaching a keeper.
    from looper import components
    components.loaded_manifest()
    with short_directory() as runtime:
        handles = []
        original_type = kept_run.KeptRun

        class ShortKeptRun(original_type):
            def __init__(self, *args, **kwargs):
                original_id = Path(kwargs['run_dir']).name
                kwargs['run_dir'] = runtime / 'run'
                kwargs['runtime_root'] = runtime
                super().__init__(*args, **kwargs)
                self.manifest['run_id'] = original_id
                handles.append(self)

        monkeypatch.setattr(kept_run, 'KeptRun', ShortKeptRun)
        monkeypatch.setattr(keeper, 'RUNS_DIR', Path('.'))

        async def check(pool):
            first, caller = await setup(pool, tmp_path, monkeypatch)
            executable = fake_claude(tmp_path / 'claude-held', HELD_EXPLORER)
            first.runtime_root, first.claude_bin, first.keeper = runtime, executable, KeeperSettings(1)
            second = subagents.SpawnService(runtime, pool=pool, claude_bin=executable, keeper=KeeperSettings(2))
            row = await first.admit(caller, brief(), key='kept-once')
            running = asyncio.create_task(first.run(row['id']))
            try:
                await until(lambda: (tmp_path / 'ready').exists(), 'the explorer first input')
                asking = await row_until(pool, row['id'], 'asking')
                assert asking['child_process']['kept'] is True
                original_record = asking['child_record_id']
                original_identity = dict(asking['child_process'])
                await first.detach(epoch=1)
                await asyncio.wait_for(running, 10)
                detached = await agent_spawns.get(row['id'], pool=pool)
                assert detached['state'] == 'asking' and detached['finished_at'] is None
                assert (await llm_records.get_record(original_record, pool=pool))['status'] == 'running'
                assert (runtime / 'run' / keeper.DETACH).exists()

                # Recovery consumes the persisted process; it must not invoke run_child.
                no_spawn = AsyncMock(side_effect=AssertionError('restart recovery spawned another process'))
                monkeypatch.setattr(subagents.spawn, 'run_child', no_spawn)
                await second.recover()
                adopted = await agent_spawns.get(row['id'], pool=pool)
                assert adopted['state'] == 'asking'
                assert adopted['child_record_id'] == original_record
                assert adopted['child_process'] == original_identity
                assert not (runtime / 'run' / keeper.DETACH).exists()
                assert (await second.admit(caller, brief(), key='kept-once'))['id'] == row['id']
                (tmp_path / 'release').touch()
                finished = await row_until(pool, row['id'], 'answered')
                collectors = list(second._running.values())
                if collectors:
                    await asyncio.wait_for(asyncio.gather(*collectors), 10)
                assert not (runtime / '.local' / 'explorer-homes').joinpath(handles[0].manifest['run_id']).exists()
                await second.recover()
                assert finished['spent_usd'] == pytest.approx(.04)
                assert finished['result']['verified'] is True
                assert await pool.fetchval("SELECT count(*) FROM agent_spawns") == 1
                assert await pool.fetchval("SELECT count(*) FROM llm_records WHERE run_kind='explorer'") == 1
                assert float(await pool.fetchval("SELECT sum(spent_usd) FROM agent_spawns")) == pytest.approx(.04)
                record = await llm_records.get_record(original_record, pool=pool)
                assert record['status'] == 'completed' and record['cost_usd'] == pytest.approx(.04)
                costs = (await llm_records.task_costs(['t'], pool=pool))['t']
                assert costs['by_role']['explorer']['cost_usd'] == pytest.approx(.04)
                assert costs['by_role']['explorer']['runs'] == 1
                assert (tmp_path / 'starts').read_text().splitlines() == ['started']
                assert await pool.fetchval("SELECT count(*) FROM llm_record_events "
                    "WHERE record_id=$1 AND event::text LIKE '%held-read%'", original_record) == 1
                no_spawn.assert_not_awaited()
            finally:
                (tmp_path / 'release').touch()
                for handle in handles:
                    with suppress(Exception):
                        await handle.end()
                for service in (first, second):
                    tasks = list(service._running.values())
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    if tasks:
                        await asyncio.gather(*tasks, return_exceptions=True)
                if not running.done():
                    running.cancel()
                await asyncio.gather(running, return_exceptions=True)
                for handle in handles:
                    if handle._keeper_process is not None:
                        await asyncio.wait_for(handle._keeper_process.wait(), 10)
        run_db(check, monkeypatch, timeout=60)


async def orphan(pool, service, caller, key, *, stale_identity=False):
    row = await service.admit(caller, brief(), key=key)
    process = await asyncio.create_subprocess_exec(sys.executable, '-c', 'import time; time.sleep(60)',
                                                  start_new_session=True)
    started = await asyncio.to_thread(proc_tree._start_time, process.pid)
    assert started is not None
    record = await llm_records.start_record(agent_id='explorer-orphan-' + key, task_id='t',
        run_kind='explorer', parent_record_id=caller.parent.record_id, pool=pool)
    await agent_spawns.move(row['id'], agent_spawns.SpawnState.ASKING,
        expect=agent_spawns.SpawnState.QUEUED, child_record_id=record,
        child_process={'pid': process.pid, 'started': 'different-start-time' if stale_identity else started}, pool=pool)
    return row, process, started, record


@pytest.mark.parametrize('stale_identity', [False, True])
def test_restart_ends_matching_orphan_but_does_not_signal_reused_pid(tmp_path, monkeypatch, stale_identity):
    async def check(pool):
        original, caller = await setup(pool, tmp_path, monkeypatch)
        row, process, started, record = await orphan(pool, original, caller, 'orphan', stale_identity=stale_identity)
        runner = AsyncMock(side_effect=AssertionError('recovery must not spawn'))
        monkeypatch.setattr(subagents.spawn, 'run_child', runner)
        restarted = subagents.SpawnService(original.runtime_root, pool=pool)
        try:
            await restarted.recover()
            result = await agent_spawns.get(row['id'], pool=pool)
            assert result['state'] == 'cancelled' and result['cancel_reason'] == 'server'
            assert (await llm_records.get_record(record, pool=pool))['status'] == 'interrupted'
            assert (await agent_spawns.commitment('t', caller.parent.record_id, pool=pool))['task_reserved'] == 0
            if stale_identity:
                assert process.returncode is None
                assert await asyncio.to_thread(proc_tree._start_time, process.pid) == started
            else:
                await asyncio.wait_for(process.wait(), 10)
            runner.assert_not_awaited()
        finally:
            if process.returncode is None:
                await proc_tree.end(process, label='explorer recovery test cleanup')
            await asyncio.wait_for(process.wait(), 10)
    run_db(check, monkeypatch, timeout=40)


def test_cancelling_orphan_keeps_reservation_until_restart_termination_succeeds(tmp_path, monkeypatch):
    async def check(pool):
        original, caller = await setup(pool, tmp_path, monkeypatch)
        row, process, _, record = await orphan(pool, original, caller, 'cancel-retry')
        monkeypatch.setattr(original, '_end_process', AsyncMock(side_effect=subagents.Unavailable('termination failed')))
        try:
            with pytest.raises(subagents.Unavailable, match='termination failed'):
                await original.cancel(row['id'], reason=agent_spawns.CancelReason.OPERATOR)
            pending = await agent_spawns.get(row['id'], pool=pool)
            assert pending['state'] == 'cancelling' and pending['cancel_reason'] == 'operator'
            assert pending['child_process']['pid'] == process.pid
            assert pending['finished_at'] is None and process.returncode is None
            assert (await agent_spawns.commitment('t', caller.parent.record_id, pool=pool))['task_reserved'] == .5
            restarted = subagents.SpawnService(original.runtime_root, pool=pool)
            no_spawn = AsyncMock(side_effect=AssertionError('recovery must not spawn'))
            monkeypatch.setattr(subagents.spawn, 'run_child', no_spawn)
            await restarted.recover()
            await asyncio.wait_for(process.wait(), 10)
            result = await agent_spawns.get(row['id'], pool=pool)
            assert result['state'] == 'cancelled' and result['cancel_reason'] == 'operator'
            assert (await llm_records.get_record(record, pool=pool))['status'] == 'interrupted'
            assert (await agent_spawns.commitment('t', caller.parent.record_id, pool=pool))['task_reserved'] == 0
            no_spawn.assert_not_awaited()
        finally:
            if process.returncode is None:
                await proc_tree.end(process, label='explorer cancellation test cleanup')
            await asyncio.wait_for(process.wait(), 10)
    run_db(check, monkeypatch, timeout=40)
