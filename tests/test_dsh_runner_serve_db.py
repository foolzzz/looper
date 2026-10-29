"""Every DSH role through isolated serve, installed SDK and local Messages."""
from __future__ import annotations

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
import shutil
import signal
import subprocess
import threading
import time

import pytest

from evals.prompt_regression.runner import EvalError, fresh_database
from tests.dsh_installed_stand_in import Reply, messages_wire, tool
from tests.dsh_runner_support import require_dsh
from tests.runner_support import require_disposable_database, serving
from tests.test_pi_runner_serve_db import (
    query, wait_until, step, start_task, task_records, task_ended, saved_events, SENTINEL,
)

pytestmark = pytest.mark.installed
KEY = "synthetic-dsh-serve-key"
FACT = "archived-orchid-6742"
USAGE = {"input": 1000, "output": 200, "cache_read": 300, "cache_write": 0}


def current_prompt(body):
    # Tool results are user messages too; find the most recent actual user text.
    for message in reversed(body["messages"]):
        if message["role"] == "user":
            content = message["content"]
            text = content if isinstance(content, str) else "\n".join(
                block.get("text", "") for block in content if block["type"] == "text")
            if text:
                return text
    raise AssertionError("request has no user text")


def request_role(body):
    tools = {tool["name"] for tool in body.get("tools", [])}
    assert not any(name.startswith("mcp__looper__") for name in tools)
    prompt = current_prompt(body)
    if "looper_tasks" in tools:
        return "manager"
    if "[CONSULT]" in prompt:
        assert {"read", "glob", "grep"} <= tools
        return "consult"
    if prompt.startswith("Review the step"):
        assert {"read", "glob", "grep", "write", "edit", "bash"} <= tools
        return "reviewer"
    if "bash" in tools:
        system = body["system"]
        text = system if isinstance(system, str) else "\n".join(block.get("text", "") for block in system)
        return re.search(r"Your step: [^\n]*\n(STEP-[A-Z]+)", text)[1]
    assert tools - {name for name in tools if name.startswith("looper_")} == {"read", "glob", "grep"}
    assert any(marker in prompt for marker in ("[PLAN]", "[CHAT]", "[DECISION]", "[RESTART]"))
    assert SENTINEL in json.dumps(body["system"])
    return "planner"


def json_answer(payload):
    return '```json\n' + json.dumps(payload) + '\n```'


def report(*files):
    return json_answer({"status": "completed", "changed_files": dict.fromkeys(files, "Stored answer"),
                        "deleted_files": [], "summary": "Done"})


def accepted(*, final=False):
    payload = {"decision": "accept", "reason": "Result checked", "summary": "Done"}
    if final:
        payload["final"] = {"delivered": "Done", "how_to_use": "Read the files",
                            "verification": "Reviewed", "open_issues": "none"}
    return json_answer(payload)


class Messages(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, outside):
        self.requests, self.errors = [], []
        self.counts = Counter()
        self.record_counts = Counter()
        self.held, self.release = threading.Event(), threading.Event()
        self.outside = outside
        self.handler = self.manager_script
        super().__init__(("127.0.0.1", 0), Handler)

    def manager_script(self, body):
        assert request_role(body) == "manager"
        prompt = current_prompt(body)
        if "[HOLD]" in prompt:
            self.held.set()
            assert self.release.wait(30), "held Manager request was not released"
        if self.counts["manager"] == 1:
            return tool("looper_tasks", {"action": "list"})
        assert FACT in json.dumps(body["messages"])
        return FACT


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            assert self.path == "/v1/messages"
            assert self.headers.get("x-api-key") == KEY
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.server.requests.append(body)
            if hasattr(self.server, "request_log"):
                with self.server.request_log.open("a") as log:
                    log.write(json.dumps(body) + "\n")
            key = request_role(body)
            assert self.headers.get("x-looper-request-id")
            # The display input_text can differ from the SDK's wire messages.
            # Attribute at arrival to the sole active record of this role.
            role = "worker" if key.startswith("STEP-") else key
            (record,) = [row for row in query(self.server.database_url,
                "SELECT id,agent_id FROM llm_records WHERE status='running'")
                if row["agent_id"].split("-", 1)[0] == role]
            self.server.record_counts[record["id"]] += 1
            self.server.counts[key] += 1
            block = self.server.handler(body)
            wire = messages_wire(Reply([block], usage=USAGE), len(self.server.requests)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(wire)))
            self.end_headers()
            self.wfile.write(wire)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as error:
            self.server.errors.append(repr(error))
            self.send_error(500)

    def log_message(self, *args):
        pass


@contextmanager
def model_api(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("OUTSIDE-CONTENT-MUST-NOT-BE-READ")
    with Messages(outside) as server:
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
def isolated_dsh(tmp_path):
    require_dsh()
    admin = os.environ.get("LOOPER_TEST_DATABASE_URL")
    if not admin:
        pytest.skip("DSH serve acceptance unverified: disposable LOOPER_TEST_DATABASE_URL is not set")
    require_disposable_database(admin, served=False)
    try:
        database = fresh_database(admin)
        url, _ = database.__enter__()
    except EvalError:
        pytest.skip("DSH serve acceptance unverified: disposable PostgreSQL unavailable or missing CREATEDB")
    try:
        with model_api(tmp_path) as provider, serving(database_url=url, runtime_root=tmp_path / "runtime",
                home=tmp_path / "home", work_dir=tmp_path / "work", log_path=tmp_path / "serve.log", runner="dsh") as api:
            provider.database_url = url
            provider.request_log = tmp_path / "requests.jsonl"
            row = api.post("/api/v1/provider_accounts/create", {"name": "Synthetic DSH account", "vendor_type": "custom",
                "api_key": KEY, "endpoints": [{"protocol": "anthropic-messages",
                "base_url": f"http://127.0.0.1:{provider.server_port}/v1"}]})["result"]["provider"]
            model = api.post("/api/v1/ops/models/create", {"provider": row["provider_id"], "name": "deepseek-flash",
                "protocol": "anthropic-messages", "context_window": 100000, "efforts": [],
                "input_usd_per_mtok": 1, "output_usd_per_mtok": 2,
                "cache_read_usd_per_mtok": 3, "cache_write_usd_per_mtok": 4})["result"]["model"]
            (runtime,) = query(url, "SELECT id FROM agent_runtimes WHERE type='dsh'")
            profile = api.post("/api/v1/profiles/create", {"name": "Synthetic DSH",
                "runtime_id": runtime["id"], "model_id": model["model_id"]})["result"]["profile"]["profile_id"]
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


def dsh_runner_processes(tmp_path):
    processes = subprocess.run(["ps", "-ww", "-A", "-o", "pid=,command="], capture_output=True,
                               text=True, check=True, timeout=5).stdout
    owned = []
    for file in (tmp_path / "runtime/.local/agent-processes").glob("*.json"):
        pid = int(file.stem)
        line = next((line for line in processes.splitlines() if line.split(maxsplit=1)[0] == str(pid)), "")
        if not line:  # A killed runner's process record may be awaiting supervisor cleanup.
            continue
        command = shlex.split(line.split(maxsplit=1)[1])
        assert Path(command[0]).name == "node" and any(
            arg.endswith("dsh_runner/src/main.ts") for arg in command[1:]), line
        owned.append(pid)
    return owned


def send(api, message_id, text):
    api.post("/api/v1/operator_chat/send", {"text": text, "session_id": "dsh-acceptance", "message_id": message_id})


def reply(api, message_id, *, timeout=30):
    messages = wait_until(lambda: api.post("/api/v1/operator_chat/messages", {"session_id": "dsh-acceptance"})["result"]["messages"],
        lambda rows: any(m["message_id"] == message_id + ":reply" for m in rows), timeout=timeout)
    return next(m["content"] for m in messages if m["message_id"] == message_id + ":reply")


def records(url):
    return query(url, "SELECT * FROM llm_records WHERE agent_id='manager' ORDER BY id")


def cli_consult_request(tmp_path, *, state, source, system_prompt, cwd, prompt):
    """Replay the exact ended Worker history via the installed CLI consult path."""
    from looper.agents.agent_settings import AgentSettings, AgentType, Launch
    from looper.agents.consult import consult_prompt
    from looper.agents.profile_registry import LegacyProviderSpec, RuntimeSpec
    from looper.agents.runtimes import ApiProtocol, AuthKind, dsh
    from tests.dsh_installed_stand_in import MessagesServer
    from tests.dsh_runner_support import guard_env
    from tests.runtime_model_helpers import resolved_profile

    _, executable = require_dsh()
    owner = tmp_path / "cli-source"
    shutil.copytree(state, owner / "dsh/sessions")
    (owner / ".session.lock").touch()
    file = tmp_path / ".local/prompts/cli-consult.md"
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(consult_prompt(system_prompt))
    runtime = dsh.DshRuntime()
    with MessagesServer([[json_answer({"answers": []})]]) as server:
        provider = LegacyProviderSpec("offline", "Synthetic", AuthKind.API_KEY,
            frozenset({ApiProtocol.ANTHROPIC_MESSAGES}), base_url=server.url, api_key=KEY)
        profile = resolved_profile("offline", "Synthetic", RuntimeSpec("offline", "DSH", "dsh", executable,
            runtime.capabilities), provider, "deepseek-flash", "high")
        launch = Launch(AgentSettings(profile=profile), role=AgentType.WORKER, consult=True)
        command = runtime.consult_command(executable, cwd=cwd, prompt_file=file, launch=launch,
            session_id=source, session_dir=owner, fork_dir=tmp_path / "cli-fork")
        process = subprocess.Popen(command, cwd=cwd, env={"PATH": os.environ["PATH"],
            "DEEPSEEK_API_KEY": KEY, **guard_env(tmp_path / "cli-guard", server.port)},
            text=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        try:
            stdout, stderr = process.communicate(prompt, timeout=30)
            assert process.returncode == 0, stderr + stdout
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate(timeout=5)
        assert len(server.requests) == 1
        body = server.requests[0]["body"]
        (tmp_path / "cli-consult-request.json").write_text(json.dumps(body))
        return body


def test_dsh_runner_manager_turn_stop_and_restart(isolated_dsh, tmp_path):
    from looper.agents.runtimes.runner.session import Descriptor

    url, api, provider = isolated_dsh
    query(url, """INSERT INTO projects (id,name) VALUES ('proj-dsh','DSH fixture');
        INSERT INTO tasks (id,project_id,requirement,status,phase)
        VALUES ('task-orchid','proj-dsh','archived-orchid-6742','completed','completed')""", execute=True)
    send(api, "list", "List the tasks.")
    assert FACT in reply(api, "list")
    (record,) = records(url)
    assert record["status"] == "completed", record["error"]
    assert record["cost_source"] == "provider_price"
    assert tuple(record[k] for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")) == (2000, 400, 600, 0)
    assert Decimal(str(record["cost_usd"])) == Decimal("0.0046")
    descriptor = Descriptor.parse(json.loads(record["metadata"])["command"])
    assert (descriptor.kind, descriptor.role) == ("dsh", "manager")
    events = saved_events(url, record)
    assert len({e["trace_id"] for e in events}) == 1
    (action,) = query(url, "SELECT * FROM manager_actions WHERE tool='looper_tasks'")
    assert action["status"] == "ok" and action["manager_id"] == "manager"
    assert json.loads(action["arguments"]) == {"action": "list"}
    (pid,) = dsh_runner_processes(tmp_path)
    session = query(url, "SELECT session_id FROM agent_sessions WHERE agent_id='manager'")[0]["session_id"]

    api.post("/api/v1/chat_settings/update", {"stop_enabled": True})
    send(api, "held", "[HOLD] keep this request open")
    assert provider.held.wait(15)
    turn = wait_until(lambda: api.post("/api/v1/operator_chat/turn", {"session_id": "dsh-acceptance"})["result"]["turn"],
                      lambda row: row and row["client_message_id"] == "held")
    before = time.monotonic()
    api.post("/api/v1/operator_chat/stop", {"message_id": turn["message_id"]})
    wait_until(lambda: records(url), lambda rows: len(rows) == 2 and rows[-1]["status"] == "interrupted", timeout=5)
    assert "已停止" in reply(api, "held", timeout=5)
    wait_until(lambda: api.post("/api/v1/operator_chat/turn", {"session_id": "dsh-acceptance"})["result"]["turn"],
               lambda row: row is None, timeout=5)
    assert time.monotonic() - before < 5
    provider.release.set()
    send(api, "continued", "Continue after stopping")
    assert FACT in reply(api, "continued")
    assert query(url, "SELECT session_id FROM agent_sessions WHERE agent_id='manager'")[0]["session_id"] == session
    assert dsh_runner_processes(tmp_path) == [pid]

    os.kill(pid, signal.SIGKILL)
    wait_until(lambda: dsh_runner_processes(tmp_path), lambda pids: bool(pids) and pid not in pids, timeout=10)
    send(api, "restarted", "Continue after runner restart")
    assert FACT in reply(api, "restarted")
    assert FACT in json.dumps(provider.requests[-1]["messages"])
    assert "List the tasks" in json.dumps(provider.requests[-1]["messages"])
    assert query(url, "SELECT session_id FROM agent_sessions WHERE agent_id='manager'")[0]["session_id"] == session
    assert dsh_runner_processes(tmp_path) and pid not in dsh_runner_processes(tmp_path)
    for record in records(url):
        assert len({event["trace_id"] for event in saved_events(url, record)}) == 1


def test_dsh_runner_task_with_every_role(isolated_dsh, tmp_path):
    from looper.agents.runtimes.runner.session import Descriptor

    url, api, provider = isolated_dsh
    counts = provider.counts
    started, release = tmp_path / "started", tmp_path / "release"
    source_saved = threading.Event()

    def handle(body):
        role = request_role(body)
        n = counts[role]
        wire, prompt = json.dumps(body["messages"]), current_prompt(body)
        if role == "planner":
            if "[PLAN]" in prompt:
                if n == 1:
                    return tool("write", {"file_path": "plan.txt", "content": "must be denied"})
                assert "LOOPER_READ_ONLY" in json.dumps(body["messages"][-1])
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
                return tool("bash", {"command": f"touch {shlex.quote(str(started))}; "
                    f"while [ ! -e {shlex.quote(str(release))} ]; do sleep 0.1; done"})
            if n == 2:
                assert "STEER-TOKEN" in wire
                return tool("write", {"file_path": "one.txt", "content": "STEER-TOKEN"})
            assert n == 3
            return report("one.txt")
        if role == "reviewer":
            if n == 1:
                assert source_saved.wait(20), "source session was not captured after Worker completion"
                return tool("bash", {"command": "cat one.txt"})
            assert n == 2 and "STEER-TOKEN" in json.dumps(body["messages"][-1])
            return json_answer({"passed": True, "findings": [], "summary": "ok"})
        if role == "STEP-TWO":
            if n == 1:
                return tool("looper_room_post", {"kind": "question", "to": ["worker@one"], "body": "What token did you store?"})
            if n == 2:
                return tool("looper_room_inbox", {"wait_seconds": 60})
            if n == 3:
                assert "STEER-TOKEN" in json.dumps(body["messages"][-1])
                return tool("write", {"file_path": "two.txt", "content": "STEER-TOKEN"})
            assert n == 4
            return report("two.txt")
        assert role == "consult"
        if n == 1:
            return tool("read", {"file_path": str(provider.outside)})
        assert n == 2 and "LOOPER_PATH" in json.dumps(body["messages"][-1])
        assert "OUTSIDE-CONTENT-MUST-NOT-BE-READ" not in wire
        question = int(re.search(r"#(\d+) from", prompt)[1])
        return json_answer({"answers": [{"reply_to": question, "body": "STEER-TOKEN"}]})

    provider.handler = handle
    task, work = start_task(api, tmp_path, "STEP-ONE and STEP-TWO")
    try:
        wait_until(started.exists)
        (pid,) = dsh_runner_processes(tmp_path)
        api.post("/api/v1/operator_chat/send", {"text": "Pass STEER-TOKEN to step one", "session_id": "dsh-acceptance",
            "message_id": "steer", "target": {"kind": "planner", "task_id": task}})
        assert "Passed on" in reply(api, "steer")
        release.touch()
        source = wait_until(lambda: task_records(url, task),
            lambda rows: any(row["agent_id"].startswith("worker") and row["status"] == "completed" for row in rows))
        source = next(row for row in source if row["agent_id"].startswith("worker") and row["status"] == "completed")
        state = tmp_path / "runtime/runners/state/dsh-native"
        (directory,) = state.glob(f"*/{source['session_id']}")
        def hashes():
            return {str(file.relative_to(directory)): hashlib.sha256(file.read_bytes()).hexdigest()
                    for file in directory.rglob("*") if file.is_file()}
        digest = hashes()
        assert digest, "positive control: Worker session has stored files"
    finally:
        release.touch()
        source_saved.set()
    detail = task_ended(api, task)
    assert detail["status"] == "completed", detail.get("error")
    assert dsh_runner_processes(tmp_path) == [pid]
    assert not (work / "plan.txt").exists()
    assert (work / "one.txt").read_text() == (work / "two.txt").read_text() == "STEER-TOKEN"
    assert counts == {"planner": 6, "STEP-ONE": 3, "reviewer": 2, "STEP-TWO": 4, "consult": 2}
    assert not any(tool["name"].startswith("mcp__looper__") for body in provider.requests for tool in body.get("tools", []))
    runs = task_records(url, task)
    (consult,) = query(url, "SELECT * FROM room_consults WHERE task_id=$1", task)
    assert consult["state"] == "answered"
    assert consult["source_record"] == source["id"]
    expected = Counter(planner=5, worker=3, reviewer=1)
    actual = Counter()
    for row in runs:
        assert row["status"] == "completed", (row["agent_id"], row["error"])
        descriptor = Descriptor.parse(json.loads(row["metadata"])["command"])
        assert descriptor.kind == "dsh"
        role = ("planner" if row["agent_id"].startswith("planner") else
                "consult" if row["agent_id"] == consult["consult_run"] else
                "reviewer" if row["agent_id"].startswith("reviewer") else
                "STEP-ONE" if row["id"] == source["id"] else "STEP-TWO")
        expected_role = "worker" if role in {"consult", "STEP-ONE", "STEP-TWO"} else role
        assert descriptor.role == expected_role
        actual[descriptor.role] += 1
        if role in {"consult", "planner"}:
            assert descriptor.read_only and set(descriptor.builtin) == {"read", "glob", "grep"}
            if role == "consult":
                assert descriptor.fork_from == source["session_id"]
                fork_session = row["session_id"]
        else:
            assert not descriptor.read_only
            assert set(descriptor.builtin) == {"read", "glob", "grep", "write", "edit", "bash"}
        events = saved_events(url, row)
        (result,) = [e for e in events if e.get("type") == "result"]
        traces = {e["trace_id"] for e in events if e.get("trace_id")}
        assert result["trace_id"] and traces == {result["trace_id"]}
        assert {e["session_id"] for e in events if e.get("session_id")} == {row["session_id"]}
        if row["id"] == source["id"]:
            (steer,) = [e for e in events if e.get("type") == "system" and e.get("subtype") == "steer_applied"]
            assert steer["trace_id"] == result["trace_id"]
        k = provider.record_counts[row["id"]]
        assert k > 0
        assert result["usage_complete"] and result["num_turns"] == k
        assert tuple(row[key] for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")) == (k * 1000, k * 200, k * 300, 0)
        assert row["cost_source"] == "provider_price"
        assert float(row["cost_usd"]) == pytest.approx(k * 0.0023)
    assert actual == expected
    assert set(provider.record_counts) == {row["id"] for row in runs}
    assert sum(provider.record_counts.values()) == len(provider.requests)
    activity = query(url, "SELECT summary,detail FROM activity WHERE task_id=$1", task)
    assert "reached the model" in json.dumps(activity)
    assert hashes() == digest
    assert not list(state.glob(f"*/{fork_session}"))
    fork = tmp_path / "runtime/.local/dsh/consult-sessions" / task / consult["consult_run"]
    assert not fork.exists()
    runner_request = next(body for body in provider.requests if request_role(body) == "consult")
    cli_request = cli_consult_request(tmp_path, state=state, source=source["session_id"],
        system_prompt=json.loads(source["metadata"])["system_prompt"], cwd=work, prompt=current_prompt(runner_request))
    (tmp_path / "runner-consult-request.json").write_text(json.dumps(runner_request))
    # Private replay input for the opt-in real-model comparison. No credentials.
    (tmp_path / "consult-source.json").write_text(json.dumps({
        "state": str(state), "source": source["session_id"], "cwd": str(work),
        "system_prompt": runner_request["system"][runner_request["system"].index("# Runtime notes (DSH)"):],
        "prompt": current_prompt(runner_request)}))
    from looper.agents.consult import CONSULT_CONTRACT, consult_prompt
    from looper.agents.system_prompt import WORK_CONTRACT

    # Compare complete history, not presence of a few markers: tool ids, results,
    # applied steering, the final Worker report and the question stay in order.
    assert runner_request["messages"][:-1] == cli_request["messages"][:-1]
    assert runner_request["messages"][-2]["content"][0]["text"] == report("one.txt")
    question = runner_request["messages"][-1]
    assert question["role"] == "user"
    assert cli_request["messages"][-1]["content"][:len(question["content"])] == question["content"]
    # Replaying a runner-owned source through the CLI changes provider route.
    # DSH records that change beside the question; it isn't forked user history.
    (notice,) = cli_request["messages"][-1]["content"][len(question["content"]):]
    assert notice["type"] == "text" and re.fullmatch(
        r"\[model changed: assistant turns above this point were generated by looper-[^/]+/deepseek-flash; "
        r"the session continues with deepseek-official/deepseek-flash\]", notice["text"])
    for body in (runner_request, cli_request):
        assert body["system"].count(CONSULT_CONTRACT) == 1
        assert WORK_CONTRACT not in body["system"]
        assert consult_prompt(json.loads(source["metadata"])["system_prompt"]) in body["system"]
    assert runner_request["system"] == cli_request["system"]
    assert runner_request["tools"] == cli_request["tools"]
    assert hashes() == digest, "CLI replay must also leave the original session untouched"


def test_dsh_runner_worker_budget_cancel_and_crash(isolated_dsh, tmp_path):
    from looper.agents import proc_tree
    from types import SimpleNamespace

    url, api, provider = isolated_dsh
    counts = Counter()
    mode = "BUDGET"
    started = tmp_path / "crash-started"
    killed = False
    recovered_pids = []

    def handle(body):
        role = request_role(body)
        counts[(mode, role)] += 1
        if mode == "CRASH" and killed:
            recovered_pids.append(dsh_runner_processes(tmp_path))
        if role == "planner":
            prompt = current_prompt(body)
            if "[PLAN]" in prompt:
                return json_answer({"steps": [step("one", "STEP-" + mode)]})
            if "[RESTART]" in prompt:
                return json_answer({"reply": "Retry the unfinished step", "action": "none"})
            assert "[DECISION]" in prompt
            # A restarted runner may execute a retry; any failed result must fail the task.
            return accepted(final=True) if "Allowed decisions now: accept" in prompt else json_answer({"decision": "fail", "reason": "Worker failed"})
        assert role == "STEP-" + mode
        if mode == "CANCEL":
            provider.held.set()
            assert provider.release.wait(30), "cancelled Worker request was not released"
        if mode == "CRASH" and counts[(mode, role)] == 1:
            return tool("bash", {"command": f"touch {shlex.quote(str(started))}; sleep 60"})
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
    (pid,) = dsh_runner_processes(tmp_path)
    children = proc_tree._tree(SimpleNamespace(pid=pid, returncode=None), proc_tree._snapshot(), {})
    assert pid in children, "positive control: process snapshot must include the live runner"
    before = time.monotonic()
    api.post("/api/v1/ops/task_control/stop", {"task_id": task})
    wait_until(lambda: task_records(url, task),
        lambda rows: any(row["agent_id"].startswith("worker") and row["status"] == "interrupted" for row in rows), timeout=5)
    assert time.monotonic() - before < 5
    provider.release.set()
    assert task_ended(api, task)["status"] == "stopped"
    assert dsh_runner_processes(tmp_path) == [pid]
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
    killed = True
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
    (restarted,) = wait_until(lambda: dsh_runner_processes(tmp_path), lambda pids: len(pids) == 1 and pids[0] != pid, timeout=10)
    assert restarted != pid
    if detail["status"] == "failed":
        # Losing the shared runner may fail the idle Planner too. Exercise the
        # operator's retry so a replacement process must actually serve work.
        api.post("/api/v1/ops/task_control/restart", {"task_id": task})
        wait_until(lambda: task_records(url, task), lambda rows: any(
            row["agent_id"].startswith("worker") and row["id"] > worker["id"]
            and row["status"] == "completed" for row in rows))
        assert task_ended(api, task)["status"] == "completed"
    assert recovered_pids and all(pids == [restarted] for pids in recovered_pids)
    assert not any(row["status"] == "running" for row in task_records(url, task))
