"""Startup retains exactly the detached runs their persisted steps still hold."""

from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from looper import components
from looper.agents import keeper, kept_dirs
from looper.agents.agent_settings import AgentType
from looper.agents.kept_run import RunHold
from looper.db import llm_records, plans
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


@pytest.mark.parametrize("role", [AgentType.WORKER, AgentType.REVIEWER])
@pytest.mark.parametrize("reason", [None, "no_detach", "epoch_mismatch", "keeper_ended", "contract_changed",
                                   "not_held", "no_record", "log_short", "bad_files", "keeper_dead",
                                   "not_resumable", "room_grant_missing", "room_grant_invalid"])
def test_startup_verdict_preserves_only_adoptable_records(tmp_path, monkeypatch, role, reason):
    async def run(db):
        run_id = role.value + '-startup'
        directory = tmp_path / keeper.RUNS_DIR / run_id
        directory.mkdir(parents=True)
        status = 'dispatched' if role is AgentType.WORKER else 'completed'
        await db.execute("INSERT INTO projects (id, name) VALUES ('project-start', 'startup')")
        await db.execute("INSERT INTO tasks (id, project_id, requirement, work_dir) "
                         "VALUES ('task-start', 'project-start', 'resume', $1)", str(tmp_path))
        await db.execute("INSERT INTO task_steps (id, task_id, step_index, instruction, status, worker_id, reviewer_id) "
                         "VALUES ('step-start', 'task-start', 0, 'run', $1, $2, $3)", status,
                         run_id if role is AgentType.WORKER else 'worker-before',
                         run_id if role is AgentType.REVIEWER else None)
        record_id = await llm_records.start_record(agent_id=run_id, task_id='task-start', pool=db)
        other = await llm_records.start_record(agent_id='worker-unkept', pool=db)
        manifest = {'manifest_version': 1, 'adoption': components.loaded_manifest().adoption,
                    'record_id': record_id}
        detach = {'format': 1, 'epoch': 1, 'consumed': {'stdout': 3}, 'stdin_offset': 0, 'at': 'now'}
        state = kept_dirs.KeeperRecord(1, 'identity', None, None, keeper.KeeperState.RUNNING,
                                      None, 1, 'socket')
        resumable = ['task-start']
        if reason == 'epoch_mismatch':
            detach['epoch'] = 2
        elif reason == 'keeper_ended':
            state = replace(state, state=keeper.KeeperState.EXITED, end_reason=keeper.EndReason.ORPHANED)
        elif reason == 'contract_changed':
            manifest['adoption'] = {}
        elif reason == 'not_held':
            await db.execute("UPDATE task_steps SET status='pending'")
        elif reason == 'no_record':
            await db.execute("UPDATE llm_records SET status='completed' WHERE id=$1", record_id)
        elif reason == 'bad_files':
            manifest['manifest_version'] = 2
        elif reason == 'not_resumable':
            resumable = []
        elif reason in ('room_grant_missing', 'room_grant_invalid'):
            manifest['room_seat'] = True
            if reason == 'room_grant_invalid':
                keeper.write_json(directory / keeper.GRANT, {'format': 1})
        keeper.write_json(directory / keeper.MANIFEST, manifest)
        if reason != 'no_detach':
            keeper.write_json(directory / keeper.DETACH, detach)
        (directory / keeper.STDOUT).write_bytes(b'{}\n' if reason != 'log_short' else b'')
        monkeypatch.setattr(kept_dirs.KeeperRecord, 'read', lambda path: state)
        monkeypatch.setattr(kept_dirs, 'live', lambda records: {run_id: reason != 'keeper_dead'})
        end = AsyncMock()
        monkeypatch.setattr(kept_dirs, 'end_keeper', end)
        # A failed intermediate startup need not have attached: epoch 3 can adopt a keeper last held at 1.
        scan = await kept_dirs.scan_at_start(tmp_path, epoch=3, resumable=resumable, pool=db)
        if reason is None:
            assert scan.record_ids == [record_id]
            assert scan.candidates[0].hold == RunHold('task-start', 'step-start', role)
            assert directory.is_dir()
            end.assert_not_awaited()
        else:
            expected = {'not_resumable': 'not_held', 'room_grant_missing': 'bad_files',
                        'room_grant_invalid': 'bad_files'}.get(reason, reason)
            assert scan.ended == {kept_dirs.Reason(expected): 1}
            assert not scan.candidates and not directory.exists()
            end.assert_awaited_once()
        assert await llm_records.end_unfinished_records(keep=scan.record_ids, pool=db) == (
            1 if reason in (None, 'no_record') else 2)
        assert (await llm_records.record_state(record_id, pool=db))['status'] == (
            'running' if reason is None else 'completed' if reason == 'no_record' else 'interrupted')
        assert (await llm_records.record_state(other, pool=db))['status'] == 'interrupted'
        holds = await plans.run_holds([run_id, 'unknown'], pool=db)
        assert set(holds) == (set() if reason == 'not_held' else {run_id})
        if holds:
            assert set(holds[run_id]) == {'id', 'task_id', 'status', 'worker_id', 'reviewer_id'}

    run_db(run, monkeypatch, timeout=10)


def test_a_normally_exited_keeper_is_still_a_candidate(tmp_path, monkeypatch):
    # Exercise the same complete candidate preflight for a keeper that died naturally during downtime.
    record = kept_dirs.KeeperRecord(1, 'old', None, None, keeper.KeeperState.EXITED,
                                   keeper.EndReason.EXITED, 1, None)
    keeper.write_json(tmp_path / keeper.DETACH,
                      {'format': 1, 'epoch': 1, 'consumed': {'stdout': 0}, 'stdin_offset': 0, 'at': 'now'})
    (tmp_path / keeper.STDOUT).touch()
    manifest = {'manifest_version': 1, 'record_id': 42, 'adoption': components.loaded_manifest().adoption}
    assert kept_dirs._candidate_reason(tmp_path, record, False, 3, manifest, {'id': 'step'}, {42}) is None
