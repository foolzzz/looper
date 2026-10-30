"""Bounded runner pools exercised through real fake-runner subprocess pipes."""
# ruff: noqa: F811 -- pytest injects the imported fixture by name.
import asyncio
import json
import logging

import pytest

from looper.agents.runtimes.runner import supervisor as s
from looper.agents.runtimes.runner.bridge import ConnectionLost
from looper.agents.runtimes.runner.protocol import ErrorCode, ProtocolError
from test_runner_supervisor import NEW, Turn, run, runners, until  # noqa: F401


def requests(lease):
    path = lease._generation.directory / "requests.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.mark.parametrize("host_cap,advertised,cap", [(2, 4, 2), (4, 2, 2), (1, 4, 1)])
def test_pool_expands_to_maximum_then_reports_total_capacity(runners, host_cap, advertised, cap):
    async def exercise():
        async with runners(session_cap=host_cap, cap=advertised) as (owner, processes):
            leases = [await owner.acquire("fake") for _ in range(cap * s.MAX_RUNNERS_PER_KIND)]
            assert len(processes) == s.MAX_RUNNERS_PER_KIND
            assert len({lease.runner_id for lease in leases[:cap]}) == 1
            assert leases[cap].runner_id != leases[0].runner_id
            assert all(len(member.leases) == cap for member in owner._kinds["fake"].members)
            with pytest.raises(ProtocolError, match=f"pool has {cap * s.MAX_RUNNERS_PER_KIND} session slots") as full:
                await owner.acquire("fake")
            assert full.value.code == ErrorCode.CAPACITY
            await leases[0].release()
            replacement = await owner.acquire("fake")
            assert replacement.runner_id == leases[0].runner_id
            assert len(processes) == s.MAX_RUNNERS_PER_KIND
    run(exercise())


def test_selects_member_with_most_free_slots(runners):
    async def exercise():
        async with runners(session_cap=3) as (owner, _):
            leases = [await owner.acquire("fake") for _ in range(4)]
            await leases[0].release()
            # Member 1 has one free slot, member 2 has two.
            chosen = await owner.acquire("fake")
            assert chosen.runner_id == leases[3].runner_id
    run(exercise())


def test_session_calls_stay_on_the_opening_member(runners):
    async def exercise():
        async with runners(session_cap=1, steer=True) as (owner, processes):
            first, second = await owner.acquire("fake"), await owner.acquire("fake")
            # Deliberately collide native IDs: routing must use the lease's bridge.
            for index, lease in enumerate((first, second)):
                sid = await lease.open_session(NEW)
                await lease.bridge.begin_turn(sid, f"trace-{index}", lambda event: None, Turn(), text=f"prompt-{index}")
                await lease.bridge.steer(sid, f"trace-{index}", f"message-{index}", f"steer-{index}")
                await lease.bridge.cancel_turn(sid, f"trace-{index}")
                await lease.release()
            assert first.session_id == second.session_id
            for index, lease in enumerate((first, second)):
                rows = requests(lease)
                assert {row["member"] for row in rows} == {processes[index].pid}
                calls = [row for row in rows if row["method"].startswith("session/")]
                assert [row["method"] for row in calls] == [
                    "session/new", "session/prompt", "session/steer", "session/cancel", "session/close"]
                assert all(row["params"].get("trace_id", f"trace-{index}") == f"trace-{index}" for row in calls)
                assert calls[1]["params"]["text"] == f"prompt-{index}"
                assert calls[2]["params"]["text"] == f"steer-{index}"
    run(exercise())


@pytest.mark.parametrize("persistent", ["owner", "state_dir"])
def test_resume_can_use_another_member_with_shared_state(runners, tmp_path, persistent):
    async def exercise():
        async with runners(session_cap=1) as (owner, _):
            kwargs = {"owner": "manager"} if persistent == "owner" else {"state_dir": tmp_path / "saved"}
            original = await owner.acquire("fake", **kwargs)
            sid = await original.open_session(NEW)
            state = original.state_dir
            (state / "history").write_text("saved turn")
            other = await owner.acquire("fake")
            await original.release()
            occupying_original = await owner.acquire("fake")
            assert occupying_original.runner_id == original.runner_id
            await other.release()
            resumed = await owner.acquire("fake", **kwargs)
            assert resumed.runner_id == other.runner_id != original.runner_id
            assert await resumed.load_session({**NEW, "session_id": sid}) == sid
            assert resumed.state_dir == state and (state / "history").read_text() == "saved turn"
            loaded = next(row for row in requests(resumed) if row["method"] == "session/load")
            assert loaded["member"] == resumed._generation.process.pid
            assert loaded["params"]["state_dir"] == str(state)
    run(exercise())


def test_idle_extra_member_stops_but_last_member_stays(runners, monkeypatch, caplog):
    monkeypatch.setattr(s, "IDLE_STOP_SECONDS", 0.05)
    caplog.set_level(logging.INFO, logger=s.__name__)
    async def exercise():
        async with runners(session_cap=1) as (owner, processes):
            first, extra = await owner.acquire("fake"), await owner.acquire("fake")
            await extra.release()
            await until(lambda: processes[1].returncode is not None and len(owner._kinds["fake"].members) == 1)
            assert first.valid and processes[0].returncode is None
            await first.release()
            await until(lambda: first._generation.idle is None)
            assert processes[0].returncode is None and len(owner._kinds["fake"].members) == 1
            replacement = await owner.acquire("fake")
            assert replacement.runner_id == first.runner_id
        messages = [record.getMessage() for record in caplog.records]
        assert sum("started:" in message for message in messages) == 2
        assert sum("stopped:" in message for message in messages) == 2
        assert all("sessions=" in message and "member" in message for message in messages)
    run(exercise())


def test_reusing_idle_member_resets_its_idle_deadline(runners, monkeypatch):
    monkeypatch.setattr(s, "IDLE_STOP_SECONDS", 0.05)
    async def exercise():
        async with runners(session_cap=1) as (owner, processes):
            await owner.acquire("fake")
            extra = await owner.acquire("fake")
            await extra.release()
            reused = await owner.acquire("fake")
            assert reused.runner_id == extra.runner_id and reused._generation.idle is None
            # Joining a timer on a separate member advances beyond the idle interval.
            first = next(iter(owner._kinds["fake"].members[0].leases))
            await first.release()
            await until(lambda: processes[0].returncode is not None)
            assert reused.valid and processes[1].returncode is None
    run(exercise())


@pytest.mark.parametrize("invalidate", [False, True], ids=["crash", "session-failure"])
def test_member_failure_only_fails_its_sessions_and_replaces_on_demand(runners, monkeypatch, invalidate):
    monkeypatch.setattr(s, "BACKOFF_BASE", 0.2)
    async def exercise():
        async with runners(session_cap=1) as (owner, processes):
            failed, healthy = await owner.acquire("fake"), await owner.acquire("fake")
            bad_sid, good_sid = await failed.open_session(NEW), await healthy.open_session(NEW)
            await failed.bridge.begin_turn(bad_sid, "bad", lambda event: None, Turn(), text="pending")
            await healthy.bridge.begin_turn(good_sid, "good", lambda event: None, Turn(), text="pending")
            if invalidate:
                await owner.invalidate(failed, ConnectionLost("cancel did not settle"))
            else:
                processes[0].kill()
            assert (await failed.bridge.wait_settled(bad_sid, "bad")).outcome == "lost"
            await until(lambda: failed._generation.failure is not None)
            await asyncio.shield(failed._generation.failure)
            state = owner._kinds["fake"]
            assert not state.restart.done()
            assert len(processes) == 2 and len(state.members) == 1
            assert not failed.valid and healthy.valid and not healthy.bridge._error
            assert healthy.bridge.sessions[good_sid].summary is None
            await healthy.bridge.cancel_turn(good_sid, "good")
            replacement = await owner.acquire("fake")
            assert len(processes) == 3 and replacement.runner_id not in {failed.runner_id, healthy.runner_id}
            assert healthy.valid and processes[1].returncode is None
    run(exercise())


@pytest.mark.parametrize("acquisitions", [2, 5])
def test_concurrent_boundary_leases_start_only_one_needed_member(runners, acquisitions):
    async def exercise():
        async with runners(session_cap=5, cap=5, slow_initialize=True) as (owner, processes):
            for _ in range(5):
                await owner.acquire("fake")
            leases = await asyncio.gather(*(owner.acquire("fake") for _ in range(acquisitions)))
            assert len(processes) == 2
            assert len({lease.runner_id for lease in leases}) == 1
            assert leases[0]._generation is owner._kinds["fake"].members[1]
    run(exercise())


def test_demand_reuses_slot_freed_during_backoff(runners, monkeypatch):
    monkeypatch.setattr(s, "BACKOFF_BASE", 0.2)
    async def exercise():
        async with runners(session_cap=1) as (owner, processes):
            failed, healthy = await owner.acquire("fake"), await owner.acquire("fake")
            await owner.invalidate(failed, ConnectionLost("runner lost"))
            assert not owner._kinds["fake"].restart.done()
            acquiring = asyncio.create_task(owner.acquire("fake"))
            try:
                await asyncio.sleep(0)
                assert not acquiring.done()
                await healthy.release()
                replacement = await acquiring
                assert replacement.runner_id == healthy.runner_id
                assert len(processes) == 2
            finally:
                acquiring.cancel()
                await asyncio.gather(acquiring, return_exceptions=True)
    run(exercise())
