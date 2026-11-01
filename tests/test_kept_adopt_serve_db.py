"""Startup adoption through isolated serve processes, real keepers and the MCP bridge."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
import signal
import subprocess
import time
from unittest.mock import patch

import pytest

from evals.prompt_regression.runner import free_port, running_server
from looper.agents import keeper
from tests.db_support import DSN
from tests.test_drain_serve_db import _completed, _events, _project, _query, _task, _until
from tests.test_kept_detach_serve_db import _drained_turns, _serve_pid, _stop
from tests.test_kept_run_serve_db import CLI, _live, _records, _running, fixture

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

# The held peer keeps producing events and accepting stdin while serve is down.
# Each tool call starts the actual MCP command from its original launch config.
_HELD = r'''
    import threading
    import subprocess

    def receive():
        for line in sys.stdin:
            log("input", pid=os.getpid(), line=line)
    threading.Thread(target=receive, daemon=True).start()

    def room_call(phase):
        config = json.loads(Path(args[args.index("--mcp-config") + 1]).read_text())["mcpServers"]["looper"]
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "adoption-fixture"}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                "name": "looper_room_roster", "arguments": {}}},
        ]
        response = subprocess.run([config["command"], *config["args"]],
            env={**os.environ, **config["env"]}, text=True,
            input="".join(json.dumps(m) + "\n" for m in messages),
            capture_output=True, timeout=15, check=True)
        answer = json.loads(response.stdout.splitlines()[-1])["result"]
        log("room", pid=os.getpid(), phase=phase, answer=answer)
        assert not answer.get("isError"), answer

    room_call("before")
    print(json.dumps({"type": "system", "subtype": "init", "session_id": "adoption-worker", "model": "fake"}), flush=True)
    sequence = 0
    called_after = False
    limit = time.monotonic() + 90
    while not (state / ("release-" + label)).exists():
        if time.monotonic() >= limit:
            raise RuntimeError("test did not release its Worker")
        if (state / "room-after").exists() and not called_after:
            room_call("after")
            called_after = True
        print(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "working " + str(sequence)}]}}), flush=True)
        sequence += 1
        time.sleep(0.2)
'''

_START = CLI.index('    if variant in ("held", "pair", "plugin"):')
_END = CLI.index('    answer("Done.', _START)
ADOPT_CLI = (CLI[:_START] + _HELD + CLI[_END:]).replace(
    '    log("worker", step=label, pid=os.getpid(), args=args)',
    '    previous = []\n'
    '    if (state / "events.jsonl").exists():\n'
    '        previous = [json.loads(line) for line in (state / "events.jsonl").read_text().splitlines()]\n'
    '    overlap = []\n'
    '    import subprocess\n'
    '    for row in previous:\n'
    '        if row["kind"] == "worker":\n'
    '            status = subprocess.run(["ps", "-o", "stat=", "-p", str(row["pid"])], capture_output=True, text=True, timeout=5)\n'
    '            if status.stdout.strip() and not status.stdout.strip().startswith("Z"):\n'
    '                overlap.append(row["pid"])\n'
    '    log("worker", step=label, pid=os.getpid(), args=args, overlap=overlap)',
).replace(
    '    else:\n        reply = {"reply": "continuing", "action": "none"}',
    '    else:\n        reply = {"reply": "Passed on", "action": "forward", "step": "s1", "text": "STEER-AFTER-RESTART"}',
)


_RETRY_HELD = r'''
    import threading
    import subprocess

    def receive():
        for line in sys.stdin:
            log("input", pid=os.getpid(), line=line)
    threading.Thread(target=receive, daemon=True).start()

    config = json.loads(Path(args[args.index("--mcp-config") + 1]).read_text())["mcpServers"]["looper"]
    assert config["env"]["LOOPER_AGENT_RECONNECT_SECONDS"] == "120", config["env"]
    with (state / "bridge.stderr").open("w") as stderr:
        bridge = subprocess.Popen([config["command"], *config["args"]],
            env={**os.environ, **config["env"]}, text=True,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr)
        log("bridge", pid=bridge.pid)
        def rpc(message):
            bridge.stdin.write(json.dumps(message) + "\n")
            bridge.stdin.flush()
            response = json.loads(bridge.stdout.readline())
            assert response["id"] == message["id"], response
            assert "error" not in response, response
            return response["result"]
        try:
            rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "retry-fixture"}}})
            rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            print(json.dumps({"type": "system", "subtype": "init", "session_id": "retry-worker", "model": "fake"}), flush=True)
            sequence, next_call = 0, 0
            limit = time.monotonic() + 90
            while not (state / ("release-" + label)).exists():
                if time.monotonic() >= limit:
                    raise RuntimeError("test did not release its Worker")
                if time.monotonic() >= next_call:
                    log("room_attempt", sequence=sequence, at=time.monotonic())
                    result = rpc({"jsonrpc": "2.0", "id": sequence + 3, "method": "tools/call", "params": {
                        "name": "looper_room_post", "arguments": {
                            "kind": "info", "body": "restart message " + str(sequence), "to": ["all"]}}})
                    log("room", sequence=sequence, answer=result)
                    assert not result.get("isError"), result
                    sequence += 1
                    next_call = time.monotonic() + 2
                print(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
                    {"type": "text", "text": "working " + str(sequence)}]}}), flush=True)
                time.sleep(0.2)
        finally:
            bridge.stdin.close()
            try:
                bridge.wait(timeout=5)
            except subprocess.TimeoutExpired:
                bridge.kill()
                bridge.wait(timeout=5)
            bridge.stdout.close()
        assert bridge.returncode == 0, bridge.returncode
'''

RETRY_CLI = ADOPT_CLI.replace(_HELD, _RETRY_HELD)


@contextmanager
def _fixture(*, keeper_mode="on", cli=ADOPT_CLI):
    # Tokens retain their original HTTP URL: restarting on another port would
    # test a different contract and make the old peer's MCP bridge unreachable.
    port = free_port()
    with fixture() as values, patch("evals.prompt_regression.runner.free_port", return_value=port):
        root, _, _, server = values
        (root / "fake.py").write_text(cli)
        server["keeper_mode"] = keeper_mode
        try:
            yield values
        except BaseException:
            for log in root.glob("*.log"):
                print(f"{log.name}:\n{log.read_text()[-8000:]}")
            raise


def _start(api, root, state):
    project = _project(api, root, "adoption")
    api.post("/api/v1/project_admin/update", {"project_id": project, "fields": {"agent_room": "on"}})
    task = _task(api, project, "held")
    _until(lambda: _events(state, "room"), "the Worker's first real MCP room call")
    return task


def _step(url, task):
    return _query(url, "SELECT id, status, worker_id FROM task_steps WHERE task_id=$1", task)[0]


def _activity(url, task):
    return [row["summary"] for row in _query(url, "SELECT summary FROM activity WHERE task_id=$1 ORDER BY seq", task)]


def _adopted(api):
    return _until(lambda: (status := api.post("/api/v1/system/status", {})["result"].get("keepers"))
                  and status["adopted_at_start"] == 1 and status, "the candidate being adopted")


def _ps(status):
    pids = {status["keeper_pid"], status["child_pid"]}
    result = subprocess.run(["ps", "-o", "pid=,ppid=,comm=", "-p", ",".join(map(str, pids))],
                            capture_output=True, text=True, timeout=5, check=True).stdout
    assert {int(line.split()[0]) for line in result.splitlines()} == pids
    return result


def _finished_once(api, url, task, state):
    detail = _completed(api, task)
    records = _records(url, task)
    assert len(records) == 1 and records[0]["status"] == "completed", records
    assert float(records[0]["cost_usd"]) == pytest.approx(0.001)
    # The only Worker record's cost appears once in the task total alongside
    # its Planner turns; checking the stored total catches double accounting.
    summed = _query(url, "SELECT SUM(cost_usd) AS cost FROM llm_records WHERE task_id=$1", task)
    assert float(detail["cost"]["cost_usd"]) == pytest.approx(float(summed[0]["cost"]))
    assert len([row for row in _events(state, "planner", "held") if row["text"].startswith("[DECISION]")]) == 1
    assert len([line for line in _activity(url, task) if line.startswith("Worker dispatched")]) == 1
    return detail


def test_a_kept_worker_carries_on_across_a_planned_restart():
    with _fixture() as (root, url, state, server):
        with running_server(**server, log_path=root / "before.log") as api:
            task = _start(api, root, state)
            directory, before = next(iter(_running(server["runtime_root"]).values()))
            worker = _step(url, task)["worker_id"]
            before_ps = _ps(before)
            _until(lambda: len((directory / keeper.STDOUT).read_bytes().splitlines()) >= 3, "stream events before detach")
            _drained_turns(api)
            _stop(url)
        assert _live(before["child_pid"]) and _live(before["keeper_pid"])
        with running_server(**server, log_path=root / "after.log") as api:
            status = _adopted(api)
            assert status["mode"] == "on" and status["attached"] == 1
            assert status["candidates_at_start"] == 1 and status["pending"] == 0
            assert _step(url, task)["worker_id"] == worker
            current = keeper.read_json(directory / keeper.STATE)
            assert current["child_pid"] == before["child_pid"]
            after_ps = _ps(current)
            assert _activity(url, task).count(f"Worker {worker} carried on across the restart") == 1
            (state / "room-after").touch()
            _until(lambda: len(_events(state, "room")) == 2, "the old token serving its room tool after adoption")
            api.post("/api/v1/operator_chat/send", {"text": "Pass STEER-AFTER-RESTART to step s1",
                "session_id": "adoption-chat", "message_id": "steer", "target": {"kind": "planner", "task_id": task}})
            _until(lambda: any("STEER-AFTER-RESTART" in row["line"] for row in _events(state, "input")),
                   "the adopted child reading the forwarded message")
            assert "STEER-AFTER-RESTART" in (directory / keeper.STDIN).read_text()
            (state / "release-s1").touch()
            _finished_once(api, url, task, state)
            print("before restart ps:\n" + before_ps + "after restart ps:\n" + after_ps)
            print("system.status.keepers:", json.dumps(status, sort_keys=True))
        print("\n".join(line for line in (root / "after.log").read_text().splitlines() if "adopt run=" in line))


def test_a_run_that_finished_while_the_server_was_down_reports_once():
    with _fixture() as (root, url, state, server):
        with running_server(**server, log_path=root / "before.log") as api:
            task = _start(api, root, state)
            directory, before = next(iter(_running(server["runtime_root"]).values()))
            before_ps = _ps(before)
            worker = _step(url, task)["worker_id"]
            _drained_turns(api)
            _stop(url)
        (state / "release-s1").touch()
        _until(lambda: (keeper.read_json(directory / keeper.STATE) or {}).get("state") == "exited",
               "the detached peer completing while serve is down")
        assert _records(url, task)[0]["status"] == "running"
        with running_server(**server, log_path=root / "after.log") as api:
            status = _adopted(api)
            _finished_once(api, url, task, state)
            assert _step(url, task)["worker_id"] == worker
            assert len(_events(state, "worker")) == 1
            print("before restart ps:\n" + before_ps)
            print("system.status.keepers:", json.dumps(status, sort_keys=True))
        print("\n".join(line for line in (root / "after.log").read_text().splitlines() if "adopt run=" in line))


def test_room_posts_retry_through_the_real_bridge_across_a_restart():
    with _fixture(cli=RETRY_CLI) as (root, url, state, server):
        with running_server(**server, log_path=root / "before.log") as api:
            project = _project(api, root, "retry")
            api.post("/api/v1/project_admin/update", {"project_id": project, "fields": {
                "agent_room": "on", "room_post_per_minute": 60, "room_broadcasts_per_run": 50}})
            task = _task(api, project, "held")
            _until(lambda: _events(state, "room"), "the first post through the real bridge")
            directory, before = next(iter(_running(server["runtime_root"]).values()))
            worker = _step(url, task)["worker_id"]
            bridge = _events(state, "bridge")[0]["pid"]
            _drained_turns(api)
            _stop(url)

        # Start only after a post is blocked while no HTTP listener exists.
        # This proves retry was needed, rather than racing a call between servers.
        def waiting_post():
            completed = {row["sequence"] for row in _events(state, "room")}
            return next((row for row in _events(state, "room_attempt")
                         if row["sequence"] not in completed and time.monotonic() - row["at"] >= 0.6), None)

        waiting = _until(waiting_post, "a write waiting for the control plane to return")
        assert _live(before["child_pid"]) and _live(bridge)
        with running_server(**server, log_path=root / "after.log") as api:
            _adopted(api)
            _until(lambda: any(row["sequence"] == waiting["sequence"] for row in _events(state, "room")),
                   "the pending post succeeding after adoption")
            _until(lambda: any(row["sequence"] > waiting["sequence"] for row in _events(state, "room")),
                   "the next periodic post succeeding")
            assert _step(url, task)["worker_id"] == worker
            assert keeper.read_json(directory / keeper.STATE)["child_pid"] == before["child_pid"]
            (state / "release-s1").touch()
            _finished_once(api, url, task, state)
            attempts = _events(state, "room_attempt")
            answers = _events(state, "room")
            assert [row["sequence"] for row in answers] == [row["sequence"] for row in attempts]
            assert all(not row["answer"].get("isError") for row in answers)
            messages = _query(url, "SELECT body FROM room_messages WHERE task_id=$1 AND sender_run=$2 ORDER BY id",
                              task, worker)
            assert [row["body"] for row in messages] == ["restart message " + str(row["sequence"]) for row in answers]
            retry_log = (state / "bridge.stderr").read_text()
            assert "agent api retry attempts=" in retry_log and "outcome=success" in retry_log, retry_log
            assert len(_events(state, "bridge")) == 1 and not _live(bridge)
            print(retry_log)


@pytest.mark.parametrize("reason", ["contract_changed", "epoch_mismatch", "not_held", "no_detach"])
def test_not_adoptable_runs_are_ended_before_their_steps_run_again(reason):
    with _fixture() as (root, url, state, server):
        with running_server(**server, log_path=root / "before.log") as api:
            task = _start(api, root, state)
            directory, before = next(iter(_running(server["runtime_root"]).values()))
            worker = _step(url, task)["worker_id"]
            record = _records(url, task)[0]["id"]
            if reason == "no_detach":
                pid = _serve_pid(url)
                os.kill(pid, signal.SIGKILL)
                _until(lambda: not _live(pid), "the undrained control plane stopping")
            else:
                _drained_turns(api)
                _stop(url)
        if reason == "contract_changed":
            manifest = keeper.read_json(directory / keeper.MANIFEST)
            manifest["adoption"] = {"run_manifest_version": 999, "keeper_protocols": []}
            (directory / keeper.MANIFEST).write_text(json.dumps(manifest))
        elif reason == "epoch_mismatch":
            detach = keeper.read_json(directory / keeper.DETACH)
            detach["epoch"] += 100
            (directory / keeper.DETACH).write_text(json.dumps(detach))
        elif reason == "not_held":
            _query(url, "UPDATE task_steps SET status='pending' WHERE task_id=$1", task, execute=True)
        else:
            assert not (directory / keeper.DETACH).exists()
        with running_server(**server, log_path=root / "after.log") as api:
            _until(lambda: len(_events(state, "worker")) == 2, "the rejected step running with a new child")
            assert not _live(before["keeper_pid"]) and not _live(before["child_pid"])
            assert _query(url, "SELECT status FROM llm_records WHERE id=$1", record)[0]["status"] == "interrupted"
            assert _step(url, task)["worker_id"] != worker
            assert all(not row["overlap"] for row in _events(state, "worker"))
            status = api.post("/api/v1/system/status", {})["result"]["keepers"]
            assert status["ended_at_start"][reason] == 1
            (state / "release-s1").touch()
            _completed(api, task)


def test_keeper_off_resumes_as_today():
    with _fixture(keeper_mode="off") as (root, url, state, server):
        with running_server(**server, log_path=root / "before.log") as api:
            task = _start(api, root, state)
            worker = _step(url, task)["worker_id"]
            old_pid = _events(state, "worker")[0]["pid"]
            # A process run cannot detach; a planned stop ends it as before.
            api.post("/api/v1/system/drain", {"scope": "turns", "timeout_seconds": 60})
            parent = subprocess.run(["ps", "-o", "ppid=", "-p", str(old_pid)],
                                    capture_output=True, text=True, timeout=5, check=True)
            _stop(url, pid=int(parent.stdout.strip()))
        assert not _live(old_pid)
        with running_server(**server, log_path=root / "after.log") as api:
            _until(lambda: len(_events(state, "room")) == 2, "the replacement Worker's successful room call")
            assert "keepers" not in api.post("/api/v1/system/status", {})["result"]
            assert _step(url, task)["worker_id"] != worker
            assert not any("carried on across the restart" in line for line in _activity(url, task))
            assert all(not row["answer"].get("isError") for row in _events(state, "room"))
            (state / "release-s1").touch()
            _completed(api, task)
            assert [record["status"] for record in _records(url, task)] == ["interrupted", "completed"]
