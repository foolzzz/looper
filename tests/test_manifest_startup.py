"""Cold component snapshots never hash the installation on the event loop."""

import asyncio
import threading
import time
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper import components, db, ops_config
from looper.agents import (
    chat_settings, keeper, kept_dirs, proc_tree, profile_registry, scheduler_settings, serve, waits,
)
from looper.agents.http_server import AgentHTTPServer
from looper.db import plans


@pytest.fixture
def cold_manifest(monkeypatch):
    # Keep the production cache wrapper: counting a replacement loaded_manifest
    # would not prove that later synchronous consumers reuse its snapshot.
    components.loaded_manifest.cache_clear()
    expected = components.manifest_from_directory(components.CONFIG.parent.parent)
    calls = []
    window = []

    def slow_manifest(root):
        calls.append(threading.get_ident())
        window.append(time.monotonic())
        time.sleep(0.5)
        window.append(time.monotonic())
        return expected

    monkeypatch.setattr(components, "manifest_from_directory", slow_manifest)
    yield expected, calls, window
    components.loaded_manifest.cache_clear()


def test_startup_warms_manifest_before_kept_scan_without_blocking(monkeypatch, tmp_path, cold_manifest, caplog):
    expected, calls, window = cold_manifest
    pool = SimpleNamespace(fetch=AsyncMock(return_value=[{"id": 1}, {"id": 2}]))
    for module, name in ((db, "ensure_schema"), (db, "close_pool"),
                         (serve, "_back_up_before_schema_change"), (serve, "seed_role_configs"),
                         (serve, "seed_channel_configs"), (scheduler_settings, "load"),
                         (chat_settings, "load"), (waits, "load"), (proc_tree, "sweep")):
        monkeypatch.setattr(module, name, AsyncMock())
    monkeypatch.setattr(db, "init_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr(profile_registry, "register_found_runtimes", AsyncMock(return_value=[]))
    monkeypatch.setattr(serve.self_check, "check_database", AsyncMock(return_value=
                        serve.self_check.Check("database", "Database", "ok", "ready")))
    monkeypatch.setattr(serve, "tasks_left_running", AsyncMock(return_value=SimpleNamespace(resumable={"task"})))
    holds = {}
    for record_id, name in enumerate(("compatible", "changed"), 1):
        directory = tmp_path / keeper.RUNS_DIR / name
        directory.mkdir(parents=True)
        record = kept_dirs.KeeperRecord(1, "old", None, None, keeper.KeeperState.EXITED,
                                       keeper.EndReason.EXITED, 1, None)
        keeper.write_json(directory / keeper.STATE, {"format": keeper.STATE_FORMAT, **asdict(record)})
        keeper.write_json(directory / keeper.DETACH,
                          {"format": 1, "epoch": 1, "consumed": {"stdout": 0}, "stdin_offset": 0, "at": "now"})
        adoption = dict(expected.adoption)
        if name == "changed":
            adoption["work_contract"] = "different"
        keeper.write_json(directory / keeper.MANIFEST,
                          {"manifest_version": 1, "record_id": record_id, "adoption": adoption})
        (directory / keeper.STDOUT).touch()
        holds[name] = {"task_id": "task", "id": name, "status": "dispatched"}
    monkeypatch.setattr(plans, "run_holds", AsyncMock(return_value=holds))
    monkeypatch.setattr(kept_dirs, "live", lambda records: {name: False for name in records})
    monkeypatch.setattr(kept_dirs, "end_keeper", AsyncMock())
    scan_at_start = kept_dirs.scan_at_start
    ticks = []

    class ReachedScan(Exception):
        pass

    async def scan(*args, **kwargs):
        assert components.loaded_manifest.cache_info().currsize == 1
        result = await scan_at_start(tmp_path, epoch=2, resumable={"task"}, pool=pool)
        assert result.record_ids == [1]
        assert result.ended == {kept_dirs.Reason.CONTRACT_CHANGED: 1}
        assert (tmp_path / keeper.RUNS_DIR / "compatible").is_dir()
        assert not (tmp_path / keeper.RUNS_DIR / "changed").exists()
        raise ReachedScan

    monkeypatch.setattr(kept_dirs, "scan_at_start", scan)

    async def run():
        async def tick():
            while True:
                await asyncio.sleep(0.05)
                ticks.append(time.monotonic())

        ticker = asyncio.create_task(tick())
        ops = ops_config.OpsConfig(runtime_root=tmp_path, path=tmp_path / "ops.toml", port=0,
                                   operator_notes_root=str(tmp_path))
        try:
            with pytest.raises(ReachedScan):
                await serve._run_agent_system_owned(ops=ops, runtime_root=tmp_path, work_dir=tmp_path,
                                                     skip_backup=True)
            # Other startup filesystem work has its own budget; isolate this
            # regression's injected half-second read from those operations.
            during_read = [window[0], *(tick for tick in ticks if window[0] < tick < window[1]), window[1]]
            assert len(during_read) >= 7
            assert max(b - a for a, b in zip(during_read, during_read[1:])) < 0.2
        finally:
            ticker.cancel()
            await asyncio.gather(ticker, return_exceptions=True)

    with caplog.at_level("INFO", logger=serve.__name__):
        asyncio.run(asyncio.wait_for(run(), 10))
    assert "startup step: code_manifest" in caplog.text
    assert len(calls) == 1 and calls[0] != threading.get_ident()
    assert components.loaded_manifest() is expected
    assert len(calls) == 1


def test_http_start_and_other_startup_callers_share_one_snapshot(tmp_path, cold_manifest):
    expected, calls, _ = cold_manifest

    async def run():
        server = AgentHTTPServer(host="127.0.0.1", port=0, static_dir=tmp_path,
                                 web_adapter=None, task_router=None, supervisor=None)
        try:
            # Starting the standalone HTTP server can precede serve's prewarm.
            first = asyncio.create_task(server.start())
            await asyncio.sleep(0.05)
            snapshot = await components.prepare_loaded_manifest()
            await first
            assert snapshot is server._components is expected
            assert components.loaded_manifest() is expected
            assert len(calls) == 1
            assert calls[0] != threading.get_ident()
        finally:
            await server.stop()

    asyncio.run(asyncio.wait_for(run(), 5))
    assert asyncio.run(components.prepare_loaded_manifest()) is expected
    assert len(calls) == 1


def test_cancelled_startup_waiter_does_not_restart_snapshot(cold_manifest):
    expected, calls, _ = cold_manifest

    async def run():
        first = asyncio.create_task(components.prepare_loaded_manifest())
        await asyncio.sleep(0.05)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert await components.prepare_loaded_manifest() is expected
        assert len(calls) == 1

    asyncio.run(asyncio.wait_for(run(), 5))


def test_failed_manifest_read_can_be_retried(monkeypatch, cold_manifest):
    expected, calls, _ = cold_manifest

    def fail(root):
        calls.append(threading.get_ident())
        raise OSError("cannot read package")

    monkeypatch.setattr(components, "manifest_from_directory", fail)

    async def run():
        results = await asyncio.gather(*(components.prepare_loaded_manifest() for _ in range(2)),
                                       return_exceptions=True)
        assert all(isinstance(result, OSError) for result in results)
        assert len(calls) == 1
        assert components.loaded_manifest.cache_info().currsize == 0
        monkeypatch.setattr(components, "manifest_from_directory", lambda root: expected)
        assert await components.prepare_loaded_manifest() is expected

    asyncio.run(asyncio.wait_for(run(), 5))
