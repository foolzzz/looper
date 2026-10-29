"""Production Pi opt-in through isolated serve, real Pi and loopback Responses."""
from __future__ import annotations

import asyncio
from collections import Counter
from contextlib import contextmanager
from decimal import Decimal
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shlex
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
        self.usage = USAGE
        self.held, self.release = threading.Event(), threading.Event()
        self.outside = outside
        self.handler = self.default_script
        super().__init__(("127.0.0.1", 0), Handler)

    def script(self, body):
        return self.handler(body)

    def default_script(self, body):
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
            events[-1]["response"]["usage"] = self.server.usage
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
            for role in ("manager", "planner", "worker", "reviewer"):
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
    wait_until(lambda: runner_processes(tmp_path), lambda pids: pid not in pids, timeout=10)
    # A replacement starts on demand, when the next message needs a lease.
    send(api, "restarted", "Continue after runner restart")
    assert FACT in reply(api, "restarted")
    assert FACT in json.dumps(provider.requests[-1]["input"])
    assert "List the tasks" in json.dumps(provider.requests[-1]["input"])
    assert query(url, "SELECT session_id FROM agent_sessions WHERE agent_id='manager'")[0]["session_id"] == session
    assert runner_processes(tmp_path) and pid not in runner_processes(tmp_path)
    for record in records(url):
        assert len({event["trace_id"] for event in saved_events(url, record)}) == 1
        assert record["model"] == "test-model"


def test_pi_runner_missing_manager_session_records_failed_resume(isolated_pi, tmp_path):
    url, api, provider = isolated_pi
    send(api, "initial", "Start a conversation")
    assert FACT in reply(api, "initial")
    (initial,) = records(url)
    assert initial["status"] == "completed"
    session = initial["session_id"]
    assert session
    (saved,) = (tmp_path / "runtime/runners/state/pi/manager").glob(f"*_{session}.jsonl")
    saved.unlink()  # Keep the database's saved session, but lose the runner's history.
    (pid,) = runner_processes(tmp_path)
    os.kill(pid, signal.SIGKILL)
    wait_until(lambda: runner_processes(tmp_path), lambda pids: pid not in pids, timeout=10)

    before = len(provider.requests)
    send(api, "missing", "Continue after losing the saved session")
    assert FACT in reply(api, "missing")
    _, failed, recovered = records(url)
    assert failed["status"] == "failed" and "could not be resumed" in failed["error"]
    assert (failed["session_id"], failed["model"]) == (session, "test-model")
    (init,) = [e for e in saved_events(url, failed) if e.get("subtype") == "init"]
    assert (init["session_id"], init["model"]) == (session, "test-model")
    assert recovered["status"] == "completed" and recovered["model"] == "test-model"
    assert recovered["session_id"] and recovered["session_id"] != session
    assert len(provider.requests) == before + 1  # The failed load never prompts the provider.


@pytest.mark.parametrize("usage,counts,cost", [
    (USAGE, (1000, 200, 300, 0), "0.0023"),
    ({**USAGE, "total_tokens": 1400, "output_tokens_details": {"reasoning_tokens": 201},
        "input_tokens_details": {"cached_tokens": 1400}}, (0, 200, 1400, 0), "0.0046"),
], ids=["charged", "priceable-terminal-buckets"])
def test_pi_runner_planner_and_worker(isolated_pi, tmp_path, usage, counts, cost):
    from looper.agents.runtimes.runner.session import Descriptor

    url, api, provider = isolated_pi
    provider.usage = usage
    work = tmp_path / "project"
    work.mkdir()
    (work / "AGENTS.md").write_text(SENTINEL)
    task = api.post("/api/v1/tasks/create", {"requirement": "Report completion", "workspace_mode": "in_place", "project": {
        "name": "Pi task fixture", "work_dir": str(work), "task_budget_usd": 2}})["result"]["task_id"]
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
        (result,) = [event for event in events if event.get("type") == "result"]
        requests = result["num_turns"]
        assert result["usage_complete"] and result["runner"]["cap_reason"] is None
        assert requests > 0 and row["cost_source"] == "provider_price"
        assert tuple(row[k] for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")) == tuple(
            requests * count for count in counts)
        assert Decimal(str(row["cost_usd"])) == requests * Decimal(cost)
        traces = {event["trace_id"] for event in events if "trace_id" in event}
        assert len(traces) <= 1
        assert {event["session_id"] for event in events if event.get("session_id")} == {row["session_id"]}
        if row["agent_id"].startswith("planner"):
            assert len(traces) == 1


def current_prompt(body):
    # Saved sessions include old PLAN/DECISION turns and, for forks, the source run.
    message = next(item for item in reversed(body["input"]) if item.get("role") == "user")
    content = message["content"]
    return content if isinstance(content, str) else "\n".join(part.get("text", "") for part in content)


def request_role(body):
    tools = {tool["name"] for tool in body.get("tools", [])}
    prompt = current_prompt(body)
    if "[CONSULT]" in prompt:
        assert tools == {"read", "grep", "find", "ls"}
        return "consult"
    if prompt.startswith("Review the step"):
        assert {"read", "bash", "edit", "write"} <= tools
        return "reviewer"
    if "bash" in tools:
        assert {"read", "bash", "edit", "write"} <= tools
        return re.search(r"Your step: [^\n]*\n(STEP-[A-Z]+)", body["instructions"])[1]
    assert tools - {name for name in tools if name.startswith("looper_")} == {"read", "grep", "find", "ls"}
    assert any(marker in prompt for marker in ("[PLAN]", "[CHAT]", "[DECISION]"))
    assert SENTINEL in body["instructions"]
    return "planner"


def json_answer(payload):
    return text_item('```json\n' + json.dumps(payload) + '\n```')


def report(*files):
    return json_answer({"status": "completed", "changed_files": dict.fromkeys(files, "Stored answer"),
                        "deleted_files": [], "summary": "Done"})


def step(key, marker, *, review=False, depends_on=()):
    return {"id": key, "instruction": marker, "acceptance_criteria": ["Report done"],
            "depends_on": list(depends_on), "review": review}


def accepted(*, final=False):
    result = {"decision": "accept", "reason": "Result checked", "summary": "Done"}
    if final:
        result["final"] = {"delivered": "Done", "how_to_use": "Read the files",
                           "verification": "Reviewed", "open_issues": "none"}
    return json_answer(result)


def start_task(api, tmp_path, requirement):
    work = tmp_path / requirement
    work.mkdir()
    (work / "AGENTS.md").write_text(SENTINEL)
    task = api.post("/api/v1/tasks/create", {"requirement": requirement, "workspace_mode": "in_place",
        "project": {"name": requirement, "work_dir": str(work)}})["result"]["task_id"]
    api.post("/api/v1/ops/task_control/start", {"task_id": task})
    return task, work


def task_records(url, task):
    return query(url, "SELECT * FROM llm_records WHERE task_id=$1 ORDER BY id", task)


def task_ended(api, task):
    return wait_until(lambda: api.post("/api/v1/tasks/detail", {"task_id": task})["result"],
                      lambda row: row["status"] in {"completed", "failed", "stopped"}, timeout=45)


def test_pi_runner_task_with_every_role(isolated_pi, tmp_path):
    from looper.agents.runtimes.runner.session import Descriptor

    url, api, provider = isolated_pi
    counts, planner_counts = Counter(), Counter()
    started, release = tmp_path / "started", tmp_path / "release"
    source_saved = threading.Event()
    requests_by_role = {}

    def handle(body):
        role = request_role(body)
        counts[role] += 1
        n = counts[role]
        requests_by_role.setdefault(role, []).append(body)
        wire, prompt = json.dumps(body["input"]), current_prompt(body)
        if role == "planner":
            planner_counts[prompt] += 1
            if "[PLAN]" in prompt:
                return json_answer({"steps": [step("one", "STEP-ONE", review=True),
                                              step("two", "STEP-TWO", depends_on=["one"])]})
            if "[CHAT]" in prompt:
                return json_answer({"reply": "Passed on", "action": "forward", "step": "one", "text": "STEER-TOKEN"})
            assert "[DECISION]" in prompt
            if "has its Worker's result" in prompt and "[DECISION] Step one " in prompt:
                return json_answer({"decision": "review", "reason": "Check the file"})
            return accepted(final="[DECISION] Step two " in prompt)
        if role == "STEP-ONE":
            if n == 1:
                return call_item(json.dumps({"command": f"touch {shlex.quote(str(started))}; "
                    f"while [ ! -e {shlex.quote(str(release))} ]; do sleep 0.1; done"}), name="bash")
            if n == 2:
                assert "STEER-TOKEN" in wire
                return call_item('{"path":"one.txt","content":"STEER-TOKEN"}', name="write", call_id="write-one")
            assert n == 3
            return report("one.txt")
        if role == "reviewer":
            if n == 1:
                assert source_saved.wait(20), "source session was not captured after Worker completion"
                return call_item('{"command":"cat one.txt"}', name="bash")
            assert n == 2 and "STEER-TOKEN" in body["input"][-1]["output"]
            return json_answer({"passed": True, "findings": [], "summary": "ok"})
        if role == "STEP-TWO":
            if n == 1:
                return call_item('{"kind":"question","to":["worker@one"],"body":"What token did you store?"}', name="looper_room_post")
            if n == 2:
                return call_item('{"wait_seconds":60}', name="looper_room_inbox", call_id="inbox-two")
            if n == 3:
                assert "STEER-TOKEN" in body["input"][-1]["output"]
                return call_item('{"path":"two.txt","content":"STEER-TOKEN"}', name="write", call_id="write-two")
            assert n == 4
            return report("two.txt")
        assert role == "consult"
        if n == 1:
            return call_item(json.dumps({"path": str(provider.outside)}), name="read", call_id="outside-consult")
        assert n == 2 and "Looper blocked read:" in body["input"][-1]["output"]
        assert "OUTSIDE-CONTENT-MUST-NOT-BE-READ" not in wire
        question = int(re.search(r"#(\d+) from", prompt)[1])
        return json_answer({"answers": [{"reply_to": question, "body": "STEER-TOKEN"}]})

    provider.handler = handle
    task, work = start_task(api, tmp_path, "STEP-ONE and STEP-TWO")
    try:
        wait_until(started.exists)
        (pid,) = runner_processes(tmp_path)
        api.post("/api/v1/operator_chat/send", {"text": "Pass STEER-TOKEN to step one", "session_id": "pi-acceptance",
            "message_id": "steer", "target": {"kind": "planner", "task_id": task}})
        assert "Passed on" in reply(api, "steer")
        release.touch()
        source = wait_until(lambda: task_records(url, task),
            lambda rows: any(row["agent_id"].startswith("worker") and row["status"] == "completed" for row in rows))
        source = next(row for row in source if row["agent_id"].startswith("worker") and row["status"] == "completed")
        directory = tmp_path / "runtime/.local/pi/run-sessions" / task / source["agent_id"]
        (session_file,) = directory.glob("*.jsonl")
        digest = hashlib.sha256(session_file.read_bytes()).hexdigest()
    finally:
        release.touch()
        source_saved.set()
    detail = task_ended(api, task)
    assert detail["status"] == "completed", detail.get("error")
    assert runner_processes(tmp_path) == [pid]
    assert (work / "one.txt").read_text() == (work / "two.txt").read_text() == "STEER-TOKEN"
    assert counts == {"planner": 5, "STEP-ONE": 3, "reviewer": 2, "STEP-TWO": 4, "consult": 2}
    assert not any(tool["name"].startswith("mcp__looper__") for body in provider.requests for tool in body.get("tools", []))
    runs = task_records(url, task)
    (consult,) = query(url, "SELECT * FROM room_consults WHERE task_id=$1", task)
    assert consult["state"] == "answered"
    assert consult["source_record"] == source["id"]
    expected = Counter(planner=5, worker=3, reviewer=1)
    actual = Counter()
    planner_requests = 0
    for row in runs:
        assert row["status"] == "completed", (row["agent_id"], row["error"])
        assert row["model"] == "test-model"
        descriptor = Descriptor.parse(json.loads(row["metadata"])["command"])
        assert descriptor.kind == "pi"
        role = ("planner" if row["agent_id"].startswith("planner") else
                "consult" if row["agent_id"] == consult["consult_run"] else
                "reviewer" if row["agent_id"].startswith("reviewer") else
                "STEP-ONE" if row["id"] == source["id"] else "STEP-TWO")
        expected_role = "worker" if role in {"consult", "STEP-ONE", "STEP-TWO"} else role
        assert descriptor.role == expected_role
        actual[descriptor.role] += 1
        if role == "consult":
            assert descriptor.read_only and descriptor.fork_from == source["session_id"]
            assert set(descriptor.builtin) == {"read", "grep", "find", "ls"}
        elif role != "planner":
            assert set(descriptor.builtin) == {"read", "bash", "edit", "write"}
        events = saved_events(url, row)
        (result,) = [e for e in events if e.get("type") == "result"]
        traces = {e["trace_id"] for e in events if e.get("trace_id")}
        assert result["trace_id"] and traces == {result["trace_id"]}
        assert {e["session_id"] for e in events if e.get("session_id")} == {row["session_id"]}
        if row["id"] == source["id"]:
            (steer,) = [e for e in events if e.get("type") == "system" and e.get("subtype") == "steer_applied"]
            assert steer["trace_id"] == result["trace_id"]
        k = planner_counts[row["input_text"]] if role == "planner" else counts[role]
        assert k > 0
        if role == "planner":
            planner_requests += k
        assert tuple(row[key] for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")) == (k * 1000, k * 200, k * 300, 0)
        assert row["cost_source"] == "provider_price"
        assert float(row["cost_usd"]) == pytest.approx(k * 0.0023)
    assert actual == expected
    assert planner_requests == counts["planner"]
    activity = query(url, "SELECT summary,detail FROM activity WHERE task_id=$1", task)
    assert "reached the model" in json.dumps(activity)
    assert hashlib.sha256(session_file.read_bytes()).hexdigest() == digest
    fork = tmp_path / "runtime/.local/pi/consult-sessions" / task / consult["consult_run"]
    assert not fork.exists()


def test_pi_runner_worker_cancel_crash_and_budget(isolated_pi, tmp_path):
    from looper.agents import proc_tree
    from types import SimpleNamespace

    url, api, provider = isolated_pi
    counts = Counter()
    mode = "BUDGET"
    started = tmp_path / "crash-started"

    def handle(body):
        role = request_role(body)
        counts[(mode, role)] += 1
        if role == "planner":
            prompt = current_prompt(body)
            if "[PLAN]" in prompt:
                return json_answer({"steps": [step("one", "STEP-" + mode)]})
            assert "[DECISION]" in prompt
            # A restarted runner may execute a retry; any failed result must fail the task.
            return accepted(final=True) if "Allowed decisions now: accept" in prompt else json_answer({"decision": "fail", "reason": "Worker failed"})
        assert role == "STEP-" + mode
        if mode == "CANCEL":
            provider.held.set()
            assert provider.release.wait(30), "cancelled Worker request was not released"
        if mode == "CRASH" and counts[(mode, role)] == 1:
            return call_item(json.dumps({"command": f"touch {shlex.quote(str(started))}; sleep 60"}), name="bash")
        return report()

    provider.handler = handle
    api.post("/api/v1/agent_settings/update_role", {"role": "worker", "values": {"run_budget_usd": 0.001}})
    task, _ = start_task(api, tmp_path, "Budget denial")
    assert task_ended(api, task)["status"] == "failed"
    (failed_step,) = query(url, "SELECT result FROM task_steps WHERE task_id=$1", task)
    assert "per-run spend cap" in json.loads(failed_step["result"])["error"]
    assert counts[("BUDGET", "STEP-BUDGET")] == 0
    api.post("/api/v1/agent_settings/update_role", {"role": "worker", "values": {"run_budget_usd": 1}})

    mode = "CANCEL"
    task, _ = start_task(api, tmp_path, "Cancel Worker")
    assert provider.held.wait(20)
    (pid,) = runner_processes(tmp_path)
    children = proc_tree._tree(SimpleNamespace(pid=pid, returncode=None), proc_tree._snapshot(), {})
    assert pid in children, "positive control: process snapshot must include the live runner"
    before = time.monotonic()
    api.post("/api/v1/ops/task_control/stop", {"task_id": task})
    wait_until(lambda: task_records(url, task),
        lambda rows: any(row["agent_id"].startswith("worker") and row["status"] == "interrupted" for row in rows), timeout=5)
    assert time.monotonic() - before < 5
    provider.release.set()
    assert task_ended(api, task)["status"] == "stopped"
    assert runner_processes(tmp_path) == [pid]
    wait_until(lambda: set(proc_tree._snapshot()) & (set(children) - {pid}), lambda pids: not pids, timeout=5)

    mode = "CRASH"
    task, _ = start_task(api, tmp_path, "Crash Worker")
    wait_until(started.exists)
    # The supervisor samples descendants every 0.5 s. The earlier crash window is a deferred gap.
    time.sleep(1)
    children = proc_tree._tree(SimpleNamespace(pid=pid, returncode=None), proc_tree._snapshot(), {})
    assert set(children) - {pid}, "positive control: the long-running bash must have a child"
    commands = subprocess.run(["ps", "-p", ",".join(map(str, children)), "-o", "command="],
                              capture_output=True, text=True, check=True, timeout=5).stdout
    assert any(line.strip() == "sleep 60" for line in commands.splitlines()), commands
    worker = next(row for row in task_records(url, task) if row["agent_id"].startswith("worker"))
    killed_at = time.monotonic()
    os.kill(pid, signal.SIGKILL)
    wait_until(lambda: set(proc_tree._snapshot()) & (set(children) - {pid}), lambda pids: not pids, timeout=5)
    assert time.monotonic() - killed_at < 5
    ended = wait_until(lambda: query(url, "SELECT * FROM llm_records WHERE id=$1", worker["id"])[0],
                       lambda row: row["status"] != "running", timeout=10)
    assert "runner" in ended["error"] and any(word in ended["error"] for word in ("lost", "exited"))
    detail = task_ended(api, task)
    assert detail["status"] in {"completed", "failed"}, detail.get("error")
    activity = query(url, "SELECT summary,detail FROM activity WHERE task_id=$1", task)
    assert ended["error"] in json.dumps(activity)
    step_result = query(url, "SELECT result FROM task_steps WHERE task_id=$1", task)[0]["result"]
    if detail["status"] == "failed":
        assert "runner" in step_result
    # New work, rather than the crash itself, must start the replacement.
    recovery_task, _ = start_task(api, tmp_path, "Work after runner crash")
    assert task_ended(api, recovery_task)["status"] == "completed"
    (restarted,) = wait_until(lambda: runner_processes(tmp_path), lambda pids: len(pids) == 1 and pids[0] != pid, timeout=10)
    assert restarted != pid
    assert not any(row["status"] == "running" for row in task_records(url, task))
