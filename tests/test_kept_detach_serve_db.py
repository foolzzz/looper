"""Planned detachment on isolated serve processes and disposable PostgreSQL."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import signal
import subprocess
from urllib.parse import urlsplit

import pytest

from evals.prompt_regression.runner import running_server
from looper.agents import keeper
from tests.db_support import DSN
from tests.test_drain_serve_db import _completed, _drain, _events, _project, _query, _task, _until
from tests.test_kept_run_serve_db import CLI, _live, _records, _running, fixture

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

# Produce complete observable events throughout the held run, including while
# there is no control plane, so the detach cursor has a meaningful readback.
STREAMING_CLI = CLI.replace(
    '        limit = time.monotonic() + 90',
    '        print(json.dumps({"type": "system", "subtype": "init", '
    '"session_id": "detach-worker", "model": "fake"}), flush=True)\n'
    '        sequence = 0\n'
    '        limit = time.monotonic() + 90',
).replace(
    '            time.sleep(0.05)',
    '            print(json.dumps({"type": "assistant", "message": {"role": "assistant", '
    '"content": [{"type": "text", "text": "working " + str(sequence)}]}}), flush=True)\n'
    '            sequence += 1\n'
    '            time.sleep(0.2)',
)


@contextmanager
def _fixture():
    # This lane is allowed only its own disposable databases on the test server.
    assert urlsplit(str(DSN)).hostname == "127.0.0.1"
    assert urlsplit(str(DSN)).port == 54329
    with fixture() as values:
        root, _, _, _ = values
        (root / "fake.py").write_text(STREAMING_CLI)
        yield values


def _serve_pid(url):
    rows = _query(url, "SELECT application_name FROM pg_stat_activity "
                  "WHERE datname=current_database() AND application_name LIKE 'looper serve %'")
    assert len(rows) == 1, rows
    return int(rows[0]["application_name"].split()[2].split("@")[0])


def _stop(url, *, pid=None):
    pid = pid or _serve_pid(url)
    os.kill(pid, signal.SIGTERM)
    _until(lambda: not _live(pid), "the control plane exiting after SIGTERM")


def _start_held(api, root, state):
    task = _task(api, _project(api, root, "detach"), "held")
    _until(lambda: _events(state, "worker", "held"), "the Worker reading its prompt")
    return task


def _drained_turns(api):
    _drain(api, "drain", {"scope": "turns", "timeout_seconds": 60})
    status = _until(lambda: (status := _drain(api))["phase"] == "drained" and status,
                    "turns draining without waiting for a keepable run")
    assert status["waiting"]["runs"] and all(run["keepable"] for run in status["waiting"]["runs"])
    return status


def test_a_planned_stop_detaches_kept_runs():
    with _fixture() as (root, url, state, server):
        log_path = root / "serve.log"
        with running_server(**server, log_path=log_path) as api:
            task = _start_held(api, root, state)
            directory, running = next(iter(_running(server["runtime_root"]).values()))
            manifest = keeper.read_json(directory / keeper.MANIFEST)
            command = manifest["command"]
            mcp_config = Path(command[command.index("--mcp-config") + 1])
            assert mcp_config.is_file()
            _until(lambda: len((directory / keeper.STDOUT).read_bytes().splitlines()) >= 3,
                   "multiple complete stream events")
            epoch = int(_query(url, "SELECT value FROM system_settings WHERE key='control_plane_epoch'")[0]["value"])
            _drained_turns(api)
            _stop(url)

        assert _live(running["keeper_pid"]) and _live(running["child_pid"])
        detached = keeper.read_json(directory / keeper.DETACH)
        assert detached["format"] == 1 and detached["epoch"] == epoch
        consumed = detached["consumed"]["stdout"]
        prefix = (directory / keeper.STDOUT).read_bytes()[:consumed]
        assert prefix and prefix.endswith(b"\n")
        expected = [json.loads(line) for line in prefix.splitlines()]
        expected = [event for event in expected if event["type"] not in ("usage", "stream_event")]
        record = _records(url, task)[0]
        assert record["status"] == "running"
        stored = _query(url, "SELECT event FROM llm_record_events WHERE record_id=$1 ORDER BY seq", record["id"])
        assert [json.loads(row["event"]) for row in stored] == expected
        assert mcp_config.is_file()
        assert not _query(url, "SELECT seq FROM activity WHERE task_id=$1 AND summary LIKE 'Worker finished%'", task)
        log = log_path.read_text()
        assert "shutdown mode=detach_kept" in log
        assert f"detach run={directory.name} offset={consumed}" in log
        # Real-path evidence: both processes are still alive after serve has exited.
        evidence = subprocess.run(["ps", "-o", "pid=,ppid=,command=", "-p",
                                   f"{running['keeper_pid']},{running['child_pid']}"],
                                  capture_output=True, text=True, timeout=5, check=True)
        assert {int(line.split()[0]) for line in evidence.stdout.splitlines()} == {
            running["keeper_pid"], running["child_pid"]}
        print("detach.json:", json.dumps(detached, sort_keys=True))
        print("surviving keeper and child:", evidence.stdout)
        print("\n".join(line for line in log.splitlines() if "shutdown mode=" in line or "detach run=" in line))


def test_without_adoption_the_next_start_ends_the_detached_run():
    with _fixture() as (root, url, state, server):
        log_path = root / "before.log"
        with running_server(**server, log_path=log_path) as api:
            task = _start_held(api, root, state)
            directory, running = next(iter(_running(server["runtime_root"]).values()))
            record_id = _records(url, task)[0]["id"]
            _drained_turns(api)
            _stop(url)
        assert (directory / keeper.DETACH).is_file()
        assert _live(running["keeper_pid"]) and _live(running["child_pid"])

        with running_server(**server, log_path=root / "after.log") as api:
            assert not _live(running["keeper_pid"]) and not _live(running["child_pid"])
            assert _query(url, "SELECT status FROM llm_records WHERE id=$1", record_id)[0]["status"] == "interrupted"
            _until(lambda: len(_events(state, "worker", "held")) == 2, "the step running again")
            (state / "release-s1").touch()
            _completed(api, task)
            records = _records(url, task)
            assert [row["status"] for row in records] == ["interrupted", "completed"]


@pytest.mark.parametrize("scope", [None, "runs"])
def test_other_shutdowns_end_kept_runs(scope):
    with _fixture() as (root, url, state, server):
        log_path = root / "serve.log"
        with running_server(**server, log_path=log_path) as api:
            task = _start_held(api, root, state)
            directory, running = next(iter(_running(server["runtime_root"]).values()))
            if scope:
                assert _drain(api, "drain", {"scope": scope})["phase"] == "draining"
            _stop(url)
        assert not (directory / keeper.DETACH).exists()
        assert not _live(running["keeper_pid"]) and not _live(running["child_pid"])
        assert _records(url, task)[0]["status"] == "interrupted"
        assert "shutdown mode=end_runs" in log_path.read_text()


def test_keeper_off_turns_drain_stop_ends_runs():
    with _fixture() as (root, url, state, server):
        server["keeper_mode"] = "off"
        log_path = root / "serve.log"
        with running_server(**server, log_path=log_path) as api:
            task = _start_held(api, root, state)
            child_pid = _events(state, "worker", "held")[0]["pid"]
            status = _drain(api, "drain", {"scope": "turns", "timeout_seconds": 60})
            assert status["phase"] == "draining"
            assert status["waiting"]["runs"] and not status["waiting"]["runs"][0]["keepable"]
            # A process run's direct parent is this isolated serve process.
            parent = subprocess.run(["ps", "-o", "ppid=", "-p", str(child_pid)],
                                    capture_output=True, text=True, timeout=5, check=True)
            _stop(url, pid=int(parent.stdout.strip()))
        assert not _live(child_pid)
        assert _records(url, task)[0]["status"] == "interrupted"
        assert not (server["runtime_root"] / keeper.RUNS_DIR).exists()
        assert "shutdown mode=end_runs" in log_path.read_text()
