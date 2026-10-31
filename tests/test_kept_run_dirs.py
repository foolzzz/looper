"""Startup owns leftover run directories; other retention passes leave them alone."""

import asyncio
import json
import signal
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import housekeeping, keeper, kept_dirs, proc_tree


ROOT = Path(__file__).resolve().parents[1]


@asynccontextmanager
async def running_keeper(runtime_root):
    directory = runtime_root / keeper.RUNS_DIR / "worker-leftover"
    directory.mkdir(parents=True, mode=0o700)
    process = await asyncio.create_subprocess_exec(
        *keeper.boot_argv(sys.executable, ROOT, directory, runtime_root, str(runtime_root),
                          [sys.executable, "-c", "import time; time.sleep(60)"]),
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL, start_new_session=True,
    )
    state = {}
    try:
        async with asyncio.timeout(10):
            while (state := keeper.read_json(directory / keeper.STATE) or {}).get("state") != "running":
                assert process.returncode is None
                await asyncio.sleep(0.03)
        yield directory, process, state
    finally:
        if process.returncode is None:
            process.kill()
        await asyncio.wait_for(process.wait(), 5)
        # Also find a child if the test failed during startup, before reading keeper.json.
        records = [keeper.read_json(path) or {} for path in
                   (runtime_root / proc_tree.RECORDS_DIR).glob("*.json")]
        records.append({"pid": state.get("child_pid"), "started": state.get("child_started")})
        for record in records:
            pid, started = record.get("pid"), record.get("started")
            if pid and started and proc_tree._start_time(pid) == started:
                await proc_tree._end(proc_tree._Orphan(pid, started), label="test cleanup", known={})
        assert not ({record.get("pid") for record in records} & set(proc_tree._snapshot()))


def test_a_live_leftover_keeper_is_ended_before_the_scan_returns(tmp_path, monkeypatch, caplog):
    async def run():
        async with running_keeper(tmp_path) as (directory, process, state):
            ended_states = []
            rmtree = kept_dirs.shutil.rmtree

            def remove(path):
                ended_states.append(keeper.read_json(path / keeper.STATE))
                rmtree(path)

            monkeypatch.setattr(kept_dirs.shutil, "rmtree", remove)
            with caplog.at_level("INFO", logger=kept_dirs.__name__):
                scan = await kept_dirs.scan_at_start(tmp_path)
            assert scan.ended == {kept_dirs.Reason.NOT_ADOPTABLE: 1}
            assert scan.released == 0
            assert ended_states[0]["state"] == "exited"
            assert ended_states[0]["end_reason"] == "ended"
            assert await asyncio.wait_for(process.wait(), 5) == 0
            assert state["child_pid"] not in proc_tree._snapshot()
            assert not directory.exists()
            assert "kept runs at start ended=" in caplog.text

    asyncio.run(run())


@pytest.mark.parametrize("leftover", ["dead", "exited"])
def test_dead_keepers_directories_are_released(tmp_path, monkeypatch, caplog, leftover):
    async def run():
        async with running_keeper(tmp_path) as (directory, process, state):
            if leftover == "dead":
                process.kill()
                await process.wait()
                monkeypatch.setattr(proc_tree, "_records", None)
                monkeypatch.setattr(proc_tree, "_server", None)
                await proc_tree.sweep(tmp_path)
            else:
                process.send_signal(signal.SIGTERM)
                await asyncio.wait_for(process.wait(), 10)
            scan = await kept_dirs.scan_at_start(tmp_path)
            assert not scan.ended and scan.released == 1
            assert not directory.exists()
            assert state["child_pid"] not in proc_tree._snapshot()

    asyncio.run(run())


@pytest.mark.parametrize("argv", [["/usr/bin/true"], ["/nonexistent-keeper-child"]], ids=["exited", "start_failed"])
def test_exited_keeper_is_released(tmp_path, argv):
    async def run():
        directory = tmp_path / keeper.RUNS_DIR / "worker-short-lived"
        directory.mkdir(parents=True, mode=0o700)
        process = await asyncio.create_subprocess_exec(
            *keeper.boot_argv(sys.executable, ROOT, directory, tmp_path, str(tmp_path), argv),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL, start_new_session=True,
        )
        try:
            assert await asyncio.wait_for(process.wait(), 10) == (0 if argv == ["/usr/bin/true"] else 3)
            state = keeper.read_json(directory / keeper.STATE)
            assert state["state"] == "exited"
            if argv == ["/nonexistent-keeper-child"]:
                assert state["child_started"] is None
            assert kept_dirs.KeeperRecord.read(directory / keeper.STATE) is not None
            scan = await kept_dirs.scan_at_start(tmp_path)
            assert scan.released == 1 and not scan.ended and scan.unidentified == 0
            assert not directory.exists()
        finally:
            if process.returncode is None:
                process.kill()
            await asyncio.wait_for(process.wait(), 5)

    asyncio.run(run())


@pytest.mark.parametrize("missing_state", [False, True], ids=["corrupt", "missing"])
def test_unreadable_state_ends_the_keeper_and_child_from_the_spawn_record(tmp_path, missing_state):
    async def run():
        async with running_keeper(tmp_path) as (directory, process, state):
            if missing_state:
                (directory / keeper.STATE).unlink()
            else:
                (directory / keeper.STATE).write_text("invalid json")
            scan = await kept_dirs.scan_at_start(tmp_path)
            assert scan.ended == {kept_dirs.Reason.BAD_FILES: 1}
            assert scan.released == 0 and scan.unidentified == 0
            assert await asyncio.wait_for(process.wait(), 5) == 0
            assert state["child_pid"] not in proc_tree._snapshot()
            assert not directory.exists()

    asyncio.run(run())


@pytest.mark.parametrize("spawn_record", ["missing", "unbound", "other_run", "invalid_identity"])
def test_unidentifiable_keeper_is_left_for_the_next_scan(tmp_path, monkeypatch, caplog, spawn_record):
    async def run():
        async with running_keeper(tmp_path) as (directory, process, state):
            (directory / keeper.STATE).write_text("invalid json")
            path = tmp_path / proc_tree.RECORDS_DIR / f"{state['child_pid']}.json"
            record = keeper.read_json(path)
            assert record is not None
            if spawn_record == "missing":
                path.unlink()
            else:
                record.pop("run_dir", None)
                if spawn_record == "other_run":
                    record["run_dir"] = str(directory.with_name("another-run").resolve())
                elif spawn_record == "invalid_identity":
                    record["run_dir"] = str(directory.resolve())
                    record["server_pid"] = True
                keeper.write_json(path, record)

            def unexpected_signal(*args):
                pytest.fail("an unidentified keeper or child must not be signalled")

            with monkeypatch.context() as patch:
                patch.setattr(kept_dirs.os, "kill", unexpected_signal)
                patch.setattr(proc_tree.os, "killpg", unexpected_signal)
                with caplog.at_level("ERROR", logger=kept_dirs.__name__):
                    for _ in range(2):
                        scan = await kept_dirs.scan_at_start(tmp_path)
                        assert scan.unidentified == 1
                        assert not scan.ended and scan.released == 0
            assert directory.exists() and process.returncode is None
            assert state["child_pid"] in proc_tree._snapshot()
            assert (f"cannot identify the keeper of {directory}; directory left in place and will be scanned again "
                    "at the next start") in caplog.text

    asyncio.run(run())


def test_scan_ends_a_child_whose_keeper_died_after_sweep(tmp_path, monkeypatch):
    async def run():
        async with running_keeper(tmp_path) as (directory, process, state):
            monkeypatch.setattr(proc_tree, "_records", None)
            monkeypatch.setattr(proc_tree, "_server", None)
            await proc_tree.sweep(tmp_path)
            assert state["child_pid"] in proc_tree._snapshot()
            assert (tmp_path / proc_tree.RECORDS_DIR / f"{state['child_pid']}.json").exists()
            process.kill()
            await asyncio.wait_for(process.wait(), 5)
            assert state["child_pid"] in proc_tree._snapshot()
            scan = await kept_dirs.scan_at_start(tmp_path)
            assert not scan.ended and scan.released == 1 and scan.unidentified == 0
            assert state["child_pid"] not in proc_tree._snapshot()
            assert not directory.exists()

    asyncio.run(run())


@pytest.mark.parametrize("bad_state", [False, True], ids=["keeper_dead", "bad_files"])
def test_scan_does_not_signal_a_reused_child_pid(tmp_path, monkeypatch, bad_state):
    async def run():
        async with running_keeper(tmp_path) as (directory, process, state):
            process.kill()
            await asyncio.wait_for(process.wait(), 5)
            if bad_state:
                (directory / keeper.STATE).write_text("invalid json")
                path = tmp_path / proc_tree.RECORDS_DIR / f"{state['child_pid']}.json"
                record = keeper.read_json(path)
                record["started"] = "a previous process's start time"
                keeper.write_json(path, record)
            else:
                keeper.write_json(directory / keeper.STATE,
                                  {**state, "child_started": "a previous process's start time"})

            def unexpected_signal(*args):
                pytest.fail("a reused pid must not be signalled")

            with monkeypatch.context() as patch:
                patch.setattr(kept_dirs.os, "kill", unexpected_signal)
                patch.setattr(proc_tree.os, "killpg", unexpected_signal)
                scan = await kept_dirs.scan_at_start(tmp_path)
            assert scan.unidentified == 0
            assert state["child_pid"] in proc_tree._snapshot()
            assert not directory.exists()

    asyncio.run(run())


@pytest.mark.parametrize("failure_at", [1, 2])
def test_unknown_liveness_refuses_to_start(tmp_path, monkeypatch, failure_at):
    async def run():
        async with running_keeper(tmp_path) as (directory, process, _state):
            bad = directory.with_name("unreadable")
            bad.mkdir()
            starts, calls = proc_tree._start_times, 0

            def unavailable(pids):
                nonlocal calls
                calls += 1
                return None if calls == failure_at else starts(pids)

            monkeypatch.setattr(proc_tree, "_start_times", unavailable)
            with pytest.raises(OSError):
                await kept_dirs.scan_at_start(tmp_path)
            assert directory.exists() and bad.exists()
            assert process.returncode is None

    asyncio.run(run())


def test_serve_refuses_unknown_liveness_before_interrupting_records(tmp_path, monkeypatch):
    from looper import db
    from looper.agents import chat_settings, profile_registry, scheduler_settings, serve, waits
    from looper.db import llm_records

    for name in ("init_pool", "ensure_schema"):
        monkeypatch.setattr(db, name, AsyncMock())
    for name in ("_back_up_before_schema_change", "seed_role_configs", "seed_channel_configs", "tasks_left_running"):
        monkeypatch.setattr(serve, name, AsyncMock())
    for settings in (scheduler_settings, waits, chat_settings):
        monkeypatch.setattr(settings, "load", AsyncMock())
    monkeypatch.setattr(profile_registry, "register_found_runtimes", AsyncMock(return_value=[]))
    monkeypatch.setattr(serve.self_check, "check_database",
                        AsyncMock(return_value=SimpleNamespace(status=serve.self_check.OK)))
    monkeypatch.setattr(proc_tree, "sweep", AsyncMock())
    interrupted = AsyncMock()
    monkeypatch.setattr(llm_records, "end_unfinished_records", interrupted)
    ops = SimpleNamespace(host="127.0.0.1", port=18767, public_url=None,
                          operator_notes_root=tmp_path, database_url="unused", keeper_mode=serve.KeeperMode.OFF)

    async def run():
        async with running_keeper(tmp_path) as (directory, process, _state):
            monkeypatch.setattr(proc_tree, "_start_times", lambda pids: None)
            with pytest.raises(serve.StartupCheckFailed, match="cannot tell whether kept runs are still running"):
                await serve._run_agent_system_owned(ops=ops, runtime_root=tmp_path, work_dir=tmp_path)
            assert directory.exists() and process.returncode is None
            interrupted.assert_not_awaited()

    asyncio.run(run())


def test_an_unresponsive_keeper_and_its_child_are_ended(tmp_path):
    async def run():
        async with running_keeper(tmp_path) as (directory, process, state):
            process.send_signal(signal.SIGSTOP)
            scan = await kept_dirs.scan_at_start(tmp_path)
            assert await asyncio.wait_for(process.wait(), 5) == -signal.SIGKILL
            assert state["child_pid"] not in proc_tree._snapshot()
            assert scan.ended == {kept_dirs.Reason.NOT_ADOPTABLE: 1}
            assert not directory.exists()

    asyncio.run(run())


def test_reused_keeper_pid_is_not_signalled(tmp_path, monkeypatch):
    record = kept_dirs.KeeperRecord(123, "old", None, None, keeper.KeeperState.STARTING, None, None, None)
    monkeypatch.setattr(proc_tree, "_start_times", lambda pids: {123: "new"})

    def unexpected_signal(*args):
        pytest.fail("a reused pid must not be signalled")

    monkeypatch.setattr(kept_dirs.os, "kill", unexpected_signal)
    asyncio.run(kept_dirs.end_keeper(record, tmp_path))


def test_no_runs_directory_means_nothing_happens(tmp_path):
    scan = asyncio.run(kept_dirs.scan_at_start(tmp_path))
    assert not scan.ended and scan.released == 0
    assert list(tmp_path.iterdir()) == []


def test_non_directory_entries_are_ignored(tmp_path):
    runs = tmp_path / keeper.RUNS_DIR
    runs.mkdir(parents=True)
    entry = runs / "not-a-directory"
    entry.write_text("keep")
    scan = asyncio.run(kept_dirs.scan_at_start(tmp_path))
    assert not scan.ended and scan.released == 0
    assert entry.read_text() == "keep"


def test_housekeeping_leaves_kept_runs_alone(tmp_path):
    async def run():
        async with running_keeper(tmp_path) as (directory, process, state):
            before = {path.name: path.read_bytes() for path in directory.iterdir()}
            report = await housekeeping.run_once(tmp_path, datetime.now(timezone.utc),
                                                 current_runner=None, commands=None)
            assert "retention" in report.failed_steps  # No database pool in this suite.
            assert process.returncode is None
            assert state["child_pid"] in proc_tree._snapshot()
            assert {path.name: path.read_bytes() for path in directory.iterdir()} == before

    asyncio.run(run())


@pytest.mark.parametrize(("field", "value"), [
    ("format", 2), ("keeper_pid", True), ("keeper_pid", -1), ("keeper_started", ""),
    ("child_pid", "123"), ("child_started", []), ("state", "unknown"),
    ("end_reason", "unknown"), ("controller_epoch", True), ("socket", 1),
])
def test_invalid_record_fields_are_bad_files(tmp_path, field, value):
    data = dict(format=1, keeper_pid=123, keeper_started="start", child_pid=124,
                child_started="start", state="running", end_reason=None,
                controller_epoch=None, socket=None)
    data[field] = value
    directory = tmp_path / keeper.RUNS_DIR / "invalid"
    directory.mkdir(parents=True)
    (directory / keeper.STATE).write_text(json.dumps(data))
    scan = asyncio.run(kept_dirs.scan_at_start(tmp_path))
    assert scan.unidentified == 1 and not scan.ended and scan.released == 0
    assert directory.exists()


def test_live_checks_all_identities_in_one_snapshot(monkeypatch):
    data = dict(keeper_pid=123, keeper_started="original", child_pid=124,
                child_started="start", state=keeper.KeeperState.RUNNING,
                end_reason=None, controller_epoch=None, socket=None)
    records = {"same": kept_dirs.KeeperRecord(**data),
               "reused": kept_dirs.KeeperRecord(**{**data, "keeper_pid": 125})}
    calls = []

    def starts(pids):
        calls.append(list(pids))
        return {123: "original", 125: "different"}

    monkeypatch.setattr(proc_tree, "_start_times", starts)
    assert kept_dirs.live(records) == {"same": True, "reused": False}
    assert calls == [[123, 125]]
