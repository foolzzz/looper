#!/usr/bin/env python3
"""Smoke the installed branch wheel without a real model or operator state.

Run with ``uv run --extra test python examples/pi/isolated-smoke.py``.
Optionally pass --database-url (or LOOPER_TEST_DATABASE_URL), --disposable-db,
--runtime-root under the OS temporary directory, and --port (0 selects a free
port). Only a fresh local database named looper_smoke_* is accepted. The caller
owns database creation/deletion. With --fake-provider, disposable chats/tasks
exercise all four roles against a loopback-only fake Chat Completions provider.
The development harness reuses the packaging tests' build/HTTP helpers; the
server and negative startup checks run in the installed wheel's interpreter.
Missing prerequisites are recorded as unverified and return 2; failures return
1. Evidence stays in the checkout's ignored .local directory.
"""
from __future__ import annotations

import argparse
import http.client
import importlib.util
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[2]
ROLES = ("manager", "planner", "worker", "reviewer")
LAST_OUTPUT = None
_OUTPUT_SEQ = 0
PREFIX = "mcp__looper__"
PROJECT_SENTINEL = "SMOKE_PROJECT_CONTEXT_8c40"
PARENT_SENTINEL = "SMOKE_PARENT_CONTEXT_93f1"
PERSONAL_SENTINEL = "SMOKE_PERSONAL_CONTEXT_64a2"
STEER_SENTINEL = "SMOKE_STEER_701d"


@contextmanager
def fake_provider():
    """The role-tool driver's local SSE protocol, with real platform results only.

    Requests advertise the actual bridge schemas. The provider chooses a tool;
    it never fabricates its result. A long inbox call leaves a window for the
    Planner's forward; cancellation waits for a real local tool child sentinel.
    """
    state = {"requests": [], "errors": [], "worker": threading.Event(), "controls": 0,
             "cancel": threading.Event(), "ready": {role: threading.Event() for role in ROLES}}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            try:
                assert self.path == "/v1/chat/completions"
                assert self.headers.get("Authorization") == "Bearer local-key"
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                received = {"role": "control", "body": body}
                state["requests"].append(received)
                call_id = "call-" + str(len(state["requests"]))
                if body.get("smoke_control"):
                    state["controls"] += 1
                    role, delta, reason = "control", {"content": "counted"}, "stop"
                else:
                    tools = {t["function"]["name"]: t["function"] for t in body["tools"]}
                    role = ("manager" if PREFIX + "looper_projects" in tools else
                            "planner" if PREFIX + "looper_task_profiles" in tools else
                            "worker" if PREFIX + "looper_room_watch" in tools else "reviewer")
                    received["role"] = role
                    assert state["ready"][role].wait(10), "role's live scoped token was not observed"
                    users = [m["content"] if isinstance(m["content"], str) else
                             "".join(block.get("text", "") for block in m["content"])
                             for m in body["messages"] if m["role"] == "user"]
                    latest = users[-1]
                    # Pi puts the task requirement in the developer prompt. A steer arrives later,
                    # as its own user turn, and must not be required of the cancellation task.
                    transcript = json.dumps(body["messages"])
                    steered = role == "worker" and STEER_SENTINEL in latest
                    cancelling = role == "worker" and "SMOKE_CANCEL" in transcript and not steered
                    after_tool = body["messages"][-1]["role"] == "tool" or steered
                    if not after_tool:
                        name, arguments = (("looper_projects", {"action": "list"}) if role == "manager" else
                                           ("looper_task_profiles", {}) if role == "planner" else
                                           ("looper_room_roster", {}) if role == "reviewer" else
                                           ("looper_room_inbox", {"wait_seconds": 10}))
                        if cancelling:
                            script = ("require('node:fs').writeFileSync('cancel-tool.json', JSON.stringify({pid:process.pid}));"
                                      "setTimeout(() => {}, 120000)")
                            name, arguments = "bash", {"command": "node -e " + shlex.quote(script)}
                        tool = tools[name if cancelling else PREFIX + name]
                        assert tool["parameters"]["type"] == "object"
                        delta = {"tool_calls": [{"index": 0, "id": call_id,
                                  "type": "function", "function": {"name": tool["name"],
                                                                     "arguments": json.dumps(arguments)}}]}
                        reason = "tool_calls"
                        if role == "worker":
                            state["cancel" if cancelling else "worker"].set()
                    else:
                        if role == "manager":
                            reply = "SMOKE_MANAGER_DONE"
                        elif role == "worker":
                            if not cancelling:
                                assert STEER_SENTINEL in transcript, "steer absent from next model request"
                            reply = json.dumps({"status": "completed", "changed_files": {}, "deleted_files": [],
                                                "summary": "cancel tool finished" if cancelling else "steering received"})
                        elif role == "reviewer":
                            reply = json.dumps({"passed": True, "findings": [], "summary": "checked"})
                        else:
                            if latest.startswith("[PLAN]"):
                                result = {"steps": [{"id": "s1", "instruction": "Read platform state; " +
                                                       ("SMOKE_CANCEL" if "SMOKE_CANCEL" in transcript else "SMOKE_WORK"),
                                                       "acceptance_criteria": ["platform state read"],
                                                       "depends_on": [], "review": True}]}
                            elif latest.startswith("[CHAT]"):
                                result = {"reply": "forwarded", "action": "forward", "step": "s1", "text": STEER_SENTINEL}
                            elif latest.startswith("[DECISION]"):
                                result = ({"decision": "review"} if "needs a passing review" in latest else
                                          {"decision": "accept", "summary": "done", "final": {
                                              "delivered": "platform read", "how_to_use": "smoke only",
                                              "verification": "fake provider", "open_issues": "none"}})
                            else:
                                result = {"reply": "acknowledged", "action": "none"}
                            reply = "```json\n" + json.dumps(result) + "\n```"
                        delta, reason = {"content": reply}, "stop"
                chunks = [{"id": "smoke-completion", "object": "chat.completion.chunk", "created": 0,
                           "model": "selected", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                          {"id": "smoke-completion", "object": "chat.completion.chunk", "created": 0,
                           "model": "selected", "choices": [{"index": 0, "delta": {}, "finish_reason": reason}],
                           "usage": {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14}}]
                data = ("".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n").encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception as error:
                state["errors"].append(str(error))
                self.send_error(500)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state["url"] = f"http://127.0.0.1:{server.server_port}"
    try:
        # Same positive counter control as the installed-wheel cold-start helper.
        client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        try:
            client.request("POST", "/v1/chat/completions", json.dumps({"smoke_control": True}),
                           {"Authorization": "Bearer local-key", "Content-Type": "application/json"})
            response = client.getresponse()
            assert response.status == 200 and b"counted" in response.read()
            assert state["controls"] == 1
            assert len(state["requests"]) == 1
            state["requests"].clear()
        finally:
            client.close()
        yield state
    finally:
        for ready in state["ready"].values():
            ready.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def assert_role_evidence(role, evidence):
    requests, records = evidence["requests"], evidence["records"]
    assert requests and records, f"{role}: no positive provider/run control"
    assert all(record["status"] == "completed" for record in records), role
    assert sum(record["input_tokens"] for record in records) == 11 * len(requests), role
    assert sum(record["output_tokens"] for record in records) == 3 * len(requests), role
    for record in records:
        assert record["role"] == role
        assert record["agent_id"] == role or record["agent_id"].startswith(role + "-")
        assert record["model"] == "selected"
        assert record["metadata"]["profile"]["runtime"]["type"] == "pi"
    advertised = {tool["function"]["name"] for tool in requests[0]["tools"]}
    assert {PREFIX + tool["name"] for tool in evidence["catalog"]} == {name for name in advertised if name.startswith(PREFIX)}
    assert evidence["scope_denied"] is True
    name = {"manager": "looper_projects", "planner": "looper_task_profiles",
            "worker": "looper_room_inbox", "reviewer": "looper_room_roster"}[role]
    assert PREFIX + name in evidence["tool_names"], role
    if role in ("manager", "planner"):
        assert len(records) >= 2 and len({record["session_id"] for record in records}) == 1
        assert records[0]["session_id"]
        assert len([m for m in requests[-1]["messages"] if m["role"] == "user"]) >= 2
    if role == "planner":
        assert advertised - {name for name in advertised if name.startswith(PREFIX)} == {"read", "grep", "find", "ls"}
        assert all(record["metadata"]["read_only"] == "tools" for record in records)
    if role == "worker":
        assert any(STEER_SENTINEL in json.dumps(body["messages"]) for body in requests[1:])
    if role != "manager":
        assert PROJECT_SENTINEL in json.dumps(requests[0]["messages"])
        assert all(any(item["path"] == "AGENTS.md" and item["delivery"] == "injected"
                       for item in record["metadata"]["project_instructions"]) for record in records)
    assert all(PARENT_SENTINEL not in json.dumps(body) and PERSONAL_SENTINEL not in json.dumps(body) for body in requests)


def session_smoke(base, token, root, work, output, provider, *, helper, installed):
    """Drive the installed service, including resident factories and Worker RPC."""
    evidence = {"roles": {}, "boundaries": {}, "provider_control": provider["controls"]}
    grants = {}
    seen = set()

    def collect_grants():
        for path in (root / ".local/mcp").glob("*.json"):
            if path in seen:
                continue
            try:
                scoped = json.loads(path.read_text())["mcpServers"]["looper"]["env"]["LOOPER_AGENT_TOKEN"]
                catalog = request(base, "/api/agent/v1/tools", scoped)["tools"]
            except (OSError, KeyError):
                continue  # A completed run may already have revoked its token.
            names = {tool["name"] for tool in catalog}
            role = ("manager" if "looper_projects" in names else "planner" if "looper_task_profiles" in names else
                    "worker" if "looper_room_watch" in names else "reviewer")
            foreign, arguments = (("looper_vault_read", {"path": "note.md"}) if role == "manager" else
                                   ("looper_projects", {"action": "list"}))
            refused = request(base, "/api/agent/v1/ops/" + foreign, scoped, "POST", arguments)
            assert refused["status"] == "error" and refused["error"]["code"] in ("invalid", "forbidden")
            grants[role] = {"catalog": catalog, "scope_denied": True, "token": scoped}
            seen.add(path)
            provider["ready"][role].set()

    def wait(check, description, timeout=90):
        deadline = time.monotonic() + timeout
        while True:
            collect_grants()
            assert not provider["errors"], provider["errors"]
            value = check()
            if value:
                return value
            if time.monotonic() >= deadline:
                raise TimeoutError(description)
            time.sleep(0.05)

    def records(**filters):
        rows = request(base, "/api/v1/runs/list", token, "POST", {"limit": 500, **filters})["result"]["records"]
        # The list endpoint filters by role but returns the persisted agent id.
        return [{**row, "role": next(role for role in ROLES if row["agent_id"] == role or
                                     row["agent_id"].startswith(role + "-"))} for row in rows]

    def snapshot_role(role, selected):
        tool_names = []
        for record in selected:
            events = request(base, "/api/v1/runs/events", token, "POST", {"record_id": record["id"], "limit": 5000})["result"]["events"]
            for row in events:
                event = row["event"]
                if event.get("type") == "assistant":
                    tool_names.extend(block["name"] for block in event["message"]["content"]
                                      if block.get("type") == "tool_use")
        grant = grants[role]
        evidence["roles"][role] = {"requests": [r["body"] for r in provider["requests"] if r["role"] == role],
                                  "records": selected, "catalog": grant["catalog"],
                                  "scope_denied": grant["scope_denied"], "tool_names": tool_names}
        assert_role_evidence(role, evidence["roles"][role])

    def send(text, role="manager", task_id=None):
        target = {"kind": role, **({"task_id": task_id} if task_id else {})}
        return request(base, "/api/chat/send", token, "POST", {"text": text, "session_id": "smoke-" + role,
                       "message_id": "smoke-" + str(time.monotonic_ns()), "target": target})

    def task(marker):
        task_id = request(base, "/api/v1/tasks/create", token, "POST", {
            "project_id": project_id, "requirement": marker})["result"]["task_id"]
        request(base, "/api/v1/ops/task_control/start", token, "POST", {"task_id": task_id})
        return task_id

    try:
        # This is the actual service's agent API; an operator token cannot impersonate an agent either.
        before = len(provider["requests"])
        for wrong in ("incorrect-local-token", token):
            try:
                request(base, "/api/agent/v1/tools", wrong)
            except urllib.error.HTTPError as error:
                assert error.code == 401
            else:
                raise AssertionError("wrong agent token accepted")
        assert len(provider["requests"]) == before
        evidence["boundaries"]["wrong-token"] = True
        for turn in (1, 2):
            send(f"SMOKE_MANAGER_TURN_{turn}: list projects")
            wait(lambda: len([r for r in records(agent_id="manager") if r["status"] == "completed"]) >= turn,
                 "Manager turn did not complete")
        wait(lambda: "SMOKE_MANAGER_DONE" in json.dumps(request(base, "/api/chat/messages?session_id=smoke-manager", token)),
             "Manager reply was not persisted in chat")
        snapshot_role("manager", records(agent_id="manager"))
        project_work = work / "project"
        project_work.mkdir()
        (project_work / "AGENTS.md").write_text(PROJECT_SENTINEL)
        (work / "AGENTS.md").write_text(PARENT_SENTINEL)
        project_id = request(base, "/api/projects", token, "POST", {
            "name": "isolated-session-smoke", "work_dir": str(project_work)})["project_id"]
        task_id = task("SMOKE_WORK")
        wait(lambda: provider["worker"].is_set(), "Worker never reached local provider")
        send(STEER_SENTINEL, "planner", task_id)
        detail = wait(lambda: (value if (value := request(base, "/api/tasks/" + task_id, token))["status"]
                              in ("completed", "failed", "cancelled", "stopped") else None), "task did not finish")
        assert detail["status"] == "completed", detail["status"]
        # Capture completed records before the cancellation task adds any more.
        wait(lambda: all(r["status"] != "running" for r in records(task_id=task_id)), "task left active runs")
        completed = records(task_id=task_id)
        for role in ("planner", "worker", "reviewer"):
            selected = [r for r in completed if r["role"] == role]
            snapshot_role(role, selected)
        evidence["boundaries"]["parent-context"] = True
        cancelled = task("SMOKE_CANCEL")
        wait(lambda: provider["cancel"].is_set(), "cancel tool was never requested")
        # A tool-created child sentinel proves execution, beyond the model's tool_use.
        tool_child = project_work / "cancel-tool.json"
        def tool_started():
            if not tool_child.is_file():
                return False
            for run in records(task_id=cancelled):
                if run["role"] == "worker" and run["status"] == "running":
                    events = request(base, "/api/v1/runs/events", token, "POST", {"record_id": run["id"]})["result"]["events"]
                    if any(block.get("type") == "tool_use" and block.get("name") == "bash"
                           for row in events for block in row["event"].get("message", {}).get("content", [])
                           if isinstance(block, dict)):
                        return True
            return False
        wait(tool_started, "cancellation did not reach a live tool")
        process_files = list((root / ".local/agent-processes").glob("*.json"))
        leaders = [int(path.stem) for path in process_files]
        assert leaders, "no running process positive control before cancellation"
        _, python, env = installed
        snapshot = helper._run([str(python), "-I", "-c", '''
import json, sys
from types import SimpleNamespace
from looper.agents import proc_tree
table = proc_tree._snapshot()
assert table, 'process listing unavailable: descendant cleanup cannot be verified'
tree = {}
for pid in sys.argv[1:]:
    tree.update(proc_tree._tree(SimpleNamespace(pid=int(pid), returncode=None), table, {}))
assert tree, 'no live tree before cancellation'
print(json.dumps({'pids': list(tree), 'groups': list(set(tree.values()))}))
''', *map(str, leaders)], cwd=work, env=env)
        tree = json.loads(snapshot.stdout)
        assert json.loads(tool_child.read_text())["pid"] in tree["pids"], "tool child missing from live tree"
        evidence["cancel_tree"] = tree
        cancelled_token = grants["worker"]["token"]
        count_before = len([r for r in provider["requests"] if r["role"] == "worker"])
        request(base, "/api/v1/ops/task_control/cancel", token, "POST", {"task_id": cancelled})
        wait(lambda: request(base, "/api/tasks/" + cancelled, token)["status"] == "cancelled", "task not cancelled")
        wait(lambda: all(r["status"] != "running" for r in records()), "cancel left running records")
        def reaped():
            for pid in tree["pids"]:
                try:
                    os.kill(pid, 0)
                    return False
                except ProcessLookupError:
                    pass
            for group in tree["groups"]:
                try:
                    os.killpg(group, 0)
                    return False
                except ProcessLookupError:
                    pass
            return not list((root / ".local/agent-processes").glob("*.json"))
        wait(reaped, "cancel left processes", timeout=60)
        try:
            request(base, "/api/agent/v1/tools", cancelled_token)
        except urllib.error.HTTPError as error:
            assert error.code == 401
        else:
            raise AssertionError("cancel left a live run token")
        assert len([r for r in provider["requests"] if r["role"] == "worker"]) == count_before
        evidence["boundaries"]["cancel-tool"] = True
    finally:
        # Persist partial results on timeout/failure too; never infer a pass from missing evidence.
        evidence["provider_requests"] = provider["requests"]
        evidence["provider_errors"] = provider["errors"]
        (output / "sessions.json").write_text(json.dumps(evidence, indent=2, default=str))


@contextmanager
def managed_process(command, *, cwd, env, grace=3, **kwargs):
    """Always reap the child and terminate its entire isolated process group."""
    process = subprocess.Popen(command, cwd=cwd, env=env, start_new_session=True, **kwargs)
    try:
        yield process
    finally:
        # Descendants can outlive the leader, so signal the group even if it exited.
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                break
            if sig == signal.SIGTERM:
                try:
                    process.wait(timeout=grace)
                except subprocess.TimeoutExpired:
                    pass
        process.wait(timeout=5)


def validate(parser, args):
    if args.port == 8767 or not 0 <= args.port < 65536:
        parser.error("port must be 0 or an unused non-8767 TCP port")
    if args.runtime_root:
        root = Path(args.runtime_root).resolve()
        temps = (Path(tempfile.gettempdir()).resolve(), Path("/tmp").resolve())
        if not any(root != temp and root.is_relative_to(temp) for temp in temps):
            parser.error("runtime root must be inside an OS temporary directory")
        if root.exists() and (not root.is_dir() or any(root.iterdir())):
            parser.error("runtime root must be new or empty")
    if args.database_url:
        try:
            dsn = urlsplit(args.database_url)
            valid = (dsn.scheme in ("postgres", "postgresql") and dsn.hostname in ("localhost", "127.0.0.1")
                     and re.fullmatch(r"looper_smoke_[a-zA-Z0-9_]+", unquote(dsn.path.lstrip("/")))
                     and not dsn.query and not dsn.fragment and dsn.port != 8767)
        except ValueError:
            valid = False
        if not args.disposable_db or not valid:
            parser.error("DB must be explicitly marked --disposable-db and use a local looper_smoke_* database")


def support():
    spec = importlib.util.spec_from_file_location("pi_packaging_support", ROOT / "tests/test_pi_packaging.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def request(base, path, token, method="GET", data=None):
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
    req = urllib.request.Request(base + path, headers=headers, method=method,
                                 data=json.dumps(data).encode() if data is not None else None)
    # Never route local smoke traffic through an ambient proxy or follow redirects.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_args, **_kwargs):
            return None
    with urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect()).open(req, timeout=3) as response:
        return json.load(response)


def registry_smoke(helper, installed, root, port, dsn, output, provider_url, *, provider_state=None):
    work, python, env = installed
    # Refuse an initialized DB before serve can resume anything or enable a channel.
    helper._run([str(python), "-I", "-c", '''
import asyncio, os, asyncpg
async def check():
    conn = await asyncpg.connect(os.environ['SMOKE_DSN'], timeout=5)
    try:
        count = await conn.fetchval("SELECT count(*) FROM information_schema.tables WHERE table_schema NOT IN ('pg_catalog','information_schema')")
        assert count == 0, 'smoke requires a fresh empty database'
    finally:
        await conn.close()
asyncio.run(check())
'''], cwd=work, env={**env, "SMOKE_DSN": dsn})
    root.mkdir(parents=True, exist_ok=True)
    ops = root / "ops"
    ops.mkdir(mode=0o700)
    config = ops / "server.toml"
    config.write_text('[database]\nurl = ' + json.dumps(dsn) + '\n')
    config.chmod(0o600)
    # A minimal PATH prevents startup from probing the operator's other CLIs.
    bin_dir = work / "safe-bin"
    bin_dir.mkdir()
    for name in ("pi", "node", "git"):
        binary = shutil.which(name)
        if binary:
            (bin_dir / name).symlink_to(Path(binary).resolve())
    env = {**env, "PATH": str(bin_dir) + ":/usr/bin:/bin"}
    if provider_state is not None:
        # Poison only the disposable HOME, never the operator's configuration.
        for name in (".pi/agent/AGENTS.md", ".claude/CLAUDE.md", ".codex/AGENTS.md", ".grok/AGENTS.md"):
            path = Path(env["HOME"]) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(PERSONAL_SENTINEL)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", port))
        port = sock.getsockname()[1]
    if port == 8767:
        raise RuntimeError("free port selection returned forbidden 8767")
    base = f"http://127.0.0.1:{port}"
    with (output / "serve.log").open("w") as log, managed_process(
        [str(python.parent / "looper"), "serve", "--host", "127.0.0.1", "--port", str(port),
         "--runtime-root", str(root)], cwd=work, env=env, stdout=log, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
    ) as process:
        deadline = time.monotonic() + 90
        while True:
            if process.poll() is not None:
                raise RuntimeError("installed serve exited before readiness; see private serve.log")
            try:
                token = (root / "operator-token").read_text().strip()
                status = request(base, "/api/v1/system/status", token, "POST", {})
                assert status["status"] == "ok" and status["result"]["status"] == "running", status
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise TimeoutError("installed serve readiness timed out")
                time.sleep(0.2)
        channels = request(base, "/api/channel-configs", token)["configs"]
        assert {c["channel_type"] for c in channels} >= {"telegram", "lark", "lark_bot"}
        assert all(not c["enabled"] for c in channels if c["channel_type"] in ("telegram", "lark", "lark_bot"))
        probe = request(base, "/api/runtimes/rt-pi/probe", token, "POST", {})
        assert not probe["runtime"]["probe_error"], "pi probe failed"
        provider = request(base, "/api/providers", token, "POST", {
            "name": "isolated-smoke", "vendor_type": "custom",
            "endpoints": [{"protocol": "openai-chat" if provider_state else "openai-responses",
                           "base_url": provider_url + "/v1"}], "api_key": "local-key",
        })["provider"]
        model = request(base, "/api/models", token, "POST", {
            "provider_id": provider["provider_id"], "name": "selected",
            "protocol": "openai-chat" if provider_state else "openai-responses",
        })["model"]
        profile = request(base, "/api/agent-profiles", token, "POST", {
            "name": "isolated-smoke", "runtime_id": "rt-pi", "model_id": model["model_id"],
            "agent_teams": False,
        })["profile"]
        profile_id = profile["profile_id"]
        for role in ROLES:
            request(base, f"/api/agent-settings/roles/{role}", token, "PUT", {"profile_id": profile_id})
        settings = request(base, "/api/agent-settings", token)
        for role in ROLES:
            assert settings["roles"][role]["values"]["profile_id"] == profile_id
            assert settings["roles"][role]["effective"]["profile"]["value"]["profile_id"] == profile_id
        registry = request(base, "/api/registry", token)
        saved = next(p for p in registry["profiles"] if p["profile_id"] == profile_id)
        assert saved["runtime_type"] == "pi"
        assert set(saved["roles"]) == set(ROLES)
        assert {layer["role"] for layer in saved["used_by"]["layers"]} == set(ROLES)
        (output / "registry.json").write_text(json.dumps({"profile": saved, "probe": probe}, indent=2, default=str))
        if provider_state is not None:
            session_smoke(base, token, root, work, output, provider_state, helper=helper, installed=installed)


def configuration_smoke(helper, installed, root, url):
    work, python, env = installed
    return helper._run([str(python), "-I", "-c", '''
import sys
from pathlib import Path
from looper.agents.agent_settings import AgentSettings, Launch
from looper.agents.library import AgentLibrary
from looper.agents.profile_registry import (Endpoint, ModelSpec, ProviderSpec, RegistryError,
    ResolvedAgentProfile, ResolvedModel, RuntimeSpec)
from looper.agents.runtimes import ApiProtocol, AuthKind, RuntimeUnsupported, VendorType
from looper.agents.runtimes.pi import PiRuntime
root, url, executable = sys.argv[1:]
prompt = Path(root) / '.local/prompts/system.md'
prompt.parent.mkdir(parents=True)
prompt.write_text('Startup validation only')
for protocol, key, expected in [(ApiProtocol.OPENAI_RESPONSES, None, 'no API key'),
                                (ApiProtocol.ANTHROPIC_MESSAGES, 'local-key', 'protocol')]:
    provider = ProviderSpec('local', 'local', VendorType.CUSTOM, AuthKind.API_KEY, api_key=key)
    model = ModelSpec('selected-model', provider.id, 'selected', protocol)
    resolved = ResolvedModel(model, provider, Endpoint(provider.id, protocol, url + '/v1'))
    profile = ResolvedAgentProfile('pi-test', 'pi-test', RuntimeSpec('rt-pi', 'pi', 'pi', executable),
                                   model.id, resolved_model=resolved)
    launch = Launch(AgentSettings(profile=profile), AgentLibrary())
    try:
        PiRuntime().run_command(executable, cwd=Path(root), prompt_file=prompt, launch=launch)
    except (RuntimeUnsupported, RegistryError) as error:
        assert expected in str(error), str(error)
    else:
        raise AssertionError('invalid provider reached process launch')
print('missing key and unsupported protocol rejected')
''', str(root), url, shutil.which("pi")], cwd=work, env=env)


def smoke(args, root, output, summary):
    if not shutil.which("uv"):
        summary["unverified"].append("uv not installed: wheel build and installation")
        return
    helper = support()
    work = root / "wheel"
    work.mkdir()
    installed = helper.install_wheel(work)
    _, python, env = installed
    located = helper._run([str(python), "-I", "-c", '''
import json, sys, looper
from looper.agents.runtimes.pi import bridge_extension
from looper.db.connection import SCHEMA_PATH
assert bridge_extension().is_file() and SCHEMA_PATH.is_file()
print(json.dumps({'sys.executable': sys.executable, 'looper.__file__': looper.__file__,
                  'extension': str(bridge_extension()), 'schema': str(SCHEMA_PATH)}))
'''], cwd=work, env=env)
    origin = json.loads(located.stdout)
    assert Path(origin["looper.__file__"]).is_relative_to(python.parent.parent)
    assert Path(origin["sys.executable"]) == python
    (output / "origin.json").write_text(json.dumps(origin, indent=2))
    summary["passed"].append("wheel installation and installed resource provenance")
    if not args.database_url:
        summary["unverified"].append("no disposable DB: installed serve and four-role registry readback")
        if args.fake_provider:
            summary["unverified"].append("fake-provider sessions, steering and cancellation require a disposable DB")
    if not shutil.which("pi"):
        summary["unverified"].append("pi not installed: safe probe and startup checks")
        return
    probed = helper._run([str(python), "-I", "-c", '''
import sys
from looper.agents.runtimes.pi import PiRuntime
probe = PiRuntime().probe(sys.argv[1])
assert not probe.error, probe.error
print(probe.version)
''', shutil.which("pi")], cwd=work, env=env)
    (output / "probe.txt").write_text(probed.stdout)
    summary["passed"].append("safe isolated pi version/help probe")
    configuration_smoke(helper, installed, root / "negative", "http://127.0.0.1:1")
    summary["passed"].append("missing key and unsupported protocol rejected before launch")
    # The helper's positive control proves a failed launch cannot hide requests.
    with helper.cold_start_api() as api:
        url = f"http://127.0.0.1:{api.server_port}"
        configuration_smoke(helper, installed, root / "counted-negative", url)
        assert not api.state["requests"]
        summary["passed"].append("invalid providers rejected; model requests = 0")
        # Deliberately bind the wrong token to a local endpoint that checks it.
        api.state["token"] = "correct-local-token"
        for mode in ("json", "rpc"):
            launch_root = root / ("wrong-token-" + mode)
            command, launch_env = helper._launch(installed, launch_root, api, mode)
            with managed_process(command, cwd=launch_root, env=launch_env, stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
                prompt = "test" if mode == "json" else json.dumps({"type": "prompt", "id": "p", "message": "test"}) + "\n"
                stdout, stderr = process.communicate(prompt, timeout=120)
                assert process.returncode != 0 and "HTTP 401" in stderr
                assert "scoped-test-token" not in stdout + stderr
            assert not api.state["requests"]
        assert len(api.state["gets"]) == 2
        summary["passed"].append("wrong bridge token rejected in JSON/RPC; model requests = 0")
        if args.database_url:
            runtime_root = Path(args.runtime_root) if args.runtime_root else root / "runtime"
            if args.fake_provider:
                with fake_provider() as provider:
                    registry_smoke(helper, installed, runtime_root, args.port, args.database_url, output,
                                   provider["url"], provider_state=provider)
                summary["passed"].append("installed four-role fake-provider sessions, scoped tools, usage, steering and cancellation")
            else:
                registry_smoke(helper, installed, runtime_root, args.port, args.database_url, output, url)
            assert not api.state["requests"]
            summary["passed"].append("installed serve, disabled channels and four-role registry readback; model requests = 0")


def main(argv=None):
    global LAST_OUTPUT, _OUTPUT_SEQ
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=os.environ.get("LOOPER_TEST_DATABASE_URL"))
    parser.add_argument("--disposable-db", action="store_true")
    parser.add_argument("--runtime-root")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--fake-provider", action="store_true", help="exercise installed sessions using only a local fake provider")
    args = parser.parse_args(argv)
    validate(parser, args)
    # Second resolution plus pid collides when one process calls main twice in the same second.
    _OUTPUT_SEQ += 1
    output = ROOT / ".local" / f"pi-smoke-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}-{_OUTPUT_SEQ}"
    output.mkdir(parents=True, mode=0o700)
    LAST_OUTPUT = output
    summary = {"mode": "fake-provider" if args.fake_provider else "startup", "passed": [], "failed": [], "unverified": []}

    def expired(*_args):
        raise TimeoutError("25 minute smoke deadline exceeded")

    def interrupted(*_args):
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGALRM, expired)
    previous_term = signal.signal(signal.SIGTERM, interrupted)
    signal.alarm(25 * 60)
    try:
        with tempfile.TemporaryDirectory(prefix="looper-pi-smoke-") as temporary:
            smoke(args, Path(temporary), output, summary)
    except PermissionError as error:
        (output / "environment.log").write_text(str(error))
        summary["unverified"].append("sandbox denied local listener/process access: HTTP model counter, wrong-token JSON/RPC startup and registry smoke")
        if args.fake_provider:
            summary["unverified"].append("installed fake-provider sessions did not complete")
    except KeyboardInterrupt:
        summary["failed"].append("interrupted; subprocess cleanup completed")
    except Exception as error:
        # Raw exceptions can contain DSNs or subprocess output; retain them privately.
        (output / "failure.log").write_text(str(error))
        summary["failed"].append(type(error).__name__ + ": see private failure.log")
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
        signal.signal(signal.SIGTERM, previous_term)
        (output / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    print("Evidence: " + str(output.relative_to(ROOT)))
    return 1 if summary["failed"] else 2 if summary["unverified"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
