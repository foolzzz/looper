"""Production Pi opt-in through isolated serve, real Pi and loopback Responses."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import threading
import time

import asyncpg
import pytest

from evals.prompt_regression.runner import EvalError, fresh_database
from fake_model_api import call_item, completed, event, text_item
from tests.pi_runner_support import require_pi
from tests.runner_support import require_disposable_database, serving

pytestmark = pytest.mark.installed
KEY = "synthetic-pi-serve-key"
FACT = "archived-orchid-6742"
SENTINEL = "PROJECT-PI-RUNNER-SENTINEL"
USAGE = {"input_tokens": 1300, "input_tokens_details": {"cached_tokens": 300},
         "output_tokens": 200, "total_tokens": 1500}


def query(url, sql, *args, execute=False):
    async def run():
        conn = await asyncpg.connect(url, timeout=5, command_timeout=10)
        try:
            if execute:
                return await conn.execute(sql, *args)
            return [dict(row) for row in await conn.fetch(sql, *args)]
        finally:
            await conn.close()
    return asyncio.run(asyncio.wait_for(run(), 20))


def wait_until(read, ready=bool, timeout=30):
    deadline = time.monotonic() + timeout
    while True:
        value = read()
        if ready(value):
            return value
        if time.monotonic() >= deadline:
            pytest.fail(f"Pi serve acceptance timed out: {value!r}")
        time.sleep(0.1)


def answer_events(item):
    initial = {**item, "arguments": ""} if item["type"] == "function_call" else {**item, "content": []}
    events = [event("response.output_item.added", output_index=0, item=initial)]
    if item["type"] == "function_call":
        events.append(event("response.function_call_arguments.delta", output_index=0,
                            item_id=item["id"], delta=item["arguments"]))
    else:
        events.append(event("response.output_text.delta", output_index=0,
                            item_id=item["id"], content_index=0, delta=item["content"][0]["text"]))
    return [*events, event("response.output_item.done", output_index=0, item=item), completed([item], usage=USAGE)]


class Responses(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, outside):
        self.requests, self.errors = [], []
        self.held, self.release = threading.Event(), threading.Event()
        self.outside = outside
        super().__init__(("127.0.0.1", 0), Handler)

    def script(self, body):
        tools = {tool["name"] for tool in body.get("tools", [])}
        last = body["input"][-1]
        wire = json.dumps(last)
        if "looper_tasks" in tools:
            if "[HOLD]" in wire:
                self.held.set()
                if not self.release.wait(30):
                    raise AssertionError("held Manager request was never stopped")
            if last.get("type") == "function_call_output":
                assert FACT in last["output"]
                return text_item(FACT)
            if "List the tasks" in wire:
                return call_item('{"action":"list"}', name="looper_tasks")
            return text_item("Manager continued with " + FACT)
        if {"read", "bash", "edit", "write"} <= tools:
            return text_item('Done.\n```json\n' + json.dumps({"status": "completed", "changed_files": {},
                "deleted_files": [], "summary": "Worker on Pi runner completed"}) + '\n```')
        assert tools - {"read", "grep", "find", "ls"}
        assert tools - {name for name in tools if name.startswith("looper_")} == {"read", "grep", "find", "ls"}
        if SENTINEL not in body["instructions"]:
            raise AssertionError("Planner lost project instructions")
        if last.get("type") == "function_call_output":
            assert "Looper blocked read:" in last["output"]
            assert "outside the task directory" in last["output"]
            payload = {"steps": [{"id": "one", "instruction": "Report completion", "acceptance_criteria": ["Report done"],
                                    "depends_on": [], "review": False}]}
        elif "[DECISION]" in wire:
            payload = {"decision": "accept", "reason": "Worker completed", "summary": "Done",
                       "final": {"delivered": "Done", "how_to_use": "Read result", "verification": "Worker reported", "open_issues": "none"}}
        else:
            assert "[PLAN]" in wire
            return call_item(json.dumps({"path": str(self.outside)}), name="read")
        return text_item('```json\n' + json.dumps(payload) + '\n```')


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            assert self.headers.get("Authorization") == f"Bearer {KEY}"
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.server.requests.append(body)
            events = answer_events(self.server.script(body))
            data = b"".join(("data: " + json.dumps(e) + "\n\n").encode() for e in events)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass  # A stopped turn closes its provider stream.
        except Exception as error:
            self.server.errors.append(repr(error))
            self.send_error(500)

    def log_message(self, *args):
        pass


@contextmanager
def model_api(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("OUTSIDE-CONTENT-MUST-NOT-BE-READ")
    with Responses(outside) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.release.set()
            server.shutdown()
            thread.join(timeout=5)
            assert not thread.is_alive()
    assert not server.errors, server.errors


@pytest.fixture
def isolated_pi(tmp_path):
    require_pi()
    admin = os.environ.get("LOOPER_TEST_DATABASE_URL")
    if not admin:
        pytest.skip("Pi serve acceptance unverified: disposable LOOPER_TEST_DATABASE_URL is not set")
    require_disposable_database(admin, served=False)
    try:
        database = fresh_database(admin)
        url, _ = database.__enter__()
    except EvalError:
        pytest.skip("Pi serve acceptance unverified: disposable PostgreSQL unavailable or missing CREATEDB")
    try:
        with model_api(tmp_path) as provider, serving(database_url=url, runtime_root=tmp_path / "runtime",
                home=tmp_path / "home", work_dir=tmp_path / "work", log_path=tmp_path / "serve.log", runner="pi") as api:
            row = api.post("/api/v1/provider_accounts/create", {"name": "Synthetic Pi account", "vendor_type": "custom",
                "api_key": KEY, "endpoints": [{"protocol": "openai-responses",
                "base_url": f"http://127.0.0.1:{provider.server_port}/v1"}]})["result"]["provider"]
            model = api.post("/api/v1/ops/models/create", {"provider": row["provider_id"], "name": "test-model",
                "protocol": "openai-responses", "context_window": 100000, "efforts": [],
                "input_usd_per_mtok": 1, "output_usd_per_mtok": 2,
                "cache_read_usd_per_mtok": 3, "cache_write_usd_per_mtok": 4})["result"]["model"]
            runtime = query(url, "SELECT id FROM agent_runtimes WHERE type='pi'")
            assert len(runtime) == 1
            profile = api.post("/api/v1/profiles/create", {"name": "Synthetic Pi",
                "runtime_id": runtime[0]["id"], "model_id": model["model_id"]})["result"]["profile"]["profile_id"]
            for role in ("manager", "planner", "worker"):
                api.post("/api/v1/agent_settings/update_role", {"role": role, "values": {"profile_id": profile, "run_budget_usd": 1}})
            yield url, api, provider
        diagnostics = (tmp_path / "serve.log").read_text()
        assert KEY not in diagnostics
        cleanup = json.loads(diagnostics.split("runner test cleanup=", 1)[1])
        assert cleanup["normal_stop"] and cleanup["identity_checks"]
        assert not cleanup["stopped_after_sigint"] and not cleanup["after_cleanup"]
    finally:
        database.__exit__(None, None, None)


def send(api, message_id, text):
    api.post("/api/v1/operator_chat/send", {"text": text, "session_id": "pi-acceptance", "message_id": message_id})


def reply(api, message_id):
    messages = wait_until(lambda: api.post("/api/v1/operator_chat/messages", {"session_id": "pi-acceptance"})["result"]["messages"],
        lambda rows: any(m["message_id"] == message_id + ":reply" for m in rows))
    return next(m["content"] for m in messages if m["message_id"] == message_id + ":reply")


def records(url):
    return query(url, "SELECT * FROM llm_records WHERE agent_id='manager' ORDER BY id")


def saved_events(url, record):
    return [json.loads(row["event"]) for row in query(url,
        "SELECT event FROM llm_record_events WHERE record_id=$1 ORDER BY seq", record["id"])]


def runner_processes(tmp_path):
    processes = subprocess.run(["ps", "-A", "-o", "pid=,command="], capture_output=True, text=True, check=True, timeout=5).stdout
    owned = []
    # Runner cwd belongs to the test; its process record gives a stable identity.
    for file in (tmp_path / "runtime/.local/agent-processes").glob("*.json"):
        pid = int(file.stem)
        line = next((line for line in processes.splitlines() if line.split(maxsplit=1)[0] == str(pid)), "")
        if "pi_runner/src/main.ts" in line:
            assert "node" in line
            owned.append(pid)
        else:
            executable = shutil.which("pi")
            assert not executable or str(Path(executable).resolve()) not in line, line
    return owned


def test_pi_runner_manager_stop_and_restart(isolated_pi, tmp_path):
    from looper.agents.runtimes.runner.session import Descriptor

    url, api, provider = isolated_pi
    query(url, """INSERT INTO projects (id,name) VALUES ('proj-pi','Pi fixture');
        INSERT INTO tasks (id,project_id,requirement,status,phase)
        VALUES ('task-orchid','proj-pi','archived-orchid-6742','completed','completed')""", execute=True)
    send(api, "list", "List the tasks.")
    assert FACT in reply(api, "list")
    (record,) = records(url)
    assert record["status"] == "completed", record["error"]
    assert record["cost_source"] == "provider_price"
    assert tuple(record[k] for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")) == (2000, 400, 600, 0)
    assert Decimal(str(record["cost_usd"])) == Decimal("0.0046")
    descriptor = Descriptor.parse(json.loads(record["metadata"])["command"])
    assert (descriptor.kind, descriptor.role) == ("pi", "manager")
    events = saved_events(url, record)
    assert len({e["trace_id"] for e in events}) == 1
    (action,) = query(url, "SELECT * FROM manager_actions WHERE tool='looper_tasks'")
    assert action["status"] == "ok" and action["manager_id"] == "manager"
    assert json.loads(action["arguments"]) == {"action": "list"}
    (pid,) = runner_processes(tmp_path)
    session = query(url, "SELECT session_id FROM agent_sessions WHERE agent_id='manager'")[0]["session_id"]

    api.post("/api/v1/chat_settings/update", {"stop_enabled": True})
    send(api, "held", "[HOLD] keep this request open")
    assert provider.held.wait(15)
    turn = wait_until(lambda: api.post("/api/v1/operator_chat/turn", {"session_id": "pi-acceptance"})["result"]["turn"],
                      lambda row: row and row["client_message_id"] == "held")
    before = time.monotonic()
    api.post("/api/v1/operator_chat/stop", {"message_id": turn["message_id"]})
    wait_until(lambda: records(url), lambda rows: len(rows) == 2 and rows[-1]["status"] == "interrupted", timeout=5)
    assert time.monotonic() - before < 5
    provider.release.set()
    send(api, "continued", "Continue after stopping")
    assert FACT in reply(api, "continued")
    assert query(url, "SELECT session_id FROM agent_sessions WHERE agent_id='manager'")[0]["session_id"] == session
    assert runner_processes(tmp_path) == [pid]

    os.kill(pid, signal.SIGKILL)
    wait_until(lambda: runner_processes(tmp_path), lambda pids: bool(pids) and pid not in pids, timeout=10)
    send(api, "restarted", "Continue after runner restart")
    assert FACT in reply(api, "restarted")
    assert FACT in json.dumps(provider.requests[-1]["input"])
    assert "List the tasks" in json.dumps(provider.requests[-1]["input"])
    assert query(url, "SELECT session_id FROM agent_sessions WHERE agent_id='manager'")[0]["session_id"] == session
    assert runner_processes(tmp_path) and pid not in runner_processes(tmp_path)
    for record in records(url):
        assert len({event["trace_id"] for event in saved_events(url, record)}) == 1


def test_pi_runner_planner_and_worker(isolated_pi, tmp_path):
    from looper.agents.runtimes.runner.session import Descriptor

    url, api, provider = isolated_pi
    work = tmp_path / "project"
    work.mkdir()
    (work / "AGENTS.md").write_text(SENTINEL)
    task = api.post("/api/v1/tasks/create", {"requirement": "Report completion", "workspace_mode": "in_place", "project": {
        "name": "Pi task fixture", "work_dir": str(work)}})["result"]["task_id"]
    api.post("/api/v1/ops/task_control/start", {"task_id": task})
    detail = wait_until(lambda: api.post("/api/v1/tasks/detail", {"task_id": task})["result"],
                        lambda row: row["status"] in {"completed", "failed", "stopped"}, timeout=45)
    assert detail["status"] == "completed", (detail.get("error"), (tmp_path / "serve.log").read_text()[-3000:])
    offered = [{tool["name"] for tool in request.get("tools", [])} for request in provider.requests]
    assert any({"read", "bash", "edit", "write"} <= tools for tools in offered)
    assert not any(name.startswith("mcp__looper__") for tools in offered for name in tools)
    planner = [tools for tools in offered if "read" in tools and "bash" not in tools]
    assert planner and all(tools - {name for name in tools if name.startswith("looper_")} == {"read", "grep", "find", "ls"} for tools in planner)
    runs = query(url, "SELECT * FROM llm_records WHERE task_id=$1 ORDER BY id", task)
    assert all(row["status"] == "completed" for row in runs), [(row["agent_id"], row["error"]) for row in runs]
    assert len([row for row in runs if row["agent_id"].startswith("worker")]) == 1
    assert not any(row["agent_id"].startswith("reviewer") for row in runs)
    worker = next(row for row in runs if row["agent_id"].startswith("worker"))
    descriptor = Descriptor.parse(json.loads(worker["metadata"])["command"])
    assert descriptor.kind == "pi" and descriptor.role == "worker"
    for row in runs:
        events = saved_events(url, row)
        traces = {event["trace_id"] for event in events if "trace_id" in event}
        assert len(traces) <= 1
        assert {event["session_id"] for event in events if event.get("session_id")} == {row["session_id"]}
        if row["agent_id"].startswith("planner"):
            assert len(traces) == 1
