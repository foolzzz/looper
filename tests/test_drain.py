"""Drain admission, deadline reporting and operator API boundaries."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from looper.agents import api_v1
from looper.agents.drain import DrainPhase, DrainScope, DrainState
from looper.agents.tiers.worker import WorkerConfig, WorkerPool, WorkerProcess
from tests.test_api_v1_auth import CLIENT_TOKEN, TOKEN, _bearer, _send, _serving
from tests.test_api_v1_auth import clients as clients  # noqa: F401


@pytest.mark.parametrize("scope,seconds", [(DrainScope.TURNS, 180), (DrainScope.RUNS, 1200)])
def test_deadline_reports_without_aborting_and_restart_starts_serving(scope, seconds):
    now = datetime(2026, 10, 30, tzinfo=timezone.utc)
    drain = DrainState(clock=lambda: now)
    runs = [{"worker_id": "w1", "task_id": "t1", "role": "worker", "since": now.isoformat(), "keepable": False}]
    drain.add_source(lambda: {"runs": runs})
    initial = drain.begin(scope)
    assert initial["phase"] == DrainPhase.DRAINING
    assert datetime.fromisoformat(initial["deadline"]) - now == timedelta(seconds=seconds)
    now += timedelta(seconds=seconds)
    expired = drain.snapshot()
    assert expired["deadline_passed"] and expired["waiting"]["runs"] == runs
    assert expired["phase"] == "draining" and drain.draining
    runs.clear()
    assert drain.snapshot()["phase"] == "drained"
    assert drain.draining  # DRAINED still holds admission.
    assert DrainState().snapshot()["phase"] == "serving"


@pytest.mark.parametrize("blocker,value", [("manager_turns", ["manager"]), ("planner_turns", ["planner-t1"]),
                                          ("gates", 1), ("waits", 1), ("consults", 1), ("starting", 1)])
def test_each_control_plane_activity_blocks_drain(blocker, value):
    drain = DrainState()
    waiting = {blocker: value}
    drain.add_source(lambda: waiting)
    assert drain.begin(DrainScope.RUNS)["phase"] == "draining"
    waiting.clear()
    assert drain.snapshot()["phase"] == "drained"


def test_cancel_wakes_the_held_worker_slot(tmp_path, monkeypatch):
    from looper.agents.tiers import worker

    monkeypatch.setattr(worker, "load_task_limit", AsyncMock(return_value=2))

    async def run():
        drain = DrainState()
        pool = WorkerPool(config=WorkerConfig(), runtime_root=tmp_path, drain=drain)
        first = WorkerProcess("w1", "s1", "p1")
        second = WorkerProcess("w2", "s2", "p1")
        async with pool.slot_for(first):
            drain.begin(DrainScope.RUNS)
            entered = asyncio.Event()

            async def held():
                async with pool.slot_for(second):
                    entered.set()

            pending = asyncio.create_task(held())
            try:
                await asyncio.sleep(0)
                assert second.status == "pending" and not entered.is_set()
                assert drain.snapshot()["waiting"]["runs"][0]["worker_id"] == "w1"
                drain.cancel()
                await asyncio.wait_for(pending, 1)
                assert entered.is_set()
            finally:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    asyncio.run(run())


@pytest.mark.usefixtures("clients")
def test_operator_drain_api_and_generated_cli_contract(tmp_path):
    async def run():
        async with _serving(tmp_path, listen=True) as server:
            for method in ("GET", "POST", "DELETE"):
                code, _, _ = await _send(server, f"{method} /api/v1/system/drain", _bearer(CLIENT_TOKEN),
                                         json.dumps({"scope": "runs"}) if method == "POST" else "")
                assert code == 403
            code, _, _ = await _send(server, "POST /api/v1/system/drain", _bearer(TOKEN), '{"scope":"bad"}')
            assert code == 400
            code, _, answer = await _send(server, "POST /api/v1/system/drain", _bearer(TOKEN),
                                           '{"scope":"turns","timeout_seconds":1}')
            assert code == 202 and answer["result"]["phase"] == "drained"
            code, _, answer = await _send(server, "GET /api/v1/system/drain", _bearer(TOKEN))
            assert code == 200 and answer["result"]["scope"] == "turns"
            code, _, answer = await _send(server, "DELETE /api/v1/system/drain", _bearer(TOKEN))
            assert code == 200 and answer["result"]["phase"] == "serving"
            assert answer["result"]["since"] is None and answer["result"]["deadline"] is None
        document = api_v1.document()
        operations = document["paths"]["/system/drain"]
        assert set(operations) == {"get", "post", "delete"}
        assert "202" in operations["post"]["responses"]
        from looper import loopercli
        commands = {(head, tail, operation.method) for head, tail, _, _, operation
                    in loopercli.generated_operations(document)}
        assert {("system", "drain", "POST"), ("system", "drain-status", "GET"),
                ("system", "drain-cancel", "DELETE")} <= commands

    asyncio.run(run())


@pytest.mark.parametrize("timeout", [0, -1, True, float("inf")])
def test_invalid_timeout_leaves_serving(timeout):
    drain = DrainState()
    with pytest.raises(ValueError):
        drain.begin(DrainScope.RUNS, timeout)
    assert not drain.draining


def test_immediate_redrain_keeps_previously_woken_turns_held():
    async def run():
        drain = DrainState()
        drain.begin(DrainScope.RUNS)
        waiter = asyncio.create_task(drain.wait_serving())
        try:
            await asyncio.sleep(0)
            drain.cancel()
            drain.begin(DrainScope.TURNS)
            await asyncio.sleep(0)
            assert not waiter.done()
            drain.cancel()
            await asyncio.wait_for(waiter, 1)
        finally:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)

    asyncio.run(run())
