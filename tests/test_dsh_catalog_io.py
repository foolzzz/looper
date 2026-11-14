"""Catalog preparation stays responsive and retries broken providers after a cooldown."""

import asyncio
import subprocess
from dataclasses import replace

import pytest

from looper.agents.runtimes import RuntimeUnsupported
from tests.test_dsh_runtime import _headless_stand_in, _installed_dsh_skills, _run_launch


@pytest.mark.parametrize("entry", ["record", "manager", "worker"])
def test_catalog_launch_keeps_the_event_loop_responsive(tmp_path, monkeypatch, agent_db, entry):
    import time
    from unittest.mock import AsyncMock

    from looper.agents.claude_cli import ClaudeRunRecorder
    from looper.agents.bus import MessageBus
    from looper.agents.message import (
        MANAGER_ADDRESS, MessageEnvelope, MessageType, channel_address, planner_address, worker_address,
    )
    from looper.agents.tiers import manager, worker
    from looper.db import llm_records

    library = _installed_dsh_skills(tmp_path, monkeypatch)
    launch = _run_launch(tmp_path, role="manager" if entry == "manager" else "worker")
    launch = replace(launch, library=replace(library, mcp_config=launch.library.mcp_config,
                                            role=launch.library.role))
    executable = _headless_stand_in(tmp_path)
    settings = replace(launch.settings, profile=replace(launch.profile,
                       runtime=replace(launch.profile.runtime, executable=executable)))
    tier = manager if entry == "manager" else worker
    monkeypatch.setattr(tier, "load_agent_settings", AsyncMock(return_value=settings))
    monkeypatch.setattr(tier, "load_installed", AsyncMock(return_value=launch.library))
    calls = []
    validation_windows = []
    original_run = subprocess.run

    def slow_provider(*args, **kwargs):
        if args[0][0] != "node":
            return original_run(*args, **kwargs)
        calls.append(True)
        started = time.monotonic()
        time.sleep(.5)
        validation_windows.append((started, time.monotonic()))
        return subprocess.CompletedProcess(args[0], 0, stdout="[]", stderr="")

    monkeypatch.setattr(subprocess, "run", slow_provider)
    if entry == "record":
        monkeypatch.setattr(llm_records, "start_record", AsyncMock(return_value=1))

    async def run():
        ticks = []
        finished = False

        async def heartbeat():
            while not finished:
                await asyncio.sleep(.01)
                ticks.append(time.monotonic())

        pulse = asyncio.create_task(heartbeat())
        await asyncio.sleep(0)
        try:
            if entry == "record":
                recorder = ClaudeRunRecorder(agent_id="worker", task_id=None, command=["dsh"],
                                            cwd=str(tmp_path), input_text="test", launch=launch)
                await recorder.start()
                assert recorder.record_id == 1
            elif entry == "manager":
                steward = manager.ManagerAgent(config=manager.ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
                steward._bus = MessageBus(tmp_path)
                steward._clean_start = True
                envelope = MessageEnvelope.create(source=channel_address("web"), target=MANAGER_ADDRESS,
                    message_type=MessageType.USER_MESSAGE, payload={"text": "read state"})
                try:
                    assert await steward._run_turn("read state", envelope)
                finally:
                    await steward._claude.stop()
                    await steward._bus.shutdown()
            else:
                pool = worker.WorkerPool(config=worker.WorkerConfig(), runtime_root=tmp_path)
                envelope = MessageEnvelope.create(source=planner_address("test"), target=worker_address("test"),
                    message_type=MessageType.WORK_ITEM,
                    payload={"task_id": "step", "instruction": "run", "context": {"work_dir": str(tmp_path)}})
                assert (await pool._execute(envelope, is_verify=False))["status"] == "completed"
        finally:
            finished = True
            await pulse
        assert calls
        # Measure the injected provider wait, excluding unrelated launch preparation and process teardown.
        # Include both edges: an on-loop sleep leaves no intervening tick and still produces a .5s gap.
        gaps = []
        for started, ended in validation_windows:
            samples = [started, *(tick for tick in ticks if started < tick < ended), ended]
            gaps.extend(right - left for left, right in zip(samples, samples[1:]))
        print(f"{entry} catalog validation max loop gap: {max(gaps):.3f}s")
        assert max(gaps) < .2, gaps

    asyncio.run(run())


def test_catalog_provider_failure_is_cached_for_one_minute(tmp_path, monkeypatch):
    from looper.agents.runtimes import dsh

    library = _installed_dsh_skills(tmp_path, monkeypatch)
    calls = []
    clock = [100.0]
    broken = [True]

    def broken_provider(*args, **kwargs):
        calls.append(True)
        if broken[0]:
            raise subprocess.TimeoutExpired(args[0], 20)
        return subprocess.CompletedProcess(args[0], 0, stdout="[]", stderr="")

    monkeypatch.setattr(subprocess, "run", broken_provider)
    # A scoped clock lets the failure expire without waiting or changing the process clock.
    monkeypatch.setattr(dsh, "_catalog_clock", lambda: clock[0], raising=False)
    for _ in range(2):
        with pytest.raises(RuntimeUnsupported, match="cannot validate DSH catalog"):
            dsh.materialize(library)
    assert len(calls) == 1
    broken[0] = False
    clock[0] += 59
    with pytest.raises(RuntimeUnsupported):
        dsh.materialize(library)
    assert len(calls) == 1
    clock[0] += 1
    recovered = dsh.materialize(library)
    assert dsh.materialize(library) == recovered
    assert len(calls) == 2
    dsh.materialize.cache_clear()
    assert dsh.materialize(library) == recovered
    assert len(calls) == 3


def test_cached_catalog_does_not_wait_for_another_library_validation(monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from looper.agents.library import AgentLibrary
    from looper.agents.runtimes import dsh

    healthy = AgentLibrary(role="healthy")
    blocked = AgentLibrary(role="blocked")
    expected = dsh.DshCatalog()
    entered, release = threading.Event(), threading.Event()

    def validate(library, executable):
        if library == blocked:
            entered.set()
            assert release.wait(5), "validation gate was not released"
            raise RuntimeUnsupported("provider unavailable")
        return expected

    dsh.materialize.cache_clear()
    try:
        with monkeypatch.context() as patch:
            patch.setattr(dsh, "_materialize", validate)
            assert dsh.materialize(healthy) is expected
            with ThreadPoolExecutor(max_workers=2) as pool:
                pending = pool.submit(dsh.materialize, blocked)
                try:
                    assert entered.wait(2), "blocked validation did not start"
                    cached = pool.submit(dsh.materialize, healthy)
                    assert cached.result(timeout=1) is expected
                finally:
                    release.set()
                with pytest.raises(RuntimeUnsupported, match="provider unavailable"):
                    pending.result(timeout=2)
    finally:
        dsh.materialize.cache_clear()
